"""Deterministic offline provider — replays a JSON script instead of calling a model.

Used by the test suite (MCP → background build → status) and for dry runs that must not
touch any account: `ENGINE_AI_PROVIDER=scripted:/path/script.json`. It never sees or
needs an API key, and every completion reports zero cost.

Script format:
  {"plan":  {"tasks": [...planner JSON...]},
   "tasks": {"<task-id>": [step, step, ...]}}
A step is a final answer string, or {"tool": name, "args": {...}, "id": "call_0"}.
Step N is played on the worker's N-th model turn (the last step repeats).
"""

from __future__ import annotations

import json
import re

from engine_core.providers.base import Completion, Provider, Usage

DONE = '{"status": "done", "summary": "scripted step complete"}'


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, script: dict, delay_s: float = 0.0):
        self.script = script
        self.delay_s = delay_s

    @staticmethod
    def load(path: str) -> "ScriptedProvider":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return ScriptedProvider(data, float(data.get("delay_s", 0)))

    def complete(self, model, messages, *, tools=None, max_output_tokens=None, temperature=None):
        if self.delay_s:
            import time
            time.sleep(self.delay_s)
        system, packet = messages[0].content, messages[1].content if len(messages) > 1 else ""
        usage = Usage(input_tokens=(len(system) + len(packet)) // 4, output_tokens=20)
        if "planner" in system:
            return Completion(json.dumps(self.script.get("plan", {"tasks": []})), model, usage)
        m = re.search(r"# Task packet (\S+)", packet)
        steps = self.script.get("tasks", {}).get(m.group(1) if m else "", [DONE]) or [DONE]
        turn = sum(1 for msg in messages if msg.role == "assistant")
        step = steps[min(turn, len(steps) - 1)]
        if isinstance(step, dict):
            call = {"id": step.get("id", f"call_{turn}"), "type": "function",
                    "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))}}
            return Completion("", model, usage, tool_calls=[call])
        return Completion(str(step), model, usage)
