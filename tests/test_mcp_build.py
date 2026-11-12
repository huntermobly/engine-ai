"""MCP → engine build workflow, end to end over the real stdio server.

The server is spawned exactly as Claude Code / Codex spawn it (python3 mcp/forge_mcp.py),
the build runs in a detached background process, and providers are the offline
ScriptedProvider (ENGINE_AI_PROVIDER=scripted:…) — no keys, no network.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp", "forge_mcp.py")

GAME = "<!doctype html><title>t</title><div id=board></div><script>/*game*/</script>"
SCRIPT = {
    "plan": {"tasks": [
        {"id": "contract", "profile": "planner", "owned_paths": ["SPEC.md"], "artifacts": ["SPEC.md"],
         "objective": "write the DOM contract"},
        {"id": "implement", "profile": "frontend", "dependencies": ["contract"],
         "owned_paths": ["index.html"], "artifacts": ["index.html"], "objective": "build the game"},
        {"id": "tests", "profile": "test-author", "dependencies": ["implement"],
         "owned_paths": ["tests"], "artifacts": ["tests/check.py"],
         "verification": ["python3 tests/check.py"], "objective": "test it"},
        {"id": "review", "profile": "reviewer", "dependencies": ["implement", "tests"],
         "objective": "review"}]},
    "tasks": {
        "contract": [{"tool": "write_file", "args": {"path": "SPEC.md", "content": "ids: board"}},
                     '{"status": "done", "summary": "contract written"}'],
        "implement": [{"tool": "write_file", "args": {"path": "index.html", "content": GAME}},
                      '{"status": "done", "summary": "game written"}'],
        "tests": [{"tool": "write_file", "args": {"path": "tests/check.py", "content":
                   "import sys; sys.exit(0 if 'board' in open('index.html').read() else 1)\n"}},
                  '{"status": "done", "summary": "tests written"}'],
        "review": ['{"status": "done", "summary": "LGTM: board present"}']},
}


class Client:
    def __init__(self, home, extra_env):
        env = {**os.environ, "ENGINE_AI_HOME": home, "ENGINE_AI_MASTER_KEY": "test-key", **extra_env}
        self.p = subprocess.Popen(["python3", SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=env, bufsize=1, cwd=tempfile.gettempdir())
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, args):
        r = self.call("tools/call", {"name": name, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.terminate()
        self.p.wait(timeout=5)
        self.p.stdout.close()


class TestMCPBuildWorkflow(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        script = os.path.join(self.home, "script.json")
        with open(script, "w") as f:
            json.dump(SCRIPT, f)
        self.c = Client(self.home, {"ENGINE_AI_PROVIDER": f"scripted:{script}"})
        self.c.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "0"}})

    def tearDown(self):
        self.c.close()

    def _wait(self, run_id, limit=60):
        deadline = time.time() + limit
        while time.time() < deadline:
            st = self.c.tool("engine_build_status", {"run_id": run_id})
            if st.get("terminal"):
                return st
            time.sleep(0.3)
        self.fail(f"build {run_id} did not finish in {limit}s: {st}")

    def test_tools_listed(self):
        names = {t["name"] for t in self.c.call("tools/list")["result"]["tools"]}
        self.assertTrue({"engine_info", "engine_build_start", "engine_build_status",
                         "engine_build_resume"} <= names)

    def test_engine_info_reports_the_loaded_source_tree(self):
        info = self.c.tool("engine_info", {})
        self.assertEqual(os.path.realpath(info["source_dir"]), os.path.realpath(ROOT))
        self.assertEqual(os.path.realpath(info["engine_core"]), os.path.realpath(os.path.join(ROOT, "engine_core")))
        self.assertEqual(info["engine_home"], self.home)

    def test_start_monitor_and_report_a_build(self):
        start = self.c.tool("engine_build_start", {"objective": "single-file game", "project": "mcp-test"})
        self.assertTrue(start["ok"], start)
        self.assertTrue(start["workspace"].startswith(os.path.join(self.home, "builds")))
        st = self._wait(start["run_id"])
        self.assertEqual(st["state"], "complete", json.dumps(st, indent=1)[:3000])
        rep = st["report"]
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertEqual(list(by), ["contract", "implement", "tests", "review"])
        self.assertEqual(by["implement"]["profile"], "frontend")
        self.assertEqual(by["review"]["dependencies"], ["implement", "tests"])
        self.assertEqual(by["tests"]["verification"][0]["exit_code"], 0)
        with open(os.path.join(start["workspace"], "index.html")) as f:
            self.assertEqual(f.read(), GAME)
        self.assertTrue(any(e["event"] == "run-end" for e in st["recent_events"]))
        self.assertEqual(rep["usage"]["workers"]["tool_calls"], 3)

    def test_two_starts_get_separate_fresh_workspaces(self):
        a = self.c.tool("engine_build_start", {"objective": "same objective"})
        b = self.c.tool("engine_build_start", {"objective": "same objective"})
        self.assertNotEqual(a["workspace"], b["workspace"])
        reports = []
        for s in (a, b):  # both background processes run CONCURRENTLY with identical task ids
            st = self._wait(s["run_id"])
            self.assertEqual(st["state"], "complete")
            self.assertEqual(st["report"]["run_id"], s["run_id"])
            self.assertTrue(os.path.isfile(os.path.join(s["workspace"], "index.html")))
            reports.append(st["report"])
        self.assertNotEqual(reports[0]["plan_path"], reports[1]["plan_path"])

    def test_resume_via_mcp_keeps_valid_work(self):
        start = self.c.tool("engine_build_start", {"objective": "game"})
        self._wait(start["run_id"])
        os.remove(os.path.join(start["workspace"], "index.html"))
        res = self.c.tool("engine_build_resume", {"workspace": start["workspace"]})
        rep = self._wait(res["run_id"])["report"]
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertTrue(rep["resumed_from_plan"])
        self.assertTrue(by["contract"]["resumed"])
        self.assertFalse(by["implement"].get("resumed"))
        self.assertEqual(rep["status"], "complete")

    def test_unknown_run_and_missing_objective_are_clean_errors(self):
        self.assertFalse(self.c.tool("engine_build_status", {"run_id": "run-nope"})["ok"])
        r = self.c.call("tools/call", {"name": "engine_build_start", "arguments": {}})
        self.assertIn("error", json.dumps(r))


class TestBlockedBuildOverMCP(unittest.TestCase):
    def test_incomplete_build_is_reported_failed_not_complete(self):
        home = tempfile.mkdtemp()
        script = dict(SCRIPT, tasks=dict(SCRIPT["tasks"], implement=['{"status": "done", "summary": "lies"}']))
        path = os.path.join(home, "s.json")
        with open(path, "w") as f:
            json.dump(script, f)
        c = Client(home, {"ENGINE_AI_PROVIDER": f"scripted:{path}"})
        try:
            start = c.tool("engine_build_start", {"objective": "game"})
            deadline = time.time() + 60
            while time.time() < deadline:
                st = c.tool("engine_build_status", {"run_id": start["run_id"]})
                if st.get("terminal"):
                    break
                time.sleep(0.3)
            self.assertEqual(st["state"], "failed")
            by = {t["id"]: t for t in st["report"]["tasks"]}
            self.assertEqual(by["implement"]["status"], "missing-artifacts")
            self.assertEqual(by["tests"]["status"], "not-run")
            self.assertEqual(st["report"]["exit_code"], 1)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
