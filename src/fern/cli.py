"""fern chat | fern serve | fern briefing"""

import argparse
import asyncio
import json
import uuid

from fern.brain import Brain
from fern.config import settings

HELP = "/model <alias>  /session  /new  /quit"


async def ask(prompt: str) -> str:
    """input() without blocking the event loop (MCP servers and HTTP clients keep running meanwhile)."""
    return (await asyncio.to_thread(input, prompt)).strip()


async def chat() -> None:
    session = f"cli-{uuid.uuid4().hex[:8]}"
    async with Brain(settings) as brain:
        print(f"fern · session {session} · {HELP}")
        while (text := await ask("> ")) != "/quit":
            if text == "/new":
                session = f"cli-{uuid.uuid4().hex[:8]}"
                print(f"new session {session}")
                continue
            if text == "/session":
                print(json.dumps(brain.store.stats(session), indent=1))
                continue
            if text.startswith("/model "):
                model_alias = text.split()[1]
                brain.store.put(session, "model", model_alias)  # no model call needed just to switch
                print(f"model: {settings.model(model_alias)}")
                continue
            if not text:
                continue
            response = await brain.run(text, session=session)
            while response["pending"]:
                pending = response["pending"][0]
                approved = (await ask(f"approve {pending['tool']} {pending['args']}? [y/N] ")).lower() == "y"
                response = await brain.resolve(pending["id"], approved)
            for command in response["commands"]:
                print(f"  ⚙ {command}")
            print(response["reply"])


async def briefing() -> None:
    async with Brain(settings) as brain:
        print((await brain.briefing("cli"))["text"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="fern")
    parser.add_argument("command", choices=["chat", "serve", "briefing"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        import logging

        import uvicorn

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        uvicorn.run("fern.server:app", host=arguments.host, port=arguments.port)
    else:
        asyncio.run(chat() if arguments.command == "chat" else briefing())
