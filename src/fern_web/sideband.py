"""Server-side channel into a live Realtime session (same session as the browser's WebRTC call, joined by call_id).

The browser carries audio only. Tool calls, approvals, live notices, idle/cost limits and transcripts run here.
"""

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fern_web.brain_client import BrainClient, BrainError

log = logging.getLogger("fern_web")
GOODBYE_SECONDS = 3  # let the spoken goodbye finish before hanging up
TRANSCRIPT_WAIT_SECONDS = 3  # transcription of the user's "yes" can land just after the model's tool call
SPOKEN_YES = re.compile(r"\b(yes|yeah|yep|yup|approved?|confirm(ed)?|go ahead|do it|send it|sure|okay|ok)\b", re.I)
SPOKEN_NO = re.compile(r"\b(no|nope|don'?t|do not|cancel|reject(ed)?|stop|wait)\b", re.I)


class VoiceSession:
    def __init__(
        self,
        session_id: str,
        device_token: str,
        call_id: str,
        brain: BrainClient,
        connect: Callable[[str], AbstractAsyncContextManager[Any]],
        hangup: Callable[[str], Any],
        max_seconds: float,
        idle_seconds: float,
        notice_poll_seconds: float,
        model_name: str = "realtime",
        transcribe_model: str = "transcribe",
    ):
        self.session_id = session_id
        self.device_token = device_token
        self.call_id = call_id
        self.brain = brain
        self._connect = connect
        self._hangup = hangup
        self.max_seconds = max_seconds
        self.idle_seconds = idle_seconds
        self.notice_poll_seconds = notice_poll_seconds
        self.browser_events: asyncio.Queue[dict] = asyncio.Queue()  # streamed to the browser (SSE)
        self.transcript: list[dict] = []
        counters = {"requests": 0, "input": 0, "cached": 0, "output": 0, "audio_input": 0, "audio_cached": 0}
        self.voice_usage = {"kind": "voice", "model": model_name, **counters, "audio_output": 0, "max_input": 0}
        self.transcription_usage = {"kind": "transcription", "model": transcribe_model, **counters}
        self.started_at = self.last_activity_at = time.monotonic()
        self.model_is_responding = False
        self.connection: Any = None
        self.closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()  # strong refs: asyncio only keeps weak ones
        self._brain_lock = asyncio.Lock()  # one brain request at a time per session keeps its history ordered
        self.awaiting_approval: list[dict] = []  # risky actions the brain paused on
        self.awaiting_since = 0.0
        self.last_user_words = ""
        self.user_spoke_at = 0.0
        self._user_spoke = asyncio.Event()
        self.task: asyncio.Task | None = None  # set by whoever starts run()

    async def run(self) -> None:
        background_tasks: list[asyncio.Task] = []
        try:
            async with self._connect(self.call_id) as connection:
                self.connection = connection
                background_tasks = [
                    asyncio.create_task(self._enforce_limits()),
                    asyncio.create_task(self._relay_notices()),
                ]
                async for event in connection:
                    await self._handle_event(event)
                    if self.closed.is_set():
                        break
        except Exception:
            if not self.closed.is_set():  # a closed socket after our own hangup is expected
                log.exception("voice session %s failed", self.session_id)
        finally:
            for task in background_tasks:
                task.cancel()
            await self._finish()

    async def _handle_event(self, event: Any) -> None:
        event_type = event.type
        if event_type == "input_audio_buffer.speech_started":
            self.last_activity_at = time.monotonic()
        elif event_type == "conversation.item.input_audio_transcription.completed":
            self.last_activity_at = self.user_spoke_at = time.monotonic()
            self.last_user_words = event.transcript
            self._user_spoke.set()
            self._add_transcription_usage(getattr(event, "usage", None))
            self.transcript.append({"role": "user", "text": event.transcript})
        elif event_type == "response.output_audio_transcript.done":
            self.transcript.append({"role": "assistant", "text": event.transcript})
        elif event_type == "response.created":
            self.model_is_responding = True
        elif event_type == "response.done":
            self.model_is_responding = False
            self.last_activity_at = time.monotonic()
            self._add_usage(event.response)
            self._start_tool_calls(event.response)
        elif event_type == "error":
            error = getattr(event, "error", None)  # type/code only: messages can echo conversation content
            log.warning(
                "realtime error in %s: %s %s",
                self.session_id,
                getattr(error, "type", "unknown"),
                getattr(error, "code", ""),
            )

    def _add_usage(self, response: Any) -> None:
        """Voice is the most expensive model per minute: count every response, audio split from text."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_details = getattr(usage, "input_token_details", None)
        cached_details = getattr(input_details, "cached_tokens_details", None)
        output_details = getattr(usage, "output_token_details", None)
        voice = self.voice_usage
        voice["requests"] += 1
        voice["input"] += usage.input_tokens or 0
        voice["cached"] += getattr(input_details, "cached_tokens", 0) or 0
        voice["output"] += usage.output_tokens or 0
        voice["audio_input"] += getattr(input_details, "audio_tokens", 0) or 0
        voice["audio_cached"] += getattr(cached_details, "audio_tokens", 0) or 0
        voice["audio_output"] += getattr(output_details, "audio_tokens", 0) or 0
        voice["max_input"] = max(voice["max_input"], usage.input_tokens or 0)

    def _add_transcription_usage(self, usage: Any) -> None:
        if usage is None:
            return
        transcription = self.transcription_usage
        transcription["requests"] += 1
        if getattr(usage, "type", "") == "duration":  # billed by time: ~600 audio tokens per minute
            transcription["input"] += round(usage.seconds * 10)
            transcription["audio_input"] += round(usage.seconds * 10)
            return
        details = getattr(usage, "input_token_details", None)
        transcription["input"] += usage.input_tokens or 0
        transcription["output"] += usage.output_tokens or 0
        transcription["audio_input"] += getattr(details, "audio_tokens", 0) or 0

    def _start_tool_calls(self, response: Any) -> None:
        """Run tool calls only from responses that completed. When the user talks over the model, the response
        is cancelled and any half-generated call is dropped; the model re-plans with what the user just said."""
        function_calls = [item for item in response.output or [] if item.type == "function_call"]
        if response.status != "completed":
            if function_calls:
                log.info("dropped %d tool call(s) from a %s response", len(function_calls), response.status)
            return
        for call in function_calls:
            # Background task so session events keep flowing while the brain works.
            task = asyncio.create_task(self._run_tool(call.name, call.call_id, call.arguments))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool(self, name: str, call_id: str, arguments: str) -> None:
        try:
            parsed_arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            log.warning("%s got malformed arguments (%d chars)", name, len(arguments or ""))  # not the user's words
            await self._send_tool_output(call_id, "The request was cut off. Ask the user to repeat it.")
            return
        if name == "ask_fern":
            await self._ask_fern(call_id, parsed_arguments.get("request", ""))
        elif name == "decide_approval":
            await self._decide_approval(call_id, bool(parsed_arguments.get("approve")))
        elif name == "end_conversation":
            # No new response: the model already said its goodbye in the turn that called this tool.
            await self._send_tool_output(call_id, "ok", respond=False)
            await asyncio.sleep(GOODBYE_SECONDS)
            self.closed.set()
            await self.close()
        else:
            await self._send_tool_output(call_id, f"unknown tool {name}")

    async def _ask_fern(self, call_id: str, request: str) -> None:
        try:
            async with self._brain_lock:
                brain_response = await self.brain.chat(self.device_token, request)
        except Exception as error:
            reference = secrets.token_hex(3)
            log.exception("ask_fern failed [ref %s]", reference)
            await self.browser_events.put({"type": "error", "message": type(error).__name__, "reference": reference})
            await self._send_tool_output(
                call_id,
                f"Failed: the request did not reach fern ({type(error).__name__}). Tell the user something went "
                f"wrong and to check the fern logs for reference {reference}.",
            )
            return
        log.info("ask_fern: tools=%s pending=%d", brain_response.get("tools"), len(brain_response.get("pending", [])))
        await self.publish(brain_response)
        await self._send_tool_output(call_id, self._describe(brain_response))

    async def _decide_approval(self, call_id: str, approve: bool) -> None:
        if not self.awaiting_approval:
            await self._send_tool_output(call_id, "Nothing is waiting for approval.")
            return
        if approve and not await self._user_said_yes():
            log.info("voice approval refused: no clear spoken yes")
            await self._send_tool_output(call_id, "Not approved: the user must say yes out loud. Ask them again.")
            return
        replies = []
        for pending in self.awaiting_approval:
            try:
                brain_response = await self.brain.approve(self.device_token, pending["id"], approve)
            except BrainError as error:
                replies.append("That approval expired." if error.status == 404 else "Approval failed.")
                continue
            await self.publish(brain_response)
            replies.append(self._describe(brain_response))
        self.awaiting_approval = []
        await self.browser_events.put({"type": "resolved", "approved": approve})
        verdict = "Approved." if approve else "Rejected."
        await self._send_tool_output(call_id, f"{verdict} {' '.join(replies)}")

    async def _user_said_yes(self) -> bool:
        """Approval is tied to the user's real transcribed speech after the question, never the model's say-so,
        so text inside a tool result can't approve anything."""
        self._user_spoke.clear()
        if self.user_spoke_at <= self.awaiting_since:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._user_spoke.wait(), TRANSCRIPT_WAIT_SECONDS)
        if self.user_spoke_at <= self.awaiting_since:
            return False
        return bool(SPOKEN_YES.search(self.last_user_words)) and not SPOKEN_NO.search(self.last_user_words)

    def _describe(self, brain_response: dict) -> str:
        """Tool output for the voice model: short, and for paused actions, what needs a yes/no."""
        pending = brain_response.get("pending", [])
        if not pending:
            return brain_response.get("reply") or ""
        actions = "; ".join(f"{action['tool']} {action['args'][:300]}" for action in pending)
        return f"Needs approval: {actions}. Say in one sentence what will happen, then ask: approve?"

    async def publish(self, brain_response: dict) -> None:
        """Brain side effects: device commands and approval cards for the browser; remember paused actions."""
        for command in brain_response.get("commands", []):
            await self.browser_events.put({"type": "command", "command": command})
        for pending in brain_response.get("pending", []):
            self.awaiting_approval.append(pending)
            self.awaiting_since = time.monotonic()
            await self.browser_events.put({"type": "pending", **pending})

    def resolved_on_screen(self, pending_id: str) -> None:
        self.awaiting_approval = [pending for pending in self.awaiting_approval if pending["id"] != pending_id]

    async def _wait_for_pause(self) -> bool:
        """Results arrive asynchronously; never cut into what the model is saying. False if the session ended."""
        for _attempt in range(150):  # up to ~30 s
            if not self.model_is_responding or self.closed.is_set():
                break
            await asyncio.sleep(0.2)
        return self.connection is not None and not self.closed.is_set()

    async def say(self, text: str) -> None:
        """Inject a system message and let the model voice it, waiting for a pause first."""
        if not await self._wait_for_pause():
            return
        await self.connection.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self.connection.send({"type": "response.create"})

    async def _send_tool_output(self, call_id: str, output: str, respond: bool = True) -> None:
        if not await self._wait_for_pause():
            return
        await self.connection.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        if respond:
            await self.connection.send({"type": "response.create"})

    async def _enforce_limits(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            over_budget = now - self.started_at > self.max_seconds
            idle_too_long = now - self.last_activity_at > self.idle_seconds
            if over_budget or idle_too_long:
                self.closed.set()
                await self.close()

    async def _relay_notices(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(self.notice_poll_seconds)
            try:
                for notice in await self.brain.notices(self.device_token):
                    await self.say(f"Notice: {notice}")
            except Exception:
                log.warning("notice poll failed", exc_info=True)

    async def close(self) -> None:
        try:
            await self._hangup(self.call_id)
        except Exception:
            log.debug("hangup failed", exc_info=True)
        if self.connection is not None:
            with contextlib.suppress(Exception):
                await self.connection.close()

    async def _finish(self) -> None:
        self.closed.set()
        await self.browser_events.put({"type": "end"})
        try:
            duration = time.monotonic() - self.started_at
            usage_rows = [self.voice_usage, self.transcription_usage]
            await self.brain.voice_log(self.device_token, duration, self.transcript, usage_rows)
        except Exception:
            log.warning("voice log failed", exc_info=True)
