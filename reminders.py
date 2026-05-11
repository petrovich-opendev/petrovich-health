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
                 days: list[int] | None = None,
                 target_date=None,
                 drug_name: str = "", drug_inn: str = "",
                 dose: str = "") -> str:
    """Add a new reminder. target_date for one-shot reminders.

    ``drug_name`` / ``drug_inn`` / ``dose`` are optional. When set, the
    fired reminder gets inline ✅/⏭️/🤔 buttons that feed PDC adherence.
    Reminders without drug_name remain generic (e.g., "позвонить врачу").
    """
    rid = str(uuid.uuid4())
    ch.insert("reminders", [[
        rid, owner_id, chat_id, text,
        hour, minute, days or [], True,
        datetime.now(), None, target_date,
        drug_inn, dose, drug_name,
    ]], column_names=[
        "id", "owner_id", "chat_id", "text",
        "hour", "minute", "days", "active",
        "created_at", "last_sent_at", "target_date",
        "drug_inn", "dose", "drug_name",
    ])

    if target_date:
        return (
            f"Напоминание установлено:\n"
            f"  {text}\n"
            f"  Когда: <b>{target_date} в {hour:02d}:{minute:02d} МСК</b>\n"
            f"  ID: <code>{rid[:8]}</code>"
        )
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
    today = now.date()

    result = ch.query(
        f"SELECT id, owner_id, chat_id, text, hour, minute, days, last_sent_at, target_date, "
        f"drug_name, drug_inn, dose "
        f"FROM reminders WHERE active = true "
        f"AND hour = {current_hour} AND minute = {current_minute}"
    )

    due = []
    for row in result.result_rows:
        (rid, owner_id, chat_id, text, hour, minute, days, last_sent, target_date,
         drug_name, drug_inn, dose) = row

        # One-shot reminder: check target_date matches today
        if target_date:
            from datetime import date as date_cls
            td = target_date if isinstance(target_date, date_cls) else date_cls.fromisoformat(str(target_date))
            if td != today:
                continue
        else:
            # Recurring: check day-of-week filter
            if days and current_dow not in days:
                continue

        # Check not already sent this minute.
        # ClickHouse stores DateTime in server TZ (normally UTC) as naive.
        # Treat it as UTC and compare to now() in UTC — never "rebrand" naive UTC
        # as MSK (that's the bug that caused double-fire within the same minute).
        if last_sent:
            last_dt = last_sent if isinstance(last_sent, datetime) else datetime.fromisoformat(str(last_sent))
            last_utc = last_dt if last_dt.tzinfo else last_dt.replace(tzinfo=timezone.utc)
            now_utc_minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
            if last_utc >= now_utc_minute:
                continue

        due.append({
            "id": str(rid),
            "owner_id": owner_id,
            "chat_id": chat_id,
            "text": text,
            "is_oneshot": target_date is not None,
            "drug_name": drug_name or "",
            "drug_inn": drug_inn or "",
            "dose": dose or "",
        })

    return due


def mark_sent(ch, rid: str, deactivate: bool = False) -> None:
    """Update last_sent_at after sending. Deactivate one-shot reminders."""
    if deactivate:
        ch.command(
            f"ALTER TABLE reminders UPDATE last_sent_at = now(), active = false "
            f"WHERE id = '{rid}'"
        )
    else:
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


# ─── Natural language reminder parser ───────────────────────────────────────

_REMINDER_RE = re.compile(
    r'(?:напомни|напомнить|напоминание|remind|поставь\s+напоминание|'
    r'разбуди|будильник|не\s+забыть|не\s+забудь)',
    re.IGNORECASE,
)


def looks_like_reminder(text: str) -> bool:
    """Does the text look like a reminder request?"""
    return bool(_REMINDER_RE.search(text))


def parse_natural_reminder(text: str) -> dict | None:
    """Parse natural language reminder request.

    Examples:
      'напомни завтра в 9:00 посмотреть анализы'
      'напомни в 15:30 принять BPC'
      'напомни послезавтра в 8 утра сдать кровь'
      'напомни через 2 часа проверить почту'

    Returns: {hour, minute, target_date (or None), text} or None.
    """
    text_lower = text.lower().strip()

    # Must contain reminder trigger
    if not _REMINDER_RE.search(text_lower):
        return None

    now = datetime.now(MSK_TZ)
    target_date = None
    hour = None
    minute = 0

    # ── Extract time ──
    # "в 9:00", "в 9", "в 15:30"
    m_time = re.search(r'в\s+(\d{1,2})[:\.](\d{2})', text_lower)
    if m_time:
        hour = int(m_time.group(1))
        minute = int(m_time.group(2))
    else:
        m_time = re.search(r'в\s+(\d{1,2})\s*(?:утра|часов|час|ч\.?)', text_lower)
        if m_time:
            hour = int(m_time.group(1))
        else:
            m_time = re.search(r'в\s+(\d{1,2})\s', text_lower)
            if m_time:
                h = int(m_time.group(1))
                if 0 <= h <= 23:
                    hour = h

    # "через N часов/минут"
    m_delta = re.search(r'через\s+(\d+)\s*(?:час|ч)', text_lower)
    if m_delta:
        delta_h = int(m_delta.group(1))
        target_time = now + timedelta(hours=delta_h)
        hour = target_time.hour
        minute = target_time.minute
        target_date = target_time.date()

    m_delta_m = re.search(r'через\s+(\d+)\s*(?:минут|мин)', text_lower)
    if m_delta_m:
        delta_m = int(m_delta_m.group(1))
        target_time = now + timedelta(minutes=delta_m)
        hour = target_time.hour
        minute = target_time.minute
        target_date = target_time.date()

    if hour is None:
        return None

    if hour > 23 or minute > 59:
        return None

    # ── Extract date ──
    if target_date is None:
        if re.search(r'послезавтра', text_lower):
            target_date = (now + timedelta(days=2)).date()
        elif re.search(r'завтра', text_lower):
            target_date = (now + timedelta(days=1)).date()
        elif re.search(r'сегодня', text_lower):
            target_date = now.date()
        else:
            # No date specified — today if time is in future, tomorrow otherwise
            if hour > now.hour or (hour == now.hour and minute > now.minute):
                target_date = now.date()
            else:
                target_date = (now + timedelta(days=1)).date()

    # ── Extract reminder text ──
    # Remove the trigger and time/date parts, keep the actual reminder content
    reminder_text = text
    # Remove trigger words
    reminder_text = _REMINDER_RE.sub('', reminder_text).strip()
    # Remove time patterns
    reminder_text = re.sub(r'в\s+\d{1,2}[:.]\d{2}', '', reminder_text).strip()
    reminder_text = re.sub(r'в\s+\d{1,2}\s*(?:утра|часов|час|ч\.?)?\b', '', reminder_text).strip()
    reminder_text = re.sub(r'через\s+\d+\s*(?:часов?|ч|минут|мин)\w*', '', reminder_text).strip()
    # Remove date words
    reminder_text = re.sub(r'(?:послезавтра|завтра|сегодня)', '', reminder_text, flags=re.IGNORECASE).strip()
    # Remove stray prepositions left after removal
    reminder_text = re.sub(r'\bна\s+(?=\s|$)', '', reminder_text).strip()
    reminder_text = re.sub(r'\bмне\b', '', reminder_text).strip()
    # Clean up punctuation and whitespace
    reminder_text = re.sub(r'^[\s,.\-!:;]+', '', reminder_text).strip()
    reminder_text = re.sub(r'[\s,.\-!:;]+$', '', reminder_text).strip()
    reminder_text = re.sub(r'\s{2,}', ' ', reminder_text).strip()

    if not reminder_text:
        reminder_text = "Напоминание"

    # Capitalize first letter
    reminder_text = reminder_text[0].upper() + reminder_text[1:] if reminder_text else reminder_text

    return {
        "hour": hour,
        "minute": minute,
        "target_date": target_date,
        "text": reminder_text,
    }


def _format_days(days: list[int] | None) -> str:
    if not days:
        return "каждый день"
    return ", ".join(DAY_NAMES.get(d, str(d)) for d in sorted(days))
