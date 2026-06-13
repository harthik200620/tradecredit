"""The agent brain: Google Gemini 2.5 Flash with function-calling.

gemini_turn() appends the user's utterance to the running conversation, calls Gemini, and
if the model invokes a tool (create_booking / log_complaint) it runs the matching handler
(which writes to the DB and pushes the row to the page), feeds the result back to Gemini, and
returns the model's spoken reply. `contents` is mutated in place to persist history.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from . import _http
from .prompts import (
    build_system_prompt,
    CREATE_BOOKING_TOOL,
    UPDATE_BOOKING_TOOL,
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
                 "GEMINI_API_KEY_4", "GEMINI_API_KEY_5", "GEMINI_API_KEY_6",
                 "GEMINI_API_KEY_7", "GEMINI_API_KEY_8", "GEMINI_API_KEY_9",
                 "GEMINI_API_KEY_10", "GEMINI_API_KEY_11", "GEMINI_API_KEY_12"):
        raw.append(_clean(name))
    out, seen = [], set()
    for k in raw:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_KEYS = _load_keys()
_key_idx = 0   # round-robins one step per request (spreads load) + advances on quota/invalid

# Sanitize the model: fall back to a known-good id if the env value is empty or garbled.
_raw_model = _clean("GEMINI_MODEL")
GEMINI_MODEL = _raw_model if re.fullmatch(r"gemini-[A-Za-z0-9.\-]+", _raw_model) else "gemini-2.5-flash-lite"
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Fields each tool needs before it may fire; the server enforces this even if the model rushes.
_REQUIRED_BY_TOOL = {
    "create_booking": ("name", "phone", "party_size", "date", "time"),
    "update_booking": ("phone",),
    "log_complaint": ("name", "phone", "issue"),
    "create_order": ("name", "phone", "items"),
    "update_order": ("phone",),
}
_SUCCESS_MSG = {
    "create_booking": "Booking saved. Now warmly confirm to the customer in spoken Telugu and "
    "mention the WhatsApp confirmation.",
    "log_complaint": "Complaint logged. Apologise warmly in Telugu, then tell the customer a "
    "WhatsApp message is coming and ask them to send a photo of the problem there, and that the "
    "team will contact them.",
    "create_order": "Order placed. In Telugu: read the items back; if dine-in/pickup say it'll "
    "be ready in about ముప్పై నిమిషాల్లో, if delivery say updates come on WhatsApp; then confirm "
    "the payment they chose — if prepaid say the WhatsApp payment link is coming, if cod say they "
    "can pay cash when it arrives. Do NOT state a rupee total.",
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


_PARTY_TE = {1: "ఒక్కరికి", 2: "ఇద్దరికి", 3: "ముగ్గురికి", 4: "నలుగురికి", 5: "ఐదుగురికి",
             6: "ఆరుగురికి", 7: "ఏడుగురికి", 8: "ఎనిమిది మందికి", 9: "తొమ్మిది మందికి",
             10: "పది మందికి"}


def _fallback_for(tool: str | None, args: dict | None) -> str:
    """The tailored spoken confirmation for a successful tool call (also reused as the fallback
    if a follow-up generation ever returns empty). Built locally from the tool args, so it is
    instant and never needs a second Gemini call."""
    a = args or {}
    name = str(a.get("name") or "").strip()
    who = f"{name} గారు, " if name else ""

    if tool == "create_booking":
        try:
            party = int(a.get("party_size") or 0)
        except (TypeError, ValueError):
            party = 0
        p = _PARTY_TE.get(party, (f"{party} మందికి" if party else ""))
        tail = f" {p}." if p else ""
        return f"{who}మీ table book అయ్యింది అండి!{tail} Details అన్నీ WhatsApp లో పంపిస్తాను 🙏"

    if tool == "update_booking":
        try:
            party = int(a.get("party_size") or 0)
        except (TypeError, ValueError):
            party = 0
        p = _PARTY_TE.get(party, (f"{party} మందికి" if party else ""))
        mid = f" — ఇప్పుడు {p}" if p else ""
        return f"మీ booking update చేశాను అండి{mid}. కొత్త details WhatsApp లో పంపిస్తాను 🙏"

    if tool == "create_order":
        items = str(a.get("items") or "").strip()
        ot = (a.get("order_type") or "").lower()
        pay = (a.get("payment") or "").lower()
        read = f"మీ {items} order తీసుకున్నాను అండి. " if items else "మీ order తీసుకున్నాను అండి. "
        ready = ("సుమారు ముప్పై నిమిషాల్లో ready అవుతుంది. " if ot in ("dinein", "pickup")
                 else "Delivery updates WhatsApp లో పంపిస్తాను. ")
        if pay == "cod":
            payline = "Order వచ్చినప్పుడు cash ఇవ్వొచ్చు. ధన్యవాదాలు! 🙏"
        elif pay == "prepaid":
            payline = "Payment link WhatsApp లో పంపిస్తాను, దాని ద్వారా pay చేయండి. ధన్యవాదాలు! 🙏"
        else:
            payline = ("Payment link WhatsApp లో వస్తుంది — link ద్వారా pay చేయొచ్చు లేదా order "
                       "వచ్చినప్పుడు cash on delivery చేయొచ్చు. ధన్యవాదాలు! 🙏")
        return who + read + ready + payline

    if tool == "update_order":
        items = str(a.get("items") or "").strip()
        if items:
            return f"మీ order update చేశాను అండి — ఇప్పుడు {items}. కొత్త details WhatsApp లో పంపిస్తాను 🙏"
        return "మీ order update చేశాను అండి 🙏 కొత్త details WhatsApp లో పంపిస్తాను."

    if tool == "log_complaint":
        return (who + "చాలా క్షమించండి అండి… మీకు WhatsApp లో message వస్తుంది, ఆ photo అక్కడ "
                "పంపండి, మా team త్వరగా మిమ్మల్ని contact చేస్తుంది.")

    return _FALLBACK_CONFIRM.get(tool, "సరే అండి, అయ్యింది.")


_JUNK_NAMES = {"n/a", "na", "none", "null", "unknown", "customer", "guest", "test", "xxx", "abc"}


def _validate_args(tool: str, args: dict) -> str | None:
    """Deterministic guards the model can't rush past: no bookings in the past, no invented
    placeholder names, no half-heard phone numbers. Returns an error message or None."""
    if tool in ("create_booking", "update_booking"):
        d, t = str(args.get("date") or ""), str(args.get("time") or "")
        if d and t:
            try:
                when = datetime.strptime(d + " " + t, "%Y-%m-%d %H:%M").replace(tzinfo=_IST)
                if when < datetime.now(_IST):
                    return (f"REJECTED: {d} {t} is already in the past — right now it is "
                            f"{_today()}. Tell the customer warmly that this time has already "
                            "passed today and ask if tomorrow at the same time works.")
            except ValueError:
                pass
    if tool in ("create_booking", "create_order", "log_complaint"):
        nm = str(args.get("name") or "").strip()
        if len(nm) < 2 or nm.lower() in _JUNK_NAMES:
            return ("Invalid name — you must ASK the customer for their real name. Never invent "
                    "one or use a placeholder.")
        digits = re.sub(r"\D", "", str(args.get("phone") or ""))
        if len(digits) < 10:
            return ("Phone number incomplete — ask the customer for their full 10-digit mobile "
                    "number before proceeding.")
    return None


def llm_available() -> bool:
    return bool(_KEYS)


def key_count() -> int:
    return len(_KEYS)


_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> str:
    """Current date AND time in Hyderabad (IST) — explicit tz because Vercel runs in UTC."""
    now = datetime.now(_IST)
    return now.strftime("%A, %Y-%m-%d, current time %I:%M %p IST")


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
                    UPDATE_BOOKING_TOOL,
                    LOG_COMPLAINT_TOOL,
                    CREATE_ORDER_TOOL,
                    UPDATE_ORDER_TOOL,
                ]
            }
        ],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        # Replies are 1-2 sentences; a tight cap + thinking off keeps generation fast.
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 300},
    }
    if "2.5" in GEMINI_MODEL:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    url = _URL.format(model=GEMINI_MODEL)
    last_err = None
    client = _http.client()  # shared keep-alive client (no per-call TLS handshake)
    # Round-robin: each new request starts on the NEXT key, so the per-key free-tier rate
    # limit (RPM/RPD) is spread across all keys instead of hammering one until it 429s —
    # effective throughput ≈ single-key limit × number of keys.
    if len(_KEYS) > 1:
        _key_idx = (_key_idx + 1) % len(_KEYS)
    # Then try keys starting there; rotate past any that are quota'd/invalid this turn.
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
                contents.append({"role": "user",
                                 "parts": [{"functionResponse": {"name": name, "response": response}}]})
                continue  # let the model ask for the missing fields

            problem = _validate_args(name, args)
            if problem:
                response = {"status": "error", "message": problem}
                contents.append({"role": "user",
                                 "parts": [{"functionResponse": {"name": name, "response": response}}]})
                continue  # let the model relay the problem and re-collect

            row = await handlers[name](args)
            if row is None:
                response = {
                    "status": "error",
                    "message": "Could not find a matching record. Tell the customer politely "
                    "in Telugu and offer to help (e.g. place a new order).",
                }
                contents.append({"role": "user",
                                 "parts": [{"functionResponse": {"name": name, "response": response}}]})
                continue  # let the model explain / recover

            # SUCCESS — speak a tailored confirmation built locally and SKIP the second Gemini
            # call (flash-lite usually returns empty text here anyway). Halves the LLM latency
            # and quota on every booking / order / complaint turn.
            last_tool, last_args = name, args
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": name, "response": {"status": "success", "id": row.get("id")}}}]})
            spoken = _fallback_for(name, args)
            contents.append({"role": "model", "parts": [{"text": spoken}]})
            return spoken

        final = "".join(text_chunks).strip()
        # flash-lite sometimes parrots internal "(System note …)" instructions into its reply —
        # strip them so they are never shown or spoken to the customer.
        final = re.sub(r"\(System[^)]*\)", "", final).strip()
        if not final:
            final = (
                _fallback_for(last_tool, last_args)
                if last_tool
                else "క్షమించండి అండి, మీరు చెప్పింది ఒక్కసారి మళ్ళీ చెప్తారా?"
            )
        return final

    return "క్షమించండి అండి, ఒక్కసారి మళ్ళీ చెప్తారా?"
