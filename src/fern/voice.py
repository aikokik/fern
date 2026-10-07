"""Server-side speech for long listens (the briefing). Swap this one function to change provider."""

from openai import AsyncOpenAI

from fern.config import Settings

TONE = "Warm, calm morning-radio host. Natural pace, light pauses between stories."


async def speak(s: Settings, text: str) -> bytes:
    r = await AsyncOpenAI().audio.speech.create(
        model=s.tts_model, voice=s.tts_voice, input=text, instructions=TONE, response_format="mp3"
    )
    return r.content
