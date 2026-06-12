"""Speech-to-text via Sarvam AI.

Primary: Saaras v3 with code-mix mode (Telugu + English "Tenglish"), tuned for short
phone-style clips. If the configured model/endpoint is rejected, it retries once with
Saarika (plain te-IN transcription) so a key change in Sarvam's API doesn't break the demo.
The browser sends a 16 kHz mono WAV; we POST it as multipart/form-data.
"""
import os
import httpx

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3").strip()
STT_URL = "https://api.sarvam.ai/speech-to-text"

# Ordered attempts: first the configured model (code-mix), then a robust fallback.
_ATTEMPTS = [
    {"model": SARVAM_STT_MODEL, "extra": {"mode": "codemix"}},
    {"model": "saarika:v2.5", "extra": {}},
]


def stt_available() -> bool:
    return bool(SARVAM_API_KEY)


async def transcribe_wav(wav_bytes: bytes) -> str:
    if not SARVAM_API_KEY:
        raise RuntimeError("SARVAM_API_KEY not set")

    headers = {"api-subscription-key": SARVAM_API_KEY}
    last_err = None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in _ATTEMPTS:
            files = {"file": ("turn.wav", wav_bytes, "audio/wav")}
            data = {"model": attempt["model"], "language_code": "te-IN", **attempt["extra"]}
            try:
                resp = await client.post(STT_URL, headers=headers, files=files, data=data)
                if resp.status_code >= 400:
                    last_err = f"Sarvam STT {resp.status_code} ({attempt['model']}): {resp.text[:300]}"
                    continue
                j = resp.json()
                return (j.get("transcript") or "").strip()
            except Exception as e:  # network / parse — try the next attempt
                last_err = f"{type(e).__name__}: {e}"
                continue
    raise RuntimeError(last_err or "Sarvam STT failed")
