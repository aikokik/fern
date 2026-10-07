# iPhone voice setup

Voice split: **Apple** listens (Siri dictation, on-device) and speaks short replies (Speak Text).
**OpenAI TTS** reads the morning briefing (natural voice; pre-generated ~15 min before alarms set through fern).

## 1. Make fern reachable from the phone

The Mac must be **awake and plugged in** at alarm time (`caffeinate -s` keeps it awake on power; lid closed = asleep).

| | Home Wi-Fi only (start here) | Anywhere (recommended) |
|---|---|---|
| Setup | nothing | install Tailscale on Mac + iPhone, same account |
| Address | `http://<Mac LAN IP>:8766` (`ipconfig getifaddr en0`) | `http://<tailscale ip -4>:8766` |
| Run | `FERN_BIND=<that IP> FERN_PORT=8766 docker compose up -d` (in `deploy/`) | same |

Without Docker: `caffeinate -s uv run fern serve --host <that IP> --port 8766`.
Your phone token: the `phone=` value of `FERN_DEVICE_TOKENS` in `.env`.

## 2. Shortcut "Fern" (say "Hey Siri, Fern")

Shortcuts app → **+** → name it **Fern** → add actions:

1. **Dictate Text** (Stop Listening: After Pause)
2. **Get Contents of URL**: `http://<address>/v1/chat`
   - Method **POST**, Headers: `Authorization` = `Bearer <phone token>`
   - Request Body **JSON**: `text` = *Dictated Text*
3. **Get Dictionary Value**: `commands` in *Contents of URL*
4. **Repeat with Each** item in *Dictionary Value*:
   - **Get Dictionary Value** `time` in *Repeat Item* → **Create Alarm** at *Dictionary Value* (Clock app action)
   - (only `alarm.set` exists today; add an **If** on `type` when more command types arrive)
5. **Get Dictionary Value**: `reply` in *Contents of URL* → **Speak Text**

Try: "Hey Siri, Fern" → "wake me at 6:30".

## 3. Automation: briefing when the alarm stops

Shortcuts → **Automation** → **+** → **Alarm** → **Is Stopped** → Any → **Run Immediately** → actions:

1. **Get Contents of URL**: `http://<address>/v1/briefing/audio`, Method **GET**, Header `Authorization` = `Bearer <phone token>`
2. **Play Sound**: *Contents of URL*

Fallback (Apple voice, no OpenAI TTS): use `/v1/briefing` and **Speak Text** instead.

## Notes
- First briefing of the day takes ~30–40 s if it wasn't pre-generated. Alarms set through fern are pre-generated
  automatically; for alarms set in the Clock app, list your usual times in `FERN_BRIEFING_AT=07:00,08:30`.
- Approvals (risky actions) come back in `pending`; approve with `POST /v1/approvals/<id>` `{"approve": true}`.
