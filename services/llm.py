"""The agent brain: Google Gemini with function-calling, scenario-aware.

gemini_turn() appends the user's utterance to the running conversation, calls Gemini with the
active scenario's system prompt + tools (qualify_lead / log_payment_outcome /
book_appointment + log_enquiry), runs the matching handler (which writes the CRM row and
pushes it to the page), and returns the agent's reply. `contents` is mutated in place.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from . import _http
from .prompts import build_system_prompt, tools_for, norm_lang, COLLECTION_CASE, CLINIC_HOURS, LEAD_CASE

def _clean(name: str, default: str = "") -> str:
    """Read an env var, removing BOM/zero-width chars plus quotes/whitespace."""
    v = os.getenv(name, default) or ""
    for ch in (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D)):
        v = v.replace(ch, "")
    return v.strip().strip('"').strip("'").strip()


def _load_keys() -> list[str]:
    """Gather Gemini API keys for rotation: a comma-separated GEMINI_API_KEYS, plus the
    numbered GEMINI_API_KEY / GEMINI_API_KEY_2 … GEMINI_API_KEY_30 vars (add more keys by
    just adding env vars — no code change). Deduped, empties dropped."""
    raw = []
    combo = _clean("GEMINI_API_KEYS")
    if combo:
        raw += [p.strip() for p in combo.split(",")]
    raw.append(_clean("GEMINI_API_KEY"))
    for n in range(2, 31):
        raw.append(_clean(f"GEMINI_API_KEY_{n}"))
    out, seen = [], set()
    for k in raw:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_KEYS = _load_keys()
_key_idx = 0   # round-robins one step per request (spreads load) + advances on quota/invalid

# Sanitize the model: fall back to a known-good id if the env value is empty or garbled.
# Default is "gemini-flash-latest" (NOT the -lite tier): far more capable — thinks better,
# handles tricky/emotional replies maturely, sounds human instead of a reflexive bot. It's a
# rolling alias, so it stays valid across all 12 mixed-age keys (pinned ids get retired for
# newer accounts). Set GEMINI_MODEL to override.
_raw_model = _clean("GEMINI_MODEL")
GEMINI_MODEL = _raw_model if re.fullmatch(r"gemini-[A-Za-z0-9.\-]+", _raw_model) else "gemini-flash-latest"
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Let the model THINK briefly before it answers — this is what turns a reflexive, bot-like
# reply into a considered, wise one (understand the intent, then respond). Costs a little
# latency. Tune with GEMINI_THINKING_BUDGET (0 = off, back to instant snap replies).
def _int_env(name: str, default: int) -> int:
    try:
        return int(_clean(name, str(default)) or default)
    except ValueError:
        return default


_THINKING_BUDGET = max(0, _int_env("GEMINI_THINKING_BUDGET", 512))
# When thinking is on, the visible answer shares the token pool with the thinking, so give it
# generous headroom (the prompt still keeps the spoken reply to one short sentence).
_MAX_OUTPUT_TOKENS = max(1024, _THINKING_BUDGET + 512) if _THINKING_BUDGET > 0 else 220

# Fields each tool needs before it may fire; the server enforces this even if the model rushes.
_REQUIRED_BY_TOOL = {
    "qualify_lead": ("status",),
    "log_payment_outcome": ("outcome",),
    "book_appointment": ("name", "phone", "service", "date", "time"),
    "log_enquiry": ("topic",),
}


def _reask(lang: str) -> str:
    """Generic 'sorry, could you say that again?' in the scenario's language."""
    return {
        "telugu": "క్షమించండి అండి, ఒక్కసారి మళ్ళీ చెప్తారా?",
        "hindi": "माफ़ कीजिए जी, एक बार फिर बता दीजिए?",
    }.get(lang, "Sorry, could you say that again?")


_MONTHS = {
    "english": ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"],
    "hindi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर",
              "अक्टूबर", "नवंबर", "दिसंबर"],
    "telugu": ["జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్", "జూలై", "ఆగస్టు",
               "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్"],
}
_DAYPART = {  # index by 0=morning 1=afternoon 2=evening 3=night
    "hindi": ["सुबह", "दोपहर", "शाम", "रात"],
    "telugu": ["ఉదయం", "మధ్యాహ్నం", "సాయంత్రం", "రాత్రి"],
}


def _humanize_when(date_iso: str, time_24: str, lang: str) -> str:
    """Turn a raw ISO date (2026-07-17) + 24h time (11:00) into a natural SPOKEN phrase, so the
    voice never reads dashes/year/colons. E.g. telugu → '17 జూలై, ఉదయం 11 గంటలకు',
    hindi → '17 जुलाई, सुबह 11 बजे', english → '17 July at 11 AM'. On a parse failure it
    returns whatever parsed (or ''), never the raw ISO string."""
    lang = lang if lang in _MONTHS else "english"
    out_date = ""
    try:
        y, mo, day = date_iso.split("-")
        mo, day = int(mo), int(day)
        if 1 <= mo <= 12:
            out_date = f"{day} {_MONTHS[lang][mo - 1]}"
    except Exception:
        out_date = ""

    out_time = ""
    try:
        hh, mm = time_24.split(":")[:2]
        hh, mm = int(hh), int(mm)
        h12 = hh % 12 or 12
        if lang == "english":
            ampm = "AM" if hh < 12 else "PM"
            out_time = f"{h12}:{mm:02d} {ampm}" if mm else f"{h12} {ampm}"
        else:
            part = _DAYPART[lang][0 if hh < 12 else 1 if hh < 16 else 2 if hh < 20 else 3]
            if lang == "hindi":
                out_time = f"{part} {h12} बजकर {mm} मिनट" if mm else f"{part} {h12} बजे"
            else:  # telugu
                out_time = f"{part} {h12} గంటల {mm} నిమిషాలకు" if mm else f"{part} {h12} గంటలకు"
    except Exception:
        out_time = ""

    if out_date and out_time:
        return f"{out_date} at {out_time}" if lang == "english" else f"{out_date}, {out_time}"
    return out_date or out_time


def _fallback_for(tool: str | None, args: dict | None, lang: str = "english") -> str:
    """Tailored confirmation for a successful tool call, in the CHOSEN language — every tool ×
    every language (also the fallback if a follow-up generation fails AFTER the tool already
    saved). Built locally from the tool args, so it is instant and never needs a second Gemini
    call."""
    a = args or {}
    lang = lang if lang in ("english", "hindi", "telugu") else "english"
    name = str(a.get("name") or "").strip()

    if tool == "qualify_lead":
        status = str(a.get("status") or "").strip().lower()
        area = str(a.get("area") or "").strip()
        if status == "not_interested":
            if lang == "hindi":
                return "कोई बात नहीं जी, आपके समय के लिए धन्यवाद — मन बदले तो हम एक कॉल दूर हैं।"
            if lang == "telugu":
                return "పర్వాలేదు అండి, ధన్యవాదాలు — మనసు మారితే ఒక్క కాల్ చేయండి."
            return "No problem, thank you for your time — we're just a call away if you change your mind."
        if status == "call_later":
            if lang == "hindi":
                return "ज़रूर जी — हम बाद में कॉल कर लेंगे। धन्यवाद!"
            if lang == "telugu":
                return "తప్పకుండా అండి — తర్వాత కాల్ చేస్తాము. ధన్యవాదాలు!"
            return "Of course — we'll call you back later. Thank you!"
        # interested
        if lang == "hindi":
            ar = f"{area} में " if area else ""
            return (f"{ar}आपके बजट में बेहद खूबसूरत ऑप्शन हैं — हमारी प्रॉपर्टी टीम जल्द आपसे "
                    "संपर्क करेगी। और कुछ जानना चाहेंगे जी?")
        if lang == "telugu":
            ar = f"{area} లో " if area else ""
            return (f"{ar}మీ బడ్జెట్ లో చాలా అందమైన ఆప్షన్స్ ఉన్నాయి అండి — మా ప్రాపర్టీ టీమ్ "
                    "త్వరలో సంప్రదిస్తుంది. ఇంకేమైనా తెలుసుకోవాలా అండి?")
        ar = f" in {area}" if area else ""
        return (f"There are beautiful options{ar} at that budget — our property team will "
                "share them shortly. Anything else you'd like to know?")

    if tool == "log_enquiry":
        if lang == "hindi":
            return "ज़रूर जी — जब चाहें कॉल कीजिए, अपॉइंटमेंट तुरंत बुक हो जाएगी।"
        if lang == "telugu":
            return "తప్పకుండా అండి — ఎప్పుడైనా కాల్ చేయండి, అపాయింట్‌మెంట్ వెంటనే బుక్ చేస్తాను."
        return "Anytime — call us whenever you like and I'll book you in."

    if tool == "log_payment_outcome":
        outcome = str(a.get("outcome") or "").strip().lower()
        ptp = str(a.get("ptp_date") or "").strip()
        if lang == "hindi":
            if outcome == "promise_to_pay":
                dt = f" {ptp} को" if ptp else ""
                return f"बहुत बढ़िया जी —{dt} पेमेंट नोट कर लिया, लिंक व्हाट्सऐप पर भेज रही हूँ। और कुछ मदद करूँ जी?"
            if outcome == "already_paid":
                return "जी — नोट कर लिया, टीम पेमेंट वेरीफाई कर लेगी। और कुछ मदद करूँ जी?"
            if outcome == "needs_time":
                dt = f"{ptp} तक कर दीजिएगा — " if ptp else ""
                return f"कोई बात नहीं जी। {dt}लिंक व्हाट्सऐप पर रहेगा। और कुछ मदद करूँ जी?"
            if outcome == "dispute":
                return "खेद है जी — नोट कर लिया, हमारे अधिकारी जल्द संपर्क करेंगे। धन्यवाद।"
            if outcome == "callback_requested":
                return "ज़रूर जी, हमारे अधिकारी आपको कॉल कर लेंगे। और कुछ मदद करूँ जी?"
            if outcome == "declined":
                return "कोई बात नहीं जी, मैं समझती हूँ — बिलकुल कोई दबाव नहीं। आपका दिन शुभ हो, धन्यवाद जी।"
            return "ठीक है जी, कोई दबाव नहीं — जब सुविधा हो तब लिंक व्हाट्सऐप पर मौजूद रहेगा। धन्यवाद जी!"
        if lang == "telugu":
            if outcome == "promise_to_pay":
                dt = f" {ptp} కి" if ptp else ""
                return f"చాలా మంచిది అండి —{dt} పేమెంట్ నోట్ చేశాను, లింక్ వాట్సాప్ లో పంపిస్తున్నాను. ఇంకేమైనా సహాయం కావాలా అండి?"
            if outcome == "already_paid":
                return "సరే అండి — నోట్ చేశాను, మా టీమ్ వెరిఫై చేస్తుంది. ఇంకేమైనా సహాయం కావాలా అండి?"
            if outcome == "needs_time":
                dt = f"{ptp} లోపు చేసేయండి — " if ptp else ""
                return f"పర్వాలేదు అండి. {dt}లింక్ వాట్సాప్ లో ఉంటుంది. ఇంకేమైనా సహాయం కావాలా అండి?"
            if outcome == "dispute":
                return "క్షమించండి అండి — నోట్ చేశాను, మా ఆఫీసర్ త్వరలో కాల్ చేస్తారు."
            if outcome == "callback_requested":
                return "తప్పకుండా అండి, మా ఆఫీసర్ మీకు కాల్ చేస్తారు. ఇంకేమైనా సహాయం కావాలా అండి?"
            if outcome == "declined":
                return "పర్వాలేదు అండి, నేను అర్థం చేసుకుంటాను — ఎలాంటి ఒత్తిడి లేదు. మీ రోజు బాగుండాలి, ధన్యవాదాలు అండి."
            return "సరే అండి, ఎలాంటి ఒత్తిడి లేదు — వీలైనప్పుడు లింక్ వాట్సాప్ లో ఉంటుంది. ధన్యవాదాలు అండి!"
        if outcome == "promise_to_pay":
            dt = f" for {ptp}" if ptp else ""
            return f"Noted{dt} — the payment link is on its way on WhatsApp. Anything else I can help with?"
        if outcome == "already_paid":
            return "Noted — our team will verify the payment. Anything else I can help with?"
        if outcome == "needs_time":
            dt = f"pay by {ptp} if you can — " if ptp else ""
            return f"No problem at all — {dt}the link will stay on WhatsApp. Anything else I can help with?"
        if outcome == "dispute":
            return "I'm sorry for the trouble — noted; an officer will call you shortly."
        if outcome == "callback_requested":
            return "Of course — one of our officers will call you. Anything else I can help with?"
        if outcome == "declined":
            return "That's completely alright — I understand, and there's no pressure at all. Have a good day, thank you."
        return "Alright, no pressure at all — whenever it's convenient, the link will be on WhatsApp. Thank you!"

    if tool == "book_appointment":
        service = str(a.get("service") or "appointment").strip()
        d, t = str(a.get("date") or "").strip(), str(a.get("time") or "").strip()
        when = _humanize_when(d, t, lang)   # natural spoken date/time, never raw ISO/24h
        if lang == "hindi":
            who = f"{name} जी, " if name else ""
            dt = f" {when}" if when else ""
            return f"{who}आपकी {service} अपॉइंटमेंट{dt} कन्फर्म हो गई — जानकारी व्हाट्सऐप पर आएगी। और कुछ मदद करूँ जी?"
        if lang == "telugu":
            who = f"{name} గారు, " if name else ""
            dt = f" {when}" if when else ""
            return f"{who}మీ {service} అపాయింట్‌మెంట్{dt} కన్ఫర్మ్ అయ్యింది — వివరాలు వాట్సాప్ లో వస్తాయి. ఇంకేమైనా సహాయం కావాలా అండి?"
        who = f"{name}, " if name else ""
        dt = f" for {when}" if when else ""
        return f"{who}your {service} appointment is confirmed{dt} — details on WhatsApp. Anything else I can help with?"

    return {"telugu": "సరే అండి, అయ్యింది.", "hindi": "ठीक है जी, हो गया।"}.get(lang, "Done.")


_JUNK_NAMES = {"n/a", "na", "none", "null", "unknown", "customer", "guest", "test", "xxx", "abc"}


def _validate_args(tool: str, args: dict) -> str | None:
    """Deterministic guards the model can't rush past: no appointments in the past, no invented
    placeholder names, no half-heard phone numbers. Returns an error message or None."""
    if tool == "book_appointment":
        d, t = str(args.get("date") or ""), str(args.get("time") or "")
        if d and t:
            try:
                when = datetime.strptime(d + " " + t, "%Y-%m-%d %H:%M").replace(tzinfo=_IST)
                if when < datetime.now(_IST):
                    return (f"REJECTED: {d} {t} is already in the past — right now it is "
                            f"{_today()}. Tell the customer warmly that this time has already "
                            "passed and offer the nearest upcoming slot.")
                open_h, close_h = CLINIC_HOURS["sunday" if when.weekday() == 6 else "weekday"]
                if not (open_h <= when.hour < close_h):
                    return (f"REJECTED: {t} on {d} is OUTSIDE clinic hours (Mon–Sat 10am–8pm, "
                            "Sunday only 10am–1pm). Tell the customer warmly which hours apply "
                            "to that day and offer the nearest slot inside the timings.")
            except ValueError:
                pass
    if tool == "book_appointment":
        nm = str(args.get("name") or "").strip()
        if len(nm) < 2 or nm.lower() in _JUNK_NAMES:
            return ("Invalid name — you must ASK the caller for their real name. Never invent "
                    "one or use a placeholder.")
        digits = re.sub(r"\D", "", str(args.get("phone") or ""))
        if len(digits) < 10:
            return ("Phone number incomplete — ask the caller for their full 10-digit mobile "
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
    """Rotate to the next key on quota (429), key-permission errors, or a per-key model
    retirement (404 'no longer available to new users' — other keys may still have it)."""
    if status in (429, 404):
        return True
    if status in (400, 403):
        t = (text or "").upper()
        return any(s in t for s in ("API_KEY_INVALID", "API KEY NOT VALID", "QUOTA", "PERMISSION_DENIED"))
    return False


async def _generate(contents: list, scenario: str = "lead", lang: str = "") -> dict:
    global _key_idx
    if not _KEYS:
        raise RuntimeError("No Gemini API key set")
    body = {
        "systemInstruction": {"parts": [{"text": build_system_prompt(_today(), scenario, lang)}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tools_for(scenario)}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        # The spoken reply stays ONE short sentence (enforced by the prompt); the token cap is
        # generous only so the model's brief THINKING isn't truncated.
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": _MAX_OUTPUT_TOKENS},
    }
    if "2.5" in GEMINI_MODEL or GEMINI_MODEL.endswith("-latest"):
        # Think a little before answering → considered, human replies instead of snap ones.
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": _THINKING_BUDGET}
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


async def gemini_turn(contents: list, user_text: str, handlers: dict, scenario: str = "lead",
                      lang: str = "") -> str:
    """Run one customer turn.

    handlers: {tool_name: async fn(args)->saved_row_dict}. Returns the agent's reply text.
    `scenario` (lead/collections/clinic) selects the persona and tool set; `lang`
    (english/hindi/telugu) selects the spoken language — empty falls back to the scenario's
    showcase default.
    """
    lang = norm_lang(lang, scenario)
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    last_tool, last_args = None, None

    for _ in range(5):  # allow a couple of tool round-trips
        try:
            data = await _generate(contents, scenario, lang)
        except Exception:
            # If a tool already saved this turn, give a graceful spoken confirmation instead
            # of surfacing a raw error (e.g. when the follow-up call hits a Gemini 429).
            if last_tool:
                return _fallback_for(last_tool, last_args, lang)
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
            if name == "log_payment_outcome":   # the case facts are known — backfill defaults
                args.setdefault("customer_name", COLLECTION_CASE["customer"])
                args.setdefault("loan_ref", COLLECTION_CASE["loan_ref"])
                args.setdefault("amount", COLLECTION_CASE["amount"])
            if name == "qualify_lead":          # the lead's identity is known from the enquiry
                args.setdefault("name", LEAD_CASE["name"])
                args.setdefault("phone", LEAD_CASE["phone"])
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
                    "message": "Could not save the record. Apologise briefly in the caller's "
                    "language and offer to note the details again.",
                }
                contents.append({"role": "user",
                                 "parts": [{"functionResponse": {"name": name, "response": response}}]})
                continue  # let the model explain / recover

            # SUCCESS — speak a tailored confirmation built locally and SKIP the second Gemini
            # call (flash-lite usually returns empty text here anyway). Halves the LLM latency
            # and quota on every tool turn.
            last_tool, last_args = name, args
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name": name, "response": {"status": "success", "id": row.get("id")}}}]})
            spoken = _fallback_for(name, args, lang)
            contents.append({"role": "model", "parts": [{"text": spoken}]})
            return spoken

        final = "".join(text_chunks).strip()
        # flash-lite sometimes parrots internal "(System note …)" instructions into its reply —
        # strip them so they are never shown or spoken to the customer.
        final = re.sub(r"\(System[^)]*\)", "", final).strip()
        if not final:
            final = (
                _fallback_for(last_tool, last_args, lang)
                if last_tool
                else _reask(lang)
            )
        return final

    return _reask(lang)
