"""Server-side speech for long listens (the briefing). Swap this one function to change provider."""

from openai import AsyncOpenAI

from fern.config import Settings

TONE = "Warm, calm morning-radio host. Natural pace, light pauses between stories."


async def speak(settings: Settings, text: str) -> bytes:
    speech = await AsyncOpenAI().audio.speech.create(
        model=settings.tts_model, voice=settings.tts_voice, input=text, instructions=TONE, response_format="mp3"
    )
    return speech.content
