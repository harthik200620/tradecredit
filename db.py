"""SQLite storage for the voice-agent demo: bookings + conversation turns.

Stdlib sqlite3 only. One shared connection guarded by a lock so it's safe under
FastAPI's async loop and uvicorn --reload on Windows.
"""
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).parent / "app.db"
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
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()


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
