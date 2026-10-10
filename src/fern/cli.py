"""fern chat | fern serve | fern usage"""

import argparse
import asyncio
import json
import time
import uuid

from fern import costs
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
    """Cost, calls and tokens per part (voice, transcription, brain, subagents, tools) over the last `days`."""
    report = costs.build_report(Store(settings).usage_by_session(time.time() - days * 86400))
    print(f"{'part':<16}{'calls':>8}{'tokens':>12}{'cost':>12}")
    for part, totals in report["totals"]["parts"].items():
        print(f"{part:<16}{totals['calls']:>8}{totals['tokens']:>12}{'$' + format(totals['cost'], '.4f'):>12}")
    total = report["totals"]
    print(f"{'total':<16}{total['calls']:>8}{total['tokens']:>12}{'$' + format(total['cost'], '.4f'):>12}")
    if report["unpriced_models"]:
        print(f"no price for: {', '.join(report['unpriced_models'])} (add it to fern/costs.py)")


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
