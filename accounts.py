"""Several Telegram accounts that auto-reply (Telethon "userbots"), with login from the admin website.

All accounts run in ONE background thread with its own asyncio loop. The web server (another
thread) calls the public methods of Manager, which hand the work to that loop.
"""
import asyncio
import logging
import os
import secrets
import shutil
import threading
import time

from telethon import TelegramClient, errors, events

import db
import media as media_store
import rules
from config import BASE_DIR

log = logging.getLogger("accounts")

SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
TELEGRAM_SERVICE_ID = 777000   # Telegram's own notifications / login codes: never auto-reply
LOGIN_TTL = 10 * 60            # unfinished logins are dropped after 10 minutes


class NotAuthorized(Exception):
    pass


FATAL_ERRORS = (NotAuthorized,) + tuple(getattr(errors, n) for n in (
    "AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError",
    "UserDeactivatedError", "UserDeactivatedBanError") if hasattr(errors, n))


def chat_key(account_id):
    return f"acc:{account_id}"


def _full_name(user):
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    return (first + " " + last).strip() or getattr(user, "username", None) or str(getattr(user, "id", ""))


def _user_dict(user):
    return {"first_name": getattr(user, "first_name", None), "last_name": getattr(user, "last_name", None),
            "username": getattr(user, "username", None)}


def _describe(msg):
    if msg.message:
        return msg.message
    for kind in ("photo", "video", "voice", "audio", "sticker", "gif", "document", "contact", "geo"):
        if getattr(msg, kind, None):
            return f"[{kind}]"
    return "[message]"


def _delete_session_files(session_name):
    base = os.path.join(SESSIONS_DIR, session_name + ".session")
    for path in (base, base + "-journal"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ======================================================================
# One logged-in account
# ======================================================================
class Runner:
    def __init__(self, mgr, account):
        self.mgr = mgr
        self.id = account["id"]
        self.session = account["session_name"]
        self.client = None
        self.task = None
        self.online = False
        self.last_error = None
        self._our_ids = set()   # message ids the auto-replier sent (so they don't count as "you replied")
        self._sending = set()   # chat ids we are sending to right now

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
        if self.client:
            await self.client.disconnect()
        self.online = False

    async def _run(self):
        while True:
            try:
                self.client = self.mgr.make_client(self.session)
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise NotAuthorized()
                me = await self.client.get_me()
                db.update_account(self.id, user_id=me.id, name=_full_name(me), username=me.username,
                                  phone=("+" + me.phone) if me.phone else None)
                self.client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
                self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
                self.online, self.last_error = True, None
                log.info("Account %s (%s) is auto-replying", self.id, _full_name(me))
                await self.client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except FATAL_ERRORS:
                self.online = False
                self.last_error = "Logged out of Telegram. Remove this account and add it again."
                log.warning("Account %s: %s", self.id, self.last_error)
                if self.client:
                    await self.client.disconnect()
                return
            except Exception as e:
                self.last_error = str(e)
                log.warning("Account %s error: %s", self.id, e)
            self.online = False
            await asyncio.sleep(5)

    # ---------- events ----------
    async def on_incoming(self, event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False) or sender.id == TELEGRAM_SERVICE_ID \
                or getattr(sender, "is_self", False):
            return
        chat = db.upsert_chat(chat_key(self.id), event.chat_id, _full_name(sender), getattr(sender, "username", None))
        db.log_message(chat["id"], "in", _describe(event.message))

        s = db.get_settings()
        if not s["bot_enabled"] or chat["bot_disabled"] or (chat["paused_until"] or 0) > time.time():
            return
        replies = rules.plan_replies(chat, event.message.message or "")
        if replies:
            asyncio.create_task(self._deliver(chat, _user_dict(sender), replies,
                                              float(s.get("reply_delay_seconds") or 0)))

    async def on_outgoing(self, event):
        """You typed a message yourself (phone/desktop) -> pause auto-replies in that chat."""
        if not event.is_private:
            return
        await asyncio.sleep(1.5)  # let send() record ids of messages the auto-replier sent
        if event.id in self._our_ids or event.chat_id in self._sending:
            return
        chat = db.query("SELECT * FROM chats WHERE connection_id = ? AND chat_id = ?",
                        (chat_key(self.id), event.chat_id), one=True)
        if not chat:
            return  # nobody wrote to you in this chat yet - nothing to track
        db.log_message(chat["id"], "owner", _describe(event.message))
        minutes = float(db.get_settings().get("owner_pause_minutes") or 0)
        if minutes > 0:
            db.update_chat(chat["id"], paused_until=time.time() + minutes * 60)

    # ---------- sending ----------
    async def send(self, chat_id, text, media=None):
        """Send a reply: photos/videos (album, text as caption) first, then voice/audio/files.
        `media` is a list of media rows (or None)."""
        if not self.online:
            raise ValueError("This account is not connected right now")
        self._sending.add(chat_id)
        try:
            sent = []
            visual, other = media_store.split(media)
            caption_ok = bool(visual) and len(text or "") <= 1024
            if text and not caption_ok:
                sent.append(await self.client.send_message(chat_id, text))
            if visual:
                paths = [media_store.path_of(m) for m in visual]
                res = await self.client.send_file(chat_id, paths if len(paths) > 1 else paths[0],
                                                  caption=text if caption_ok else None)
                sent.extend(res if isinstance(res, list) else [res])
            for m in other:
                sent.append(await self.client.send_file(chat_id, media_store.path_of(m),
                                                        voice_note=m["kind"] == "voice",
                                                        force_document=m["kind"] == "file"))
            for msg in sent:
                self._our_ids.add(msg.id)
            if len(self._our_ids) > 5000:
                self._our_ids = set(sorted(self._our_ids)[-1000:])
            return sent[-1].id if sent else None
        finally:
            asyncio.get_running_loop().call_later(3, self._sending.discard, chat_id)

    async def _deliver(self, chat, customer, replies, delay):
        for template, kind, rule_id, media_ids in replies:
            media = db.get_media_list(media_ids)
            if delay > 0:
                action = media_store.chat_action(media, "record-audio", "photo")
                async with self.client.action(chat["chat_id"], action):
                    await asyncio.sleep(min(delay, 30))
            fresh = db.get_chat(chat["id"])
            if fresh and (fresh["bot_disabled"] or (fresh["paused_until"] or 0) > time.time()):
                return  # you jumped in while it was "typing"
            body = rules.render(template, customer).strip()
            try:
                await self.send(chat["chat_id"], body, media)
                db.log_message(chat["id"], "bot", media_store.log_text(body, media), kind=kind, rule_id=rule_id)
            except Exception as e:
                log.warning("Account %s failed to send: %s", self.id, e)
                db.log_message(chat["id"], "bot", f"⚠️ Failed to send: {e}", kind="error")
                return


# ======================================================================
# A login in progress (QR code or phone + code)
# ======================================================================
class Login:
    def __init__(self, method):
        self.id = secrets.token_urlsafe(12)
        self.method = method              # "qr" | "phone"
        self.session = "acc_" + secrets.token_hex(8)
        self.state = "starting"           # waiting_scan | need_code | need_password | done | error
        self.client = None
        self.url = None                   # QR login link (tg://login?token=...)
        self.phone = None
        self.code_hash = None
        self.error = None
        self.account = None
        self.task = None
        self.created = time.time()

    def view(self):
        return {"id": self.id, "method": self.method, "state": self.state, "url": self.url,
                "error": self.error, "account": self.account}


# ======================================================================
# Manager: all accounts + logins
# ======================================================================
class Manager:
    kind = "user"

    def __init__(self, api_id, api_hash):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.loop = None
        self.ready = threading.Event()
        self.runners = {}
        self.logins = {}
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def make_client(self, session_name):
        return TelegramClient(os.path.join(SESSIONS_DIR, session_name), self.api_id, self.api_hash,
                              device_model="Auto Reply Admin", system_version="1.0", app_version="1.0")

    # ---------- thread / loop ----------
    def run_forever(self):
        asyncio.run(self._main())

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self._import_old_login()
        for acc in db.list_accounts():
            if acc["enabled"]:
                self._start_runner(acc)
        self.ready.set()
        while True:
            await asyncio.sleep(30)
            await self._drop_stale_logins()

    def _call(self, coro, timeout=60):
        if not self.ready.wait(15):
            coro.close()
            raise ValueError("The account manager is still starting, try again in a moment")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _import_old_login(self):
        """Earlier versions used a single `account.session` file; turn it into the first account."""
        old = os.path.join(BASE_DIR, "account.session")
        if os.path.exists(old):
            name = "acc_" + secrets.token_hex(8)
            shutil.move(old, os.path.join(SESSIONS_DIR, name + ".session"))
            db.create_account(name, name="(connecting…)")
            log.info("Imported old account.session as a new account")

    def _start_runner(self, acc):
        runner = Runner(self, acc)
        self.runners[acc["id"]] = runner
        runner.start()

    # ---------- accounts (called from the web thread) ----------
    def list_accounts(self):
        out = []
        for acc in db.list_accounts():
            r = self.runners.get(acc["id"])
            out.append({**acc, "online": bool(r and r.online and r.client and r.client.is_connected()),
                        "last_error": r.last_error if r else None})
        return out

    def set_enabled(self, account_id, enabled):
        return self._call(self._set_enabled(account_id, enabled))

    async def _set_enabled(self, account_id, enabled):
        acc = db.get_account(account_id)
        if not acc:
            raise ValueError("Account not found")
        db.update_account(account_id, enabled=int(enabled))
        runner = self.runners.pop(account_id, None)
        if runner:
            await runner.stop()
        if enabled:
            self._start_runner(db.get_account(account_id))
        return True

    def remove(self, account_id):
        return self._call(self._remove(account_id))

    async def _remove(self, account_id):
        acc = db.get_account(account_id)
        if not acc:
            raise ValueError("Account not found")
        runner = self.runners.pop(account_id, None)
        if runner:
            await runner.stop()
        await self._log_out_session(acc["session_name"])
        db.delete_account(account_id)
        return True

    async def _log_out_session(self, session_name):
        """End the session on Telegram's side (it disappears from Settings → Devices) and delete the file."""
        client = self.make_client(session_name)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()
        except Exception as e:
            log.warning("Could not log out session %s: %s", session_name, e)
        finally:
            await client.disconnect()
            _delete_session_files(session_name)

    def send_text(self, account_id, chat_id, text):
        runner = self.runners.get(account_id)
        if not runner:
            raise ValueError("This account is turned off or was removed")
        return self._call(runner.send(chat_id, text), timeout=30)

    # ---------- login (called from the web thread) ----------
    def start_qr_login(self):
        return self._call(self._start_qr_login())

    def start_phone_login(self, phone):
        return self._call(self._start_phone_login(phone))

    def submit_code(self, login_id, code):
        return self._call(self._submit_code(login_id, code))

    def submit_password(self, login_id, password):
        return self._call(self._submit_password(login_id, password))

    def login_status(self, login_id):
        return self._get_login(login_id).view()

    def cancel_login(self, login_id):
        return self._call(self._cancel_login(login_id))

    def _get_login(self, login_id):
        lg = self.logins.get(login_id)
        if not lg:
            raise ValueError("This login has expired. Please start again.")
        return lg

    async def _start_qr_login(self):
        lg = Login("qr")
        lg.client = self.make_client(lg.session)
        await lg.client.connect()
        qr = await lg.client.qr_login()
        lg.url, lg.state = qr.url, "waiting_scan"
        self.logins[lg.id] = lg
        lg.task = asyncio.create_task(self._wait_for_qr_scan(lg, qr))
        return lg.view()

    async def _wait_for_qr_scan(self, lg, qr):
        while lg.state == "waiting_scan":
            try:
                await qr.wait()              # waits until scanned or until this QR code expires (~30 s)
                await self._finish_login(lg)
            except asyncio.TimeoutError:
                await asyncio.sleep(1)       # Telegram returns the same token until it has really expired
                await qr.recreate()
                lg.url = qr.url
            except errors.SessionPasswordNeededError:
                lg.state = "need_password"
            except Exception as e:
                lg.state, lg.error = "error", str(e)

    async def _start_phone_login(self, phone):
        if ":" in str(phone):
            raise ValueError("That looks like a bot token. Enter a PHONE NUMBER, e.g. +85512345678")
        phone = "".join(ch for ch in str(phone) if ch.isdigit() or ch == "+")
        if len(phone.lstrip("+")) < 8:
            raise ValueError("Enter a phone number with country code, e.g. +85512345678")
        lg = Login("phone")
        lg.client = self.make_client(lg.session)
        await lg.client.connect()
        try:
            sent = await lg.client.send_code_request(phone)
        except errors.PhoneNumberInvalidError:
            await self._discard(lg)
            raise ValueError("This phone number is not valid") from None
        except errors.FloodWaitError as e:
            await self._discard(lg)
            raise ValueError(f"Too many attempts. Telegram asks to wait {e.seconds} seconds.") from None
        except Exception as e:
            await self._discard(lg)
            raise ValueError(f"Could not send the code: {e}") from None
        lg.phone, lg.code_hash, lg.state = phone, sent.phone_code_hash, "need_code"
        self.logins[lg.id] = lg
        return lg.view()

    async def _submit_code(self, login_id, code):
        lg = self._get_login(login_id)
        code = "".join(ch for ch in str(code) if ch.isdigit())
        try:
            await lg.client.sign_in(phone=lg.phone, code=code, phone_code_hash=lg.code_hash)
        except errors.SessionPasswordNeededError:
            lg.state = "need_password"
            return lg.view()
        except errors.PhoneCodeInvalidError:
            raise ValueError("Wrong code. Check the code in your Telegram app and try again.") from None
        except errors.PhoneCodeExpiredError:
            raise ValueError("The code expired. Cancel and start again.") from None
        await self._finish_login(lg)
        return lg.view()

    async def _submit_password(self, login_id, password):
        lg = self._get_login(login_id)
        try:
            await lg.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise ValueError("Wrong two-step verification password") from None
        await self._finish_login(lg)
        return lg.view()

    async def _finish_login(self, lg):
        me = await lg.client.get_me()
        await lg.client.disconnect()
        if me.bot:
            await self._discard(lg)
            lg.state, lg.error = "error", "That is a bot. Log in with a normal Telegram account."
            return
        info = {"user_id": me.id, "name": _full_name(me), "username": me.username,
                "phone": ("+" + me.phone) if me.phone else None}
        existing = next((a for a in db.list_accounts() if a["user_id"] == me.id), None)
        if existing:
            # Same person logged in again: switch the account to the new session, keep its chats.
            runner = self.runners.pop(existing["id"], None)
            if runner:
                await runner.stop()
            await self._log_out_session(existing["session_name"])
            db.update_account(existing["id"], session_name=lg.session, enabled=1, **info)
            account_id = existing["id"]
        else:
            account_id = db.create_account(lg.session, **info)
        self._start_runner(db.get_account(account_id))
        lg.state, lg.account = "done", info["name"]
        log.info("Logged in %s", info["name"])

    async def _cancel_login(self, login_id):
        lg = self.logins.pop(login_id, None)
        if lg and lg.state != "done":
            await self._discard(lg)
        return True

    async def _discard(self, lg):
        if lg.task and lg.task is not asyncio.current_task():
            lg.task.cancel()
        if lg.client:
            await lg.client.disconnect()
        _delete_session_files(lg.session)

    async def _drop_stale_logins(self):
        for login_id, lg in list(self.logins.items()):
            if time.time() - lg.created > LOGIN_TTL:
                self.logins.pop(login_id, None)
                if lg.state != "done":
                    await self._discard(lg)
