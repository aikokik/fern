"""`fern-github`: Claude's GitHub access, a shell command it may run inside the builder.

It calls the shipper, which holds the GitHub token, with FERN_GITHUB_TOOLS_TOKEN. That token only allows reading
PRs, issues and CI, creating issues, and closing or reopening. Merging and pushing aren't possible.
"""

import argparse
import json
import sys

import httpx

from fern_builder.config import builder_settings


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fern-github", description="fern's GitHub repo: PRs, issues, CI.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list pull requests or issues")
    listing.add_argument("kind", choices=["prs", "issues"])
    listing.add_argument("--state", choices=["open", "closed", "all"], default="open")
    commands.add_parser("read", help="a PR or issue with comments (and branches + CI for PRs)").add_argument(
        "number", type=int
    )
    create = commands.add_parser("create-issue", help="create an issue")
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    for command in ("close", "reopen"):
        commands.add_parser(command, help=f"{command} a PR or issue").add_argument("number", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    params, payload = None, None
    match arguments.command:
        case "list":
            method, path, params = "GET", "/github/list", {"kind": arguments.kind, "state": arguments.state}
        case "read":
            method, path = "GET", f"/github/item/{arguments.number}"
        case "create-issue":
            method, path, payload = "POST", "/github/issues", {"title": arguments.title, "body": arguments.body}
        case _:  # close / reopen
            state = "closed" if arguments.command == "close" else "open"
            method, path, payload = "POST", f"/github/item/{arguments.number}/state", {"state": state}
    headers = {"Authorization": f"Bearer {builder_settings.github_tools_token}"}
    with httpx.Client(base_url=builder_settings.shipper_url, headers=headers, timeout=60) as client:
        response = client.request(method, path, params=params, json=payload)
    if response.status_code >= 400:
        detail = response.json().get("detail", "") if response.content else ""
        print(f"fern-github: failed ({response.status_code}) {detail}".strip(), file=sys.stderr)
        return 1
    print(json.dumps(response.json(), indent=1))
    return 0
