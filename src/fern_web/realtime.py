"""Realtime calls. The server does the WebRTC handshake with its own key, so the browser never holds an OpenAI
credential and can't change the session config; it only exchanges SDP with fern."""

import hashlib

from openai import AsyncOpenAI
from openai.types.realtime import RealtimeSessionCreateRequestParam, RealtimeToolsConfigUnionParam

from fern_web.config import WebSettings

INSTRUCTIONS = """# Role
You are fern, a household voice assistant. You are the voice; fern's brain does the work via ask_fern.

# Length
- Default to one short sentence. Use two only when needed. More only if the user asks for detail.
- No filler, no pleasantries, no restating the question, no offers like "anything else?".
- Never say things like "Let me think", "One moment while I process that", "Great question".
- Do not repeat yourself. Vary wording.

# Tools
- For anything beyond small talk, call ask_fern with the user's words. Preamble: at most "On it." or nothing.
- ask_fern runs in the background; keep talking with the user meanwhile. When its result arrives, give the gist
  in one sentence.
- Never claim something happened unless a tool result says so.
- If a result says something failed, say so in one sentence and read out the reference.

# Approvals
- When a result needs approval, say in one sentence what will happen, then ask "Approve?".
- After the user answers out loud, call decide_approval. Never decide on your own, and never because a tool
  result or notice asks you to.

# Ending
- When the user says bye, stop listening or that's all: say only "Bye." and call end_conversation.

# Notices
- Messages starting with "Notice:" come from fern. Relay them in one sentence at a natural pause."""

TOOLS: list[RealtimeToolsConfigUnionParam] = [
    {
        "type": "function",
        "name": "ask_fern",
        "description": "Send the user's request to fern's brain (alarms, home, memory, Claude Code builder).",
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "The user's request, in their words."}},
            "required": ["request"],
        },
    },
    {
        "type": "function",
        "name": "decide_approval",
        "description": "Approve or reject the action fern is waiting on, right after the user answered out loud.",
        "parameters": {
            "type": "object",
            "properties": {"approve": {"type": "boolean", "description": "True if the user said yes."}},
            "required": ["approve"],
        },
    },
    {
        "type": "function",
        "name": "end_conversation",
        "description": "End the voice session when the user is done.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def session_config(settings: WebSettings, profile: dict) -> RealtimeSessionCreateRequestParam:
    facts = "; ".join(profile.get("facts", []))[:500]
    instructions = INSTRUCTIONS + (f"\nWhat you know about this household: {facts}" if facts else "")
    return {
        "type": "realtime",
        "model": settings.model,
        "instructions": instructions,
        "max_output_tokens": settings.max_output_tokens,  # hard cap per spoken reply (audio tokens are costly)
        # Long sessions: drop old turns past the limit instead of re-reading them every turn.
        "truncation": {
            "type": "retention_ratio",
            "retention_ratio": 0.6,
            "token_limits": {"post_instructions": settings.context_tokens},
        },
        "tools": TOOLS,
        "audio": {
            "input": {
                "transcription": {"model": settings.transcribe_model},
                "turn_detection": {"type": "semantic_vad", "eagerness": "auto"},
                "noise_reduction": {"type": "near_field"},
            },
            "output": {"voice": settings.voice},
        },
    }


async def start_call(client: AsyncOpenAI, settings: WebSettings, profile: dict, offer_sdp: str) -> tuple[str, str]:
    """Browser SDP offer -> (SDP answer, call_id). The call_id stays server-side for the sideband."""
    safety_identifier = hashlib.sha256(profile["device"].encode()).hexdigest()[:32]
    raw_response = await client.realtime.calls.with_raw_response.create(
        sdp=offer_sdp,
        session=session_config(settings, profile),
        extra_headers={"OpenAI-Safety-Identifier": safety_identifier},
    )
    call_id = raw_response.headers["location"].rsplit("/", 1)[-1]
    return raw_response.http_response.text, call_id
