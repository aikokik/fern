"""Realtime calls. The server does the WebRTC handshake with its own key, so the browser never holds an OpenAI
credential and can't change the session config; it only exchanges SDP with fern."""

import hashlib

from openai import AsyncOpenAI

from fern_web.config import WebSettings

INSTRUCTIONS = """You are fern, a household assistant, speaking with the user by voice.
For anything beyond small talk (questions, news, alarms, home control, memory, plans, changes to fern itself),
call ask_fern with the user's request in their own words, then speak its reply naturally and briefly.
Never claim you did something unless ask_fern's result says it was done. If the result says approval is needed,
tell the user to tap Approve on screen; a spoken yes does not count.
When the user says stop listening, goodbye, or that's all, say a short goodbye and call end_conversation.
Messages starting with "Notice:" come from fern itself; relay them briefly when there's a natural pause."""

TOOLS = [
    {
        "type": "function", "name": "ask_fern",
        "description": "Send the user's request to fern's brain (tools, memory, news, alarms, home, approvals).",
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "The user's request, in their words."}},
            "required": ["request"],
        },
    },
    {
        "type": "function", "name": "end_conversation",
        "description": "End the voice session when the user is done.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def session_config(s: WebSettings, profile: dict) -> dict:
    facts = "; ".join(profile.get("facts", []))[:500]
    instructions = INSTRUCTIONS + (f"\nWhat you know about this household: {facts}" if facts else "")
    return {
        "type": "realtime",
        "model": s.model,
        "instructions": instructions,
        "tools": TOOLS,
        "audio": {
            "input": {
                "transcription": {"model": s.transcribe_model},
                "turn_detection": {"type": "semantic_vad", "eagerness": "auto"},
                "noise_reduction": {"type": "near_field"},
            },
            "output": {"voice": s.voice},
        },
    }


async def start_call(client: AsyncOpenAI, s: WebSettings, profile: dict, offer_sdp: str) -> tuple[str, str]:
    """Browser SDP offer -> (SDP answer, call_id). The call_id stays server-side for the sideband."""
    safety_id = hashlib.sha256(profile["device"].encode()).hexdigest()[:32]
    raw = await client.realtime.calls.with_raw_response.create(
        sdp=offer_sdp, session=session_config(s, profile), extra_headers={"OpenAI-Safety-Identifier": safety_id},
    )
    return raw.http_response.text, raw.headers["location"].rsplit("/", 1)[-1]
