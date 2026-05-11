"""Current protocol inference from recent diagnostician digests.

The user logs his TRT/GH/peptide cycle through chat — the daily digest LLM
extracts those mentions into ``daily_digest.new_info``. This module asks
Opus to roll up the last ``PROTOCOL_LOOKBACK_DAYS`` of digests into a
*current* stack (not a history) and then computes follow-up lab dates from
a hardcoded mapping that is cross-checked against ``protocols.yaml``.

The single heavy LLM call (~30s on Opus) lives in ``get_current_protocol``;
``protocols.yaml`` is reference data only — the source of truth is what the
user actually said in chat, not what the catalogue lists.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from utils import parse_json_response

log = logging.getLogger("health-bot")

PROJECT_DIR = Path(__file__).resolve().parent
PROTOCOLS_YAML = PROJECT_DIR / "protocols.yaml"

# How far back to look for protocol mentions. 30d (the original window) was
# too narrow: a TRT/GH cycle started >30d ago but still active gets dropped,
# and /alerts then can't anchor due-dates to it. 90d covers typical AAS
# cycle length while still letting "закончил X" mentions push the substance
# out of current_stack via the prompt rules.
PROTOCOL_LOOKBACK_DAYS = 90

# Follow-up rules: which substance class triggers which lab, after how many
# weeks. The LLM gets these as instructions in the prompt; we recompute
# due_at deterministically in Python (P-COMP-01: no LLM arithmetic for
# dates that drive UX warnings).
FOLLOWUP_RULES = {
    # Any AAS / oral hepatotoxic compound → ALT recheck at +6 weeks
    "aas": {"test": "АЛТ", "weeks": 6, "rationale": "контроль печени на ААС"},
    # Growth hormone / GHS → IGF-1 at +8 weeks
    "gh": {"test": "ИФР-1", "weeks": 8, "rationale": "ответ на ГР/секретагог"},
    # Estrogenic AAS (testosterone, нандролон, etc.) → E2 at +4 weeks
    "estrogenic": {"test": "Эстрадиол", "weeks": 4,
                   "rationale": "ароматизация ААС в эстрадиол"},
}


def _load_protocols_index() -> dict[str, dict]:
    """Light index of compound name → class for cross-validation only.

    Returns an empty dict if YAML missing/broken — protocol inference must
    not crash on a config typo.
    """
    if not PROTOCOLS_YAML.exists():
        return {}
    try:
        data = yaml.safe_load(PROTOCOLS_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("protocols.yaml parse failed: %s", exc)
        return {}
    return data.get("compounds", {}) or {}


def _call_claude(prompt: str, timeout: int = 240) -> str:
    """Single retry on 529/transient failure — see CLAUDE.md / 30s wait."""
    import time as _time

    for attempt in (1, 2):
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-opus-4-7", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        detail = (result.stderr or result.stdout or "").strip()[:300]
        log.warning("protocol claude attempt %d failed: %s", attempt, detail)
        if attempt == 1:
            _time.sleep(30)
    raise RuntimeError(f"claude failed after retry: {detail}")


_PROMPT_TEMPLATE = """Ты — клинический фармаколог. Твоя задача — по сериям дневных дайджестов (поле new_info) восстановить ТЕКУЩИЙ стек препаратов пользователя (АС, пептиды, SARM, ГР, AI/SERM) и дать рекомендации по follow-up анализам.

ПРАВИЛА:
1. Источник истины — дайджесты ниже. Каталог `protocols.yaml` НЕ источник того, что принимает пользователь — только справочник для классификации.
2. Дедуплицируй: если препарат упоминался несколько раз, оставь ОДНУ запись с самой свежей датой как started_at и самой свежей упомянутой дозой.
3. Если в дайджесте сказано «закончил X», «прекратил X», «слетел с курса X», «снял X» — НЕ включай X в current_stack.
4. Если в дайджесте «начал X», «поставил укол X», «сегодня первый укол X» — это start, started_at = дата дайджеста.
5. Если препарат упоминается без явного start — started_at = дата самого раннего упоминания в окне.
6. source_digest_date = дата того дайджеста, откуда взято последнее упоминание дозы.
7. Сохраняй РУССКИЕ названия как у пользователя (ТЗТ, ГР, тест-Э, BPC-157, ипаморелин, анастрозол).
8. Сохраняй ЕДИНИЦЫ как сказал пользователь: ТЗТ — мг, ГР — МЕ, BPC — мкг.
9. Для каждого вещества в стеке заполни поле class из множества: "aas" (любой ААС/SARM/орал), "gh" (гормон роста или GHS типа MK-677, ипаморелин, CJC-1295), "estrogenic_aas" (тестостерон/нандролон/любой ароматизирующийся АС), "ai" (анастрозол, экземестан), "serm" (тамоксифен, кломифен), "peptide_healing" (BPC-157, TB-500), "other".
10. Один препарат может иметь несколько классов: например тестостерон энантат → ["aas","estrogenic_aas"]. ГР (соматропин) → ["gh"].

FOLLOW-UP АНАЛИЗЫ — заполни due_dates по этим правилам:
- Если в стеке есть вещество класса "aas" → добавь {"test":"АЛТ","weeks_from_start":6,"trigger_substance":<имя>,"rationale":"контроль печени на ААС"}.
- Если в стеке есть "gh" → {"test":"ИФР-1","weeks_from_start":8,"trigger_substance":<имя>,"rationale":"ответ на ГР"}.
- Если в стеке есть "estrogenic_aas" → {"test":"Эстрадиол","weeks_from_start":4,"trigger_substance":<имя>,"rationale":"контроль ароматизации"}.

ВЫХОД — СТРОГО один JSON-объект, никакого markdown:
{
  "current_stack": [
    {"substance": "ТЗТ", "dose": "250 мг/нед", "started_at": "2026-04-15", "source_digest_date": "2026-05-07", "classes": ["aas","estrogenic_aas"]}
  ],
  "due_dates": [
    {"test": "АЛТ", "weeks_from_start": 6, "trigger_substance": "ТЗТ", "rationale": "..."}
  ],
  "raw_extractions": ["цитата фрагмента из дайджеста на которой основан вывод"]
}

Если стек пуст — current_stack=[], due_dates=[], raw_extractions=[].

=== ДНЕВНЫЕ ДАЙДЖЕСТЫ (последние __LOOKBACK__ дней, по убыванию даты) ===
__DIGESTS__

=== СПРАВОЧНИК ВЕЩЕСТВ (имена для классификации, дозы НЕ читать как факт) ===
__CATALOGUE__
"""


def _format_digests(rows: list[tuple]) -> str:
    """Build the digest text block: ``YYYY-MM-DD: <new_info>`` per row."""
    lines = []
    for d, ni in rows:
        ni_clean = (ni or "").strip()
        if not ni_clean:
            continue
        lines.append(f"{d}: {ni_clean}")
    return "\n".join(lines) if lines else "(нет упоминаний препаратов в последних дайджестах)"


def _format_catalogue(compounds: dict) -> str:
    """Compact one-line-per-compound list: name + class. Keeps prompt small."""
    if not compounds:
        return "(каталог пуст)"
    lines = []
    for key, c in compounds.items():
        name = c.get("name", key)
        cls = c.get("class", "")
        lines.append(f"  {name} [{cls}]")
    return "\n".join(lines)


def _last_test_date(ch: Any, owner_id: str, test_name: str) -> date | None:
    """Most recent collected_at for a biomarker — fuzzy match on name.

    Used to suppress due-date warnings when the user already has a fresh
    result (so the bot doesn't nag him to retake АЛТ a week after he just
    did). Search is ILIKE %name% to match Russian variants.
    """
    try:
        result = ch.query(
            "SELECT max(collected_at) FROM lab_results "
            "WHERE owner_id = {o:String} AND biomarker ILIKE {n:String}",
            parameters={"o": owner_id, "n": f"%{test_name}%"},
        )
        if not result.result_rows:
            return None
        v = result.result_rows[0][0]
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
    except Exception as exc:
        log.warning("_last_test_date failed for %s: %s", test_name, exc)
    return None


def _parse_iso(d: str | None) -> date | None:
    if not d:
        return None
    try:
        return date.fromisoformat(d)
    except (TypeError, ValueError):
        return None


def get_current_protocol(ch: Any, owner_id: str) -> dict:
    """Infer current stack + due-dates for follow-up labs.

    Returns a dict shaped as documented in tg_listener `/protocol` handler:
        {
          "current_stack": [...],
          "due_dates": [{"test","due_at","rationale", ...}],
          "raw_extractions": [...]
        }

    Empty dict shape is returned when there are no digests with new_info.
    """
    cutoff = date.today() - timedelta(days=PROTOCOL_LOOKBACK_DAYS)
    rows = ch.query(
        "SELECT date, new_info FROM daily_digest "
        "WHERE owner_id = {o:String} AND date >= {c:Date} "
        "ORDER BY date DESC",
        parameters={"o": owner_id, "c": cutoff},
    ).result_rows

    has_content = any((r[1] or "").strip() for r in rows)
    if not has_content:
        return {"current_stack": [], "due_dates": [], "raw_extractions": []}

    digests_block = _format_digests(rows)
    catalogue_block = _format_catalogue(_load_protocols_index())

    prompt = (
        _PROMPT_TEMPLATE
        .replace("__DIGESTS__", digests_block)
        .replace("__CATALOGUE__", catalogue_block)
        .replace("__LOOKBACK__", str(PROTOCOL_LOOKBACK_DAYS))
    )

    raw = _call_claude(prompt)
    parsed = parse_json_response(raw)
    if not parsed:
        log.error("get_current_protocol: no JSON in response (first 300): %s", raw[:300])
        return {"current_stack": [], "due_dates": [], "raw_extractions": [],
                "error": "LLM did not return JSON"}

    stack: list[dict] = parsed.get("current_stack") or []
    raw_due: list[dict] = parsed.get("due_dates") or []
    raw_ex: list[str] = parsed.get("raw_extractions") or []

    # Compute due_at deterministically — never trust the LLM with dates.
    today = date.today()
    enriched_due: list[dict] = []
    # Map substance → started_at for date math
    started_map: dict[str, date | None] = {
        s.get("substance", ""): _parse_iso(s.get("started_at")) for s in stack
    }

    for d in raw_due:
        test = d.get("test", "").strip()
        weeks = d.get("weeks_from_start")
        trigger = d.get("trigger_substance", "").strip()
        rationale = d.get("rationale", "").strip()
        if not test or not isinstance(weeks, (int, float)):
            continue
        start = started_map.get(trigger)
        if start is None:
            # No anchor date — skip rather than hallucinate one
            continue
        due_at = start + timedelta(weeks=int(weeks))
        # Suppress if user already has a fresh result AFTER the trigger started.
        last = _last_test_date(ch, owner_id, test)
        if last is not None and last >= start:
            log.info("Suppressed due_date for %s — last sample %s ≥ start %s",
                     test, last, start)
            continue
        enriched_due.append({
            "test": test,
            "due_at": due_at.isoformat(),
            "trigger_substance": trigger,
            "rationale": (
                f"{rationale} ({weeks} нед. после старта {trigger})"
                if rationale else f"{weeks} нед. после старта {trigger}"
            ),
            "overdue": due_at < today,
        })

    # Sort: overdue first, then by due_at ascending
    enriched_due.sort(key=lambda e: (not e["overdue"], e["due_at"]))

    return {
        "current_stack": stack,
        "due_dates": enriched_due,
        "raw_extractions": raw_ex,
    }


__all__ = ["get_current_protocol", "FOLLOWUP_RULES"]
