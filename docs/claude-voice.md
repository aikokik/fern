# Talking to Claude Code through fern

fern's voice can hand the conversation to Claude Code: plan a feature by voice (long text on screen), have it
built, try it, and ship a PR. Claude runs on **your Pro/Max plan** (Claude Code CLI), inside fern's sandbox.

Status: **phase 2 of 4** (builder + shipper, voice switching + screen panel). Preview (phase 3) and usage
(phase 4) come next.

## Talking to Claude
| Say | What happens |
|---|---|
| "switch to Claude about weather tool" | Same voice, now relaying to Claude Code for topic `weather tool` (asks for a topic if you didn't name one) |
| anything else | Sent to Claude. It answers in a sentence or two; plans and details appear in the **Claude panel** on screen |
| "option 2" / "the first one" (or tap it) | Answers Claude's on-screen question |
| "build it" | Claude implements the agreed plan and runs tests + lint (can take minutes; the session stays open) |
| "ship it" | Commit, push `claude/<topic>`, open the PR; the link shows in the panel |
| "list topics" / "continue weather tool" | See resumable topics / pick one up again |
| "back to fern" | fern's own tools again; the panel stays readable |

While talking to Claude, fern's tools (alarms, memory, approvals) are off; say "back to fern" first.
One Claude request runs at a time per session; asking again during a build gets "still working".

## How it's isolated
| Container | Has | Doesn't have |
|---|---|---|
| `builder` | Claude Code CLI, the bridge API, a plain copy of the repo per topic | GitHub token, `.env`, `data/`, internet beyond Anthropic/claude.ai/GitHub/PyPI |
| `shipper` | GitHub token (repo-scoped), the real git clones | Claude, your files |

- Claude's processes see only `CLAUDE_CODE_OAUTH_TOKEN`. Plan turns are read-only. Build turns may edit files
  and run only `uv sync / pytest / ruff / pyright`.
- Git runs only in the shipper, which never reads hooks or config that Claude wrote, and pushes only `claude/<topic>`
  branches. Merging stays yours.

## Setup (once)
1. **Claude token** (on your Mac, needs the Claude Code CLI): `claude setup-token`, then copy the printed token.
2. **GitHub token**: github.com → Settings → Developer settings → Fine-grained tokens → only `aikokik/fern` →
   **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Checks: Read**.
3. Add to `.env` (generate the two shared secrets with `openssl rand -hex 24`):
   ```
   CLAUDE_CODE_OAUTH_TOKEN=…
   FERN_BUILDER_GITHUB_TOKEN=github_pat_…
   FERN_BRIDGE_TOKEN=…
   FERN_SHIPPER_TOKEN=…
   FERN_GITHUB_TOOLS_TOKEN=…          # a third random secret: Claude's GitHub command
   FERN_CLAUDE_MODEL=sonnet
   FERN_CLAUDE_DEVICES=phone          # optional: device names allowed to switch to Claude (empty = all)
   ```
4. `cp .env deploy/.env && cd deploy && FERN_BIND=0.0.0.0 docker compose up -d --build`

## GitHub
- **Claude** runs `fern-github` (list / read PRs, issues and CI, create issues, close / reopen). The command calls the
  shipper with `FERN_GITHUB_TOOLS_TOKEN`, which only unlocks those calls. The GitHub token stays in the shipper,
  and merging or pushing isn't possible. Issues Claude creates are marked "Created by Claude Code via fern".
- **fern** has `github_list` / `github_read` and, with your spoken yes, `github_create_issue`, `github_open_pr` and
  `github_set_state`, using `FERN_GITHUB_REPO` + `FERN_GITHUB_TOKEN` (fine-grained, `aikokik/fern` only:
  **Pull requests** and **Issues** Read and write, **Checks** and **Contents** Read). Never merge.

## Bridge API (used by fern's web app)
`Authorization: Bearer $FERN_BRIDGE_TOKEN`, reachable only inside the sealed network at `http://builder:8770`.

| Call | Does |
|---|---|
| `POST /topics/{name}/turn {text}` | One plan-mode turn → `{say, show, question, turn_cost_usd}` |
| `POST /topics/{name}/build {text?}` | Implement the agreed plan, run tests and lint |
| `POST /topics/{name}/ship {title}` | Commit, push `claude/<topic>`, open the PR → `{pr_url}` |
| `GET /topics` | Resumable topics with status |
