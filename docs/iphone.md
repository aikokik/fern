# iPhone setup

Two ways to talk to fern:
- **Voice web app** (conversation mode, interruptible, OpenAI Realtime): open `<address>` in Safari.
- **Shortcuts** (Siri dictation / Back Tap / Action Button, Apple voice).

## 1. Make fern reachable from the phone

1. Install **Tailscale** on the host (Mac or server) and the iPhone, same account.
2. On the host: `cd deploy && docker compose up -d --build`, then `tailscale serve --bg https / http://127.0.0.1:8766`.
3. Your `<address>` is the `https://<host>.<tailnet>.ts.net` URL that `tailscale serve status` prints.
   HTTPS is required: browsers only allow the microphone on secure pages.

The host must be on and awake (a Mac: plugged in, `caffeinate -s`; lid closed = asleep). A server avoids this:
see [hosting.md](hosting.md). Your phone token: the `phone=` value of `FERN_DEVICE_TOKENS` in `.env`.

## 1b. Voice web app

1. Safari → `<address>` → tap ⚙ → paste your phone token → Save.
2. Share → **Add to Home Screen** (opens full-screen like an app).
3. Tap the mic: it keeps listening and answering until you say "stop listening"/"bye", tap again, or stay silent
   for 2 minutes. You can interrupt it mid-sentence.
4. Risky actions: fern says what will happen and asks "Approve?". Answer **yes / no out loud** (only your own
   transcribed speech after the question counts), or tap the card.
5. Alarms show **Add to Clock**. Create this helper once: Shortcuts → **+** → name **Fern Alarm** → actions
   **Receive Text input from Share Sheet/Shortcuts** (default) → **Create Alarm** at *Shortcut Input*.

iPhone only lets web pages use the mic while the page is open and the screen is on.

## 2. Shortcut "Fern" (Back Tap, Action Button, or "Hey Siri, Fern")

Shortcuts app → **+** → name it **Fern** → add actions:

1. **Dictate Text** (Stop Listening: After Pause)
2. **Get Contents of URL**: `<address>/v1/chat`
   - Method **POST**, Headers: `Authorization` = `Bearer <phone token>`
   - Request Body **JSON**: `text` = *Dictated Text*
3. **Get Dictionary Value**: `commands` in *Contents of URL*
4. **Repeat with Each** item in *Dictionary Value*:
   - **Get Dictionary Value** `time` in *Repeat Item* → **Create Alarm** at *Dictionary Value* (Clock app action)
   - (only `alarm.set` exists today; add an **If** on `type` when more command types arrive)
5. **Get Dictionary Value**: `reply` in *Contents of URL* → **Speak Text**

Try: "Hey Siri, Fern" → "wake me at 6:30".

## Notes
- Approvals (risky actions) come back in `pending`; the voice web app asks "Approve?" (answer out loud), or approve with
  `POST /v1/approvals/<id>` `{"approve": true}`.
