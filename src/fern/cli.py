"""fern chat | fern serve | fern usage"""

import argparse
import asyncio
import json
import time
import uuid

from fern.brain import Brain
from fern.config import settings
from fern.store import Store

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


def print_usage_report(days: float) -> None:
    """Tokens per model (brain + voice) over the last `days`, highest output first."""
    rows = Store(settings).usage_since(time.time() - days * 86400)
    print(f"{'model':<26}{'requests':>10}{'input':>12}{'cached':>12}{'output':>10}{'cache hit':>11}")
    for row in rows:
        print(
            f"{row['model']:<26}{row['requests']:>10}{row['input']:>12}{row['cached']:>12}"
            f"{row['output']:>10}{row['cache_hit']:>11.0%}"
        )
    if not rows:
        print(f"no usage recorded in the last {days:g} days")


def main() -> None:
    parser = argparse.ArgumentParser(prog="fern")
    parser.add_argument("command", choices=["chat", "serve", "usage"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--days", type=float, default=7, help="usage: how many days back")
    arguments = parser.parse_args()
    if arguments.command == "usage":
        print_usage_report(arguments.days)
        return
    if arguments.command == "serve":
        import logging

        import uvicorn

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        uvicorn.run("fern.server:app", host=arguments.host, port=arguments.port)
    else:
        asyncio.run(chat())
