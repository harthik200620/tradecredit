# Verba — AI Voice & Chat Agents (a Sahayak AI product)

One app, three agents in three languages, plus a live CRM:

| Route | What it shows |
|---|---|
| `/` | Scenario picker → **📞 Inbound lead call** — Verba's own line answers, qualifies, books a callback · **💳 Payment reminder** — Suvidha Finserv, a polite EMI reminder that logs the outcome · **💬 WhatsApp assistant** — Ananya Clinic chat that answers instantly and books appointments. **Every scenario runs in all three languages** (language chips on each card); the defaults are EN / हिंदी / తెలుగు respectively |
| `/crm` | **Verba CRM** — every call/chat outcome writes back here live (leads, promises-to-pay, appointments, enquiries) |

**Stack:** Sarvam Saaras v3 (speech-to-text) · Google Gemini (brain + one tool per
scenario, 12-key rotation) · ElevenLabs `eleven_v3` (voice, auto-falls back to Sarvam
Bulbul) · FastAPI + SQLite. No telephony, no ffmpeg, no Node — the browser captures
16 kHz WAV itself. The chat scenario is text-only (instant replies, zero TTS spend).

## Setup
```powershell
cd "C:\Users\HP\Claude\Projects\AI service clients\voice-agent"
& "C:\Users\HP\AppData\Local\Programs\Python\Python313\python.exe" -m pip install -r requirements.txt

copy .env.example .env      # then open .env and paste your keys
```
Fill `.env`:
- `GEMINI_API_KEY` (+ optional `_2`…`_12` for rotation) — required (aistudio.google.com/apikey)
- `SARVAM_API_KEY` — speech-to-text. Without it the page uses the browser's built-in
  recognition (Chrome/Edge only).
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` — the voice (English/Hindi).
  `ELEVENLABS_VOICE_ID_TE` — optional separate Telugu voice.
- `ADMIN_PASSWORD` — the access password for the pages.

## Run
```powershell
& "C:\Users\HP\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn main:app --reload --port 8000
```
Open **http://localhost:8000** in Chrome/Edge (use `localhost`, not a file:// path — the mic
needs a secure context, which localhost satisfies). Pick a scenario; in the voice ones the
**agent speaks first** (it "picks up"), in the chat one it greets and you type.

You can also **type** in the voice scenarios (box at the bottom) to run without a microphone.

## Controls
- **📞 Start call / Stop** — begin or pause the call (the agent answers first).
- **Restart** — clear the conversation for a fresh run (CRM rows are kept — it's a CRM).
- **⇄ Scenarios** — back to the picker.
- **Mic sensitivity** — raise it in a noisy room, lower it if it cuts you off.

## Where the facts live (EDIT ME)
- `services/prompts.py` → `COLLECTION_CASE` (customer, EMI amount, due date, loan ref),
  `CLINIC` (timings, doctors, prices), the three system prompts and openers.
- CRM rows land in `app.db` → `SELECT * FROM crm;`

## Notes / honest caveats
- Voice turns take ~2.5–4 s (three cloud hops); the chat scenario replies near-instantly.
- The "calls" run in the browser — there is no phone number attached yet. For a pitch,
  run it full-screen on a phone and keep a screen recording as backup.
- ElevenLabs Telugu/Hindi = `eleven_v3` only; `/config` shows which voice is actually live.
- On Vercel each serverless instance has its own ephemeral DB; the CRM page also receives
  rows live from the agent tab (BroadcastChannel + localStorage), so the write-back moment
  never depends on which instance served the call.
