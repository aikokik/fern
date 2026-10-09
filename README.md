# fern: household AI

```
phone/laptop ─Tailscale HTTPS─▶ ingress ─▶ web (src/fern_web: voice UI, Realtime handshake + server channel, /v1 pass-through)
     │ WebRTC audio ──▶ OpenAI Realtime ◀── server channel (tool calls) ──┘          │ device token
                                                                                     ▼
                                       brain (src/fern: API only, sealed net, read-only, non-root) ─▶ egress proxy (allowlist)
                                        ├─ fern (main model): alarms, home, memory, approvals, scheduler (1 call per action)
                                        └─ request_change (approved spec) ─▶ Claude Code Routine ─▶ PR ─▶ you merge ─▶ deployer
```

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

Also `GET /healthz`, `GET /v1/profile`, `GET /v1/notices`, `POST /v1/voice-log` (used by the web app).

**Docs:** [iPhone + voice web app](docs/iphone.md) · [Claude Code builder](docs/builder.md) · [always-on hosting](docs/hosting.md)

## Extend
- **Tool:** `@tool(Tier.READ|ACT|RISKY, group=...)` in a module with `TOOLS` (+ `GROUPS`); `FERN_PLUGINS=my.module`.
- **Skill:** `skills/<name>/SKILL.md` with `name`/`description` frontmatter; only the index goes in the prompt
  (`read_skill` stays hidden until a skill exists).
- **Token usage:** `uv run fern usage --days 7` shows tokens per model (brain + voice). Budget knobs:
  `FERN_REASONING_EFFORT`, `FERN_VOICE_MAX_OUTPUT_TOKENS`, `FERN_VOICE_CONTEXT_TOKENS`, `FERN_MODELS`.
- **Standing instructions:** `data/HOUSEHOLD.md`.
- **MCP:** `data/mcp.json` `{"name": {"command": "...", "args": []}}`; every call needs approval.
- **Memory research:** implement `remember/recall` (`fern.memory.Memory`), set `FERN_MEMORY=module:Class`.
- **Models / local / fine-tuned:** `FERN_MODELS={"main": "...", "local": "litellm/hosted_vllm/my-model"}`, then `/model local`.
- **RL / fine-tuning data:** `data/trajectories.jsonl` (Responses-format items + usage per run); eval scores = rewards.

## Test & eval
```bash
uv run pytest                                                   # offline, scripted model
uv run inspect eval evals/household.py --model openai/gpt-5-mini && uv run inspect view
```
Add cases to `evals/household.jsonl`: `tools` expected, `forbid`den, `pending` (approval expected).
