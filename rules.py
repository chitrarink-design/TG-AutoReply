"""Keyword matching and reply templating."""
import re
import time

import db

MATCH_TYPES = ("contains", "exact", "starts", "regex")


def _norm(s):
    return " ".join((s or "").casefold().split())


def keywords_of(rule):
    return [k.strip() for k in (rule.get("keywords") or "").splitlines() if k.strip()]


def rule_matches(rule, text):
    if not text:
        return False
    t = _norm(text)
    mt = rule.get("match_type", "contains")
    for kw in keywords_of(rule):
        if mt == "regex":
            try:
                if re.search(kw, text, re.IGNORECASE):
                    return True
            except re.error:
                continue
        else:
            k = _norm(kw)
            if (mt == "contains" and k in t) or (mt == "exact" and k == t) or (mt == "starts" and t.startswith(k)):
                return True
    return False


def find_match(text):
    for rule in db.list_rules(enabled_only=True):
        if rule_matches(rule, text):
            return rule
    return None


def render(template, user):
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    username = user.get("username") or ""
    values = {
        "first_name": first or username or "",
        "last_name": last,
        "name": (first + " " + last).strip() or username,
        "username": ("@" + username) if username else "",
    }
    return re.sub(r"\{(first_name|last_name|name|username)\}", lambda m: values[m.group(1)], template or "")


def validate_rule(data):
    """Return (clean_rule, error)."""
    rule = {
        "name": str(data.get("name") or "").strip()[:100],
        "match_type": data.get("match_type") or "contains",
        "keywords": "\n".join(k.strip() for k in str(data.get("keywords") or "").splitlines() if k.strip()),
        "reply": str(data.get("reply") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "media_ids": data.get("media_ids") or [],
    }
    try:
        rule["priority"] = int(data.get("priority", 100))
    except (TypeError, ValueError):
        return None, "Priority must be a number"
    if rule["match_type"] not in MATCH_TYPES:
        return None, "Unknown match type"
    if not rule["keywords"]:
        return None, "Add at least one keyword"
    rule["media_ids"], err = clean_media_ids(rule["media_ids"])
    if err:
        return None, err
    if not rule["reply"] and not rule["media_ids"]:
        return None, "Add a reply text, a photo or a voice message"
    if len(rule["reply"]) > 4096:
        return None, "Reply is longer than Telegram's 4096 character limit"
    if rule["match_type"] == "regex":
        for kw in keywords_of(rule):
            try:
                re.compile(kw)
            except re.error as e:
                return None, f"Invalid regex '{kw}': {e}"
    if not rule["name"]:
        rule["name"] = keywords_of(rule)[0][:40]
    return rule, None


MAX_ATTACHMENTS = 10


def clean_media_ids(ids):
    """Validate a list of attachment ids. Returns (ids, error)."""
    if not isinstance(ids, list):
        return [], "Invalid attachments"
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return [], "Invalid attachments"
    if len(ids) > MAX_ATTACHMENTS:
        return [], f"At most {MAX_ATTACHMENTS} attachments per reply"
    if len(db.get_media_list(ids)) != len(ids):
        return [], "An attachment was not found, please upload it again"
    return ids, None


def plan_replies(chat, text):
    """Decide which replies to send. Returns list of (template, kind, rule_id, media_ids)."""
    s = db.get_settings()
    replies = []
    if not chat["welcomed"]:
        db.update_chat(chat["id"], welcomed=1)
        if s["welcome_enabled"] and ((s["welcome_text"] or "").strip() or s.get("welcome_media_ids")):
            replies.append((s["welcome_text"], "welcome", None, s.get("welcome_media_ids") or []))

    rule = find_match(text)
    if rule:
        replies.append((rule["reply"], "rule", rule["id"], rule.get("media_ids") or []))
        db.bump_rule_hits(rule["id"])
    elif not replies and s["fallback_enabled"] and ((s["fallback_text"] or "").strip() or s.get("fallback_media_ids")):
        cooldown = float(s.get("fallback_cooldown_minutes") or 0) * 60
        if time.time() - (chat["last_fallback_at"] or 0) >= cooldown:
            db.update_chat(chat["id"], last_fallback_at=time.time())
            replies.append((s["fallback_text"], "fallback", None, s.get("fallback_media_ids") or []))
    return replies
