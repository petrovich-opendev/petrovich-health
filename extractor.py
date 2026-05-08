"""LLM-based biomarker extraction from raw PDF text."""
from __future__ import annotations

import json
import logging
import re
from datetime import date

from llm_client import LLMError, chat_completion

log = logging.getLogger("health-bot")

CLASSIFICATION_PROMPT = """Ты классифицируешь медицинский документ.

ЖЁСТКИЕ ПРАВИЛА:
1. ВЫХОД — только один JSON-объект. Никакого markdown, преамбулы, объяснений, ``` или текста до/после JSON.
2. Не придумывай данные. Если поля нет в тексте — null.
3. Дата в формате YYYY-MM-DD. Если в документе DD.MM.YYYY — конвертируй. Если года нет — null.
4. summary — пересказ того, что написано в документе. Не добавляй интерпретации, диагнозов, выводов.
5. Отвечай на русском.

Схема ответа:
{
  "doc_class": "lab_results|prescription|estimate|referral|consultation|research|certificate|other",
  "doc_type": "blood|biochemistry|hormones|eeg|holter|mri|ultrasound|ecg|plan|receipt|other",
  "title": "краткое название документа",
  "collected_at": "YYYY-MM-DD или null",
  "lab_name": "название лаборатории или null",
  "summary": "1-2 предложения, только то что есть в тексте"
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

EXTRACTION_PROMPT = """Ты — детерминированный парсер лабораторных анализов. Извлеки ВСЕ числовые показатели из текста.

ЖЁСТКИЕ ПРАВИЛА:
1. ВЫХОД — только один JSON-объект. Никакого markdown, текста до/после, никаких ``` и комментариев.
2. НЕ выдумывай показатели, значения, единицы или границы нормы. Только то, что дословно есть в тексте.
3. Извлеки КАЖДЫЙ числовой показатель — не пропускай ни одного.
4. Если значение нечисловое («отрицательный», «не обнаружено», «следы») — пропусти.
5. Если число записано с запятой («3,9») — конвертируй в float через точку (3.9).
6. Единицы — копируй из PDF буква в букву. Не конвертируй и не нормализуй (если в PDF «мМЕ/л» — пиши «мМЕ/л», не «мЕд/л»).
7. ref_low / ref_high — числа из PDF. Если указан один предел («<5.2», «>30») — другой = null. Если нормы вообще нет — оба null.
8. biomarker — нормализуй на русском: «Гемоглобин», «Глюкоза», «ТТГ», «Холестерин общий», «АЛТ», «АСТ», «ИФР-1», «25-OH витамин D» и т.д.
9. biomarker_original — точное название как в PDF (включая скобки и латиницу).
10. category — одно из: blood (ОАК), biochemistry, hormones, vitamins, urine, coagulation, immunology, other.
11. collected_at — YYYY-MM-DD. Ищи «дата забора», «дата сдачи», дату в шапке. Не было найдено — null.
12. lab_name — название клиники/лаборатории если оно явно указано (Инвитро, Helix, KDL, Гемотест, CMD, ЦМД, ЛабКвест и др.). Иначе null.

Схема ответа:
{
  "collected_at": "YYYY-MM-DD" | null,
  "lab_name": "название" | null,
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
    model: str | None = None,
    timeout: int = 60,
) -> dict:
    """
    Classify a medical document before extraction.
    """
    prompt = CLASSIFICATION_PROMPT + raw_text[:8000]
    try:
        raw = chat_completion(prompt, model=model, max_tokens=2048, timeout=timeout)
        parsed = _parse_json(raw)
        if parsed and "doc_class" in parsed:
            log.info("Classified: doc_class=%s doc_type=%s", parsed["doc_class"], parsed.get("doc_type"))
            return parsed
    except LLMError as exc:
        log.warning("Classification LLM failed: %s", exc)
    except Exception as exc:
        log.warning("Classification failed: %s", exc)

    return {"doc_class": "other", "doc_type": "other", "title": "", "summary": "",
            "collected_at": None, "lab_name": None}


def extract_biomarkers(
    raw_text: str,
    primary_model: str | None = None,
    fallback_model: str | None = None,
    timeout: int = 240,
) -> dict:
    """
    Send raw PDF text to LLM for structured extraction.

    Returns dict with keys: collected_at, lab_name, results (list of biomarkers).
    """
    prompt = EXTRACTION_PROMPT + raw_text[:15000]  # limit to avoid token overflow

    models_to_try = [primary_model] if primary_model == fallback_model else [primary_model, fallback_model]
    for model in models_to_try:
        try:
            raw = chat_completion(prompt, model=model, max_tokens=8000, timeout=timeout)
            parsed = _parse_json(raw)
            if parsed and "results" in parsed:
                log.info("Extracted %d biomarkers with %s", len(parsed["results"]), model)
                return parsed
            log.warning("Extraction returned no 'results' field, model=%s", model)
        except LLMError as exc:
            log.warning("Extraction LLM error model=%s: %s", model, exc)
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
