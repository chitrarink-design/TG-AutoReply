"""Uploaded attachments (photos, voice messages, ...) for replies."""
import os
import secrets
import shutil
import subprocess

import db
from config import BASE_DIR

MEDIA_DIR = os.path.join(BASE_DIR, "media")
MAX_SIZE = 20 * 1024 * 1024  # 20 MB

EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".m4a", "audio/webm": ".webm",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "video/mp4": ".mp4", "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}
# Telegram plays these as real voice messages (round "play" bubble with waveform)
VOICE_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a"}


def path_of(m):
    return os.path.join(MEDIA_DIR, m["filename"])


def _ext(orig_name, mime):
    ext = os.path.splitext(orig_name or "")[1].lower()
    if ext and len(ext) <= 6 and ext[1:].isalnum():
        return ext
    return EXT_BY_MIME.get((mime or "").split(";")[0].strip().lower(), ".bin")


def _convert_to_ogg(src):
    """Convert any audio to OGG/Opus with ffmpeg (if installed). Returns new path or None."""
    if not shutil.which("ffmpeg"):
        return None
    dst = os.path.splitext(src)[0] + ".ogg"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c:a", "libopus", "-b:a", "48k", dst],
                       check=True, timeout=120)
        os.remove(src)
        return dst
    except Exception:
        return None


def save_upload(data, orig_name, mime, want_voice):
    """Store an uploaded file. Returns (media_row, note) - note is a warning for the admin, or None."""
    if not data:
        raise ValueError("The file is empty")
    if len(data) > MAX_SIZE:
        raise ValueError("The file is larger than 20 MB")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    ext = _ext(orig_name, mime)
    path = os.path.join(MEDIA_DIR, secrets.token_hex(8) + ext)
    with open(path, "wb") as f:
        f.write(data)

    note = None
    if mime.startswith("image/") and ext != ".gif":
        kind = "photo"
    elif mime.startswith("video/") or ext == ".gif":
        kind = "video"
    elif mime.startswith("audio/") or ext in VOICE_EXTS | {".webm", ".wav"}:
        kind = "voice" if want_voice else "audio"
        if kind == "voice" and ext not in VOICE_EXTS:
            converted = _convert_to_ogg(path)
            if converted:
                path, ext = converted, ".ogg"
            else:
                kind = "audio"
                note = ("This recording format can't be sent as a voice message (install ffmpeg to convert it), "
                        "so it will be sent as an audio file. Tip: use Safari, or upload an MP3/M4A file.")
    else:
        kind = "file"

    media_id = db.create_media(os.path.basename(path), orig_name or os.path.basename(path), kind, mime,
                               os.path.getsize(path))
    return db.get_media(media_id), note


def split(media):
    """(photos/videos, everything else) from a list of media rows."""
    media = media or []
    return [m for m in media if m["kind"] in ("photo", "video")], [m for m in media if m["kind"] not in ("photo", "video")]


def chat_action(media, voice_action, photo_action):
    """What the account shows while 'typing' (recording voice / sending photo / typing)."""
    kinds = {m["kind"] for m in media or []}
    if "photo" in kinds or "video" in kinds:
        return photo_action
    if "voice" in kinds:
        return voice_action
    return "typing"


def log_text(text, media):
    """How a sent reply appears in the Chats page."""
    if not media:
        return text
    icons = {"photo": "🖼 photo", "video": "🎬 video", "voice": "🎤 voice", "audio": "🎵 audio"}
    tags = " ".join(f"[{icons.get(m['kind'], '📎 file')}]" for m in media)
    return f"{tags} {text}".strip()
