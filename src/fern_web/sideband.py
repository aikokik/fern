"""Server-side channel into a live Realtime session (same session as the browser's WebRTC call, joined by call_id).

The browser carries audio only. Tool calls, approvals, live notices, idle/cost limits and transcripts run here.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fern_web.brain_client import BrainClient

log = logging.getLogger("fern_web")
GOODBYE_SECONDS = 4  # let the spoken goodbye finish before hanging up


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
        self.started_at = self.last_activity_at = time.monotonic()
        self.model_is_responding = False
        self.connection: Any = None
        self.closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()  # strong refs: asyncio only keeps weak ones
        self._brain_lock = asyncio.Lock()  # one brain request at a time per session keeps its history ordered
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
            self.last_activity_at = time.monotonic()
            self.transcript.append({"role": "user", "text": event.transcript})
        elif event_type == "response.output_audio_transcript.done":
            self.transcript.append({"role": "assistant", "text": event.transcript})
        elif event_type == "response.created":
            self.model_is_responding = True
        elif event_type == "response.done":
            self.model_is_responding = False
            self.last_activity_at = time.monotonic()
            self._start_tool_calls(event.response)
        elif event_type == "error":
            log.warning("realtime error in %s: %s", self.session_id, getattr(event, "error", event))

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
        if name == "end_conversation":
            await self._send_tool_output(call_id, "ok")
            await asyncio.sleep(GOODBYE_SECONDS)
            self.closed.set()
            await self.close()
            return
        if name != "ask_fern":
            await self._send_tool_output(call_id, f"unknown tool {name}")
            return
        try:
            request = json.loads(arguments or "{}").get("request", "")
        except json.JSONDecodeError:
            log.warning("ask_fern got malformed arguments: %.80r", arguments)
            await self._send_tool_output(call_id, "The request was cut off. Ask the user to repeat it.")
            return
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
        await self.publish(brain_response)
        reply = brain_response.get("reply") or ""
        if brain_response.get("pending"):
            reply += " (Waiting for the user to tap Approve or Reject on screen.)"
        await self._send_tool_output(call_id, reply)

    async def publish(self, brain_response: dict) -> None:
        """Brain side effects the browser must act on: device commands and approval cards."""
        for command in brain_response.get("commands", []):
            await self.browser_events.put({"type": "command", "command": command})
        for pending in brain_response.get("pending", []):
            await self.browser_events.put({"type": "pending", **pending})

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

    async def _send_tool_output(self, call_id: str, output: str) -> None:
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
            await self.brain.voice_log(self.device_token, duration, self.transcript)
        except Exception:
            log.warning("voice log failed", exc_info=True)
