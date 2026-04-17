"""
Workout data detector, LLM parser, and formatter.

Handles free-text workout logs pasted from Apple Notes or similar apps.
Uses an adaptive glossary (workout_glossary.yaml) for abbreviation expansion.
Unknown abbreviations are tracked and can be added to the glossary.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

log = logging.getLogger("health-bot")

PROJECT_DIR = Path(__file__).resolve().parent
GLOSSARY_PATH = PROJECT_DIR / "workout_glossary.yaml"
MSK_TZ = timezone(timedelta(hours=3))

# ─────────────────────────────────────────────────────────────────────────────
# Glossary loader (cached, auto-reload on file change)
# ─────────────────────────────────────────────────────────────────────────────
_glossary_cache: dict | None = None
_glossary_mtime: float = 0


def load_glossary() -> dict:
    """Load workout_glossary.yaml, cache until file changes."""
    global _glossary_cache, _glossary_mtime
    if not GLOSSARY_PATH.exists():
        return {}
    mtime = GLOSSARY_PATH.stat().st_mtime
    if _glossary_cache is not None and mtime == _glossary_mtime:
        return _glossary_cache
    _glossary_cache = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8")) or {}
    _glossary_mtime = mtime
    log.info("Loaded workout glossary (%d sections)", len(_glossary_cache))
    return _glossary_cache


def glossary_as_text() -> str:
    """Format glossary for LLM prompt context."""
    g = load_glossary()
    if not g:
        return "(no glossary)"
    lines = []
    for section, items in g.items():
        if not isinstance(items, dict):
            continue
        lines.append(f"\n## {section}")
        for abbr, info in items.items():
            if isinstance(info, dict):
                full = info.get("full", info.get("en", ""))
                lines.append(f"  {abbr} = {full}")
            elif isinstance(info, str):
                lines.append(f"  {abbr} = {info}")
    return "\n".join(lines)


def add_glossary_term(section: str, abbr: str, full: str, en: str = "") -> None:
    """Add a new term to the glossary YAML file."""
    g = load_glossary()
    if section not in g:
        g[section] = {}
    g[section][abbr] = {"full": full, "en": en} if en else full
    GLOSSARY_PATH.write_text(
        yaml.safe_dump(g, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    global _glossary_cache, _glossary_mtime
    _glossary_cache = g
    _glossary_mtime = GLOSSARY_PATH.stat().st_mtime
    log.info("Added glossary term: %s/%s = %s", section, abbr, full)


# ─────────────────────────────────────────────────────────────────────────────
# Workout text detector (pure Python, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

# Muscle group keywords
_MUSCLE_RE = re.compile(
    r'(?:СПИНА|ГРУДЬ|НОГИ|ПЛЕЧИ|БИЦЕПС|ТРИЦЕПС|ПРЕСС|ПРЕДПЛЕЧЬ|ИКРЫ|'
    r'ЯГОДИЦ|ДЕЛЬТ|ТРАПЕЦИ|ШИРОЧАЙШ|КВАДРИЦЕПС|БЕДРО|БЕДРА)',
    re.IGNORECASE,
)

# Workout structure markers
_WORKOUT_STRUCTURE_RE = re.compile(
    r'(?:РАЗМИНКА|ЗАМИНКА|РАСТЯЖКА|КАРДИО|ЦИКЛ|ПОДХОД|СУПЕРСЕТ|'
    r'ПОДТЯГИВАН|ОТЖИМАН|ПРИСЕД|ТЯГА|ЖИМ|СГИБАН|РАЗГИБАН|РАЗВЕДЕН|'
    r'СКРУЧИВ|ПЛАНКА|ВЫПАД|МЕРТВЫЙ ЖУК|ЛОДОЧКА|СКОРПИОН|ХОЛЛОУ|'
    r'ПОДЪЕМ|ПОДЪЁМ|НАКЛОН)',
    re.IGNORECASE,
)

# Effort level markers specific to workout logs
_EFFORT_RE = re.compile(
    r'(?:\bЛГ\b|\bСР\b|\bТЖ\b|\bСМСТ\b|\bотказ\b)',
    re.IGNORECASE,
)

# Set/rep notation patterns
_SETREP_RE = re.compile(
    r'(?:'
    r'[хx]\s*\d+'           # х3, x4
    r'|\d+\s*[хx]\s*\d+'   # 10х3
    r'|\+\d+\s*кг'         # +25кг
    r'|/\d+\s*(?:[хx]|$|\s)'  # /10 х3
    r'|\d+кг/\d+'          # 80кг/10
    r'|\d+\s*(?:НЕД|нед)'  # 1 НЕД
    r'|\d+\s*(?:СЕТ|сет|ПОДХОД|подход)'
    r'|\d+С\s*\|\s*\d+П'   # 2С|3П
    r')',
    re.IGNORECASE,
)

# Grip/equipment abbreviations
_GRIP_RE = re.compile(
    r'(?:\bШХ\b|\bНХ\b|\bУХ\b|\bОХ\b|\bПХ\b|\bРТ\b|\bГТ\b|\bВТ\b|\bПНБ\b|\bГА\b|\bШТ\b'
    r'|\bСШНС\b|\bРГС\b|\bСГЛ\b|\bСГС\b|\bРГН\b|\bСМВ\b|\bЖЛ\b|\bЖС\b|\bФП\b)',
    re.IGNORECASE,
)

# Body weight pattern in workout context
_BW_RE = re.compile(r'ВЕС\s+\d+[.,]\d+\s*кг', re.IGNORECASE)

# Week headers
_WEEK_RE = re.compile(r'\d\s*НЕД', re.IGNORECASE)


def workout_score(text: str) -> int:
    """Calculate workout likelihood score. Higher = more likely workout data."""
    score = 0
    score += len(_MUSCLE_RE.findall(text)) * 4
    score += len(_WORKOUT_STRUCTURE_RE.findall(text)) * 3
    score += len(_EFFORT_RE.findall(text)) * 3
    score += len(_SETREP_RE.findall(text)) * 2
    score += len(_GRIP_RE.findall(text)) * 2
    score += len(_BW_RE.findall(text)) * 3
    score += len(_WEEK_RE.findall(text)) * 2
    return score


def looks_like_workout_data(text: str) -> bool:
    """Heuristic: is this pasted workout/training log data?"""
    if len(text) < 80:
        return False
    return workout_score(text) >= 8


# ─────────────────────────────────────────────────────────────────────────────
# LLM parser — text → structured JSON
# ─────────────────────────────────────────────────────────────────────────────

_PARSE_PROMPT_TEMPLATE = """Ты — парсер тренировочных данных. Пользователь скопировал текст тренировки из Apple Notes.
ВАЖНО: при копировании из Apple Notes ТЕРЯЕТСЯ табличное форматирование — колонки становятся потоком текста через табы/пробелы/переводы строк. Ты должен ВОССТАНОВИТЬ структуру по смыслу.

=== ГЛОССАРИЙ СОКРАЩЕНИЙ ===
{glossary}

=== ПРАВИЛА ПАРСИНГА ===
1. Определи день тренировки и группы мышц из заголовка (напр. "ВТ - СПИНА, БИЦЕПС, ПРЕСС")
2. Определи номер цикла и количество недель (напр. "ЦИКЛ №2 — 6 НЕДЕЛЬ")
3. Извлеки вес тела (напр. "ВЕС 91,30кг" с датой)
4. Раздели на секции: РАЗМИНКА, основные упражнения, ЗАМИНКА/ПРЕСС
5. Для каждого упражнения извлеки:
   - название (расшифруй сокращения)
   - хват/вариант
   - данные по неделям: вес, повторения, модификатор (Ф/Ч/Ж/К/0), оценка (ЛГ/СР/ТЖ/СМСТ/отказ)
6. Формат записи подходов: "Ф/10 х3" = фиксатор, 10 повторов, 3 подхода
7. "+25кг/10" = +25 кг дополнительного веса на 10 повторов
8. "Ч+Ж/8" = начал чисто, дожал жимом, 8 повторов
9. Если встречаешь НЕЗНАКОМОЕ сокращение — добавь в unknown_terms

Верни СТРОГО JSON:
{{
  "training_day": "ВТ - СПИНА, БИЦЕПС, ПРЕСС",
  "muscle_groups": ["back", "biceps", "abs"],
  "cycle_number": 2,
  "cycle_weeks": 6,
  "cycle_start_date": "2026-03-30",
  "body_weights": [
    {{"date": "2026-03-31", "weight_kg": 91.3}},
    {{"date": "2026-04-07", "weight_kg": 90.4}}
  ],
  "warmup": [
    {{"exercise": "растяжка сгибателей в выпаде", "params": "30 сек"}},
    {{"exercise": "наклоны в выпаде", "params": "вперёд/в сторону 10/10"}}
  ],
  "exercises": [
    {{
      "number": 1,
      "name": "Подтягивания на перекладине",
      "variant": "нейтральный широкий хват",
      "type": "single",
      "weeks": [
        {{
          "week": 1,
          "sets": [
            {{"modifier": "Ф", "reps": 10, "sets_count": 3, "effort": "ЛГ"}},
            {{"modifier": "Ч", "reps": 10, "sets_count": 1, "effort": "ТЖ"}}
          ]
        }},
        {{
          "week": 2,
          "sets": [
            {{"modifier": "Ф", "reps": 10, "sets_count": 1, "effort": "ЛГ"}},
            {{"modifier": "Ч", "reps": 10, "sets_count": 1, "effort": "ТЖ"}},
            {{"modifier": "Ч+Ж", "reps": 8, "sets_count": 1, "effort": "ТЖ"}},
            {{"modifier": "Ч+К", "reps": 10, "sets_count": 1, "effort": "отказ"}}
          ]
        }}
      ]
    }},
    {{
      "number": 2,
      "name": "Тяга Т-грифа",
      "variant": "свободно, узкий хват",
      "type": "single",
      "weeks": [
        {{
          "week": 1,
          "sets": [
            {{"weight_kg": 25, "reps": 10, "sets_count": 3, "effort": "СР"}},
            {{"weight_kg": 30, "reps": 10, "sets_count": 1, "effort": "ТЖ"}}
          ]
        }}
      ]
    }},
    {{
      "number": 3,
      "name": "Суперсет: Горизонтальная тяга стоя ШХ (рычажная) + Подъём на бицепс сидя",
      "variant": "2 суперсета | 3 подхода",
      "type": "superset",
      "exercises_in_set": [
        "Горизонтальная тяга стоя широким хватом (рычажная тяга)",
        "Подъём на бицепс сидя (4 ступени)"
      ],
      "weeks": [
        {{
          "week": 1,
          "sets": [
            {{"weights": ["60кг", "8кг"], "reps": 10, "effort": "СР"}},
            {{"weights": ["70кг", "9кг"], "reps": 10, "effort": ""}},
            {{"weights": ["80кг", "10кг"], "reps": 10, "effort": "отказ"}}
          ]
        }}
      ]
    }}
  ],
  "cooldown": [
    {{
      "exercise": "ПРЕСС",
      "details": [
        {{"name": "подносы ног лёжа", "reps": 10}},
        {{"name": "книжка-пресс с упором", "reps": 20}}
      ],
      "weeks": [
        {{"week": 1, "sets": "х3", "notes": "2 круг отказ, сделал 7 подъёмов"}},
        {{"week": 2, "sets": "3 круга", "notes": "вместо 2 и 4 - 15 ситап, 30с планка"}}
      ]
    }}
  ],
  "unknown_terms": [],
  "notes": "Любые дополнительные заметки из текста"
}}

=== ТЕКСТ ТРЕНИРОВКИ ===
{text}"""


def parse_workout_text(text: str) -> dict | None:
    """Parse workout text via LLM with glossary context. Returns parsed dict or None."""
    glossary_text = glossary_as_text()
    prompt = _PARSE_PROMPT_TEMPLATE.format(glossary=glossary_text, text=text)

    # Haiku first — fast enough for workout parsing, Sonnet as fallback
    for model in ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]:
        timeout = 120 if "haiku" in model else 180
        try:
            log.info("Parsing workout with model=%s (%d chars)", model, len(text))
            result = subprocess.run(
                ["claude", "-p", "--model", model, prompt],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                log.warning("Workout parse failed (rc=%d) model=%s", result.returncode, model)
                continue

            # Extract JSON from response
            output = result.stdout.strip()
            m = re.search(r"\{.*\}", output, re.DOTALL)
            if not m:
                log.warning("No JSON found in workout parse response")
                continue

            parsed = json.loads(m.group(0))
            log.info("Workout parsed: %d exercises, %d unknown terms",
                     len(parsed.get("exercises", [])),
                     len(parsed.get("unknown_terms", [])))
            return parsed

        except json.JSONDecodeError as exc:
            log.warning("JSON decode error in workout parse: %s", exc)
        except subprocess.TimeoutExpired:
            log.warning("Workout parse timeout model=%s", model)
        except Exception as exc:
            log.warning("Workout parse error model=%s: %s", model, exc)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Response formatter
# ─────────────────────────────────────────────────────────────────────────────

# ─── RPE mapping ───
_RPE_MAP = {
    "ЛГ": 6, "СР": 7.5, "ТЖ": 9, "СМСТ": 8, "отказ": 10,
}


def _extract_weight(s: dict) -> float:
    """Extract numeric weight from a set dict."""
    w = s.get("weight_kg", 0) or 0
    if isinstance(w, (int, float)):
        return float(w)
    if isinstance(w, str):
        m = re.search(r"[\d.]+", w.replace(",", "."))
        return float(m.group()) if m else 0
    return 0


def _extract_reps(s: dict) -> int:
    r = s.get("reps", 0) or 0
    return int(r) if isinstance(r, (int, float)) else 0


def _extract_sets_count(s: dict) -> int:
    c = s.get("sets_count", 1) or 1
    return max(1, int(c))


def _effort_to_rpe(effort) -> float | None:
    """Convert effort string(s) to average RPE."""
    if not effort:
        return None
    if isinstance(effort, list):
        vals = [_RPE_MAP.get(e.strip(), 0) for e in effort if isinstance(e, str)]
        vals = [v for v in vals if v > 0]
        return sum(vals) / len(vals) if vals else None
    if isinstance(effort, str):
        return _RPE_MAP.get(effort.strip())
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Analytics engine (all math in Python, zero LLM arithmetic)
# ─────────────────────────────────────────────────────────────────────────────

def compute_exercise_analytics(exercises: list[dict]) -> list[dict]:
    """Compute per-exercise analytics across weeks.

    Returns list of dicts with: name, weeks[], volume_trend, weight_trend,
    effort_trend, max_weight, total_volume, verdict.
    """
    results = []
    for ex in exercises:
        name = ex.get("name", "?")
        weeks_data = ex.get("weeks", [])
        if not weeks_data:
            continue

        week_stats = []
        for w in weeks_data:
            week_num = w.get("week", 0)
            sets = w.get("sets", [])
            total_volume = 0  # kg × reps
            total_reps = 0
            total_sets = 0
            max_w = 0.0
            rpe_values = []

            for s in sets:
                weight = _extract_weight(s)
                reps = _extract_reps(s)
                count = _extract_sets_count(s)
                total_sets += count
                total_reps += reps * count
                if weight > 0:
                    total_volume += weight * reps * count
                    max_w = max(max_w, weight)
                rpe = _effort_to_rpe(s.get("effort"))
                if rpe is not None:
                    rpe_values.append(rpe)

            week_stats.append({
                "week": week_num,
                "total_volume_kg": round(total_volume),
                "total_reps": total_reps,
                "total_sets": total_sets,
                "max_weight_kg": max_w,
                "avg_rpe": round(sum(rpe_values) / len(rpe_values), 1) if rpe_values else None,
            })

        # Trends (first vs last week)
        analysis = {"name": name, "weeks": week_stats}

        if len(week_stats) >= 2:
            first, last = week_stats[0], week_stats[-1]

            # Weight progression
            if first["max_weight_kg"] > 0 and last["max_weight_kg"] > 0:
                diff = last["max_weight_kg"] - first["max_weight_kg"]
                pct = (diff / first["max_weight_kg"]) * 100
                analysis["weight_change_kg"] = diff
                analysis["weight_change_pct"] = round(pct, 1)

            # Volume progression
            if first["total_volume_kg"] > 0 and last["total_volume_kg"] > 0:
                diff = last["total_volume_kg"] - first["total_volume_kg"]
                pct = (diff / first["total_volume_kg"]) * 100
                analysis["volume_change_kg"] = diff
                analysis["volume_change_pct"] = round(pct, 1)

            # RPE trend
            if first.get("avg_rpe") and last.get("avg_rpe"):
                analysis["rpe_change"] = round(last["avg_rpe"] - first["avg_rpe"], 1)

            # Bodyweight exercise? (no external weights)
            is_bw = first["max_weight_kg"] == 0 and last["max_weight_kg"] == 0

            # Rep progression for bodyweight exercises
            if is_bw:
                rep_diff = last["total_reps"] - first["total_reps"]
                rep_pct = (rep_diff / first["total_reps"] * 100) if first["total_reps"] > 0 else 0
                analysis["rep_change"] = rep_diff
                analysis["rep_change_pct"] = round(rep_pct, 1)
                # For BW: also track modifier progression (Ф→Ч→0 = improvement)
                analysis["is_bodyweight"] = True

            # Verdict
            wc = analysis.get("weight_change_kg", 0)
            vc = analysis.get("volume_change_pct", 0)
            rpe_c = analysis.get("rpe_change", 0)
            rc = analysis.get("rep_change", 0)

            if is_bw:
                # Bodyweight: judge by reps and RPE
                if rc > 0 and (rpe_c is None or rpe_c <= 0):
                    analysis["verdict"] = "progression"
                elif rc > 0 and rpe_c and rpe_c > 0.5:
                    analysis["verdict"] = "grinding"
                elif rc <= 0 and (rpe_c is None or rpe_c >= 0):
                    analysis["verdict"] = "plateau"
                else:
                    analysis["verdict"] = "stable"
            elif wc > 0 and vc > 0:
                analysis["verdict"] = "progression"
            elif wc > 0 and rpe_c and rpe_c > 0.5:
                analysis["verdict"] = "grinding"
            elif wc == 0 and vc <= 0:
                analysis["verdict"] = "plateau"
            elif wc < 0:
                analysis["verdict"] = "regression"
            else:
                analysis["verdict"] = "stable"

        results.append(analysis)
    return results


def compute_body_weight_trend(body_weights: list[dict]) -> dict | None:
    """Analyze body weight changes across the cycle."""
    valid = [bw for bw in body_weights if bw.get("weight_kg")]
    if len(valid) < 2:
        return None
    first = valid[0]["weight_kg"]
    last = valid[-1]["weight_kg"]
    diff = last - first
    return {
        "first_kg": first,
        "last_kg": last,
        "change_kg": round(diff, 2),
        "points": len(valid),
        "trend": "gaining" if diff > 0.3 else "losing" if diff < -0.3 else "stable",
    }


def compute_total_session_volume(exercises: list[dict]) -> dict:
    """Compute total session volume per week across all exercises."""
    week_totals: dict[int, dict] = {}
    for ex in exercises:
        for w in ex.get("weeks", []):
            wn = w.get("week", 0)
            if wn not in week_totals:
                week_totals[wn] = {"volume_kg": 0, "reps": 0, "sets": 0}
            for s in w.get("sets", []):
                weight = _extract_weight(s)
                reps = _extract_reps(s)
                count = _extract_sets_count(s)
                week_totals[wn]["sets"] += count
                week_totals[wn]["reps"] += reps * count
                if weight > 0:
                    week_totals[wn]["volume_kg"] += round(weight * reps * count)
    return dict(sorted(week_totals.items()))


# ─────────────────────────────────────────────────────────────────────────────
# Response formatter — analytics-first
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_LABELS = {
    "progression": "📈 Прогресс",
    "grinding": "⚠️ Через силу (вес растёт, RPE под потолком)",
    "plateau": "📊 Плато",
    "regression": "📉 Регресс",
    "stable": "➡️ Стабильно",
}


def format_workout_response(parsed: dict, raw_text: str,
                            prev_cycle_data: dict | None = None) -> str:
    """Format parsed workout with full analytics."""
    lines = []
    exercises = parsed.get("exercises", [])

    # ── Header ──
    day = parsed.get("training_day", "")
    if day:
        lines.append(f"<b>{day}</b>")
    cycle = parsed.get("cycle_number", 0)
    weeks = parsed.get("cycle_weeks", 0)
    if cycle:
        lines.append(f"Цикл #{cycle}" + (f" ({weeks} нед.)" if weeks else ""))

    # ── Body weight trend ──
    bw_trend = compute_body_weight_trend(parsed.get("body_weights", []))
    if bw_trend:
        icon = "📈" if bw_trend["trend"] == "gaining" else "📉" if bw_trend["trend"] == "losing" else "➡️"
        lines.append(
            f"\n{icon} <b>Вес:</b> {bw_trend['first_kg']} → {bw_trend['last_kg']} кг "
            f"(<b>{bw_trend['change_kg']:+.1f} кг</b> за {bw_trend['points']} замера)"
        )

    # ── Session volume per week ──
    session_vol = compute_total_session_volume(exercises)
    if len(session_vol) >= 2:
        first_wk = list(session_vol.values())[0]
        last_wk = list(session_vol.values())[-1]
        vol_diff = last_wk["volume_kg"] - first_wk["volume_kg"]
        lines.append(
            f"\n<b>Объём тренировки:</b>"
        )
        for wn, wv in session_vol.items():
            lines.append(f"  Нед.{wn}: {wv['volume_kg']:,} кг × {wv['sets']} подходов × {wv['reps']} повторов")
        if first_wk["volume_kg"] > 0:
            vol_pct = (vol_diff / first_wk["volume_kg"]) * 100
            icon = "📈" if vol_diff > 0 else "📉" if vol_diff < 0 else "➡️"
            lines.append(f"  {icon} Динамика: <b>{vol_diff:+,} кг ({vol_pct:+.0f}%)</b>")

    # ── Per-exercise analytics ──
    analytics = compute_exercise_analytics(exercises)
    if analytics:
        lines.append(f"\n<b>Анализ по упражнениям:</b>")
        for a in analytics:
            name = a["name"]
            ws = a.get("weeks", [])
            verdict = _VERDICT_LABELS.get(a.get("verdict", ""), "")

            line = f"\n<b>{name}</b>"
            if verdict:
                line += f" — {verdict}"
            lines.append(line)

            # Weight progression (or rep progression for bodyweight)
            wc = a.get("weight_change_kg")
            if wc is not None and wc != 0:
                lines.append(f"  Вес: {ws[0]['max_weight_kg']}→{ws[-1]['max_weight_kg']} кг (<b>{wc:+.0f} кг</b>)")
            elif a.get("is_bodyweight") and a.get("rep_change") is not None:
                rc = a["rep_change"]
                lines.append(f"  Повторы: {ws[0]['total_reps']}→{ws[-1]['total_reps']} (<b>{rc:+d} повторов</b>)")

            # Volume
            vc = a.get("volume_change_pct")
            if vc is not None and vc != 0:
                lines.append(f"  Объём: {ws[0]['total_volume_kg']:,}→{ws[-1]['total_volume_kg']:,} кг (<b>{vc:+.0f}%</b>)")

            # RPE
            rpe_c = a.get("rpe_change")
            if rpe_c is not None and abs(rpe_c) >= 0.3:
                lines.append(f"  Усилие: RPE {ws[0].get('avg_rpe','-')}→{ws[-1].get('avg_rpe','-')} (<b>{rpe_c:+.1f}</b>)")

            # Single week — just show stats
            if len(ws) == 1:
                w = ws[0]
                parts = []
                if w["max_weight_kg"]:
                    parts.append(f"макс {w['max_weight_kg']} кг")
                parts.append(f"{w['total_sets']} подходов")
                parts.append(f"{w['total_reps']} повторов")
                if w.get("avg_rpe"):
                    parts.append(f"RPE {w['avg_rpe']}")
                lines.append(f"  {' | '.join(parts)}")

    # ── Cross-cycle comparison ──
    if prev_cycle_data:
        lines.append(f"\n<b>Сравнение с циклом #{prev_cycle_data.get('cycle_number', '?')}:</b>")
        # This will be filled when we have historical data

    # ── Recommendations based on verdicts ──
    verdicts = [a.get("verdict") for a in analytics if a.get("verdict")]
    if verdicts:
        lines.append(f"\n<b>Рекомендации:</b>")
        plateau_count = verdicts.count("plateau")
        regression_count = verdicts.count("regression")
        grinding_count = verdicts.count("grinding")
        progression_count = verdicts.count("progression")

        if progression_count == len(verdicts):
            lines.append("  Отличная прогрессия по всем упражнениям. Продолжай в том же темпе.")
        else:
            if plateau_count:
                lines.append(f"  Плато ({plateau_count}) — смени схему нагрузки, добавь вариации или дропсеты.")
            if regression_count:
                lines.append(f"  Регресс ({regression_count}) — проверь восстановление, сон, питание. Возможно нужен deload.")
            if grinding_count:
                lines.append(f"  На пределе ({grinding_count}) — RPE высокий, риск перетренировки. Рассмотри deload-неделю.")
            if progression_count:
                lines.append(f"  Прогресс ({progression_count}) — продолжай наращивать.")

    # ── Unknown terms ──
    unknown = parsed.get("unknown_terms", [])
    if unknown:
        lines.append(f"\n<b>Незнакомые сокращения:</b> {', '.join(str(u) for u in unknown)}")
        lines.append("Расшифруй их — и я запомню на будущее.")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main processing entry point
# ─────────────────────────────────────────────────────────────────────────────

def process_workout_data(
    text: str, chat_id: str, owner_id: str,
    send_typing_fn=None, send_message_fn=None,
) -> str | None:
    """Full pipeline: detect → parse → store → respond. Returns response text."""
    if send_typing_fn:
        send_typing_fn(chat_id)

    parsed = parse_workout_text(text)
    if parsed is None:
        return "Не удалось разобрать тренировку. Попробуй скопировать текст ещё раз."

    # Determine workout date
    workout_date = date.today()
    bws = parsed.get("body_weights", [])
    if bws and bws[-1].get("date"):
        try:
            workout_date = date.fromisoformat(bws[-1]["date"])
        except (ValueError, TypeError):
            pass
    elif parsed.get("cycle_start_date"):
        try:
            workout_date = date.fromisoformat(parsed["cycle_start_date"])
        except (ValueError, TypeError):
            pass

    # Body weight (latest)
    bw = 0.0
    if bws:
        for bw_entry in reversed(bws):
            if bw_entry.get("weight_kg"):
                bw = float(bw_entry["weight_kg"])
                break

    # Muscle groups
    muscle_groups = parsed.get("muscle_groups", [])

    # Store in CH
    from db import insert_training_entry
    entry_id = insert_training_entry(
        owner_id=owner_id,
        workout_date=workout_date,
        entry_type="workout",
        training_day=parsed.get("training_day", ""),
        muscle_groups=muscle_groups,
        cycle_number=parsed.get("cycle_number", 0),
        cycle_weeks=parsed.get("cycle_weeks", 0),
        body_weight_kg=bw,
        raw_text=text,
        parsed_json=json.dumps(parsed, ensure_ascii=False),
        unknown_terms=parsed.get("unknown_terms", []),
    )
    log.info("Stored workout entry %s (date=%s, muscles=%s, owner=%s)",
             entry_id[:8], workout_date, muscle_groups, owner_id)

    # Auto-learn unknown terms — save for future prompt
    unknown = parsed.get("unknown_terms", [])
    if unknown:
        log.info("Unknown workout terms: %s", unknown)

    # Load previous cycle data for cross-cycle comparison
    prev_cycle = None
    current_cycle = parsed.get("cycle_number", 0)
    if current_cycle > 1:
        from db import query_recent_workouts
        prev_workouts = query_recent_workouts(50, owner_id)
        for pw in prev_workouts:
            if pw.get("cycle_number") and pw["cycle_number"] < current_cycle:
                # Same muscle group day
                pw_muscles = set(pw.get("muscle_groups", []))
                cur_muscles = set(muscle_groups)
                if pw_muscles & cur_muscles:
                    try:
                        prev_cycle = json.loads(pw.get("parsed_json", "{}"))
                        prev_cycle["cycle_number"] = pw["cycle_number"]
                    except Exception:
                        pass
                    break

    return format_workout_response(parsed, text, prev_cycle)
