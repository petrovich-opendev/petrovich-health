"""ClickHouse interface for health_analytics — multi-tenant by owner_id."""
from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime
from typing import Any

import clickhouse_connect
from dotenv import load_dotenv

load_dotenv()

import threading as _threading

_client = None
_client_lock = _threading.Lock()

# Telegram chat_id for a 1-to-1 user is a positive int64 (≤ 19 digits).
# We don't serve supergroups, so the leading-minus form is unnecessary —
# rejecting it removes one whole class of "owner_id looks valid but isn't
# a real user" edge cases (the synthetic demo seeder uses "999..." too).
# `^\d{1,19}$` is strict and matches Telegram's actual chat_id shape.
_OWNER_ID_RE = re.compile(r"^\d{1,19}$")


def get_client() -> clickhouse_connect.driver.Client:
    """Singleton CH client. Double-checked under a lock so concurrent
    init from the poll thread + reminder thread can't leak a second
    socket on first call."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = clickhouse_connect.get_client(
                host=os.getenv("CH_HOST", "localhost"),
                port=int(os.getenv("CH_PORT", "8123")),
                database=os.getenv("CH_DATABASE", "health_analytics"),
                username=os.getenv("CH_USER", "default"),
                password=os.getenv("CH_PASSWORD", ""),
                # Synchronous insert visibility: user saves data then immediately
                # asks LLM — the read must see the write. async_insert would hide
                # the row for ~1 sec, breaking the post-save analysis path.
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
    return _client


def _validate_owner_id(owner_id: Any) -> str:
    """Accept only digit-only strings (Telegram chat_id shape).

    Blocks SQL injection at the boundary into every _own() call-site.
    Raise — never silently sanitize — so the failure surfaces in logs.
    """
    s = str(owner_id)
    if not _OWNER_ID_RE.match(s):
        raise ValueError(f"Invalid owner_id shape: {owner_id!r}")
    return s


# ─── owner_id filter helper ─────────────────────────────────────────────────
def _own(owner_id: str) -> str:
    """WHERE clause fragment for owner isolation.

    owner_id is validated to digit-only before interpolation — safe from SQL
    injection even under f-string concatenation.
    """
    return f"owner_id = '{_validate_owner_id(owner_id)}'"


# ─── Inserts ─────────────────────────────────────────────────────────────────
def insert_lab_results(rows: list[dict], owner_id: str = "524605979") -> int:
    if not rows:
        return 0
    client = get_client()
    from dedup import filter_new_lab_results
    rows, skipped = filter_new_lab_results(client, rows, owner_id)
    if skipped:
        # caller already logs aggregate counts; this is the audit trail.
        import logging
        logging.getLogger("health-bot").info(
            "insert_lab_results: skipped %d duplicate biomarker rows", skipped
        )
    if not rows:
        return 0
    columns = [
        "id", "collected_at", "category", "biomarker", "biomarker_original",
        "value", "unit", "ref_low", "ref_high", "is_abnormal",
        "lab_name", "source_file", "raw_text", "notes", "owner_id",
    ]
    data = []
    for r in rows:
        val = float(r["value"])
        ref_low = r.get("ref_low")
        ref_high = r.get("ref_high")
        is_abnormal = False
        if ref_low is not None and val < ref_low:
            is_abnormal = True
        if ref_high is not None and val > ref_high:
            is_abnormal = True
        data.append([
            str(uuid.uuid4()), r["collected_at"], r.get("category", "other"),
            r["biomarker"], r.get("biomarker_original", r["biomarker"]),
            val, r.get("unit", ""), ref_low, ref_high, is_abnormal,
            r.get("lab_name", ""), r.get("source_file", ""),
            r.get("raw_text", ""), r.get("notes", ""), owner_id,
        ])
    client.insert("lab_results", data, column_names=columns)
    return len(data)


def insert_document(
    collected_at: date, doc_type: str, title: str, source_file: str,
    full_text: str, lab_name: str = "", summary: str = "",
    owner_id: str = "524605979",
) -> bool:
    """Insert a document. Returns False if a same-source_file row already exists."""
    client = get_client()
    from dedup import is_document_duplicate
    if is_document_duplicate(client, owner_id, source_file):
        import logging
        logging.getLogger("health-bot").info(
            "dedup_skip documents owner=%s source_file=%s", owner_id, source_file
        )
        return False
    client.insert("documents", [[
        str(uuid.uuid4()), datetime.now(), collected_at, doc_type, title,
        lab_name, source_file, full_text, summary, owner_id,
    ]], column_names=[
        "id", "uploaded_at", "collected_at", "doc_type", "title",
        "lab_name", "source_file", "full_text", "summary", "owner_id",
    ])
    return True


def insert_chat_message(role: str, text: str, message_id: int = 0,
                        owner_id: str = "524605979") -> bool:
    """Insert a chat row. Returns False only for re-insert of same user message_id."""
    client = get_client()
    from dedup import is_chat_message_duplicate
    if is_chat_message_duplicate(client, owner_id, role, message_id):
        import logging
        logging.getLogger("health-bot").info(
            "dedup_skip chat_log owner=%s message_id=%s", owner_id, message_id
        )
        return False
    client.insert("chat_log", [[
        datetime.now(), role, text, message_id, 0, owner_id,
    ]], column_names=["ts", "role", "text", "message_id", "tokens_used", "owner_id"])
    return True


def insert_upload_log(
    source_file: str, file_size: int, pages: int, biomarkers_extracted: int,
    lab_name: str, collected_at: date, status: str = "ok",
    error_message: str = "", raw_text: str = "",
    owner_id: str = "524605979",
) -> bool:
    """Log an upload attempt. Returns False if same file+date already ok-logged.

    Failed/partial uploads (status != 'ok') are always inserted — they record
    a retry attempt, which is useful debugging signal.
    """
    client = get_client()
    if status == "ok":
        from dedup import is_upload_log_duplicate
        if is_upload_log_duplicate(client, owner_id, source_file, collected_at):
            import logging
            logging.getLogger("health-bot").info(
                "dedup_skip upload_log owner=%s file=%s date=%s",
                owner_id, source_file, collected_at,
            )
            return False
    client.insert("upload_log", [[
        str(uuid.uuid4()), datetime.now(), source_file, file_size, pages,
        biomarkers_extracted, lab_name, collected_at, status,
        error_message, raw_text, owner_id,
    ]], column_names=[
        "id", "uploaded_at", "source_file", "file_size_bytes", "pages",
        "biomarkers_extracted", "lab_name", "collected_at", "status",
        "error_message", "raw_text", "owner_id",
    ])
    return True


# ─── Queries (all filtered by owner_id) ─────────────────────────────────────
def query_recent_chat(limit: int = 10, owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT ts, role, text FROM chat_log "
        f"WHERE {_own(owner_id)} ORDER BY ts DESC LIMIT {{lim:UInt32}}",
        parameters={"lim": limit},
    )
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return list(reversed(rows))


def query_biomarker_trend(biomarker: str, limit: int = 50,
                          owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, value, unit, ref_low, ref_high, is_abnormal, lab_name "
        f"FROM lab_results WHERE {_own(owner_id)} AND biomarker ILIKE {{bm:String}} "
        f"ORDER BY collected_at",
        parameters={"bm": f"%{biomarker}%"},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows[:limit]]


def query_latest_results(limit: int = 30, owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, "
        f"is_abnormal, lab_name FROM lab_results WHERE {_own(owner_id)} "
        f"ORDER BY collected_at DESC, biomarker LIMIT {{lim:UInt32}}",
        parameters={"lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_abnormal(limit: int = 50, owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, "
        f"lab_name FROM lab_results WHERE {_own(owner_id)} AND is_abnormal = true "
        f"ORDER BY collected_at DESC LIMIT {{lim:UInt32}}",
        parameters={"lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_all_biomarkers(owner_id: str = "524605979") -> list[str]:
    client = get_client()
    result = client.query(
        f"SELECT DISTINCT biomarker FROM lab_results WHERE {_own(owner_id)} ORDER BY biomarker"
    )
    return [row[0] for row in result.result_rows]


def query_summary_stats(owner_id: str = "524605979") -> dict:
    client = get_client()
    r = client.query(
        f"SELECT count(), min(collected_at), max(collected_at), uniqExact(biomarker), "
        f"uniqExact(source_file) FROM lab_results WHERE {_own(owner_id)}"
    )
    row = r.result_rows[0] if r.result_rows else (0, None, None, 0, 0)
    return {
        "total_records": row[0],
        "earliest_date": str(row[1]) if row[1] else "N/A",
        "latest_date": str(row[2]) if row[2] else "N/A",
        "unique_biomarkers": row[3],
        "unique_files": row[4],
    }


def query_fulltext_search(query: str, limit: int = 30,
                          owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, "
        f"is_abnormal, lab_name, source_file FROM lab_results "
        f"WHERE {_own(owner_id)} AND ("
        f"  positionCaseInsensitiveUTF8(biomarker, {{q:String}}) > 0 "
        f"  OR positionCaseInsensitiveUTF8(biomarker_original, {{q:String}}) > 0 "
        f"  OR positionCaseInsensitiveUTF8(raw_text, {{q:String}}) > 0) "
        f"ORDER BY collected_at DESC LIMIT {{lim:UInt32}}",
        parameters={"q": query, "lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_documents_search(query: str, limit: int = 10,
                           owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, doc_type, title, lab_name, "
        f"substring(full_text, "
        f"  greatest(1, positionCaseInsensitiveUTF8(full_text, {{q:String}}) - 100), "
        f"  300) as context_snippet "
        f"FROM documents WHERE {_own(owner_id)} AND ("
        f"  positionCaseInsensitiveUTF8(full_text, {{q:String}}) > 0 "
        f"  OR positionCaseInsensitiveUTF8(title, {{q:String}}) > 0) "
        f"ORDER BY collected_at DESC LIMIT {{lim:UInt32}}",
        parameters={"q": query, "lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_all_documents(limit: int = 20, owner_id: str = "524605979") -> list[dict]:
    client = get_client()
    result = client.query(
        f"SELECT collected_at, doc_type, title, lab_name, length(full_text) as text_len "
        f"FROM documents WHERE {_own(owner_id)} "
        f"ORDER BY collected_at DESC LIMIT {{lim:UInt32}}",
        parameters={"lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_spc_data(owner_id: str = "524605979") -> dict[str, list]:
    """Get biomarker time series for SPC (only those with ≥2 points)."""
    client = get_client()
    result = client.query(
        f"SELECT biomarker, collected_at, value, unit, ref_low, ref_high "
        f"FROM lab_results WHERE {_own(owner_id)} "
        f"AND biomarker IN ("
        f"  SELECT biomarker FROM lab_results WHERE {_own(owner_id)} "
        f"  GROUP BY biomarker HAVING count() >= 2"
        f") ORDER BY biomarker, collected_at"
    )
    from spc import SPCPoint
    series: dict[str, list] = {}
    for row in result.result_rows:
        bm = row[0]
        series.setdefault(bm, []).append(SPCPoint(
            date=row[1], value=row[2], unit=row[3],
            ref_low=row[4], ref_high=row[5],
        ))
    return series


def query_health_profile(owner_id: str = "524605979") -> str | None:
    client = get_client()
    result = client.query(
        f"SELECT date, profile_text, overall_status, key_findings, watchlist, "
        f"correlations, missing_data, alerts "
        f"FROM health_profile WHERE {_own(owner_id)} ORDER BY date DESC LIMIT 1"
    )
    if not result.result_rows:
        return None
    r = result.result_rows[0]
    parts = [f"Дата профиля: {r[0]}", f"Статус: {r[2]}", f"\n{r[1]}"]
    if r[3] and r[3] != "[]":
        parts.append(f"\nКлючевые находки: {r[3]}")
    if r[4] and r[4] != "[]":
        parts.append(f"\nПод наблюдением: {r[4]}")
    if r[5] and r[5] != "[]":
        parts.append(f"\nКорреляции: {r[5]}")
    if r[6] and r[6] != "[]":
        parts.append(f"\nНе хватает: {r[6]}")
    if r[7] and r[7] != "[]":
        parts.append(f"\nАлерты: {r[7]}")
    return "\n".join(parts)


def query_recent_digests(days: int = 7, owner_id: str = "524605979") -> str:
    client = get_client()
    result = client.query(
        f"SELECT date, digest, user_concerns, new_info FROM daily_digest "
        f"WHERE {_own(owner_id)} ORDER BY date DESC LIMIT {{d:UInt32}}",
        parameters={"d": days},
    )
    if not result.result_rows:
        return ""
    lines = []
    for r in result.result_rows:
        line = f"{r[0]}: {r[1]}"
        if r[2]:
            line += f" | Беспокоит: {r[2]}"
        if r[3]:
            line += f" | Новое: {r[3]}"
        lines.append(line)
    return "\n".join(lines)


def query_fresh_uploads(hours: int = 48, owner_id: str = "524605979",
                        max_biomarkers: int = 200, max_docs: int = 20) -> str:
    """Return biomarkers + documents uploaded within the last `hours`.
    This section is placed FIRST in the LLM context so just-saved data is never
    masked by an older health_profile snapshot. Hard caps on rows prevent
    prompt-bloat if user dumps many files at once.
    """
    client = get_client()
    fresh = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, "
        f"  is_abnormal, lab_name, source_file, uploaded_at "
        f"FROM lab_results FINAL WHERE {_own(owner_id)} "
        f"  AND uploaded_at >= now() - INTERVAL {{h:UInt32}} HOUR "
        f"ORDER BY uploaded_at DESC, biomarker "
        f"LIMIT {{lim:UInt32}}",
        parameters={"h": hours, "lim": max_biomarkers},
    )
    fresh_bio_lines = []
    for row in fresh.result_rows:
        flag = " [ВНЕ НОРМЫ]" if row[7] else ""
        ref = ""
        if row[5] is not None or row[6] is not None:
            ref = f" (норма: {row[5] or '?'}–{row[6] or '?'})"
        lab = f" | {row[8]}" if row[8] else ""
        fresh_bio_lines.append(
            f"  загружено {row[10]:%Y-%m-%d %H:%M} | замер {row[0]} | "
            f"{row[2]}: {row[3]} {row[4]}{ref}{flag}{lab}"
        )
    fresh_docs = client.query(
        f"SELECT uploaded_at, collected_at, doc_type, title "
        f"FROM documents WHERE {_own(owner_id)} "
        f"  AND uploaded_at >= now() - INTERVAL {{h:UInt32}} HOUR "
        f"ORDER BY uploaded_at DESC LIMIT {{lim:UInt32}}",
        parameters={"h": hours, "lim": max_docs},
    )
    fresh_doc_lines = []
    for row in fresh_docs.result_rows:
        fresh_doc_lines.append(
            f"  загружен {row[0]:%Y-%m-%d %H:%M} | дата {row[1]} | {row[2]} | {row[3]}"
        )
    if not fresh_bio_lines and not fresh_doc_lines:
        return ""
    parts = []
    if fresh_bio_lines:
        parts.append("Биомаркеры:\n" + "\n".join(fresh_bio_lines))
    if fresh_doc_lines:
        parts.append("Документы:\n" + "\n".join(fresh_doc_lines))
    return "\n\n".join(parts)


def query_for_llm_context(question: str, owner_id: str = "524605979") -> str:
    stats = query_summary_stats(owner_id)
    profile = query_health_profile(owner_id)
    digests = query_recent_digests(7, owner_id)
    fresh = query_fresh_uploads(48, owner_id)

    if stats["total_records"] == 0 and not profile:
        return "(Нет данных в базе. Загрузите анализы через PDF.)"

    client = get_client()

    # ── All results (LIMIT 500 to bound prompt size) ──
    latest = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, is_abnormal "
        f"FROM lab_results FINAL WHERE {_own(owner_id)} "
        f"ORDER BY collected_at DESC, biomarker LIMIT 500"
    )
    latest_lines = []
    for row in latest.result_rows:
        flag = " [!!!]" if row[7] else ""
        ref = ""
        if row[5] is not None or row[6] is not None:
            ref = f" (норма: {row[5] or '?'}–{row[6] or '?'})"
        latest_lines.append(f"  {row[0]} | {row[1]} | {row[2]}: {row[3]} {row[4]}{ref}{flag}")

    # ── Dynamics: biomarkers with ≥2 measurements, last 20 points per (biomarker, unit).
    # Grouping by (biomarker, unit) is critical: mixing mg/dl and mmol/l glucose
    # in one series would produce a nonsensical 1700%-jump trend line.
    dynamics = client.query(
        f"SELECT biomarker, unit, "
        f"  arraySlice(arrayReverseSort(x -> x.1, groupArray((collected_at, value))), 1, 20) as pts, "
        f"  any(ref_low) as ref_low, "
        f"  any(ref_high) as ref_high, "
        f"  count() as cnt "
        f"FROM lab_results FINAL WHERE {_own(owner_id)} "
        f"GROUP BY biomarker, unit HAVING cnt >= 2 "
        f"ORDER BY biomarker, unit"
    )
    dynamics_lines = []
    for row in dynamics.result_rows:
        bm, unit, pts, ref_low, ref_high, cnt = row
        # pts is array of (date, value) tuples — sorted DESC by date, top 20.
        # Python sort ASC for readable left-to-right trend.
        points = sorted(pts, key=lambda t: t[0])
        dates = [p[0] for p in points]
        vals = [p[1] for p in points]
        ref = ""
        if ref_low is not None or ref_high is not None:
            ref = f" (норма: {ref_low or '?'}–{ref_high or '?'})"

        trend_parts = [f"{d}={v}" for d, v in zip(dates, vals)]

        first_val, last_val = vals[0], vals[-1]
        diff = last_val - first_val
        pct = (diff / first_val * 100) if first_val != 0 else 0
        arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"

        dynamics_lines.append(
            f"  {bm} {unit}{ref}: {' → '.join(trend_parts)} "
            f"[{arrow} {diff:+.1f} ({pct:+.0f}%), {cnt} замеров]"
        )

    abnormal = client.query(
        f"SELECT collected_at, biomarker, value, unit, ref_low, ref_high "
        f"FROM lab_results FINAL WHERE {_own(owner_id)} AND is_abnormal = true "
        f"ORDER BY collected_at DESC LIMIT 50"
    )
    abnormal_lines = []
    for row in abnormal.result_rows:
        abnormal_lines.append(
            f"  {row[0]} | {row[1]}: {row[2]} {row[3]} (норма: {row[4] or '?'}–{row[5] or '?'})")

    docs = client.query(
        f"SELECT collected_at, doc_type, title, "
        f"substring(full_text, 1, 1500) as text_preview "
        f"FROM documents WHERE {_own(owner_id)} ORDER BY collected_at DESC LIMIT 10"
    )
    doc_lines = []
    for row in docs.result_rows:
        doc_lines.append(f"--- {row[2]} ({row[1]}, {row[0]}) ---\n{row[3]}")

    sections = []
    if fresh:
        sections.append(
            "=== СВЕЖИЕ ЗАГРУЗКИ (последние 48 часов) ===\n"
            "Эти данные загружены пользователем недавно. "
            "Если HEALTH PROFILE ниже противоречит им — профиль устарел, доверяй этим цифрам.\n\n"
            f"{fresh}")
    if profile:
        sections.append(f"=== HEALTH PROFILE (snapshot) ===\n{profile}")
    if digests:
        sections.append(f"=== ДАЙДЖЕСТЫ (7 дней) ===\n{digests}")

    sections.append(
        f"=== БАЗА ===\nЗаписей: {stats['total_records']}, "
        f"период: {stats['earliest_date']}—{stats['latest_date']}, "
        f"показателей: {stats['unique_biomarkers']}, файлов: {stats['unique_files']}")

    if dynamics_lines:
        sections.append(
            f"=== ДИНАМИКА (показатели с ≥2 замерами) ===\n"
            + chr(10).join(dynamics_lines))

    sections.append(f"=== ВСЕ РЕЗУЛЬТАТЫ ===\n{chr(10).join(latest_lines) or '(нет)'}")
    sections.append(f"=== ВНЕ НОРМЫ ===\n{chr(10).join(abnormal_lines) or '(в норме)'}")
    sections.append(f"=== ДОКУМЕНТЫ ===\n{chr(10).join(doc_lines) or '(нет)'}")

    return "\n\n".join(sections)


# ─── Training entries ──────────────────────────────────────────────────────
def insert_training_entry(
    owner_id: str,
    workout_date: date,
    entry_type: str,
    training_day: str,
    muscle_groups: list[str],
    cycle_number: int,
    cycle_weeks: int,
    body_weight_kg: float,
    raw_text: str,
    parsed_json: str,
    unknown_terms: list[str] | None = None,
    source: str = "text",
) -> str:
    """Insert a training entry. Returns entry ID, or "" if deduped."""
    client = get_client()
    from dedup import is_training_entry_duplicate
    if is_training_entry_duplicate(client, owner_id, workout_date, raw_text):
        import logging
        logging.getLogger("health-bot").info(
            "dedup_skip training_entries owner=%s date=%s text_prefix=%r",
            owner_id, workout_date, raw_text[:50],
        )
        return ""
    entry_id = str(uuid.uuid4())
    client.insert("training_entries", [[
        entry_id, owner_id, datetime.now(), workout_date, entry_type,
        training_day, muscle_groups, cycle_number, cycle_weeks,
        body_weight_kg, raw_text, parsed_json,
        unknown_terms or [], source,
    ]], column_names=[
        "id", "owner_id", "uploaded_at", "workout_date", "entry_type",
        "training_day", "muscle_groups", "cycle_number", "cycle_weeks",
        "body_weight_kg", "raw_text", "parsed_json",
        "unknown_terms", "source",
    ])
    return entry_id


def query_recent_workouts(limit: int = 10, owner_id: str = "524605979") -> list[dict]:
    """Get recent workout entries."""
    client = get_client()
    result = client.query(
        f"SELECT workout_date, training_day, muscle_groups, cycle_number, "
        f"body_weight_kg, parsed_json "
        f"FROM training_entries WHERE {_own(owner_id)} "
        f"ORDER BY workout_date DESC, uploaded_at DESC LIMIT {{lim:UInt32}}",
        parameters={"lim": limit},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]


def query_exercise_progress(exercise_name: str, owner_id: str = "524605979") -> list[dict]:
    """Search training entries for a specific exercise by name."""
    client = get_client()
    result = client.query(
        f"SELECT workout_date, training_day, cycle_number, parsed_json "
        f"FROM training_entries WHERE {_own(owner_id)} "
        f"AND positionCaseInsensitiveUTF8(parsed_json, {{q:String}}) > 0 "
        f"ORDER BY workout_date",
        parameters={"q": exercise_name},
    )
    return [dict(zip(result.column_names, row)) for row in result.result_rows]
