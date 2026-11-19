"""Worker loop — request → validated tool calls → bounded results → continuation → outcome.

Per upgrade.md §3-4:
- Each turn is bounded (turns/calls/output); every result is size-bounded before it
  re-enters model context.
- Tool effects are JOURNALED: after a transport failure the loop resumes from the
  checkpoint instead of blindly repeating writes.
- auth/quota provider errors stop the loop for escalation — never account rotation.
- The final outcome is STRUCTURED (status, changed files, checks, unresolved questions)
  and persisted outside the model context; full logs go to the event log, not prompts.
- Checkpoints after every turn make runs resumable (run → crash → resume RUN_ID).
- HONEST outcomes: a final answer without the structured JSON is `unstructured`, never
  `done`; a task past its wall-clock budget is `timeout`; usage (model calls, tool
  calls, input/output tokens) is counted per task and returned with the outcome.
"""

from __future__ import annotations

import json
import time

from engine_core.providers.base import Message, Provider, ProviderError
from engine_core.runtime.state import StateStore
from engine_core.tools.executor import Executor
from engine_core.telemetry.usage import UsageJournal

MAX_RESULT_CHARS = 8000  # bounded tool results before re-entering model context
MAX_NUDGES = 2  # per attempt: re-prompts after a truncated/empty reply before giving up

SYSTEM_PROMPT = (
    "You are an engine-ai worker executing one bounded task packet. Work only within the "
    "permitted paths. Call tools as needed; when the task's acceptance criteria are met, "
    "reply with a JSON object on its own: "
    '{"status":"done"|"blocked","summary":"...","changed_files":[...],"checks":[...],'
    '"unresolved":[...]}. Otherwise continue working. '
    "NEVER report status done until every required artifact EXISTS ON DISK and your own "
    "verification commands have actually been run — a verifier will re-execute them and "
    "a claimed-but-unwritten file fails the task. If you cannot complete an artifact, "
    'report {"status":"blocked"} with the reason instead of claiming completion.'
)


class WorkerOutcome(dict):
    """Structured outcome: status, patch reference, changed files, checks, unresolved."""


def _tool_target(call: dict) -> str:
    """What a tool call acted on (a path or a command), short, for the progress stream —
    so "what did the agent do" reads `write_file index.html`, not just `write_file`."""
    raw = (call.get("function") or {}).get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "command", "cmd", "query"):
        val = args.get(key)
        if isinstance(val, list):
            val = " ".join(map(str, val))
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val if len(val) <= 120 else val[:119] + "…"
    return ""


class Worker:
    def __init__(self, provider: Provider, model: str, executor: Executor, store: StateStore,
                 config, profile, usage: UsageJournal | None = None, max_result_chars: int = MAX_RESULT_CHARS,
                 *, finalize: bool = True, run_id: str = "", progress=None):
        self.provider = provider
        self.model = model
        self.executor = executor
        self.store = store
        self.config = config
        self.profile = profile
        self.usage = usage
        self.max_result_chars = max_result_chars
        self.finalize = finalize  # False: the caller (pipeline) owns the final state change
        self.run_id = run_id
        self.progress = progress or (lambda *a, **k: None)

    # ---------------------------------------------------------------- tool defs
    def _tool_schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": name,
            "description": (spec.get("description") or "")[:500],
            "parameters": spec.get("inputSchema") or {"type": "object", "properties": {}}}}
            for name, spec in self.executor.registry.schemas().items()
            if self.executor.policy.allows(name, self.executor.registry)]

    # ---------------------------------------------------------------- main loop
    def run(self, assignment: dict, packet_text: str, checkpoint: dict | None = None) -> WorkerOutcome:
        task_id, fence = assignment["task_id"], assignment["fence"]
        max_turns = self.profile.max_model_turns_per_task if hasattr(self.profile, "max_model_turns_per_task") \
            else self.config.max_model_turns_per_task
        max_calls = self.profile.budgets.get("calls", 40) if hasattr(self.profile, "budgets") else 40
        timeout_s = float(getattr(self.config, "task_timeout_s", 900) or 900)
        messages: list[Message] = [Message("system", SYSTEM_PROMPT), Message("user", packet_text)]
        calls_made = 0
        counts = {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
                  "cost_usd": 0.0}
        started = time.time()
        # journal scope: a RESUMED attempt replays its own effects; a new attempt doesn't
        scope = (checkpoint or {}).get("effect_scope") or \
            f"{self.run_id}/{task_id}/attempt-{assignment.get('attempt', fence)}"

        if checkpoint:  # resume: replay the journaled tool results, not the effects
            for entry in checkpoint.get("history", []):
                messages.append(Message(entry["role"], entry["content"]))
            calls_made = checkpoint.get("calls_made", 0)

        def _ckpt(turn, **extra):
            self.store.checkpoint(task_id, fence, {
                "turn": turn, "calls_made": calls_made, "effect_scope": scope, "model": self.model,
                "history": [{"role": m.role, "content": m.content[:2000]} for m in messages[2:]],
                **extra})

        outcome: WorkerOutcome | None = None
        nudges = 0
        try:
            for turn in range(1, max_turns + 1):
                if time.time() - started > timeout_s:
                    outcome = WorkerOutcome({"status": "timeout", "task": task_id,
                                             "error": f"task exceeded its {timeout_s:g}s budget",
                                             "turns": turn - 1})
                    break
                if not self.store.heartbeat(task_id, assignment["owner"], fence):
                    return WorkerOutcome({"status": "stale-lease", "task": task_id,
                                          "note": "a newer attempt owns this task"})
                self.progress("turn", task=task_id, turn=turn)
                completion = self.provider.complete(
                    self.model, messages, tools=self._tool_schemas(),
                    max_output_tokens=self.profile.max_output_tokens)
                counts["model_calls"] += 1
                counts["input_tokens"] += completion.usage.input_tokens
                counts["output_tokens"] += completion.usage.output_tokens
                counts["cost_usd"] += completion.usage.cost_usd
                if self.usage:
                    self.usage.record(task_id, completion.model, completion.usage)
                # per-call usage in the live progress stream, so token totals (and the
                # dashboard) move while a task runs, not only once the report lands
                self.progress("usage", task=task_id, model=completion.model,
                              input_tokens=completion.usage.input_tokens,
                              output_tokens=completion.usage.output_tokens,
                              cost_usd=completion.usage.cost_usd)
                messages.append(Message("assistant", completion.content,
                                        tool_calls=list(completion.tool_calls)))
                if completion.tool_calls:
                    exhausted = False
                    for call in completion.tool_calls:
                        if calls_made >= max_calls:  # answer EVERY call id (wire format)
                            exhausted = True
                            messages.append(Message("tool", "error: tool-call budget exhausted",
                                                    tool_call_id=call.get("id")))
                            continue
                        calls_made += 1
                        counts["tool_calls"] += 1
                        name = (call.get("function") or {}).get("name", "?")
                        result = self.executor.execute(call, context={
                            "task_id": task_id, "run_id": self.run_id, "effect_scope": scope,
                            "workspace": getattr(self.executor.policy, "root", None)})
                        self.progress("tool", task=task_id, tool=name, ok=result.get("ok"),
                                      target=_tool_target(call))
                        messages.append(Message("tool", result["content"][:self.max_result_chars],
                                                tool_call_id=call.get("id")))
                    _ckpt(turn)
                    if exhausted:
                        outcome = WorkerOutcome({"status": "call-budget-exhausted", "task": task_id,
                                                 "calls": calls_made})
                        break
                    continue
                truncated = completion.finish_reason == "length"
                empty = not (completion.content or "").strip()
                if (truncated or empty) and nudges < MAX_NUDGES and turn < max_turns:
                    # the model was cut off (or said nothing): tell it, instead of ending the
                    # attempt on a reply it never meant as final — free models hit this when
                    # they try to emit a whole large file in one tool call
                    nudges += 1
                    if empty:
                        messages.pop()  # an empty assistant turn is noise (some APIs reject it)
                    limit = self.profile.max_output_tokens
                    messages.append(Message("user", (
                        f"Your last reply was cut off at the output limit (~{limit} tokens) and was "
                        "NOT applied — no file was written. Write large files in parts: write_file "
                        "with the first section, then append_file for each further section, keeping "
                        "every call well under the limit. Then reply with the outcome JSON.")
                        if truncated else
                        "Your last reply was empty. Continue the task: call a tool, or reply with "
                        "the outcome JSON object."))
                    self.progress("nudge", task=task_id, reason="truncated" if truncated else "empty")
                    continue
                outcome = self._parse_outcome(completion.content, task_id)
                if outcome.get("status") == "unstructured" and (truncated or empty):
                    outcome["error"] = (
                        f"model output was {'cut off at max_output_tokens' if truncated else 'empty'} "
                        f"{nudges + 1} time(s) — raise the profile's max_output_tokens or use a model "
                        "that writes large files in parts")
                break
            if outcome is None:  # ran out of turns without a final answer
                outcome = WorkerOutcome({"status": "turn-budget-exhausted", "task": task_id,
                                         "turns": max_turns, "calls": calls_made})
        except ProviderError as e:
            if e.kind in (ProviderError.AUTH, ProviderError.QUOTA, ProviderError.PAID_REFUSED):
                _ckpt(-1, error=e.message)  # escalate, never rotate credentials or spin
                outcome = WorkerOutcome({"status": f"provider-{e.kind}", "task": task_id,
                                         "error": e.message, "checkpointed": True})
            else:
                outcome = WorkerOutcome({"status": "provider-error", "task": task_id,
                                         "error": e.message, "kind": e.kind})
        outcome["duration_s"] = round(time.time() - started, 2)
        outcome["calls"] = calls_made
        outcome["usage"] = counts
        outcome["effect_scope"] = scope
        if self.finalize:
            if outcome.get("status") == "done":
                self.store.complete(task_id, fence, dict(outcome))
            else:
                self.store.fail(task_id, fence, str(outcome.get("error") or outcome["status"]),
                                blocked=str(outcome["status"]).startswith("provider-"))
        return outcome

    # ---------------------------------------------------------------- outcome
    @staticmethod
    def _parse_outcome(text: str, task_id: str) -> WorkerOutcome:
        """Extract the structured outcome JSON; tolerate surrounding prose."""
        try:
            start = text.index("{")
            depth, end = 0, -1
            for i, ch in enumerate(text[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end > 0:
                data = json.loads(text[start:end + 1])
                if isinstance(data, dict) and "status" in data:
                    return WorkerOutcome({**data, "task": task_id})
        except (ValueError, json.JSONDecodeError):
            pass
        # prose is NOT a completion claim the engine can accept — v0.17 counted it as done
        return WorkerOutcome({"status": "unstructured", "task": task_id, "summary": text[:2000],
                              "error": "final answer had no structured outcome JSON"})
