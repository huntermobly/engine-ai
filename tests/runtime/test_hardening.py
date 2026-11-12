"""Regression tests for the engine build workflow hardening (v0.18).

Every test here reproduces a defect found while running the real agent pipeline;
each FAILED on v0.17.6 (37583f7) and passes after the fix. All providers are fakes —
no network, no keys.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from engine_core.config import Config  # noqa: E402
from engine_core.pipeline import BuildPipeline, PipelineError  # noqa: E402
from engine_core.profiles import Assignments  # noqa: E402
from engine_core.providers.base import Completion, Provider, ProviderError, Usage  # noqa: E402
from engine_core.tools.executor import Executor  # noqa: E402
from engine_core.tools.policy import Policy  # noqa: E402
from engine_core.tools.registry import Registry, build_registry  # noqa: E402

try:
    from engine_core.tools import sandbox
    HAVE_SANDBOX = sandbox.available()
except ImportError:  # v0.17.6 had no sandbox module at all
    sandbox, HAVE_SANDBOX = None, False

FREE = "fake/model:free"


# ----------------------------------------------------------------- fakes
def _tool_call(name, args, call_id="call_0"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptProvider(Provider):
    """Planner returns `plan`; a worker for task T plays `tasks[T]` step by step.
    A step is {"tool", "args", "id"} or a final text; a callable step gets the packet."""
    name = "script"

    def __init__(self, plan, tasks, log, profile="?", fail=None):
        self.plan, self.tasks, self.log, self.profile, self.fail = plan, tasks, log, profile, fail

    def complete(self, model, messages, *, tools=None, max_output_tokens=None, temperature=None):
        system, packet = messages[0].content, messages[1].content
        if "planner" in system:
            self.log.append({"role": "planner", "profile": self.profile, "model": model,
                             "prompt": packet})
            return Completion(json.dumps(self.plan), model, Usage(input_tokens=100, output_tokens=50))
        tid = re.search(r"# Task packet (\S+)", packet).group(1)
        turn = sum(1 for m in messages if m.role == "assistant")
        self.log.append({"role": "worker", "task": tid, "profile": self.profile, "model": model,
                         "packet": packet, "turn": turn, "tools": [t["function"]["name"] for t in tools or []]})
        if self.fail and self.fail(tid, self.profile):
            raise self.fail(tid, self.profile)
        steps = self.tasks.get(tid, ['{"status": "done", "summary": "nothing to do"}'])
        step = steps[min(turn, len(steps) - 1)]
        if callable(step):
            step = step(packet)
        if isinstance(step, dict):
            return Completion("", model, Usage(input_tokens=20, output_tokens=7),
                              tool_calls=[_tool_call(step["tool"], step["args"], step.get("id", "call_0"))])
        return Completion(step, model, Usage(input_tokens=20, output_tokens=7))


DONE = '{"status": "done", "summary": "ok"}'


def _write(path, content, call_id="call_0"):
    return {"tool": "write_file", "args": {"path": path, "content": content}, "id": call_id}


def make_pipeline(home, plan, tasks, *, log=None, fail=None, config=None, model=FREE,
                  memory=None, models=None):
    assignments = Assignments(home, config)
    for name in assignments.profiles:
        assignments.set(name, credential_ref="fake-key",
                        model_ref=(models or {}).get(name, model))
    log = log if log is not None else []

    def factory(profile, key):
        return ScriptProvider(plan, tasks, log, profile=profile.name, fail=fail)

    pipe = BuildPipeline(home, config or Config(), assignments, provider_factory=factory,
                         memory=memory)
    return pipe, log


def task(tid, profile="frontend", deps=(), owned=(), artifacts=None, verification=(),
         objective=None):
    t = {"id": tid, "title": tid, "profile": profile, "dependencies": list(deps),
         "owned_paths": list(owned), "verification": list(verification),
         "objective": objective or f"do {tid}"}
    if artifacts is not None:
        t["artifacts"] = list(artifacts)
    return t


# ----------------------------------------------------------------- effect journal
class TestEffectJournal(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.journal = os.path.join(tempfile.mkdtemp(), "effects.db")

    def _ex(self):
        ex = Executor(build_registry(), Policy(root=self.ws), journal_path=self.journal)
        self.addCleanup(ex.close)
        return ex

    def _write(self, ex, path, content, ctx, call_id="call_0"):
        return ex.execute(_tool_call("write_file", {"path": path, "content": content}, call_id),
                          context={"workspace": self.ws, **ctx})

    def test_same_call_id_in_another_task_or_run_still_writes(self):
        # free models number their calls call_0, call_1… in every task: a GLOBAL
        # journal key made task B's write look "already applied" and skipped it
        self._write(self._ex(), "a.txt", "A", {"task_id": "contract", "run_id": "run1"})
        r = self._write(self._ex(), "b.txt", "B", {"task_id": "impl", "run_id": "run1"})
        self.assertFalse(r.get("replayed"), r)
        r = self._write(self._ex(), "c.txt", "C", {"task_id": "contract", "run_id": "run2"})
        self.assertFalse(r.get("replayed"), r)
        for name, body in (("a.txt", "A"), ("b.txt", "B"), ("c.txt", "C")):
            with open(os.path.join(self.ws, name)) as f:
                self.assertEqual(f.read(), body)

    def test_legitimate_retry_replay_does_not_repeat_effect(self):
        calls = []
        reg = Registry()
        reg.register("append", lambda a, c=None: calls.append(a) or {"ok": True})
        ex = Executor(reg, Policy(root=self.ws), journal_path=self.journal)
        self.addCleanup(ex.close)
        call = _tool_call("append", {"line": "x"}, "call_7")
        ctx = {"task_id": "t", "run_id": "r", "workspace": self.ws}
        ex.execute(call, context=ctx)
        again = Executor(reg, Policy(root=self.ws), journal_path=self.journal)  # after a crash
        self.addCleanup(again.close)
        r = again.execute(call, context=ctx)
        self.assertTrue(r.get("replayed"))
        self.assertEqual(len(calls), 1, "side effect applied exactly once")

    def test_same_call_id_with_different_args_is_not_a_replay(self):
        ex = self._ex()
        ctx = {"task_id": "t", "run_id": "r"}
        self._write(ex, "a.txt", "first", ctx)
        r = self._write(ex, "a.txt", "second", ctx)  # model reused call_0 for a new write
        self.assertFalse(r.get("replayed"), r)
        with open(os.path.join(self.ws, "a.txt")) as f:
            self.assertEqual(f.read(), "second")

    def test_worker_resumed_from_checkpoint_replays_instead_of_repeating(self):
        """crash after a write → resume the SAME attempt from its checkpoint: the model
        re-issues call_0, the journal answers it, the append happens exactly once."""
        from engine_core.runtime.state import StateStore
        from engine_core.runtime.task_graph import TaskSpec
        from engine_core.runtime.worker import Worker
        store = StateStore(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.addCleanup(store.close)
        store.upsert(TaskSpec.from_dict({"id": "t", "title": "t", "objective": "o"}))
        calls = {"n": 0}

        class Flaky(Provider):
            def __init__(self, crash):
                self.crash = crash

            def complete(self, model, messages, **kw):
                if not any(m.role == "tool" for m in messages):
                    return Completion("", model, Usage(), tool_calls=[_tool_call(
                        "append_file", {"path": "log.txt", "content": "line\n"}, "call_0")])
                if self.crash:
                    raise ProviderError(ProviderError.QUOTA, "connection lost mid-task")
                return Completion(DONE, model, Usage())
        from engine_core.config import Profile
        prof = Profile("frontend")
        a1 = store.claim("t", owner="w1")
        ex = self._ex
        out1 = Worker(Flaky(True), FREE, ex(), store, Config(), prof, finalize=False, run_id="r").run(
            {**a1, "attempt": 1}, "# Task packet t")
        self.assertEqual(out1["status"], "provider-quota")
        ckpt = store.get("t")["checkpoint"]
        store._set("t", "pending", lease_owner=None, lease_expires=None)
        a2 = store.claim("t", owner="w2")
        Flaky.complete_calls = 0

        class Replay(Flaky):  # resumed model starts over and re-issues call_0
            def complete(self, model, messages, **kw):
                tool_msgs = [m for m in messages if m.role == "tool"]
                if len(tool_msgs) <= 1 and not any("already_applied" in m.content for m in tool_msgs):
                    return Completion("", model, Usage(), tool_calls=[_tool_call(
                        "append_file", {"path": "log.txt", "content": "line\n"}, "call_0")])
                return Completion(DONE, model, Usage())
        out2 = Worker(Replay(False), FREE, ex(), store, Config(), prof, finalize=False, run_id="r").run(
            {**a2, "attempt": 2}, "# Task packet t", checkpoint=ckpt)
        self.assertEqual(out2["status"], "done")
        self.assertEqual(out2["effect_scope"], out1["effect_scope"], "resume must keep its attempt's scope")
        with open(os.path.join(self.ws, "log.txt")) as f:
            self.assertEqual(f.read(), "line\n", "the resumed attempt repeated an applied write")

    def _run_worker(self, replies):
        """Run one worker attempt against a scripted list of Completions (by model turn)."""
        from engine_core.config import Profile
        from engine_core.runtime.state import StateStore
        from engine_core.runtime.task_graph import TaskSpec
        from engine_core.runtime.worker import Worker
        store = StateStore(os.path.join(tempfile.mkdtemp(), "s.db"))
        self.addCleanup(store.close)
        store.upsert(TaskSpec.from_dict({"id": "t", "title": "t", "objective": "o"}))
        seen = []

        class Scripted(Provider):
            def complete(self, model, messages, **kw):
                seen.append([m.content for m in messages if m.role == "user"][-1])
                return replies[min(len(seen) - 1, len(replies) - 1)]
        a = store.claim("t", owner="w")
        out = Worker(Scripted(), FREE, self._ex(), store, Config(), Profile("test-author"),
                     finalize=False, run_id="r").run({**a, "attempt": 1}, "# Task packet t")
        return out, seen

    def test_truncated_reply_is_reprompted_not_taken_as_final(self):
        # the live tic-tac-toe build: a free model emitted a whole test file in one call,
        # hit max_output_tokens, and the empty truncated reply ended the attempt
        write = Completion("", FREE, Usage(), tool_calls=[_tool_call(
            "write_file", {"path": "a.txt", "content": "part1"}, "call_0")])
        out, seen = self._run_worker([Completion("", FREE, Usage(), finish_reason="length"),
                                      write, Completion(DONE, FREE, Usage())])
        self.assertEqual(out["status"], "done", out)
        self.assertIn("append_file", seen[1], "the model must be told to write in parts")
        with open(os.path.join(self.ws, "a.txt")) as f:
            self.assertEqual(f.read(), "part1")

    def test_empty_reply_is_reprompted(self):
        out, seen = self._run_worker([Completion("  ", FREE, Usage()), Completion(DONE, FREE, Usage())])
        self.assertEqual(out["status"], "done", out)
        self.assertIn("was empty", seen[1])

    def test_repeated_truncation_gives_up_with_the_real_cause(self):
        out, seen = self._run_worker([Completion("", FREE, Usage(), finish_reason="length")])
        self.assertEqual(out["status"], "unstructured")
        self.assertIn("max_output_tokens", out["error"])
        self.assertEqual(len(seen), 3, "original call + exactly MAX_NUDGES re-prompts")

    def test_internal_typeerror_does_not_execute_tool_twice(self):
        calls = []

        def tool(args, context):
            calls.append(1)
            raise TypeError("bug inside the tool")
        reg = Registry()
        reg.register("t", tool)
        Executor(reg, Policy(root=self.ws)).execute(_tool_call("t", {}), context={"task_id": "x"})
        self.assertEqual(len(calls), 1, "a TypeError raised INSIDE the tool re-ran it")


# ----------------------------------------------------------------- ownership
class TestOwnership(unittest.TestCase):
    def setUp(self):
        self.ws = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(self.ws, "own"))
        os.makedirs(os.path.join(self.ws, "other"))
        with open(os.path.join(self.ws, "other", "contract.md"), "w") as f:
            f.write("SHARED CONTRACT")
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.ex = Executor(build_registry(), Policy(root=self.ws, permitted_paths=["own"]))

    def _call(self, name, args, ex=None):
        return (ex or self.ex).execute(_tool_call(name, args),
                                       context={"workspace": self.ws, "task_id": "t", "run_id": "r"})

    def _exists(self, *rel):
        return os.path.exists(os.path.join(self.ws, *rel))

    def test_write_inside_owned_path_allowed(self):
        self.assertTrue(self._call("write_file", {"path": "own/a.txt", "content": "x"})["ok"])
        self.assertTrue(self._exists("own", "a.txt"))

    def test_append_respects_ownership(self):
        self.assertTrue(self._call("append_file", {"path": "own/big.html", "content": "a"})["ok"])
        self.assertTrue(self._call("append_file", {"path": "own/big.html", "content": "b"})["ok"])
        with open(os.path.join(self.ws, "own", "big.html")) as f:
            self.assertEqual(f.read(), "ab")
        r = self._call("append_file", {"path": "other/contract.md", "content": "x"})
        self.assertFalse(r["ok"])

    def test_cross_task_write_blocked(self):
        r = self._call("write_file", {"path": "other/contract.md", "content": "pwned"})
        self.assertFalse(r["ok"], r)
        with open(os.path.join(self.ws, "other", "contract.md")) as f:
            self.assertEqual(f.read(), "SHARED CONTRACT")

    def test_parent_traversal_blocked(self):
        r = self._call("write_file", {"path": "own/../other/x.txt", "content": "x"})
        self.assertFalse(r["ok"], r)
        self.assertFalse(self._exists("other", "x.txt"))

    def test_absolute_paths(self):
        r = self._call("write_file", {"path": os.path.join(self.outside, "x"), "content": "x"})
        self.assertFalse(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.outside, "x")))
        r = self._call("write_file", {"path": os.path.join(self.ws, "own", "abs.txt"), "content": "x"})
        self.assertTrue(r["ok"], r)

    def test_symlink_file_escape_blocked(self):
        os.symlink(os.path.join(self.ws, "other", "contract.md"), os.path.join(self.ws, "own", "link"))
        r = self._call("write_file", {"path": "own/link", "content": "pwned"})
        self.assertFalse(r["ok"], r)
        with open(os.path.join(self.ws, "other", "contract.md")) as f:
            self.assertEqual(f.read(), "SHARED CONTRACT")

    def test_symlink_dir_escape_blocked(self):
        os.symlink(self.outside, os.path.join(self.ws, "own", "dir"))
        r = self._call("write_file", {"path": "own/dir/x.txt", "content": "x"})
        self.assertFalse(r["ok"], r)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "x.txt")))

    def test_relative_cwd_is_workspace_relative(self):
        # the handler joined a relative cwd onto the SERVER's cwd while the policy
        # checked it against the workspace — writes landed outside the checked path
        old = os.getcwd()
        os.chdir(self.outside)
        try:
            r = self._call("write_file", {"path": "a.txt", "content": "x", "cwd": "own"})
        finally:
            os.chdir(old)
        self.assertTrue(r["ok"], r)
        self.assertTrue(self._exists("own", "a.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.outside, "own", "a.txt")))

    def test_shared_reads_allowed(self):
        r = self._call("read_file", {"path": "other/contract.md"})
        self.assertTrue(r["ok"], r)
        self.assertIn("SHARED CONTRACT", r["content"])

    def test_credential_files_never_readable(self):
        for rel in ("keys.csv", "engine-ai-env.sh"):
            with open(os.path.join(self.ws, rel), "w") as f:
                f.write("sk-or-v1-secret")
            r = self._call("read_file", {"path": rel})
            self.assertFalse(r["ok"], (rel, r))
            self.assertNotIn("sk-or-v1-secret", r["content"])


@unittest.skipUnless(HAVE_SANDBOX, "bubblewrap sandbox unavailable on this host")
class TestShellIsolation(unittest.TestCase):
    def setUp(self):
        self.ws = os.path.realpath(tempfile.mkdtemp())
        for d in ("own", "other"):
            os.makedirs(os.path.join(self.ws, d))
        with open(os.path.join(self.ws, "other", "contract.md"), "w") as f:
            f.write("SHARED CONTRACT")
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.realpath(tempfile.mkdtemp())  # stands in for ~/.engine-ai
        with open(os.path.join(self.home, "master.key"), "w") as f:
            f.write("SECRET-MASTER-KEY")
        self.ex = Executor(build_registry(), Policy(root=self.ws, permitted_paths=["own"],
                                                    hidden_paths=[self.home]))

    def _sh(self, cmd, ex=None):
        r = (ex or self.ex).execute(_tool_call("run_command", {"command": cmd}),
                                    context={"workspace": self.ws, "task_id": "t", "run_id": "r"})
        return r, json.loads(r["content"]) if r["content"].startswith("{") else {}

    def test_shell_can_read_shared_and_write_owned(self):
        r, out = self._sh("cat other/contract.md && echo ok > own/x.txt")
        self.assertTrue(r["ok"], r)
        self.assertIn("SHARED CONTRACT", out["stdout"])
        self.assertTrue(os.path.exists(os.path.join(self.ws, "own", "x.txt")))

    def test_shell_cannot_write_another_tasks_path(self):
        self._sh("echo pwned > other/contract.md; echo new > other/new.txt")
        with open(os.path.join(self.ws, "other", "contract.md")) as f:
            self.assertEqual(f.read(), "SHARED CONTRACT")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "other", "new.txt")))

    def test_shell_cannot_write_outside_workspace(self):
        self._sh(f"echo x > {self.outside}/escaped.txt; cd /; echo y > {self.outside}/y.txt")
        self.assertEqual(os.listdir(self.outside), [])

    def test_shell_cannot_escape_via_symlink(self):
        os.symlink(os.path.join(self.ws, "other", "contract.md"), os.path.join(self.ws, "own", "l"))
        self._sh("echo pwned > own/l")
        with open(os.path.join(self.ws, "other", "contract.md")) as f:
            self.assertEqual(f.read(), "SHARED CONTRACT")

    def test_shell_cannot_read_credential_store(self):
        _, out = self._sh(f"cat {self.home}/master.key; ls ~ | wc -l")
        self.assertNotIn("SECRET-MASTER-KEY", json.dumps(out))

    def test_shell_hides_the_whole_home_directory(self):
        # a token anywhere under $HOME (not just the known stores) must be invisible.
        # The fake home must NOT live under /tmp (the sandbox's private tmpfs hides /tmp
        # anyway, which would make this test pass without the $HOME mask).
        base = "/var/tmp" if os.access("/var/tmp", os.W_OK) else os.path.expanduser("~")
        fake_home = os.path.realpath(tempfile.mkdtemp(prefix="engine-ai-test-home-", dir=base))
        self.addCleanup(shutil.rmtree, fake_home, True)
        os.makedirs(os.path.join(fake_home, "notes"))
        with open(os.path.join(fake_home, "notes", "token.txt"), "w") as f:
            f.write("HOME-SECRET-TOKEN")
        old = os.environ.get("HOME")
        os.environ["HOME"] = fake_home
        try:
            ex = Executor(build_registry(), Policy(root=self.ws, permitted_paths=["own"]))
            _, out = self._sh(f"cat {fake_home}/notes/token.txt; ls -A ~", ex)
        finally:
            os.environ["HOME"] = old
        self.assertNotIn("HOME-SECRET-TOKEN", json.dumps(out))
        self.assertNotIn("notes", out.get("stdout", ""))

    def test_read_only_profile_shell_persists_nothing(self):
        ex = Executor(build_registry(), Policy(read_only=True, root=self.ws, permitted_paths=["own"]))
        r, out = self._sh("echo t > own/t.txt; python3 -c 'print(40+2)'", ex)
        self.assertIn("42", out.get("stdout", ""), r)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "own", "t.txt")))


class TestShellWithoutIsolation(unittest.TestCase):
    def test_mutating_shell_refused_when_no_sandbox(self):
        ws = tempfile.mkdtemp()
        outside = tempfile.mkdtemp()
        ex = Executor(build_registry(), Policy(root=ws, shell_isolation="none"))
        r = ex.execute(_tool_call("run_command", {"command": f"echo x > {outside}/x"}),
                       context={"workspace": ws, "task_id": "t", "run_id": "r"})
        self.assertFalse(r["ok"])
        self.assertEqual(os.listdir(outside), [], "an unisolated shell must not run")


# ----------------------------------------------------------------- worker outcome
class TestWorkerAndProvider(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_unstructured_final_answer_is_not_done(self):
        plan = {"tasks": [task("t", owned=["out"])]}
        pipe, _ = make_pipeline(self.home, plan, {"t": ["I have finished everything!"]})
        rep = pipe.run("x", workspace=os.path.join(self.home, "ws"))
        self.assertNotEqual(rep["complete"], rep["total"], "prose was counted as done")

    def test_missing_artifact_fails_even_if_model_claims_done(self):
        plan = {"tasks": [task("t", owned=["index.html"], artifacts=["index.html"])]}
        pipe, _ = make_pipeline(self.home, plan, {"t": [DONE]})
        rep = pipe.run("x", workspace=os.path.join(self.home, "ws"))
        t = {r["id"]: r for r in rep["tasks"]}["t"]
        self.assertNotEqual(t["status"], "done")
        self.assertIn("index.html", json.dumps(t))

    def test_daily_free_quota_429_is_quota_and_not_retried(self):
        from engine_core.providers import openrouter as OR
        sent = []

        def transport(url, body, headers, timeout):
            sent.append(1)
            return 429, {"error": {"message": "Rate limit exceeded: free-models-per-day. "
                                              "Add 10 credits to unlock 1000 free model requests per day"}}
        sleep, OR.time.sleep = OR.time.sleep, lambda s: None
        try:
            p = OR.OpenRouterProvider(api_key="k", transport=transport)
            with self.assertRaises(ProviderError) as cm:
                p.complete("m:free", [])
        finally:
            OR.time.sleep = sleep
        self.assertEqual(cm.exception.kind, ProviderError.QUOTA)
        self.assertEqual(len(sent), 1, "a daily quota must not be retried")

    def test_charge_under_free_only_is_refused(self):
        from engine_core.providers import openrouter as OR

        def transport(url, body, headers, timeout):
            return 200, {"model": body["model"], "choices": [{"message": {"content": "hi"}}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.02}}
        p = OR.OpenRouterProvider(api_key="k", transport=transport, free_only=True)
        with self.assertRaises(ProviderError) as cm:
            p.complete("m:free", [])
        self.assertEqual(cm.exception.kind, ProviderError.QUOTA)

    def test_paid_model_refused_under_free_only_before_any_call(self):
        plan = {"tasks": [task("t")]}
        log = []
        pipe, _ = make_pipeline(self.home, plan, {}, log=log, model="openai/gpt-4o")
        with self.assertRaises(PipelineError) as cm:
            pipe.run("x", workspace=os.path.join(self.home, "ws"))
        self.assertIn("free", str(cm.exception).lower())
        self.assertEqual(log, [], "no model may be called")

    def test_task_wall_clock_timeout(self):
        plan = {"tasks": [task("t", owned=["a"])]}

        def slow(packet):
            time.sleep(0.4)
            return {"tool": "list_dir", "args": {"path": "."}}
        cfg = Config()
        cfg.task_timeout_s = 0.5
        pipe, _ = make_pipeline(self.home, plan, {"t": [slow]}, config=cfg)
        t0 = time.time()
        rep = pipe.run("x", workspace=os.path.join(self.home, "ws"))
        self.assertLess(time.time() - t0, 5)
        t = {r["id"]: r for r in rep["tasks"]}["t"]
        self.assertIn("timeout", t["status"])


# ----------------------------------------------------------------- pipeline
class TestPipelineWorkflow(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ws = os.path.join(self.home, "ws")

    PLAN = {"tasks": [
        task("contract", profile="planner", owned=["SPEC.md"], artifacts=["SPEC.md"]),
        task("impl", profile="frontend", deps=["contract"], owned=["index.html"],
             artifacts=["index.html"]),
        task("tests", profile="test-author", deps=["impl"], owned=["tests"],
             artifacts=["tests/t.py"]),
        task("review", profile="reviewer", deps=["impl", "tests"]),
    ]}
    SCRIPT = {
        "contract": [_write("SPEC.md", "CELL IDS: cell-0..cell-8"), DONE],
        "impl": [_write("index.html", "<html>game</html>"), DONE],
        "tests": [_write("tests/t.py", "print('ok')"), DONE],
        "review": ['{"status": "done", "summary": "LGTM"}'],
    }

    def test_contract_and_dependency_outputs_are_handed_to_dependents(self):
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        rep = pipe.run("build it", workspace=self.ws)
        self.assertEqual(rep["status"], "complete", rep)
        impl = next(e for e in log if e.get("task") == "impl")
        self.assertIn("CELL IDS: cell-0..cell-8", impl["packet"], "contract text not handed over")
        review = next(e for e in log if e.get("task") == "review")
        self.assertIn("<html>game</html>", review["packet"], "reviewer never saw the implementation")
        self.assertIn("index.html", review["packet"])

    def test_profiles_and_models_routed_per_task(self):
        models = {"planner": "p/plan:free", "frontend": "p/front:free",
                  "test-author": "p/test:free", "reviewer": "p/rev:free"}
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT, models=models)
        pipe.run("x", workspace=self.ws)
        seen = {(e.get("task"), e["profile"], e["model"]) for e in log if e["role"] == "worker"}
        self.assertIn(("impl", "frontend", "p/front:free"), seen)
        self.assertIn(("review", "reviewer", "p/rev:free"), seen)
        self.assertEqual(log[0]["model"], "p/plan:free")

    def test_reviewer_is_read_only(self):
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        pipe.run("x", workspace=self.ws)
        review = next(e for e in log if e.get("task") == "review")
        self.assertNotIn("write_file", review["tools"])

    def test_report_lists_every_planned_task_with_details(self):
        script = dict(self.SCRIPT, impl=['{"status": "blocked", "summary": "cannot"}'])
        pipe, _ = make_pipeline(self.home, self.PLAN, script)
        rep = pipe.run("x", workspace=self.ws)
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertEqual(set(by), {"contract", "impl", "tests", "review"})
        self.assertEqual(by["tests"]["status"], "not-run")
        self.assertEqual(by["review"]["dependencies"], ["impl", "tests"])
        self.assertEqual(by["impl"]["profile"], "frontend")
        self.assertIn("SPEC.md", by["contract"]["artifacts"])
        self.assertNotEqual(rep["status"], "complete")
        self.assertNotEqual(rep["exit_code"], 0)

    def test_quota_stops_run_without_trying_other_profiles(self):
        plan = {"tasks": [task("a", profile="frontend"), task("b", profile="backend")]}

        def fail(tid, profile):
            return ProviderError(ProviderError.QUOTA, "free-models-per-day") if profile == "frontend" else None
        pipe, log = make_pipeline(self.home, plan, {}, fail=fail)
        rep = pipe.run("x", workspace=self.ws)
        self.assertFalse(any(e.get("task") == "b" for e in log),
                         "after a quota error the run must stop, not move on to another key")
        self.assertEqual(rep["status"], "blocked")
        self.assertEqual(rep["exit_code"], 2)
        self.assertIn("b", rep["not_run"])

    def test_usage_separates_planner_and_workers(self):
        pipe, _ = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        rep = pipe.run("x", workspace=self.ws)
        u = rep["usage"]
        self.assertEqual(u["planner"], {"model_calls": 1, "input_tokens": 100, "output_tokens": 50})
        # contract 2 turns, impl 2, tests 2, review 1 = 7 worker model calls, 3 tool calls
        self.assertEqual(u["workers"]["model_calls"], 7)
        self.assertEqual(u["workers"]["tool_calls"], 3)
        self.assertEqual(u["workers"]["input_tokens"], 7 * 20)
        self.assertEqual(u["workers"]["output_tokens"], 7 * 7)
        self.assertEqual(u["total"]["input_tokens"], 100 + 140)

    def test_verification_commands_keep_their_quoting(self):
        plan = {"tasks": [task("t", owned=["a.txt"], artifacts=["a.txt"],
                               verification=["python3 -c 'import sys; sys.exit(0)'"])]}
        pipe, _ = make_pipeline(self.home, plan, {"t": [_write("a.txt", "x"), DONE]})
        rep = pipe.run("x", workspace=self.ws)
        self.assertEqual(rep["status"], "complete", json.dumps(rep["tasks"], indent=1))

    @unittest.skipUnless(HAVE_SANDBOX, "bubblewrap sandbox unavailable on this host")
    def test_verification_commands_are_sandboxed(self):
        outside = tempfile.mkdtemp()
        plan = {"tasks": [task("t", owned=["a.txt"], artifacts=["a.txt"],
                               verification=[f"bash -c 'echo x > {outside}/v.txt'"])]}
        pipe, _ = make_pipeline(self.home, plan, {"t": [_write("a.txt", "x"), DONE]})
        pipe.run("x", workspace=self.ws)
        self.assertEqual(os.listdir(outside), [], "a planner-chosen command escaped the workspace")

    def test_failed_task_retried_with_its_failure_as_context(self):
        plan = {"tasks": [task("t", owned=["a.txt"], artifacts=["a.txt"])]}

        def attempt(packet):
            if "Latest failure" in packet:
                return {"tool": "write_file", "args": {"path": "a.txt", "content": "x"}}
            return DONE  # first attempt claims done without writing
        pipe, log = make_pipeline(self.home, plan, {"t": [attempt, DONE]})
        rep = pipe.run("x", workspace=self.ws)
        self.assertEqual(rep["status"], "complete", rep["tasks"])
        self.assertEqual({t["id"]: t for t in rep["tasks"]}["t"]["attempts"], 2)

    def test_resume_reuses_stored_plan_and_validates_artifacts(self):
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        pipe.run("x", workspace=self.ws)
        os.remove(os.path.join(self.ws, "index.html"))  # artifact vanished
        log.clear()
        pipe2, log2 = make_pipeline(self.home, {"tasks": []}, self.SCRIPT, log=log)
        rep = pipe2.resume(self.ws)
        self.assertFalse(any(e["role"] == "planner" for e in log2), "resume must not re-plan")
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertTrue(by["contract"].get("resumed"))
        self.assertFalse(by["impl"].get("resumed"), "impl's artifact is gone — it must rebuild")
        self.assertFalse(by["tests"].get("resumed"), "dependency changed → downstream re-runs")
        self.assertEqual(rep["status"], "complete")

    def test_resume_with_everything_valid_runs_no_model(self):
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        pipe.run("x", workspace=self.ws)
        log.clear()
        rep = make_pipeline(self.home, {"tasks": []}, self.SCRIPT, log=log)[0].resume(self.ws)
        self.assertEqual(log, [])
        self.assertTrue(all(t.get("resumed") for t in rep["tasks"]))
        self.assertEqual(rep["exit_code"], 0)

    def test_edited_artifact_invalidates_downstream(self):
        pipe, log = make_pipeline(self.home, self.PLAN, self.SCRIPT)
        pipe.run("x", workspace=self.ws)
        with open(os.path.join(self.ws, "SPEC.md"), "a") as f:
            f.write("\nchanged by hand")
        log.clear()
        rep = make_pipeline(self.home, {"tasks": []}, self.SCRIPT, log=log)[0].resume(self.ws)
        ran = {e["task"] for e in log if e["role"] == "worker"}
        # the hand-edited task is RE-VERIFIED and kept (its agent does not overwrite the
        # edit); everything built on it rebuilds against the new content
        self.assertEqual(ran, {"impl", "tests", "review"})
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertTrue(by["contract"].get("reverified"))
        with open(os.path.join(self.ws, "SPEC.md")) as f:
            self.assertIn("changed by hand", f.read())

    UPSTREAM_PLAN = {"tasks": [
        task("lib", profile="backend", owned=["calc.py"], artifacts=["calc.py"]),
        task("tests", profile="test-author", deps=["lib"], owned=["test_calc.py"],
             artifacts=["test_calc.py"], verification=["python3 -m unittest -q test_calc"])]}
    TEST_SRC = "import unittest, calc\nclass T(unittest.TestCase):\n    def test(self): self.assertEqual(calc.add(2, 3), 5)\n"
    UPSTREAM = '{"status": "upstream-defect", "file": "calc.py", "summary": "add() subtracts: add(2,3) == -1"}'

    def _tests_steps(self):
        def final(packet):
            if "was reopened" in packet:
                return DONE
            return self.UPSTREAM if "Latest failure" in packet else DONE
        return [_write("test_calc.py", self.TEST_SRC), final]

    def test_upstream_defect_reopens_the_owner_instead_of_blind_retries(self):
        lib = lambda p: _write("calc.py", "def add(a, b):\n    return a + b\n" if "found a defect" in p
                               else "def add(a, b):\n    return a - b\n")
        pipe, log = make_pipeline(self.home, self.UPSTREAM_PLAN,
                                  {"lib": [lib, DONE], "tests": self._tests_steps()})
        rep = pipe.run("x", workspace=self.ws)
        self.assertEqual(rep["status"], "complete", rep["tasks"])
        reopened = [r for r in rep["results"] if r.get("reopened_by") == "tests"]
        self.assertEqual([r["task"] for r in reopened], ["lib"])
        self.assertTrue(any("found a defect in calc.py" in e["packet"] for e in log
                            if e["role"] == "worker" and e["task"] == "lib"),
                        "the owner must receive the downstream evidence")
        # and the fixed run resumes cleanly: nothing re-runs
        log.clear()
        rep2 = make_pipeline(self.home, {"tasks": []}, {}, log=log)[0].resume(self.ws)
        self.assertEqual(rep2["status"], "complete")
        self.assertEqual([e for e in log if e["role"] == "worker"], [])

    def test_upstream_defect_owner_reopened_only_once(self):
        lib = _write("calc.py", "def add(a, b):\n    return a - b\n")  # never fixes it
        always = lambda p: self.UPSTREAM if "Latest failure" in p else DONE
        pipe, log = make_pipeline(self.home, self.UPSTREAM_PLAN,
                                  {"lib": [lib, DONE], "tests": [_write("test_calc.py", self.TEST_SRC), always]})
        rep = pipe.run("x", workspace=self.ws)
        self.assertEqual(rep["status"], "failed")
        lib_runs = {e["packet"] for e in log if e["role"] == "worker" and e["task"] == "lib" and e["turn"] == 0}
        self.assertEqual(len(lib_runs), 2, "original run + exactly one reopen")
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertEqual(by["tests"]["status"], "upstream-defect")

    def test_upstream_defect_in_unowned_file_names_the_problem(self):
        steps = [_write("test_calc.py", self.TEST_SRC),
                 lambda p: ('{"status": "upstream-defect", "file": "vendor/x.py", "summary": "bad"}'
                            if "Latest failure" in p else DONE)]
        lib = _write("calc.py", "def add(a, b):\n    return a - b\n")
        pipe, log = make_pipeline(self.home, self.UPSTREAM_PLAN, {"lib": [lib, DONE], "tests": steps})
        rep = pipe.run("x", workspace=self.ws)
        by = {t["id"]: t for t in rep["tasks"]}
        self.assertIn("no upstream task", by["tests"]["error"])
        self.assertEqual(by["tests"]["attempts"], 2, "no retries after an upstream-defect report")

    def test_edited_artifact_failing_its_checks_reruns_the_task(self):
        plan = {"tasks": [task("t", owned=["a.txt"], artifacts=["a.txt"],
                               verification=["grep -q good a.txt"])]}
        script = {"t": [_write("a.txt", "good"), DONE]}
        pipe, log = make_pipeline(self.home, plan, script)
        self.assertEqual(pipe.run("x", workspace=self.ws)["status"], "complete")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("broken by hand")
        log.clear()
        rep = make_pipeline(self.home, {"tasks": []}, script, log=log)[0].resume(self.ws)
        self.assertIn("t", {e["task"] for e in log if e["role"] == "worker"})
        self.assertEqual(rep["status"], "complete")

    def test_concurrent_runs_in_one_workspace_refused(self):
        gate = threading.Event()

        def slow(packet):
            gate.wait(5)
            return DONE
        plan = {"tasks": [task("t")]}
        pipe, _ = make_pipeline(self.home, plan, {"t": [slow]})
        th = threading.Thread(target=lambda: pipe.run("x", workspace=self.ws))
        th.start()
        time.sleep(0.3)
        try:
            with self.assertRaises(PipelineError) as cm:
                make_pipeline(self.home, plan, {"t": [DONE]})[0].run("x", workspace=self.ws)
            self.assertIn("busy", str(cm.exception))
        finally:
            gate.set()
            th.join(10)

    def test_concurrent_builds_with_overlapping_ids_stay_separate(self):
        plan = {"tasks": [task("contract", owned=["out.txt"], artifacts=["out.txt"]),
                          task("impl", deps=["contract"], owned=["impl.txt"], artifacts=["impl.txt"])]}
        reports, errors = {}, []

        def build(name):
            try:
                script = {"contract": [_write("out.txt", f"from {name}"), DONE],
                          "impl": [_write("impl.txt", f"impl {name}"), DONE]}
                p, _ = make_pipeline(self.home, plan, script)
                reports[name] = p.run(name, workspace=os.path.join(self.home, name))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=build, args=(n,)) for n in ("A", "B")]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(errors, [])
        for name in ("A", "B"):
            self.assertEqual(reports[name]["status"], "complete", reports[name])
            with open(os.path.join(self.home, name, "out.txt")) as f:
                self.assertEqual(f.read(), f"from {name}")
        self.assertNotEqual(reports["A"]["state_db"], reports["B"]["state_db"])
        self.assertNotEqual(reports["A"]["run_id"], reports["B"]["run_id"])

    def test_stale_task_outside_graph_cannot_enter_run(self):
        from engine_core.runtime.state import StateStore
        from engine_core.runtime.task_graph import TaskSpec
        pipe, log = make_pipeline(self.home, {"tasks": [task("t")]}, {})
        st = StateStore(pipe.state_path(self.ws))
        self.addCleanup(st.close)
        st.upsert(TaskSpec.from_dict({"id": "stale", "title": "s", "objective": "o"}))
        st.mark_ready(["stale"])
        rep = pipe.run("x", workspace=self.ws)
        self.assertNotIn("stale", {t["id"] for t in rep["tasks"]})
        self.assertFalse(any(e.get("task") == "stale" for e in log))


class TestPlannerOutput(unittest.TestCase):
    def test_bare_json_array_plan_is_accepted(self):
        # live nemotron output (run-20261109-160211): a top-level ARRAY of tasks; v0.17
        # parsed only the first task object and failed with "planner returned no tasks"
        from engine_core.pipeline import parse_plan
        text = '```json\n[{"id": "contract-spec", "profile": "planner"}, ' \
               '{"id": "impl", "dependencies": ["contract-spec"]}]\n```'
        self.assertEqual([t["id"] for t in parse_plan(text)], ["contract-spec", "impl"])
        self.assertEqual([t["id"] for t in parse_plan('Plan: {"tasks": [{"id": "a"}]} ok')], ["a"])
        with self.assertRaises(PipelineError):
            parse_plan("no plan here")

    def test_planning_failure_writes_a_failed_report(self):
        home = tempfile.mkdtemp()

        class Bad(ScriptProvider):
            def complete(self, model, messages, **kw):
                return Completion("I cannot plan this.", model, Usage(input_tokens=5, output_tokens=3))
        assignments = Assignments(home)
        for name in assignments.profiles:
            assignments.set(name, credential_ref="k", model_ref=FREE)
        pipe = BuildPipeline(home, Config(), assignments,
                             provider_factory=lambda p, k: Bad({}, {}, [], p.name))
        ws = os.path.join(home, "ws")
        rep = pipe.run("x", workspace=ws)
        self.assertEqual(rep["status"], "failed")
        self.assertEqual(rep["exit_code"], 1)
        self.assertIn("planner", rep["error"])
        self.assertTrue(os.path.isfile(rep["report_path"]))
        self.assertEqual(rep["usage"]["planner"]["model_calls"], 1)


class TestProjectMemoryHandoff(unittest.TestCase):
    def test_handoff_uses_only_this_projects_memory(self):
        home = tempfile.mkdtemp()
        sys.path.insert(0, os.path.join(REPO, "mcp"))
        from memory import Memory
        mem = Memory(os.path.join(home, "memory.db"))
        self.addCleanup(mem.close)
        mem.save(["tic-tac-toe", "port"], "Serve the game on port 8001.", project="ttt-a", kind="fact")
        mem.save(["tic-tac-toe", "port"], "Serve the game on port 9009.", project="ttt-b", kind="fact")
        plan = {"tasks": [task("t")]}
        for project, mine, theirs in (("ttt-a", "8001", "9009"), ("ttt-b", "9009", "8001")):
            log = []
            pipe, _ = make_pipeline(home, plan, {}, log=log, memory=mem)
            rep = pipe.run("build the tic-tac-toe port", workspace=os.path.join(home, project),
                           project=project)
            packet = next(e for e in log if e["role"] == "worker")["packet"]
            self.assertIn(mine, packet)
            self.assertNotIn(theirs, packet, f"{project} saw another project's memory")
            self.assertIn(mine, log[0]["prompt"], "planner did not get project memory")
            self.assertNotIn(theirs, log[0]["prompt"])
            self.assertEqual(rep["project"], project)
        # the run's verified outcome is saved back to ITS project only
        a = json.dumps(mem.recall(["t"], project="ttt-a", k=10))
        self.assertIn("ttt-a", a)


if __name__ == "__main__":
    unittest.main()
