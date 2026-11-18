"""Entry point: starts the auto-replier and the admin website.

    .venv/bin/python app.py

MODE=user  -> replies from your own Telegram accounts (needs TG_API_ID, TG_API_HASH; log in on the website)
MODE=bot   -> customers chat with your bot (needs BOT_TOKEN)
"""
import logging
import os
import sys
import threading

from config import load_env

load_env()

import db  # noqa: E402
import web  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not password:
        sys.exit("ADMIN_PASSWORD is missing. Copy .env.example to .env and fill it in.")

    db.init()

    mode = os.environ.get("MODE", "user").strip().lower()
    if mode == "user":
        if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
            sys.exit("MODE=user needs TG_API_ID and TG_API_HASH in .env (from https://my.telegram.org).")
        try:
            import accounts
        except ImportError:
            sys.exit("Telethon is missing. Run with the project venv:  .venv/bin/python app.py")
        web.BOT = accounts.Manager(os.environ["TG_API_ID"], os.environ["TG_API_HASH"])
    elif mode == "bot":
        import bot
        token = os.environ.get("BOT_TOKEN", "").strip()
        if token:
            web.BOT = bot.Bot(token)
        else:
            print("WARNING: BOT_TOKEN is empty - only the admin website will run.")
    else:
        sys.exit("MODE must be 'user' or 'bot'.")

    if web.BOT:
        threading.Thread(target=web.BOT.run_forever, daemon=True).start()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    try:
        web.serve(host, port, password)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
