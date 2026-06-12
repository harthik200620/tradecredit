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

from . import _http

# Strip BOM / zero-width chars (U+FEFF, U+200B-U+200D) that dashboard bulk-pastes inject
# and that str.strip() does NOT remove. Built via chr() so the source stays pure ASCII.
_JUNK = (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D))


def _clean(name: str, default: str = "") -> str:
    """Read an env var, removing BOM/zero-width chars plus quotes/whitespace."""
    v = os.getenv(name, default) or ""
    for ch in _JUNK:
        v = v.replace(ch, "")
    return v.strip().strip('"').strip("'").strip()


TTS_PROVIDER = _clean("TTS_PROVIDER", "elevenlabs").lower()


def _load_eleven_keys() -> list[str]:
    """ELEVENLABS_API_KEY + _2/_3 (or comma-separated ELEVENLABS_API_KEYS) for rotation —
    free accounts get 10k credits/month, which heavy demo days exhaust. NOTE: the voice
    (ELEVENLABS_VOICE_ID) must be added to EACH account's voice library."""
    raw = []
    combo = _clean("ELEVENLABS_API_KEYS")
    if combo:
        raw += [p.strip() for p in combo.split(",")]
    for name in ("ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY_2", "ELEVENLABS_API_KEY_3"):
        raw.append(_clean(name))
    out, seen = [], set()
    for k in raw:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_ELEVEN_KEYS = _load_eleven_keys()
_eleven_key_idx = 0
ELEVEN_KEY = _ELEVEN_KEYS[0] if _ELEVEN_KEYS else ""
ELEVEN_VOICE = _clean("ELEVENLABS_VOICE_ID")
ELEVEN_MODEL = _clean("ELEVENLABS_MODEL_ID", "eleven_v3")

SARVAM_KEY = _clean("SARVAM_API_KEY")
SARVAM_TTS_MODEL = _clean("SARVAM_TTS_MODEL", "bulbul:v2")
SARVAM_TTS_SPEAKER = _clean("SARVAM_TTS_SPEAKER", "anushka")

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
        resp = await _http.client().get(
            "https://api.elevenlabs.io/v1/models",
            headers={"xi-api-key": ELEVEN_KEY}, timeout=15,
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
    """ElevenLabs TTS with key rotation. On quota/auth failure it advances to the next key;
    when every key is exhausted it flips _eleven_ok off so later turns skip the wasted call
    and go straight to Sarvam (no extra latency, and /config reports the truth)."""
    global _eleven_key_idx, _eleven_ok, _eleven_reason
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}"
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
    last_detail, quota_fail = "", False
    for _ in range(max(1, len(_ELEVEN_KEYS))):
        key = _ELEVEN_KEYS[_eleven_key_idx] if _ELEVEN_KEYS else ELEVEN_KEY
        resp = await _http.client().post(
            url, headers={"xi-api-key": key, "Content-Type": "application/json"},
            params=params, json=body,
        )
        if resp.status_code < 400:
            return resp.content, "audio/mpeg"
        last_detail = resp.text[:200]
        quota_fail = resp.status_code in (401, 429) or "quota_exceeded" in last_detail
        if quota_fail and len(_ELEVEN_KEYS) > 1:
            _eleven_key_idx = (_eleven_key_idx + 1) % len(_ELEVEN_KEYS)
            continue
        break
    if quota_fail:
        _eleven_ok = False
        _eleven_reason = "ElevenLabs credits exhausted (all keys) — speaking with Sarvam fallback"
    raise RuntimeError(f"ElevenLabs failed: {last_detail}")


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
    resp = await _http.client().post(url, headers=headers, json=body)
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
