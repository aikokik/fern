# fern: interactive coding by voice

Talk to **Claude Code** from your phone the way you'd chat with it in VS Code: say "open Claude", describe what you
want, watch its steps stream onto the screen, and approve its actions with a spoken "yes" or a tap. 

```
phone/laptop ─▶ web (Docker: voice page, Realtime handshake, server-side channel) ─▶ brain (Docker, sealed network)
     │ WebRTC audio ⇄ OpenAI Realtime                    │                             └─▶ egress proxy (allowlist)
     │                                                   └─▶ fern-bridge on the Mac ─▶ Claude Code (Agent SDK)
     └ cards, captions, approvals                            your folder, login, gh, permission prompts by voice
```

```bash
uv sync
cp .env.example .env            # set OPENAI_API_KEY; tokens via `openssl rand -hex 24`
set -a; . ./.env; set +a
cd deploy && cp ../.env .env && docker compose up -d --build   # sandboxed; entry point 127.0.0.1:8766 (FERN_PORT)
```

## Test & eval
```bash
uv run pytest                                                   # offline, scripted model
uv run inspect eval evals/household.py --model openai/gpt-6-luna && uv run inspect view
```
Add cases to `evals/household.jsonl`: `tools` expected, `forbid`den, `pending` (approval expected).

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
