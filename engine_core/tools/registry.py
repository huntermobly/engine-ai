"""Tool registry (upgrade.md §4).

A profile's `read_only` flag and its permitted tools are enforced HERE, by the executor,
not merely included in the prompt. `default_registry()` exposes the existing MCP tools
(compatibility surface) when the mcp package is importable.
"""

from __future__ import annotations

import os

from dataclasses import dataclass


@dataclass
class ToolEntry:
    fn: object
    schema: dict
    read_only: bool = False
    description: str = ""
    shell: bool = False  # runs model-chosen shell commands: sandbox-only (Policy)


class UnknownTool(KeyError):
    pass


class Registry:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(self, name: str, fn, schema: dict | None = None, read_only: bool = False,
                 description: str = "", shell: bool = False) -> None:
        self._tools[name] = ToolEntry(fn=fn, schema=schema or {}, read_only=read_only,
                                      description=description, shell=shell)

    def get(self, name: str) -> ToolEntry:
        if name not in self._tools:
            raise UnknownTool(name)
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> dict[str, dict]:
        return {name: {"inputSchema": t.schema, "description": t.description or name,
                       "read_only": t.read_only} for name, t in self._tools.items()}


def _register_build_tools(reg: Registry) -> None:
    """File + shell tools workers need to actually BUILD things.

    Paths are resolved by the executor's Policy (`context["policy"]`) — the SAME
    resolution that was checked is the one written, so a relative `cwd` can never
    drift onto the server process's own working directory. Shell commands run only
    inside the bubblewrap sandbox (engine_core/tools/sandbox.py).
    """
    from engine_core.tools.policy import Policy

    def _policy(context: dict | None) -> Policy:
        ctx = context or {}
        return ctx.get("policy") or Policy(root=ctx.get("workspace") or os.getcwd())

    def _path(args: dict, context: dict | None, key: str = "path") -> str:
        return _policy(context).resolve(args.get(key, ""), args.get("cwd") or args.get("root"))

    def read_file(args, context=None):
        with open(_path(args, context), encoding="utf-8", errors="ignore") as f:
            return {"ok": True, "path": args["path"], "content": f.read(50_000)}

    def write_file(args, context=None):
        path = _path(args, context)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = args.get("content", "")
        # O_NOFOLLOW: a symlink swapped in after the policy check is refused, not followed
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        return {"ok": True, "path": args["path"], "bytes": len(data)}

    def append_file(args, context=None):
        """Append to a file (models with small output caps write big files in chunks)."""
        path = _path(args, context)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = args.get("content", "")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(data)
        return {"ok": True, "path": args["path"], "appended": len(data),
                "size": os.path.getsize(path)}

    def list_dir(args, context=None):
        base = _path(args, context)
        entries = []
        for name in sorted(os.listdir(base))[:200]:
            full = os.path.join(base, name)
            entries.append(("/" if os.path.isdir(full) else " ", name))
        return {"ok": True, "path": args.get("path", "."), "entries": entries}

    def run_command(args, context=None):
        import subprocess
        from engine_core.tools import sandbox
        pol = _policy(context)
        argv = args["command"] if isinstance(args["command"], list) else \
            ["bash", "-c", str(args["command"])]
        cwd = pol.resolve(args.get("cwd") or "")
        workspace = pol.root or cwd
        timeout = max(1, min(int(args.get("timeout", 60) or 60), 600))
        try:
            wrapped = sandbox.wrap([str(a) for a in argv], workspace=workspace,
                                   writable=pol.writable_dirs(), hidden=pol.hidden_paths, cwd=cwd)
            p = subprocess.run(wrapped, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            return {"ok": p.returncode == 0, "exit_code": p.returncode, "sandboxed": True,
                    "stdout": (p.stdout or "")[-4000:], "stderr": (p.stderr or "")[-2000:]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "exit_code": None, "stderr": f"command timed out after {timeout}s"}
        except (OSError, RuntimeError) as e:
            return {"ok": False, "exit_code": None, "stderr": f"{e.__class__.__name__}: {e}"}

    reg.register("read_file", read_file,
                 {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"]}, read_only=True,
                 description="Read a file in the workspace (bounded to 50k chars).")
    reg.register("write_file", write_file,
                 {"type": "object", "properties": {"path": {"type": "string"},
                                                   "content": {"type": "string"}},
                  "required": ["path", "content"]},
                 description="Create/overwrite a file in the workspace.")
    reg.register("append_file", append_file,
                 {"type": "object", "properties": {"path": {"type": "string"},
                                                   "content": {"type": "string"}},
                  "required": ["path", "content"]},
                 description="Append content to a file in your owned paths (write large files "
                             "in several chunks: write_file the first, append_file the rest).")
    reg.register("list_dir", list_dir,
                 {"type": "object", "properties": {"path": {"type": "string"}}},
                 read_only=True, description="List a workspace directory.")
    reg.register("run_command", run_command,
                 {"type": "object",
                  "properties": {"command": {}, "timeout": {"type": "integer"},
                                 "cwd": {"type": "string"}},
                  "required": ["command"]},
                 description="Run a command in the workspace inside a sandbox: offline, the "
                             "workspace is readable, only your owned directories keep writes "
                             "(create files with write_file). Bounded, no stdin.",
                 shell=True)


def build_registry() -> Registry:
    """The tools a build worker needs (no MCP surface — standalone-safe)."""
    reg = Registry()
    reg.register("report_status", lambda a: {"ok": True, "status": "working"},
                 {"type": "object", "properties": {}}, read_only=True,
                 description="Report progress")
    _register_build_tools(reg)
    return reg


def default_registry(mcp_tools: bool = True) -> Registry:
    """A registry with the MCP toolkit's tools registered (read-only ones marked)."""
    reg = build_registry()
    if mcp_tools:
        try:
            from engine_core._compat import mcp_module
            forge = mcp_module("forge_mcp")
            forge.TOOLS  # force registry population without running main()
            read_only_tools = {"search_repo", "list_skills", "get_skill", "list_skills",
                               "list_secrets", "deploy_readiness", "memory_recall", "memory_get",
                               "memory_list", "memory_context", "app_list", "app_find",
                               "knowledge_search", "knowledge_domains", "context_pack",
                               "suggest_experts", "web_search", "web_search_status",
                               "so_search", "so_debug", "responsive_audit", "index_repo",
                               "import_repo_skills", "app_resume"}
            for spec in forge.TOOLS:
                reg.register(spec["name"], forge.HANDLERS[spec["name"]],
                             schema=spec.get("inputSchema"),
                             read_only=spec["name"] in read_only_tools,
                             description=spec.get("description", ""))
        except Exception:  # noqa: BLE001 - mcp surface optional for the standalone runtime
            pass
    return reg
