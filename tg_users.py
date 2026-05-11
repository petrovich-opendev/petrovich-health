"""User registry — users.yaml load, resolve, admin chat-ids, add-on-request."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml as _yaml

USERS_YAML_PATH = Path(__file__).resolve().parent / "users.yaml"

# Pending access requests: {chat_id: {name, username, first_name, last_name, requested_at}}
_access_requests: dict[str, dict] = {}

log = logging.getLogger("health-bot")

# Attacker-controlled fields from Telegram (first_name, last_name, username)
# eventually land in users.yaml via /approve. yaml.safe_load on next bot start
# parses whatever ends up there — a name containing ": &x\n!!python/" is at
# minimum a yaml-corruption DoS, at worst a parser-version-specific exploit.
# Whitelist: letters (incl. Cyrillic), digits, space, hyphen, dot, underscore.
_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._\-А-Яа-яЁё]")
_USERNAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize_name(s: str, max_len: int = 64) -> str:
    """Clean attacker-controlled display name before writing/logging."""
    if not s:
        return ""
    s = _NAME_SAFE_RE.sub("", s).strip()
    return s[:max_len]


def _sanitize_username(s: str, max_len: int = 32) -> str:
    """Telegram usernames are A-Za-z0-9_ per spec; we enforce that."""
    if not s:
        return ""
    s = _USERNAME_SAFE_RE.sub("", s.lstrip("@"))
    return s[:max_len]


def load_users() -> dict:
    """Load users.yaml → {username_lower: {name, role, chat_id}}.

    Fails-closed on duplicate chat_id across users — that would make
    resolve_user's chat_id fallback non-deterministic (first-row-wins),
    which is exactly the cross-tenant identity confusion we want to
    refuse rather than tolerate.
    """
    if not USERS_YAML_PATH.exists():
        return {}
    data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
    users = {}
    seen_chat_ids: set[str] = set()
    for u in data.get("users", []):
        uname = u.get("username", "").lower().lstrip("@")
        chat_id = str(u.get("chat_id", ""))
        if chat_id and chat_id in seen_chat_ids:
            log.error("DUPLICATE chat_id=%s in users.yaml — refusing to load (fail-closed)", chat_id)
            return {}
        if chat_id:
            seen_chat_ids.add(chat_id)
        if uname:
            users[uname] = {
                "name": u.get("name", uname),
                "role": u.get("role", "user"),
                "chat_id": chat_id,
            }
    return users


def resolve_user(message: dict) -> dict | None:
    """Check if message sender is authorized. Returns user dict with owner_id or None."""
    users = load_users()
    from_user = message.get("from", {})
    username = (from_user.get("username") or "").lower()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if username in users:
        user = users[username]
        if not user["chat_id"] and chat_id:
            _save_chat_id(username, chat_id)
        return {"owner_id": chat_id, "name": user["name"], "role": user["role"]}

    for uname, u in users.items():
        if u["chat_id"] == chat_id:
            return {"owner_id": chat_id, "name": u["name"], "role": u["role"]}

    return None


def _save_chat_id(username: str, chat_id: str) -> None:
    """Auto-save resolved chat_id back to users.yaml."""
    try:
        data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
        for u in data.get("users", []):
            if u.get("username", "").lower().lstrip("@") == username:
                u["chat_id"] = int(chat_id)
                break
        USERS_YAML_PATH.write_text(
            _yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        log.info("Saved chat_id=%s for @%s", chat_id, username)
    except Exception as exc:
        log.warning("Failed to save chat_id: %s", exc)


def _get_admin_chat_ids() -> list[str]:
    """Get chat_ids of all admin users."""
    users = load_users()
    return [u["chat_id"] for u in users.values()
            if u.get("role") == "admin" and u.get("chat_id")]


def _add_user_to_yaml(name: str, chat_id: str, username: str = "") -> bool:
    """Add a new user to users.yaml. Returns True on success.

    name/username come from Telegram profile fields — sanitise BEFORE the
    write so a hostile display name like "x\\n!!python/object" can't
    corrupt the YAML file or break yaml.safe_load on next start.
    """
    try:
        # chat_id from /approve admin argument — must look like a Telegram chat_id.
        if not re.match(r"^-?\d{1,19}$", str(chat_id)):
            log.warning("Refusing to add user: chat_id %r not digits", chat_id)
            return False
        name = _sanitize_name(name)
        username = _sanitize_username(username)
        if not name:
            log.warning("Refusing to add user: name empty after sanitisation")
            return False

        data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
        users_list = data.get("users", [])

        for u in users_list:
            if str(u.get("chat_id", "")) == str(chat_id):
                return False
            if username and u.get("username", "").lower() == username.lower():
                return False

        new_user = {"name": name, "role": "user", "chat_id": int(chat_id)}
        if username:
            new_user["username"] = username
        users_list.append(new_user)

        data["users"] = users_list
        USERS_YAML_PATH.write_text(
            _yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        log.info("Added user: %s (chat_id=%s, @%s)", name, chat_id, username or "no_username")
        return True
    except Exception as exc:
        log.error("Failed to add user: %s", exc)
        return False
