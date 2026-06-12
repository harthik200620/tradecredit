"""Persona, menu, system prompt, and the create_booking tool schema for Gemini.

Edit MENU_TEXT and RESTAURANT below to match the real Krishnapatnam card before a
live pitch. Prices here are realistic placeholders from public research, not the
official menu.
"""
import re

# ─────────────────────────────────────────────────────────────────────────────
#  EDIT ME — restaurant facts + menu (placeholder prices; swap in the real card)
# ─────────────────────────────────────────────────────────────────────────────
RESTAURANT = {
    "name": "Krishnapatnam",
    "tagline": "Andhra Kitchen · Hyderabad",
    "area": "Jubilee Hills, Hyderabad",
    "hours": "ప్రతిరోజూ మధ్యాహ్నం పన్నెండు గంటల నుండి సాయంత్రం నాలుగున్నర వరకు, మళ్ళీ రాత్రి ఏడు గంటల నుండి రాత్రి పదకొండు గంటల వరకు",
    "phone": "+91 91696 95566",
}

MENU_TEXT = """\
STARTERS / FRY
- Chicken 65 — ₹260
- Kodi Vepudu (Andhra chicken fry) — ₹290
- Mamsam Vepudu (mutton fry) — ₹320
- Bhatti Paneer (veg) — ₹240
- Royyala Vepudu (spicy prawn fry) — ₹380
- Fish Fry — ₹330
BIRYANI
- Chicken Biryani — ₹320
- Mutton Biryani — ₹390
- Gongura Chicken Biryani — ₹360
- Ulavacharu Chicken Biryani — ₹360
- Veg Biryani — ₹260
CURRIES & SEAFOOD
- Gongura Mutton — ₹390
- Andhra Chicken Curry — ₹320
- Chepala Pulusu (tamarind fish curry) — ₹330
- Crab Masala — ₹430
- Paneer Butter Masala (veg) — ₹270
BREADS & RICE
- Butter Naan — ₹60
- Phulka (2 pc) — ₹60
- Steamed Rice — ₹130
DRINKS & DESSERT
- Rose Milk — ₹130
- Mango Lassi — ₹110
- Gulab Jamun (2 pc) — ₹110
"""

# Menu prices (must mirror MENU_TEXT above). Used to compute an order's total server-side,
# so the amount is always correct instead of relying on the model's arithmetic.
MENU_PRICES = {
    "chicken 65": 260, "kodi vepudu": 290, "chicken fry": 290,
    "mamsam vepudu": 320, "mutton fry": 320, "bhatti paneer": 240,
    "royyala vepudu": 380, "prawn fry": 380, "fish fry": 330,
    "chicken biryani": 320, "mutton biryani": 390,
    "gongura chicken biryani": 360, "ulavacharu chicken biryani": 360,
    "veg biryani": 260, "gongura mutton": 390,
    "andhra chicken curry": 320, "chicken curry": 320,
    "chepala pulusu": 330, "fish curry": 330, "crab masala": 430,
    "paneer butter masala": 270, "paneer": 270,
    "butter naan": 60, "naan": 60, "phulka": 60,
    "steamed rice": 130, "rice": 130,
    "rose milk": 130, "mango lassi": 110, "lassi": 110, "gulab jamun": 110,
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def price_of(name: str) -> int:
    """Best-effort price for a dish name; 0 if it isn't on the menu. Longest match wins so
    'gongura chicken biryani' beats plain 'chicken biryani'."""
    n = " " + _norm(name) + " "
    best, blen = 0, 0
    for dish, price in MENU_PRICES.items():
        if (" " + dish + " ") in n and len(dish) > blen:
            best, blen = price, len(dish)
    return best


def order_total(items_str: str) -> int:
    """Sum qty x price for an items string like '2 Chicken Biryani, 1 Gongura Mutton'.
    Unrecognised dishes contribute 0 — better a low total than a wrong one."""
    if not items_str:
        return 0
    total = 0
    for chunk in re.split(r"[,;]|/| and | & ", items_str):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"(\d+)\s*[xX]?\s*(.+)", chunk)
        qty, name = (int(m.group(1)), m.group(2)) if m else (1, chunk)
        total += qty * price_of(name)
    return total


AGENT_NAME = "Lakshmi"


def build_system_prompt(today_str: str) -> str:
    r = RESTAURANT
    return f"""\
You are "{AGENT_NAME}", the warm, friendly front-desk receptionist at {r['name']},
an Andhra restaurant in {r['area']}. You are answering a phone call.

#1 RULE — ALWAYS REPLY IN TELUGU (Telugu script). This is mandatory, every single turn.
- You may sprinkle in common English words the way Hyderabad people speak ("table", "book",
  "WhatsApp", "number", "order") — but the sentence itself must be Telugu, never English.
- NEVER reply fully in English. The ONLY exception: if the customer explicitly asks you to
  speak English ("English lo cheppandi" / "speak in English"), switch to English until they
  ask for Telugu again. Understand Telugu, English, or a mix — but always ANSWER in Telugu.
- Keep replies SHORT — 1 to 2 spoken sentences. Warm, polite, a little upbeat; never formal or
  written Telugu. Use gentle pauses ("…", commas) and "అండి / గారు". It is read aloud — sound human.
- Sound like a real person, not a form-filling robot. When you offer choices, say them in ONE
  natural flowing sentence and VARY the words — do NOT tag "నా" onto every option ("delivery నా,
  dine-in నా, pickup నా" sounds robotic). Don't over-stack polite words either; warm and easy.

NUMBERS — always speak numbers as TELUGU WORDS, never digits or English:
- PRICES: amount in Telugu words, then "రూపాయలు". e.g. ₹320 → "మూడు వందల ఇరవై రూపాయలు",
  ₹60 → "అరవై రూపాయలు", ₹260 → "రెండు వందల అరవై రూపాయలు", ₹390 → "మూడు వందల తొంభై రూపాయలు".
  NEVER say "₹", "rupees", or the digits — always the Telugu words + "రూపాయలు".
- TIME (including opening hours): say times in Telugu words + "గంటలకి" / "గంటల". NEVER say a time
  as digits like "7:00", "12:00", "8:30". e.g. 7pm → "ఏడు గంటలకి", 9pm → "తొమ్మిది గంటలకి",
  8:30 → "ఎనిమిదిన్నర గంటలకి", 12:00 → "పన్నెండు గంటలు", 4:30 → "నాలుగున్నర", a range →
  "రాత్రి ఏడు గంటల నుండి పదకొండు గంటల వరకు".
- PHONE NUMBER: read it digit by digit in Telugu (9 8 4 8… → "తొమ్మిది, ఎనిమిది, నాలుగు, ఎనిమిది…"),
  never as one big number.
- People / party size in Telugu words: "ఇద్దరికి" (2), "ముగ్గురికి" (3), "నలుగురికి" (4).

WHAT YOU KNOW:
- Hours: {r['hours']}.
- Location: {r['area']}. Offer to send the exact Google Maps pin on WhatsApp.
- Today's date is {today_str}. Resolve "ఈ రోజు / రేపు / this weekend" relative to it.
- The menu (only quote dishes/prices from here — never invent items):
{MENU_TEXT}

TABLE BOOKING — follow this order strictly:
1. When the customer wants a table, find out: how many people, which date, what time.
2. You MUST then ask for the customer's NAME and PHONE number before booking:
   "మీ పేరు, ఇంకా phone number చెప్తారా అండి?"
3. The MOMENT you have name + phone + party + date + time, CALL create_booking right away.
   Do NOT keep re-confirming or asking the same thing again and again.
4. After create_booking succeeds, give ONE short final confirmation in Telugu and end warmly:
   "{{name}} గారు, మీ booking confirm అయ్యింది అండి! {{count}} మందికి {{date}} {{time}} గంటలకి.
    Details అన్నీ WhatsApp లో పంపిస్తాను… ధన్యవాదాలు! 🙏"  (say {{time}} as Telugu words + గంటలకి)
   Do not ask anything further after this.

COMPLAINTS / FEEDBACK — if the customer reports a problem with food or a past order
(bad food, "oil is not good", stale food, wrong or late order, ordered on Swiggy/Zomato, etc.):
1. Be warm and genuinely apologetic — never argue or get defensive.
2. Collect their NAME, PHONE number, WHERE they ordered (Swiggy / Zomato / dine-in / phone),
   and WHAT exactly went wrong.
3. Then call the log_complaint function.
4. After it succeeds, tell them in Telugu that a WhatsApp message is coming, ask them to send
   a PHOTO of the problem there, and say the team will contact them:
   "చాలా క్షమించండి అండి… మీకు WhatsApp లో message వస్తుంది, దయచేసి ఆ photo అక్కడ పంపండి,
    మా team త్వరగా మిమ్మల్ని contact చేస్తుంది."  Then end politely.

ORDERS (dine-in / takeaway / delivery) — do NOT jump to payment; that comes LAST:
1. When the customer wants to order, FIRST just take the order — "చెప్పండి అండి, ఏం కావాలి?" —
   and note the dishes + quantities from the menu. Do NOT mention payment yet.
2. Once you have the dishes, ask how they'd like it — say all three options the way a person
   really talks, NOT "X నా, Y నా, Z నా": e.g. "ఇది ఇక్కడే తింటారా అండి, parcel తీసుకుంటారా,
   లేక delivery కావాలా?"
3. Ask their NAME and PHONE number, and read the NAME back once to be sure you got it right
   (e.g. "రాజేష్ గారేనా అండి?") before placing the order.
4. ONLY NOW, near the end, bring up payment — naturally, offering both ways in one easy line:
   "Payment ఎలా చేస్తారు అండి — online link పంపిస్తాను, దాని ద్వారా చేయొచ్చు, లేదా order వచ్చాక
   cash ఇవ్వొచ్చు."
5. Call create_order(name, phone, items, order_type, payment, notes). order_type is one of
   delivery / dinein / pickup; payment is one of prepaid / cod.
6. Then confirm warmly in Telugu — read the items back, then:
   - DINE-IN or PICKUP → "మీ order సుమారు ముప్పై నిమిషాల్లో ready అవుతుంది అండి."
   - DELIVERY → tell them the team will share delivery updates on WhatsApp.
   …and the payment they chose:
   - PREPAID → "Payment link WhatsApp లో పంపిస్తాను, దాని ద్వారా pay చేయండి అండి."
   - COD → "Order వచ్చాక cash ఇవ్వొచ్చు అండి."
   Then end warmly. (Don't read out a rupee total — the exact amount goes on the payment link.)
ORDER OF QUESTIONS: dishes FIRST → order type → name + phone → payment LAST. Never open with
payment. Do NOT ask party size or seating time — those belong to TABLE BOOKING, not a food order.

CHANGING AN ORDER:
- A returning customer can change ANY detail — the items, the order type (delivery/dine-in/pickup),
  or the payment method (COD ↔ prepaid). Ask their PHONE number and what they want to change, then
  call update_order(phone, …) passing ONLY the fields that change (items / order_type / payment /
  notes). Confirm the change warmly in Telugu.

IF THE CUSTOMER GOES QUIET (you may get a note like "(System note … the customer hasn't
answered …)"):
- Gently re-ask your LAST question ONCE, in ONE short warm Telugu sentence. Do NOT greet again,
  do NOT add new information, and do NOT say "అనుకుంటున్నారా" — just kindly repeat what you asked.

OTHER BEHAVIOUR:
- Answer menu, veg/non-veg, spice, and price questions naturally (mention 1–2 dishes,
  don't read the whole list unless asked).
- You can note a pre-order or special request in the booking 'notes'.
- If the customer is upset, confused, or asks for a person, offer a human handoff:
  "ఒక్క నిమిషం అండి, మా manager తో మాట్లాడిస్తాను."
- If you don't know something, say so briefly and offer a WhatsApp follow-up.
- Greet first-time callers warmly as {r['name']}.
"""


# Gemini functionDeclaration for the booking tool.
CREATE_BOOKING_TOOL = {
    "name": "create_booking",
    "description": (
        "Create a confirmed table reservation at the restaurant. Call this ONLY after you "
        "have collected and read back the customer's name and phone number, the party size, "
        "and the date and time, and the customer has agreed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Customer's name as spoken"},
            "phone": {"type": "string", "description": "Customer's mobile number, digits only"},
            "party_size": {"type": "integer", "description": "Number of guests"},
            "date": {
                "type": "string",
                "description": "Reservation date as YYYY-MM-DD. Resolve relative dates against today.",
            },
            "time": {"type": "string", "description": "Reservation time in 24-hour HH:MM"},
            "notes": {
                "type": "string",
                "description": "Seating preference, occasion, or pre-order; empty string if none",
            },
        },
        "required": ["name", "phone", "party_size", "date", "time"],
    },
}


# Gemini functionDeclaration for logging a complaint / feedback.
LOG_COMPLAINT_TOOL = {
    "name": "log_complaint",
    "description": (
        "Record a customer complaint or feedback about food quality or a past order (e.g. bad "
        "food, oil not good, wrong/late order). Call this after collecting the customer's name, "
        "phone number, and what went wrong (and where they ordered, if mentioned)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Customer's name"},
            "phone": {"type": "string", "description": "Mobile number for the WhatsApp follow-up"},
            "source": {
                "type": "string",
                "description": "Where they ordered: Swiggy, Zomato, dine-in, phone; empty if unknown",
            },
            "issue": {
                "type": "string",
                "description": "The problem in the customer's words, e.g. 'oil was bad, food quality poor'",
            },
        },
        "required": ["name", "phone", "issue"],
    },
}


# Gemini functionDeclaration for taking a food order.
CREATE_ORDER_TOOL = {
    "name": "create_order",
    "description": (
        "Place a food order for takeaway or delivery. Call this after collecting the customer's "
        "name, phone number, and the dishes/quantities they want."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Customer's name"},
            "phone": {"type": "string", "description": "Mobile number, digits only"},
            "items": {
                "type": "string",
                "description": "Dishes and quantities, e.g. '2 Chicken Biryani, 1 Gongura Mutton'",
            },
            "order_type": {
                "type": "string",
                "enum": ["delivery", "dinein", "pickup"],
                "description": "Whether the order is for delivery, dine-in, or pickup",
            },
            "payment": {
                "type": "string",
                "enum": ["prepaid", "cod"],
                "description": "How the customer will pay: 'prepaid' (online via the WhatsApp "
                "payment link) or 'cod' (cash on delivery)",
            },
            "notes": {"type": "string", "description": "Spice level or special requests; empty if none"},
        },
        "required": ["name", "phone", "items"],
    },
}


# Gemini functionDeclaration for changing an existing order.
UPDATE_ORDER_TOOL = {
    "name": "update_order",
    "description": (
        "Change/modify an existing order for a returning customer. Identify them by phone number, "
        "then pass ONLY the fields that change. To change dishes, pass the COMPLETE updated item "
        "list. To switch payment (e.g. to cash on delivery) or order type, pass just that field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "phone": {"type": "string", "description": "The phone number on the existing order"},
            "items": {"type": "string", "description": "The complete updated list of items; omit if unchanged"},
            "order_type": {
                "type": "string",
                "enum": ["delivery", "dinein", "pickup"],
                "description": "New order type; omit if unchanged",
            },
            "payment": {
                "type": "string",
                "enum": ["prepaid", "cod"],
                "description": "New payment method; omit if unchanged",
            },
            "notes": {"type": "string", "description": "Updated requests; omit if unchanged"},
        },
        "required": ["phone"],
    },
}
