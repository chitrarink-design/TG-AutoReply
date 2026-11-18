"""Admin website + JSON API (standard library http.server)."""
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import media as media_store
import rules
from config import BASE_DIR

BOT = None            # set by app.py when a token is configured
ADMIN_PASSWORD = ""
SESSIONS = {}         # token -> expiry
SESSION_TTL = 7 * 24 * 3600
STATIC_DIR = os.path.join(BASE_DIR, "static")

SETTING_TYPES = {
    "bot_enabled": bool, "welcome_enabled": bool, "fallback_enabled": bool,
    "welcome_text": str, "fallback_text": str,
    "fallback_cooldown_minutes": float, "reply_delay_seconds": float, "owner_pause_minutes": float,
}


def _kind():
    return getattr(BOT, "kind", "user")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoReplyAdmin"

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    # ---------- helpers ----------
    def _send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ApiError(413, "Request too large")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "Invalid JSON") from None

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                return v
        return None

    def _authed(self):
        tok = self._token()
        exp = SESSIONS.get(tok) if tok else None
        return bool(exp and exp > time.time())

    # ---------- routing ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Method not allowed")
                return self._static(path)
            if path == "/api/login" and method == "POST":
                return self._login()
            if not self._authed():
                raise ApiError(401, "Please log in")
            if method != "GET" and self.headers.get("X-Requested-With") != "admin":
                raise ApiError(403, "Missing CSRF header")
            for pattern, m, fn in ROUTES:
                match = re.fullmatch(pattern, path)
                if match and m == method:
                    result = fn(self, *match.groups())
                    if result is not None:  # None = the handler already sent its own response (files)
                        self._send(200, result)
                    return
            raise ApiError(404, "Not found")
        except ApiError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": f"Server error: {e}"})

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            raise ApiError(404, "Not found")
        ctype = {"html": "text/html", "css": "text/css", "js": "application/javascript",
                 "svg": "image/svg+xml", "png": "image/png"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    # ---------- auth ----------
    def _login(self):
        pw = str(self._body().get("password") or "")
        if not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            time.sleep(1)
            raise ApiError(401, "Wrong password")
        tok = secrets.token_urlsafe(32)
        SESSIONS[tok] = time.time() + SESSION_TTL
        cookie = f"sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"
        self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})

    def logout(self):
        SESSIONS.pop(self._token(), None)
        return {"ok": True}

    # ---------- API ----------
    def status(self):
        info = {"configured": BOT is not None, "mode": getattr(BOT, "kind", None)}
        if _kind() == "user":
            accs = BOT.list_accounts()
            info.update({"accounts": len(accs), "online_accounts": sum(a["online"] for a in accs),
                         "online": any(a["online"] for a in accs), "last_error": None})
        elif BOT:
            info.update({
                "username": (BOT.me or {}).get("username"),
                "name": (BOT.me or {}).get("first_name"),
                # A long poll lasts up to 30 s, so "seen within 90 s" means the bot is alive.
                "online": BOT.last_error is None and time.time() - (BOT.last_poll_at or 0) < 90,
                "last_error": BOT.last_error,
            })
        return {"bot": info, "stats": db.stats(_kind()),
                "bot_enabled": db.get_settings()["bot_enabled"]}

    def list_rules(self):
        return [{**r, "media": db.get_media_list(r["media_ids"])} for r in db.list_rules()]

    # ---------- attachments ----------
    def upload_media(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > media_store.MAX_SIZE:
            raise ApiError(413, "The file is larger than 20 MB")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = (qs.get("name") or [""])[0][:200]
        want_voice = (qs.get("voice") or ["1"])[0] == "1"
        data = self.rfile.read(length) if length else b""
        try:
            m, note = media_store.save_upload(data, name, self.headers.get("Content-Type"), want_voice)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return {**m, "note": note}

    def get_media_info(self, media_id):
        m = db.get_media(int(media_id))
        if not m:
            raise ApiError(404, "Attachment not found")
        return m

    def media_file(self, media_id):
        m = db.get_media(int(media_id))
        path = media_store.path_of(m) if m else None
        if not path or not os.path.isfile(path):
            raise ApiError(404, "Attachment not found")
        mime = m["mime"] or ""
        safe = mime.split("/")[0] in ("image", "audio", "video") and "svg" not in mime
        with open(path, "rb") as f:
            self._send(200, f.read(), mime if safe else "application/octet-stream",
                       headers={"Content-Disposition": "inline" if safe else "attachment"})
        return None

    def create_rule(self):
        rule, err = rules.validate_rule(self._body())
        if err:
            raise ApiError(400, err)
        return db.get_rule(db.create_rule(rule))

    def update_rule(self, rule_id):
        if not db.get_rule(int(rule_id)):
            raise ApiError(404, "Rule not found")
        rule, err = rules.validate_rule(self._body())
        if err:
            raise ApiError(400, err)
        db.update_rule(int(rule_id), rule)
        return db.get_rule(int(rule_id))

    def delete_rule(self, rule_id):
        db.delete_rule(int(rule_id))
        return {"ok": True}

    def get_settings(self):
        s = db.get_settings()
        return {**s, "welcome_media": db.get_media_list(s.get("welcome_media_ids")),
                "fallback_media": db.get_media_list(s.get("fallback_media_ids"))}

    def save_settings(self):
        data = self._body()
        clean = {}
        for k in ("welcome_media_ids", "fallback_media_ids"):
            if k in data:
                ids, err = rules.clean_media_ids(data[k] or [])
                if err:
                    raise ApiError(400, err)
                clean[k] = ids
        for k, typ in SETTING_TYPES.items():
            if k not in data:
                continue
            try:
                v = typ(data[k])
            except (TypeError, ValueError):
                raise ApiError(400, f"Invalid value for {k}") from None
            if typ is float:
                if v < 0:
                    raise ApiError(400, f"{k} cannot be negative")
                v = int(v) if v == int(v) else v
            clean[k] = v
        db.save_settings(clean)
        return db.get_settings()

    def test_message(self):
        data = self._body()
        text = str(data.get("text") or "")
        user = {"first_name": data.get("first_name") or "Dara", "username": "dara"}
        s = db.get_settings()
        rule = rules.find_match(text)
        replies = []
        def item(kind, template, media_ids, **extra):
            return {"kind": kind, "text": rules.render(template, user).strip(),
                    "media": db.get_media_list(media_ids), **extra}
        if data.get("new_chat") and s["welcome_enabled"] and (s["welcome_text"].strip() or s.get("welcome_media_ids")):
            replies.append(item("welcome", s["welcome_text"], s.get("welcome_media_ids")))
        if rule:
            replies.append(item("rule", rule["reply"], rule["media_ids"], rule=rule["name"]))
        elif not replies and s["fallback_enabled"] and (s["fallback_text"].strip() or s.get("fallback_media_ids")):
            replies.append(item("fallback", s["fallback_text"], s.get("fallback_media_ids")))
        return {"replies": replies, "bot_enabled": s["bot_enabled"]}

    def list_chats(self):
        return db.list_chats(_kind())

    def get_chat(self, chat_ref):
        chat = db.get_chat(int(chat_ref))
        if not chat:
            raise ApiError(404, "Chat not found")
        return {"chat": chat, "messages": db.list_messages(chat["id"]), "now": time.time()}

    def update_chat(self, chat_ref):
        chat = db.get_chat(int(chat_ref))
        if not chat:
            raise ApiError(404, "Chat not found")
        data = self._body()
        fields = {}
        if "bot_disabled" in data:
            fields["bot_disabled"] = int(bool(data["bot_disabled"]))
        if "pause_minutes" in data:
            minutes = float(data["pause_minutes"] or 0)
            fields["paused_until"] = time.time() + minutes * 60 if minutes > 0 else None
        if data.get("reset_welcome"):
            fields["welcomed"] = 0
        db.update_chat(chat["id"], **fields)
        return db.get_chat(chat["id"])

    def send_to_chat(self, chat_ref):
        if not BOT:
            raise ApiError(400, "BOT_TOKEN is not configured")
        chat = db.get_chat(int(chat_ref))
        if not chat:
            raise ApiError(404, "Chat not found")
        text = str(self._body().get("text") or "").strip()
        if not text:
            raise ApiError(400, "Message is empty")
        try:
            if _kind() == "user" and chat["connection_id"].startswith("acc:"):
                BOT.send_text(int(chat["connection_id"][4:]), chat["chat_id"], text)
            elif chat["connection_id"] == BOT.kind:
                BOT.send_text(chat["chat_id"], text)
            else:
                raise ApiError(400, "This chat belongs to another mode; it can't be answered from here.")
        except ApiError:
            raise
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        except Exception as e:
            msg = str(e)
            if "blocked" in msg:
                msg = "this person has blocked you / the bot"
            raise ApiError(502, f"Telegram: {msg}") from None
        db.log_message(chat["id"], "admin", text, kind="manual")
        # You're talking to this person yourself now -> pause auto-replies in this chat for a while.
        minutes = float(db.get_settings().get("owner_pause_minutes") or 0)
        if minutes > 0:
            db.update_chat(chat["id"], paused_until=time.time() + minutes * 60)
        return {"ok": True}

    # ---------- accounts (MODE=user) ----------
    def _mgr(self):
        if _kind() != "user":
            raise ApiError(400, "Accounts are only used in MODE=user")
        return BOT

    def _run(self, fn, *args):
        try:
            return fn(*args)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        except TimeoutError:
            raise ApiError(504, "Telegram did not answer in time, try again") from None

    def list_accounts(self):
        return self._mgr().list_accounts()

    def update_account(self, account_id):
        data = self._body()
        self._run(self._mgr().set_enabled, int(account_id), bool(data.get("enabled")))
        return {"ok": True}

    def delete_account(self, account_id):
        self._run(self._mgr().remove, int(account_id))
        return {"ok": True}

    def login_qr(self):
        return self._run(self._mgr().start_qr_login)

    def login_phone(self):
        return self._run(self._mgr().start_phone_login, str(self._body().get("phone") or ""))

    def login_status(self, login_id):
        return self._run(self._mgr().login_status, login_id)

    def login_code(self, login_id):
        return self._run(self._mgr().submit_code, login_id, str(self._body().get("code") or ""))

    def login_password(self, login_id):
        return self._run(self._mgr().submit_password, login_id, str(self._body().get("password") or ""))

    def login_cancel(self, login_id):
        return self._run(self._mgr().cancel_login, login_id)


ROUTES = [
    (r"/api/logout", "POST", Handler.logout),
    (r"/api/status", "GET", Handler.status),
    (r"/api/rules", "GET", Handler.list_rules),
    (r"/api/media", "POST", Handler.upload_media),
    (r"/api/media/(\d+)", "GET", Handler.get_media_info),
    (r"/api/media/(\d+)/file", "GET", Handler.media_file),
    (r"/api/rules", "POST", Handler.create_rule),
    (r"/api/rules/(\d+)", "PUT", Handler.update_rule),
    (r"/api/rules/(\d+)", "DELETE", Handler.delete_rule),
    (r"/api/settings", "GET", Handler.get_settings),
    (r"/api/settings", "PUT", Handler.save_settings),
    (r"/api/test", "POST", Handler.test_message),
    (r"/api/chats", "GET", Handler.list_chats),
    (r"/api/chats/(\d+)", "GET", Handler.get_chat),
    (r"/api/chats/(\d+)", "PUT", Handler.update_chat),
    (r"/api/chats/(\d+)/send", "POST", Handler.send_to_chat),
    (r"/api/accounts", "GET", Handler.list_accounts),
    (r"/api/accounts/(\d+)", "PUT", Handler.update_account),
    (r"/api/accounts/(\d+)", "DELETE", Handler.delete_account),
    (r"/api/accounts/login/qr", "POST", Handler.login_qr),
    (r"/api/accounts/login/phone", "POST", Handler.login_phone),
    (r"/api/accounts/login/([\w-]+)", "GET", Handler.login_status),
    (r"/api/accounts/login/([\w-]+)/code", "POST", Handler.login_code),
    (r"/api/accounts/login/([\w-]+)/password", "POST", Handler.login_password),
    (r"/api/accounts/login/([\w-]+)/cancel", "POST", Handler.login_cancel),
]


def serve(host, port, password):
    global ADMIN_PASSWORD
    ADMIN_PASSWORD = password
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Admin website: http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
    httpd.serve_forever()
