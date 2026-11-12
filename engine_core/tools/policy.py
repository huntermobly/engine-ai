"""Execution policy (upgrade.md §4-5) — enforced by the executor, not the prompt.

- `read_only` profiles physically cannot call writing tools.
- ONE resolver (`resolve`) turns a tool's `path`/`cwd` into a real absolute path; the
  same resolved path is both CHECKED here and USED by the build tools, so a relative
  `cwd` can never be checked against the workspace but executed against the server's
  own working directory.
- Reads: anywhere inside the workspace (shared contracts stay readable).
- Writes: only inside the task's `permitted_paths` (owned paths, workspace-relative);
  with no owned paths, anywhere inside the workspace. Symlinks are resolved first, so
  a link inside an owned dir that points at another task's file is a cross-task write.
- Credential files (keys.csv, engine-ai-env.sh, master.key, …) and hidden stores
  (engine-ai home, ~/.codex, ~/.ssh, gh, Claude) are never readable or writable.
- Shell commands (`run_command`) only run inside the bubblewrap sandbox
  (engine_core/tools/sandbox.py). When it is unavailable — or `shell_isolation="none"`
  — shell commands are REFUSED, never run unconfined.
"""

from __future__ import annotations

import fnmatch
import os

CREDENTIAL_NAMES = ("keys.csv", "*.keys.csv", "engine-ai-env.sh", "master.key",
                    ".credentials.json", "auth.json", "id_rsa", "id_ed25519")
SHELL_ISOLATION = ("auto", "bwrap", "none")
PATH_ARGS = ("path", "file", "target")


class PolicyViolation(PermissionError):
    pass


def default_hidden_paths() -> list[str]:
    home = os.path.expanduser("~")
    engine_home = os.environ.get("ENGINE_AI_HOME") or os.path.join(home, ".engine-ai")
    return [engine_home, os.path.join(home, ".codex"), os.path.join(home, ".ssh"),
            os.path.join(home, ".config", "gh"), os.path.join(home, ".claude"),
            os.path.join(home, ".aws")]


def _within(path: str, scope: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(scope), path]) == os.path.realpath(scope)
    except ValueError:
        return False


class Policy:
    def __init__(self, *, read_only: bool = False, allowed_tools: set[str] | None = None,
                 permitted_paths: list[str] | None = None, root: str | None = None,
                 hidden_paths: list[str] | None = None, shell_isolation: str = "auto"):
        if shell_isolation not in SHELL_ISOLATION:
            raise ValueError(f"shell_isolation must be one of {SHELL_ISOLATION}")
        self.read_only = read_only
        self.allowed_tools = allowed_tools
        self.root = os.path.realpath(root) if root else None
        base = self.root or os.getcwd()
        # owned paths are WORKSPACE-relative (planner writes `src/x`, not `/abs/src/x`)
        self.permitted_paths = [os.path.realpath(p if os.path.isabs(p) else os.path.join(base, p))
                                for p in (permitted_paths or [])]
        self.hidden_paths = [os.path.realpath(p) for p in
                             (default_hidden_paths() if hidden_paths is None else
                              default_hidden_paths() + list(hidden_paths))]
        self.shell_isolation = shell_isolation

    # ---------------------------------------------------------------- resolution
    def resolve(self, path: str = "", cwd: str | None = None) -> str:
        """Real absolute path for a tool argument (relative = workspace-relative)."""
        base = self.root or os.getcwd()
        if cwd:
            base = cwd if os.path.isabs(cwd) else os.path.join(base, cwd)
        full = path if os.path.isabs(path or "") else os.path.join(base, path or "")
        return os.path.realpath(full)

    def shell_available(self) -> bool:
        if self.shell_isolation == "none":
            return False
        from engine_core.tools import sandbox
        return sandbox.available()

    def writable_dirs(self) -> list[str]:
        """Directories a sandboxed shell may persist writes into."""
        if self.read_only:
            return []
        if self.permitted_paths:
            return [p for p in self.permitted_paths if os.path.isdir(p)]
        return [self.root] if self.root else []

    # ---------------------------------------------------------------- checks
    def allows(self, tool: str, registry) -> bool:
        """Whole-tool gate (also filters the tool list shown to the model)."""
        try:
            entry = registry.get(tool)
        except KeyError:
            return False
        if getattr(entry, "shell", False) and not self.shell_available():
            return False
        if self.read_only and not entry.read_only and not getattr(entry, "shell", False):
            return False  # sandboxed shell is fine read-only: nothing is writable
        if self.allowed_tools is not None and tool not in self.allowed_tools:
            return False
        return True

    def check(self, tool: str, args: dict, registry) -> None:
        """Raise PolicyViolation unless this call is permitted."""
        if not self.allows(tool, registry):
            entry = registry._tools.get(tool) if hasattr(registry, "_tools") else None
            if entry is not None and getattr(entry, "shell", False) and not self.shell_available():
                reason = ("shell commands need the bubblewrap sandbox, which is unavailable "
                          "(or shell_isolation=none) — use write_file/read_file instead")
            elif self.read_only:
                reason = "read-only profile"
            else:
                reason = "not in the profile's allowed tools"
            raise PolicyViolation(f"tool {tool!r} blocked: {reason}")
        entry = registry.get(tool)
        writes = not entry.read_only and not getattr(entry, "shell", False)
        cwd = args.get("cwd") or args.get("root")
        if isinstance(cwd, str) and cwd:
            self._check_read(self.resolve(cwd), cwd)  # a cwd must stay inside the workspace
        for key in PATH_ARGS:
            val = args.get(key)
            if isinstance(val, str):
                real = self.resolve(val, cwd if isinstance(cwd, str) else None)
                (self._check_write if writes else self._check_read)(real, val)

    def _credential(self, real: str) -> bool:
        name = os.path.basename(real)
        if any(fnmatch.fnmatch(name, pat) for pat in CREDENTIAL_NAMES):
            return True
        if self.root and _within(real, self.root):
            return False  # a workspace that happens to live under ~/.engine-ai is fine
        return any(_within(real, h) for h in self.hidden_paths)

    def _check_read(self, real: str, shown: str) -> None:
        if self._credential(real):
            raise PolicyViolation(f"path {shown!r}: credential store access is denied")
        if self.root and not _within(real, self.root):
            raise PolicyViolation(f"path {shown!r} is outside the permitted workspace")

    def _check_write(self, real: str, shown: str) -> None:
        self._check_read(real, shown)
        if self.permitted_paths and not any(_within(real, p) for p in self.permitted_paths):
            owned = ", ".join(os.path.relpath(p, self.root) if self.root else p
                              for p in self.permitted_paths)
            raise PolicyViolation(f"path {shown!r} is not owned by this task (owned: {owned})")
