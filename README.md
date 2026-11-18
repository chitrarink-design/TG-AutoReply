# Telegram Auto-Reply + Admin Website

Auto-replies to people who message **your Telegram accounts**. You can add as many accounts as you
like, and log them in from the admin website by **scanning a QR code** or with a phone number + code.
No Telegram Business or Premium is needed.

(There is also `MODE=bot`, where customers chat with a bot from @BotFather instead.)

## Features
- **Many accounts**: each one auto-replies using the same rules. Turn any account on or off, or remove it.
- **Easy login**: scan a QR code with Telegram on your phone, or use phone number + code (+ 2FA password if set).
- **Reply rules**: keywords (Khmer or English, one per line) → reply. Match types: contains / exact / starts with / regex. Priority sets the order.
- **Photos & voice**: any reply (welcome, default, each rule) can include a photo/video or a voice message.
  Record voice right in the browser, or upload an MP3 / M4A / OGG file. Photos are sent with the text as caption.
- **Welcome message**: sent once, the first time a person messages an account.
- **Default reply**: sent when no rule matches, with a cooldown so it isn't sent again and again.
- **Typing delay**: shows "typing…" before replying, so it feels natural.
- **Human takeover**: when you reply yourself (phone, desktop or the website), auto-replies pause in that chat for N minutes.
- **Chats page**: every conversation, showing which account it came to. Reply from the website, or turn auto-reply off for one person.
- **Test page**: try a message and see which reply would be sent.
- Only private one-to-one chats get replies. Groups, channels, bots and Telegram's service messages are ignored.
- Variables in replies: `{first_name}`, `{name}`, `{username}`.

## Setup

1. **Get API keys** (once): open https://my.telegram.org → log in → **API development tools** →
   create an app (any name). Copy **api_id** and **api_hash**. One pair works for all your accounts.
2. **Put them in `.env`**:
   ```
   MODE=user
   TG_API_ID=1234567
   TG_API_HASH=abcdef0123456789...
   ADMIN_PASSWORD=your-admin-password
   ```
3. **Install** (once):
   ```bash
   python3 -m venv .venv && .venv/bin/pip install telethon
   ```
4. **Run**:
   ```bash
   .venv/bin/python app.py
   ```
5. Open http://localhost:8080 → log in → **Accounts** → **+ Add account**.
   - **QR (easiest)**: on your phone open Telegram → *Settings → Devices → Link Desktop Device* → scan.
   - **Phone**: enter the number, then the code Telegram sends to your app.

### Important
- The files in `sessions/` give **full access to those Telegram accounts**. Never share or upload them.
  Removing an account on the website logs it out and deletes its file.
- Each account appears as **"Auto Reply Admin"** in Telegram → Settings → Devices.
- Telegram may limit accounts that send a lot of automated messages. Keep replies helpful, and
  don't run more than one copy of the app at a time.

## Running 24/7
Replies only happen while `app.py` is running. On a VPS, copy the folder **including `sessions/` and
`data.db`**, set `HOST=0.0.0.0`, and keep it running with `systemd`, `pm2`, `screen` or Docker. Put the
admin site behind HTTPS (Caddy or Nginx) before opening it to the internet, since you log in to
Telegram accounts through it.

## Files
| File | Purpose |
|---|---|
| `app.py` | Starts the auto-replier and the website |
| `accounts.py` | All logged-in accounts, QR / phone login (Telethon) |
| `bot.py` | Alternative: auto-reply as a bot (`MODE=bot`) |
| `rules.py` | Keyword matching, reply choice, `{variable}` templates |
| `media.py` | Uploaded photos / voice (stored in `media/`) |
| `db.py` | SQLite storage (`data.db`) |
| `web.py` | Admin website and JSON API |
| `static/index.html` | The admin UI |
