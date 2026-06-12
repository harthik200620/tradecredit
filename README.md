# Krishnapatnam — Telugu AI Voice Receptionist (Sahayak AI demo)

A browser voice agent that takes restaurant calls in **Telugu + English (code-mix)**,
answers menu/hours questions, and **books tables live** — the booking appears on the page
and is saved to a SQLite database. Built to pitch Krishnapatnam (Hyderabad).

**Stack:** Sarvam Saaras v3 (speech-to-text) · Google Gemini 2.5 Flash (brain + booking
tool) · ElevenLabs `eleven_v3` (voice, auto-falls back to Sarvam Bulbul) · FastAPI + SQLite.
No telephony, no Pipecat, no ffmpeg, no Node — the browser captures 16 kHz WAV itself.

## Setup
```powershell
cd "C:\Users\HP\Claude\Projects\AI service clients\voice-agent"
& "C:\Users\HP\AppData\Local\Programs\Python\Python313\python.exe" -m pip install -r requirements.txt

copy .env.example .env      # then open .env and paste your keys
```
Fill `.env`:
- `GEMINI_API_KEY` — required (aistudio.google.com/apikey)
- `SARVAM_API_KEY` — Telugu speech-to-text (dashboard.sarvam.ai). Without it the page uses
  the browser's built-in te-IN recognition (Chrome/Edge only).
- `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` — the voice. **Telugu needs the `eleven_v3`
  model**; if your key doesn't have it the app auto-uses Sarvam Bulbul. Get the key at
  elevenlabs.io → Profile → API Keys; pick a voice id from elevenlabs.io → Voices.

## Run
```powershell
& "C:\Users\HP\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn main:app --reload --port 8000
```
Open **http://localhost:8000** in Chrome/Edge (use `localhost`, not a file:// path — the mic
needs a secure context, which localhost satisfies). Click **Start talking**, allow the mic,
and say e.g. *"7 గంటలకి 4 మందికి table కావాలి"*. The agent asks your name + phone, confirms in
Telugu, mentions WhatsApp, and a booking card appears.

You can also **type** a message (box at the bottom) to demo without a microphone or keys.

## Controls
- **Start talking / Stop** — begin or pause listening.
- **Restart** — clear the conversation + board for a fresh demo (saved bookings stay in the DB).
- **Mic sensitivity** — raise it in a noisy room, lower it if it cuts you off.

## What it does (mirrors the Sahayak "AI Voice" product)
Table booking (name + phone, confirmation, WhatsApp note), menu & price Q&A, hours/location,
order notes, human-handoff offer, full transcript + bookings stored in `app.db`.

## Notes / honest caveats
- ~2.5–4 s per turn (three cloud hops). Status pills show what it's doing.
- ElevenLabs Telugu = `eleven_v3` only; `/config` shows which voice is actually live.
- Menu prices in `services/prompts.py` are realistic placeholders — paste the real
  Krishnapatnam card there before an actual pitch (look for the "EDIT ME" banner).
- Inspect the database any time: `SELECT * FROM bookings;` in `app.db`.
