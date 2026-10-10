# Talking to Claude Code through fern

fern's voice can hand the conversation to Claude Code: plan a feature by voice (long text on screen), have it
built, try it, and ship a PR. Claude runs on **your Pro/Max plan** (Claude Code CLI), inside fern's sandbox.

Status: **phase 1 of 4** (builder + shipper services). Voice switching (phase 2), preview (phase 3) and usage
(phase 4) come next.

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
   **Contents: Read and write**, **Pull requests: Read and write**.
3. Add to `.env` (generate the two shared secrets with `openssl rand -hex 24`):
   ```
   CLAUDE_CODE_OAUTH_TOKEN=…
   FERN_BUILDER_GITHUB_TOKEN=github_pat_…
   FERN_BRIDGE_TOKEN=…
   FERN_SHIPPER_TOKEN=…
   FERN_CLAUDE_MODEL=sonnet
   ```
4. `cp .env deploy/.env && cd deploy && FERN_BIND=0.0.0.0 docker compose up -d --build`

## Bridge API (used by fern's web app in phase 2)
`Authorization: Bearer $FERN_BRIDGE_TOKEN`, reachable only inside the sealed network at `http://builder:8770`.

| Call | Does |
|---|---|
| `POST /topics/{name}/turn {text}` | One plan-mode turn → `{say, show, question, turn_cost_usd}` |
| `POST /topics/{name}/build {text?}` | Implement the agreed plan, run tests and lint |
| `POST /topics/{name}/ship {title}` | Commit, push `claude/<topic>`, open the PR → `{pr_url}` |
| `GET /topics` | Resumable topics with status |
