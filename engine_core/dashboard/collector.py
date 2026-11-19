"""Aggregate engine-ai's on-disk records into one dashboard snapshot.

Read-only. Every number comes from a file engine-ai already writes — nothing is invented:

  agents (engine workers)  builds-state/<ws>/progress.jsonl  plan/task/turn/tool/usage events
                           builds-state/<ws>/report.json     final per-task usage (older runs)
                           build-runs/<run_id>.json          pid, objective, start time
                           usage.db                          standalone `engine_core run` calls
  the AI (host assistant)  activity.jsonl                    every engine-ai MCP tool it called
                           memory.db / sessions.db           memory it saved, app sessions
  pricing                  models.json                       the cached OpenRouter catalog

Token cost SAVED is an estimate with a stated method: the tokens the engine's agents
processed, priced at a reference paid model from the catalog (default Claude Sonnet),
minus what the provider actually charged (free routes report $0). The host assistant's
own token usage is not visible to engine-ai and is never claimed.

Secrets are never read: no vault table, no master key, no tool arguments.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import time

DEFAULT_BASELINE = "anthropic/claude-sonnet-5.5"
BASELINE_FALLBACKS = ("anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-5",
                      "anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.5")
COMPARE_MODELS = ("anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5",
                  "anthropic/claude-opus-5.5")
FEED_LIMIT = 200
RUN_LIMIT = 40
STOP_STATES = ("complete", "failed", "blocked")
TERMINAL_TASK = ("done", "not-run")
QUIET_EVENTS = ("turn", "usage")  # counted, but too chatty for the activity feed


def _alive(pid) -> bool | None:
    if not pid:
        return None
    try:
        from engine_core.mcp_build import _alive as alive
        return alive(int(pid))
    except Exception:  # noqa: BLE001
        return None


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


class Pricing:
    """USD per token, from the cached OpenRouter catalog (models.json)."""

    def __init__(self, home: str):
        data = _read_json(os.path.join(home, "models.json")) or {}
        self.models = {m["id"]: m for m in data.get("models", []) if isinstance(m, dict) and m.get("id")}
        self.refreshed_at = data.get("refreshed_at")

    def price(self, model: str) -> tuple[float, float] | None:
        m = self.models.get(model)
        if not m:
            return None
        return float(m.get("pricing_prompt") or 0.0), float(m.get("pricing_completion") or 0.0)

    def is_free(self, model: str) -> bool:
        m = self.models.get(model)
        return ":free" in (model or "") or bool(m and m.get("free"))

    def cost(self, model: str, tin: int, tout: int) -> float | None:
        p = self.price(model)
        return None if p is None else tin * p[0] + tout * p[1]

    def name(self, model: str) -> str:
        return (self.models.get(model) or {}).get("name") or model


def _empty_task(tid: str) -> dict:
    return {"id": tid, "profile": None, "model": None, "status": "pending", "attempts": 0,
            "turns": 0, "tool_calls": 0, "tool_failures": 0, "tools": {}, "model_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "error": "",
            "started_at": None, "ended_at": None, "last_action": ""}


class Collector:
    def __init__(self, home: str, baseline_model: str | None = None):
        self.home = os.path.realpath(os.path.expanduser(home))
        self.baseline_request = baseline_model
        self._cache: dict[str, tuple[tuple, object]] = {}

    # ---------------------------------------------------------------- change detection
    def watched_paths(self) -> list[str]:
        h = self.home
        paths = [os.path.join(h, n) for n in (
            "activity.jsonl", "memory.db", "memory.db-wal", "sessions.db", "sessions.db-wal",
            "usage.db", "usage.db-wal", "models.json")]
        paths += glob.glob(os.path.join(h, "build-runs", "*.json"))
        for d in glob.glob(os.path.join(h, "builds-state", "*", "")):
            paths += [os.path.join(d, n) for n in ("progress.jsonl", "status.json", "report.json",
                                                   "plan.json")]
        return paths

    def fingerprint(self) -> tuple:
        """Cheap: stat every watched file. Also the liveness of any 'running' build's pid,
        since a crashed process changes the truth without touching a file."""
        out = []
        for p in self.watched_paths():
            try:
                st = os.stat(p)
                out.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        for pid in self._running_pids():
            out.append(("pid", pid, _alive(pid)))
        return tuple(sorted(out, key=str))

    def _running_pids(self) -> list[int]:
        pids = []
        for p in glob.glob(os.path.join(self.home, "builds-state", "*", "status.json")):
            st = self._cached_json(p)
            if isinstance(st, dict) and st.get("state") == "running" and st.get("pid"):
                pids.append(int(st["pid"]))
        return pids

    def _stat_key(self, path: str) -> tuple | None:
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _cached(self, path: str, parse):
        key = self._stat_key(path)
        if key is None:
            return None
        hit = self._cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        val = parse(path)
        self._cache[path] = (key, val)
        return val

    def _cached_json(self, path: str):
        return self._cached(path, _read_json)

    @staticmethod
    def _parse_jsonl(path: str) -> list[dict]:
        out = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a half-written last line while the writer is mid-append
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            pass
        return out

    # ---------------------------------------------------------------- agents
    def _build_records(self) -> dict[str, dict]:
        recs = {}
        for p in glob.glob(os.path.join(self.home, "build-runs", "*.json")):
            if p.endswith(".out.json"):
                continue
            r = self._cached_json(p)
            if isinstance(r, dict) and r.get("run_id"):
                recs[r["run_id"]] = r
        return recs

    def _workspace_runs(self, state_dir: str, records: dict, pricing: Pricing) -> tuple[list, list]:
        """Split one workspace's progress stream into runs; → (runs, feed events)."""
        events = self._cached(os.path.join(state_dir, "progress.jsonl"), self._parse_jsonl) or []
        status = self._cached_json(os.path.join(state_dir, "status.json")) or {}
        report = self._cached_json(os.path.join(state_dir, "report.json")) or {}
        plan = self._cached_json(os.path.join(state_dir, "plan.json")) or {}
        workspace = status.get("workspace") or report.get("workspace") or plan.get("workspace") or ""
        objective = report.get("objective") or plan.get("objective") or ""
        project = report.get("project") or plan.get("project") or ""

        runs: list[dict] = []
        feed: list[dict] = []
        cur: dict | None = None

        def new_run(run_id, kind, ts):
            rec = records.get(run_id) or {}
            return {"run_id": run_id or "unknown", "kind": kind, "workspace": workspace,
                    "workspace_name": os.path.basename(workspace.rstrip("/")) if workspace else
                    os.path.basename(state_dir.rstrip("/")),
                    "objective": rec.get("objective") or objective, "project": rec.get("project") or project,
                    "started_at": ts, "ended_at": None, "last_event_at": ts, "state": "running",
                    "pid": rec.get("pid"), "tasks": {}, "order": [], "planned": [],
                    "usage": {"model_calls": 0, "tool_calls": 0, "turns": 0, "input_tokens": 0,
                              "output_tokens": 0, "cost_usd": 0.0},
                    "usage_source": "live", "_samples": [], "_has_usage_events": False,
                    "plan_model": None}

        for ev in events:
            kind, ts = ev.get("event"), ev.get("ts")
            if kind in ("plan-start", "resume-start"):
                cur = new_run(ev.get("run_id"), "build" if kind == "plan-start" else "resume", ts)
                if kind == "resume-start":
                    cur["planned_by_run"] = ev.get("planned_by_run")
                if ev.get("project"):
                    cur["project"] = ev["project"]
                runs.append(cur)
            elif cur is None:
                cur = new_run(None, "build", ts)
                runs.append(cur)
            cur["last_event_at"] = ts
            tid = ev.get("task")
            t = None
            if tid and tid != "planner":
                if tid not in cur["tasks"]:
                    cur["tasks"][tid] = _empty_task(tid)
                    cur["order"].append(tid)
                t = cur["tasks"][tid]
            u = cur["usage"]
            if kind == "plan-end":
                cur["planned"] = list(ev.get("tasks") or [])
                for p in cur["planned"]:
                    if p not in cur["tasks"]:
                        cur["tasks"][p] = _empty_task(p)
                        cur["order"].append(p)
            elif kind == "task-start" and t is not None:
                t.update(status="running", profile=ev.get("profile"), model=ev.get("model"),
                         attempts=ev.get("attempt") or t["attempts"] + 1)
                t["started_at"] = t["started_at"] or ts
            elif kind == "turn" and t is not None:
                t["turns"] += 1
                u["turns"] += 1
            elif kind == "tool" and t is not None:
                t["tool_calls"] += 1
                u["tool_calls"] += 1
                name = ev.get("tool") or "?"
                t["tools"][name] = t["tools"].get(name, 0) + 1
                if ev.get("ok") is False:
                    t["tool_failures"] += 1
                t["last_action"] = f"{name} {ev.get('target') or ''}".strip()
            elif kind == "usage":
                cur["_has_usage_events"] = True
                tin, tout = int(ev.get("input_tokens") or 0), int(ev.get("output_tokens") or 0)
                cost = float(ev.get("cost_usd") or 0.0)
                model = ev.get("model") or (t or {}).get("model") or ""
                u["model_calls"] += 1
                u["input_tokens"] += tin
                u["output_tokens"] += tout
                u["cost_usd"] += cost
                if t is not None:
                    t["model_calls"] += 1
                    t["input_tokens"] += tin
                    t["output_tokens"] += tout
                    t["cost_usd"] += cost
                elif tid == "planner":
                    cur["plan_model"] = model
                cur["_samples"].append((ts, model, tin, tout, cost, 1))
            elif kind == "task-end" and t is not None:
                t.update(status=ev.get("status") or "?", error=ev.get("error") or "")
                t["ended_at"] = ts
            elif kind == "task-skip" and t is not None:
                t["status"] = ("done" if ev.get("status") == "done" else ev.get("status") or "skipped")
                t["resumed"] = ev.get("reason") == "resumed"
                t["error"] = "" if t["resumed"] else (ev.get("reason") or "")
            elif kind == "run-end":
                cur["state"] = ev.get("status") or "finished"
                cur["ended_at"] = ts
            if kind not in QUIET_EVENTS:
                feed.append({"ts": ts, "event": kind, "run_id": cur["run_id"], "task": tid,
                             "project": cur["project"], "workspace": cur["workspace_name"],
                             **{k: ev[k] for k in ("tool", "ok", "target", "status", "profile", "model",
                                                   "attempt", "error", "reason", "complete", "total",
                                                   "file", "by") if k in ev},
                             **({"planned": len(ev.get("tasks") or [])} if kind == "plan-end" else {})})

        # older runs: no per-call usage events → the final report's task usage (that run only)
        if report.get("run_id"):
            target = next((r for r in runs if r["run_id"] == report["run_id"]), None)
            if target is not None and not target["_has_usage_events"]:
                self._apply_report_usage(target, report, pricing, runs)

        # resumed/skipped tasks carry no profile in their events: the stored plan has it
        planned = {t.get("id"): t for t in plan.get("tasks") or [] if isinstance(t, dict)}
        for r in runs:
            for t in r["tasks"].values():
                spec = planned.get(t["id"]) or {}
                t["profile"] = t["profile"] or spec.get("profile")

        # liveness: only the newest run can still be running
        for i, r in enumerate(runs):
            if r["ended_at"] is not None:
                continue
            newest = i == len(runs) - 1
            pid = r.get("pid") or (status.get("pid") if status.get("run_id") == r["run_id"] else None)
            alive = _alive(pid) if newest else False
            if newest and alive:
                r["state"] = "running"
            elif newest and alive is None and status.get("run_id") == r["run_id"] \
                    and status.get("state") == "running" and time.time() - (r["last_event_at"] or 0) < 900:
                r["state"] = "running"  # no pid to check: trust a recent heartbeat
            else:
                r["state"] = "crashed"  # process gone without a run-end: resume it
            for t in r["tasks"].values():
                if t["status"] == "running" and r["state"] == "crashed":
                    t["status"] = "interrupted"
        return runs, feed

    @staticmethod
    def _apply_report_usage(run: dict, report: dict, pricing: Pricing, runs: list) -> None:
        run["usage_source"] = "report"
        ts = run["ended_at"] or run["last_event_at"]
        u = run["usage"]
        planner = (report.get("usage") or {}).get("planner") or {}
        if planner.get("model_calls"):
            model = report.get("plan_model") or ""
            run["plan_model"] = model
            u["model_calls"] += planner.get("model_calls", 0)
            u["input_tokens"] += planner.get("input_tokens", 0)
            u["output_tokens"] += planner.get("output_tokens", 0)
            run["_samples"].append((ts, model, planner.get("input_tokens", 0),
                                    planner.get("output_tokens", 0), None, planner["model_calls"]))
        for row in report.get("tasks") or []:
            tu = row.get("usage") or {}
            t = run["tasks"].get(row.get("id"))
            if t is None:
                continue
            t["model"] = t["model"] or row.get("model")
            t["profile"] = t["profile"] or row.get("profile")
            calls = int(tu.get("model_calls") or 0)
            if not calls:
                continue
            owner, owner_task = run, t
            if row.get("resumed") or t.get("resumed"):
                # a resumed row carries the usage of the EARLIER run that did the work:
                # credit that run (once, only if it has no data of its own), never this one
                owner = next((r for r in runs if r is not run and not r["_has_usage_events"]
                              and r["usage_source"] == "live"
                              and (r["tasks"].get(row.get("id")) or {}).get("status") == "done"
                              and not r["tasks"][row["id"]].get("resumed")
                              and not r["tasks"][row["id"]]["model_calls"]), None)
                if owner is None:
                    continue
                owner_task = owner["tasks"][row["id"]]
                owner["usage_source"] = "report"
            tin, tout = int(tu.get("input_tokens") or 0), int(tu.get("output_tokens") or 0)
            model = owner_task["model"] or row.get("model") or ""
            owner_task["model_calls"] = calls
            owner_task["input_tokens"], owner_task["output_tokens"] = tin, tout
            ou = owner["usage"]
            ou["model_calls"] += calls
            ou["input_tokens"] += tin
            ou["output_tokens"] += tout
            owner["_samples"].append((owner_task["ended_at"] or owner["ended_at"] or ts,
                                      model, tin, tout, None, calls))

    def _standalone_usage(self) -> list[tuple]:
        conn = _ro(os.path.join(self.home, "usage.db"))
        if conn is None:
            return []
        try:
            return [(r["ts"], r["model"], r["input_tokens"] or 0, r["output_tokens"] or 0, r["cost_usd"] or 0.0, 1)
                    for r in conn.execute("SELECT ts, model, input_tokens, output_tokens, cost_usd FROM usage")]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------------------------------------------------------------- the AI
    def _ai_activity(self) -> list[dict]:
        return self._cached(os.path.join(self.home, "activity.jsonl"), self._parse_jsonl) or []

    def _memory(self) -> tuple[list[dict], dict]:
        conn = _ro(os.path.join(self.home, "memory.db"))
        if conn is None:
            return [], {}
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, project, kind, source, substr(text, 1, 280) AS text, created_at "
                "FROM pockets ORDER BY id DESC LIMIT 100")]
            kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM pockets GROUP BY kind")}
            return rows, kinds
        except sqlite3.Error:
            return [], {}
        finally:
            conn.close()

    def _sessions(self) -> list[dict]:
        conn = _ro(os.path.join(self.home, "sessions.db"))
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(
                "SELECT slug, name, mode, branch, substr(summary, 1, 200) AS summary, created_at, "
                "last_opened FROM sessions ORDER BY COALESCE(last_opened, created_at) DESC LIMIT 50")]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------------------------------------------------------------- savings
    def _baseline(self, pricing: Pricing) -> dict:
        wanted = [self.baseline_request] if self.baseline_request else list(BASELINE_FALLBACKS)
        for m in wanted:
            p = pricing.price(m)
            if p and (p[0] or p[1]):
                return {"model": m, "name": pricing.name(m), "available": True,
                        "usd_per_m_input": round(p[0] * 1e6, 4), "usd_per_m_output": round(p[1] * 1e6, 4)}
        return {"model": wanted[0], "name": wanted[0], "available": False,
                "usd_per_m_input": None, "usd_per_m_output": None,
                "note": "baseline model not in the cached catalog — refresh it: engine-ai runtime models refresh"}

    @staticmethod
    def _actual_cost(pricing: Pricing, model: str, tin: int, tout: int, reported) -> float:
        if reported is not None:
            return float(reported)  # what the provider actually charged
        if pricing.is_free(model):
            return 0.0
        return pricing.cost(model, tin, tout) or 0.0

    def _savings(self, samples: list[tuple], pricing: Pricing, baseline: dict) -> dict:
        by_model: dict[str, dict] = {}
        for _ts, model, tin, tout, reported, calls in samples:
            m = by_model.setdefault(model or "unknown", {"model": model or "unknown", "calls": 0,
                                                         "input_tokens": 0, "output_tokens": 0,
                                                         "actual_cost_usd": 0.0})
            m["calls"] += calls
            m["input_tokens"] += tin
            m["output_tokens"] += tout
            m["actual_cost_usd"] += self._actual_cost(pricing, model, tin, tout, reported)
        tin = sum(m["input_tokens"] for m in by_model.values())
        tout = sum(m["output_tokens"] for m in by_model.values())
        actual = sum(m["actual_cost_usd"] for m in by_model.values())
        rows = []
        for m in sorted(by_model.values(), key=lambda m: -(m["input_tokens"] + m["output_tokens"])):
            paid_route = m["model"].replace(":free", "") if ":free" in m["model"] else None
            rows.append({**m, "name": pricing.name(m["model"]), "free": pricing.is_free(m["model"]),
                         "actual_cost_usd": round(m["actual_cost_usd"], 6),
                         "baseline_cost_usd": round(pricing.cost(baseline["model"], m["input_tokens"],
                                                                 m["output_tokens"]) or 0, 6)
                         if baseline["available"] else None,
                         "paid_route": paid_route,
                         "paid_route_cost_usd": round(pricing.cost(paid_route, m["input_tokens"],
                                                                   m["output_tokens"]), 6)
                         if paid_route and pricing.price(paid_route) else None})
        compare = []
        for ref in dict.fromkeys(list(COMPARE_MODELS) + [baseline["model"]]):
            c = pricing.cost(ref, tin, tout)
            if c is not None and pricing.price(ref) != (0.0, 0.0):
                compare.append({"model": ref, "name": pricing.name(ref), "cost_usd": round(c, 6),
                                "saved_usd": round(c - actual, 6), "is_baseline": ref == baseline["model"]})
        base_cost = pricing.cost(baseline["model"], tin, tout) if baseline["available"] else None
        return {"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout,
                "free_tokens": sum(m["input_tokens"] + m["output_tokens"] for m in rows if m["free"]),
                "actual_cost_usd": round(actual, 6),
                "baseline_cost_usd": round(base_cost, 6) if base_cost is not None else None,
                "saved_usd": round(base_cost - actual, 6) if base_cost is not None else None,
                "saved_pct": round(100 * (base_cost - actual) / base_cost, 1) if base_cost else None,
                "by_model": rows, "compare": sorted(compare, key=lambda c: c["cost_usd"])}

    @staticmethod
    def _timeline(samples: list[tuple], pricing: Pricing, baseline: dict, max_buckets: int = 36) -> dict:
        pts = sorted((s for s in samples if s[0]), key=lambda s: s[0])
        if not pts:
            return {"bucket_s": 0, "buckets": []}
        lo, hi = pts[0][0], max(pts[-1][0], pts[0][0] + 1)
        steps = (60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 7 * 86400)
        size = next((s for s in steps if (hi - lo) / s <= max_buckets), steps[-1])
        start = lo - lo % size
        buckets: dict[int, dict] = {}
        for ts, model, tin, tout, reported, calls in pts:
            k = int((ts - start) // size)
            b = buckets.setdefault(k, {"t": start + k * size, "input_tokens": 0, "output_tokens": 0,
                                       "calls": 0, "saved_usd": 0.0})
            b["input_tokens"] += tin
            b["output_tokens"] += tout
            b["calls"] += calls
            if baseline["available"]:
                base = pricing.cost(baseline["model"], tin, tout) or 0.0
                b["saved_usd"] += base - Collector._actual_cost(pricing, model, tin, tout, reported)
        last = max(buckets)  # contiguous axis: empty buckets are zero, not missing
        out = [buckets.get(k) or {"t": start + k * size, "input_tokens": 0, "output_tokens": 0,
                                  "calls": 0, "saved_usd": 0.0} for k in range(last + 1)]
        for b in out:
            b["saved_usd"] = round(b["saved_usd"], 6)
        return {"bucket_s": size, "buckets": out}

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        pricing = Pricing(self.home)
        baseline = self._baseline(pricing)
        records = self._build_records()

        runs, agent_feed = [], []
        for d in sorted(glob.glob(os.path.join(self.home, "builds-state", "*", ""))):
            if not os.path.exists(os.path.join(d, "progress.jsonl")):
                continue
            r, f = self._workspace_runs(d, records, pricing)
            runs += r
            agent_feed += f
        seen = {r["run_id"] for r in runs}
        for rid, rec in records.items():  # launched, but no progress written (yet / ever)
            if rid in seen:
                continue
            alive = _alive(rec.get("pid"))
            runs.append({"run_id": rid, "kind": "resume" if rec.get("resume") else "build",
                         "workspace": rec.get("workspace") or "",
                         "workspace_name": os.path.basename((rec.get("workspace") or "").rstrip("/")),
                         "objective": rec.get("objective") or "", "project": rec.get("project") or "",
                         "started_at": rec.get("started_at"), "ended_at": None,
                         "last_event_at": rec.get("started_at"),
                         "state": "starting" if alive else "crashed", "pid": rec.get("pid"),
                         "tasks": {}, "order": [], "planned": [], "usage_source": "live",
                         "usage": {"model_calls": 0, "tool_calls": 0, "turns": 0, "input_tokens": 0,
                                   "output_tokens": 0, "cost_usd": 0.0}, "_samples": []})

        samples = [s for r in runs for s in r["_samples"]]
        standalone = self._standalone_usage()
        samples += standalone
        savings = self._savings(samples, pricing, baseline)
        timeline = self._timeline(samples, pricing, baseline)

        profiles: dict[str, dict] = {}
        tools: dict[str, int] = {}
        task_states: dict[str, int] = {}
        for r in runs:
            for t in r["tasks"].values():
                task_states[t["status"]] = task_states.get(t["status"], 0) + 1
                for name, n in t["tools"].items():
                    tools[name] = tools.get(name, 0) + n
                if not t["profile"]:
                    continue
                p = profiles.setdefault(t["profile"], {"profile": t["profile"], "tasks": 0, "done": 0,
                                                       "failed": 0, "tool_calls": 0, "tokens": 0,
                                                       "models": []})
                p["tasks"] += 1
                p["done"] += t["status"] == "done" and not t.get("resumed")
                p["failed"] += t["status"] not in ("done", "running", "pending", "not-run")
                p["tool_calls"] += t["tool_calls"]
                p["tokens"] += t["input_tokens"] + t["output_tokens"]
                if t["model"] and t["model"] not in p["models"]:
                    p["models"].append(t["model"])

        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        public_runs = []
        for r in runs[:RUN_LIMIT]:
            r = {k: v for k, v in r.items() if not k.startswith("_")}
            r["tasks"] = [r["tasks"][t] for t in r.pop("order")]
            r["usage"]["cost_usd"] = round(r["usage"]["cost_usd"], 6)
            end = r["ended_at"] or (time.time() if r["state"] == "running" else r["last_event_at"])
            r["duration_s"] = round(end - r["started_at"], 1) if end and r.get("started_at") else None
            if baseline["available"]:
                base = pricing.cost(baseline["model"], r["usage"]["input_tokens"], r["usage"]["output_tokens"])
                r["saved_usd"] = round((base or 0) - r["usage"]["cost_usd"], 6)
            public_runs.append(r)

        # ---- the AI: tool calls it made through engine-ai, memory it kept, sessions, builds
        activity = self._ai_activity()
        memory, memory_kinds = self._memory()
        sessions = self._sessions()
        ai_tools: dict[str, dict] = {}
        for a in activity:
            s = ai_tools.setdefault(a.get("tool") or "?", {"tool": a.get("tool") or "?", "calls": 0,
                                                            "failed": 0, "total_ms": 0.0, "last_at": 0})
            s["calls"] += 1
            s["failed"] += not a.get("ok", True)
            s["total_ms"] += float(a.get("ms") or 0)
            s["last_at"] = max(s["last_at"], a.get("ts") or 0)
        for s in ai_tools.values():
            s["avg_ms"] = round(s.pop("total_ms") / s["calls"], 1)

        ai_feed = [{"ts": a.get("ts"), "type": "tool", "tool": a.get("tool"), "ok": a.get("ok", True),
                    "ms": a.get("ms"), "error": a.get("error")} for a in activity[-FEED_LIMIT:]]
        agent_memory = 0
        for m in memory:
            src = m.get("source") or ""
            entry = {"ts": m.get("created_at"), "type": "memory", "kind": m.get("kind"),
                     "project": m.get("project"), "text": m.get("text"), "source": src, "id": m.get("id")}
            if src.startswith(("engine-build:", "engine-run:")):
                agent_memory += 1
                agent_feed.append({**entry, "event": "memory", "run_id": src.split(":", 1)[1]})
            else:
                ai_feed.append(entry)
        for s in sessions:
            ai_feed.append({"ts": s.get("created_at"), "type": "session", "name": s.get("name") or s.get("slug"),
                            "mode": s.get("mode"), "text": s.get("summary")})
        for rec in records.values():
            ai_feed.append({"ts": rec.get("started_at"), "type": "build", "run_id": rec.get("run_id"),
                            "resume": bool(rec.get("resume")), "text": rec.get("objective") or "",
                            "workspace": os.path.basename((rec.get("workspace") or "").rstrip("/"))})
        ai_feed.sort(key=lambda e: e.get("ts") or 0, reverse=True)
        agent_feed.sort(key=lambda e: e.get("ts") or 0, reverse=True)

        running = [r for r in public_runs if r["state"] in ("running", "starting")]
        agent_totals = {"runs": len(runs), "running": len(running),
                        "model_calls": sum(r["usage"]["model_calls"] for r in runs) + len(standalone),
                        "tool_calls": sum(r["usage"]["tool_calls"] for r in runs),
                        "turns": sum(r["usage"]["turns"] for r in runs),
                        "input_tokens": savings["input_tokens"], "output_tokens": savings["output_tokens"],
                        "tasks_done": sum(1 for r in runs for t in r["tasks"].values()
                                          if t["status"] == "done" and not t.get("resumed")),
                        "tasks_failed": sum(1 for r in runs for t in r["tasks"].values()
                                            if t["status"] not in ("done", "running", "pending", "not-run")),
                        "files_written": sum(t["tools"].get("write_file", 0) + t["tools"].get("append_file", 0)
                                             for r in runs for t in r["tasks"].values()),
                        "commands_run": tools.get("run_command", 0),
                        "memory_saved": agent_memory,
                        "standalone_calls": len(standalone)}
        ai_totals = {"tool_calls": len(activity), "failed": sum(1 for a in activity if not a.get("ok", True)),
                     "distinct_tools": len(ai_tools), "builds_started": len(records),
                     "memory_saved": sum(memory_kinds.values()) - agent_memory,
                     "memory_by_kind": memory_kinds, "sessions": len(sessions),
                     "logging_since": activity[0].get("ts") if activity else None}
        return {
            "generated_at": round(time.time(), 3), "home": self.home,
            "baseline": baseline, "catalog_refreshed_at": pricing.refreshed_at,
            "agents": {"totals": agent_totals, "runs": public_runs,
                       "profiles": sorted(profiles.values(), key=lambda p: -p["tasks"]),
                       "tools": sorted(({"tool": k, "calls": v} for k, v in tools.items()),
                                       key=lambda x: -x["calls"]),
                       "task_states": task_states, "feed": agent_feed[:FEED_LIMIT]},
            "ai": {"totals": ai_totals, "tools": sorted(ai_tools.values(), key=lambda s: -s["calls"]),
                   "feed": ai_feed[:FEED_LIMIT], "sessions": sessions[:20],
                   "scope": "engine-ai MCP tool calls, memory, sessions and builds the host assistant "
                            "made. Its own model tokens are billed by its provider and not visible here."},
            "savings": {**savings, "timeline": timeline,
                        "method": "Tokens processed by engine agents × the baseline model's catalog price, "
                                  "minus what the provider actually charged (free routes report $0). "
                                  "An estimate of what the same tokens would have cost on the baseline."},
        }
