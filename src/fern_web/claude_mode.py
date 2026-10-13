"""Claude mode for a voice session: talk to Claude Code like in the VS Code chat, through the bridge on the Mac.

Claude's steps and replies stream to the screen; the voice speaks a short gist. When Claude needs permission or asks
a question, the user hears it and answers by voice ("yes", "option 2") or by tapping, like VS Code's prompts.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
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
SESSIONS_LISTED = 8
PULL_REQUEST_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


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
        self.working = False  # a Claude turn is running
        self.permission: dict | None = None  # what Claude is waiting for the user to approve
        self.question: dict | None = None  # Claude's open questions: {id, questions, answers}
        self.listed_sessions: list[dict] = []  # the last list shown, so "open session 2" means its second entry

    async def handle(self, name: str, call_id: str, arguments: dict) -> None:
        if name == "switch_backend":
            await self._switch(call_id, arguments.get("target", ""))
        elif not self.active:
            await self.session.send_tool_output(call_id, "Not talking to Claude. Call switch_backend to claude first.")
        elif name == "ask_claude" and self.question:
            await self.session.send_tool_output(call_id, await self._answer_question(arguments.get("request", "")))
        elif name == "ask_claude" and self.working:
            await self.session.send_tool_output(call_id, "Claude is still working. Say 'stop' to interrupt it.")
        elif name == "ask_claude":
            await self._turn(call_id, arguments.get("request", ""))
        else:
            await self.session.send_tool_output(call_id, await self._action(arguments))

    async def _action(self, arguments: dict) -> str:
        match arguments.get("action"):
            case "stop":
                await self._call_bridge(self.client.interrupt)
                return "Stopping Claude."
            case "new_chat":
                await self._call_bridge(self.client.new_chat)
                return "The next message starts a new session with Claude."
            case "list_sessions":
                return await self._list_sessions()
            case _:
                return await self._open_session(arguments.get("number") or 0)

    async def _list_sessions(self) -> str:
        try:
            self.listed_sessions = (await self.client.sessions())[:SESSIONS_LISTED]
        except Exception as error:
            return f"Couldn't list Claude's sessions ({_describe(error)}). Tell the user."
        lines = [
            f"{number}. {session['title']} ({time.strftime('%b %d %H:%M', time.localtime(session['updated']))})"
            + (" (current)" if session["current"] else "")
            for number, session in enumerate(self.listed_sessions, 1)
        ]
        await self.session.browser_events.put(
            {"type": "claude_text", "markdown": "**Claude sessions**\n" + "\n".join(lines)}
        )
        spoken_titles = " ".join(
            f"{number}: {session['title']}." for number, session in enumerate(self.listed_sessions[:3], 1)
        )
        return f"{len(lines)} sessions are on screen, newest first. Read the first few briefly: {spoken_titles}"

    async def _open_session(self, number: int) -> str:
        if not 1 <= number <= len(self.listed_sessions):
            return "Which session? List them first (claude_action list_sessions), then open one by its number."
        chosen = self.listed_sessions[number - 1]
        await self._call_bridge(lambda: self.client.open_session(chosen["id"]))
        return f"Opened '{chosen['title']}'. The next message continues it."

    async def _switch(self, call_id: str, target: str) -> None:
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
        self.active = True
        await self._apply("claude")
        await self.session.send_tool_output(call_id, "Switched to Claude Code. Say so in a few words.")

    async def _apply(self, mode: Mode) -> None:
        await self.session.update_instructions(instructions_for(mode, self.household_facts))
        await self.session.browser_events.put({"type": "mode", "mode": mode})

    async def _turn(self, call_id: str, text: str) -> None:
        """One Claude turn. The tool call stays open until Claude is done; questions and permission requests in
        between are put to the user as notices."""
        self.working = True
        final: dict | None = None
        try:
            async with self.session.handling("claude"):
                await self.session.browser_events.put({"type": "claude_turn", "text": text})
                async for event in self.client.send(text):
                    final = await self._on_event(event) or final
        except Exception as error:
            final = {"type": "error", "message": _describe(error), "reference": secrets.token_hex(3)}
        finally:
            self.working, self.permission, self.question = False, None, None
        if final is None or final["type"] == "error":
            failure = final or {"message": "no reply", "reference": secrets.token_hex(3)}
            log.warning("claude turn failed [ref %s]: %s", failure["reference"], failure["message"])
            await self.session.browser_events.put({"type": "error", **failure})
            await self.session.send_tool_output(
                call_id, f"Claude Code failed. Tell the user, with reference {failure['reference']}."
            )
            return
        for url in dict.fromkeys(PULL_REQUEST_LINK.findall(final["reply"])):
            await self.session.browser_events.put({"type": "claude_link", "label": "Pull request", "url": url})
        await self.session.send_tool_output(call_id, spoken(final["reply"]))

    async def _on_event(self, event: dict) -> dict | None:
        """Panel updates and prompts for the user; returns the turn's final event (done/error)."""
        match event["type"]:
            case "progress":
                await self.session.browser_events.put({"type": "claude_progress", "text": event["text"]})
            case "text":
                await self.session.browser_events.put({"type": "claude_text", "markdown": event["markdown"]})
            case "permission":
                self.permission = event
                self.session.awaiting_since = time.monotonic()  # only a "yes" spoken after this question counts
                card = {"type": "claude_permission", "id": event["id"], "detail": event["detail"]}
                await self.session.browser_events.put(card)
                await self.session.say(f"Notice: Claude wants permission for {event['detail']}. Ask the user: approve?")
            case "question":
                self.question = {"id": event["id"], "questions": event["questions"], "answers": {}}
                await self._put_question()
            case "done" | "error":
                return event
        return None

    async def _put_question(self) -> None:
        assert self.question is not None
        current = self.question["questions"][len(self.question["answers"])]
        await self.session.browser_events.put(
            {
                "type": "claude_question",
                "text": current["question"],
                "options": current["options"],
                "descriptions": current.get("descriptions", []),
            }
        )
        await self.session.say(f"Notice: Claude asks: {current['question']} {spoken_options(current['options'])}")

    async def _answer_question(self, text: str) -> str:
        """The user's answer to Claude's current question: 'option 2' becomes its label, anything else goes as is."""
        assert self.question is not None
        question = self.question
        current = question["questions"][len(question["answers"])]
        question["answers"][current["question"]] = chosen_label(text, current["options"]) or text.strip()
        if len(question["answers"]) < len(question["questions"]):
            await self._put_question()
            return "Noted. Ask the next question."
        self.question = None
        await self._call_bridge(lambda: self.client.answer(question["id"], question["answers"]))
        await self.session.browser_events.put({"type": "claude_question_answered"})
        return "Answer sent to Claude."

    async def decide(self, approve: bool, always: bool = False) -> str:
        """The user's answer to Claude's permission request (spoken or tapped). Returns what to tell the voice."""
        if self.permission is None:
            return "Claude isn't waiting for permission."
        permission, self.permission = self.permission, None
        decision = "always" if approve and always else "allow" if approve else "deny"
        await self._call_bridge(lambda: self.client.decide(permission["id"], decision))
        await self.session.browser_events.put({"type": "claude_permission_resolved", "decision": decision})
        return "Approved. Claude continues." if approve else "Rejected. Claude will adjust."

    async def answer_from_screen(self, text: str) -> None:
        """A tapped option: same as saying it."""
        if self.question:
            await self.session.say(f"Notice: the user answered on screen. {await self._answer_question(text)}")

    async def decide_from_screen(self, decision: str) -> None:
        reply = await self.decide(decision != "deny", always=decision == "always")
        await self.session.say(f"Notice: the user answered on screen. {reply}")

    async def end(self) -> None:
        """The voice call ended: nobody can answer Claude's open prompt anymore, so stop that turn."""
        if self.permission or self.question:
            await self._call_bridge(self.client.interrupt)

    async def _call_bridge(self, request: Callable[[], Awaitable[None]]) -> None:
        try:
            await request()
        except Exception as error:
            reference = secrets.token_hex(3)
            log.warning("claude bridge call failed [ref %s]: %s", reference, _describe(error))
            await self.session.browser_events.put(
                {"type": "error", "message": _describe(error), "reference": reference}
            )


def _describe(error: Exception) -> str:
    return str(error)[:120] if isinstance(error, ClaudeUnavailableError) else type(error).__name__


def chosen_label(text: str, options: list[str]) -> str | None:
    """'option 2' / 'the second one' -> that option's label."""
    if not (match := OPTION_ANSWER.match(text)):
        return None
    token = match.group(1).lower()
    index = int(token) if token.isdigit() else OPTION_WORDS[token]
    return options[index - 1] if 1 <= index <= len(options) else None


def spoken_options(labels: list[str]) -> str:
    if all(len(label) <= SPOKEN_OPTION_CHARS for label in labels):
        return " ".join(f"Option {number}: {label}." for number, label in enumerate(labels, 1))
    return f"{len(labels)} options are on screen; answer with a number."


def spoken(reply: str) -> str:
    """For the voice to rephrase (see CLAUDE_MODE): a short gist; the panel has everything."""
    first_paragraph = reply.strip().split("\n\n", 1)[0]
    gist = summary(first_paragraph) or "Done."
    more = "\nMore detail is on screen." if gist != reply.strip() else ""
    return f"Claude's answer: {gist}{more}"


def summary(text: str) -> str:
    """Whole sentences up to SPOKEN_CHARS (the full text is on screen)."""
    if len(text) <= SPOKEN_CHARS:
        return text
    head = text[:SPOKEN_CHARS]
    sentence_end = max(head.rfind(". "), head.rfind("? "), head.rfind("! "))
    return head[: sentence_end + 1] if sentence_end > 0 else head.rsplit(" ", 1)[0] + "…"
