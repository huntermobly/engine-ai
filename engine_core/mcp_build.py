"""Start / monitor / resume engine builds from an MCP client (Claude Code, Codex).

A build takes minutes (several model turns per agent), far longer than an MCP tool call
should block, so `start_build` launches `python3 -m engine_core build` as a DETACHED
background process from THIS source tree and returns immediately with a run id;
`build_status` reads the run's live status file, recent progress events and final
report. Nothing here calls a model itself.

Every run gets its own record in <home>/build-runs/<run_id>.json (pid, workspace,
command, log paths). Status distinguishes running / complete / failed / blocked /
crashed (process gone without a final report) so a client never waits forever.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMINAL = ("complete", "failed", "blocked")


def _slug(text: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n]


def _runs_dir(home: str) -> str:
    d = os.path.join(home, "build-runs")
    os.makedirs(d, exist_ok=True)
    return d


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie child of this process counts as finished
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][:1] != "Z"
    except OSError:
        return True


def _workspace_busy(home: str, workspace: str) -> bool:
    from engine_core.pipeline import BuildPipeline
    lock = os.path.join(BuildPipeline(home).run_dir(workspace), "lock")
    if not os.path.exists(lock):
        return False
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def start_build(home: str, objective: str | None = None, *, workspace: str | None = None,
                project: str | None = None, max_tasks: int = 6, config: str | None = None,
                resume: bool = False) -> dict:
    """Launch a background build (or resume) and return its run id immediately."""
    if not resume and not (objective or "").strip():
        return {"ok": False, "error": "objective is required"}
    run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if resume and not workspace:
        return {"ok": False, "error": "resume needs the workspace of an earlier build"}
    # a FRESH workspace per run unless the caller names one
    workspace = os.path.realpath(os.path.expanduser(workspace or os.path.join(
        home, "builds", f"{_slug(objective) or 'build'}-{run_id[-6:]}")))
    if resume:
        from engine_core.pipeline import run_dir_for
        if not os.path.isfile(os.path.join(run_dir_for(home, workspace), "plan.json")):
            return {"ok": False, "error": f"no stored plan for {workspace} — nothing to resume; "
                                          "start a build there first (engine_build_start)"}
    if _workspace_busy(home, workspace):
        return {"ok": False, "error": f"workspace busy: another build is running in {workspace}"}
    os.makedirs(workspace, exist_ok=True)
    rd = _runs_dir(home)
    out_path, err_path = os.path.join(rd, f"{run_id}.out.json"), os.path.join(rd, f"{run_id}.log")
    cmd = [sys.executable, "-m", "engine_core"]
    if resume:
        cmd += ["resume", workspace]
    else:
        cmd += ["build", objective, "--workspace", workspace, "--max-tasks", str(int(max_tasks))]
        if project:
            cmd += ["--project", project]
    if config:
        cmd += ["--config", config]
    cmd += ["--json", "--run-id", run_id]
    env = {**os.environ, "ENGINE_AI_HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True)  # survives the MCP call
    record = {"run_id": run_id, "pid": proc.pid, "workspace": workspace, "objective": objective,
              "project": project, "resume": resume, "started_at": time.time(),
              "command": cmd[:3] + [c if len(c) < 200 else c[:200] + "…" for c in cmd[3:]],
              "source_dir": REPO_ROOT, "out": out_path, "log": err_path}
    with open(os.path.join(rd, f"{run_id}.json"), "w") as f:
        json.dump(record, f, indent=2)
    return {"ok": True, "run_id": run_id, "pid": proc.pid, "workspace": workspace,
            "log": err_path, "source_dir": REPO_ROOT,
            "next": f"call engine_build_status with run_id={run_id!r} to monitor"}


def _tail(path: str, n: int) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _compact_report(rep: dict) -> dict:
    keep = ("run_id", "project", "workspace", "status", "exit_code", "stopped", "total", "complete",
            "not_run", "usage", "plan", "plan_model", "plan_warnings", "resumed_from_plan",
            "report_path", "plan_path", "duration_s")
    out = {k: rep.get(k) for k in keep if k in rep}
    out["tasks"] = [{k: t.get(k) for k in ("id", "profile", "model", "dependencies", "owned_paths",
                                           "artifacts", "status", "attempts", "resumed", "error",
                                           "verification", "summary", "usage")}
                    for t in rep.get("tasks", [])]
    for t in out["tasks"]:
        if t.get("summary"):
            t["summary"] = str(t["summary"])[:400]
        if t.get("error"):
            t["error"] = str(t["error"])[:400]
        for c in t.get("verification") or []:
            c["output_tail"] = (c.get("output_tail") or "")[-300:]
    return out


def build_status(home: str, run_id: str | None = None, workspace: str | None = None,
                 events: int = 12) -> dict:
    """Live status of a run (by id) or of a workspace's latest run."""
    from engine_core.pipeline import BuildPipeline
    record = {}
    if run_id:
        try:
            with open(os.path.join(_runs_dir(home), f"{run_id}.json")) as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"ok": False, "error": f"unknown run id {run_id!r}"}
        workspace = record["workspace"]
    if not workspace:
        return {"ok": False, "error": "pass run_id or workspace"}
    workspace = os.path.realpath(os.path.expanduser(workspace))
    rd = BuildPipeline(home).run_dir(workspace)
    status = {}
    try:
        with open(os.path.join(rd, "status.json")) as f:
            status = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    report = None
    try:
        with open(os.path.join(rd, "report.json")) as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    if run_id and report and report.get("run_id") != run_id:
        report = None  # that report belongs to an earlier run of the workspace
    if run_id and status.get("run_id") not in (None, run_id):
        status = {}  # likewise the live status: a resume must not look "complete" at birth
    alive = _alive(record.get("pid")) if record else None
    if report:
        state = report["status"]
    elif record and not alive:
        state = "crashed"  # process gone, no final report: see the log, then resume
    else:
        state = status.get("state") or ("starting" if alive else "unknown")
    now = time.time()
    out = {"ok": True, "run_id": run_id or status.get("run_id"), "state": state,
           "terminal": state in TERMINAL or state == "crashed", "workspace": workspace,
           "process_alive": alive, "pid": record.get("pid"),
           "elapsed_s": round(now - record["started_at"], 1) if record else None,
           "seconds_since_last_event": round(now - status["updated_at"], 1) if status.get("updated_at") else None,
           "current_task": status.get("current_task"), "task_states": status.get("tasks", {}),
           "recent_events": _tail(os.path.join(rd, "progress.jsonl"), events),
           "log": record.get("log"), "source_dir": record.get("source_dir")}
    if report:
        out["report"] = _compact_report(report)
    if state == "crashed":
        try:
            with open(record["log"]) as f:
                out["log_tail"] = f.read()[-1500:]
        except OSError:
            pass
        out["next"] = "inspect log_tail, then engine_build_resume with this workspace"
    elif not out["terminal"]:
        out["next"] = "still running — poll engine_build_status again (or pass wait_s)"
    return out


def wait_status(home: str, run_id: str, wait_s: float = 0, poll_s: float = 2.0) -> dict:
    """build_status, optionally waiting (bounded) until the run reaches a terminal state."""
    deadline = time.time() + max(0.0, min(float(wait_s or 0), 45.0))
    while True:
        st = build_status(home, run_id=run_id)
        if not st.get("ok") or st.get("terminal") or time.time() >= deadline:
            return st
        time.sleep(poll_s)


def engine_info(home: str) -> dict:
    """Which source tree this server/process actually loaded — proves the client is
    connected to the code under test, not an older install or another checkout."""
    import engine_core
    from engine_core.tools import sandbox

    def git(*a):
        try:
            p = subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
            return p.stdout.strip() if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    try:
        with open(os.path.join(REPO_ROOT, "package.json")) as f:
            version = json.load(f).get("version")
    except (OSError, json.JSONDecodeError):
        version = None
    return {"source_dir": REPO_ROOT, "engine_core": os.path.dirname(os.path.abspath(engine_core.__file__)),
            "version": version, "git_commit": git("rev-parse", "HEAD"),
            "git_dirty": bool(git("status", "--porcelain")), "pid": os.getpid(),
            "python": sys.version.split()[0], "engine_home": home,
            "shell_sandbox": "bubblewrap" if sandbox.available() else "unavailable (shell commands refused)",
            "provider_mode": os.environ.get("ENGINE_AI_PROVIDER") or "openrouter (per-profile keys)"}
