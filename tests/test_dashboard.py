"""Live dashboard: collector accounting, AI activity log, and the SSE sync server."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from engine_core.dashboard.collector import Collector  # noqa: E402
from engine_core.runtime.worker import _tool_target  # noqa: E402
from engine_core.telemetry import activity  # noqa: E402
from runtime.test_pipeline import DONE, PLAN, _pipeline  # noqa: E402

SONNET = "anthropic/claude-sonnet-5.5"


def _catalog(home, extra=()):
    models = [{"id": SONNET, "name": "Claude Sonnet", "pricing_prompt": 2e-06,
               "pricing_completion": 1e-05, "free": False},
              {"id": "fake/model:free", "name": "Fake (free)", "pricing_prompt": 0.0,
               "pricing_completion": 0.0, "free": True}, *extra]
    with open(os.path.join(home, "models.json"), "w") as f:
        json.dump({"refreshed_at": time.time(), "models": models, "pinned": {}}, f)


def _progress(home, ws_name, events, status=None):
    d = os.path.join(home, "builds-state", ws_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "progress.jsonl"), "a") as f:
        for e in events:
            f.write(json.dumps({"ts": time.time(), **e}) + "\n")
    if status is not None:
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump(status, f)
    return d


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestCollectorWithRealPipeline(unittest.TestCase):
    """The collector reads what BuildPipeline actually writes — no hand-made fixtures."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        _catalog(self.home)

    def test_live_usage_events_match_the_report(self):
        pipe, _ = _pipeline(self.home, PLAN, worker_texts=[DONE, DONE, DONE])
        out = pipe.run("build a small API", workspace=os.path.join(self.home, "ws"))
        snap = Collector(self.home).snapshot()
        report = out["usage"]["total"]
        sv = snap["savings"]
        self.assertEqual((sv["input_tokens"], sv["output_tokens"]),
                         (report["input_tokens"], report["output_tokens"]))
        self.assertEqual(snap["agents"]["totals"]["model_calls"], report["model_calls"])
        run = snap["agents"]["runs"][0]
        self.assertEqual(run["state"], "complete")
        self.assertEqual(run["usage_source"], "live", "per-call usage events drive the totals")
        self.assertEqual({t["status"] for t in run["tasks"]}, {"done"})
        self.assertEqual({t["profile"] for t in run["tasks"]}, {"backend", "test-author"})
        # free route: charged $0, so the saving is the full baseline price of those tokens
        expected = sv["input_tokens"] * 2e-06 + sv["output_tokens"] * 1e-05
        self.assertAlmostEqual(sv["baseline_cost_usd"], expected, places=9)
        self.assertEqual(sv["actual_cost_usd"], 0.0)
        self.assertAlmostEqual(sv["saved_usd"], expected, places=9)
        self.assertEqual(sv["saved_pct"], 100.0)

    def test_resume_does_not_double_count(self):
        ws = os.path.join(self.home, "ws")
        pipe, _ = _pipeline(self.home, PLAN, worker_texts=[DONE, DONE, DONE])
        first = pipe.run("build a small API", workspace=ws)
        pipe2, _ = _pipeline(self.home, PLAN, worker_texts=[DONE, DONE, DONE])
        pipe2.resume(ws)  # every task is kept: no new model calls
        snap = Collector(self.home).snapshot()
        self.assertEqual(len(snap["agents"]["runs"]), 2)
        self.assertEqual(snap["savings"]["input_tokens"], first["usage"]["total"]["input_tokens"])
        resumed = snap["agents"]["runs"][0]
        self.assertTrue(all(t.get("resumed") for t in resumed["tasks"]))
        self.assertEqual(snap["agents"]["totals"]["tasks_done"], 3, "kept tasks are not re-counted")


class TestCollectorAccounting(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        _catalog(self.home, extra=[{"id": "paid/model", "name": "Paid", "pricing_prompt": 1e-06,
                                    "pricing_completion": 2e-06, "free": False}])

    def test_reported_cost_is_subtracted_from_the_saving(self):
        _progress(self.home, "ws-a", [
            {"event": "plan-start", "run_id": "r1", "project": "p"},
            {"event": "task-start", "task": "t", "profile": "backend", "model": "paid/model", "attempt": 1},
            {"event": "usage", "task": "t", "model": "paid/model", "input_tokens": 1000,
             "output_tokens": 100, "cost_usd": 0.0012},
            {"event": "task-end", "task": "t", "status": "done"},
            {"event": "run-end", "status": "complete", "complete": 1, "total": 1}])
        sv = Collector(self.home).snapshot()["savings"]
        self.assertAlmostEqual(sv["baseline_cost_usd"], 1000 * 2e-06 + 100 * 1e-05)
        self.assertAlmostEqual(sv["actual_cost_usd"], 0.0012)
        self.assertAlmostEqual(sv["saved_usd"], 0.003 - 0.0012)

    def test_unknown_baseline_reports_no_saving_instead_of_inventing_one(self):
        _progress(self.home, "ws-a", [
            {"event": "plan-start", "run_id": "r1"},
            {"event": "usage", "task": "t", "model": "fake/model:free", "input_tokens": 50,
             "output_tokens": 5, "cost_usd": 0}])
        snap = Collector(self.home, baseline_model="not/in-catalog").snapshot()
        self.assertFalse(snap["baseline"]["available"])
        self.assertIsNone(snap["savings"]["saved_usd"])
        self.assertEqual(snap["savings"]["total_tokens"], 55)

    def test_dead_process_without_run_end_is_crashed(self):
        _progress(self.home, "ws-a", [
            {"event": "plan-start", "run_id": "r1"},
            {"event": "plan-end", "tasks": ["a", "b"]},
            {"event": "task-start", "task": "a", "profile": "backend", "model": "fake/model:free",
             "attempt": 1}],
            status={"run_id": "r1", "pid": _dead_pid(), "state": "running"})
        run = Collector(self.home).snapshot()["agents"]["runs"][0]
        self.assertEqual(run["state"], "crashed")
        self.assertEqual([t["status"] for t in run["tasks"]], ["interrupted", "pending"])

    def test_live_process_is_running_and_tool_targets_reach_the_feed(self):
        _progress(self.home, "ws-a", [
            {"event": "plan-start", "run_id": "r1"},
            {"event": "task-start", "task": "a", "profile": "frontend", "model": "fake/model:free",
             "attempt": 1},
            {"event": "tool", "task": "a", "tool": "write_file", "ok": True, "target": "index.html"}],
            status={"run_id": "r1", "pid": os.getpid(), "state": "running"})
        snap = Collector(self.home).snapshot()
        run = snap["agents"]["runs"][0]
        self.assertEqual(run["state"], "running")
        self.assertEqual(run["tasks"][0]["last_action"], "write_file index.html")
        self.assertEqual(snap["agents"]["totals"]["files_written"], 1)
        self.assertEqual(snap["agents"]["feed"][0]["target"], "index.html")

    def test_half_written_progress_line_is_skipped(self):
        d = _progress(self.home, "ws-a", [{"event": "plan-start", "run_id": "r1"}])
        with open(os.path.join(d, "progress.jsonl"), "a") as f:
            f.write('{"event": "tool", "task"')  # writer mid-append
        self.assertEqual(len(Collector(self.home).snapshot()["agents"]["runs"]), 1)

    def test_fingerprint_changes_when_activity_is_recorded(self):
        c = Collector(self.home)
        before = c.fingerprint()
        self.assertEqual(before, c.fingerprint(), "stable when nothing changed")
        activity.record(self.home, "memory_save", True, 3.0)
        self.assertNotEqual(before, c.fingerprint())
        ai = c.snapshot()["ai"]
        self.assertEqual(ai["totals"]["tool_calls"], 1)
        self.assertEqual(ai["tools"][0]["tool"], "memory_save")

    def test_empty_home_is_a_valid_snapshot(self):
        snap = Collector(tempfile.mkdtemp()).snapshot()
        self.assertEqual(snap["agents"]["runs"], [])
        self.assertEqual(snap["savings"]["total_tokens"], 0)
        json.dumps(snap)


class TestActivityLog(unittest.TestCase):
    def test_mcp_tool_call_is_logged_without_its_arguments(self):
        home = tempfile.mkdtemp()
        code = ("import json,sys; sys.path.insert(0, 'mcp'); import forge_mcp as m; "
                "print(json.dumps(m.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':"
                "{'name':'memory_recall','arguments':{'keywords':['SECRET-TOKEN-XYZ']}}})))")
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                           timeout=60, env={**os.environ, "ENGINE_AI_HOME": home,
                                            "ENGINE_AI_MASTER_KEY": "test-key"})
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(activity.activity_path(home)) as f:
            raw = f.read()
        rec = json.loads(raw.strip().splitlines()[-1])
        self.assertEqual(rec["tool"], "memory_recall")
        self.assertIn("ms", rec)
        self.assertNotIn("SECRET-TOKEN-XYZ", raw, "tool arguments must never be logged")

    def test_outcome_ok_and_record_never_raises(self):
        self.assertFalse(activity.outcome_ok({"ok": False}))
        self.assertTrue(activity.outcome_ok({"ok": True}))
        self.assertTrue(activity.outcome_ok(["list result"]))
        activity.record("/proc/definitely/not/writable", "x", True, 1.0)  # swallowed

    def test_tool_target_summarises_path_or_command(self):
        call = lambda args: {"function": {"name": "t", "arguments": json.dumps(args)}}  # noqa: E731
        self.assertEqual(_tool_target(call({"path": "src/app.py", "content": "x" * 999})), "src/app.py")
        self.assertEqual(_tool_target(call({"command": ["python3", "-m", "unittest"]})),
                         "python3 -m unittest")
        self.assertEqual(len(_tool_target(call({"command": "x" * 500}))), 120)
        self.assertEqual(_tool_target({"function": {"arguments": "{not json"}}), "")


class TestServer(unittest.TestCase):
    def setUp(self):
        from engine_core.dashboard.server import serve
        self.home = tempfile.mkdtemp()
        _catalog(self.home)
        started = threading.Event()
        box = {}

        def ready(url, httpd, hub):
            box.update(url=url, httpd=httpd, hub=hub)
            started.set()
        threading.Thread(target=serve, args=(self.home,), daemon=True,
                         kwargs={"port": 0, "interval": 0.2, "ready": ready}).start()
        self.assertTrue(started.wait(10))
        self.url, self.httpd, self.hub = box["url"], box["httpd"], box["hub"]

    def tearDown(self):
        self.hub.stop()
        self.httpd.shutdown()

    def test_page_snapshot_and_host_guard(self):
        with urllib.request.urlopen(self.url, timeout=5) as r:
            self.assertIn(b"engine-ai", r.read())
        with urllib.request.urlopen(self.url + "api/snapshot", timeout=5) as r:
            self.assertIn("savings", json.load(r))
        req = urllib.request.Request(self.url + "api/snapshot", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)
        cm.exception.close()

    def test_stream_pushes_a_new_snapshot_when_files_change(self):
        snaps = []
        got_two = threading.Event()

        def listen():
            with urllib.request.urlopen(self.url + "api/stream", timeout=10) as r:
                event = None
                for line in r:
                    line = line.decode().rstrip("\n")
                    if line.startswith("event:"):
                        event = line[7:]
                    elif line.startswith("data:") and event == "snapshot":
                        snaps.append(json.loads(line[6:]))
                        if len(snaps) >= 2:
                            got_two.set()
                            return
        threading.Thread(target=listen, daemon=True).start()
        deadline = time.time() + 5
        while not snaps and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(snaps, "a snapshot is sent on connect")
        _progress(self.home, "ws-live", [
            {"event": "plan-start", "run_id": "r-live"},
            {"event": "usage", "task": "planner", "model": "fake/model:free", "input_tokens": 70,
             "output_tokens": 7, "cost_usd": 0}])
        self.assertTrue(got_two.wait(5), "the change is pushed without a reload")
        self.assertGreater(snaps[1]["version"], snaps[0]["version"])
        self.assertEqual(snaps[1]["savings"]["total_tokens"], 77)


if __name__ == "__main__":
    unittest.main()
