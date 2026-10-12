# Talking to Claude Code through fern

fern's voice can hand the conversation to Claude Code: plan a feature by voice (long text on screen), have it
built, try it, and ship a PR. Claude runs on **your Pro/Max plan** (Claude Code CLI), inside fern's sandbox.

Status: **phase 2 of 4** (builder, voice switching + screen panel). Preview (phase 3) and usage (phase 4) come
next.

## Talking to Claude
| Say | What happens |
|---|---|
| "switch to Claude about weather tool" | Same voice, now relaying to Claude Code for topic `weather tool` (asks for a topic if you didn't name one) |
| anything else | Sent to Claude. It answers in a sentence or two; plans and details appear in the **Claude panel** on screen |
| "option 2" / "the first one" (or tap it) | Answers Claude's on-screen question |
| "build it" | Claude implements the agreed plan and runs tests + lint (can take minutes; the session stays open) |
| "ship it" | Claude commits, pushes `claude/<topic>` and opens the PR itself; the link shows in the panel |
| "list topics" / "continue weather tool" | See resumable topics / pick one up again |
| "back to fern" | fern's own tools again; the panel stays readable |

While talking to Claude, fern's tools (alarms, memory, approvals) are off; say "back to fern" first.
One Claude request runs at a time per session; asking again during a build gets "still working".

## How it's isolated
The `builder` container has the Claude Code CLI, the bridge API and a git clone of the repo per topic. It doesn't
have `.env`, `data/`, or internet beyond Anthropic/claude.ai/GitHub/PyPI.

- Claude's processes see only `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN` (its own GitHub access), not the bridge
  token.
- Plan turns can read files and use `gh`.
- Build turns can also edit files and run `uv sync / pytest / ruff / pyright`, `git` and `gh`.
- **GitHub is Claude's own access:** `gh` + `git` with `FERN_BUILDER_GITHUB_TOKEN`. It's told to work on
  `claude/<topic>` and never push to main or merge, but **only the token's scope and GitHub's rules enforce
  that**. Add a ruleset on `main`: GitHub → Settings → Rules → "Restrict deletions", "Block force pushes",
  "Require a pull request before merging".

## Setup (once)
1. **Claude token** (on your Mac, needs the Claude Code CLI): `claude setup-token`, then copy the printed token.
2. **GitHub token**: github.com → Settings → Developer settings → Fine-grained tokens → only `aikokik/fern` →
   **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Checks: Read**.
3. Add to `.env` (generate the bridge secret with `openssl rand -hex 24`):
   ```
   CLAUDE_CODE_OAUTH_TOKEN=…
   FERN_BUILDER_GITHUB_TOKEN=github_pat_…
   FERN_BRIDGE_TOKEN=…
   FERN_CLAUDE_MODEL=sonnet
   FERN_CLAUDE_DEVICES=phone          # optional: device names allowed to switch to Claude (empty = all)
   ```
4. `cp .env deploy/.env && cd deploy && FERN_BIND=0.0.0.0 docker compose up -d --build`

## GitHub
- **Claude** uses `gh` and `git` directly: PRs, issues, CI, comments, commits and pushes. Ask it by voice
  ("Claude, what's failing on PR 8?").
- **fern** has `github_list` / `github_read` and, with your spoken yes, `github_create_issue`, `github_open_pr` and
  `github_set_state`, using `FERN_GITHUB_REPO` + `FERN_GITHUB_TOKEN` (fine-grained, `aikokik/fern` only:
  **Pull requests**, **Issues** Read and write, **Checks** Read). Never merge.

## Bridge API (used by fern's web app)
`Authorization: Bearer $FERN_BRIDGE_TOKEN`, reachable only inside the sealed network at `http://builder:8770`.

| Call | Does |
|---|---|
| `POST /topics/{name}/turn {text}` | One plan-mode turn (clones the repo on first use) → `{say, show, question, turn_cost_usd}` |
| `POST /topics/{name}/build {text?}` | Implement the agreed plan, run tests and lint |
| `POST /topics/{name}/ship {title}` | A build turn where Claude commits, pushes `claude/<topic>` and opens the PR → `{pr_url}` |
| `GET /topics` | Resumable topics with status |
