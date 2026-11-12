"""Tool executor with bounded results + effect journal (upgrade.md §4).

- Results are size-bounded before they re-enter model context.
- Every EXECUTED (side-effecting) call is journaled with an idempotency key scoped to
  (run, task, attempt, tool, call id, ARGUMENTS). Replaying the same call after a crash
  or transport failure does not repeat a write that already landed — but free models
  number their calls `call_0, call_1…` in EVERY task, so a bare `tool:call_id` key
  (v0.17) silently skipped other tasks' and other runs' writes.
- Policy violations return a typed error the model can read and route around.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import threading
import time

from engine_core.tools.policy import Policy, PolicyViolation
from engine_core.tools.registry import Registry, UnknownTool

JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects_v2(
  key TEXT PRIMARY KEY, scope TEXT, tool TEXT, call_id TEXT, args_hash TEXT, ok INTEGER,
  result_summary TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_effects_scope ON effects_v2(scope);
"""


def effect_scope(context: dict | None) -> str:
    """run/task/attempt the effect belongs to (`effect_scope` overrides — the worker
    passes its attempt's scope so a resumed attempt replays its own journal)."""
    ctx = context or {}
    if ctx.get("effect_scope"):
        return str(ctx["effect_scope"])
    return f"{ctx.get('run_id', '')}/{ctx.get('task_id', '')}"


def effect_key(tool: str, call_id: str, scope: str = "", args_hash: str = "") -> str:
    raw = json.dumps([scope, tool, call_id, args_hash])
    return hashlib.sha256(raw.encode()).hexdigest()


def _accepts_context(fn) -> bool:
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        varargs = any(p.kind == p.VAR_POSITIONAL for p in inspect.signature(fn).parameters.values())
        return varargs or len(params) >= 2
    except (TypeError, ValueError):
        return False


class Executor:
    def __init__(self, registry: Registry, policy: Policy, journal_path: str | None = None,
                 max_result_chars: int = 8000):
        self.registry = registry
        self.policy = policy
        self.max_result_chars = max_result_chars
        self.db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if journal_path:
            os.makedirs(os.path.dirname(journal_path), exist_ok=True)
            self.db = sqlite3.connect(journal_path, check_same_thread=False, timeout=15)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(JOURNAL_SCHEMA)
            self.db.commit()

    def close(self) -> None:
        if self.db is not None:
            with self._lock:
                self.db.close()
                self.db = None

    def _journal_lookup(self, key: str) -> dict | None:
        if not self.db:
            return None
        with self._lock:
            r = self.db.execute("SELECT result_summary, ok FROM effects_v2 WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return {"ok": bool(r[1]), "summary": json.loads(r[0])}
        except json.JSONDecodeError:
            return {"ok": bool(r[1]), "summary": {"journaled": True}}

    def _journal_write(self, key: str, scope: str, tool: str, call_id: str, args_hash: str,
                       ok: bool, summary) -> None:
        if not self.db:
            return
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO effects_v2 VALUES(?,?,?,?,?,?,?,?)",
                            (key, scope, tool, call_id, args_hash, int(ok),
                             json.dumps(summary, default=str)[:4000], time.time()))

    def execute(self, call: dict, context: dict | None = None) -> dict:
        """Execute one model-requested tool call → {"content": str, "ok": bool}."""
        name = call.get("function", {}).get("name") or call.get("name") or ""
        raw_args = call.get("function", {}).get("arguments") or call.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            return {"content": f"error: arguments for {name!r} are not valid JSON", "ok": False}
        try:
            entry = self.registry.get(name)
        except UnknownTool as e:  # typed error, the model can recover
            return {"content": f"error: unknown tool {e}", "ok": False, "blocked": True}
        call_id = str(call.get("id") or "")
        scope = effect_scope(context)
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
        key = effect_key(name, call_id, scope, args_hash)

        prior = None if entry.read_only else self._journal_lookup(key)
        if prior:
            return {"content": json.dumps(
                {"ok": prior["ok"], "already_applied": True, "prior_summary": prior["summary"]}),
                "ok": prior["ok"], "replayed": True}
        ctx = {**(context or {}), "policy": self.policy}
        try:
            self.policy.check(name, args, self.registry)
            # context-aware tools (build tools) get workspace + policy; MCP handlers don't.
            # Decided from the SIGNATURE — retrying on TypeError re-ran a tool that
            # raised TypeError internally (a duplicated side effect).
            out = entry.fn(args, ctx) if _accepts_context(entry.fn) else entry.fn(args)
            ok = not (isinstance(out, dict) and out.get("ok") is False)
            if not entry.read_only:
                self._journal_write(key, scope, name, call_id, args_hash, ok, out)
        except PolicyViolation as e:
            return {"content": f"error: {e}", "ok": False, "blocked": True}
        except Exception as e:  # noqa: BLE001 - operational error, serialized for the model
            if not entry.read_only:
                self._journal_write(key, scope, name, call_id, args_hash, False, {"error": str(e)})
            return {"content": f"error: {e!r}", "ok": False}
        text = json.dumps(out, default=str)
        if len(text) > self.max_result_chars:
            text = text[:self.max_result_chars] + f'… (truncated at {self.max_result_chars} chars)'
        return {"content": text, "ok": ok}
