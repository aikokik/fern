"""Claude mode for a voice session: the user's words go to Claude Code (through the builder bridge) instead of
fern's brain. Only Claude's short `say` text is spoken; plans and details go to the screen as markdown, and its
question shows as tappable options (or answer by voice: "option 2", "the first one").
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fern_web.claude_client import ClaudeClient, ClaudeUnavailableError
from fern_web.realtime import Mode, instructions_for

if TYPE_CHECKING:
    from fern_web.sideband import VoiceSession

log = logging.getLogger("fern_web")
SPOKEN_CHARS = 240  # voice tokens are the costly part: speak a summary, the screen gets everything
SPOKEN_OPTION_CHARS = 40
CLAUDE_TOOLS = {"switch_backend", "ask_claude", "claude_action"}
OPTION_WORDS = {"one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3, "four": 4, "fourth": 4}
OPTION_ANSWER = re.compile(
    r"^\s*(?:option|number|the)?\s*(\d|one|two|three|four|first|second|third|fourth)\b(?:\s+(?:one|option))?\s*[.!]?\s*$",
    re.I,
)


class ClaudeMode:
    def __init__(
        self,
        session: VoiceSession,
        client: ClaudeClient,
        device: str,
        allowed_devices: tuple[str, ...],
        household_facts: str,
    ):
        self.session = session
        self.client = client
        self.device = device
        self.allowed_devices = allowed_devices
        self.household_facts = household_facts
        self.active = False
        self.topic = ""
        self.last_question: dict | None = None
        self._lock = asyncio.Lock()  # one Claude request at a time; topics are conversations, not job queues

    async def handle(self, name: str, call_id: str, arguments: dict) -> None:
        if name == "switch_backend":
            await self._switch(call_id, arguments.get("target", ""), (arguments.get("topic") or "").strip())
        elif not self.active:
            await self.session.send_tool_output(call_id, "Not talking to Claude. Call switch_backend to claude first.")
        elif name == "ask_claude":
            text = self._resolve_option(arguments.get("request", ""))
            await self._reply_to_tool(call_id, lambda: self.client.turn(self.topic, text))
        elif arguments.get("action") == "build":
            await self._reply_to_tool(call_id, lambda: self.client.build(self.topic))
        elif arguments.get("action") == "ship":
            title = (arguments.get("title") or "").strip() or f"{self.topic}: built with Claude Code by voice"
            await self._reply_to_tool(call_id, lambda: self.client.ship(self.topic, title))
        else:
            await self._reply_to_tool(call_id, self._topics)

    async def answer_from_screen(self, text: str) -> None:
        """A tapped option or typed answer: same as speaking it, but the reply is injected (no tool call to answer)."""
        reply = await self._request(lambda: self.client.turn(self.topic, self._resolve_option(text)))
        if reply is not None:
            await self.session.say(f"Rephrase Claude's reply as instructed:\n{self._spoken(reply)}")

    async def _switch(self, call_id: str, target: str, topic: str) -> None:
        if target == "fern":
            self.active = False
            await self._apply("fern")
            await self.session.send_tool_output(call_id, "Switched back to fern. Say so in a few words.")
            return
        if not self.client.configured:
            await self.session.send_tool_output(call_id, "Claude Code isn't set up yet. Tell the user.")
            return
        if self.allowed_devices and self.device not in self.allowed_devices:
            await self.session.send_tool_output(call_id, "This device isn't allowed to use Claude Code. Tell the user.")
            return
        topic = topic or self.topic
        if not topic:
            await self.session.send_tool_output(
                call_id, "Ask the user for a short topic name (like 'weather tool'), then call switch_backend again."
            )
            return
        self.active, self.topic, self.last_question = True, topic, None
        await self._apply("claude")
        await self.session.send_tool_output(call_id, f"Switched to Claude Code, topic {topic}. Say so in a few words.")

    async def _apply(self, mode: Mode) -> None:
        await self.session.update_instructions(instructions_for(mode, self.topic, self.household_facts))
        await self.session.browser_events.put(
            {"type": "mode", "mode": mode, "topic": self.topic if mode == "claude" else ""}
        )

    def _resolve_option(self, text: str) -> str:
        """'option 2' / 'the second one' -> the option's label, so Claude gets a real answer."""
        question, match = self.last_question, OPTION_ANSWER.match(text)
        if not (question and match):
            return text
        token = match.group(1).lower()
        index = int(token) if token.isdigit() else OPTION_WORDS[token]
        if not 1 <= index <= len(question["options"]):
            return text
        self.last_question = None
        return f'My answer to "{question["text"]}": {question["options"][index - 1]}'

    async def _topics(self) -> dict:
        topics = await self.client.topics()
        lines = [
            f"- **{topic['slug']}**: {topic['status']}" + (f" · {topic['pr_url']}" if topic.get("pr_url") else "")
            for topic in topics
        ]
        say = f"{len(topics)} topics, they're on screen." if topics else "No topics yet."
        return {"say": say, "show": "\n".join(lines), "question": None}

    async def _reply_to_tool(self, call_id: str, request: Callable[[], Awaitable[dict]]) -> None:
        reply = await self._request(request)
        if reply is None:
            await self.session.send_tool_output(
                call_id, "Claude Code failed. Tell the user to check the reference on screen."
            )
            return
        await self.session.send_tool_output(call_id, self._spoken(reply))

    async def _request(self, request: Callable[[], Awaitable[dict]]) -> dict | None:
        if self._lock.locked():  # e.g. a build that takes minutes: answer now instead of queueing
            return {"say": "Claude is still working on the previous request. Try again when it's done."}
        try:
            async with self._lock, self.session.handling("claude"):
                reply = await request()
        except Exception as error:
            reference = secrets.token_hex(3)
            log.warning("claude request failed [ref %s]: %s", reference, type(error).__name__)
            message = str(error)[:120] if isinstance(error, ClaudeUnavailableError) else type(error).__name__
            await self.session.browser_events.put({"type": "error", "message": message, "reference": reference})
            return None
        await self._publish(reply)
        return reply

    async def _publish(self, reply: dict) -> None:
        """One panel update per reply: new markdown (if any) and the current question (None clears the old one)."""
        self.last_question = reply.get("question") or None
        panel_update = {
            "type": "claude_reply",
            "say": reply.get("say") or "",
            "markdown": reply.get("show") or "",
            "question": self.last_question,
        }
        await self.session.browser_events.put(panel_update)
        if reply.get("pr_url"):
            await self.session.browser_events.put(
                {"type": "claude_link", "label": "Pull request", "url": reply["pr_url"]}
            )

    def _spoken(self, reply: dict) -> str:
        """For the voice to rephrase (see CLAUDE_MODE): a short gist and the question; the panel has everything."""
        if reply.get("status") == "shipped":
            return "Claude shipped it. The pull request link is on screen."
        say = reply.get("say") or ""
        gist = summary(say) or "Done."
        lines = [f"Claude's answer: {gist}"]
        if gist != say or reply.get("show"):
            lines.append("More detail is on screen.")
        if question := reply.get("question"):
            labels = question["options"]
            if all(len(label) <= SPOKEN_OPTION_CHARS for label in labels):
                options = " ".join(f"Option {number}: {label}." for number, label in enumerate(labels, 1))
            else:
                options = f"{len(labels)} options are on screen; answer with a number."
            lines.append(f"Question: {summary(question['text'])} {options}")
        return "\n".join(lines)


def summary(text: str) -> str:
    """Whole sentences up to SPOKEN_CHARS (the full text is on screen)."""
    if len(text) <= SPOKEN_CHARS:
        return text
    head = text[:SPOKEN_CHARS]
    sentence_end = max(head.rfind(". "), head.rfind("? "), head.rfind("! "))
    return head[: sentence_end + 1] if sentence_end > 0 else head.rsplit(" ", 1)[0] + "…"
