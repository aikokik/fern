"""Server-side channel into a live Realtime session (same session as the browser's WebRTC call, joined by call_id).

The browser carries audio only. Tool calls, approvals, live notices, idle/cost limits and transcripts run here.
"""

import asyncio
import json
import logging
import time
from typing import Any, AsyncContextManager, Callable

from fern_web.brain_client import BrainClient

log = logging.getLogger("fern_web")


class VoiceSession:
    def __init__(
        self, sid: str, token: str, call_id: str, brain: BrainClient,
        connect: Callable[[str], AsyncContextManager[Any]], hangup: Callable[[str], Any],
        max_seconds: float, idle_seconds: float, notice_poll: float,
    ):
        self.sid, self.token, self.call_id, self.brain = sid, token, call_id, brain
        self._connect, self._hangup = connect, hangup
        self.max_seconds, self.idle_seconds, self.notice_poll = max_seconds, idle_seconds, notice_poll
        self.events: asyncio.Queue[dict] = asyncio.Queue()  # -> browser (SSE)
        self.transcript: list[dict] = []
        self.started = self.last_activity = time.monotonic()
        self.responding = False
        self.conn: Any = None
        self.closed = asyncio.Event()

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        try:
            async with self._connect(self.call_id) as conn:
                self.conn = conn
                tasks = [asyncio.create_task(self._watchdog()), asyncio.create_task(self._notices())]
                async for ev in conn:
                    await self._on(ev)
                    if self.closed.is_set():
                        break
        except Exception:
            if not self.closed.is_set():  # a closed socket after our own hangup is expected
                log.exception("voice session %s failed", self.sid)
        finally:
            for t in tasks:
                t.cancel()
            await self._finish()

    async def _on(self, ev: Any) -> None:
        t = ev.type
        if t == "response.function_call_arguments.done":
            asyncio.create_task(self._tool(ev.name, ev.call_id, ev.arguments))  # keep events flowing meanwhile
        elif t == "input_audio_buffer.speech_started":
            self.last_activity = time.monotonic()
        elif t == "conversation.item.input_audio_transcription.completed":
            self.last_activity = time.monotonic()
            self.transcript.append({"role": "user", "text": ev.transcript})
        elif t == "response.output_audio_transcript.done":
            self.transcript.append({"role": "assistant", "text": ev.transcript})
        elif t == "response.created":
            self.responding = True
        elif t == "response.done":
            self.responding = False
            self.last_activity = time.monotonic()
        elif t == "error":
            log.warning("realtime error in %s: %s", self.sid, getattr(ev, "error", ev))

    async def _tool(self, name: str, call_id: str, arguments: str) -> None:
        if name == "end_conversation":
            await self._output(call_id, "ok")
            await asyncio.sleep(4)  # let the goodbye play
            self.closed.set()
            await self.close()
            return
        if name != "ask_fern":
            await self._output(call_id, f"unknown tool {name}")
            return
        try:
            request = json.loads(arguments or "{}").get("request", "")
            out = await self.brain.chat(self.token, request)
        except Exception as e:
            log.exception("ask_fern failed")
            await self._output(call_id, f"fern is unavailable right now ({type(e).__name__}).")
            return
        await self.publish(out)
        reply = out.get("reply") or ""
        if out.get("pending"):
            reply += " (Waiting for the user to tap Approve or Reject on screen.)"
        await self._output(call_id, reply)

    async def publish(self, out: dict) -> None:
        """Brain side effects the browser must act on: device commands and approval cards."""
        for c in out.get("commands", []):
            await self.events.put({"type": "command", "command": c})
        for p in out.get("pending", []):
            await self.events.put({"type": "pending", **p})

    async def say(self, text: str) -> None:
        """Inject a system message and let the model voice it, waiting for a pause first."""
        for _ in range(50):
            if not self.responding or self.closed.is_set():
                break
            await asyncio.sleep(0.2)
        if self.conn is None or self.closed.is_set():
            return
        await self.conn.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": text}],
        }})
        await self.conn.send({"type": "response.create"})

    async def _output(self, call_id: str, output: str) -> None:
        await self.conn.send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": output,
        }})
        await self.conn.send({"type": "response.create"})

    async def _watchdog(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - self.started > self.max_seconds or now - self.last_activity > self.idle_seconds:
                self.closed.set()
                await self.close()

    async def _notices(self) -> None:
        while not self.closed.is_set():
            await asyncio.sleep(self.notice_poll)
            try:
                for n in await self.brain.notices(self.token):
                    await self.say(f"Notice: {n}")
            except Exception:
                log.warning("notice poll failed", exc_info=True)

    async def close(self) -> None:
        try:
            await self._hangup(self.call_id)
        except Exception:
            log.debug("hangup failed", exc_info=True)
        if self.conn is not None:
            try:
                await self.conn.close()
            except Exception:
                log.debug("close failed", exc_info=True)

    async def _finish(self) -> None:
        self.closed.set()
        await self.events.put({"type": "end"})
        try:
            await self.brain.voice_log(self.token, time.monotonic() - self.started, self.transcript)
        except Exception:
            log.warning("voice log failed", exc_info=True)
