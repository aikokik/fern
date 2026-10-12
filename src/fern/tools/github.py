"""GitHub for fern's own repo (FERN_GITHUB_REPO): read PRs, issues and CI; create issues; open, close or reopen PRs.

Writes are RISKY (your yes first). Merging isn't possible from here. Titles, bodies and comments are written by
whoever can post to the repo, so everything read comes back marked untrusted.
"""

import logging
import secrets
from typing import Any, Literal

import httpx
from agents import RunContextWrapper

from fern.config import Settings
from fern.core import EgressBlockedError, FernContext, Tier, describe_http_error, tool, untrusted

log = logging.getLogger("fern")
BODY_CHARS = 2000
COMMENT_CHARS = 500
COMMENTS_SHOWN = 5


class GitHubError(Exception):
    pass


def github_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json"}


async def _call(context: FernContext, method: str, path: str, **request_options: Any) -> Any:
    settings = context.settings
    if not (settings.github_repo and settings.github_token):
        raise GitHubError("GitHub isn't set up (FERN_GITHUB_REPO / FERN_GITHUB_TOKEN)")
    url = f"https://api.github.com/repos/{settings.github_repo}{path}"
    try:
        response = await context.http.request(method, url, headers=github_headers(settings), **request_options)
        response.raise_for_status()
    except (httpx.HTTPError, EgressBlockedError) as error:
        reference = secrets.token_hex(3)
        log.warning("github %s failed [ref %s]: %s", method, reference, describe_http_error(error))
        denied = isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (403, 404)
        hint = " (the token may lack a permission)" if denied else ""
        raise GitHubError(f"GitHub request failed{hint}. Fern logs reference {reference}.") from None
    return response.json() if response.content else {}


@tool(Tier.READ)
async def github_list(
    run: RunContextWrapper[FernContext],
    kind: Literal["prs", "issues"],
    state: Literal["open", "closed", "all"] = "open",
) -> str:
    """List pull requests or issues in fern's GitHub repo.

    Args:
        kind: 'prs' or 'issues'.
        state: 'open' (default), 'closed' or 'all'.
    """
    try:
        items = await _call(run.context, "GET", "/pulls" if kind == "prs" else "/issues", params={"state": state})
    except GitHubError as error:
        return f"error: {error}"
    if kind == "issues":
        items = [item for item in items if "pull_request" not in item]  # the issues API lists PRs too
    lines = [f"#{item['number']} [{item['state']}] {item['title']} ({item['user']['login']})" for item in items[:20]]
    return untrusted("github", "\n".join(lines)) if lines else f"no {state} {kind}"


@tool(Tier.READ)
async def github_read(run: RunContextWrapper[FernContext], number: int) -> str:
    """Read a pull request or issue: description, latest comments and, for PRs, branches and CI status.

    Args:
        number: The PR or issue number.
    """
    context = run.context
    try:
        item = await _call(context, "GET", f"/issues/{number}")
        comments = await _call(context, "GET", f"/issues/{number}/comments") if item.get("comments") else []
        lines = [f"#{number} [{item['state']}] {item['title']} ({item['user']['login']})"]
        if "pull_request" in item:
            pull = await _call(context, "GET", f"/pulls/{number}")
            check_runs = (await _call(context, "GET", f"/commits/{pull['head']['sha']}/check-runs"))["check_runs"]
            checks = ", ".join(f"{check['name']}: {check['conclusion'] or check['status']}" for check in check_runs)
            merged = " (merged)" if pull.get("merged_at") else ""
            lines += [f"PR {pull['head']['ref']} -> {pull['base']['ref']}{merged}", f"CI: {checks or 'none'}"]
    except GitHubError as error:
        return f"error: {error}"
    lines += ["", (item.get("body") or "(no description)")[:BODY_CHARS]]
    lines += [
        f"\n{comment['user']['login']}: {comment['body'][:COMMENT_CHARS]}" for comment in comments[-COMMENTS_SHOWN:]
    ]
    return untrusted("github", "\n".join(lines))


@tool(Tier.RISKY)
async def github_create_issue(run: RunContextWrapper[FernContext], title: str, body: str) -> str:
    """Create an issue in fern's GitHub repo. The user approves this exact text first.

    Args:
        title: Short title.
        body: Markdown description. No secrets or personal data.
    """
    try:
        issue = await _call(run.context, "POST", "/issues", json={"title": title[:200], "body": body[:10_000]})
    except GitHubError as error:
        return f"error: {error}"
    return f"Created issue #{issue['number']}: {issue['html_url']}"


@tool(Tier.RISKY)
async def github_open_pr(
    run: RunContextWrapper[FernContext], head: str, title: str, body: str = "", base: str = "main"
) -> str:
    """Open a pull request from an existing branch. Merging stays with the user on GitHub.

    Args:
        head: The branch with the changes, e.g. 'claude/weather-tool'.
        title: Short PR title.
        body: Markdown description.
        base: Branch to merge into (default 'main').
    """
    payload = {"head": head, "base": base, "title": title[:200], "body": body[:10_000]}
    try:
        pull = await _call(run.context, "POST", "/pulls", json=payload)
    except GitHubError as error:
        return f"error: {error}"
    return f"Opened PR #{pull['number']}: {pull['html_url']}"


@tool(Tier.RISKY)
async def github_set_state(run: RunContextWrapper[FernContext], number: int, state: Literal["open", "closed"]) -> str:
    """Close or reopen a pull request or issue.

    Args:
        number: The PR or issue number.
        state: 'closed' to close, 'open' to reopen.
    """
    try:
        await _call(run.context, "PATCH", f"/issues/{number}", json={"state": state})  # works for PRs too
    except GitHubError as error:
        return f"error: {error}"
    return f"#{number} is now {state}."


TOOLS = [github_list, github_read, github_create_issue, github_open_pr, github_set_state]
