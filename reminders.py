"""
Reminder system — schedule, check, send via Telegram.

Reminders stored in CH, checked every 60 seconds by background thread.
Natural language parsing via LLM for add/edit/delete.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from datetime import datetime, timezone, timedelta

log = logging.getLogger("health-bot")
MSK_TZ = timezone(timedelta(hours=3))

DAY_NAMES = {
    1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс",
}
DAY_NAMES_REV = {
    "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6, "вс": 7,
    "понедельник": 1, "вторник": 2, "среда": 3, "четверг": 4,
    "пятница": 5, "суббота": 6, "воскресенье": 7,
}


def get_reminders(ch, owner_id: str) -> list[dict]:
    """Get all active reminders for user."""
    result = ch.query(
        f"SELECT id, text, hour, minute, days, active "
        f"FROM reminders WHERE owner_id = '{owner_id}' AND active = true "
        f"ORDER BY hour, minute"
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def get_all_reminders(ch, owner_id: str) -> list[dict]:
    """Get all reminders including inactive."""
    result = ch.query(
        f"SELECT id, text, hour, minute, days, active, created_at "
        f"FROM reminders WHERE owner_id = '{owner_id}' "
        f"ORDER BY active DESC, hour, minute"
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def add_reminder(ch, owner_id: str, chat_id: str,
                 text: str, hour: int, minute: int,
                 days: list[int] | None = None) -> str:
    """Add a new reminder. Returns confirmation message."""
    rid = str(uuid.uuid4())
    ch.insert("reminders", [[
        rid, owner_id, chat_id, text,
        hour, minute, days or [], True,
        datetime.now(), None,
    ]], column_names=[
        "id", "owner_id", "chat_id", "text",
        "hour", "minute", "days", "active",
        "created_at", "last_sent_at",
    ])

    days_str = _format_days(days)
    return (
        f"Напоминание добавлено:\n"
        f"  {text}\n"
        f"  Время: {hour:02d}:{minute:02d} МСК\n"
        f"  Дни: {days_str}\n"
        f"  ID: <code>{rid[:8]}</code>"
    )


def delete_reminder(ch, owner_id: str, rid_prefix: str) -> str:
    """Delete (deactivate) reminder by ID prefix."""
    reminders = get_all_reminders(ch, owner_id)
    for r in reminders:
        rid = str(r["id"])
        if rid.startswith(rid_prefix) or rid_prefix in rid[:8]:
            # ReplacingMergeTree — insert with active=false
            ch.command(
                f"ALTER TABLE reminders UPDATE active = false "
                f"WHERE id = '{rid}' AND owner_id = '{owner_id}'"
            )
            return f"Напоминание удалено: {r['text']} ({r['hour']:02d}:{r['minute']:02d})"
    return f"Напоминание с ID '{rid_prefix}' не найдено."


def format_reminders_list(reminders: list[dict]) -> str:
    """Format reminders for display."""
    if not reminders:
        return "Нет активных напоминаний."
    lines = ["<b>Напоминания:</b>\n"]
    for r in reminders:
        status = "✅" if r.get("active", True) else "❌"
        days_str = _format_days(r.get("days", []))
        lines.append(
            f"{status} {r['hour']:02d}:{r['minute']:02d} | {r['text']}\n"
            f"    Дни: {days_str} | ID: <code>{str(r['id'])[:8]}</code>"
        )
    return "\n".join(lines)


def check_due_reminders(ch) -> list[dict]:
    """Check which reminders are due right now. Called every 60 sec."""
    now = datetime.now(MSK_TZ)
    current_hour = now.hour
    current_minute = now.minute
    current_dow = now.isoweekday()  # 1=Mon..7=Sun

    result = ch.query(
        f"SELECT id, owner_id, chat_id, text, hour, minute, days, last_sent_at "
        f"FROM reminders WHERE active = true "
        f"AND hour = {current_hour} AND minute = {current_minute}"
    )

    due = []
    for row in result.result_rows:
        rid, owner_id, chat_id, text, hour, minute, days, last_sent = row

        # Check day-of-week filter
        if days and current_dow not in days:
            continue

        # Check not already sent this minute
        if last_sent:
            last_dt = last_sent if isinstance(last_sent, datetime) else datetime.fromisoformat(str(last_sent))
            if last_dt.replace(tzinfo=MSK_TZ) >= now.replace(second=0, microsecond=0):
                continue

        due.append({
            "id": str(rid),
            "owner_id": owner_id,
            "chat_id": chat_id,
            "text": text,
        })

    return due


def mark_sent(ch, rid: str) -> None:
    """Update last_sent_at after sending."""
    ch.command(
        f"ALTER TABLE reminders UPDATE last_sent_at = now() "
        f"WHERE id = '{rid}'"
    )


def parse_reminder_command(text: str) -> dict | None:
    """
    Try to parse simple reminder commands without LLM.
    Formats:
      /remind 09:00 Принять BPC-157
      /remind 09:00 пн,ср,пт Принять BPC-157
      /remind удалить abc123
      /remind список
    """
    text = text.strip()
    if not text.startswith("/remind"):
        return None

    body = text[len("/remind"):].strip()

    if not body or body.lower() in ("список", "list"):
        return {"action": "list"}

    # Delete
    if body.lower().startswith(("удалить ", "удали ", "delete ", "del ")):
        rid = body.split(maxsplit=1)[1].strip()
        return {"action": "delete", "id": rid}

    # Add: try to parse time + optional days + text
    # Pattern: HH:MM [days] text
    m = re.match(r"(\d{1,2}):(\d{2})\s*(.*)", body)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2))
    rest = m.group(3).strip()

    if hour > 23 or minute > 59:
        return None

    # Check for day names at start
    days = []
    words = rest.split(maxsplit=1)
    if words:
        day_part = words[0].lower().rstrip(",")
        day_tokens = [d.strip() for d in day_part.split(",")]
        parsed_days = []
        for dt in day_tokens:
            if dt in DAY_NAMES_REV:
                parsed_days.append(DAY_NAMES_REV[dt])
        if parsed_days:
            days = parsed_days
            rest = words[1] if len(words) > 1 else ""

    if not rest:
        return None

    return {"action": "add", "hour": hour, "minute": minute, "days": days, "text": rest}


def _format_days(days: list[int] | None) -> str:
    if not days:
        return "каждый день"
    return ", ".join(DAY_NAMES.get(d, str(d)) for d in sorted(days))
