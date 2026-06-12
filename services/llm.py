"""The agent brain: Google Gemini 2.5 Flash with function-calling.

gemini_turn() appends the user's utterance to the running conversation, calls Gemini, and
if the model invokes a tool (create_booking / log_complaint) it runs the matching handler
(which writes to the DB and pushes the row to the page), feeds the result back to Gemini, and
returns the model's spoken reply. `contents` is mutated in place to persist history.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

import httpx

from .prompts import (
    build_system_prompt,
    CREATE_BOOKING_TOOL,
    LOG_COMPLAINT_TOOL,
    CREATE_ORDER_TOOL,
    UPDATE_ORDER_TOOL,
)

def _clean(name: str, default: str = "") -> str:
    """Read an env var, removing BOM/zero-width chars plus quotes/whitespace."""
    v = os.getenv(name, default) or ""
    for ch in (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D)):
        v = v.replace(ch, "")
    return v.strip().strip('"').strip("'").strip()


def _load_keys() -> list[str]:
    """Gather Gemini API keys for rotation: a comma-separated GEMINI_API_KEYS, plus the
    numbered GEMINI_API_KEY / GEMINI_API_KEY_2.. vars. Deduped, empties dropped."""
    raw = []
    combo = _clean("GEMINI_API_KEYS")
    if combo:
        raw += [p.strip() for p in combo.split(",")]
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
                 "GEMINI_API_KEY_4", "GEMINI_API_KEY_5"):
        raw.append(_clean(name))
    out, seen = [], set()
    for k in raw:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_KEYS = _load_keys()
_key_idx = 0   # index of the current key; advances on quota/invalid errors and persists

# Sanitize the model: fall back to a known-good id if the env value is empty or garbled.
_raw_model = _clean("GEMINI_MODEL")
GEMINI_MODEL = _raw_model if re.fullmatch(r"gemini-[A-Za-z0-9.\-]+", _raw_model) else "gemini-2.5-flash-lite"
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Fields each tool needs before it may fire; the server enforces this even if the model rushes.
_REQUIRED_BY_TOOL = {
    "create_booking": ("name", "phone", "party_size", "date", "time"),
    "log_complaint": ("name", "phone", "issue"),
    "create_order": ("name", "phone", "items"),
    "update_order": ("phone", "items"),
}
_SUCCESS_MSG = {
    "create_booking": "Booking saved. Now warmly confirm to the customer in spoken Telugu and "
    "mention the WhatsApp confirmation.",
    "log_complaint": "Complaint logged. Apologise warmly in Telugu, then tell the customer a "
    "WhatsApp message is coming and ask them to send a photo of the problem there, and that the "
    "team will contact them.",
    "create_order": "Order placed. In Telugu: read the items back; if dine-in/pickup say it'll "
    "be ready in about ముప్పై నిమిషాల్లో, if delivery say updates come on WhatsApp; then say a "
    "payment link is coming on WhatsApp and they can pay via it or cash on delivery. Do NOT "
    "state a rupee total.",
    "update_order": "Order updated. Confirm the change warmly in Telugu with the new total in "
    "Telugu words + రూపాయలు.",
}
# Spoken even if the follow-up generation fails (e.g. Gemini 429) AFTER the tool already saved.
_FALLBACK_CONFIRM = {
    "create_booking": "Table book అయ్యింది అండి 🙏 Confirmation details WhatsApp లో పంపిస్తాను, ధన్యవాదాలు!",
    "log_complaint": "చాలా క్షమించండి అండి… మీకు WhatsApp లో message వస్తుంది, ఆ photo అక్కడ పంపండి, మా team త్వరగా మిమ్మల్ని contact చేస్తుంది.",
    "create_order": "మీ order తీసుకున్నాను అండి 🙏 Payment link WhatsApp లో పంపిస్తాను, దాని ద్వారా pay చేయొచ్చు లేదా order వచ్చినప్పుడు cash on delivery కూడా చేయొచ్చు. ధన్యవాదాలు!",
    "update_order": "మీ order update చేశాను అండి 🙏 కొత్త details WhatsApp లో పంపిస్తాను.",
}


def _fallback_for(tool: str | None, args: dict | None) -> str:
    """Spoken line used when the model adds no text after a tool call (common on flash-lite).
    For orders it tailors the line to the order type so dine-in/pickup still hear ~30 min."""
    if tool == "create_order":
        ot = ((args or {}).get("order_type") or "").lower()
        if ot in ("dinein", "pickup"):
            ready = "మీ order తీసుకున్నాను అండి, సుమారు ముప్పై నిమిషాల్లో ready అవుతుంది. "
        else:
            ready = "మీ order తీసుకున్నాను అండి, delivery updates WhatsApp లో పంపిస్తాను. "
        return (ready + "Payment link కూడా WhatsApp లో వస్తుంది — దాని ద్వారా pay చేయొచ్చు లేదా "
                "order వచ్చినప్పుడు cash on delivery చేయొచ్చు. ధన్యవాదాలు! 🙏")
    return _FALLBACK_CONFIRM.get(tool, "సరే అండి, అయ్యింది.")


def llm_available() -> bool:
    return bool(_KEYS)


def key_count() -> int:
    return len(_KEYS)


def _today() -> str:
    return datetime.now().strftime("%A, %Y-%m-%d")


def _should_rotate(status: int, text: str) -> bool:
    """Rotate to the next key on quota (429) or key-permission errors."""
    if status == 429:
        return True
    if status in (400, 403):
        t = (text or "").upper()
        return any(s in t for s in ("API_KEY_INVALID", "API KEY NOT VALID", "QUOTA", "PERMISSION_DENIED"))
    return False


async def _generate(contents: list) -> dict:
    global _key_idx
    if not _KEYS:
        raise RuntimeError("No Gemini API key set")
    body = {
        "systemInstruction": {"parts": [{"text": build_system_prompt(_today())}]},
        "contents": contents,
        "tools": [
            {
                "functionDeclarations": [
                    CREATE_BOOKING_TOOL,
                    LOG_COMPLAINT_TOOL,
                    CREATE_ORDER_TOOL,
                    UPDATE_ORDER_TOOL,
                ]
            }
        ],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 800},
    }
    url = _URL.format(model=GEMINI_MODEL)
    last_err = None
    async with httpx.AsyncClient(timeout=40) as client:
        # Try keys starting at the current one; rotate past any that are quota'd/invalid.
        for _ in range(len(_KEYS)):
            resp = await client.post(url, params={"key": _KEYS[_key_idx]}, json=body)
            if resp.status_code < 400:
                return resp.json()
            last_err = f"Gemini {resp.status_code} (key {_key_idx + 1}/{len(_KEYS)}): {resp.text[:160]}"
            if _should_rotate(resp.status_code, resp.text):
                _key_idx = (_key_idx + 1) % len(_KEYS)
                continue
            raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("All Gemini keys exhausted — " + (last_err or "quota/invalid"))


async def gemini_turn(contents: list, user_text: str, handlers: dict) -> str:
    """Run one customer turn.

    handlers: {tool_name: async fn(args)->saved_row_dict}. Returns the assistant's spoken text.
    """
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    last_tool, last_args = None, None

    for _ in range(5):  # allow a couple of tool round-trips
        try:
            data = await _generate(contents)
        except Exception:
            # If a tool already saved this turn, give a graceful spoken confirmation instead
            # of surfacing a raw error (e.g. when the follow-up call hits a Gemini 429).
            if last_tool:
                return _fallback_for(last_tool, last_args)
            raise
        candidates = data.get("candidates") or []
        if not candidates:
            break
        parts = (candidates[0].get("content") or {}).get("parts") or []

        text_chunks, fcall = [], None
        for p in parts:
            if "text" in p:
                text_chunks.append(p["text"])
            if "functionCall" in p:
                fcall = p["functionCall"]

        # Persist the model's turn (echo functionCall back so Gemini keeps the thread).
        contents.append({"role": "model", "parts": parts})

        if fcall and fcall.get("name") in handlers:
            name = fcall["name"]
            args = dict(fcall.get("args") or {})
            missing = [k for k in _REQUIRED_BY_TOOL.get(name, ()) if not args.get(k)]
            if missing:
                response = {
                    "status": "error",
                    "message": "Missing " + ", ".join(missing)
                    + ". Politely ask the customer for these before proceeding.",
                }
            else:
                row = await handlers[name](args)
                if row is None:
                    response = {
                        "status": "error",
                        "message": "Could not find a matching record. Tell the customer politely "
                        "in Telugu and offer to help (e.g. place a new order).",
                    }
                else:
                    last_tool, last_args = name, args
                    response = {
                        "status": "success",
                        "id": row.get("id"),
                        "message": _SUCCESS_MSG.get(name, "Done."),
                    }
            contents.append(
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": response}}],
                }
            )
            continue  # loop again to get the spoken reply

        final = "".join(text_chunks).strip()
        if not final:
            final = (
                _fallback_for(last_tool, last_args)
                if last_tool
                else "క్షమించండి అండి, మీరు చెప్పింది ఒక్కసారి మళ్ళీ చెప్తారా?"
            )
        return final

    return "క్షమించండి అండి, ఒక్కసారి మళ్ళీ చెప్తారా?"
