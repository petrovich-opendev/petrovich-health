"""Proactive health alerts.

Module layout: each alerter exports a ``check_*(...)`` function that
returns ``list[dict]``. The /alerts Telegram command concatenates them.

* ``check_lab_fatigue`` — biomarkers on the cycle-watch list that haven't
  been resampled in too long. User runs a TRT + GH protocol; АЛТ/АСТ/
  гематокрит/ИФР-1/ЛПНП need to be checked at least every 6 weeks.

Alerters are pure read-only — no side effects, no LLM calls. Suitable
for cron, on-demand ``/alerts``, or other check pipelines.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

log = logging.getLogger("health-bot")


# Hardcoded watch list — judgment call: a config file is overkill for v1
# and these biomarkers are uniquely tied to the user's TRT+ГР protocol.
# If the protocol changes, edit here.
CYCLE_WATCH_BIOMARKERS = [
    "АЛТ",
    "АСТ",
    "Гематокрит",
    "Гемоглобин",
    "ИФР-1",
    "Тестостерон общий",
    "Эстрадиол",
    "ЛПНП",
    "ЛПВП",
    "ТТГ",
    "Креатинин",
    "25-OH витамин D",
]

# Recommended cadence on a TRT+ГР cycle.
WARN_WEEKS = 6
CRITICAL_WEEKS = 10


def _to_date(d: Any) -> date | None:
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        try:
            return date.fromisoformat(d)
        except ValueError:
            return None
    return None


def _weeks_between(then: date, now: date) -> int:
    return max(0, (now - then).days // 7)


def check_lab_fatigue(ch: Any, owner_id: str) -> list[dict]:
    """Return alerts for cycle-watch biomarkers not sampled recently.

    ``ch`` is a clickhouse client (``db.get_client()`` result). ``owner_id``
    is digit-only validated upstream — this function trusts it (mirrors
    ``db._own`` semantics; centralized validation lives in ``db.py``).

    For biomarkers never seen at all, ``last_seen=None``, ``weeks_since=999``,
    severity=critical.
    """
    # Reuse db._validate_owner_id rather than re-validating here.
    from db import _validate_owner_id

    owner_id = _validate_owner_id(owner_id)
    today = date.today()

    rows = ch.query(
        f"SELECT biomarker, max(collected_at) FROM lab_results "
        f"WHERE owner_id = '{owner_id}' GROUP BY biomarker"
    ).result_rows
    last_seen_map: dict[str, date] = {}
    for biomarker, ts in rows:
        d = _to_date(ts)
        if d is not None:
            last_seen_map[biomarker] = d

    alerts: list[dict] = []
    for bm in CYCLE_WATCH_BIOMARKERS:
        last = last_seen_map.get(bm)
        if last is None:
            alerts.append({
                "kind": "lab_fatigue",
                "biomarker": bm,
                "last_seen": None,
                "weeks_since": 999,
                "severity": "critical",
                "message": (
                    f"{bm} — ни разу не сдан, "
                    f"на ТЗТ+ГР цикле обязателен"
                ),
            })
            continue

        weeks = _weeks_between(last, today)
        if weeks < WARN_WEEKS:
            continue
        severity = "critical" if weeks > CRITICAL_WEEKS else "warn"
        alerts.append({
            "kind": "lab_fatigue",
            "biomarker": bm,
            "last_seen": last.isoformat(),
            "weeks_since": weeks,
            "severity": severity,
            "message": (
                f"{bm} — {weeks} нед. без замера, "
                f"на ТЗТ+ГР цикле рекомендуется не реже {WARN_WEEKS} недель"
            ),
        })

    # Order: critical first, then by weeks descending — biggest neglect at top.
    alerts.sort(
        key=lambda a: (0 if a["severity"] == "critical" else 1, -a["weeks_since"])
    )
    return alerts


# Severity icons used in /alerts Telegram output.
SEVERITY_ICONS = {"warn": "⚠️", "critical": "🚨"}


def format_alerts(alerts: list[dict]) -> str:
    """Render a combined alerts list as the /alerts Telegram message."""
    if not alerts:
        return "✅ Все ключевые биомаркеры сданы недавно, тренды стабильны."
    lines = ["🚨 <b>Активные алерты</b>"]
    for a in alerts:
        icon = SEVERITY_ICONS.get(a.get("severity", "warn"), "⚠️")
        lines.append(f"• {icon} {a['message']}")
    return "\n".join(lines)
