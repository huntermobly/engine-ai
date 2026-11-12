"""Regression tests for the hands-on usability audit: each test pins one bug that the
unit suite missed but a real user hits (wrong cwd, silent empty search, empty skill
registry, resume of a non-build, placeholder keys, …). Offline — no network, no keys."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "engine-ai.js")
sys.path.insert(0, os.path.join(ROOT, "mcp"))
sys.path.insert(0, ROOT)


class MCP:
    def __init__(self, home):
        env = {**os.environ, "ENGINE_AI_HOME": home, "ENGINE_AI_MASTER_KEY": "test-key"}
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "mcp", "forge_mcp.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.i = 0
        self.raw("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}})

    def raw(self, method, params):
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method,
                                       "params": params}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, args):
        r = self.raw("tools/call", {"name": name, "arguments": args})
        if "error" in r:
            return r
        return json.loads(r["result"]["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.p.stdout.close()


def _node():
    return shutil.which("node")


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "test_x.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test(self): pass\n")
        self.env = {**os.environ, "ENGINE_AI_HOME": os.path.join(self.tmp, "home"),
                    "ENGINE_AI_MASTER_KEY": "test-key"}

    def py(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "engine_core", "__main__.py"), *args], cwd=self.tmp, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_verify_accepts_double_dash_form_from_readme(self):
        r = self.py("verify", "--path", self.tmp, "--", sys.executable, "-m", "unittest", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["passed"])

    def test_verify_quoted_string_splits_on_and(self):
        r = self.py("verify", "--path", self.tmp, f"{sys.executable} -m unittest -q && false")
        self.assertEqual(r.returncode, 1)
        checks = json.loads(r.stdout)["checks"]
        self.assertEqual([c["exit_code"] for c in checks], [0, 1])

    def test_verify_without_command_is_a_usage_error(self):
        r = self.py("verify", "--path", self.tmp)
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs a command", r.stderr)

    @unittest.skipUnless(_node(), "node not installed")
    def test_runtime_runs_in_the_users_directory(self):
        r = subprocess.run(["node", CLI, "runtime", "verify", "--path", ".", "pwd"], cwd=self.tmp,
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["checks"][0]["output_tail"].strip()
        self.assertEqual(os.path.realpath(out), os.path.realpath(self.tmp))

    @unittest.skipUnless(_node(), "node not installed")
    def test_unknown_commands_exit_nonzero(self):
        for args in (["bogus"], ["knowledge", "bogus"]):
            r = subprocess.run(["node", CLI, *args], env=self.env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("unknown", r.stderr)
        r = subprocess.run(["node", CLI], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)  # bare `engine-ai` still prints help


class TestWebSearch(unittest.TestCase):
    def test_bot_check_page_is_an_error_not_zero_hits(self):
        import websearch
        challenge = b"<html><form id='challenge-form'>anomaly detected</form></html>"
        orig = websearch.SearchProvider._fetch
        websearch.SearchProvider._fetch = staticmethod(lambda *a, **k: challenge)
        try:
            with self.assertRaises(RuntimeError) as cm:
                websearch.WebSearch().search("anything", 3)
        finally:
            websearch.SearchProvider._fetch = orig
        self.assertIn("bot-check", str(cm.exception))
        self.assertIn("BRAVE_API_KEY", str(cm.exception))

    def test_real_results_are_not_mistaken_for_a_challenge(self):
        from websearch import DuckDuckGoProvider
        page = ('<a rel="nofollow" class="result__a" href="https://x.dev">X</a>'
                '<a class="result__snippet" href="#">a challenge-free snippet</a>')
        self.assertFalse(DuckDuckGoProvider.is_challenge(page))


class TestKeys(unittest.TestCase):
    def test_example_placeholders_are_rejected(self):
        from engine_core.profiles import parse_key_csv
        with open(os.path.join(ROOT, "keys.example.csv"), encoding="utf-8") as f:
            rows, errors = parse_key_csv(f.read())
        self.assertEqual(rows, [])
        self.assertTrue(errors and all("placeholder" in e for e in errors))

    def test_real_looking_keys_still_import(self):
        from engine_core.profiles import parse_key_csv
        rows, errors = parse_key_csv("planner,sk-or-v1-0123456789abcdef,qwen/qwen3-coder:free\n")
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["profile"], "planner")


class TestMCPUsability(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.c = MCP(self.home)
        self.addCleanup(self.c.close)

    def test_bundled_skills_listed_on_fresh_install(self):
        names = {s["name"] for s in self.c.tool("list_skills", {})}
        bundled = {d for d in os.listdir(os.path.join(ROOT, "skills"))
                   if os.path.isfile(os.path.join(ROOT, "skills", d, "SKILL.md"))}
        self.assertTrue(bundled and bundled <= names, (bundled, names))
        skill = self.c.tool("get_skill", {"name": "deployable-app"})
        self.assertEqual(skill["source"], "bundled")

    def test_resume_without_a_stored_plan_is_refused_up_front(self):
        ws = os.path.join(self.home, "never-built")
        r = self.c.tool("engine_build_resume", {"workspace": ws})
        self.assertFalse(r["ok"])
        self.assertIn("no stored plan", r["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.home, "build-runs"))
                         and os.listdir(os.path.join(self.home, "build-runs")))

    def test_deploy_verify_reports_a_missing_directory(self):
        r = self.c.tool("deploy_verify", {"path": os.path.join(self.home, "nope")})
        self.assertFalse(r["passed"])
        self.assertIn("not a directory", r["error"])

    def test_scaffold_kind_is_validated_against_the_schema(self):
        tools = self.c.raw("tools/list", {})["result"]["tools"]
        kind = next(t for t in tools if t["name"] == "scaffold_app")["inputSchema"]["properties"]["kind"]
        self.assertEqual(kind["enum"], ["node-api", "python-api", "static"])
        r = self.c.tool("scaffold_app", {"path": os.path.join(self.home, "a"), "kind": "node"})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("node-api", r["error"]["message"])


class TestRunAndCodexEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, "ENGINE_AI_HOME": os.path.join(self.tmp, "home"),
                    "ENGINE_AI_MASTER_KEY": "test-key"}

    def rt(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "engine_core", "__main__.py"), *args],
                              cwd=self.tmp, env={**self.env, **(env or {})}, capture_output=True,
                              text=True, timeout=120)

    def test_run_executes_a_graph_offline(self):
        graph, script = os.path.join(self.tmp, "g.json"), os.path.join(self.tmp, "s.json")
        with open(graph, "w") as f:
            json.dump({"tasks": [{"id": "a", "objective": "x"},
                                 {"id": "b", "dependencies": ["a"], "objective": "y"}]}, f)
        with open(script, "w") as f:
            json.dump({"tasks": {"a": ['{"status": "done", "summary": "a"}'],
                                 "b": ['{"status": "done", "summary": "b"}']}}, f)
        r = self.rt("run", "--graph", graph, env={"ENGINE_AI_PROVIDER": f"scripted:{script}"})
        self.assertEqual(r.returncode, 0, r.stderr)  # was NameError: graph (since v0.16.1)
        self.assertIn("ran 2 task(s)", r.stdout)
        self.assertIn('"complete": 2', r.stdout)

    def test_codex_env_exports_vault_keys_without_writing_files(self):
        keys = os.path.join(self.tmp, "k.csv")
        with open(keys, "w") as f:
            f.write("backend,sk-or-v1-0123456789abcdef,qwen/qwen3-coder:free\n")
        self.assertEqual(self.rt("keys", keys).returncode, 0)
        os.remove(keys)
        before = {os.path.join(d, f) for d, _, fs in os.walk(self.tmp) for f in fs}
        r = self.rt("codex-env")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "export OPENROUTER_BACKEND_KEY=sk-or-v1-0123456789abcdef")
        after = {os.path.join(d, f) for d, _, fs in os.walk(self.tmp) for f in fs}
        self.assertFalse({p for p in after - before if p.endswith(".sh")})

    def test_codex_env_with_no_keys_fails_cleanly(self):
        r = self.rt("codex-env")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")


@unittest.skipUnless(shutil.which("codex"), "codex CLI not installed")
class TestCodexProfiles(unittest.TestCase):
    """The generated config must make `codex --profile engine_<p>` pick THAT profile's
    model, provider and key variable. Offline: no key is exported, so Codex stops at
    'Missing environment variable' before any request is sent."""

    def test_each_profile_selects_its_own_provider_model_and_key(self):
        from engine_core.codex_config import write_config
        codex_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, codex_home, True)
        rows = [{"profile": p, "model": f"vendor/model-{p}:free"}
                for p in ("planner", "backend", "test-author")]
        write_config(codex_home, rows, active="planner")
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
        env.update(CODEX_HOME=codex_home, HOME=codex_home)
        for profile, var in (("backend", "OPENROUTER_BACKEND_KEY"),
                             ("test-author", "OPENROUTER_TEST_AUTHOR_KEY")):
            r = subprocess.run(["codex", "--profile", "engine_" + profile.replace("-", "_"), "exec",
                                "--skip-git-repo-check", "hi"], cwd=codex_home, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
            out = r.stdout + r.stderr
            self.assertIn(f"model: vendor/model-{profile}:free", out)
            self.assertIn(f"provider: engine_{profile.replace('-', '_')}", out)
            self.assertIn(var, out)


class TestSearchRepo(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.repo = os.path.join(self.home, "repo")
        files = {
            "src/lease_store.py": "def renew_lease(token):\n    # fencing token guards stale writers\n    return token + 1\n",
            "src/userApi.ts": "export function fetchUserById(id) { return db.get(id) }\n",
            "README.md": "# demo\nA small project.\n",
            ".mcp.json": "{}\n",
        }
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)

    def engine(self):
        import importlib
        os.environ["ENGINE_AI_HOME"] = self.home
        import engine as eng
        eng = importlib.reload(eng)
        e = eng.Engine()
        self.addCleanup(lambda: e.db.close())
        return e

    def test_relevant_file_ranks_first(self):
        e = self.engine()
        e.index_repo(self.repo)
        self.assertEqual(e.search_repo(self.repo, "lease fencing token", 3)[0]["path"], "src/lease_store.py")
        self.assertEqual(e.search_repo(self.repo, "fetch user by id", 3)[0]["path"], "src/userApi.ts")
        self.assertEqual(e.search_repo(self.repo, "zzzz-nothing", 3), [])

    def test_index_from_before_fts_is_backfilled(self):
        e = self.engine()
        e.index_repo(self.repo)
        e.db.execute("DELETE FROM rag_fts")  # simulate a pre-FTS database
        e.db.commit()
        e.index_repo(self.repo)  # every file unchanged → nothing re-chunked
        self.assertEqual(e.search_repo(self.repo, "fencing", 1)[0]["path"], "src/lease_store.py")


if __name__ == "__main__":
    unittest.main()
