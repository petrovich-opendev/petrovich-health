"""User registry — users.yaml load, resolve, admin chat-ids, add-on-request."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml as _yaml

USERS_YAML_PATH = Path(__file__).resolve().parent / "users.yaml"

# Pending access requests: {chat_id: {name, username, first_name, last_name, requested_at}}
_access_requests: dict[str, dict] = {}

log = logging.getLogger("health-bot")


def load_users() -> dict:
    """Load users.yaml → {username_lower: {name, role, chat_id}}."""
    if not USERS_YAML_PATH.exists():
        return {}
    data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
    users = {}
    for u in data.get("users", []):
        uname = u.get("username", "").lower().lstrip("@")
        if uname:
            users[uname] = {
                "name": u.get("name", uname),
                "role": u.get("role", "user"),
                "chat_id": str(u.get("chat_id", "")),
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
    """Add a new user to users.yaml. Returns True on success."""
    try:
        data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
        users_list = data.get("users", [])

        for u in users_list:
            if str(u.get("chat_id", "")) == chat_id:
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
