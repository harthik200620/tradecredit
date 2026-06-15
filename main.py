"""Sahayak AI — Telugu voice receptionist demo (Krishnapatnam).

FastAPI app:
  GET  /              -> the single-page demo UI
  GET  /config        -> which STT/TTS providers are live (the page configures itself)
  GET  /api/bookings  -> recent bookings (populates the panel on load / after restart)
  WS   /ws            -> the turn loop: audio/text in -> transcript, reply, TTS, bookings out

Run:  python -m uvicorn main:app --reload --port 8000   ->  http://localhost:8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
from services import stt, tts, llm
from services.prompts import order_total

STATIC_DIR = Path(__file__).parent / "static"


def _clean_env(v: str) -> str:
    """Strip BOM / zero-width chars (CLI pipes and dashboard pastes inject these) plus quotes
    and surrounding whitespace — the same hygiene the service modules apply to their keys. Without
    it, a stray invisible char in ADMIN_PASSWORD makes EVERY password compare as 'wrong'."""
    for ch in (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D)):
        v = (v or "").replace(ch, "")
    return v.strip().strip('"').strip("'").strip()


ADMIN_PASSWORD = _clean_env(os.getenv("ADMIN_PASSWORD", "sahayak@ai"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await tts.probe_elevenlabs()
    yield


app = FastAPI(title="Sahayak AI · Telugu Voice Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/config")
async def config():
    # The page hits /config on load — use it to warm the ElevenLabs probe on a serverless
    # cold start so the FIRST spoken turn doesn't pay for the probe.
    if tts._eleven_ok is None:
        await tts.probe_elevenlabs()
    return {
        "restaurant": "Krishnapatnam",
        "llm_ok": llm.llm_available(),
        "stt": "sarvam" if stt.stt_available() else "webspeech",
        "tts": tts.active_provider(),
        "voice_ok": tts.eleven_ok(),
        "voice_detail": tts.eleven_reason(),
        "model": llm.GEMINI_MODEL,
        "llm_keys": llm.key_count(),
    }


@app.get("/api/bookings")
async def bookings():
    return {"bookings": db.recent_bookings()}


@app.get("/api/complaints")
async def complaints():
    return {"complaints": db.recent_complaints()}


@app.get("/api/orders")
async def orders():
    return {"orders": db.recent_orders()}


@app.post("/api/login")
async def api_login(password: str = Form(default="")):
    """Admin gate — validates the access password for the demo."""
    return {"ok": password == ADMIN_PASSWORD}


# One spoken acknowledgment per language, played the INSTANT the caller stops talking, masking
# the ~4-5s eleven_v3 synthesis of the real reply. Synthesized once per (provider, voice, lang)
# and cached. One consistent line per language — no cycling through phrases.
_FILLER_TEXTS = {
    "english": ["One moment…"],
    "hindi": ["एक मिनट…"],
    "telugu": ["ఒక్క నిమిషం అండి…"],
}
_filler_cache: dict[str, list] = {}

# Caller's chosen language → Sarvam STT language_code.
_LANG_CODE = {"english": "en-IN", "hindi": "hi-IN", "telugu": "te-IN"}


@app.post("/api/fillers")
async def api_fillers(password: str = Form(default=""), lang: str = Form(default="english")):
    if password != ADMIN_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if tts._eleven_ok is None:          # settle the provider BEFORE keying the cache
        await tts.probe_elevenlabs()
    lang = (lang or "english").lower()
    texts = _FILLER_TEXTS.get(lang, _FILLER_TEXTS["english"])
    key = f"{tts.active_provider()}::{tts._voice_for(lang)}::{lang}"
    if key not in _filler_cache:
        # SEQUENTIAL on purpose: the ElevenLabs free tier allows only 2 concurrent requests,
        # and a parallel warm-up 429s. One-time cost at page load, so latency doesn't matter.
        out = []
        for t in texts:
            try:
                a, m = await tts.synthesize(t, lang)
                if a:
                    out.append({"b64": base64.b64encode(a).decode("ascii"), "mime": m})
            except Exception:
                pass
        if out:  # keep one consistent voice — drop clips that fell back to another provider
            out = [o for o in out if o["mime"] == out[0]["mime"]]
        _filler_cache[key] = out
    return {"fillers": _filler_cache[key]}


@app.post("/api/say")
async def api_say(text: str = Form(default=""), password: str = Form(default=""),
                  lang: str = Form(default="english")):
    """TTS-only — speak a fixed line without invoking the LLM (used for no-reply nudges)."""
    if password != ADMIN_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    audio_b64, mime = None, None
    try:
        a, m = await tts.synthesize(text, lang)
        if a:
            audio_b64, mime = base64.b64encode(a).decode("ascii"), m
    except Exception:
        pass
    return {"audio_b64": audio_b64, "audio_mime": mime}


def _latest_for_phone(snapshot: list, phone: str) -> dict | None:
    """Newest snapshot row whose phone matches — compared on the LAST 10 digits so
    '+91 98765 43210' and '9876543210' are the same caller."""
    digits = re.sub(r"\D", "", str(phone or ""))[-10:]
    if not digits:
        return None
    matches = [r for r in snapshot if isinstance(r, dict)
               and re.sub(r"\D", "", str(r.get("phone", "")))[-10:] == digits]
    return max(matches, key=lambda r: r.get("id") or 0) if matches else None


@app.post("/api/turn")
async def api_turn(
    text: str = Form(default=""),
    history: str = Form(default="[]"),
    orders: str = Form(default="[]"),
    bookings: str = Form(default="[]"),
    password: str = Form(default=""),
    lang: str = Form(default="english"),
    audio: UploadFile = File(default=None),
):
    """Stateless turn for HTTP/serverless clients (Vercel has no WebSocket).
    The client carries the conversation history — and a snapshot of the orders/bookings it has
    seen, so updates still work when another serverless instance (fresh /tmp DB) gets the turn."""
    if password != ADMIN_PASSWORD:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        contents = json.loads(history) if history else []
    except Exception:
        contents = []
    try:
        client_orders = json.loads(orders) if orders else []
    except Exception:
        client_orders = []
    try:
        client_bookings = json.loads(bookings) if bookings else []
    except Exception:
        client_bookings = []

    user_text = (text or "").strip()
    transcript = user_text
    if audio is not None:
        wav = await audio.read()
        try:
            transcript = await stt.transcribe_wav(wav, _LANG_CODE.get(lang.lower(), "en-IN"))
        except Exception as e:
            return {"error": f"stt: {e}", "history": contents}
        user_text = transcript
    if not user_text:
        return {"error": "no input", "history": contents}

    captured = {"booking": None, "complaint": None, "order": None}

    async def on_booking(args):
        captured["booking"] = db.insert_booking(args)
        return captured["booking"]

    async def on_update_booking(args):
        row = db.update_latest_booking(
            args.get("phone", ""), args.get("party_size"), args.get("date"),
            args.get("time"), args.get("name"), args.get("notes"),
        )
        if row is None:  # this instance never saw the booking — recover from the client snapshot
            base = _latest_for_phone(client_bookings, args.get("phone", ""))
            if base:
                merged = {**base}
                for k in ("party_size", "date", "time", "name", "notes"):
                    if args.get(k):
                        merged[k] = args[k]
                merged["status"] = "changed"
                row = db.upsert_booking(merged)
        if row:
            captured["booking"] = row
        return row

    async def on_complaint(args):
        captured["complaint"] = db.insert_complaint(args)
        return captured["complaint"]

    async def on_order(args):
        captured["order"] = db.insert_order(args)
        return captured["order"]

    async def on_update_order(args):
        row = db.update_latest_order(
            args.get("phone", ""), args.get("items"), args.get("notes"),
            args.get("order_type"), args.get("payment"),
        )
        if row is None:  # this instance never saw the order — recover from the client snapshot
            base = _latest_for_phone(client_orders, args.get("phone", ""))
            if base:
                merged = {**base}
                for k in ("items", "order_type", "payment", "notes"):
                    if args.get(k):
                        merged[k] = args[k]
                if args.get("items"):
                    merged["total"] = order_total(args["items"])
                merged["status"] = "changed"
                row = db.upsert_order(merged)
        if row:
            captured["order"] = row
        return row

    try:
        reply = await llm.gemini_turn(
            contents, user_text,
            {
                "create_booking": on_booking,
                "update_booking": on_update_booking,
                "log_complaint": on_complaint,
                "create_order": on_order,
                "update_order": on_update_order,
            },
            lang=lang,
        )
    except Exception as e:
        return {"error": f"llm: {e}", "transcript": transcript, "history": contents}

    # Synthesize ONLY the first sentence here and hand the rest back as text — the client
    # plays the first chunk immediately and fetches the remainder via /api/say while it plays.
    # Cuts the rest-of-reply TTS time out of the perceived response.
    chunks = _split_for_tts(reply)
    audio_b64, mime, rest_text = None, None, None
    if chunks:
        try:
            a, m = await tts.synthesize(chunks[0], lang)
            if a:
                audio_b64, mime = base64.b64encode(a).decode("ascii"), m
                if len(chunks) > 1:
                    rest_text = chunks[1]
        except Exception:
            pass

    return {
        "transcript": transcript,
        "reply": reply,
        "booking": captured["booking"],
        "complaint": captured["complaint"],
        "order": captured["order"],
        "history": contents,
        "audio_b64": audio_b64,
        "audio_mime": mime,
        "rest_text": rest_text,
    }


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


_SENT_END = re.compile(r"[.!?…।]\s")


def _split_for_tts(text: str) -> list[str]:
    """[first sentence, remainder] so the first sentence can start playing while the rest
    synthesizes. Short replies stay a single chunk (no extra TTS round-trip)."""
    t = (text or "").strip()
    if len(t) < 55:
        return [t] if t else []
    m = _SENT_END.search(t)
    if not m:
        return [t]
    first, rest = t[: m.end()].strip(), t[m.end():].strip()
    return [first, rest] if rest else [t]


async def _process_text(ws: WebSocket, state: dict, text: str, silent: bool = False):
    """One full customer turn. `silent` hides the user echo (internal no-reply nudges)."""
    text = (text or "").strip()
    if not text:
        await _send(ws, {"type": "status", "state": "idle"})
        return
    sid = state["session_id"]
    if not silent:
        await _send(ws, {"type": "transcript", "role": "user", "text": text})
    db.log_turn(sid, "user", text)

    await _send(ws, {"type": "status", "state": "thinking"})

    async def on_booking(args: dict) -> dict:
        row = db.insert_booking(args)
        await _send(ws, {"type": "booking_created", "booking": row})
        db.log_turn(sid, "tool", "booking " + json.dumps(args, ensure_ascii=False))
        return row

    async def on_update_booking(args: dict):
        row = db.update_latest_booking(
            args.get("phone", ""), args.get("party_size"), args.get("date"),
            args.get("time"), args.get("name"), args.get("notes"),
        )
        if row:
            await _send(ws, {"type": "booking_created", "booking": row})
            db.log_turn(sid, "tool", "booking_update " + json.dumps(args, ensure_ascii=False))
        return row

    async def on_complaint(args: dict) -> dict:
        row = db.insert_complaint(args)
        await _send(ws, {"type": "complaint_created", "complaint": row})
        db.log_turn(sid, "tool", "complaint " + json.dumps(args, ensure_ascii=False))
        return row

    async def on_order(args: dict) -> dict:
        row = db.insert_order(args)
        await _send(ws, {"type": "order_created", "order": row})
        db.log_turn(sid, "tool", "order " + json.dumps(args, ensure_ascii=False))
        return row

    async def on_update_order(args: dict):
        row = db.update_latest_order(
            args.get("phone", ""), args.get("items"), args.get("notes"),
            args.get("order_type"), args.get("payment"),
        )
        if row:
            await _send(ws, {"type": "order_created", "order": row})
            db.log_turn(sid, "tool", "order_update " + json.dumps(args, ensure_ascii=False))
        return row

    try:
        assistant_text = await llm.gemini_turn(
            state["contents"],
            text,
            {
                "create_booking": on_booking,
                "update_booking": on_update_booking,
                "log_complaint": on_complaint,
                "create_order": on_order,
                "update_order": on_update_order,
            },
            lang=state.get("lang", "english"),
        )
    except Exception as e:
        await _send(ws, {"type": "error", "where": "llm", "message": str(e), "recoverable": True})
        await _send(ws, {"type": "status", "state": "idle"})
        return

    await _send(ws, {"type": "assistant_text", "role": "assistant", "text": assistant_text})
    db.log_turn(sid, "assistant", assistant_text)

    await _send(ws, {"type": "status", "state": "speaking"})
    # Sentence-by-sentence, SEQUENTIAL on purpose: chunk 2 synthesizes while chunk 1 is already
    # playing in the browser, and staying at 1 concurrent request keeps the ElevenLabs free
    # tier (2-concurrent limit) from 429ing when fillers or /api/say overlap.
    for chunk in _split_for_tts(assistant_text):
        try:
            audio, mime = await tts.synthesize(chunk, state.get("lang", "english"))
        except Exception:
            audio, mime = None, None
        if audio:
            await _send(ws, {"type": "tts_audio_meta", "mime": mime, "bytes": len(audio)})
            await ws.send_bytes(audio)
    await _send(ws, {"type": "status", "state": "idle"})


async def _process_audio(ws: WebSocket, state: dict, wav: bytes):
    await _send(ws, {"type": "status", "state": "transcribing"})
    try:
        text = await stt.transcribe_wav(wav, _LANG_CODE.get(state.get("lang", "english").lower(), "en-IN"))
    except Exception as e:
        await _send(ws, {"type": "error", "where": "stt", "message": str(e), "recoverable": True})
        await _send(ws, {"type": "status", "state": "idle"})
        return
    if not text:
        await _send(ws, {"type": "status", "state": "idle", "detail": "no speech detected"})
        return
    await _process_text(ws, state, text)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state = {"session_id": uuid.uuid4().hex, "contents": [], "lang": "english"}
    db.ensure_conversation(state["session_id"])
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if msg.get("bytes") is not None:
                await _process_audio(ws, state, msg["bytes"])
                continue

            raw = msg.get("text")
            if raw is None:
                continue
            data = json.loads(raw)
            mtype = data.get("type")

            if mtype == "hello":
                if data.get("password") != ADMIN_PASSWORD:
                    await _send(ws, {"type": "error", "where": "auth",
                                     "message": "unauthorized", "recoverable": False})
                    await ws.close()
                    return
                state["lang"] = (data.get("lang") or "english")
                # client may carry its own session id; keep the server one authoritative
                await _send(ws, {"type": "status", "state": "idle", "detail": "connected"})
            elif mtype == "turn_text":
                if data.get("lang"):
                    state["lang"] = data["lang"]
                await _process_text(
                    ws, state, data.get("text", ""), silent=bool(data.get("silent"))
                )
            elif mtype == "control":
                action = data.get("action")
                if action == "restart":
                    state["session_id"] = uuid.uuid4().hex
                    state["contents"] = []
                    db.ensure_conversation(state["session_id"])
                    await _send(ws, {"type": "status", "state": "idle", "detail": "restarted"})
                elif action == "stop":
                    await _send(ws, {"type": "status", "state": "idle", "detail": "stopped"})
    except WebSocketDisconnect:
        pass
    except Exception:
        # keep the server alive; the client will reconnect on next Start
        try:
            await ws.close()
        except Exception:
            pass
