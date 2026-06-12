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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Sanitize the model: trim whitespace/quotes and fall back to a known-good id if the env
# value is empty or garbled (e.g. a stray character from a dashboard bulk-paste).
_raw_model = os.getenv("GEMINI_MODEL", "").strip().strip('"').strip("'").strip()
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
    "create_order": "Order placed. Confirm in Telugu: read the items back, say the total in "
    "Telugu words + రూపాయలు, and mention the WhatsApp follow-up.",
    "update_order": "Order updated. Confirm the change warmly in Telugu with the new total in "
    "Telugu words + రూపాయలు.",
}
# Spoken even if the follow-up generation fails (e.g. Gemini 429) AFTER the tool already saved.
_FALLBACK_CONFIRM = {
    "create_booking": "Table book అయ్యింది అండి 🙏 Confirmation details WhatsApp లో పంపిస్తాను, ధన్యవాదాలు!",
    "log_complaint": "చాలా క్షమించండి అండి… మీకు WhatsApp లో message వస్తుంది, ఆ photo అక్కడ పంపండి, మా team త్వరగా మిమ్మల్ని contact చేస్తుంది.",
    "create_order": "మీ order తీసుకున్నాను అండి 🙏 Details అన్నీ WhatsApp లో పంపిస్తాను, ధన్యవాదాలు!",
    "update_order": "మీ order update చేశాను అండి 🙏 కొత్త details WhatsApp లో పంపిస్తాను.",
}


def llm_available() -> bool:
    return bool(GEMINI_API_KEY)


def _today() -> str:
    return datetime.now().strftime("%A, %Y-%m-%d")


async def _generate(contents: list) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
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
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            _URL.format(model=GEMINI_MODEL),
            params={"key": GEMINI_API_KEY},
            json=body,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:300]}")
        return resp.json()


async def gemini_turn(contents: list, user_text: str, handlers: dict) -> str:
    """Run one customer turn.

    handlers: {tool_name: async fn(args)->saved_row_dict}. Returns the assistant's spoken text.
    """
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    last_tool = None

    for _ in range(5):  # allow a couple of tool round-trips
        try:
            data = await _generate(contents)
        except Exception:
            # If a tool already saved this turn, give a graceful spoken confirmation instead
            # of surfacing a raw error (e.g. when the follow-up call hits a Gemini 429).
            if last_tool:
                return _FALLBACK_CONFIRM.get(last_tool, "సరే అండి, అయ్యింది.")
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
                    last_tool = name
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
                _FALLBACK_CONFIRM.get(last_tool)
                if last_tool
                else "క్షమించండి అండి, మీరు చెప్పింది ఒక్కసారి మళ్ళీ చెప్తారా?"
            )
        return final

    return "క్షమించండి అండి, ఒక్కసారి మళ్ళీ చెప్తారా?"
