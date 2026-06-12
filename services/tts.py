"""Text-to-speech behind a single pluggable synthesize() function.

Default voice = ElevenLabs (model from ELEVENLABS_MODEL_ID, normally "eleven_v3" — the
only ElevenLabs model that speaks Telugu). At startup we probe the key with GET /v1/models;
if the model isn't available we transparently fall back to Sarvam Bulbul (reliably natural
Telugu). If neither is usable, synthesize() returns (None, None) and the agent shows text only.

synthesize(text) -> (audio_bytes | None, mime | None)
  ElevenLabs -> mp3  (audio/mpeg)
  Sarvam     -> wav  (audio/wav)   both play natively in the browser.
"""
from __future__ import annotations

import os
import base64
import httpx

TTS_PROVIDER = os.getenv("TTS_PROVIDER", "elevenlabs").strip().lower()

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVEN_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVEN_MODEL = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3").strip()

SARVAM_KEY = os.getenv("SARVAM_API_KEY", "").strip()
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2").strip()
SARVAM_TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "anushka").strip()

# Probe results (set by probe_elevenlabs at startup)
_eleven_ok: bool | None = None
_eleven_reason: str = "not probed"


def eleven_ok() -> bool:
    return bool(_eleven_ok)


def eleven_reason() -> str:
    return _eleven_reason


def active_provider() -> str:
    """What will actually speak, given keys + probe result."""
    if TTS_PROVIDER == "none":
        return "none"
    if TTS_PROVIDER == "elevenlabs" and _eleven_ok:
        return "elevenlabs"
    if SARVAM_KEY:
        return "sarvam"
    return "none"


async def probe_elevenlabs() -> None:
    """Check whether the configured ElevenLabs model is available on this key."""
    global _eleven_ok, _eleven_reason
    if TTS_PROVIDER != "elevenlabs":
        _eleven_ok = False
        _eleven_reason = f"TTS_PROVIDER={TTS_PROVIDER}"
        return
    if not ELEVEN_KEY:
        _eleven_ok = False
        _eleven_reason = "no ELEVENLABS_API_KEY"
        return
    if not ELEVEN_VOICE:
        _eleven_ok = False
        _eleven_reason = "no ELEVENLABS_VOICE_ID"
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/models", headers={"xi-api-key": ELEVEN_KEY}
            )
            resp.raise_for_status()
            models = resp.json()
            ids = {m.get("model_id") for m in models} if isinstance(models, list) else set()
            if ELEVEN_MODEL in ids:
                _eleven_ok = True
                _eleven_reason = "ok"
            else:
                _eleven_ok = False
                _eleven_reason = (
                    f"{ELEVEN_MODEL} not on this key — falling back to Sarvam. "
                    f"available: {sorted(i for i in ids if i)}"
                )
    except Exception as e:
        _eleven_ok = False
        _eleven_reason = f"probe failed ({type(e).__name__}: {e}) — falling back to Sarvam"


async def _elevenlabs(text: str) -> tuple[bytes | None, str | None]:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
    headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
    params = {"output_format": "mp3_44100_128"}
    body = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.5,
            "use_speaker_boost": True,
        },
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(url, headers=headers, params=params, json=body)
        resp.raise_for_status()
        return resp.content, "audio/mpeg"


async def _sarvam(text: str) -> tuple[bytes | None, str | None]:
    if not SARVAM_KEY:
        return None, None
    url = "https://api.sarvam.ai/text-to-speech"
    headers = {"api-subscription-key": SARVAM_KEY, "Content-Type": "application/json"}
    body = {
        "inputs": [text[:480]],  # Bulbul caps input length
        "target_language_code": "te-IN",
        "model": SARVAM_TTS_MODEL,
        "speaker": SARVAM_TTS_SPEAKER,
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        j = resp.json()
        audios = j.get("audios") or []
        if not audios:
            return None, None
        return base64.b64decode(audios[0]), "audio/wav"


async def synthesize(text: str) -> tuple[bytes | None, str | None]:
    text = (text or "").strip()
    if not text or TTS_PROVIDER == "none":
        return None, None

    # Lazy probe (serverless cold start may not have run the startup hook).
    if _eleven_ok is None:
        await probe_elevenlabs()

    # Preferred: ElevenLabs (if probe said it's usable)
    if TTS_PROVIDER == "elevenlabs" and _eleven_ok:
        try:
            audio, mime = await _elevenlabs(text)
            if audio:
                return audio, mime
        except Exception:
            pass  # fall through to Sarvam

    # Fallback / explicit Sarvam
    try:
        return await _sarvam(text)
    except Exception:
        return None, None
