"""engine_core runtime CLI (upgrade.md §7 — the proposed future CLI, first slice).

  python3 -m engine_core status                     task states + quota + usage totals
  python3 -m engine_core plan --graph tasks.json    validate a task graph (cycles, unknown deps)
  python3 -m engine_core run --config engine.yaml --graph tasks.json [--free-only]
                                                       lease + execute ready tasks via a provider
  python3 -m engine_core verify --path DIR -- cmd...   run verification commands → evidence
  python3 -m engine_core build "<objective>"          multi-agent build (plan → workers → verify)

`connect`/`doctor`/`knowledge` stay in the existing engine-ai CLI (preserved).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine_core.config import Config, ConfigError, load_config  # noqa: E402
from engine_core.providers.base import ProviderError  # noqa: E402
from engine_core.providers.catalog import CatalogError  # noqa: E402
from engine_core.providers.limiter import Limits, RateLimiter  # noqa: E402
from engine_core.providers.openrouter import OpenRouterProvider  # noqa: E402
from engine_core.runtime.scheduler import QuotaPaused, Scheduler  # noqa: E402
from engine_core.runtime.state import StateError, StateStore  # noqa: E402
from engine_core.runtime.task_graph import GraphError, TaskGraph, TaskSpec  # noqa: E402
from engine_core.runtime.worker import Worker  # noqa: E402
from engine_core.telemetry.usage import UsageJournal  # noqa: E402
from engine_core.tools.executor import Executor  # noqa: E402
from engine_core.tools.policy import Policy  # noqa: E402
from engine_core.tools.registry import default_registry  # noqa: E402
from engine_core.verification.runner import Verifier  # noqa: E402

HOME = os.environ.get("ENGINE_AI_HOME") or os.path.expanduser("~/.engine-ai")


def _store() -> StateStore:
    os.makedirs(HOME, exist_ok=True)
    return StateStore(os.path.join(HOME, "runtime.db"))


def _load_graph(path: str) -> TaskGraph:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise GraphError(f"cannot read graph {path!r}: {e}") from e
    except json.JSONDecodeError as e:
        raise GraphError(f"graph {path!r} is not valid JSON: {e}") from e
    graph = TaskGraph()
    for t in data.get("tasks", data if isinstance(data, list) else []):
        graph.add(TaskSpec.from_dict(t))
    graph.validate()
    return graph


def cmd_status(args) -> int:
    store = _store()
    try:
        return _status(args, store)
    finally:
        store.close()


def _status(args, store) -> int:
    sched = Scheduler(store, load_runtime_config(args))
    st = sched.status()
    print(json.dumps(st, indent=2))
    if not st.get("tasks"):
        print("note: this is the standalone `run` queue. Multi-agent builds keep their task state per "
              "workspace — see:  engine-ai runtime build-status --workspace DIR  (or --run-id ID)",
              file=sys.stderr)
    usage_path = os.path.join(HOME, "usage.db")
    if os.path.exists(usage_path):
        print(json.dumps(UsageJournal(usage_path).totals(), indent=2))
    return 0


def load_runtime_config(args) -> Config:
    if getattr(args, "config", None) and os.path.exists(args.config):
        return load_config(args.config)
    return Config()


def cmd_plan(args) -> int:
    graph = _load_graph(args.graph)
    print("graph OK — topological order:")
    for tid in graph.topological_order():
        t = graph.tasks[tid]
        deps = ", ".join(t.dependencies) or "-"
        print(f"  {tid:24} profile={t.profile:12} deps: {deps}")
    return 0


def cmd_run(args) -> int:
    from engine_core.profiles import Assignments
    config = load_config(args.config) if args.config else Config()
    graph = _load_graph(args.graph)  # validated BEFORE any key/provider work
    store = _store()
    try:
        return _run(args, config, graph, store)
    finally:
        store.close()


def _run(args, config, graph, store) -> int:
    from engine_core.profiles import Assignments
    for spec in graph.tasks.values():
        store.upsert(spec)
    store.mark_ready(store.ready_ids())
    # no config file → the ten built-in profiles (with their env credential refs);
    # a config file's profiles replace/extend them
    assignments = Assignments(HOME, config if args.config else None)
    # --agent maps to that agent's profile; --profile names a profile directly
    if args.agent:
        profile = assignments.profile_for_agent(args.agent)
    else:
        profile = assignments.profile(args.profile)
    # limits are account facts, not code constants: defaults reflect OpenRouter's
    # published free tier at review time; override per account via env vars.
    limiter = RateLimiter()
    limiter.configure(profile.account_group, Limits(
        requests_per_minute=int(os.environ.get("ENGINE_AI_RPM", 20)),
        requests_per_day=int(os.environ.get("ENGINE_AI_RPD", 50))))
    scripted = os.environ.get("ENGINE_AI_PROVIDER", "")
    api_key = "scripted-no-key" if scripted.startswith("scripted:") else assignments.resolve_key(profile)
    if not api_key:
        env_name = "OPENROUTER_" + profile.name.upper().replace("-", "_") + "_KEY"
        print(f"✗ profile {profile.name!r} has no API key configured "
              f"(credential_ref {profile.credential_ref!r}).\n"
              f"  export {env_name}=…  — or store it encrypted and reference it:\n"
              f"    engine-ai runtime profiles set {profile.name} "
              f"--credential-ref vault:{env_name}\n"
              f"    then store the key itself with the set_secret MCP tool "
              f"(name {env_name})", file=sys.stderr)
        return 1
    if scripted.startswith("scripted:"):  # offline dry run: no key, no network, no catalog
        from engine_core.providers.scripted import ScriptedProvider
        provider, model_id = ScriptedProvider.load(scripted.split(":", 1)[1]), "scripted/offline:free"
    else:
        catalog = _catalog()
        model_info = catalog.resolve(args.model or profile.model_ref)
        if args.free_only and not model_info.free:
            print(f"✗ {model_info.id} is not free and routing is free-only", file=sys.stderr)
            return 1
        provider = OpenRouterProvider(api_key=api_key,
                                      limiter=limiter, account_group=profile.account_group)
        model_id = model_info.id
    owned = [p for t in graph.tasks.values() for p in t.owned_paths]
    executor = Executor(default_registry(),
                        Policy(read_only=profile.read_only,
                               permitted_paths=owned or None),
                        journal_path=os.path.join(HOME, "effects.db"))
    usage = UsageJournal(os.path.join(HOME, "usage.db"))
    worker = Worker(provider, model_id, executor, store, config, profile, usage)
    sched = Scheduler(store, config, limiter)

    ran = 0
    try:
        while True:
            assignments = sched.next_assignments("cli", profile.account_group)
            if not assignments:
                break
            for a in assignments:
                spec = graph.tasks[a["task_id"]]
                packet = (f"# Task packet {spec.id}\nTask {spec.id}: {spec.title or spec.objective}\n"
                          f"Acceptance: {spec.acceptance}\nVerification: {spec.verification}")
                outcome = worker.run(a, packet)
                print(json.dumps(outcome, indent=2)[:2000])
                ran += 1
                if outcome.get("status", "").startswith("provider-"):
                    print(f"✋ stopping: {outcome['status']} (checkpoint saved, resume later)",
                          file=sys.stderr)
                    return 2
    except QuotaPaused as e:
        print(f"⏸ quota paused: {e} (state checkpointed — re-run later)", file=sys.stderr)
        return 2
    finally:
        usage.close()
        executor.close()
    print(f"ran {ran} task(s); status:")
    print(json.dumps(sched.status(), indent=2))
    return 0


def _catalog():
    from engine_core.providers.catalog import Catalog
    return Catalog()


def cmd_profiles(args) -> int:
    """Per-agent API-key/model assignment: list / show / set / clear / agents."""
    from engine_core.profiles import Assignments
    config = load_config(args.config) if getattr(args, "config", None) else None
    a = Assignments(HOME, config)

    if args.profiles_cmd == "list":
        rows = a.status(catalog=_catalog())
        print(f"{'profile':16} {'key':16} {'credential_ref':38} {'model':24} agents")
        for r in rows:
            model = r.get("model", r["model_ref"])
            agents = ",".join(r["agents"]) or "-"
            print(f"{r['profile']:16} {r['key']:16} {r['credential_ref']:38} {str(model)[:24]:24} {agents}")
        missing = [r["profile"] for r in rows if r["key"] == "MISSING"]
        if missing:
            print(f"\n{len(missing)} profile(s) without a key: {', '.join(missing)}")
            print("assign one:  engine-ai runtime profiles set <profile> "
                  "--credential-ref vault:OPENROUTER_<PROFILE>_KEY --model <id>")
        return 0
    if args.profiles_cmd == "show":
        from engine_core.profiles import mask
        p = a.profile(args.profile)
        print(json.dumps({"name": p.name, "provider": p.provider,
                          "credential_ref": p.credential_ref,
                          "key": mask(a.resolve_key(p)),
                          "model_ref": p.model_ref,
                          "account_group": p.account_group, "read_only": p.read_only,
                          "input_target_tokens": p.input_target_tokens,
                          "max_output_tokens": p.max_output_tokens,
                          "agents": sorted(ag for ag, pr in a.agents.items() if pr == p.name)},
                         indent=2))
        return 0
    if args.profiles_cmd == "set":
        p = a.set(args.profile, credential_ref=args.credential_ref,
                  model_ref=args.model, read_only=args.read_only,
                  account_group=args.account_group)
        print(f"profile {p.name!r}: credential_ref={p.credential_ref} model_ref={p.model_ref}"
              + (" read_only" if p.read_only else ""))
        print("(references only — the key itself stays in the env var or the vault; "
              "store it with set_secret, never in shared files)")
        return 0
    if args.profiles_cmd == "clear":
        print(f"cleared overrides for {args.profile!r}" if a.clear(args.profile)
              else f"no overrides for {args.profile!r}")
        return 0
    # agents: engine agents + domain experts -> profiles
    print("engine agents:")
    for agent, profile in sorted(a.agents.items()):
        print(f"  {agent:24} → {profile}")
    print("domain experts (via suggest_experts routing):")
    for agent, profile in sorted(a.domain_agent_profiles().items()):
        print(f"  {agent:24} → {profile}")
    return 0


def cmd_keys(args) -> int:
    """Detect and/or import a user's keys.csv into the 10 agent profiles."""
    from engine_core.profiles import Assignments, detect_keys_file
    a = Assignments(HOME)
    if args.path:
        found = {"path": os.path.abspath(args.path)}
    else:
        found = detect_keys_file(HOME)
        if not found:
            print("no keys.csv found. Drop one at ~/.engine-ai/keys.csv or ./keys.csv with")
            print("lines `key,model` (10 lines = 10 agent profiles) — or pass a path:")
            print("  engine-ai runtime keys /path/to/keys.csv")
            return 1
    print(f"keys file: {found['path']}")
    if found.get("in_git_repo"):
        print("  ⚠ inside a git repo — keep it git-ignored; NEVER commit API keys")
    out = a.import_keys(path=found["path"], set_secret=None, get_secret=None)
    if not out.get("ok"):
        print(f"✗ {out.get('error')}")
        for e in out.get("errors", [])[:5]:
            print(f"  {e}")
        return 1
    print(f"imported {out['imported']} key(s) — permanent, encrypted at rest:")
    for r in out["profiles"]:
        print(f"  {r['profile']:16} {r['key']:14} → {r['model']}")
    return 0


def _make_pipeline(args):
    """BuildPipeline for the CLI. ENGINE_AI_PROVIDER=scripted:<file.json> swaps every
    profile to the offline ScriptedProvider (no keys, no network, isolated assignments)."""
    from engine_core.pipeline import BuildPipeline
    from engine_core.profiles import Assignments
    config = load_config(args.config) if getattr(args, "config", None) else Config()
    mode = os.environ.get("ENGINE_AI_PROVIDER", "")

    def progress(rec):
        if getattr(args, "quiet", False):
            return
        ev = rec["event"]
        if ev in ("plan-start", "plan-end", "task-start", "task-end", "task-skip", "task-reopen", "nudge", "run-end",
                  "resume-start"):
            detail = {k: v for k, v in rec.items() if k not in ("ts", "event")}
            print(f"[{time.strftime('%H:%M:%S')}] {ev} {json.dumps(detail, default=str)[:220]}",
                  file=sys.stderr, flush=True)
    if mode.startswith("scripted:"):
        import tempfile
        from engine_core.providers.scripted import ScriptedProvider
        provider = ScriptedProvider.load(mode.split(":", 1)[1])
        overrides = os.path.join(tempfile.mkdtemp(prefix="engine-scripted-"), "profiles.json")
        assignments = Assignments(HOME, config, overrides_path=overrides)
        for name in assignments.profiles:
            assignments.set(name, credential_ref="scripted-no-key", model_ref="scripted/offline:free")
        return BuildPipeline(HOME, config, assignments, provider_factory=lambda p, k: provider,
                             progress=progress)
    return BuildPipeline(HOME, config, progress=progress)


def _print_report(out: dict) -> None:
    print(f"run {out['run_id']}  project={out['project']}  status={out['status'].upper()}  "
          f"({out['complete']}/{out['total']} tasks done)  exit={out['exit_code']}")
    print(f"workspace: {out['workspace']}")
    if out.get("stopped"):
        print(f"stopped:   {out['stopped']}")
    print(f"{'task':22} {'profile':15} {'status':22} {'att':>3}  deps → artifacts")
    for t in out["tasks"]:
        print(f"{t['id']:22} {t['profile']:15} {t['status'] + (' (re-verified)' if t.get('reverified') else ' (resumed)' if t.get('resumed') else ''):22} "
              f"{t.get('attempts', 0):>3}  {','.join(t['dependencies']) or '-'} → "
              f"{','.join(t.get('artifacts') or []) or '-'}")
        if t.get("error") and t["status"] != "done":
            print(f"{'':22} ↳ {str(t['error'])[:150]}")
    u = out["usage"]
    print(f"engine provider usage: planner {u['planner']['model_calls']} calls "
          f"({u['planner']['input_tokens']} in / {u['planner']['output_tokens']} out tokens); "
          f"workers {u['workers']['model_calls']} model calls, {u['workers']['tool_calls']} tool calls "
          f"({u['workers']['input_tokens']} in / {u['workers']['output_tokens']} out tokens)")
    print(f"report: {out.get('report_path')}")


def cmd_build(args) -> int:
    """objective → planner → per-profile workers → verified artifacts. Exit 0 only when
    EVERY planned task is done; 1 failed; 2 blocked (quota/auth/paid-refused — resume later)."""
    from engine_core.pipeline import PipelineError
    pipeline = _make_pipeline(args)
    try:
        out = pipeline.run(args.objective, workspace=args.workspace, max_tasks=args.max_tasks,
                           project=args.project, run_id=args.run_id)
    except PipelineError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except ProviderError as e:  # planner call itself hit quota/auth
        print(f"✗ provider error ({e.kind}): {e.message} — nothing ran; retry later", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, default=str)) if args.json else _print_report(out)
    return out["exit_code"]


def cmd_resume(args) -> int:
    """Re-run the STORED plan of a workspace: valid finished tasks resume, the rest run."""
    from engine_core.pipeline import PipelineError
    pipeline = _make_pipeline(args)
    try:
        out = pipeline.resume(args.workspace, run_id=args.run_id)
    except PipelineError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str)) if args.json else _print_report(out)
    return out["exit_code"]


def cmd_build_status(args) -> int:
    """Read-only view of a workspace's build: live status, last events, last report."""
    from engine_core.mcp_build import build_status
    out = build_status(HOME, workspace=args.workspace, run_id=args.run_id)
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


def cmd_models(args) -> int:
    catalog = _catalog()
    if args.models_cmd == "refresh":
        try:
            n = catalog.refresh()
        except Exception as e:  # noqa: BLE001 - network failure → clean CLI error
            print(f"✗ catalog refresh failed: {e}", file=sys.stderr)
            return 1
        print(f"refreshed {n} models (age resets; cached at {catalog.path})")
        return 0
    if args.models_cmd == "pin":
        try:
            catalog.pin(args.name, args.model_id)
        except CatalogError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        print(f"pinned {args.name!r} → {args.model_id}")
        return 0
    # list: eligible models given the routing constraints
    models = catalog.eligible(free_only=not args.include_paid,
                              supports_tools=not args.all_capabilities)
    if not models:
        print("(no models cached — run: engine-ai runtime models refresh)")
        return 0
    for m in models[: int(args.limit)]:
        tags = ("free" if m.free else f"${m.pricing_prompt:g}/M") + (", tools" if m.supports_tools else "")
        print(f"  {m.id:48} ctx={m.context_length or '?':>8}  {tags}")
    return 0


def cmd_codex_env(args) -> int:
    """Print `export OPENROUTER_<P>_KEY=…` lines from the encrypted vault, for
    `eval "$(engine-ai runtime codex-env)"` — the keys reach the shell without ever
    being written to disk. Nothing but the exports goes to stdout."""
    import shlex
    from engine_core.profiles import Assignments, vault_getter, vault_name
    getter = vault_getter()
    if getter is None:
        print("✗ encrypted vault unavailable", file=sys.stderr)
        return 1
    n = 0
    for name, prof in sorted(Assignments(HOME).profiles.items()):
        ref = prof.credential_ref or ""
        key = getter(ref[len("vault:"):]) if ref.startswith("vault:") else None
        if key:
            print(f"export {vault_name(name)}={shlex.quote(key)}")
            n += 1
    if not n:
        print("✗ no profile keys in the vault — import them first: engine-ai runtime keys",
              file=sys.stderr)
        return 1
    print(f"# {n} key(s) exported — now: codex --profile engine_<profile>", file=sys.stderr)
    return 0


def cmd_verify(args) -> int:
    import shlex
    verifier = Verifier()
    words = [w for w in args.command if w != "--"] if args.command[:1] == ["--"] else list(args.command)
    if not words:
        print("✗ verify needs a command:  verify --path DIR -- pytest -q", file=sys.stderr)
        return 2
    # one word = a shell string as typed ("npm test && npm run lint"); several words
    # (after `--`) are re-quoted, keeping a bare && as the check separator
    line = words[0] if len(words) == 1 else " ".join(w if w == "&&" else shlex.quote(w) for w in words)
    # run through bash so quoting/&& works as typed; segments split on &&
    commands = [["bash", "-c", c.strip()] for c in line.split("&&") if c.strip()]
    result = verifier.run_checks(args.path, commands, commit=_git_sha(args.path))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def _git_sha(path: str) -> str | None:
    import subprocess
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                           text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def cmd_dashboard(args) -> int:
    from engine_core.dashboard import serve
    serve(HOME, host=args.host, port=args.port,
          baseline_in=args.baseline_in, baseline_out=args.baseline_out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine-ai runtime", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="task states + quota + usage")
    p.add_argument("--config")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="validate a task dependency graph")
    p.add_argument("--graph", required=True, help="JSON file: {tasks: [TaskSpec…]}")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="lease and execute ready tasks via a provider")
    p.add_argument("--config", help="engine config (YAML/JSON)")
    p.add_argument("--graph", required=True)
    p.add_argument("--profile", default="backend",
                   help="run under this profile (its key + model)")
    p.add_argument("--agent", help="run under an agent's assigned profile "
                                   "(e.g. engine-app-builder)")
    p.add_argument("--model", help="override the profile's model ref")
    p.add_argument("--free-only", action="store_true", default=False)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("verify", help="run verification commands → evidence")
    p.add_argument("--path", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command(s) to run, after `--` or as one quoted string; "
                        "'&&' splits sequential checks")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("profiles", help="per-agent API key + model assignments")
    p.add_argument("--config", help="base engine config (YAML/JSON)")
    pr_sub = p.add_subparsers(dest="profiles_cmd", required=True)
    pr_sub.add_parser("list", help="all profiles: key status (masked), model, agents")
    ps = pr_sub.add_parser("show", help="one profile in full")
    ps.add_argument("profile")
    pset = pr_sub.add_parser("set", help="assign a credential ref / model to a profile")
    pset.add_argument("profile")
    pset.add_argument("--credential-ref", help="env:VAR or vault:NAME (references only)")
    pset.add_argument("--model", help="model id or catalog:<name> ref")
    pset.add_argument("--read-only", dest="read_only", action="store_true", default=None)
    pset.add_argument("--account-group")
    pcl = pr_sub.add_parser("clear", help="remove local overrides for a profile")
    pcl.add_argument("profile")
    pr_sub.add_parser("agents", help="agent → profile mapping (engine + domain experts)")
    p.set_defaults(fn=cmd_profiles)

    p = sub.add_parser("keys", help="detect/import keys.csv → the 10 agent profiles")
    p.add_argument("path", nargs="?", help="keys.csv path (default: auto-detect)")
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser("build", help="multi-agent build: plan → execute per-profile → verify")
    p.add_argument("objective", help="what the agents should build, in plain language")
    p.add_argument("--workspace", help="dir to work in (default ~/.engine-ai/builds/<slug>)")
    p.add_argument("--project", help="memory project name (default: workspace folder name)")
    p.add_argument("--config", help="engine config (YAML/JSON)")
    p.add_argument("--max-tasks", type=int, default=6)
    p.add_argument("--json", action="store_true", help="print the full JSON report")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--run-id", help=argparse.SUPPRESS)  # set by the MCP launcher
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("resume", help="re-run a workspace's stored plan (retry failed/blocked tasks)")
    p.add_argument("workspace")
    p.add_argument("--config", help="engine config (YAML/JSON)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--run-id", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("dashboard", help="live web dashboard: agent activity, tokens, cost saved")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--baseline-in", type=float, default=3.0,
                   help="baseline $ per 1M input tokens used to estimate savings (default 3)")
    p.add_argument("--baseline-out", type=float, default=15.0,
                   help="baseline $ per 1M output tokens used to estimate savings (default 15)")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("build-status", help="live status + last report of a workspace build")
    p.add_argument("--workspace")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_build_status)

    p = sub.add_parser("codex-env", help='print vault keys as exports: eval "$(engine-ai runtime codex-env)"')
    p.set_defaults(fn=cmd_codex_env)

    p = sub.add_parser("models", help="model catalog: list / refresh / pin")
    m_sub = p.add_subparsers(dest="models_cmd", required=True)
    pl = m_sub.add_parser("list", help="list eligible models (free-only by default)")
    pl.add_argument("--include-paid", action="store_true")
    pl.add_argument("--all-capabilities", action="store_true",
                    help="don't require tool support")
    pl.add_argument("--limit", default=25)
    m_sub.add_parser("refresh", help="fetch the live catalog (network)")
    pp = m_sub.add_parser("pin", help="pin a catalog:<name> reference to a model ID")
    pp.add_argument("name")
    pp.add_argument("model_id")
    p.set_defaults(fn=cmd_models)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ConfigError, GraphError, CatalogError, StateError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except ProviderError as e:
        print(f"✗ provider error ({e.kind}): {e.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
