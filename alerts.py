"""Proactive health alerts.

Module layout: each alerter exports a ``check_*(...)`` function that
returns ``list[dict]``. The /alerts Telegram command concatenates them.

* ``check_lab_fatigue`` — biomarkers on the cycle-watch list that haven't
  been resampled in too long. User runs a TRT + GH protocol; АЛТ/АСТ/
  гематокрит/ИФР-1/ЛПНП need to be checked at least every 6 weeks.
* ``check_trend_reversals`` — XmR signal on a known-watched series. Reuses
  ``spc.compute_xmr`` and ``db.query_spc_data``; reads the structured
  ``SPCResult.signals`` field rather than parsing localized strings.

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

# Lab forms use multiple synonyms for the same analyte. Map canonical →
# extra accepted spellings observed in real lab_results. Canonical itself
# is always counted; do not duplicate it here.
BIOMARKER_ALIASES: dict[str, list[str]] = {
    "АЛТ": ["АЛТ (аланинаминотрансфераза)"],
    "АСТ": ["АСТ (аспартатаминотрансфераза)"],
    "ИФР-1": ["Соматомедин С (ИФР-1)", "Соматомедин С"],
    "Тестостерон общий": ["Тестостерон"],
    "25-OH витамин D": [
        "Витамин D",
        "Витамин D (25-OH)",
        "Витамин D3 (25-OH)",
        "Витамин D3 (25-гидроксивитамин D)",
    ],
}


def _accepted_names(canonical: str) -> list[str]:
    """Canonical name plus all known aliases."""
    return [canonical, *BIOMARKER_ALIASES.get(canonical, [])]

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
        last = max(
            (last_seen_map[name] for name in _accepted_names(bm) if name in last_seen_map),
            default=None,
        )
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


def check_trend_reversals(ch: Any, owner_id: str) -> list[dict]:
    """Return alerts for cycle-watch biomarkers showing SPC signals.

    Walks every series in ``query_spc_data`` for the owner, computes XmR,
    and emits a structured alert per signal-bearing series. We restrict to
    ``CYCLE_WATCH_BIOMARKERS`` — every other biomarker is informational and
    not worth interrupting the user for. ≥4 points required: with 2-3
    points the control limits are too noisy to trust a "reversal".

    ``ch`` is accepted for symmetry with ``check_lab_fatigue`` but the
    underlying ``query_spc_data`` opens its own client; we don't use it.
    """
    from db import _validate_owner_id, query_spc_data
    from spc import compute_xmr

    owner_id = _validate_owner_id(owner_id)
    series = query_spc_data(owner_id)

    # Reverse map: every accepted name → its canonical, so an alias series
    # is treated as the watch-list biomarker without merging series across
    # different lab spellings (units/methods may differ).
    accepted_to_canonical: dict[str, str] = {}
    for canonical in CYCLE_WATCH_BIOMARKERS:
        for name in _accepted_names(canonical):
            accepted_to_canonical[name] = canonical

    alerts: list[dict] = []
    for biomarker, points in series.items():
        if biomarker not in accepted_to_canonical:
            continue
        if len(points) < 4:
            continue

        result = compute_xmr(biomarker, points)
        if result is None or not result.signals:
            continue

        # Display the canonical watch-list name so /alerts is consistent
        # with check_lab_fatigue output even when the lab uses a synonym.
        display_name = accepted_to_canonical[biomarker]

        # Pick the most severe signal for this biomarker — one alert per
        # series; the structured signal field tells the user which rule
        # fired, no need to spam them with every WE rule. critical>warn,
        # then prefer beyond-limit rules over consecutive-direction rules.
        _RULE_PRIORITY = {
            "beyond_ucl": 0, "beyond_lcl": 0,
            "7_above_mean": 1, "7_below_mean": 1,
            "2_of_3_above_2sigma": 2, "2_of_3_below_2sigma": 2,
            "moving_range_jump": 3,
            "6_consecutive_rising": 4, "6_consecutive_falling": 4,
            "3_consecutive_rising": 5, "3_consecutive_falling": 5,
        }
        best = min(
            result.signals,
            key=lambda s: (
                0 if s["severity"] == "critical" else 1,
                _RULE_PRIORITY.get(s["rule"], 99),
            ),
        )

        last_point = result.points[-1]
        last_value = last_point.value
        last_date = last_point.date
        last_date_iso = (
            last_date.isoformat() if isinstance(last_date, date) else str(last_date)
        )
        last_date_ru = (
            last_date.strftime("%d.%m.%Y") if isinstance(last_date, date)
            else str(last_date)
        )

        direction = best.get("direction")
        rule = best["rule"]
        severity = best["severity"]
        trend_word = (
            "rising" if direction == "up"
            else "declining" if direction == "down"
            else "volatile"
        )
        rule_phrase = _rule_phrase(rule, direction)
        unit = result.unit or ""
        message = (
            f"{display_name}: {rule_phrase}, "
            f"последнее {last_value} {unit} ({last_date_ru})"
        ).strip()

        alerts.append({
            "kind": "trend",
            "biomarker": display_name,
            "trend": trend_word,
            "last_value": last_value,
            "last_date": last_date_iso,
            "rule": rule,
            "severity": severity,
            "message": message,
        })

    alerts.sort(
        key=lambda a: (0 if a["severity"] == "critical" else 1, a["biomarker"])
    )
    return alerts


def _rule_phrase(rule: str, direction: str | None) -> str:
    """Localized rule description for the alert message."""
    arrow_up = "вверх"
    arrow_down = "вниз"
    arrow = arrow_up if direction == "up" else arrow_down if direction == "down" else ""
    table = {
        "beyond_ucl": "точка выше верхней контрольной границы",
        "beyond_lcl": "точка ниже нижней контрольной границы",
        "7_above_mean": "7 точек подряд выше среднего — сдвиг вверх",
        "7_below_mean": "7 точек подряд ниже среднего — сдвиг вниз",
        "6_consecutive_rising": "6 подряд растущих точек",
        "6_consecutive_falling": "6 подряд падающих точек",
        "3_consecutive_rising": "3 подряд растущие точки",
        "3_consecutive_falling": "3 подряд падающие точки",
        "2_of_3_above_2sigma": "2 из 3 последних выше 2σ",
        "2_of_3_below_2sigma": "2 из 3 последних ниже 2σ",
        "moving_range_jump": f"резкое движение {arrow}".strip(),
    }
    return table.get(rule, rule)


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
