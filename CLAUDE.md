# fern

Household AI. Two packages, separate containers:
- `src/fern/`: the **brain**, API only. OpenAI Agents SDK orchestrator (`brain.py`), tools, memory,
  approvals, scheduler. HTTP API in `server.py`. No UI here.
- `src/fern_web/`: the **voice interface**. Static page + OpenAI Realtime session minting + a server-side channel that runs
  tool calls by calling the brain's HTTP API. Must never import `fern`.

## Conventions
- Tools: `@tool(Tier.READ|ACT|RISKY, group=...)` from `src/fern/core.py`. RISKY = needs human approval. Built-in
  tools have no `group` (always visible); use `group` only for large plugin tool sets, loaded via `load_tools`.
- Token budget: action tools (`ACTION_TOOLS` in `brain.py`) end the run with their own output, so return a short,
  user-ready sentence, or a string starting with `error` to hand the problem back to the model. Prefer adding a plugin module (exposes `TOOLS`, optional `GROUPS`) under
  `src/fern/tools/` and registering it in `src/fern/tools/__init__.py`.
- Skills: `skills/<name>/SKILL.md` with `name`/`description` frontmatter.
- External content returned to the model goes through `untrusted(source, text)`.
- Outbound HTTP uses `FernContext.http` (egress allowlist). New hosts need `FERN_ALLOWED_HOSTS` **and** `deploy/allowlist`.
- Keep code minimal: no speculative abstractions, match the surrounding style, comments only for non-obvious "why".
- Names are descriptive: no single-letter or cryptic variables (`settings`, `response`, `event`, `job`, not `s`, `r`,
  `ev`, `j`). Tool functions take `run: RunContextWrapper[FernContext]` and read state via `run.context`.

## Commands
- Lint + types: `uv run ruff check . && uv run ruff format . && uv run pyright src evals tests` (CI enforces all).
- Tests: `uv run pytest` (offline; `agents.testing.ScriptedModel` + `httpx.MockTransport`, fake world in `src/fern/fakes.py`).
- Evals: `uv run inspect eval evals/household.py --model openai/<model>` (needs a key; don't run in CI).
- Every change ships with tests. Every new behavior the model should exhibit also gets a case in `evals/household.jsonl`.

## Security rules (hard)
- Never read, print or modify `.env*` or anything under `data/` (secrets, household memory).
- Never weaken: tool tiers, approval policy, egress allowlists, `deploy/` sandbox settings, auth.
  If a request truly requires touching these, do the minimum, and say so first in the PR description under **Security**.
- Never add tools that run shell commands, eval code, or fetch model-chosen URLs from the brain.
- Never merge; open a PR.
