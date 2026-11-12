"""The multi-agent build pipeline — one objective → planner → specialist workers → verified work.

The planner (its own profile/key/model) decomposes the objective into a VALIDATED task
graph; each task runs under ITS profile's key + model; dependents receive their
dependencies' outcomes AND artifact contents in their task packet; project memory
(scoped to this project only) feeds the planner and every worker; every task is
checked before it counts (required artifacts exist on disk, verification commands
pass inside the sandbox).

State, scheduling and honesty rules (each one a regression test in
tests/runtime/test_hardening.py):
- State is per WORKSPACE (`builds-state/<tag>-<hash>/`): planners reuse generic task ids,
  so a shared DB let one project skip another's tasks. A workspace lock refuses a
  second concurrent run in the same workspace instead of letting leases interleave.
- The plan is stored; `resume(workspace)` re-executes it WITHOUT re-planning. A
  completed task resumes only if its spec is unchanged, its recorded artifacts still
  hash the same, and every dependency also resumed — any re-run upstream task
  invalidates everything downstream.
- Failed tasks are retried (config.max_attempts) with the failure in the packet;
  failed/blocked dependencies stop their dependents; tasks that never ran are reported.
- Quota/auth/paid-refused provider errors STOP the run (status blocked, exit 2) —
  never move on to another profile's key (that would be account rotation).
- Free-only routing: every model the run would use is checked BEFORE the first call.
- Usage is split: planner vs workers (model calls, tool calls, input/output tokens).
  This is the ENGINE's provider usage only — the host assistant (Claude Code / Codex)
  that started the build has its own, separate usage the engine cannot see.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import time
import uuid

from engine_core.config import Config
from engine_core.context.compiler import CodeSpan, ContextPacket
from engine_core.profiles import Assignments
from engine_core.providers.base import ProviderError, is_free_model
from engine_core.providers.limiter import Limits, RateLimiter
from engine_core.providers.openrouter import OpenRouterProvider
from engine_core.providers.base import Message
from engine_core.runtime.state import StateStore
from engine_core.runtime.task_graph import GraphError, TaskGraph, TaskSpec
from engine_core.runtime.worker import Worker
from engine_core.tools.executor import Executor
from engine_core.tools.policy import Policy, default_hidden_paths
from engine_core.tools.registry import Registry, build_registry
from engine_core.verification.evidence import EvidenceManifest
from engine_core.verification.runner import Verifier

PROFILES_HELP = ("planner|frontend|backend|services|database|test-author|reviewer|"
                 "security-review|researcher|documentation")
PLANNER_SYSTEM = (
    "You are engine-ai's planner. Decompose the objective into a MINIMAL task graph "
    "(2-6 tasks) for a team of specialist agents. Give the CONTRACT (interfaces, ids, "
    "file layout), the IMPLEMENTATION, the TESTS and the REVIEW to SEPARATE tasks when the "
    "objective needs them; the contract comes FIRST and the others depend on it. Each "
    "task OWNS the files it writes (owned_paths, workspace-relative, never shared between "
    "tasks) and lists the files it must produce (artifacts). Reviewer/security-review "
    "tasks are READ-ONLY: no owned_paths, no artifacts — they report a verdict. "
    "Verification commands must run offline in a few seconds (e.g. `node --test tests/x.test.js`, "
    "`python3 -m unittest`). Reply with ONLY a JSON object, no prose: {\"tasks\":[{"
    "\"id\":\"kebab-case-id\", \"title\":\"...\", \"profile\":\"" + PROFILES_HELP + "\", "
    "\"dependencies\":[\"other-task-ids\"], \"objective\":\"one bounded packet\", "
    "\"acceptance\":[\"criteria\"], \"owned_paths\":[\"paths\"], \"artifacts\":[\"files\"], "
    "\"verification\":[\"shell commands\"]}]}."
)
WORKER_RULES = (
    "Paths are relative to the workspace root. Read shared files (contracts, upstream "
    "artifacts) freely; WRITE only inside your owned paths — the executor rejects anything "
    "else, and shell commands run in an offline sandbox where only your owned directories "
    "keep writes (create files with write_file). Your required artifacts must exist on disk "
    "before you report done; a verifier re-checks them and re-runs the verification commands.\n"
    "If a check fails because of a defect in a file you do NOT own (an upstream artifact), "
    "do not bend your work around the bug and do not keep retrying — report "
    '{"status":"upstream-defect","file":"<that path>","summary":"<what is wrong + the evidence>"}; '
    "the engine reopens the task that owns the file and then re-runs yours."
)
READ_ONLY_RULES = (
    "You are a READ-ONLY reviewer: you cannot write files. Read the real implementation and "
    "tests (read_file/list_dir, run_command for tests), then report "
    '{"status":"done","summary":"<verdict + concrete findings>","checks":[...]} — or '
    '{"status":"blocked",...} if the implementation is missing or broken.'
)
STOP_STATUSES = ("provider-quota", "provider-auth", "provider-paid_refused", "provider-error")
UPSTREAM = "upstream-defect"
MAX_REOPENS = 1  # per owner task per run: a reopened upstream gets ONE fix attempt cycle
FILE_HINT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


class PipelineError(RuntimeError):
    pass


def run_dir_for(home: str, workspace: str) -> str:
    """builds-state/<tag>-<hash>/ for a workspace — shared by the pipeline and the
    MCP launcher (which must check for a stored plan BEFORE spawning a resume)."""
    ws = os.path.realpath(workspace)
    tag = _slug(os.path.basename(ws), 32) or "ws"
    digest = hashlib.sha256(ws.encode()).hexdigest()[:12]
    return os.path.join(home, "builds-state", f"{tag}-{digest}")


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of model output (tolerates prose/fences)."""
    text = re.sub(r"```(?:json)?|```", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise PipelineError("planner did not return a JSON object; output head: "
                        + text[:300].replace("\n", " "))


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:  # skip string contents (braces inside strings don't count)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_plan(text: str) -> list:
    """Planner output → list of task dicts. Accepts {"tasks": [...]}, a bare top-level
    ARRAY of tasks (free models often answer that way), prose and ``` fences."""
    clean = re.sub(r"```(?:json)?|```", "", text or "").strip()
    candidates = []
    try:
        candidates.append(json.loads(clean))
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = clean.find(open_ch)
        while start != -1 and len(candidates) < 8:
            chunk = _balanced(clean, start, open_ch, close_ch)
            if chunk:
                try:
                    candidates.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass
            start = clean.find(open_ch, start + 1)
    for c in candidates:
        if isinstance(c, dict) and isinstance(c.get("tasks"), list) and c["tasks"]:
            return c["tasks"]
        if isinstance(c, list) and c and all(isinstance(t, dict) and t.get("id") for t in c):
            return c
    raise PipelineError("planner returned no task list; output head: "
                        + (text or "")[:300].replace("\n", " "))


def _sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _keywords(*texts: str, limit: int = 12) -> list[str]:
    stop = {"the", "and", "with", "that", "this", "from", "into", "must", "your", "for",
            "one", "all", "are", "any", "not", "use", "build", "file", "files"}
    out: list[str] = []
    for t in texts:
        for w in re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", (t or "").lower()):
            if w not in stop and w not in out:
                out.append(w)
    return out[:limit]


def _slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n]


class _Lock:
    """flock on the workspace run dir — per open file description, so it also refuses
    a second run from another thread of the same process."""

    def __init__(self, path: str, info: dict):
        self.path, self.info, self.fd = path, info, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.read(self.fd, 400).decode(errors="ignore").strip()
            except OSError:
                holder = ""
            os.close(self.fd)
            self.fd = None
            raise PipelineError(f"workspace busy: another build run holds it ({holder or 'unknown'}) "
                                "— wait for it, or use a different workspace")
        os.ftruncate(self.fd, 0)
        os.write(self.fd, json.dumps(self.info).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class BuildPipeline:
    def __init__(self, home: str | None = None, config: Config | None = None,
                 assignments: Assignments | None = None,
                 provider_factory=None, registry: Registry | None = None,
                 limiter: RateLimiter | None = None, verifier: Verifier | None = None,
                 memory=None, catalog=None, progress=None):
        self.home = home or (os.environ.get("ENGINE_AI_HOME")
                             or os.path.expanduser("~/.engine-ai"))
        self.config = config or Config()
        self.assignments = assignments or Assignments(self.home, self.config)
        self.limiter = limiter or RateLimiter()
        self.limiter.configure("team-primary", Limits(
            requests_per_minute=int(os.environ.get("ENGINE_AI_RPM", 20)),
            requests_per_day=int(os.environ.get("ENGINE_AI_RPD", 50))))
        self.provider_factory = provider_factory or self._default_provider
        self.registry = registry or build_registry()  # read/write/list/run — real tools
        self.hidden = default_hidden_paths() + [self.home]
        self.verifier = verifier or Verifier(sandbox=True, hidden_paths=self.hidden)
        self._memory = memory
        self._owns_memory = memory is None  # never close a memory the caller injected
        self._catalog = catalog
        self._progress_cb = progress
        self._progress_path: str | None = None
        self._status_path: str | None = None
        self._status: dict = {}
        self.usage = {"planner": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0},
                      "workers": {"model_calls": 0, "tool_calls": 0, "input_tokens": 0,
                                  "output_tokens": 0}}

    # ---------------------------------------------------------------- paths
    def run_dir(self, workspace: str) -> str:
        """builds-state/<tag>-<hash>/ — plan.json, state.db, report.json, progress."""
        return run_dir_for(self.home, workspace)

    def state_path(self, workspace: str) -> str:
        """Task-state DB for ONE build workspace (re-running the same workspace
        resumes; a different workspace never sees its tasks)."""
        return os.path.join(self.run_dir(workspace), "state.db")

    # ---------------------------------------------------------------- providers / models
    def _default_provider(self, profile, api_key: str) -> OpenRouterProvider:
        return OpenRouterProvider(api_key=api_key, limiter=self.limiter,
                                  account_group=profile.account_group,
                                  free_only=self.config.free_only,
                                  timeout=int(min(180, self.config.task_timeout_s)))

    def _model_for(self, profile_name: str) -> str:
        ref = self.assignments.profile(profile_name).model_ref
        if ref.startswith("catalog:"):
            if self._catalog is None:
                from engine_core.providers.catalog import Catalog
                self._catalog = Catalog()
            try:
                ref = self._catalog.resolve(ref).id
            except Exception as e:  # noqa: BLE001 - unresolvable ref → clean, pre-call error
                raise PipelineError(f"profile {profile_name!r}: cannot resolve model {ref!r} ({e}) "
                                    "— assign a concrete model id (openrouter_import / profiles set)")
        if self.config.free_only and not is_free_model(ref):
            raise PipelineError(f"profile {profile_name!r} routes to {ref!r}, which is not a free "
                                "model, and routing is free-only — refusing before any call "
                                "(assign a ':free' model, or set routing.free_only: false explicitly)")
        return ref

    def _preflight(self, profile_name: str) -> str:
        """Key first (the actionable error), then model resolution + free-only check."""
        profile = self.assignments.profile(profile_name)
        if not self.assignments.resolve_key(profile):
            raise PipelineError(f"profile {profile_name!r} has no API key — import keys "
                                "first (engine-ai runtime keys, or openrouter_import)")
        return self._model_for(profile_name)

    def _provider_for(self, profile_name: str):
        profile = self.assignments.profile(profile_name)
        key = self.assignments.resolve_key(profile)
        if not key:
            raise PipelineError(f"profile {profile_name!r} has no API key — import keys "
                                "first (engine-ai runtime keys, or openrouter_import)")
        return profile, self.provider_factory(profile, key)

    # ---------------------------------------------------------------- memory
    def memory(self):
        if self._memory is None:
            from engine_core._compat import mcp_module
            self._memory = mcp_module("memory").Memory(os.path.join(self.home, "memory.db"))
        return self._memory

    def _recall(self, project: str, *texts: str) -> str:
        try:
            rec = self.memory().recall(_keywords(*texts), k=5, project=project)
        except Exception:  # noqa: BLE001 - memory is context, never a build blocker
            return ""
        lines = [f"- [{p['kind']} #{p['id']}] {p['preview']}" for p in rec.get("pockets", [])]
        return (f"Project memory for {project!r} (this project only):\n" + "\n".join(lines)) if lines else ""

    def _remember(self, project: str, spec: TaskSpec, row: dict) -> None:
        verified = bool(row.get("verification")) and all(c["passed"] for c in row["verification"])
        text = (f"[{spec.id}] ({spec.profile}) {str(row.get('summary') or '')[:600]} "
                f"artifacts: {', '.join(row.get('artifacts') or []) or 'none'}; "
                f"verification: {'passed' if verified else 'not run'}")
        try:
            self.memory().save([project, spec.id] + _keywords(spec.title, spec.objective, limit=6),
                               text, {"run_id": row.get("run_id"), "task": spec.id,
                                      "artifacts": row.get("artifact_hashes", {})},
                               project=project, kind="fact" if verified else "idea",
                               source=f"engine-build:{row.get('run_id')}")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- progress
    def _progress(self, event: str, **data) -> None:
        rec = {"ts": round(time.time(), 3), "event": event, **data}
        if self._progress_path:
            with open(self._progress_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        if self._status_path:
            st = self._status
            st["updated_at"] = rec["ts"]
            st["last_event"] = rec
            if "task" in data and event in ("task-start", "task-end", "task-skip"):
                st.setdefault("tasks", {})[data["task"]] = data.get("status", "running")
                st["current_task"] = data["task"] if event == "task-start" else None
            tmp = self._status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, default=str)
            os.replace(tmp, self._status_path)
        if self._progress_cb:
            self._progress_cb(rec)

    # ---------------------------------------------------------------- plan
    def plan(self, objective: str, max_tasks: int = 6, memory_ctx: str = "") -> dict:
        """Planner agent (its own key/model) ideates → validated, normalised TaskGraph."""
        model = self._preflight("planner")
        profile, provider = self._provider_for("planner")
        prompt = objective + (f"\n\n{memory_ctx}" if memory_ctx else "")
        completion = provider.complete(
            model, [Message("system", PLANNER_SYSTEM), Message("user", prompt)],
            max_output_tokens=profile.max_output_tokens)
        u = self.usage["planner"]
        u["model_calls"] += 1
        u["input_tokens"] += completion.usage.input_tokens
        u["output_tokens"] += completion.usage.output_tokens
        specs = parse_plan(completion.content)
        graph, warnings = self._graph_from(specs[:max_tasks])
        return {"graph": graph, "plan_model": completion.model, "warnings": warnings,
                "tasks": graph.topological_order()}

    def _graph_from(self, specs: list) -> tuple[TaskGraph, list[str]]:
        graph, warnings, owners = TaskGraph(), [], {}
        for t in specs:
            if not isinstance(t, dict) or not t.get("id"):
                raise PipelineError(f"planner task missing id: {json.dumps(t)[:200]}")
            t = dict(t)
            t.setdefault("profile", "backend")
            if t["profile"] not in self.assignments.profiles:
                warnings.append(f"{t['id']}: unknown profile {t['profile']!r} → backend")
                t["profile"] = "backend"
            for key in ("owned_paths", "artifacts", "verification", "acceptance", "dependencies"):
                v = t.get(key) or []
                t[key] = [str(x) for x in (v if isinstance(v, list) else [v])]
            for p in t["owned_paths"] + t["artifacts"]:
                norm = os.path.normpath(p)
                if os.path.isabs(p) or norm == ".." or norm.startswith(".." + os.sep):
                    raise PipelineError(f"plan invalid: task {t['id']!r} path {p!r} leaves the workspace")
            if self.assignments.profile(t["profile"]).read_only:
                if t["owned_paths"] or t["artifacts"]:
                    warnings.append(f"{t['id']}: read-only profile {t['profile']!r} cannot own "
                                    "files — dropped its owned_paths/artifacts")
                t["owned_paths"], t["artifacts"] = [], []
            elif t["artifacts"] and not t["owned_paths"]:
                t["owned_paths"] = list(t["artifacts"])
            for a in t["artifacts"]:
                if not any(os.path.normpath(a) == os.path.normpath(o) or
                           os.path.normpath(a).startswith(os.path.normpath(o) + os.sep)
                           for o in t["owned_paths"]):
                    t["owned_paths"].append(a)
                    warnings.append(f"{t['id']}: artifact {a!r} added to its owned paths")
            for o in t["owned_paths"]:
                n = os.path.normpath(o)
                for other_path, other in owners.items():
                    if n == other_path or n.startswith(other_path + os.sep) or \
                            other_path.startswith(n + os.sep):
                        raise PipelineError(f"plan invalid: tasks {other!r} and {t['id']!r} both own "
                                            f"{o!r} — owned paths must not overlap")
                owners[n] = t["id"]
            try:
                graph.add(TaskSpec.from_dict(t))
            except GraphError as e:
                raise PipelineError(f"plan invalid: {e}")
        try:
            graph.validate()  # cycles / unknown deps — fail BEFORE spending worker quota
        except GraphError as e:
            raise PipelineError(f"plan invalid: {e}")
        return graph, warnings

    # ---------------------------------------------------------------- packets
    def _packet(self, spec: TaskSpec, workspace: str, upstream: dict, memory_ctx: str,
                latest_failure: str, profile) -> str:
        spans, decisions = [], []
        for dep in spec.dependencies:
            row = upstream.get(dep) or {}
            decisions.append(f"upstream {dep} ({row.get('profile')}): {row.get('status')} — "
                             f"{str(row.get('summary') or '')[:500]}")
            for rel in row.get("artifacts") or []:
                full = os.path.join(workspace, rel)
                try:
                    with open(full, encoding="utf-8", errors="replace") as f:
                        text = f.read(30_000)
                except OSError:
                    continue
                spans.append(CodeSpan(rel, 1, text.count("\n") + 1, text,
                                      file_hash=_sha256(full) or "", origin=f"artifact:{dep}"))
        if memory_ctx:
            decisions.append(memory_ctx)
        instructions = READ_ONLY_RULES if profile.read_only else WORKER_RULES
        if spec.artifacts:
            instructions += "\nRequired artifacts: " + ", ".join(spec.artifacts)
        if spec.verification:
            instructions += "\nVerification commands (re-run by the engine): " + "; ".join(spec.verification)
        packet = ContextPacket.build(spec, spans, decisions=decisions, latest_failure=latest_failure,
                                     budget_tokens=max(4000, int(profile.input_target_tokens)),
                                     instructions=instructions)
        return packet.render()

    # ---------------------------------------------------------------- checks
    def _check(self, spec: TaskSpec, workspace: str) -> tuple[str | None, list, dict]:
        """→ (failure or None, verification checks, artifact hashes)."""
        required = list(spec.artifacts) or [p for p in spec.owned_paths
                                            if FILE_HINT.search(os.path.basename(p))]
        hashes = {a: _sha256(os.path.join(workspace, a)) for a in required}
        missing = [a for a, h in hashes.items() if h is None]
        if missing:
            return f"required artifacts missing on disk: {', '.join(missing)}", [], hashes
        checks = []
        if spec.verification:
            cmds = []
            for c in spec.verification:
                try:
                    cmds.append(shlex.split(c))
                except ValueError as e:
                    return f"verification command {c!r} is not valid shell: {e}", [], hashes
            run = self.verifier.run_checks(workspace, [c for c in cmds if c])
            checks = [{"command": " ".join(map(shlex.quote, c.get("command", []))),
                       "exit_code": c.get("exit_code"), "passed": c.get("exit_code") == 0,
                       "duration_s": c.get("duration_s"), "output_tail": (c.get("output_tail") or "")[-800:]}
                      for c in run["checks"]]
            if not run["passed"]:
                failed = next((c for c in checks if not c["passed"]), {})
                return (f"verification failed: {failed.get('command')} → exit {failed.get('exit_code')}\n"
                        f"{failed.get('output_tail', '')}", checks, hashes)
        return None, checks, hashes

    @staticmethod
    def _digest(spec: TaskSpec, hashes: dict, dep_digests: dict) -> str:
        return hashlib.sha256(json.dumps([spec.to_dict(), hashes, dep_digests],
                                         sort_keys=True).encode()).hexdigest()[:24]

    # ---------------------------------------------------------------- execute
    def execute(self, graph: TaskGraph, workspace: str, *, run_id: str | None = None,
                project: str | None = None, objective: str = "", store: StateStore | None = None,
                memory_ctx: str | None = None) -> dict:
        own = store is None
        store = store or StateStore(self.state_path(os.path.realpath(workspace)))
        try:
            return self._execute(graph, workspace, run_id=run_id, project=project,
                                 objective=objective, store=store, memory_ctx=memory_ctx)
        finally:
            if own:
                store.close()

    def _execute(self, graph: TaskGraph, workspace: str, *, run_id, project, objective, store,
                 memory_ctx) -> dict:
        workspace = os.path.realpath(workspace)
        os.makedirs(workspace, exist_ok=True)
        run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        project = project or _slug(os.path.basename(workspace)) or "default"
        store = store or StateStore(self.state_path(workspace))
        started = time.time()
        order = graph.topological_order()
        memory_ctx = self._recall(project, objective) if memory_ctx is None else memory_ctx
        rows: dict[str, dict] = {}
        resumable: dict[str, str] = {}  # tid → digest, for tasks valid to resume

        for tid in order:  # deps first: resumability propagates downstream
            spec = graph.tasks[tid]
            prev = store.get(tid)
            store.upsert(spec)
            res = (prev or {}).get("result") or {}
            ok = (prev is not None and prev["state"] == "complete"
                  and prev["spec"].to_dict() == spec.to_dict()
                  and all(d in resumable for d in spec.dependencies)
                  and res.get("dep_digests") == {d: resumable[d] for d in spec.dependencies if d in resumable}
                  and all(_sha256(os.path.join(workspace, a)) == h
                          for a, h in (res.get("artifact_hashes") or {}).items()))
            # a completed task whose ONLY change is its own artifacts (a hand edit, e.g. a
            # bug fixed between runs) is RE-VERIFIED instead of re-run: if its checks still
            # pass the edit is kept (new hashes → new digest, so dependents rebuild on it);
            # if they fail, the task re-runs as before.
            edited = (not ok and prev is not None and prev["state"] == "complete"
                      and prev["spec"].to_dict() == spec.to_dict()
                      and all(d in resumable for d in spec.dependencies)
                      and res.get("dep_digests") == {d: resumable[d] for d in spec.dependencies})
            reverified = None
            if edited:
                failure, checks, hashes = self._check(spec, workspace)
                if failure is None:
                    deps = {d: resumable[d] for d in spec.dependencies}
                    digest = self._digest(spec, hashes, deps)
                    row = {**res.get("row", {}), "artifact_hashes": hashes, "dep_digests": deps,
                           "digest": digest, "verification": checks or res.get("row", {}).get("verification")}
                    store._set(tid, "complete", result_json=json.dumps({
                        "row": row, "artifact_hashes": hashes, "dep_digests": deps, "digest": digest},
                        default=str))
                    reverified = digest
                    res = {"digest": digest, "row": row}
            if ok or reverified:
                resumable[tid] = res.get("digest", "")
                rows[tid] = {**res.get("row", {}), "status": "done", "resumed": True,
                             "attempts": 0, **({"reverified": True} if reverified else {})}
                self._progress("task-skip", task=tid, status="done",
                               reason="artifacts edited since last run — re-verified, edit kept"
                               if reverified else "resumed")
            elif prev is not None:  # failed / blocked / interrupted / invalidated → retry
                store._set(tid, "pending", lease_owner=None, lease_expires=None)

        stopped: str | None = None
        results, evidence = [], []
        reopens: dict[str, int] = {}
        for tid in order:
            spec = graph.tasks[tid]
            if tid in rows and rows[tid].get("resumed"):
                results.append({"task": tid, "status": "done", "resumed": True})
                continue
            base_row = {"id": tid, "title": spec.title, "profile": spec.profile,
                        "dependencies": list(spec.dependencies), "owned_paths": list(spec.owned_paths),
                        "artifacts": list(spec.artifacts), "run_id": run_id}
            bad_dep = next((d for d in spec.dependencies if rows.get(d, {}).get("status") != "done"), None)
            if stopped or bad_dep or time.time() - started > self.config.run_timeout_s:
                reason = (stopped or (f"dependency {bad_dep!r} is {rows.get(bad_dep, {}).get('status', 'not-run')}"
                                      if bad_dep else f"run timeout ({self.config.run_timeout_s:g}s)"))
                rows[tid] = {**base_row, "status": "not-run", "error": reason, "attempts": 0}
                self._progress("task-skip", task=tid, status="not-run", reason=reason)
                continue
            rows[tid] = self._run_task(spec, workspace, store, run_id, project, rows,
                                       memory_ctx, base_row, evidence)
            if rows[tid]["status"] == UPSTREAM:
                self._reopen_upstream(graph, order, tid, workspace, store, run_id, project, rows,
                                      resumable, memory_ctx, evidence, results, reopens)
            results.append({"task": tid, **{k: rows[tid].get(k) for k in
                                            ("status", "summary", "error", "attempts")}})
            if rows[tid]["status"] == "done":
                self._mark_complete(spec, rows, resumable, store, project)
            elif rows[tid]["status"] in STOP_STATUSES:
                stopped = (f"run stopped after {tid!r}: {rows[tid]['status']} "
                           f"({str(rows[tid].get('error'))[:160]}) — no other profile is tried")

        tasks = [rows[t] for t in order]
        complete = sum(1 for r in tasks if r["status"] == "done")
        blocked = stopped is not None or any(r["status"] in STOP_STATUSES for r in tasks)
        status = "complete" if complete == len(tasks) else ("blocked" if blocked else "failed")
        w = self.usage["workers"]
        report = {
            "run_id": run_id, "project": project, "workspace": workspace, "objective": objective,
            "status": status, "exit_code": {"complete": 0, "failed": 1, "blocked": 2}[status],
            "stopped": stopped, "total": len(tasks), "complete": complete,
            "not_run": [r["id"] for r in tasks if r["status"] == "not-run"],
            "tasks": tasks, "results": results, "evidence": evidence,
            "usage": {"planner": dict(self.usage["planner"]), "workers": dict(w),
                      "total": {k: self.usage["planner"].get(k, 0) + w.get(k, 0)
                                for k in ("model_calls", "input_tokens", "output_tokens")},
                      "scope": "engine provider calls only (planner + workers); the host "
                               "assistant's own usage is separate and not measured here"},
            "state_db": getattr(store, "path", None), "duration_s": round(time.time() - started, 2),
        }
        return report

    def _run_task(self, spec: TaskSpec, workspace: str, store: StateStore, run_id: str,
                  project: str, rows: dict, memory_ctx: str, base_row: dict, evidence: list,
                  feedback: str = "") -> dict:
        tid = spec.id
        try:
            model = self._model_for(spec.profile)
            profile, provider = self._provider_for(spec.profile)
        except PipelineError as e:
            return {**base_row, "status": "blocked", "error": str(e), "attempts": 0}
        row = {**base_row, "model": model, "read_only": profile.read_only,
               "usage": {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}}
        latest_failure = feedback
        task_memory = self._recall(project, spec.title, spec.objective) or memory_ctx
        for attempt in range(1, self.config.max_attempts + 1):
            self._progress("task-start", task=tid, profile=spec.profile, model=model, attempt=attempt)
            packet = self._packet(spec, workspace, rows, task_memory, latest_failure, profile)
            assignment = store.claim(tid, owner=f"{run_id}:{spec.profile}")
            assignment["attempt"] = attempt
            policy = Policy(read_only=profile.read_only, permitted_paths=spec.owned_paths or None,
                            root=workspace, hidden_paths=[self.home],
                            shell_isolation=self.config.shell_isolation)
            executor = Executor(self.registry, policy, journal_path=os.path.join(self.home, "effects.db"))
            worker = Worker(provider, model, executor, store, self.config, profile,
                            finalize=False, run_id=run_id,
                            progress=lambda ev, **d: self._progress(ev, **d))
            try:
                outcome = worker.run(assignment, packet)
            finally:
                executor.close()  # one effect-journal connection per attempt — release it
            for k, v in (outcome.get("usage") or {}).items():
                if k in row["usage"]:
                    row["usage"][k] += v
                if k in self.usage["workers"]:
                    self.usage["workers"][k] += v
            status = str(outcome.get("status"))
            failure, checks, hashes = None, [], {}
            if status == "done":
                failure, checks, hashes = self._check(spec, workspace)
                if checks:
                    evidence.append({"task": tid, "passed": failure is None, "checks": checks,
                                     "manifest": EvidenceManifest.from_run(
                                         tid, {"checks": checks, "passed": failure is None}).manifest_hash()})
                if failure:
                    status = "missing-artifacts" if failure.startswith("required artifacts") \
                        else "verification-failed"
            elif status == UPSTREAM:
                failure = (f"defect reported in upstream file {outcome.get('file')!r}: "
                           f"{str(outcome.get('summary') or '')[:1500]}")
                row["upstream_file"] = str(outcome.get("file") or "")
            else:
                failure = str(outcome.get("error") or outcome.get("summary") or status)
            row.update({"status": status, "summary": str(outcome.get("summary") or "")[:2000],
                        "error": failure, "attempts": attempt, "verification": checks,
                        "artifact_hashes": hashes,
                        "artifacts": list(hashes) or list(spec.artifacts)})
            self._progress("task-end", task=tid, status=status, attempt=attempt,
                           error=(failure or "")[:300])
            if status == "done":
                return row
            store._set(tid, "failed" if status not in STOP_STATUSES else "blocked",
                       error=(failure or status)[:2000], lease_owner=None, lease_expires=None)
            if status in STOP_STATUSES or status in ("stale-lease", UPSTREAM):
                return row  # never retry into an exhausted quota / revoked key, nor around
                # someone else's bug — execute() reopens the owning task instead
            if attempt < self.config.max_attempts:
                latest_failure = f"Attempt {attempt} ended with {status}: {failure}"
                if status == "verification-failed":
                    latest_failure += ("\nIf this failure comes from a file you do not own, report "
                                       "upstream-defect (see rules) instead of retrying.")
                store._set(tid, "pending", lease_owner=None, lease_expires=None)
        return row

    # ---------------------------------------------------------------- completion / reopen
    def _mark_complete(self, spec: TaskSpec, rows: dict, resumable: dict, store: StateStore,
                       project: str) -> None:
        tid = spec.id
        deps = {d: resumable.get(d) or rows[d].get("digest") for d in spec.dependencies}
        rows[tid]["dep_digests"] = deps
        rows[tid]["digest"] = self._digest(spec, rows[tid].get("artifact_hashes", {}), deps)
        store._set(tid, "complete", result_json=json.dumps({
            "row": {k: v for k, v in rows[tid].items() if k not in ("resumed",)},
            "artifact_hashes": rows[tid].get("artifact_hashes", {}),
            "dep_digests": deps, "digest": rows[tid]["digest"]}, default=str))
        self._remember(project, spec, rows[tid])

    @staticmethod
    def _owner_of(path: str, graph: TaskGraph, exclude: str) -> str | None:
        """The task whose owned_paths/artifacts cover `path` (a file or under a dir)."""
        rel = os.path.normpath((path or "").strip()) if path else ""
        if not rel or rel == "." or rel.startswith("..") or os.path.isabs(rel):
            return None
        for t in graph.tasks.values():
            if t.id == exclude:
                continue
            for p in list(t.owned_paths) + list(t.artifacts):
                p = os.path.normpath(p.strip().rstrip("/"))
                if rel == p or rel.startswith(p + os.sep):
                    return t.id
        return None

    @staticmethod
    def _ancestors(graph: TaskGraph, tid: str) -> set:
        seen, stack = set(), list(graph.tasks[tid].dependencies)
        while stack:
            d = stack.pop()
            if d not in seen and d in graph.tasks:
                seen.add(d)
                stack.extend(graph.tasks[d].dependencies)
        return seen

    def _reopen_upstream(self, graph, order, tid, workspace, store, run_id, project, rows,
                         resumable, memory_ctx, evidence, results, reopens) -> None:
        """`tid` reported a defect in a file it doesn't own: reopen the owning task (once)
        with that evidence, re-verify what was already built on it, then retry `tid`."""
        row = rows[tid]
        file = row.get("upstream_file", "")
        owner = self._owner_of(file, graph, exclude=tid)
        if owner is None or owner not in self._ancestors(graph, tid):
            row["error"] = f"{row.get('error')} — no upstream task of {tid!r} owns {file!r}; fix it by hand, then resume"
            return
        if reopens.get(owner, 0) >= MAX_REOPENS or rows.get(owner, {}).get("status") != "done":
            row["error"] = (f"{row.get('error')} — {owner!r} owns {file!r} but was already reopened "
                            "this run; fix it by hand (the edit is kept on resume), then resume")
            return
        reopens[owner] = reopens.get(owner, 0) + 1
        ospec = graph.tasks[owner]
        self._progress("task-reopen", task=owner, by=tid, file=file)
        base = {"id": owner, "title": ospec.title, "profile": ospec.profile,
                "dependencies": list(ospec.dependencies), "owned_paths": list(ospec.owned_paths),
                "artifacts": list(ospec.artifacts), "run_id": run_id, "reopened_by": tid}
        store._set(owner, "pending", lease_owner=None, lease_expires=None)  # complete → claimable
        feedback = (f"Task {tid!r} ({graph.tasks[tid].profile}) found a defect in {file}, which "
                    f"YOU own:\n{row.get('error')}\nFix that defect; keep everything else working.")
        orow = self._run_task(ospec, workspace, store, run_id, project, rows, memory_ctx, base,
                              evidence, feedback=feedback)
        rows[owner] = orow
        results.append({"task": owner, "reopened_by": tid, **{k: orow.get(k) for k in
                                                              ("status", "summary", "error", "attempts")}})
        if orow["status"] != "done":
            row["error"] = f"{row.get('error')} — reopened {owner!r} but it ended {orow['status']}"
            return
        resumable.pop(owner, None)
        self._mark_complete(ospec, rows, resumable, store, project)
        # tasks already built on the old artifact: keep them only if their checks still pass
        for t in order[:order.index(tid)]:
            if t == owner or owner not in self._ancestors(graph, t) or rows.get(t, {}).get("status") != "done":
                continue
            failure, checks, hashes = self._check(graph.tasks[t], workspace)
            if failure:
                rows[t] = {**rows[t], "status": "verification-failed", "resumed": False,
                           "error": f"after reopening {owner!r}: {failure}"}
                store._set(t, "failed", error=rows[t]["error"][:2000], lease_owner=None, lease_expires=None)
            else:
                resumable.pop(t, None)
                rows[t] = {**rows[t], "artifact_hashes": hashes}
                self._mark_complete(graph.tasks[t], rows, resumable, store, project)
        spec = graph.tasks[tid]
        base_row = {k: row[k] for k in ("id", "title", "profile", "dependencies", "owned_paths",
                                        "artifacts", "run_id") if k in row}
        bad = next((d for d in spec.dependencies if rows.get(d, {}).get("status") != "done"), None)
        if bad:
            row["error"] = f"{row.get('error')} — dependency {bad!r} is {rows[bad]['status']} after the reopen"
            return
        store._set(tid, "pending", lease_owner=None, lease_expires=None)  # failed → claimable
        rows[tid] = self._run_task(spec, workspace, store, run_id, project, rows, memory_ctx, base_row,
                                   evidence, feedback=f"Upstream task {owner!r} was reopened to fix {file}. "
                                                      "Re-run your checks against the fixed version.")
        rows[tid]["after_reopen"] = owner

    # ---------------------------------------------------------------- run / resume
    def _open_run(self, workspace: str, run_id: str) -> str:
        d = self.run_dir(workspace)
        os.makedirs(d, exist_ok=True)
        self._progress_path = os.path.join(d, "progress.jsonl")
        self._status_path = os.path.join(d, "status.json")
        self._status = {"run_id": run_id, "workspace": os.path.realpath(workspace), "pid": os.getpid(),
                        "state": "running", "started_at": round(time.time(), 3), "tasks": {}}
        return d

    def _finish(self, d: str, report: dict) -> dict:
        report["plan_path"] = os.path.join(d, "plan.json")
        report["report_path"] = os.path.join(d, "report.json")
        report["progress_path"] = self._progress_path
        with open(report["report_path"] + ".tmp", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        os.replace(report["report_path"] + ".tmp", report["report_path"])
        self._status["state"] = report["status"]
        self._status["exit_code"] = report["exit_code"]
        self._progress("run-end", status=report["status"], complete=report["complete"],
                       total=report["total"])
        self.close()
        return report

    def close(self) -> None:
        """Release the memory DB (reopened lazily if this pipeline is used again)."""
        if not self._owns_memory:
            return
        mem, self._memory = self._memory, None
        if mem is not None and hasattr(mem, "close"):
            mem.close()

    def run(self, objective: str, workspace: str | None = None, max_tasks: int = 6,
            project: str | None = None, run_id: str | None = None) -> dict:
        if not objective or not objective.strip():
            raise PipelineError("an objective is required — what should the agents build?")
        workspace = os.path.realpath(workspace or os.path.join(
            self.home, "builds", _slug(objective) or f"build-{int(time.time())}"))
        run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        project = project or _slug(os.path.basename(workspace)) or "default"
        self._preflight("planner")  # key + free-only + resolvability: BEFORE any call or lock
        with _Lock(os.path.join(self.run_dir(workspace), "lock"),
                   {"run_id": run_id, "pid": os.getpid(), "since": time.time()}):
            d = self._open_run(workspace, run_id)
            memory_ctx = self._recall(project, objective)
            self._progress("plan-start", run_id=run_id, project=project)
            try:
                plan = self.plan(objective, max_tasks=max_tasks, memory_ctx=memory_ctx)
            except (PipelineError, ProviderError) as e:
                # a planning failure is a REPORTED outcome, not a crash: clients see why
                blocked = isinstance(e, ProviderError) and e.kind in (
                    ProviderError.QUOTA, ProviderError.AUTH, ProviderError.PAID_REFUSED, ProviderError.RATE_LIMIT)
                report = {"run_id": run_id, "project": project, "workspace": workspace,
                          "objective": objective, "status": "blocked" if blocked else "failed",
                          "exit_code": 2 if blocked else 1, "phase": "plan",
                          "error": f"planner: {getattr(e, 'message', None) or e}",
                          "stopped": None, "total": 0, "complete": 0, "not_run": [], "tasks": [],
                          "results": [], "evidence": [],
                          "usage": {"planner": dict(self.usage["planner"]),
                                    "workers": dict(self.usage["workers"]),
                                    "total": dict(self.usage["planner"]),
                                    "scope": "engine provider calls only"}}
                return self._finish(d, report)
            graph = plan["graph"]
            for t in graph.tasks.values():  # every model the run will call, before the first worker
                self._preflight(t.profile)
            with open(os.path.join(d, "plan.json"), "w", encoding="utf-8") as f:
                json.dump({"objective": objective, "project": project, "workspace": workspace,
                           "planned_by_run": run_id, "plan_model": plan["plan_model"],
                           "warnings": plan["warnings"],
                           "tasks": [graph.tasks[t].to_dict() for t in plan["tasks"]]}, f, indent=2)
            self._progress("plan-end", tasks=plan["tasks"], warnings=plan["warnings"])
            report = self.execute(graph, workspace, run_id=run_id, project=project,
                                  objective=objective, memory_ctx=memory_ctx)
            report.update({"plan": plan["tasks"], "plan_model": plan["plan_model"],
                           "plan_warnings": plan["warnings"], "resumed_from_plan": False})
            return self._finish(d, report)

    def resume(self, workspace: str, run_id: str | None = None) -> dict:
        """Re-execute the STORED plan for this workspace (no re-planning): valid completed
        tasks resume, failed/blocked/not-run/invalidated ones run again."""
        workspace = os.path.realpath(workspace)
        d = self.run_dir(workspace)
        try:
            with open(os.path.join(d, "plan.json"), encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, json.JSONDecodeError):
            raise PipelineError(f"no stored plan for {workspace} — start a build first")
        graph, _ = self._graph_from(stored["tasks"])
        for t in graph.tasks.values():
            self._preflight(t.profile)
        run_id = run_id or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        with _Lock(os.path.join(d, "lock"), {"run_id": run_id, "pid": os.getpid(), "since": time.time()}):
            self._open_run(workspace, run_id)
            self._progress("resume-start", run_id=run_id, planned_by_run=stored.get("planned_by_run"))
            report = self.execute(graph, workspace, run_id=run_id, project=stored.get("project"),
                                  objective=stored.get("objective", ""))
            report.update({"plan": graph.topological_order(), "plan_model": stored.get("plan_model"),
                           "plan_warnings": stored.get("warnings", []), "resumed_from_plan": True,
                           "planned_by_run": stored.get("planned_by_run")})
            return self._finish(d, report)
