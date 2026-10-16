"""What Claude Code reports about each voice run, kept for fern's "Claude activity" panel.

Every field comes from the Claude Agent SDK's messages or from Claude Code's own hook input; nothing is inferred.
A run is one message from the user: its input, then a timeline in the order Claude produced it (text, thinking,
tool calls with input, output and status; a subagent's steps carry its Agent call as `parent`), and the final result
with Claude Code's own numbers (turns, duration, cost, usage). Finished runs are appended to a JSONL file, so history
survives restarts. Text longer than TEXT_LIMIT characters is cut.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

TEXT_LIMIT = 4000
HISTORY = 50  # runs kept in memory and served to the panel
ACTIVE = ("running", "waiting_for_you", "stopping")


def clip(value: Any) -> Any:
    """Cut long text, keeping a tool input's structure (the panel renders commands, paths and edits from it)."""
    if isinstance(value, str):
        return value if len(value) <= TEXT_LIMIT else value[:TEXT_LIMIT] + f"\n…[cut at {TEXT_LIMIT} characters]"
    if isinstance(value, dict):
        return {key: clip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clip(item) for item in value]
    return value


def tool_result_text(content: Any) -> str:
    """A tool result as Claude Code reported it: plain text, or its text parts joined."""
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return "" if content is None else str(content)


class RunLog:
    def __init__(self, path: Path):
        self.path = path
        self.runs: list[dict] = self._load()
        self.listeners: set[asyncio.Queue[dict]] = set()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()[-HISTORY:]
        return [json.loads(line) for line in lines if line.strip()]

    def _changed(self, run: dict) -> None:
        snapshot = json.loads(json.dumps(run))
        for listener in self.listeners:
            listener.put_nowait(snapshot)

    def subscribe(self) -> asyncio.Queue[dict]:
        listener: asyncio.Queue[dict] = asyncio.Queue()
        self.listeners.add(listener)
        return listener

    def unsubscribe(self, listener: asyncio.Queue[dict]) -> None:
        self.listeners.discard(listener)

    def newest_first(self) -> list[dict]:
        return list(reversed(self.runs))

    def start(self, prompt: str) -> dict:
        run = {
            "id": uuid.uuid4().hex,
            "input": clip(prompt),
            "status": "running",
            "started": time.time(),
            "finished": None,
            "timeline": [],
            "output": "",
        }
        self.runs = [*self.runs, run][-HISTORY:]
        self._changed(run)
        return run

    def update(self, run: dict, **reported: Any) -> None:
        run.update({key: value for key, value in reported.items() if value is not None})
        self._changed(run)

    def add_text(self, run: dict, kind: str, text: str, parent: str | None) -> None:
        """Claude's text or thinking, where it appeared in the run."""
        run["timeline"].append({"kind": kind, "text": clip(text), "parent": parent})
        self._changed(run)

    def add_tool(self, run: dict, tool_use_id: str, tool: str, tool_input: Any, parent: str | None) -> None:
        run["timeline"].append(
            {"kind": "tool", "id": tool_use_id, "tool": tool, "input": clip(tool_input), "status": "running",
             "output": "", "started": time.time(), "finished": None, "parent": parent}
        )  # fmt: skip
        self._changed(run)

    def _tool(self, run: dict, tool_use_id: str | None) -> dict | None:
        return next((entry for entry in run["timeline"] if entry.get("id") == tool_use_id), None)

    def tool_status(self, run: dict, tool_use_id: str | None, status: str) -> None:
        if tool := self._tool(run, tool_use_id):
            tool["status"] = status
            self._changed(run)

    def tool_result(self, run: dict, tool_use_id: str, content: Any, is_error: bool | None) -> None:
        if tool := self._tool(run, tool_use_id):
            tool.update(output=clip(tool_result_text(content)), finished=time.time())
            if tool["status"] != "denied":
                tool["status"] = "error" if is_error else "done"
            self._changed(run)

    def finish(self, run: dict, status: str, **reported: Any) -> None:
        if run["status"] not in ACTIVE:
            return
        if run["status"] == "stopping":
            status = "stopped"
        for tool in run["timeline"]:  # a tool still "running" when the run ends never got a result
            if tool.get("status") in ("running", "waiting_for_you"):
                tool["status"] = "stopped"
        self.update(run, status=status, finished=time.time(), **reported)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as runs_file:
            runs_file.write(json.dumps(run, ensure_ascii=False) + "\n")
