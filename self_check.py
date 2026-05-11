"""LLM-based self-verification layer for extract / digest / profile.

Each public function makes ONE extra Claude call comparing the source data
to the first-pass output. Goal: catch hallucinated numbers, units, and
entities — not clinical interpretation.

Returns a discrepancy report:
    {"verified": bool, "discrepancies": [...], "summary": "..."}

Wiring is observational only (logs); the original return value of the
caller is not blocked on verification failure.

Kill switch: env LLM_SELFCHECK=0 → all verify_* return verified=True
without making any LLM call. Read at call time, not module load.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from utils import parse_json_response

log = logging.getLogger("health-bot")


# Tolerance rules are described in the prompts (so the LLM applies them);
# a Python-side helper exists too for any future deterministic re-check.
# Kept loose: ±0.05 absolute OR ±1% relative; date/unit normalization OK.
_TOLERANCE_NOTE = """ПРАВИЛА ТОЛЕРАНТНОСТИ (НЕ считать расхождением):
- Нормализация имени биомаркера: «Hb (Hemoglobin)» в источнике == «Гемоглобин» в результате — OK.
- Регистр и пробелы в единицах: «Ед/л» == «ед/л» == «ед / л» — OK.
- Числовое значение совпадает в пределах ±0.05 по модулю ИЛИ ±1% относительно — OK.
- Формат даты: «15.04.2026» == «2026-04-15» — OK.
- Запятая vs точка как десятичный разделитель: «3,9» == «3.9» — OK.

ЧТО ФЛАГАТЬ:
- Числовое значение не находится в источнике вообще или отличается больше допуска.
- Единица измерения принципиально другая (мг vs МЕ, нг/мл vs мкг/л — разные классы).
- Биомаркер/сущность, которой в источнике нет.
- Дата, которой в источнике нет.

ЧТО НЕ ФЛАГАТЬ:
- Клиническая интерпретация («АЛТ повышен», «вероятно дефицит D»). Это вывод, не факт.
- Стилистика, порядок слов, синонимы для тематических меток.
"""

_OUTPUT_CONTRACT = """ЖЁСТКИЕ ПРАВИЛА ВЫХОДА:
1. ВЫХОД — только один JSON-объект. Никакого markdown, текста до/после, ``` или комментариев.
2. Язык полей — русский.
3. Будь СКЕПТИЧЕН: задача — найти ЛЮБОЕ несоответствие, а не оправдать модель.
4. verified=true только если discrepancies — пустой массив. Любое расхождение → verified=false.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────
def _selfcheck_enabled() -> bool:
    # Read at call time (not import time) so toggling LLM_SELFCHECK in env
    # takes effect for the next call without restarting the process.
    return os.getenv("LLM_SELFCHECK", "1").strip() not in ("0", "false", "False", "")


def _empty_ok(reason: str = "selfcheck disabled") -> dict:
    return {"verified": True, "discrepancies": [], "summary": reason}


def _call_verifier(prompt: str, model: str = "claude-opus-4-7", timeout: int = 240) -> dict | None:
    """Invoke `claude -p` with the verification prompt. Returns parsed JSON or None.

    Same subprocess pattern as extractor / diagnostician, deliberately not
    imported from there to avoid circular deps.
    """
    try:
        log.info("self_check: calling claude model=%s (%d chars)", model, len(prompt))
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            log.warning("self_check: claude failed rc=%d: %s", result.returncode, detail)
            return None
        return parse_json_response(result.stdout.strip())
    except subprocess.TimeoutExpired:
        log.warning("self_check: claude timeout after %ds", timeout)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("self_check: claude error: %s", exc)
        return None


def _normalise_report(parsed: dict | None) -> dict:
    """Coerce the LLM output into the canonical contract shape."""
    if not isinstance(parsed, dict):
        return {"verified": False, "discrepancies": [],
                "summary": "verifier returned no JSON"}
    discrepancies = parsed.get("discrepancies") or []
    if not isinstance(discrepancies, list):
        discrepancies = []
    # verified=True iff discrepancies is empty (defensive — even if LLM
    # contradicts itself, the contract wins).
    verified = bool(parsed.get("verified")) and len(discrepancies) == 0
    if discrepancies and verified:
        verified = False
    return {
        "verified": verified,
        "discrepancies": discrepancies,
        "summary": str(parsed.get("summary", "")),
    }


def _log_discrepancies(scope: str, report: dict) -> None:
    discs = report.get("discrepancies") or []
    log.info(
        "verify_%s: verified=%s discrepancies=%d",
        scope, report.get("verified"), len(discs),
    )
    for i, d in enumerate(discs):
        try:
            log.warning("verify_%s discrepancy[%d]: %s", scope, i, json.dumps(d, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            log.warning("verify_%s discrepancy[%d]: %r", scope, i, d)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Extraction verification
# ─────────────────────────────────────────────────────────────────────────────
_VERIFY_EXTRACTION_PROMPT = """Ты — строгий верификатор извлечения лабораторных анализов.

Задача: для КАЖДОЙ строки результата проверить, что биомаркер, значение и единица измерения ДОСЛОВНО присутствуют в исходном тексте PDF (с учётом правил толерантности ниже).

""" + _TOLERANCE_NOTE + """

Схема ответа:
{
  "verified": true|false,
  "discrepancies": [
    {
      "row_index": 0,
      "field": "value|unit|biomarker|date|lab_name",
      "extracted": "что написано в результате",
      "found_in_source": "что нашлось в источнике (или 'not_found')"
    }
  ],
  "summary": "1-2 предложения о результате проверки"
}

""" + _OUTPUT_CONTRACT + """

=== ИСХОДНЫЙ ТЕКСТ PDF ===
__SOURCE_TEXT__

=== ИЗВЛЕЧЁННЫЙ РЕЗУЛЬТАТ ===
collected_at: __COLLECTED_AT__
lab_name: __LAB_NAME__
results (по строкам):
__ROWS__
"""


def verify_extraction(source_text: str, result: dict) -> dict:
    """Verify that an extractor result matches the source PDF text."""
    if not _selfcheck_enabled():
        return _empty_ok()

    rows = result.get("results") or []
    rows_lines = []
    for i, r in enumerate(rows):
        rows_lines.append(
            f"[{i}] biomarker={r.get('biomarker')!r} "
            f"biomarker_original={r.get('biomarker_original')!r} "
            f"value={r.get('value')!r} unit={r.get('unit')!r} "
            f"ref_low={r.get('ref_low')!r} ref_high={r.get('ref_high')!r}"
        )
    rows_block = "\n".join(rows_lines) if rows_lines else "(пусто)"

    prompt = (
        _VERIFY_EXTRACTION_PROMPT
        .replace("__SOURCE_TEXT__", (source_text or "")[:15000])
        .replace("__COLLECTED_AT__", str(result.get("collected_at")))
        .replace("__LAB_NAME__", str(result.get("lab_name")))
        .replace("__ROWS__", rows_block)
    )

    report = _normalise_report(_call_verifier(prompt, timeout=240))
    return report


# ─────────────────────────────────────────────────────────────────────────────
# 2. Digest verification
# ─────────────────────────────────────────────────────────────────────────────
_VERIFY_DIGEST_PROMPT = """Ты — строгий верификатор дневного дайджеста переписки.

Источник — лог чата с метками USER / BOT. Результат — JSON с полями digest, topics, user_concerns, new_info.

ПРОВЕРЬ:
1. Каждое утверждение в `user_concerns` опирается на реплику USER (не BOT). Если жалоба процитирована из BOT — это расхождение.
2. Каждый ФАКТ в `new_info` (особенно дозы препаратов с единицами — мг, МЕ, мкг) опирается на USER-сообщение и совпадает по числу/единице.
3. `digest` не вводит сущностей (препараты, симптомы, события), которых нет в логе.

""" + _TOLERANCE_NOTE + """

Схема ответа:
{
  "verified": true|false,
  "discrepancies": [
    {
      "field": "user_concerns|new_info|digest|topics",
      "extracted": "фрагмент из результата",
      "found_in_source": "что нашлось в источнике (или 'not_found' / 'found_only_in_BOT')"
    }
  ],
  "summary": "1-2 предложения"
}

""" + _OUTPUT_CONTRACT + """

=== ЛОГ ЧАТА ===
__CHAT_LOG__

=== РЕЗУЛЬТАТ ДАЙДЖЕСТА ===
digest: __DIGEST__
topics: __TOPICS__
user_concerns: __USER_CONCERNS__
new_info: __NEW_INFO__
"""


def verify_digest(chat_log: str, digest_output: dict) -> dict:
    """Verify a daily digest against the chat log."""
    if not _selfcheck_enabled():
        return _empty_ok()

    prompt = (
        _VERIFY_DIGEST_PROMPT
        .replace("__CHAT_LOG__", (chat_log or "")[:15000])
        .replace("__DIGEST__", str(digest_output.get("digest", "")))
        .replace("__TOPICS__", json.dumps(digest_output.get("topics", []), ensure_ascii=False))
        .replace("__USER_CONCERNS__", str(digest_output.get("user_concerns", "")))
        .replace("__NEW_INFO__", str(digest_output.get("new_info", "")))
    )

    report = _normalise_report(_call_verifier(prompt, timeout=180))
    return report


# ─────────────────────────────────────────────────────────────────────────────
# 3. Profile verification
# ─────────────────────────────────────────────────────────────────────────────
_VERIFY_PROFILE_PROMPT = """Ты — строгий верификатор персонального медицинского профиля.

Источник — собранный блок: лабораторные строки + дневные дайджесты + SPC-блок. Результат — JSON-профиль с полями key_findings, watchlist, correlations.

ПРОВЕРЬ:
1. Каждое числовое утверждение в `key_findings` («АЛТ 185 Ед/л», «Гемоглобин 145 г/л», «-12% за 3 месяца») — соответствует строке в источнике. Если число не находится — расхождение.
2. Для каждого элемента `watchlist`:
   - `last_value` совпадает с самым свежим значением этого биомаркера в источнике;
   - `last_date` совпадает с датой этого самого свежего значения.
3. Каждый биомаркер в `correlations` реально упомянут в исходных данных.

НЕ флагай: клинические выводы («вероятно стеатогепатоз», «низкая конверсия D3»), evidence-уровни в скобках, рекомендации к действию.
ФЛАГАЙ: выдуманные числа («r=0.85» если в SPC-блоке его нет), ошибочные единицы, биомаркеры/даты, отсутствующие в источнике.

""" + _TOLERANCE_NOTE + """

Схема ответа:
{
  "verified": true|false,
  "discrepancies": [
    {
      "field": "key_findings|watchlist|correlations",
      "index": 0,
      "extracted": "фрагмент из профиля",
      "found_in_source": "ближайшее совпадение или 'not_found'"
    }
  ],
  "summary": "1-2 предложения"
}

""" + _OUTPUT_CONTRACT + """

=== ИСТОЧНИК (lab + digests + SPC) ===
__LAB_DATA__

=== ПРОФИЛЬ — РЕЗУЛЬТАТ ===
key_findings: __KEY_FINDINGS__
watchlist: __WATCHLIST__
correlations: __CORRELATIONS__
"""


def verify_profile(lab_data: str, profile_output: dict) -> dict:
    """Verify a health profile against its assembled source block."""
    if not _selfcheck_enabled():
        return _empty_ok()

    prompt = (
        _VERIFY_PROFILE_PROMPT
        .replace("__LAB_DATA__", (lab_data or "")[:60000])
        .replace("__KEY_FINDINGS__", json.dumps(profile_output.get("key_findings", []), ensure_ascii=False))
        .replace("__WATCHLIST__", json.dumps(profile_output.get("watchlist", []), ensure_ascii=False))
        .replace("__CORRELATIONS__", json.dumps(profile_output.get("correlations", []), ensure_ascii=False))
    )

    # Profile source is large (~30-60k chars) → allow a longer timeout.
    report = _normalise_report(_call_verifier(prompt, timeout=420))
    return report


__all__ = [
    "verify_extraction",
    "verify_digest",
    "verify_profile",
]
