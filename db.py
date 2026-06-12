"""SQLite storage for the voice-agent demo: bookings + conversation turns.

Stdlib sqlite3 only. One shared connection guarded by a lock so it's safe under
FastAPI's async loop and uvicorn --reload on Windows.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from services.prompts import order_total

# On Vercel the project filesystem is read-only except /tmp (ephemeral per instance).
_DB_BASE = Path("/tmp") if os.getenv("VERCEL") else Path(__file__).parent
DB_PATH = _DB_BASE / "app.db"
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  phone       TEXT    NOT NULL,
  party_size  INTEGER NOT NULL,
  date        TEXT    NOT NULL,
  time        TEXT    NOT NULL,
  notes       TEXT    DEFAULT '',
  status      TEXT    DEFAULT 'confirmed',
  created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS conversations (
  session_id  TEXT    PRIMARY KEY,
  created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS turns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT    NOT NULL,
  role        TEXT    NOT NULL,          -- user | assistant | tool
  text        TEXT,
  audio_ref   TEXT,
  created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS complaints (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  phone       TEXT    NOT NULL,
  source      TEXT    DEFAULT '',          -- swiggy | zomato | dine-in | phone
  issue       TEXT    NOT NULL,
  status      TEXT    DEFAULT 'open',
  created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS orders (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  phone       TEXT    NOT NULL,
  items       TEXT    NOT NULL,
  order_type  TEXT    DEFAULT '',           -- delivery | dinein | pickup
  payment     TEXT    DEFAULT '',           -- prepaid | cod
  total       INTEGER DEFAULT 0,            -- computed rupee total from the menu
  notes       TEXT    DEFAULT '',
  status      TEXT    DEFAULT 'received',   -- received | changed | confirmed
  created_at  TEXT    DEFAULT (datetime('now','localtime'))
);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    try:
        with _lock:
            conn = _connect()
            conn.executescript(SCHEMA)
            # migrate older DBs created before the order_type / payment columns existed
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
            if "order_type" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN order_type TEXT DEFAULT ''")
            if "payment" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN payment TEXT DEFAULT ''")
            if "total" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN total INTEGER DEFAULT 0")
            conn.commit()
    except Exception:
        pass


def ensure_conversation(session_id: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR IGNORE INTO conversations (session_id) VALUES (?)", (session_id,)
        )
        conn.commit()


def log_turn(session_id: str, role: str, text: str, audio_ref: str | None = None) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO turns (session_id, role, text, audio_ref) VALUES (?,?,?,?)",
            (session_id, role, text, audio_ref),
        )
        conn.commit()


def insert_booking(b: dict) -> dict:
    """Insert a booking and return the saved row as a dict."""
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO bookings (name, phone, party_size, date, time, notes, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                str(b.get("name", "")).strip(),
                str(b.get("phone", "")).strip(),
                int(b.get("party_size") or 0),
                str(b.get("date", "")).strip(),
                str(b.get("time", "")).strip(),
                str(b.get("notes", "") or "").strip(),
                str(b.get("status", "confirmed")).strip(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def update_latest_booking(
    phone: str,
    party_size=None,
    date: str | None = None,
    time: str | None = None,
    name: str | None = None,
    notes: str | None = None,
) -> dict | None:
    """Update the most recent booking for a phone number, changing only the fields passed.
    Returns the updated row or None if no booking matches."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM bookings WHERE phone=? ORDER BY id DESC LIMIT 1", (str(phone).strip(),)
        ).fetchone()
        if not row:
            return None
        try:
            new_party = int(party_size) if party_size else row["party_size"]
        except (TypeError, ValueError):
            new_party = row["party_size"]
        new_date = row["date"] if date in (None, "") else str(date).strip()
        new_time = row["time"] if time in (None, "") else str(time).strip()
        new_name = row["name"] if name in (None, "") else str(name).strip()
        new_notes = row["notes"] if notes is None else str(notes).strip()
        conn.execute(
            "UPDATE bookings SET name=?, party_size=?, date=?, time=?, notes=?, status='changed' "
            "WHERE id=?",
            (new_name, new_party, new_date, new_time, new_notes, row["id"]),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM bookings WHERE id=?", (row["id"],)).fetchone())


def recent_bookings(limit: int = 50) -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def insert_complaint(c: dict) -> dict:
    """Insert a complaint/feedback row and return it as a dict."""
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO complaints (name, phone, source, issue, status) VALUES (?,?,?,?,?)",
            (
                str(c.get("name", "")).strip(),
                str(c.get("phone", "")).strip(),
                str(c.get("source", "") or "").strip(),
                str(c.get("issue", "")).strip(),
                str(c.get("status", "open")).strip(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def recent_complaints(limit: int = 50) -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM complaints ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def insert_order(o: dict) -> dict:
    """Insert a food order and return the saved row."""
    items = str(o.get("items", "")).strip()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO orders (name, phone, items, order_type, payment, total, notes, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(o.get("name", "")).strip(),
                str(o.get("phone", "")).strip(),
                items,
                str(o.get("order_type", "") or "").strip().lower(),
                str(o.get("payment", "") or "").strip().lower(),
                int(o.get("total") or order_total(items)),
                str(o.get("notes", "") or "").strip(),
                str(o.get("status", "received")).strip(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def recent_orders(limit: int = 50) -> list[dict]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_order(o: dict) -> dict:
    """Write an order row PRESERVING its id — used when a client-carried snapshot is the source
    of truth (Vercel instances each have their own ephemeral /tmp DB)."""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO orders (id, name, phone, items, order_type, payment, total, "
            "notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?, datetime('now','localtime')))",
            (
                int(o.get("id") or 0) or None,
                str(o.get("name", "")).strip(),
                str(o.get("phone", "")).strip(),
                str(o.get("items", "")).strip(),
                str(o.get("order_type", "") or "").strip().lower(),
                str(o.get("payment", "") or "").strip().lower(),
                int(o.get("total") or 0),
                str(o.get("notes", "") or "").strip(),
                str(o.get("status", "changed")).strip(),
                o.get("created_at"),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT 1",
            (str(o.get("phone", "")).strip(),),
        ).fetchone()
        return dict(row)


def upsert_booking(b: dict) -> dict:
    """Booking twin of upsert_order — preserves the id from a client-carried snapshot."""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO bookings (id, name, phone, party_size, date, time, notes, "
            "status, created_at) VALUES (?,?,?,?,?,?,?,?,COALESCE(?, datetime('now','localtime')))",
            (
                int(b.get("id") or 0) or None,
                str(b.get("name", "")).strip(),
                str(b.get("phone", "")).strip(),
                int(b.get("party_size") or 0),
                str(b.get("date", "")).strip(),
                str(b.get("time", "")).strip(),
                str(b.get("notes", "") or "").strip(),
                str(b.get("status", "changed")).strip(),
                b.get("created_at"),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM bookings WHERE phone=? ORDER BY id DESC LIMIT 1",
            (str(b.get("phone", "")).strip(),),
        ).fetchone()
        return dict(row)


def update_latest_order(
    phone: str,
    items: str | None = None,
    notes: str | None = None,
    order_type: str | None = None,
    payment: str | None = None,
) -> dict | None:
    """Update the most recent order for a phone number, changing only the fields passed
    (items / notes / order_type / payment). Returns the updated row or None if not found."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT 1", (str(phone).strip(),)
        ).fetchone()
        if not row:
            return None
        new_items = row["items"] if items in (None, "") else str(items).strip()
        new_notes = row["notes"] if notes is None else str(notes).strip()
        new_type = row["order_type"] if order_type in (None, "") else str(order_type).strip().lower()
        new_pay = row["payment"] if payment in (None, "") else str(payment).strip().lower()
        new_total = row["total"] if items in (None, "") else order_total(new_items)
        conn.execute(
            "UPDATE orders SET items=?, notes=?, order_type=?, payment=?, total=?, status='changed' WHERE id=?",
            (new_items, new_notes, new_type, new_pay, new_total, row["id"]),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone())


# Ensure tables exist at import time too — Vercel serverless may not run the ASGI lifespan.
init_db()
