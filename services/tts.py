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
import re
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
ELEVEN_VOICE = _clean("ELEVENLABS_VOICE_ID")                        # English (primary)
ELEVEN_VOICE_HI = _clean("ELEVENLABS_VOICE_ID_HI") or ELEVEN_VOICE  # Hindi — own voice if set
ELEVEN_VOICE_TE = _clean("ELEVENLABS_VOICE_ID_TE") or ELEVEN_VOICE  # Telugu — own voice if set
ELEVEN_MODEL = _clean("ELEVENLABS_MODEL_ID", "eleven_v3")            # Telugu (v3-only language)
# EN/HI ride multilingual_v2: steadier pronunciation (Indian place names!) and faster than v3.
ELEVEN_MODEL_ENHI = _clean("ELEVENLABS_MODEL_ID_ENHI", "eleven_multilingual_v2")


def _model_for(lang: str) -> str:
    return ELEVEN_MODEL if (lang or "").lower() == "telugu" else ELEVEN_MODEL_ENHI


def _voice_for(lang: str) -> str:
    """eleven_v3 is multilingual, but a voice designed for a language sounds best in it —
    each language can have its own voice; unset ones fall back to the primary."""
    l = (lang or "").lower()
    if l == "telugu":
        return ELEVEN_VOICE_TE
    if l == "hindi":
        return ELEVEN_VOICE_HI
    return ELEVEN_VOICE


# 128 kbps mp3 keeps consonants crisp (clarity on phone-grade audio) for a tiny transfer cost
# over 64 kbps. Override with ELEVENLABS_OUTPUT_FORMAT if a plan needs a different format.
_OUTPUT_FORMAT = _clean("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")


def _voice_settings_for(lang: str) -> dict:
    """Tuned for a CLEAR, WARM, PROFESSIONAL read — not theatrical.
      • high similarity_boost  → keeps the chosen voice's own timbre (sweet, recognisable)
      • low style              → removes exaggerated emphasis / accent swings ("too much
                                 prosody" that makes it hard to follow)
      • stability              → steady on multilingual_v2 (EN/HI); eleven_v3 (Telugu) reads
                                 most naturally at 0.5 — pushing it higher there goes flat.
      • use_speaker_boost      → lifts intelligibility.
    Every knob is env-overridable (ELEVENLABS_STABILITY / _SIMILARITY / _STYLE) for quick
    A/B tuning once you hear a call, no code change needed."""
    is_v3 = _model_for(lang) == ELEVEN_MODEL         # eleven_v3 path (Telugu)
    default_stability = "0.5" if is_v3 else "0.6"
    return {
        "stability": _float_env("ELEVENLABS_STABILITY", default_stability),
        "similarity_boost": _float_env("ELEVENLABS_SIMILARITY", "0.85"),
        "style": _float_env("ELEVENLABS_STYLE", "0.1"),
        "use_speaker_boost": True,
    }


def _float_env(name: str, default: str) -> float:
    try:
        return float(_clean(name, default))
    except ValueError:
        return float(default)


# eleven_v3 treats [bracketed] text as performance directions (e.g. [laughs], [whispers]) and
# does NOT read it aloud — strip any such tags so the voice speaks EXACTLY the reply, nothing
# more or less. The short cap avoids touching genuine bracketed content.
_AUDIO_TAG = re.compile(r"\[[^\]\n]{0,30}\]")


def _strip_audio_tags(text: str) -> str:
    return _AUDIO_TAG.sub("", text).strip()


# The English voice mis-reads some names (e.g. "Riya" as "Rye-ya"). Respell them phonetically
# for the SPOKEN audio ONLY — the on-screen transcript keeps the real spelling. Word-boundary
# and case-insensitive; native-script replies are untouched (there the names are रिया / రియా).
# Add more pairs here whenever a word is mispronounced.
_PRONOUNCE = {
    "Riya": "Reeya",
    "Priya": "Preeya",
}
_PRONOUNCE_RE = [(re.compile(r"\b" + re.escape(k) + r"\b", re.I), v) for k, v in _PRONOUNCE.items()]


def _fix_pronunciation(text: str) -> str:
    for rx, repl in _PRONOUNCE_RE:
        text = rx.sub(repl, text)
    return text

SARVAM_KEY = _clean("SARVAM_API_KEY")
SARVAM_TTS_MODEL = _clean("SARVAM_TTS_MODEL", "bulbul:v2")
SARVAM_TTS_SPEAKER = _clean("SARVAM_TTS_SPEAKER", "anushka")

# eleven_v3 is the ONLY ElevenLabs model that speaks Telugu, and it is noticeably SLOW.
# Sarvam Bulbul speaks Telugu natively — faster, and it reads Indic numbers/dates cleanly.
# So Telugu defaults to Sarvam whenever a Sarvam key exists (falls back to eleven_v3 if not).
# Force eleven_v3 with TELUGU_TTS=elevenlabs.
TELUGU_TTS = _clean("TELUGU_TTS", "sarvam").lower()

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


async def _elevenlabs(text: str, lang: str = "english") -> tuple[bytes | None, str | None]:
    """ElevenLabs TTS with key rotation. On quota/auth failure it advances to the next key;
    when every key is exhausted it flips _eleven_ok off so later turns skip the wasted call
    and go straight to Sarvam (no extra latency, and /config reports the truth)."""
    global _eleven_key_idx, _eleven_ok, _eleven_reason
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{_voice_for(lang)}"
    params = {"output_format": _OUTPUT_FORMAT}  # crisp, clear consonants
    body = {
        "text": text,
        "model_id": _model_for(lang),
        # Clear, warm, professional delivery — see _voice_settings_for().
        "voice_settings": _voice_settings_for(lang),
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
        # 401 / quota_exceeded = the KEY is dead (flip off). A bare 429 is usually just the
        # free tier's 2-concurrent-request limit — transient, never disable the voice for it.
        quota_fail = resp.status_code == 401 or "quota_exceeded" in last_detail
        if (quota_fail or resp.status_code == 429) and len(_ELEVEN_KEYS) > 1:
            _eleven_key_idx = (_eleven_key_idx + 1) % len(_ELEVEN_KEYS)
            continue
        break
    if quota_fail:
        _eleven_ok = False
        _eleven_reason = "ElevenLabs credits exhausted (all keys) — speaking with Sarvam fallback"
    elif resp.status_code in (402, 404):
        # voice_not_found / paid_plan_required — this voice can never work on this key, so
        # disable after ONE failed try instead of paying the dead round-trip on every turn.
        _eleven_ok = False
        _eleven_reason = f"ElevenLabs voice unusable ({resp.status_code}) — speaking with Sarvam fallback"
    raise RuntimeError(f"ElevenLabs failed: {last_detail}")


_SARVAM_LANG = {"english": "en-IN", "hindi": "hi-IN", "telugu": "te-IN"}


async def _sarvam(text: str, lang: str = "english") -> tuple[bytes | None, str | None]:
    if not SARVAM_KEY:
        return None, None
    url = "https://api.sarvam.ai/text-to-speech"
    headers = {"api-subscription-key": SARVAM_KEY, "Content-Type": "application/json"}
    body = {
        "inputs": [text[:480]],  # Bulbul caps input length
        "target_language_code": _SARVAM_LANG.get((lang or "").lower(), "en-IN"),
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


async def synthesize(text: str, lang: str = "english") -> tuple[bytes | None, str | None]:
    text = _fix_pronunciation(_strip_audio_tags((text or "").strip()))
    if not text or TTS_PROVIDER == "none":
        return None, None

    lng = (lang or "").lower()

    # Lazy probe (serverless cold start may not have run the startup hook).
    if _eleven_ok is None:
        await probe_elevenlabs()

    # Telugu: prefer Sarvam Bulbul — faster than the slow eleven_v3 and native to the language
    # (also reads numbers/dates more cleanly). Only when a Sarvam key exists; else fall through
    # to ElevenLabs v3 as before.
    if lng == "telugu" and TELUGU_TTS == "sarvam" and SARVAM_KEY:
        try:
            audio, mime = await _sarvam(text, lang)
            if audio:
                return audio, mime
        except Exception:
            pass  # fall through to ElevenLabs v3

    # Preferred: ElevenLabs (if probe said it's usable)
    if TTS_PROVIDER == "elevenlabs" and _eleven_ok:
        try:
            audio, mime = await _elevenlabs(text, lang)
            if audio:
                return audio, mime
        except Exception:
            pass  # fall through to Sarvam

    # Fallback / explicit Sarvam
    try:
        return await _sarvam(text, lang)
    except Exception:
        return None, None
