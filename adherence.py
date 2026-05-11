"""PDC (Proportion of Days Covered) adherence engine.

PDC is the industry standard adherence metric used by CMS, payers and
pharma PSP teams (vs MPR, which can exceed 100% on early refills and is
falling out of favor). For a given drug and period:

    PDC = covered_days / period_days

A *covered day* = at least one `taken` event in `medication_events_v1`
for that drug on that date. A drug becomes adherent when PDC ≥ 0.80
(the threshold ROCKET, MEDS, ISPOR all converge on).

This module computes PDC per drug per window (30/90/180d), classifies
patients into adherent / non-adherent buckets, and feeds /alerts when a
drug drops below threshold. The /alerts hook is what makes this
operationally useful — without it, the dashboard would be just a
retrospective report.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger("health-bot")

ADHERENCE_THRESHOLD = 0.80
PDC_WINDOWS_DAYS = (30, 90, 180)
NON_ADHERENT_REASONS = ("forgot", "na_unavailable", "na_cost", "na_ae", "skipped")

_DEFAULT_DAYS_SUPPLY = 1
_DOSE_INTERVAL_PATTERNS = (
    # Russian dosing frequency cues → days of coverage per dose
    (re.compile(r"\b(в\s*нед|/\s*нед|раз\s*в\s*нед|еженедел)", re.I), 7),
    (re.compile(r"\b(в\s*2\s*нед|раз\s*в\s*2\s*нед|каждые\s*2\s*нед)", re.I), 14),
    (re.compile(r"\b(в\s*мес|раз\s*в\s*мес|ежемесяч|/мес)", re.I), 30),
    (re.compile(r"\b(в\s*день|/\s*ден|ежеднев|раз\s*в\s*ден|2\s*раза\s*в\s*ден|3\s*раза\s*в\s*ден)", re.I), 1),
)


def dose_to_days_supply(dose: str) -> int:
    """Map a free-text dose like "5 мг/нед" to days-of-coverage per take.

    PDC formula needs to know how long one dose covers the patient, not
    just whether they took it today. For a weekly injection, one taken
    event counts as 7 covered days. Defaults to 1 (daily) if the regex
    finds no interval cue — safe under-counting, never inflates PDC.
    """
    if not dose:
        return _DEFAULT_DAYS_SUPPLY
    for pat, days in _DOSE_INTERVAL_PATTERNS:
        if pat.search(dose):
            return days
    return _DEFAULT_DAYS_SUPPLY


@dataclass
class DrugPDC:
    drug_name: str
    drug_inn: str
    window_days: int
    covered_days: int
    period_days: int
    pdc: float
    taken_events: int
    miss_events: int
    miss_reasons: dict[str, int]
    is_adherent: bool

    def __str__(self) -> str:
        flag = "✅" if self.is_adherent else "⚠️"
        return (f"{flag} {self.drug_name} — PDC {self.pdc*100:.0f}% "
                f"({self.covered_days}/{self.period_days}д, taken={self.taken_events}, "
                f"miss={self.miss_events})")


def record_medication_event(
    ch, owner_id: str, drug_name: str, drug_inn: str, dose: str,
    status: str, source: str = "manual", reminder_id: str | None = None,
    note: str = "", ts: datetime | None = None,
) -> str:
    """Append a single event. Caller passes status from the inline button
    callback (taken/skipped/forgot/na_*) or a manual entry. ts defaults
    to now() — only override for backfill / test fixtures.
    """
    import uuid
    event_id = str(uuid.uuid4())
    ch.insert("medication_events_v1", [[
        event_id, owner_id, ts or datetime.now(), drug_name, drug_inn,
        dose, status, source, reminder_id, note,
    ]], column_names=[
        "id", "owner_id", "ts", "drug_name", "drug_inn", "dose",
        "status", "source", "reminder_id", "note",
    ])
    return event_id


def compute_pdc(ch, owner_id: str, drug_name: str, window_days: int) -> DrugPDC:
    """Compute PDC for one drug in a window ending today.

    PDC convention here: each ``taken`` event gives ``days_supply`` days
    of coverage starting from the dose date. Coverage from successive
    doses unions (no double-counting), and is clipped to the window. So
    a weekly injection on day 0 covers days 0–6; a follow-up on day 7
    covers 7–13; etc.

    ``days_supply`` is inferred per-event from the dose string by
    ``dose_to_days_supply`` (e.g., "5 мг/нед" → 7, "7 мг/день" → 1).
    """
    period_start_d = date.today() - timedelta(days=window_days - 1)
    period_start_iso = period_start_d.isoformat()
    rows = ch.query(
        "SELECT toDate(ts) as d, status, dose, drug_inn FROM medication_events_v1 "
        "WHERE owner_id={o:String} AND drug_name={n:String} "
        "AND ts >= toDateTime({s:String})",
        parameters={"o": owner_id, "n": drug_name, "s": f"{period_start_iso} 00:00:00"},
    ).result_rows

    covered_dates: set[date] = set()
    taken = 0
    miss = 0
    miss_reasons: dict[str, int] = defaultdict(int)
    inn = ""
    period_end = date.today()
    for d, status, dose, this_inn in rows:
        if this_inn:
            inn = this_inn
        if status == "taken":
            taken += 1
            supply = dose_to_days_supply(dose or "")
            for k in range(supply):
                day = d + timedelta(days=k)
                if period_start_d <= day <= period_end:
                    covered_dates.add(day)
        elif status in NON_ADHERENT_REASONS:
            miss += 1
            miss_reasons[status] += 1

    covered = len(covered_dates)
    pdc = covered / window_days if window_days else 0.0
    return DrugPDC(
        drug_name=drug_name, drug_inn=inn, window_days=window_days,
        covered_days=covered, period_days=window_days, pdc=pdc,
        taken_events=taken, miss_events=miss,
        miss_reasons=dict(miss_reasons),
        is_adherent=pdc >= ADHERENCE_THRESHOLD,
    )


def list_drugs_for_owner(ch, owner_id: str, since_days: int = 180) -> list[str]:
    """Distinct drug_name values seen in events for the past ``since_days``.

    We anchor the window to recent events because a drug discontinued a
    year ago shouldn't show up in ``/adherence`` — it's not actionable.
    """
    cutoff = (date.today() - timedelta(days=since_days)).isoformat()
    rows = ch.query(
        "SELECT DISTINCT drug_name FROM medication_events_v1 "
        "WHERE owner_id={o:String} AND ts >= toDateTime({c:String}) "
        "AND length(drug_name) > 0 ORDER BY drug_name",
        parameters={"o": owner_id, "c": f"{cutoff} 00:00:00"},
    ).result_rows
    return [r[0] for r in rows]


def adherence_summary(ch, owner_id: str, window_days: int = 30) -> dict[str, Any]:
    """Build the dataset /adherence renders. Returns:

        {"window_days": 30,
         "drugs": [DrugPDC, ...],
         "below_threshold": [DrugPDC, ...]}
    """
    drugs = list_drugs_for_owner(ch, owner_id)
    pdcs = [compute_pdc(ch, owner_id, d, window_days) for d in drugs]
    return {
        "window_days": window_days,
        "drugs": pdcs,
        "below_threshold": [p for p in pdcs if not p.is_adherent],
    }


def adherence_alerts(ch, owner_id: str) -> list[str]:
    """One-line alert per drug that fell below the 0.80 cutoff in 30d.

    Called from /alerts so the user sees PDC issues alongside lab-fatigue
    and SPC alarms — single pane of glass for non-routine signals.
    """
    summary = adherence_summary(ch, owner_id, window_days=30)
    alerts = []
    for p in summary["below_threshold"]:
        # Suppress drugs with too-few events: PDC=0% on 1 take is noise
        if p.taken_events + p.miss_events < 5:
            continue
        reasons = ", ".join(
            f"{k}={v}" for k, v in sorted(p.miss_reasons.items(), key=lambda kv: -kv[1])
        )
        alerts.append(
            f"⚠️ Приверженность {p.drug_name}: PDC {p.pdc*100:.0f}% "
            f"за 30д (целевой ≥80%). Причины: {reasons or '—'}"
        )
    return alerts


__all__ = [
    "ADHERENCE_THRESHOLD", "PDC_WINDOWS_DAYS",
    "DrugPDC", "record_medication_event",
    "compute_pdc", "list_drugs_for_owner",
    "adherence_summary", "adherence_alerts",
]
