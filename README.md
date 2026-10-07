# fern: household AI brain

```
device (Shortcut/iPad/TV/laptop) ──HTTPS+token──▶ ingress ─▶ brain (sealed net, read-only, non-root) ─▶ egress proxy (allowlist) ─▶ internet
                                                             ├─ fern (main model): core tools, deferred groups, skills, memory, MCP
                                                             └─ news_desk (fast model, subagent): reads untrusted content, no side-effect tools
                                                                  └─ reader container: HN item id → article text (only open egress; no URLs, no private IPs)
```

## Run
```bash
uv sync
cp .env.example .env            # set OPENAI_API_KEY; tokens via `openssl rand -hex 24`
set -a; . ./.env; set +a
uv run fern chat                 # REPL: /model <alias>  /session  /new  /quit
uv run fern serve                # API on 127.0.0.1:8765
cd deploy && cp ../.env .env && docker compose up -d --build   # sandboxed (FERN_PORT/FERN_BIND to change port/IP)
```

## API (header `Authorization: Bearer <device token>`)
| | |
|---|---|
| `POST /v1/chat` `{text, session?, model?}` | → `{reply, commands, pending, tools}`; the device executes `commands` (e.g. `alarm.set`) |
| `POST /v1/approvals/{id}` `{approve}` | resolve a risky action (single use, expires in 5 min) |
| `GET /v1/briefing` | briefing text (cached 2h; pre-generated before alarms) |
| `GET /v1/briefing/audio` | the same as mp3 (OpenAI TTS) |
| `GET /v1/sessions/{s}/stats` | tokens + cache-hit per model |

**iPhone voice:** see [docs/iphone.md](docs/iphone.md) (Siri shortcut + alarm-stop automation).

## Extend
- **Tool:** `@tool(Tier.READ|ACT|RISKY, group=...)` in a module with `TOOLS` (+ `GROUPS`); `FERN_PLUGINS=my.module`.
- **Skill:** `skills/<name>/SKILL.md` with `name`/`description` frontmatter; only the index goes in the prompt.
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
