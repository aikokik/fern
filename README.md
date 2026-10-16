# fern: interactive coding by voice

Talk to **Claude Code** from your phone the way you'd chat with it in VS Code: say "open Claude", describe what you
want, watch its steps stream onto the screen, and approve its actions with a spoken "yes" or a tap. fern also runs a
small, sandboxed household assistant (alarms, memory, research, recipes and photos on screen) behind the same voice.

- **Voice Claude, like the VS Code chat.** Claude Code runs on your Mac through the Claude Agent SDK, with your folder,
  login, `gh`, permission rules and memories. Anything your rules would ask about comes to you as "Approve?" by
  voice or on screen. Sessions are shared with the VS Code chat: list them, open one, or start a new one by voice.
- **`@claude` on GitHub.** Comment `@claude` on an issue or PR: questions get a short answer in the thread, tasks go
  to a `todo` issue list, and "implement this" opens a PR.
- **A living canvas.** An audio-reactive orb, live captions, and cards the assistant chooses itself: answers,
  photos, recipes whose servings rescale, checklists, choices to tap, tables, and Claude's live work.
- **Sandboxed by default.** The household brain runs in a sealed container with an egress allowlist; risky actions
  need a human; the browser never holds an API key and never renders model-written HTML.

```
phone/laptop ─▶ web (Docker: voice page, Realtime handshake, server-side channel) ─▶ brain (Docker, sealed network)
     │ WebRTC audio ⇄ OpenAI Realtime                    │                             └─▶ egress proxy (allowlist)
     │                                                   └─▶ fern-bridge on the Mac ─▶ Claude Code (Agent SDK)
     └ cards, captions, approvals                            your folder, login, gh, permission prompts by voice
```

Status: a personal project, shared for reference. No license: the code is visible, all rights reserved.

## Run
```bash
uv sync
cp .env.example .env            # set OPENAI_API_KEY; tokens via `openssl rand -hex 24`
set -a; . ./.env; set +a
uv run fern chat                 # REPL: /model <alias>  /session  /new  /quit
uv run fern serve                # brain API on 127.0.0.1:8765
uv run fern-web                  # voice web app on 127.0.0.1:8766 (talks to the brain; http://localhost works for the mic)
cd deploy && cp ../.env .env && docker compose up -d --build   # sandboxed; entry point 127.0.0.1:8766 (FERN_PORT)
```

## API (header `Authorization: Bearer <device token>`)
| | |
|---|---|
| `POST /v1/chat` `{text, session?, model?}` | → `{reply, commands, pending, tools}`; the device executes `commands` (e.g. `alarm.set`) |
| `POST /v1/approvals/{id}` `{approve}` | resolve a risky action (single use, expires in 5 min) |
| `GET /v1/sessions/{s}/stats` | tokens + cache-hit per model |

Also `GET /healthz`, `GET /v1/profile`, `GET /v1/notices`, `POST /v1/voice-log` (used by the web app), and
`GET /v1/usage` (cost report). **Usage page:** open `/usage` in the web app (same device token).

**Docs:** [talking to Claude Code by voice](docs/claude-voice.md) · [iPhone + voice web app](docs/iphone.md) · [Claude Code builder (cloud)](docs/builder.md) · [always-on hosting](docs/hosting.md)

## Extend
- **Tool:** `@tool(Tier.READ|ACT|RISKY, group=...)` in a module with `TOOLS` (+ `GROUPS`); `FERN_PLUGINS=my.module`.
- **Skill:** `skills/<name>/SKILL.md` with `name`/`description` frontmatter; only the index goes in the prompt
  (`read_skill` stays hidden until a skill exists).
- **Research documents:** set `FERN_VECTOR_STORE_IDS=vs_...` to let the research subagent use OpenAI file_search.
- **On-screen cards:** fern shows recipes (servings rescale), checklists, choices and tables by itself, and photos with
  `FERN_PEXELS_KEY` (free key at pexels.com/api). The page renders only known card types, never model-written HTML.
- **Token usage:** the `/usage` page or `uv run fern usage --days 7` (cost per part; prices in `src/fern/costs.py`). Knobs:
  `FERN_REASONING_EFFORT`, `FERN_VOICE_MAX_OUTPUT_TOKENS`, `FERN_VOICE_CONTEXT_TOKENS`, `FERN_MODELS`.
- **Standing instructions:** `data/HOUSEHOLD.md`.
- **MCP:** `data/mcp.json` `{"name": {"command": "...", "args": []}}`; every call needs approval.
- **Memory research:** implement `remember/recall` (`fern.memory.Memory`), set `FERN_MEMORY=module:Class`.
- **Models / local / fine-tuned:** `FERN_MODELS={"main": "...", "local": "litellm/hosted_vllm/my-model"}`, then `/model local`.
- **RL / fine-tuning data:** `data/trajectories.jsonl` (Responses-format items + usage per run); eval scores = rewards.

## Test & eval
```bash
uv run pytest                                                   # offline, scripted model
uv run inspect eval evals/household.py --model openai/gpt-6-luna && uv run inspect view
```
Add cases to `evals/household.jsonl`: `tools` expected, `forbid`den, `pending` (approval expected).
