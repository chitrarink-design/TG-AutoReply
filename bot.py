"""Telegram auto-reply bot: customers chat with the bot directly (long polling, standard library only)."""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import db
import media as media_store
import rules

log = logging.getLogger("bot")

DIRECT = "bot"  # value stored in chats.connection_id for direct chats with the bot
ALLOWED_UPDATES = ["message"]


class TelegramError(Exception):
    pass


def _full_name(user):
    return " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or user.get("username") or ""


def _describe(msg):
    text = msg.get("text") or msg.get("caption")
    if text:
        return text
    for kind in ("photo", "video", "voice", "audio", "document", "sticker", "animation", "video_note",
                 "contact", "location"):
        if kind in msg:
            return f"[{kind}]"
    return "[message]"


class Bot:
    kind = DIRECT

    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.me = None
        self.last_error = None
        self.last_poll_at = None

    # ---------- Telegram API ----------
    def api(self, method, params=None, timeout=20, file=None):
        """Call the Bot API. `file` = (field, path) uploads a file with multipart/form-data."""
        if file:
            boundary = "----autoreply" + str(time.time_ns())
            parts = []
            for k, v in (params or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            field, path = file
            with open(path, "rb") as f:
                content = f.read()
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                          f'filename="{os.path.basename(path)}"\r\n'
                          'Content-Type: application/octet-stream\r\n\r\n').encode() + content + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            req = urllib.request.Request(self.base + method, data=b"".join(parts),
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            timeout = max(timeout, 120)
        else:
            req = urllib.request.Request(
                self.base + method,
                data=json.dumps(params or {}).encode(),
                headers={"Content-Type": "application/json"},
            )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read())
            except ValueError:
                raise TelegramError(f"HTTP {e.code}") from None
        if not res.get("ok"):
            raise TelegramError(res.get("description", "Unknown Telegram error"))
        return res["result"]

    def send_text(self, chat_id, text, media=None):
        """Send a reply: photos/videos first (text as caption), then voice/audio/files."""
        visual, other = media_store.split(media)
        caption_ok = bool(visual) and len(text or "") <= 1024
        result = None
        if text and not caption_ok:
            result = self.api("sendMessage", {"chat_id": chat_id, "text": text})
        for i, m in enumerate(visual):  # one by one: simple and works for any number of photos
            params = {"chat_id": chat_id}
            if i == 0 and caption_ok and text:
                params["caption"] = text
            method, field = ("sendVideo", "video") if m["kind"] == "video" else ("sendPhoto", "photo")
            result = self.api(method, params, file=(field, media_store.path_of(m)))
        for m in other:
            method, field = {"voice": ("sendVoice", "voice"), "audio": ("sendAudio", "audio")}.get(
                m["kind"], ("sendDocument", "document"))
            result = self.api(method, {"chat_id": chat_id}, file=(field, media_store.path_of(m)))
        return result

    # ---------- polling loop ----------
    def run_forever(self):
        offset = None
        while True:
            try:
                if self.me is None:
                    self.me = self.api("getMe")
                    self.api("deleteWebhook")
                    log.info("Bot started as @%s", self.me.get("username"))
                    self.last_error = None
                self.last_poll_at = time.time()  # we reached Telegram; now waiting for messages
                params = {"timeout": 30, "allowed_updates": ALLOWED_UPDATES}
                if offset is not None:
                    params["offset"] = offset
                updates = self.api("getUpdates", params, timeout=45)
                self.last_error = None
                self.last_poll_at = time.time()
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        self.handle(update)
                    except Exception:
                        log.exception("Failed to handle update %s", update.get("update_id"))
            except Exception as e:  # network problems, bad token, two copies running...
                self.last_error = str(e)
                log.warning("Polling error: %s", e)
                time.sleep(5)

    # ---------- incoming messages ----------
    def handle(self, update):
        m = update.get("message")
        if not m:
            return
        customer = m.get("chat") or {}
        sender = m.get("from") or {}
        if customer.get("type") != "private" or sender.get("is_bot"):
            return  # only one-to-one chats with real people
        chat = db.upsert_chat(DIRECT, customer["id"], _full_name(customer), customer.get("username"))
        db.log_message(chat["id"], "in", _describe(m))
        text = m.get("text") or m.get("caption") or ""
        if text.startswith("/start"):
            text = ""  # the /start command itself should only trigger the welcome message
        self.auto_reply(chat, customer, text)

    # ---------- auto reply decision ----------
    def auto_reply(self, chat, customer, text):
        s = db.get_settings()
        if not s["bot_enabled"]:
            return
        if chat["bot_disabled"] or (chat["paused_until"] or 0) > time.time():
            return
        replies = rules.plan_replies(chat, text)
        if replies:
            threading.Thread(
                target=self._deliver,
                args=(chat, customer, replies, float(s.get("reply_delay_seconds") or 0)),
                daemon=True,
            ).start()

    def _deliver(self, chat, customer, replies, delay):
        for template, kind, rule_id, media_ids in replies:
            media = db.get_media_list(media_ids)
            if delay > 0:
                action = media_store.chat_action(media, "record_voice", "upload_photo")
                try:
                    self.api("sendChatAction", {"chat_id": chat["chat_id"], "action": action})
                except Exception:
                    pass
                time.sleep(min(delay, 30))
            fresh = db.get_chat(chat["id"])
            if fresh and (fresh["bot_disabled"] or (fresh["paused_until"] or 0) > time.time()):
                return  # admin jumped in while we were "typing"
            body = rules.render(template, customer).strip()
            try:
                self.send_text(chat["chat_id"], body, media)
                db.log_message(chat["id"], "bot", media_store.log_text(body, media), kind=kind, rule_id=rule_id)
            except Exception as e:
                log.warning("Failed to send reply: %s", e)
                db.log_message(chat["id"], "bot", f"⚠️ Failed to send: {e}", kind="error")
                return
