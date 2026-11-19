"""Host-assistant activity log — which engine-ai MCP tools the AI called, and how it went.

The MCP server appends one JSON line per `tools/call` to <home>/activity.jsonl so the
dashboard can show what the host assistant (Claude Code / Codex) did through engine-ai.
Deliberately minimal: tool name, ok, duration, a short error CLASS — never the
arguments or results (they can carry secrets, keys, file contents). Logging must never
break a tool call, so every failure here is swallowed.
"""

from __future__ import annotations

import json
import os
import threading
import time

FILENAME = "activity.jsonl"
MAX_BYTES = 4 * 1024 * 1024  # rotate to activity.jsonl.1 past this — the log stays small

_lock = threading.Lock()


def activity_path(home: str) -> str:
    return os.path.join(home, FILENAME)


def record(home: str, tool: str, ok: bool, duration_ms: float, error: str | None = None) -> None:
    """Append one tool-call record. Never raises."""
    rec = {"ts": round(time.time(), 3), "tool": str(tool)[:80], "ok": bool(ok),
           "ms": round(float(duration_ms), 1)}
    if error:
        rec["error"] = str(error)[:80]
    try:
        path = activity_path(home)
        with _lock:
            os.makedirs(home, exist_ok=True)
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never fail the call it observes
        pass


def outcome_ok(out) -> bool:
    """Operational outcome of a tool result: `{"ok": false, ...}` counts as not-ok."""
    return not (isinstance(out, dict) and out.get("ok") is False)
