"""Sahayak AI — Telugu voice receptionist demo (Krishnapatnam).

FastAPI app:
  GET  /              -> the single-page demo UI
  GET  /config        -> which STT/TTS providers are live (the page configures itself)
  GET  /api/bookings  -> recent bookings (populates the panel on load / after restart)
  WS   /ws            -> the turn loop: audio/text in -> transcript, reply, TTS, bookings out

Run:  python -m uvicorn main:app --reload --port 8000   ->  http://localhost:8000
"""
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import db
from services import stt, tts, llm

STATIC_DIR = Path(__file__).parent / "static"


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
    return {
        "restaurant": "Krishnapatnam",
        "llm_ok": llm.llm_available(),
        "stt": "sarvam" if stt.stt_available() else "webspeech",
        "tts": tts.active_provider(),
        "voice_ok": tts.eleven_ok(),
        "voice_detail": tts.eleven_reason(),
    }


@app.get("/api/bookings")
async def bookings():
    return {"bookings": db.recent_bookings()}


@app.get("/api/complaints")
async def complaints():
    return {"complaints": db.recent_complaints()}


async def _send(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


async def _process_text(ws: WebSocket, state: dict, text: str):
    """One full customer turn from a transcript/typed text."""
    text = (text or "").strip()
    if not text:
        await _send(ws, {"type": "status", "state": "idle"})
        return
    sid = state["session_id"]
    await _send(ws, {"type": "transcript", "role": "user", "text": text})
    db.log_turn(sid, "user", text)

    await _send(ws, {"type": "status", "state": "thinking"})

    async def on_booking(args: dict) -> dict:
        row = db.insert_booking(args)
        await _send(ws, {"type": "booking_created", "booking": row})
        db.log_turn(sid, "tool", "booking " + json.dumps(args, ensure_ascii=False))
        return row

    async def on_complaint(args: dict) -> dict:
        row = db.insert_complaint(args)
        await _send(ws, {"type": "complaint_created", "complaint": row})
        db.log_turn(sid, "tool", "complaint " + json.dumps(args, ensure_ascii=False))
        return row

    try:
        assistant_text = await llm.gemini_turn(
            state["contents"],
            text,
            {"create_booking": on_booking, "log_complaint": on_complaint},
        )
    except Exception as e:
        await _send(ws, {"type": "error", "where": "llm", "message": str(e), "recoverable": True})
        await _send(ws, {"type": "status", "state": "idle"})
        return

    await _send(ws, {"type": "assistant_text", "role": "assistant", "text": assistant_text})
    db.log_turn(sid, "assistant", assistant_text)

    await _send(ws, {"type": "status", "state": "speaking"})
    try:
        audio, mime = await tts.synthesize(assistant_text)
    except Exception:
        audio, mime = None, None
    if audio:
        await _send(ws, {"type": "tts_audio_meta", "mime": mime, "bytes": len(audio)})
        await ws.send_bytes(audio)
    await _send(ws, {"type": "status", "state": "idle"})


async def _process_audio(ws: WebSocket, state: dict, wav: bytes):
    await _send(ws, {"type": "status", "state": "transcribing"})
    try:
        text = await stt.transcribe_wav(wav)
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
    state = {"session_id": uuid.uuid4().hex, "contents": []}
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
                # client may carry its own session id; keep the server one authoritative
                await _send(ws, {"type": "status", "state": "idle", "detail": "connected"})
            elif mtype == "turn_text":
                await _process_text(ws, state, data.get("text", ""))
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
