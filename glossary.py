"""
Global Bot Glossary — learns terms from all interactions, no personal data.

Domains: medical, workout, nutrition, protocol, general
Statuses: candidate → verified → trusted (or rejected)

The glossary grows automatically:
1. Unknown workout abbreviations → candidates
2. Lab biomarker names → auto-verified (from real data)
3. LLM-parsed terms → candidates with context
4. User corrections → trusted

Used for:
- Enriching LLM prompts with domain knowledge
- Improving parser accuracy over time
- Future: RAG, graph, agent training
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

log = logging.getLogger("health-bot")


# ─────────────────────────────────────────────────────────────────────────────
# Core CRUD
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(term: str) -> str:
    """Normalize term for deduplication: lowercase, strip, collapse spaces."""
    return re.sub(r'\s+', ' ', term.strip().lower())


def add_term(
    ch,
    term: str,
    domain: str = "general",
    category: str = "",
    definition: str = "",
    definition_en: str = "",
    aliases: list[str] | None = None,
    related_terms: list[str] | None = None,
    source: str = "auto",
    status: str = "candidate",
    confidence: float = 0.5,
    context_example: str = "",
) -> str:
    """Add or update a glossary term. Returns term ID."""
    normalized = _normalize(term)

    # Check if exists
    existing = ch.query(
        "SELECT id, usage_count, context_examples, status, confidence "
        "FROM glossary_terms FINAL "
        "WHERE term_normalized = {n:String} AND domain = {d:String}",
        parameters={"n": normalized, "d": domain},
    )

    if existing.result_rows:
        # Update: bump usage_count, add context, keep higher status
        row = existing.result_rows[0]
        eid = row[0]
        old_count = row[1]
        old_contexts = row[2] or []
        old_status = row[3]
        old_confidence = row[4]

        # Don't downgrade status
        status_rank = {"rejected": 0, "candidate": 1, "verified": 2, "trusted": 3}
        if status_rank.get(status, 0) < status_rank.get(old_status, 0):
            status = old_status

        new_confidence = max(old_confidence, confidence)
        new_contexts = list(old_contexts)
        if context_example and context_example not in new_contexts:
            new_contexts = (new_contexts + [context_example])[-5:]  # keep last 5

        # created_at must be a valid datetime — CH column is non-nullable DateTime.
        # On update we don't fetch the original; use now() (ReplacingMergeTree
        # FINAL collapses on (id, updated_at) so created_at value is benign).
        ch.insert("glossary_terms", [[
            eid, datetime.now(), datetime.now(), term, normalized, domain, category,
            definition or "", definition_en or "",
            aliases or [], [], related_terms or [],
            source, status, new_confidence,
            old_count + 1, new_contexts, "{}",
        ]], column_names=[
            "id", "created_at", "updated_at", "term", "term_normalized",
            "domain", "category", "definition", "definition_en",
            "aliases", "antonyms", "related_terms",
            "source", "status", "confidence",
            "usage_count", "context_examples", "metadata",
        ])
        log.debug("Glossary updated: %s/%s (count=%d)", domain, term, old_count + 1)
        return eid

    # New term
    tid = str(uuid.uuid4())
    contexts = [context_example] if context_example else []
    ch.insert("glossary_terms", [[
        tid, datetime.now(), datetime.now(), term, normalized, domain, category,
        definition, definition_en,
        aliases or [], [], related_terms or [],
        source, status, confidence,
        1, contexts, "{}",
    ]], column_names=[
        "id", "created_at", "updated_at", "term", "term_normalized",
        "domain", "category", "definition", "definition_en",
        "aliases", "antonyms", "related_terms",
        "source", "status", "confidence",
        "usage_count", "context_examples", "metadata",
    ])
    log.info("Glossary new term: %s/%s [%s] = %s", domain, term, status, definition[:80])
    return tid


def lookup_term(ch, term: str, domain: str | None = None) -> dict | None:
    """Find a term in the glossary."""
    normalized = _normalize(term)
    where = "term_normalized = {n:String}"
    params = {"n": normalized}
    if domain:
        where += " AND domain = {d:String}"
        params["d"] = domain

    result = ch.query(
        f"SELECT term, domain, category, definition, definition_en, "
        f"aliases, related_terms, status, confidence, usage_count "
        f"FROM glossary_terms FINAL WHERE {where} LIMIT 1",
        parameters=params,
    )
    if not result.result_rows:
        return None
    r = result.result_rows[0]
    return dict(zip(result.column_names, r))


def search_glossary(ch, query: str, domain: str | None = None,
                    limit: int = 20) -> list[dict]:
    """Search glossary by term or definition text."""
    where = (
        "(positionCaseInsensitiveUTF8(term, {q:String}) > 0 "
        "OR positionCaseInsensitiveUTF8(definition, {q:String}) > 0 "
        "OR has(aliases, {q:String}))"
    )
    params = {"q": query, "lim": limit}
    if domain:
        where += " AND domain = {d:String}"
        params["d"] = domain

    result = ch.query(
        f"SELECT term, domain, category, definition, status, usage_count "
        f"FROM glossary_terms FINAL WHERE {where} "
        f"ORDER BY usage_count DESC LIMIT {{lim:UInt32}}",
        parameters=params,
    )
    return [dict(zip(result.column_names, r)) for r in result.result_rows]


def get_domain_terms(ch, domain: str, min_status: str = "candidate",
                     limit: int = 100) -> list[dict]:
    """Get all terms for a domain, filtered by minimum status."""
    status_filter = {
        "candidate": "status IN ('candidate','verified','trusted')",
        "verified": "status IN ('verified','trusted')",
        "trusted": "status = 'trusted'",
    }.get(min_status, "1=1")

    result = ch.query(
        f"SELECT term, category, definition, definition_en, aliases, status, confidence "
        f"FROM glossary_terms FINAL "
        f"WHERE domain = {{d:String}} AND {status_filter} "
        f"ORDER BY category, term LIMIT {{lim:UInt32}}",
        parameters={"d": domain, "lim": limit},
    )
    return [dict(zip(result.column_names, r)) for r in result.result_rows]


def glossary_stats(ch) -> dict:
    """Get glossary statistics."""
    result = ch.query(
        "SELECT domain, status, count() as cnt "
        "FROM glossary_terms FINAL GROUP BY domain, status "
        "ORDER BY domain, status"
    )
    stats = {}
    total = 0
    for row in result.result_rows:
        domain, status, cnt = row
        stats.setdefault(domain, {})[status] = cnt
        total += cnt
    return {"domains": stats, "total": total}


# ─────────────────────────────────────────────────────────────────────────────
# Auto-collection from bot interactions
# ─────────────────────────────────────────────────────────────────────────────

def learn_from_lab_results(ch, biomarker: str, category: str = "",
                           unit: str = "", ref_range: str = "") -> None:
    """Auto-learn biomarker names from lab result uploads."""
    definition = f"Биомаркер ({category})" if category else "Биомаркер"
    if unit:
        definition += f", единица: {unit}"
    if ref_range:
        definition += f", норма: {ref_range}"

    add_term(
        ch, term=biomarker, domain="medical", category=category or "biomarker",
        definition=definition, source="lab_upload", status="verified",
        confidence=0.9,
    )


def learn_from_workout(ch, parsed: dict) -> None:
    """Auto-learn exercise names and terms from parsed workout data."""
    # Exercises
    for ex in parsed.get("exercises", []):
        name = ex.get("name", "")
        if name and len(name) > 2:
            variant = ex.get("variant", "")
            defn = f"Упражнение: {name}"
            if variant:
                defn += f" ({variant})"
            add_term(
                ch, term=name, domain="workout", category="exercise",
                definition=defn, source="workout_parse", status="verified",
                confidence=0.8,
            )

    # Unknown terms → candidates
    for ut in parsed.get("unknown_terms", []):
        if isinstance(ut, str) and len(ut) > 1:
            # Try to split "ABBR — guess?" format
            parts = re.split(r'\s*[—\-]\s*', ut, maxsplit=1)
            term = parts[0].strip()
            guess = parts[1].strip() if len(parts) > 1 else ""
            add_term(
                ch, term=term, domain="workout", category="unknown",
                definition=guess, source="workout_unknown", status="candidate",
                confidence=0.3,
            )

    # Muscle groups
    for mg in parsed.get("muscle_groups", []):
        add_term(
            ch, term=mg, domain="workout", category="muscle_group",
            definition=f"Группа мышц: {mg}", source="workout_parse",
            status="trusted", confidence=1.0,
        )


def learn_from_document(ch, doc_type: str, title: str, keywords: list[str] | None = None) -> None:
    """Auto-learn medical terms from uploaded documents."""
    if title and len(title) > 3:
        add_term(
            ch, term=title, domain="medical", category=doc_type,
            definition=f"Медицинский документ: {title}", source="doc_upload",
            status="candidate", confidence=0.5,
        )
    for kw in (keywords or []):
        if kw and len(kw) > 2:
            add_term(
                ch, term=kw, domain="medical", category="keyword",
                source="doc_upload", status="candidate", confidence=0.4,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Glossary context for LLM prompts
# ─────────────────────────────────────────────────────────────────────────────

def glossary_context_for_prompt(ch, domain: str | None = None,
                                max_terms: int = 50) -> str:
    """Build glossary context block for LLM prompt injection."""
    if domain:
        terms = get_domain_terms(ch, domain, min_status="verified", limit=max_terms)
    else:
        # Get verified+ terms from all domains
        result = ch.query(
            "SELECT term, domain, category, definition "
            "FROM glossary_terms FINAL "
            "WHERE status IN ('verified','trusted') "
            "ORDER BY usage_count DESC "
            f"LIMIT {max_terms}"
        )
        terms = [dict(zip(result.column_names, r)) for r in result.result_rows]

    if not terms:
        return ""

    lines = ["=== ГЛОССАРИЙ БОТА ==="]
    current_domain = ""
    for t in terms:
        d = t.get("domain", "")
        if d != current_domain:
            current_domain = d
            lines.append(f"\n[{d}]")
        defn = t.get("definition", "")
        lines.append(f"  {t['term']}: {defn}" if defn else f"  {t['term']}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Seed from workout_glossary.yaml (one-time migration)
# ─────────────────────────────────────────────────────────────────────────────

def seed_from_workout_glossary(ch) -> int:
    """Import workout_glossary.yaml into global glossary. Idempotent."""
    from pathlib import Path
    import yaml

    gpath = Path(__file__).resolve().parent / "workout_glossary.yaml"
    if not gpath.exists():
        return 0

    data = yaml.safe_load(gpath.read_text(encoding="utf-8")) or {}
    count = 0

    section_domain = {
        "effort_levels": ("workout", "effort"),
        "rep_modifiers": ("workout", "rep_modifier"),
        "grip_types": ("workout", "grip"),
        "exercise_abbrevs": ("workout", "exercise"),
        "equipment": ("workout", "equipment"),
        "set_notation": ("workout", "notation"),
        "muscle_groups": ("workout", "muscle_group"),
        "technique": ("workout", "technique"),
        "stance": ("workout", "stance"),
        "section_markers": ("workout", "section"),
        "days": ("general", "day"),
    }

    for section, items in data.items():
        if not isinstance(items, dict):
            continue
        domain, category = section_domain.get(section, ("general", section))
        for abbr, info in items.items():
            if isinstance(info, dict):
                defn = info.get("full", "")
                defn_en = info.get("en", "")
            elif isinstance(info, str):
                defn = info
                defn_en = ""
            else:
                continue
            add_term(
                ch, term=str(abbr), domain=domain, category=category,
                definition=defn, definition_en=defn_en,
                source="glossary_yaml", status="trusted", confidence=1.0,
            )
            count += 1

    log.info("Seeded %d terms from workout_glossary.yaml", count)
    return count
