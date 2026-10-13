# Talking to Claude Code through fern

Say "open Claude" to fern and you're talking to Claude Code, the same as the VS Code chat:
- your project folder and real working copy
- your Claude login and plan, your `gh`
- your permission rules and saved memories
- it continues your latest conversation

Claude has no voice of its own, so fern's voice speaks for it: a short gist out loud, everything else on screen.

## How it works
```
phone/laptop ─▶ fern web (Docker) ─▶ egress proxy ─▶ fern-bridge on your Mac ─▶ Claude Code (Agent SDK)
                  voice + panel                       127.0.0.1:8770, token      your folder, login, gh, settings
```
- **`fern-bridge`** (`src/fern_builder/bridge.py`) runs on the Mac, not in Docker. It drives Claude Code with the
  **Claude Agent SDK**, the same engine IDE integrations use.
- **Your setup applies:** `setting_sources = user, project, local`, so your allow/deny rules (e.g. no `.env`),
  CLAUDE.md and memories apply as in VS Code.
- **Your latest conversation continues, the same session** VS Code uses, not a copy. "New session" starts fresh;
  "list my sessions" and "open session 2" switch between this project's sessions. After 20 idle minutes the
  connection closes, and the next message continues your latest session again.
- Don't type in the same session in VS Code while Claude is working on it by voice: both would write to it at once.
- **Live progress:** Claude's steps ("Bash: uv run pytest", "Edit: src/…") and text stream to the panel while it
  works.
- **Prompts come to you:** when your settings would make Claude ask permission, fern shows **Approve / Always /
  Reject** and asks "Approve?" out loud.
  - Only your own transcribed "yes", said after the question, or a tap approves.
  - "Always" (or "yes, don't ask again") saves the rule to `.claude/settings.local.json`, like VS Code.
  - Claude's multiple-choice questions show as buttons; answer "option 2" or tap.

## Talking to Claude
| Say | What happens |
|---|---|
| "open Claude" / "switch to Claude" | Same voice, now relaying to Claude Code |
| anything | Sent to Claude; steps and text stream to the panel, the voice gives the gist |
| "yes" / "no" / "always" (after "Approve?") | Answers Claude's permission prompt |
| "option 2" / "the first one" (or tap) | Answers Claude's question |
| "stop" | Interrupts what Claude is doing |
| "new session" / "new chat" | The next message starts a fresh session |
| "list my sessions" | This project's recent sessions, numbered, on screen (the first few read out) |
| "open session 2" | The next message continues that session |
| "back to fern" | fern's own tools again; the panel stays readable |

While talking to Claude, fern's tools (alarms, memory) are off; say "back to fern" first. If you hang up while Claude
waits on a prompt, that turn is stopped.

## Setup (once, on the Mac)
1. Be logged in to Claude Code, as you are for VS Code. The Agent SDK ships its own CLI and uses the same login.
2. `uv sync --extra bridge`
3. In `.env`: `FERN_BRIDGE_TOKEN=…` (`openssl rand -hex 24`). The bridge reads only this key from `.env`, so no
   other secret reaches Claude. Optional: `FERN_CLAUDE_DEVICES=phone` (devices allowed to open Claude).
4. Start the bridge and keep it running (LaunchAgent, from the repo root):
   ```
   sed -e "s|PROJECT_DIR|$PWD|g" -e "s|HOME_DIR|$HOME|g" deploy/com.fern.bridge.plist > ~/Library/LaunchAgents/com.fern.bridge.plist
   launchctl load ~/Library/LaunchAgents/com.fern.bridge.plist
   ```
   Or run it in a terminal: `uv run --extra bridge fern-bridge`. Logs: `~/Library/Logs/fern-bridge.log`.
5. `cp .env deploy/.env && cd deploy && docker compose up -d --build --remove-orphans`

## Security
Voice Claude isn't sandboxed: like the VS Code plugin, it can do whatever you could do on this Mac, within your
permission rules. What protects you:
- **Approvals:** your rules, plus a spoken or tapped approval for anything they'd ask about. A permission request
  can't approve itself: it needs your own "yes", transcribed after the question.
- **Who can open Claude:** `FERN_CLAUDE_DEVICES`, which limits it to your devices.
- **The bridge itself:** it listens only on `127.0.0.1` and needs `FERN_BRIDGE_TOKEN`. Docker reaches it through
  the egress proxy (`host.docker.internal` is on the allowlist). The brain's own in-app allowlist still blocks it.
- **Context cost:** continuing a long session means each voice turn re-reads that context. It's cached, but it uses
  plan limits faster. Claude Code compacts it automatically.

## Bridge API (fern_web → fern-bridge)
`Authorization: Bearer $FERN_BRIDGE_TOKEN`.

| Call | Does |
|---|---|
| `POST /messages {text}` | One turn, streamed as NDJSON: `progress`, `text`, `permission`, `question`, `ping`, then `done` / `error` |
| `POST /permissions/{id} {decision}` | `allow`, `always` or `deny` |
| `POST /questions/{id} {answers}` | `{question: label}` |
| `POST /interrupt` | Stop the current turn |
| `POST /new` | The next message starts a fresh conversation |
| `GET /sessions` | This project's recent sessions (`id`, `title`, `updated`, `current`) |
| `POST /sessions/{id}/open` | The next message continues that session |
