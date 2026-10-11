"""The tool-calling loop.

The model answers every step with one JSON object (enforced by a JSON-schema
grammar in llama-server), naming a tool and its input. Tool results go back
to the model as observations until it calls ``final_answer``.

Commands that the safety classifier marks as destructive are not run.
Instead the loop pauses with status ``needs_confirmation`` and resumes when
``resolve()`` is called with the human's decision. This works the same for a
terminal prompt and for the HTTP API.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import safety
from .config import Config
from .llm import LLMError, LlamaClient
from .tools import ShellTool, clip

TOOLS = ["shell", "search_docs", "list_devices", "remote_exec", "final_answer"]

SYSTEM_PROMPT = """You are PiAgent, a careful assistant running on a Raspberry Pi 5 (Raspberry Pi OS Lite, 64-bit, Bookworm).
Reply with exactly one JSON object per message: {"thought": "...", "tool": "...", "input": "...", "device": "..."}.

Tools:
- shell: run one bash command on this Pi as the unprivileged user "piagent" (no sudo). input = the command line.
- search_docs: semantic search over the local documents in /srv/docs. input = what to look for.
- list_devices: list the enrolled remote devices and whether they are online. input = "".
- remote_exec: run one command on an enrolled remote device. device = its name, input = the command. Shell features (pipes, redirection, variables) are not available there, and the device only runs commands its own policy allows.
- final_answer: reply to the user and stop. input = the answer.

Rules:
- Use one tool per message, then wait for the observation.
- Prefer read-only commands. Non-interactive commands only: no editors or pagers; limit long output (for example with | head -n 50).
- Commands that delete, overwrite, kill, install or reconfigure are shown to the user for approval automatically. Just propose the command; do not ask for permission in final_answer.
- If a command is refused or rejected, do not try to get around it. Explain what happened in final_answer.
- For questions about local documents use search_docs and name the files you used.
- Keep "thought" to one short sentence. Be concise and factual in final_answer."""


def action_schema(tools: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "maxLength": 300},
            "tool": {"type": "string", "enum": tools},
            "input": {"type": "string"},
            "device": {"type": "string"},
        },
        "required": ["thought", "tool", "input"],
        "additionalProperties": False,
    }


@dataclass
class Pending:
    id: str
    tool: str
    input: str
    device: str | None
    level: str
    reasons: list[str]
    thought: str = ""


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict] = field(default_factory=list)
    pending: Pending | None = None
    turn_start: int = 0
    steps: int = 0
    rejected: set = field(default_factory=set)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def summary(self) -> dict:
        return {
            "session_id": self.id,
            "created": self.created,
            "updated": self.updated,
            "pending": asdict(self.pending) if self.pending else None,
            "messages": self.messages,
        }


def parse_action(text: str) -> dict:
    """Parse the model's JSON action, tolerating stray text around it."""
    try:
        obj = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {"thought": "", "tool": "final_answer", "input": text.strip()}
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            return {"thought": "", "tool": "final_answer", "input": text.strip()}
    if not isinstance(obj, dict):
        return {"thought": "", "tool": "final_answer", "input": str(obj)}
    obj.setdefault("thought", "")
    obj.setdefault("input", "")
    obj["tool"] = obj.get("tool") if obj.get("tool") in TOOLS else "final_answer"
    for key in ("thought", "input", "device"):
        if key in obj and not isinstance(obj[key], str):
            obj[key] = json.dumps(obj[key])
    return obj


class Agent:
    def __init__(self, cfg: Config, llm: LlamaClient, docs=None, devices=None, shell: ShellTool | None = None):
        self.cfg = cfg
        self.llm = llm
        self.docs = docs
        self.devices = devices
        self.shell = shell or ShellTool(cfg.agent)

    # -- prompt -----------------------------------------------------------
    def _prompt(self, session: Session) -> list[dict]:
        budget = self.cfg.agent.context_chars - len(SYSTEM_PROMPT)
        msgs = session.messages
        selected: list[int] = []
        used = 0
        for idx in range(len(msgs) - 1, -1, -1):
            size = len(msgs[idx]["content"]) + 16
            if selected and used + size > budget:
                break
            selected.append(idx)
            used += size
        selected.reverse()
        if session.turn_start < len(msgs) and session.turn_start not in selected:
            # Always keep the user's current request, dropping older context instead.
            selected = [session.turn_start] + [i for i in selected if i > session.turn_start]
        return [{"role": "system", "content": SYSTEM_PROMPT}] + [msgs[i] for i in selected]

    def _next_action(self, session: Session, tools: list[str] = TOOLS) -> dict:
        reply = self.llm.chat(self._prompt(session), json_schema=action_schema(tools))
        action = parse_action(reply)
        if action["tool"] not in tools:
            action["tool"] = "final_answer"
        session.messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        return action

    def _observe(self, session: Session, tool: str, text: str) -> None:
        session.messages.append({
            "role": "user",
            "content": f"Observation from {tool}:\n{text}\n\nReply with the next JSON action.",
        })

    # -- tools --------------------------------------------------------------
    def _search_docs(self, query: str) -> str:
        if self.docs is None:
            return "Document search is not available."
        hits = self.docs.search(query)
        if not hits:
            return "No matching documents (is /srv/docs empty or not indexed yet?)."
        lines = []
        budget = self.cfg.agent.max_output_chars
        for i, hit in enumerate(hits, 1):
            text, _ = clip(hit["text"], max(200, budget // len(hits)))
            lines.append(f"[{i}] {hit['path']} (similarity {hit['score']}):\n{text}")
        return "\n\n".join(lines)

    def _list_devices(self) -> str:
        if self.devices is None:
            return "Remote devices are not available."
        devs = self.devices.list()
        if not devs:
            return "No devices are enrolled."
        return "\n".join(
            f"- {d['name']}: {'online' if d['online'] else 'offline'}, platform={d['platform'] or 'unknown'}, "
            f"host={d['hostname'] or 'unknown'}, local policy: {d['policy'] or 'unknown'}"
            for d in devs
        )

    def _remote_exec(self, device: str, command: str) -> tuple[str, dict]:
        cid = self.devices.enqueue(device, command, timeout=self.cfg.agent.command_timeout, requested_by="agent")
        result = self.devices.wait_result(cid, self.cfg.devices.result_timeout) or {}
        status = result.get("status", "unknown")
        if status in ("queued", "sent"):
            text = f"No result from {device} within {self.cfg.devices.result_timeout}s (status: {status}). Is the client running?"
        elif status == "done":
            out, _ = clip(result.get("stdout") or "", self.cfg.agent.max_output_chars)
            err, _ = clip(result.get("stderr") or "", self.cfg.agent.max_output_chars // 2)
            text = f"exit_code: {result.get('exit_code')}\nstdout:\n{out or '(none)'}" + (f"\nstderr:\n{err}" if err else "")
        else:
            text = f"Command {status} on {device}: {result.get('reason') or 'no reason given'}"
        return text, {"command_id": cid, "status": status, "exit_code": result.get("exit_code")}

    def _execute(self, session: Session, tool: str, arg: str, device: str | None, events: list) -> None:
        if tool == "shell":
            res = self.shell.run(arg)
            events.append({"type": "tool_result", "tool": tool, "input": arg, "exit_code": res.exit_code,
                           "output": res.observation()})
            self._observe(session, tool, res.observation())
        elif tool == "remote_exec":
            text, meta = self._remote_exec(device, arg)
            events.append({"type": "tool_result", "tool": tool, "device": device, "input": arg, "output": text, **meta})
            self._observe(session, f"remote_exec on {device}", text)

    # -- main loop ------------------------------------------------------------
    def run_turn(self, session: Session, message: str) -> dict:
        if session.pending:
            raise RuntimeError("session is waiting for a confirmation")
        session.messages.append({"role": "user", "content": message})
        session.turn_start = len(session.messages) - 1
        session.steps = 0
        return self._loop(session, [])

    def resolve(self, session: Session, approve: bool) -> dict:
        pending = session.pending
        if pending is None:
            raise RuntimeError("nothing is waiting for confirmation")
        session.pending = None
        events: list = [{"type": "confirmation", "approved": approve, "tool": pending.tool,
                         "input": pending.input, "device": pending.device}]
        if approve:
            self._execute(session, pending.tool, pending.input, pending.device, events)
        else:
            session.rejected.add((pending.tool, pending.device, pending.input))
            self._observe(session, pending.tool, "The user REJECTED this command. It was not run. Do not retry it.")
        return self._loop(session, events)

    def _loop(self, session: Session, events: list) -> dict:
        session.updated = time.time()
        try:
            while True:
                if session.steps >= self.cfg.agent.max_steps:
                    self._observe(session, "system", "Step limit reached. Give your final_answer now.")
                    action = self._next_action(session, ["final_answer"])
                else:
                    action = self._next_action(session)
                session.steps += 1
                tool, arg, device = action["tool"], action["input"].strip(), (action.get("device") or "").strip() or None
                if tool == "final_answer":
                    events.append({"type": "answer", "text": arg})
                    return self._result(session, "done", events, answer=arg)
                events.append({"type": "tool_call", "tool": tool, "input": arg, "device": device, "thought": action["thought"]})

                if tool == "search_docs":
                    text = self._search_docs(arg)
                    events.append({"type": "tool_result", "tool": tool, "input": arg, "output": text})
                    self._observe(session, tool, text)
                    continue
                if tool == "list_devices":
                    text = self._list_devices()
                    events.append({"type": "tool_result", "tool": tool, "output": text})
                    self._observe(session, tool, text)
                    continue
                if tool == "remote_exec":
                    if self.devices is None:
                        self._observe(session, tool, "Remote devices are not available.")
                        continue
                    if not device or not self.devices.exists(device):
                        self._observe(session, tool, f"Unknown device {device!r}. Use list_devices to see enrolled devices.")
                        continue

                # shell / remote_exec: vet the command first.
                if not arg:
                    self._observe(session, tool, "Empty command.")
                    continue
                if (tool, device, arg) in session.rejected:
                    self._observe(session, tool, "The user already rejected this exact command. Do not retry it.")
                    continue
                verdict = safety.classify(
                    arg, self.cfg.agent.extra_confirm_patterns, self.cfg.agent.extra_deny_patterns
                )
                if verdict.level == safety.DENY:
                    reason = "; ".join(verdict.reasons)
                    events.append({"type": "refused", "tool": tool, "input": arg, "device": device, "reasons": verdict.reasons})
                    self._observe(session, tool, f"REFUSED by policy, not run: {reason}. Do not try to work around this.")
                    continue
                if verdict.level == safety.CONFIRM:
                    session.pending = Pending(uuid.uuid4().hex[:12], tool, arg, device, verdict.level,
                                              verdict.reasons, action["thought"])
                    return self._result(session, "needs_confirmation", events)
                self._execute(session, tool, arg, device, events)
        except LLMError as exc:
            events.append({"type": "error", "text": str(exc)})
            return self._result(session, "error", events, error=f"language model unavailable: {exc}")

    def _result(self, session: Session, status: str, events: list, answer: str | None = None, error: str | None = None) -> dict:
        session.updated = time.time()
        return {
            "session_id": session.id,
            "status": status,
            "answer": answer,
            "error": error,
            "pending": asdict(session.pending) if session.pending else None,
            "events": events,
        }

    def run_interactive(self, session: Session, message: str, confirm) -> dict:
        """Run a turn to completion, calling ``confirm(pending) -> bool`` as needed."""
        result = self.run_turn(session, message)
        while result["status"] == "needs_confirmation":
            result = self.resolve(session, bool(confirm(session.pending)))
        return result
