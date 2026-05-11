"""Drug-Drug Interaction checker.

The table ``drug_interactions_v1`` is a curated DDI knowledge base; this
module is the read side — it normalises a user-typed drug name to a
canonical INN, then queries the table for all known partners.

For the CEO demo the seed CSV is purposely small (~25 pairs) but
focused: every PROMOMED flagship (tirzepatide / semaglutide / apalutamide /
lenalidomide / anastrozole / tamoxifen) is covered, plus the
high-prevalence "everyday" pairs the user will recognise (warfarin
+NSAIDs, statins+macrolides, levothyroxine+iron).

The normaliser is intentionally LLM-free: a regex/keyword map handles
the common cases (trade-name → INN), and a deterministic fallback
returns the input lowercased. This keeps `/ddi` and the
``/protocol``-add hook fast and offline — no per-call Claude latency,
no API budget.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("health-bot")


# Trade name → INN mapping. Covers PROMOMED flagships + classic generics
# the bot is likely to see. Keys are *normalised forms* (lowercased,
# trademark/star symbols stripped). Add aliases freely — the longer
# the list, the more lookups succeed.
_TRADE_TO_INN: dict[str, str] = {
    # PROMOMED flagships
    "тирзетта": "тирзепатид",
    "велгия": "семаглутид",
    "велгия эко": "семаглутид",
    "квинсента": "семаглутид",
    "семальтара": "семаглутид перорально",
    "прадетро": "апалутамид",
    "леналидомид": "леналидомид",
    "велаксин": "венлафаксин",
    "арепливир": "фавипиравир",
    # Competitor GLP-1 and common comparators
    "семавик": "семаглутид",          # Герофарм
    "ozempic": "семаглутид",
    "озепик": "семаглутид",
    "wegovy": "семаглутид",
    "вегови": "семаглутид",
    "rybelsus": "семаглутид перорально",
    "ребелсас": "семаглутид перорально",
    "mounjaro": "тирзепатид",
    "маунджаро": "тирзепатид",
    "saxenda": "лираглутид",
    "саксенда": "лираглутид",
    "victoza": "лираглутид",
    # Other onco/endo classics
    "erleada": "апалутамид",
    "эрлеада": "апалутамид",
    "revlimid": "леналидомид",
    "ревлимид": "леналидомид",
    "arimidex": "анастрозол",
    "anastrozol": "анастрозол",
    "анастрозол": "анастрозол",
    "tamoxifen": "тамоксифен",
    "тамоксифен": "тамоксифен",
    # Common Rx the bot sees often
    "glucophage": "метформин",
    "сиофор": "метформин",
    "метформин": "метформин",
    "warfarin": "варфарин",
    "варфарин": "варфарин",
    "varfarex": "варфарин",
    "lipitor": "аторвастатин",
    "аторвастатин": "аторвастатин",
    "synthroid": "левотироксин",
    "л-тироксин": "левотироксин",
    "эутирокс": "левотироксин",
    "левотироксин": "левотироксин",
}


def normalise_drug(name: str) -> str:
    """Return a canonical INN string from any user-typed drug reference.

    Strips trademark/star symbols, lowercases, drops trailing dose info,
    then maps trade names to INN via ``_TRADE_TO_INN``. Falls back to the
    cleaned input — the DDI table may still match if the user typed the
    INN directly.
    """
    if not name:
        return ""
    s = name.strip().lower()
    # Strip ®/™/asterisks and any (parens) (lab labels often look like
    # "Тирзетта®(*)*" — these are dataset artefacts, not the drug name).
    s = re.sub(r"[®™()*]", " ", s)
    # Drop "5 мг", "250 мг", "20 шт" suffixes — they're dose/pack, not name.
    s = re.sub(r"\b\d+(?:[\.,]\d+)?\s*(?:мг|мкг|ме|мл|г|ед|шт)\b.*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _TRADE_TO_INN.get(s, s)


@dataclass
class Interaction:
    drug_b_inn: str
    severity: str
    mechanism: str
    recommendation: str
    source: str

    def render(self) -> str:
        icon = {"major": "🚨", "moderate": "⚠️", "minor": "ℹ️"}.get(self.severity, "•")
        return (f"{icon} <b>{self.drug_b_inn}</b> [{self.severity}]\n"
                f"   Механизм: {self.mechanism}\n"
                f"   Что делать: {self.recommendation}\n"
                f"   Источник: <i>{self.source}</i>")


def check_interactions(ch, drug_inn: str) -> list[Interaction]:
    """All known DDI partners of ``drug_inn`` (already-normalised INN).

    Returns ordered by severity (major → moderate → minor) so the most
    critical pairs surface first in the rendered list.
    """
    if not drug_inn:
        return []
    rows = ch.query(
        "SELECT drug_b_inn, severity, mechanism, recommendation, source "
        "FROM drug_interactions_v1 WHERE drug_a_inn = {n:String} "
        "ORDER BY multiIf(severity='major',1,severity='moderate',2,3), drug_b_inn",
        parameters={"n": drug_inn},
    ).result_rows
    return [Interaction(*r) for r in rows]


def check_stack(ch, drug_inns: list[str]) -> list[tuple[str, Interaction]]:
    """Return all DDI hits BETWEEN drugs in the supplied stack.

    Output: list of (drug_a_inn, Interaction). Each interacting pair
    shows up once with the alphabetically-first drug as the key —
    de-dup is intentional: the user doesn't need "A↔B" and "B↔A" twice.
    """
    seen: set[tuple[str, str]] = set()
    hits: list[tuple[str, Interaction]] = []
    stack = [d for d in (normalise_drug(x) for x in drug_inns) if d]
    for drug in stack:
        for ix in check_interactions(ch, drug):
            if ix.drug_b_inn in stack:
                pair = tuple(sorted((drug, ix.drug_b_inn)))
                if pair in seen:
                    continue
                seen.add(pair)
                hits.append((pair[0], Interaction(
                    drug_b_inn=pair[1] if pair[0] == drug else drug,
                    severity=ix.severity,
                    mechanism=ix.mechanism,
                    recommendation=ix.recommendation,
                    source=ix.source,
                )))
    # major first
    severity_order = {"major": 0, "moderate": 1, "minor": 2}
    hits.sort(key=lambda h: (severity_order.get(h[1].severity, 9), h[0]))
    return hits


__all__ = ["normalise_drug", "Interaction", "check_interactions", "check_stack"]
