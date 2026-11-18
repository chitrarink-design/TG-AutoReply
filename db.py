"""SQLite storage for rules, settings, connections, chats and message log."""
import json
import os
import sqlite3
import threading
import time

from config import BASE_DIR

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  match_type TEXT NOT NULL DEFAULT 'contains',
  keywords TEXT NOT NULL DEFAULT '',
  reply TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 100,
  enabled INTEGER NOT NULL DEFAULT 1,
  hits INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY,
  user_id INTEGER,
  user_chat_id INTEGER,
  name TEXT,
  username TEXT,
  can_reply INTEGER NOT NULL DEFAULT 0,
  is_enabled INTEGER NOT NULL DEFAULT 1,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS chats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connection_id TEXT NOT NULL,
  chat_id INTEGER NOT NULL,
  name TEXT,
  username TEXT,
  welcomed INTEGER NOT NULL DEFAULT 0,
  bot_disabled INTEGER NOT NULL DEFAULT 0,
  paused_until REAL,
  last_fallback_at REAL,
  last_message_at REAL,
  last_text TEXT,
  UNIQUE(connection_id, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_ref INTEGER NOT NULL,
  direction TEXT NOT NULL,      -- in | bot | owner | admin
  text TEXT,
  kind TEXT,                    -- welcome | rule | fallback | manual ...
  rule_id INTEGER,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_ref, id);
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,      -- stored in media/<filename>
  orig_name TEXT,
  kind TEXT NOT NULL,          -- photo | video | voice | audio | file
  mime TEXT,
  size INTEGER,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_name TEXT NOT NULL UNIQUE,   -- file sessions/<session_name>.session
  user_id INTEGER,
  name TEXT,
  username TEXT,
  phone TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "bot_enabled": True,
    "welcome_enabled": True,
    "welcome_text": "សួស្តី {first_name} 👋\nអរគុណសម្រាប់សារ! យើងនឹងឆ្លើយតបឆាប់ៗនេះ។",
    "fallback_enabled": True,
    "fallback_text": "អរគុណ {first_name}! 🙏 Admin នឹងឆ្លើយតបឆាប់ៗ។",
    "fallback_cooldown_minutes": 60,
    "reply_delay_seconds": 2,
    "owner_pause_minutes": 30,
    "welcome_media_ids": [],
    "fallback_media_ids": [],
}

SEED_RULES = [
    ("Price / តម្លៃ", "contains", "price\nតម្លៃ\nhow much\nប៉ុន្មាន",
     "💰 តម្លៃផលិតផល សូមមើលនៅទីនេះ៖ (put your price list here)"),
    ("Location / ទីតាំង", "contains", "location\naddress\nទីតាំង\nនៅឯណា",
     "📍 ហាងរបស់យើងនៅ៖ (put your address / Google Maps link here)"),
    ("Opening hours / ម៉ោងបើក", "contains", "open\nhours\nម៉ោង",
     "🕘 យើងបើកពីម៉ោង 8:00 ព្រឹក ដល់ 8:00 យប់ រៀងរាល់ថ្ងៃ។"),
]


def _path():
    return os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "data.db")


def _connect():
    conn = sqlite3.connect(_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=(), one=False):
    with _lock:
        conn = _connect()
        try:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
            return (rows[0] if rows else None) if one else rows
        finally:
            conn.close()


def execute(sql, params=()):
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def init():
    with _lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(rules)")]
            if "media_ids" not in cols:  # upgrade older databases: attachments are now a JSON list of media ids
                conn.execute("ALTER TABLE rules ADD COLUMN media_ids TEXT NOT NULL DEFAULT '[]'")
                if "media_id" in cols:
                    conn.execute("UPDATE rules SET media_ids = '[' || media_id || ']' WHERE media_id IS NOT NULL")
            for name in ("welcome", "fallback"):  # old single-attachment settings -> lists
                row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"{name}_media_id",)).fetchone()
                if row:
                    old = json.loads(row[0])
                    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                                 (f"{name}_media_ids", json.dumps([old] if old else [])))
                    conn.execute("DELETE FROM settings WHERE key = ?", (f"{name}_media_id",))
            conn.commit()
            first_run = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
            if first_run:
                for i, (name, mt, kw, reply) in enumerate(SEED_RULES):
                    conn.execute(
                        "INSERT INTO rules(name, match_type, keywords, reply, priority, created_at) VALUES (?,?,?,?,?,?)",
                        (name, mt, kw, reply, (i + 1) * 10, time.time()),
                    )
            conn.commit()
        finally:
            conn.close()


# ---------- settings ----------
def get_settings():
    out = dict(DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except ValueError:
            pass
    return out


def save_settings(values):
    for k, v in values.items():
        if k in DEFAULT_SETTINGS:
            execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


# ---------- rules ----------
def _decode_rule(r):
    if r:
        r["media_ids"] = _media_ids(r)
        r.pop("media_id", None)
    return r


def list_rules(enabled_only=False):
    sql = "SELECT * FROM rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return [_decode_rule(r) for r in query(sql + " ORDER BY priority ASC, id ASC")]


def get_rule(rule_id):
    return _decode_rule(query("SELECT * FROM rules WHERE id = ?", (rule_id,), one=True))


def create_rule(r):
    return execute(
        "INSERT INTO rules(name, match_type, keywords, reply, priority, enabled, media_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (r["name"], r["match_type"], r["keywords"], r["reply"], r["priority"], int(r["enabled"]), json.dumps(r["media_ids"]),
         time.time()),
    )


def update_rule(rule_id, r):
    execute(
        "UPDATE rules SET name=?, match_type=?, keywords=?, reply=?, priority=?, enabled=?, media_ids=? WHERE id=?",
        (r["name"], r["match_type"], r["keywords"], r["reply"], r["priority"], int(r["enabled"]), json.dumps(r["media_ids"]),
         rule_id),
    )


def delete_rule(rule_id):
    execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def bump_rule_hits(rule_id):
    execute("UPDATE rules SET hits = hits + 1 WHERE id = ?", (rule_id,))


# ---------- business connections ----------
def upsert_connection(c):
    execute(
        """INSERT INTO connections(id, user_id, user_chat_id, name, username, can_reply, is_enabled, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, user_chat_id=excluded.user_chat_id,
             name=excluded.name, username=excluded.username, can_reply=excluded.can_reply,
             is_enabled=excluded.is_enabled, updated_at=excluded.updated_at""",
        (c["id"], c["user_id"], c["user_chat_id"], c["name"], c["username"],
         int(c["can_reply"]), int(c["is_enabled"]), time.time()),
    )


def get_connection(conn_id):
    return query("SELECT * FROM connections WHERE id = ?", (conn_id,), one=True)


def list_connections():
    return query("SELECT * FROM connections ORDER BY updated_at DESC")


# ---------- chats & messages ----------
def upsert_chat(connection_id, chat_id, name, username):
    execute(
        """INSERT INTO chats(connection_id, chat_id, name, username) VALUES (?,?,?,?)
           ON CONFLICT(connection_id, chat_id) DO UPDATE SET name=excluded.name, username=excluded.username""",
        (connection_id, chat_id, name, username),
    )
    return query("SELECT * FROM chats WHERE connection_id = ? AND chat_id = ?", (connection_id, chat_id), one=True)


def get_chat(chat_ref):
    return query("SELECT * FROM chats WHERE id = ?", (chat_ref,), one=True)


def update_chat(chat_ref, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE chats SET {cols} WHERE id = ?", (*fields.values(), chat_ref))


def _chat_filter(kind):
    # 'user' mode: chats of logged-in accounts (connection_id 'acc:<id>'); 'bot' mode: chats with the bot.
    return ("c.connection_id LIKE 'acc:%'", ()) if kind == "user" else ("c.connection_id = ?", (kind,))


def list_chats(kind, limit=200):
    where, params = _chat_filter(kind)
    return query(
        f"""SELECT c.*, a.name AS account_name, a.username AS account_username
            FROM chats c LEFT JOIN accounts a ON c.connection_id = 'acc:' || a.id
            WHERE {where} ORDER BY COALESCE(c.last_message_at, 0) DESC LIMIT ?""",
        (*params, limit),
    )


def log_message(chat_ref, direction, text, kind=None, rule_id=None):
    now = time.time()
    execute(
        "INSERT INTO messages(chat_ref, direction, text, kind, rule_id, created_at) VALUES (?,?,?,?,?,?)",
        (chat_ref, direction, text, kind, rule_id, now),
    )
    update_chat(chat_ref, last_message_at=now, last_text=(text or "")[:200])


def list_messages(chat_ref, limit=300):
    rows = query("SELECT * FROM messages WHERE chat_ref = ? ORDER BY id DESC LIMIT ?", (chat_ref, limit))
    return list(reversed(rows))


def stats(kind):
    day_start = time.time() - 86400
    return {
        "rules": query("SELECT COUNT(*) AS n FROM rules WHERE enabled = 1", one=True)["n"],
        "chats": query(f"SELECT COUNT(*) AS n FROM chats c WHERE {_chat_filter(kind)[0]}",
                       _chat_filter(kind)[1], one=True)["n"],
        "incoming_24h": query(
            "SELECT COUNT(*) AS n FROM messages WHERE direction = 'in' AND created_at > ?", (day_start,), one=True)["n"],
        "auto_replies_24h": query(
            "SELECT COUNT(*) AS n FROM messages WHERE direction = 'bot' AND created_at > ?", (day_start,), one=True)["n"],
    }


# ---------- logged-in Telegram accounts (MODE=user) ----------
def list_accounts():
    return query("SELECT * FROM accounts ORDER BY id")


def get_account(account_id):
    return query("SELECT * FROM accounts WHERE id = ?", (account_id,), one=True)


def create_account(session_name, **fields):
    cols = ["session_name", "created_at", *fields]
    vals = [session_name, time.time(), *fields.values()]
    return execute(f"INSERT INTO accounts({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)


def update_account(account_id, **fields):
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        execute(f"UPDATE accounts SET {cols} WHERE id = ?", (*fields.values(), account_id))


def delete_account(account_id):
    execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# ---------- media attachments (photos, voice, ...) ----------
def create_media(filename, orig_name, kind, mime, size):
    return execute("INSERT INTO media(filename, orig_name, kind, mime, size, created_at) VALUES (?,?,?,?,?,?)",
                   (filename, orig_name, kind, mime, size, time.time()))


def _media_ids(row):
    try:
        return [int(i) for i in json.loads(row.get("media_ids") or "[]")]
    except (TypeError, ValueError):
        return []


def get_media_list(ids):
    """Media rows for a list of ids, in the same order; missing ones are skipped."""
    return [m for m in (get_media(i) for i in (ids or [])) if m]


def get_media(media_id):
    if not media_id:
        return None
    return query("SELECT * FROM media WHERE id = ?", (media_id,), one=True)
