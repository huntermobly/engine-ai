"""Live dashboard: snapshot math, stale-run detection, and SSE push on file change."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from engine_core import dashboard  # noqa: E402


def _write_run(home, events, status):
    d = os.path.join(home, "builds-state", "demo-abc")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "status.json"), "w") as f:
        json.dump(status, f)
    with open(os.path.join(d, "progress.jsonl"), "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_live_usage_and_savings(self):
        now = time.time()
        _write_run(self.home, [
            {"ts": now, "event": "plan-start", "project": "demo"},
            {"ts": now, "event": "usage", "task": "(planner)", "model": "m:free",
             "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 0.0},
            {"ts": now, "event": "task-start", "task": "a", "profile": "frontend", "model": "m:free"},
            {"ts": now, "event": "tool", "task": "a", "tool": "write_file", "ok": True},
            {"ts": now, "event": "usage", "task": "a", "model": "m:free",
             "input_tokens": 0, "output_tokens": 1_000_000, "cost_usd": 0.5},
            {"ts": now, "event": "task-end", "task": "a", "status": "done"},
        ], {"run_id": "r1", "state": "running", "pid": os.getpid(), "tasks": {"a": "done"},
            "updated_at": now})
        snap = dashboard.Collector(self.home, 3.0, 15.0).snapshot()
        t = snap["totals"]
        self.assertEqual((t["input_tokens"], t["output_tokens"], t["model_calls"], t["tool_calls"]),
                         (1_000_000, 1_000_000, 2, 1))
        self.assertEqual((t["baseline_usd"], t["cost_usd"], t["saved_usd"]), (18.0, 0.5, 17.5))
        agents = {a["profile"]: a for a in snap["agents"]}
        self.assertEqual(agents["planner"]["in"], 1_000_000)
        self.assertEqual(agents["frontend"]["done"], 1)
        self.assertEqual((snap["runs"][0]["state"], snap["runs"][0]["usage_source"]), ("running", "live"))
        self.assertTrue(any("write_file" in f["text"] for f in snap["feed"]))

    def test_dead_build_marked_stale(self):
        _write_run(self.home, [{"ts": 1, "event": "task-start", "task": "a", "profile": "p"}],
                   {"state": "running", "pid": 2_000_000_000, "tasks": {"a": "running"}})
        run = dashboard.Collector(self.home, 3, 15).snapshot()["runs"][0]
        self.assertEqual(run["state"], "stale")
        self.assertEqual(run["tasks"][0]["status"], "interrupted")

    def test_sse_pushes_on_file_change(self):
        old = dashboard.POLL_S
        dashboard.POLL_S = 0.1
        _write_run(self.home, [], {"state": "running", "pid": os.getpid(), "tasks": {}})
        hub = dashboard.Hub(dashboard.Collector(self.home, 3, 15))
        srv = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(hub))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/events", timeout=5)
            first = json.loads(resp.readline().decode()[len("data: "):])
            resp.readline()
            self.assertEqual(first["totals"]["input_tokens"], 0)
            _write_run(self.home, [{"ts": time.time(), "event": "usage", "task": "x",
                                    "input_tokens": 42, "output_tokens": 0}],
                       {"state": "running", "pid": os.getpid()})
            second = json.loads(resp.readline().decode()[len("data: "):])
            self.assertEqual(second["totals"]["input_tokens"], 42)
        finally:
            dashboard.POLL_S = old
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
