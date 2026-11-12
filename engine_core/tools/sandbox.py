"""Kernel-enforced isolation for shell commands (bubblewrap).

`owned_paths` checks on write_file mean nothing if `run_command` can run
`echo x > ../other-task/file` — so every model-chosen shell command (worker
`run_command` AND planner-chosen verification commands) runs inside a bubblewrap
mount namespace:

- `/` is mounted READ-ONLY; `/tmp` is a private tmpfs.
- `$HOME` is replaced by an empty tmpfs (hides ~/.engine-ai vault, ~/.codex, gh/ssh
  tokens, …); toolchain directories on PATH that live under $HOME (nvm, pyenv,
  ~/.local/bin, cargo) are re-mounted read-only so the same node/python run.
- extra `hidden` paths (e.g. a custom ENGINE_AI_HOME) are replaced by empty tmpfs.
- the workspace is a WRITABLE-BUT-DISCARDED overlay (tests may write caches);
  only `writable` directories (the task's owned directories) persist writes.
- network is unshared (offline).

This PREVENTS writes; it is not an after-the-fact audit. When bubblewrap is not
usable on the host, `available()` is False and callers must refuse mutating shell
commands (see Policy.shell_isolation) instead of running them unconfined.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


@lru_cache(maxsize=1)
def available() -> bool:
    """True when bubblewrap exists AND can create the namespaces we need here."""
    b = bwrap_path()
    if not b:
        return False
    try:
        p = subprocess.run([b, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                            "--tmpfs", "/tmp", "--unshare-net", "--die-with-parent", "true"],
                           capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _toolchain_dirs(home: str) -> list[str]:
    """PATH entries under $HOME (+ the roots they need) to re-expose read-only."""
    out: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isdir(entry):
            continue
        real = os.path.realpath(entry)
        for cand in (entry, real):
            if not cand.startswith(home + os.sep):
                continue
            base = os.path.basename(cand)
            parent = os.path.dirname(cand)
            if base in ("bin", "shims") and parent != home:
                if parent == os.path.join(home, ".local"):
                    out += [cand, os.path.join(parent, "lib")]
                else:
                    out.append(parent)  # e.g. ~/.nvm/versions/node/vX (bin + lib)
            else:
                out.append(cand)
    for extra in (".pyenv",):  # pyenv shims exec ~/.pyenv/libexec + versions
        d = os.path.join(home, extra)
        if any(e.startswith(d) for e in out) and os.path.isdir(d):
            out.append(d)
    seen, uniq = set(), []
    for d in sorted(set(out), key=len):
        if os.path.isdir(d) and not any(d.startswith(s + os.sep) for s in seen):
            seen.add(d)
            uniq.append(d)
    return uniq


def wrap(argv: list[str], *, workspace: str, writable: list[str] | None = None,
         hidden: list[str] | None = None, cwd: str | None = None) -> list[str]:
    """bubblewrap argv that runs `argv` under the isolation described above."""
    b = bwrap_path()
    if not b:
        raise RuntimeError("bubblewrap is not installed")
    home = os.path.realpath(os.path.expanduser("~"))
    ws = os.path.realpath(workspace)
    cmd = [b, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    cmd += ["--tmpfs", home]
    for d in _toolchain_dirs(home):
        cmd += ["--ro-bind", d, d]
    for h in hidden or []:
        h = os.path.realpath(h)
        if os.path.isdir(h) and not (ws == h or ws.startswith(h + os.sep)):
            cmd += ["--tmpfs", h]
    # workspace: writable scratch overlay whose changes are DISCARDED on exit
    cmd += ["--overlay-src", ws, "--tmp-overlay", ws]
    for w in writable or []:
        w = os.path.realpath(w)
        if os.path.isdir(w) and (w == ws or w.startswith(ws + os.sep)):
            cmd += ["--bind", w, w]  # persistent: the task's own directories only
    run_dir = os.path.realpath(cwd or ws)
    cmd += ["--setenv", "HOME", home, "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "PWD", run_dir,  # pyenv/nvm shims cd to $PWD; the parent's is hidden
            "--setenv", "PYENV_DIR", run_dir,  # set by a parent pyenv shim → hidden dir
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session",
            "--chdir", run_dir, "--"]
    return cmd + list(argv)
