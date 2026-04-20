"""LLM-based biomarker extraction from raw PDF text."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import date

log = logging.getLogger("health-bot")

CLASSIFICATION_PROMPT = """Классифицируй медицинский документ. Верни СТРОГО JSON (без markdown):
{
  "doc_class": "lab_results|prescription|estimate|referral|consultation|research|certificate|other",
  "doc_type": "тип (blood/biochemistry/hormones/eeg/holter/mri/ultrasound/ecg/plan/receipt/other)",
  "title": "краткое название документа",
  "collected_at": "YYYY-MM-DD или null",
  "lab_name": "название лаборатории или клиники или null",
  "summary": "1-2 предложения: что это за документ и ключевая информация из него"
}

Классы:
- lab_results — готовые результаты анализов с числовыми значениями
- estimate — смета/план/заказ на анализы (ещё не сдано, только список + цены)
- prescription — назначение врача, рецепт
- referral — направление на исследование
- consultation — заключение врача после осмотра
- research — результат исследования (ЭЭГ, Холтер, МРТ, УЗИ) — текстовое заключение
- certificate — справка, выписка
- other — не удалось определить

=== ТЕКСТ ===
"""

EXTRACTION_PROMPT = """Ты — медицинский парсер лабораторных анализов. Из текста ниже извлеки ВСЕ лабораторные показатели.

ПРАВИЛА:
1. Извлеки КАЖДЫЙ показатель — не пропускай ни один
2. Если значение нечисловое (например "отрицательный", "не обнаружено") — пропусти, берём только числовые
3. Нормализуй название показателя (biomarker) на русском: "Гемоглобин", "Глюкоза", "ТТГ", "Холестерин общий" и т.д.
4. Сохрани оригинальное название из PDF как biomarker_original
5. Определи категорию: blood (ОАК), biochemistry (биохимия), hormones, vitamins, urine, coagulation, immunology, other
6. Единицы измерения — как в PDF
7. Границы нормы (ref_low, ref_high) — из PDF. Если указан только один предел, второй = null. Если нет нормы — оба null
8. Определи дату сдачи анализа (collected_at) в формате YYYY-MM-DD. Ищи: "дата забора", "дата сдачи", дату в шапке
9. Определи название лаборатории (lab_name). Ищи: "Инвитро", "Helix", "KDL", "Гемотест", "CMD" и др.
10. Если дата или лаборатория не найдены — верни null для них

Верни СТРОГО JSON (без markdown, без ```):
{
  "collected_at": "YYYY-MM-DD" или null,
  "lab_name": "название" или null,
  "results": [
    {
      "biomarker": "Гемоглобин",
      "biomarker_original": "Hemoglobin (Hb)",
      "category": "blood",
      "value": 145.0,
      "unit": "г/л",
      "ref_low": 130.0,
      "ref_high": 160.0
    }
  ]
}

=== ТЕКСТ ИЗ PDF ===
"""


def classify_document(
    raw_text: str,
    model: str = "claude-opus-4-7",
    timeout: int = 60,
) -> dict:
    """
    Classify a medical document before extraction.
    """
    prompt = CLASSIFICATION_PROMPT + raw_text[:8000]
    try:
        log.info("Classifying document with model=%s (%d chars)", model, len(raw_text))
        result = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            parsed = _parse_json(result.stdout.strip())
            if parsed and "doc_class" in parsed:
                log.info("Classified: doc_class=%s doc_type=%s", parsed["doc_class"], parsed.get("doc_type"))
                return parsed
    except Exception as exc:
        log.warning("Classification failed: %s", exc)

    return {"doc_class": "other", "doc_type": "other", "title": "", "summary": "",
            "collected_at": None, "lab_name": None}


def extract_biomarkers(
    raw_text: str,
    primary_model: str = "claude-opus-4-7",
    fallback_model: str = "claude-opus-4-7",
    timeout: int = 120,
) -> dict:
    """
    Send raw PDF text to LLM for structured extraction.

    Returns dict with keys: collected_at, lab_name, results (list of biomarkers).
    """
    prompt = EXTRACTION_PROMPT + raw_text[:15000]  # limit to avoid token overflow

    for model in [primary_model, fallback_model]:
        try:
            log.info("Extracting biomarkers with model=%s (text %d chars)", model, len(raw_text))
            result = subprocess.run(
                ["claude", "-p", "--model", model, prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                log.warning("claude CLI failed (rc=%d) model=%s: %s",
                            result.returncode, model, result.stderr.strip()[:200])
                continue

            parsed = _parse_json(result.stdout.strip())
            if parsed and "results" in parsed:
                log.info("Extracted %d biomarkers with %s", len(parsed["results"]), model)
                return parsed

        except subprocess.TimeoutExpired:
            log.warning("Extraction timeout model=%s", model)
        except Exception as exc:
            log.warning("Extraction error model=%s: %s", model, exc)

    raise RuntimeError("Failed to extract biomarkers from PDF text")


def _parse_json(raw: str) -> dict | None:
    """Extract JSON from LLM response, handling markdown wrappers."""
    # Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try finding JSON block
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def validate_results(data: dict, fallback_date: date | None = None) -> tuple[list[dict], list[str]]:
    """
    Validate and clean extracted results.

    Returns:
        (valid_rows, warnings)
    """
    warnings: list[str] = []
    valid: list[dict] = []

    collected_at = data.get("collected_at")
    if collected_at:
        try:
            collected_at = date.fromisoformat(collected_at)
        except (ValueError, TypeError):
            warnings.append(f"Invalid date '{collected_at}', using fallback")
            collected_at = fallback_date or date.today()
    else:
        collected_at = fallback_date or date.today()
        warnings.append("Date not found in PDF, using today")

    lab_name = data.get("lab_name") or ""

    for i, r in enumerate(data.get("results", [])):
        biomarker = r.get("biomarker", "").strip()
        if not biomarker:
            warnings.append(f"Row {i}: empty biomarker name, skipped")
            continue

        try:
            value = float(r["value"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"Row {i} ({biomarker}): invalid value '{r.get('value')}', skipped")
            continue

        ref_low = _safe_float(r.get("ref_low"))
        ref_high = _safe_float(r.get("ref_high"))

        valid.append({
            "collected_at": collected_at,
            "category": r.get("category", "other"),
            "biomarker": biomarker,
            "biomarker_original": r.get("biomarker_original", biomarker),
            "value": value,
            "unit": r.get("unit", ""),
            "ref_low": ref_low,
            "ref_high": ref_high,
            "lab_name": lab_name,
        })

    return valid, warnings


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
