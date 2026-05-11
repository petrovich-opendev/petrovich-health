"""Telegram API primitives + send_and_log (TG send + chat_log dedup)."""
from __future__ import annotations

import logging
import os
import re as _re
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import insert_chat_message

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
POLL_TIMEOUT_SEC = 30
MAX_MSG_LEN = 3800

log = logging.getLogger("health-bot")

_HTML_TAG_RE = _re.compile(r"<[^>]+>")


def tg_api(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=kwargs, timeout=POLL_TIMEOUT_SEC + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def get_updates(offset: int | None = None) -> list[dict]:
    params: dict = {"timeout": POLL_TIMEOUT_SEC,
                    "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    return tg_api("getUpdates", **params).get("result", [])


def split_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        addition = block if not current else "\n\n" + block
        if len(current) + len(addition) > limit:
            if current:
                parts.append(current)
            current = block
        else:
            current += addition
    if current:
        parts.append(current)
    return parts or [text[:limit]]


def send_message(chat_id: str, text: str,
                 reply_markup: dict | None = None) -> None:
    chunks = split_message(text)
    # reply_markup attaches to the LAST chunk only (Telegram convention —
    # one keyboard per logical message, even when split for the 4096 cap).
    for i, chunk in enumerate(chunks):
        params: dict = {
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if reply_markup is not None and i == len(chunks) - 1:
            params["reply_markup"] = reply_markup
        tg_api("sendMessage", **params)


def send_photo(chat_id: str, photo_bytes: bytes, caption: str = "") -> None:
    """Send a photo (PNG bytes) to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    resp = requests.post(url, data=data, files=files, timeout=30)
    if resp.status_code != 200:
        log.error("sendPhoto failed: %s", resp.text[:200])


def send_document_file(chat_id: str, doc_bytes: bytes, filename: str, caption: str = "") -> None:
    """Send a document file to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {"document": (filename, doc_bytes, "application/pdf")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    resp = requests.post(url, data=data, files=files, timeout=30)
    if resp.status_code != 200:
        log.error("sendDocument failed: %s", resp.text[:200])


def download_photo(file_id: str, dest_path: Path) -> None:
    """Download a photo from Telegram."""
    file_info = tg_api("getFile", file_id=file_id)
    file_path = file_info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)


def download_file(file_id: str, dest_path: Path) -> None:
    """Download a file from Telegram to local disk."""
    file_info = tg_api("getFile", file_id=file_id)
    file_path = file_info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)


def send_typing(chat_id: str) -> None:
    try:
        tg_api("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass


def send_and_log(chat_id: str, text: str, owner_id: str,
                 reply_markup: dict | None = None) -> None:
    """Send a bot-facing message to Telegram and record it in chat_log.

    Guarantees:
      - splits long texts on \\n\\n boundaries
      - retries each chunk as plain text if HTML parse fails (Telegram 400)
      - chat_log is written ONLY if at least one chunk reached the user —
        never fake a delivery

    ``reply_markup`` attaches an inline keyboard to the LAST chunk only
    (Telegram convention: one keyboard per logical message). Used by
    reminders to surface ✅ Принял / ⏭️ Пропустил / 🤔 Забыл inline buttons
    that drive the adherence pipeline.
    """
    if not text:
        return
    chunks = split_message(text)
    sent_any = False
    sent_all = True
    first_message_id = 0
    for i, chunk in enumerate(chunks):
        delivered = False
        is_last = i == len(chunks) - 1
        params = {"chat_id": chat_id, "text": chunk,
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup is not None and is_last:
            params["reply_markup"] = reply_markup
        try:
            resp = tg_api("sendMessage", **params)
            if not first_message_id:
                first_message_id = int(resp.get("result", {}).get("message_id") or 0)
            delivered = True
        except Exception as exc:
            log.warning("sendMessage HTML failed (%s) — retrying plain", exc)
            plain = _HTML_TAG_RE.sub("", chunk)
            params_plain = {"chat_id": chat_id, "text": plain,
                            "disable_web_page_preview": True}
            if reply_markup is not None and is_last:
                params_plain["reply_markup"] = reply_markup
            try:
                resp = tg_api("sendMessage", **params_plain)
                if not first_message_id:
                    first_message_id = int(resp.get("result", {}).get("message_id") or 0)
                delivered = True
            except Exception as exc2:
                log.error("sendMessage plain failed: %s", exc2)
        if delivered:
            sent_any = True
        else:
            sent_all = False
    if not sent_any:
        log.error("send_and_log: ALL %d chunks failed for chat=%s — NOT logging to chat_log",
                  len(chunks), chat_id)
        return
    logged_text = text if sent_all else (text + "\n[partial delivery]")
    try:
        # first_message_id anchors the bot reply to its actual Telegram id —
        # makes (owner_id, message_id) a real key for bot rows too, and lets
        # dedup catch a retransmit of the same reply.
        insert_chat_message("bot", logged_text[:10000],
                            message_id=first_message_id, owner_id=owner_id)
    except Exception as exc:
        log.error("chat_log insert failed: %s", exc)
