"""fern chat | fern serve | fern briefing"""

import argparse
import asyncio
import json
import uuid

from fern.brain import Brain
from fern.config import settings

HELP = "/model <alias>  /session  /new  /quit"


async def chat() -> None:
    session = f"cli-{uuid.uuid4().hex[:8]}"
    async with Brain(settings) as brain:
        print(f"fern · session {session} · {HELP}")
        while (text := input("› ").strip()) != "/quit":
            model = None
            if text == "/new":
                session = f"cli-{uuid.uuid4().hex[:8]}"
                print(f"new session {session}")
                continue
            if text == "/session":
                print(json.dumps(brain.store.stats(session), indent=1))
                continue
            if text.startswith("/model "):
                model, text = text.split()[1], "(model switched; acknowledge in one word)"
            if not text:
                continue
            out = await brain.run(text, session=session, model=model)
            while out["pending"]:
                p = out["pending"][0]
                ok = input(f"approve {p['tool']} {p['args']}? [y/N] ").lower() == "y"
                out = await brain.resolve(p["id"], ok)
            for c in out["commands"]:
                print(f"  ⚙ {c}")
            print(out["reply"])


async def briefing() -> None:
    async with Brain(settings) as brain:
        print((await brain.briefing("cli"))["text"])


def main() -> None:
    p = argparse.ArgumentParser(prog="fern")
    p.add_argument("cmd", choices=["chat", "serve", "briefing"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()
    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("fern.server:app", host=a.host, port=a.port)
    else:
        asyncio.run(chat() if a.cmd == "chat" else briefing())
