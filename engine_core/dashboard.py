"""Live dashboard — what the AI planner and each specialist agent did, tokens used, and
the cost saved versus running the same tokens on a paid baseline model.

    engine-ai runtime dashboard [--port 8787] [--baseline-in 3 --baseline-out 15]

Stdlib only. Reads the files the pipeline already writes per workspace
(`builds-state/<tag>-<hash>/{status.json,progress.jsonl,report.json,plan.json}`),
rebuilds a snapshot when any of them change, and pushes it to every open browser over
Server-Sent Events (`/events`). `/api/snapshot` returns the same JSON for scripts.

"Saved" is an ESTIMATE: (tokens × baseline $/Mtok) − actual provider cost. The engine
routes to free models, so actual cost is usually $0. The baseline price is a flag, not a
fact about any vendor — set it to whatever model you would otherwise have paid for.
Only the engine's own provider calls are counted; the host assistant's usage is not
visible here.
"""

from __future__ import annotations

import glob
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FEED_LIMIT = 250
POLL_S = 1.0


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a line being written right now
    except OSError:
        pass
    return out


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _describe(ev: dict) -> str:
    """One human sentence per progress event."""
    e, t = ev.get("event"), ev.get("task", "")
    if e == "plan-start":
        return f"Planner started decomposing the objective for {ev.get('project', '?')}"
    if e == "plan-end":
        return f"Planner produced {len(ev.get('tasks') or [])} tasks: {', '.join(ev.get('tasks') or [])}"
    if e == "task-start":
        return f"{ev.get('profile', 'agent')} agent started '{t}' on {ev.get('model', '?')} (attempt {ev.get('attempt', 1)})"
    if e == "turn":
        return f"'{t}' — model turn {ev.get('turn')}"
    if e == "tool":
        return f"'{t}' called {ev.get('tool')} → {'ok' if ev.get('ok') else 'failed'}"
    if e == "usage":
        return (f"'{t}' used {ev.get('input_tokens', 0):,} in / {ev.get('output_tokens', 0):,} out tokens"
                f" on {ev.get('model', '?')}")
    if e == "nudge":
        return f"'{t}' nudged ({ev.get('reason')})"
    if e == "task-end":
        err = f" — {ev['error']}" if ev.get("error") else ""
        return f"'{t}' finished: {ev.get('status')}{err}"
    if e == "task-skip":
        return f"'{t}' skipped ({ev.get('status')}): {str(ev.get('reason', ''))[:160]}"
    if e == "run-end":
        return f"Run ended {ev.get('status')}: {ev.get('complete')}/{ev.get('total')} tasks complete"
    if e == "resume-start":
        return f"Resumed plan from {ev.get('planned_by_run')}"
    return e or "event"


class Collector:
    def __init__(self, home: str, baseline_in: float, baseline_out: float):
        self.root = os.path.join(home, "builds-state")
        self.bin, self.bout = baseline_in, baseline_out

    def signature(self) -> tuple:
        sig = []
        for p in sorted(glob.glob(os.path.join(self.root, "*", "*"))):
            if p.endswith((".json", ".jsonl")):
                try:
                    st = os.stat(p)
                    sig.append((p, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        # liveness changes without a file write (a crashed build) must also refresh
        return tuple(sig), int(time.time() // 5)

    def _baseline(self, i: int, o: int) -> float:
        return i / 1e6 * self.bin + o / 1e6 * self.bout

    def snapshot(self) -> dict:
        runs, feed, series, agents = [], [], [], {}
        for d in sorted(glob.glob(os.path.join(self.root, "*"))):
            if not os.path.isdir(d):
                continue
            status = _read_json(os.path.join(d, "status.json")) or {}
            report = _read_json(os.path.join(d, "report.json")) or {}
            plan = _read_json(os.path.join(d, "plan.json")) or {}
            events = _read_jsonl(os.path.join(d, "progress.jsonl"))
            if not (status or events or report):
                continue
            name = os.path.basename(d)
            project = plan.get("project") or report.get("project") or next(
                (e.get("project") for e in events if e.get("project")), name)

            tok_in = tok_out = calls = tools = 0
            cost = 0.0
            task_profile, task_model = {}, {}
            for ev in events:
                kind, tid = ev.get("event"), ev.get("task")
                if kind == "task-start":
                    task_profile[tid], task_model[tid] = ev.get("profile", "?"), ev.get("model", "?")
                    a = agents.setdefault(ev.get("profile", "?"), _agent())
                    a["models"].add(ev.get("model", "?"))
                    a["started"] += 1
                elif kind == "task-end":
                    a = agents.setdefault(task_profile.get(tid, "?"), _agent())
                    a["done" if ev.get("status") == "done" else "failed"] += 1
                    a["last"] = f"{tid}: {ev.get('status')}"
                elif kind == "tool":
                    tools += 1
                    agents.setdefault(task_profile.get(tid, "?"), _agent())["tools"] += 1
                elif kind == "usage":
                    i, o = int(ev.get("input_tokens") or 0), int(ev.get("output_tokens") or 0)
                    c = float(ev.get("cost_usd") or 0)
                    tok_in, tok_out, calls, cost = tok_in + i, tok_out + o, calls + 1, cost + c
                    prof = "planner" if tid == "(planner)" else task_profile.get(tid, "?")
                    a = agents.setdefault(prof, _agent())
                    a["in"] += i
                    a["out"] += o
                    a["calls"] += 1
                    a["cost"] += c
                    series.append((ev.get("ts", 0), i + o, c, self._baseline(i, o)))
                if kind != "turn":
                    feed.append({"ts": ev.get("ts", 0), "project": project, "event": kind,
                                 "task": tid, "text": _describe(ev)})

            # Runs from before live usage events existed: fall back to the final report.
            live_usage = calls > 0
            if not live_usage and report.get("usage"):
                tot = report["usage"].get("total", {})
                tok_in, tok_out = int(tot.get("input_tokens", 0)), int(tot.get("output_tokens", 0))
                calls = int(tot.get("model_calls", 0))
                tools = tools or int(report["usage"].get("workers", {}).get("tool_calls", 0))
                for t in report.get("tasks", []):
                    u = t.get("usage") or {}
                    a = agents.setdefault(t.get("profile", "?"), _agent())
                    a["in"] += int(u.get("input_tokens", 0))
                    a["out"] += int(u.get("output_tokens", 0))
                    a["calls"] += int(u.get("model_calls", 0))
                    if t.get("model"):
                        a["models"].add(t["model"])
                if tok_in or tok_out:
                    series.append((status.get("updated_at") or 0, tok_in + tok_out, 0.0,
                                   self._baseline(tok_in, tok_out)))

            state = status.get("state") or report.get("status") or "unknown"
            if state == "running" and not _alive(status.get("pid")):
                state = "stale"  # the process is gone but never wrote run-end
            task_states = dict(status.get("tasks") or {})
            for t in report.get("tasks", []):
                task_states.setdefault(t["id"], t.get("status"))
            for tid in plan.get("tasks", []) if isinstance(plan.get("tasks"), list) else []:
                key = tid.get("id") if isinstance(tid, dict) else tid
                if key:
                    task_states.setdefault(key, "pending")
            if state != "running":  # a dead build's "running" task never finished
                task_states = {k: ("interrupted" if v == "running" else v) for k, v in task_states.items()}
            base = self._baseline(tok_in, tok_out)
            runs.append({
                "id": name, "run_id": status.get("run_id") or report.get("run_id"),
                "project": project, "objective": plan.get("objective") or report.get("objective"),
                "workspace": status.get("workspace") or report.get("workspace"),
                "state": state, "started_at": status.get("started_at"),
                "updated_at": status.get("updated_at") or (events[-1]["ts"] if events else None),
                "current_task": status.get("current_task") if state == "running" else None,
                "tasks": [{"id": k, "status": v, "profile": task_profile.get(k),
                           "model": task_model.get(k)} for k, v in task_states.items()],
                "input_tokens": tok_in, "output_tokens": tok_out, "model_calls": calls,
                "tool_calls": tools, "cost_usd": round(cost, 6), "baseline_usd": round(base, 4),
                "saved_usd": round(base - cost, 4), "usage_source": "live" if live_usage else "report",
            })

        runs.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
        feed.sort(key=lambda f: f["ts"], reverse=True)
        series.sort()
        cum, acc = [], [0, 0.0, 0.0]
        for ts, tok, c, b in series:
            acc = [acc[0] + tok, acc[1] + c, acc[2] + b]
            cum.append({"ts": ts, "tokens": acc[0], "cost": round(acc[1], 6), "baseline": round(acc[2], 4)})
        tin = sum(r["input_tokens"] for r in runs)
        tout = sum(r["output_tokens"] for r in runs)
        cost = sum(r["cost_usd"] for r in runs)
        base = self._baseline(tin, tout)
        return {
            "generated_at": time.time(),
            "baseline": {"input_per_mtok": self.bin, "output_per_mtok": self.bout},
            "totals": {"runs": len(runs), "active": sum(r["state"] == "running" for r in runs),
                       "input_tokens": tin, "output_tokens": tout,
                       "model_calls": sum(r["model_calls"] for r in runs),
                       "tool_calls": sum(r["tool_calls"] for r in runs),
                       "tasks_done": sum(t["status"] == "done" for r in runs for t in r["tasks"]),
                       "tasks_total": sum(len(r["tasks"]) for r in runs),
                       "cost_usd": round(cost, 6), "baseline_usd": round(base, 4),
                       "saved_usd": round(base - cost, 4),
                       "saved_pct": round(100 * (base - cost) / base, 1) if base else 0.0},
            "agents": [{"profile": k, **{kk: (sorted(v) if isinstance(v, set) else v) for kk, v in a.items()},
                        "baseline_usd": round(self._baseline(a["in"], a["out"]), 4)}
                       for k, a in sorted(agents.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))],
            "runs": runs, "feed": feed[:FEED_LIMIT], "series": cum,
        }


def _agent() -> dict:
    return {"models": set(), "started": 0, "done": 0, "failed": 0, "tools": 0,
            "calls": 0, "in": 0, "out": 0, "cost": 0.0, "last": ""}


class Hub:
    """Watches the files and broadcasts a fresh snapshot to subscribers on change."""

    def __init__(self, collector: Collector):
        self.c = collector
        self.latest = json.dumps(collector.snapshot(), default=str)
        self.subs: set[queue.Queue] = set()
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        sig = self.c.signature()
        while True:
            time.sleep(POLL_S)
            try:
                new = self.c.signature()
                if new == sig:
                    continue
                sig = new
                self.latest = json.dumps(self.c.snapshot(), default=str)
            except Exception:  # noqa: BLE001 — never let a bad file kill the watcher
                continue
            with self.lock:
                for q in list(self.subs):
                    q.put(self.latest)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


def make_handler(hub: Hub):
    page = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "rb").read()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/snapshot":
                return self._send(200, hub.latest.encode(), "application/json")
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = hub.subscribe()
                try:
                    data = hub.latest
                    while True:
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                        data = None
                        while data is None:
                            try:
                                data = q.get(timeout=15)
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.unsubscribe(q)
                return
            self._send(404, b"not found", "text/plain")

    return H


def serve(home: str, host: str = "127.0.0.1", port: int = 8787,
          baseline_in: float = 3.0, baseline_out: float = 15.0) -> None:
    hub = Hub(Collector(home, baseline_in, baseline_out))
    srv = ThreadingHTTPServer((host, port), make_handler(hub))
    srv.daemon_threads = True
    print(f"engine-ai dashboard → http://{host}:{port}  (live via SSE; Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
