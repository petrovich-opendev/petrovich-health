"""LLM context builders — protocols / ranges / antagonists / glossary."""
from __future__ import annotations

from pathlib import Path

from glossary import glossary_context_for_prompt

PROJECT_DIR = Path(__file__).resolve().parent

_PROTOCOLS_CACHE: str | None = None
_RANGES_CACHE: str | None = None
_ANTAGONISTS_CACHE: str | None = None


def _load_protocols() -> str:
    """Load protocols.yaml as text for LLM context (cached)."""
    global _PROTOCOLS_CACHE
    if _PROTOCOLS_CACHE is None:
        p = PROJECT_DIR / "protocols.yaml"
        _PROTOCOLS_CACHE = p.read_text(encoding="utf-8") if p.exists() else ""
    return _PROTOCOLS_CACHE


def _load_ranges() -> str:
    """Load optimal_ranges.yaml — includes biomarker ranges + heuristics + lab
    artifacts + cancer screening. Always attached to ask_llm context."""
    global _RANGES_CACHE
    if _RANGES_CACHE is None:
        p = PROJECT_DIR / "optimal_ranges.yaml"
        _RANGES_CACHE = p.read_text(encoding="utf-8") if p.exists() else ""
    return _RANGES_CACHE


def _load_antagonists() -> str:
    """Load nutrient_antagonists.yaml — drug/food/mineral interactions."""
    global _ANTAGONISTS_CACHE
    if _ANTAGONISTS_CACHE is None:
        p = PROJECT_DIR / "nutrient_antagonists.yaml"
        _ANTAGONISTS_CACHE = p.read_text(encoding="utf-8") if p.exists() else ""
    return _ANTAGONISTS_CACHE


_INTERACTION_KEYWORDS = [
    "взаимодейств", "совместим", "препарат", "лекарств", "таблетк",
    "пью", "принима", "назначил", "грейпфрут", "варфарин", "статин",
    "левотирокс", "эутирокс", "l-t4", "метформин", "ингибитор", "ипп",
    "парацетамол", "ибупрофен", "нпвс", "зверобой", "cyp", "антибиотик",
    "антидепрессант", "запор", "тошнот", "glp", "семаглутид", "тирзепатид",
    "омепразол", "аспирин", "паразит", "гепатотокс", "нефротокс",
]


def _is_interaction_question(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in _INTERACTION_KEYWORDS)


_PROTOCOL_KEYWORDS = [
    "доз", "дозировк", "протокол", "пептид", "тестостерон", "тзт", "trt",
    "гормон роста", "hgh", "гр ", "бпц", "bpc", "tb-500", "tb500",
    "ипаморелин", "ipamorelin", "cjc", "ghrp", "анастрозол", "аримидекс",
    "экземестан", "аромазин", "тамоксифен", "кломид", "кломифен",
    "хгч", "hcg", "пкт", "pct", "курс", "бласт", "круиз",
    "разведени", "разводить", "шприц", "тик", "ед/день", "мг/нед",
    "полупериод", "полураспад", "pt-141", "pt141",
]


def _is_protocol_question(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in _PROTOCOL_KEYWORDS)


def _build_glossary_context() -> str:
    """Include global glossary in LLM prompt if terms exist."""
    try:
        from db import get_client
        return glossary_context_for_prompt(get_client(), max_terms=30)
    except Exception:
        return ""


def _build_protocol_context(question: str) -> str:
    """Include protocol knowledge base if question is about compounds/dosing."""
    if not _is_protocol_question(question):
        return ""
    protocols = _load_protocols()
    if not protocols:
        return ""
    return (
        "\n=== БАЗА ПРОТОКОЛОВ И ДОЗИРОВОК ===\n"
        "Используй данные ниже для расчётов. Все дозы, полупериоды, формулы — из базы. "
        "Считай конкретные числа: мг, мкг, ЕД, тики шприца. "
        "Учитывай анализы пользователя при рекомендациях.\n\n"
        f"{protocols}\n"
    )


def _build_ranges_context() -> str:
    """Always attach optimal_ranges.yaml — ranges, heuristics, lab artifacts,
    screening. Core clinical reasoning reference."""
    ranges = _load_ranges()
    if not ranges:
        return ""
    return (
        "\n=== ОПТИМАЛЬНЫЕ ДИАПАЗОНЫ, ЭВРИСТИКИ, АРТЕФАКТЫ, ОНКОСКРИНИНГ ===\n"
        "Справочник для интерпретации. Используй `heuristics` для расчётов "
        "(De Ritis, anion gap, corrected Ca, MCV-anemia, HOMA-IR, FIB-4). "
        "Сверяй замеры с `lab_artifacts` перед клиническим выводом. "
        "При разговоре о возрасте / риске — сверяйся с `cancer_screening`.\n\n"
        f"{ranges}\n"
    )


def _build_antagonists_context(question: str) -> str:
    """Attach nutrient_antagonists.yaml when question touches on drugs / food /
    interactions. Covers CYP3A4, hepatotox, nephrotox, QT-prolonging, warfarin,
    serotonergic — and mineral antagonisms."""
    if not _is_interaction_question(question):
        return ""
    antagonists = _load_antagonists()
    if not antagonists:
        return ""
    return (
        "\n=== ВЗАИМОДЕЙСТВИЯ: ПРЕПАРАТЫ / ПИЩА / МИНЕРАЛЫ ===\n"
        "Проверяй назначения и питание пользователя против этой базы. "
        "Если упомянут препарат из `hepatotoxic_drugs_warning` и АЛТ/АСТ "
        "повышены — явно предупреди. При совместном приёме препаратов из "
        "`qt_prolonging_drugs` — напомни про ЭКГ.\n\n"
        f"{antagonists}\n"
    )
