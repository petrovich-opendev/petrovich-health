"""ClickHouse interface for health_analytics — multi-tenant by owner_id."""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime
from typing import Any

import clickhouse_connect
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client() -> clickhouse_connect.driver.Client:
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=os.getenv("CH_HOST", "localhost"),
            port=int(os.getenv("CH_PORT", "8123")),
            database=os.getenv("CH_DATABASE", "health_analytics"),
            username=os.getenv("CH_USER", "default"),
            password=os.getenv("CH_PASSWORD", ""),
        )
    return _client


# ─── owner_id filter helper ─────────────────────────────────────────────────
def _own(owner_id: str) -> str:
    """WHERE clause fragment for owner isolation."""
    return f"owner_id = '{owner_id}'"


# ─── Inserts ─────────────────────────────────────────────────────────────────
def insert_lab_results(rows: list[dict], owner_id: str = "524605979") -> int:
    if not rows:
        return 0
    client = get_client()
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
) -> None:
    client = get_client()
    client.insert("documents", [[
        str(uuid.uuid4()), datetime.now(), collected_at, doc_type, title,
        lab_name, source_file, full_text, summary, owner_id,
    ]], column_names=[
        "id", "uploaded_at", "collected_at", "doc_type", "title",
        "lab_name", "source_file", "full_text", "summary", "owner_id",
    ])


def insert_chat_message(role: str, text: str, message_id: int = 0,
                        owner_id: str = "524605979") -> None:
    client = get_client()
    client.insert("chat_log", [[
        datetime.now(), role, text, message_id, 0, owner_id,
    ]], column_names=["ts", "role", "text", "message_id", "tokens_used", "owner_id"])


def insert_upload_log(
    source_file: str, file_size: int, pages: int, biomarkers_extracted: int,
    lab_name: str, collected_at: date, status: str = "ok",
    error_message: str = "", raw_text: str = "",
    owner_id: str = "524605979",
) -> None:
    client = get_client()
    client.insert("upload_log", [[
        str(uuid.uuid4()), datetime.now(), source_file, file_size, pages,
        biomarkers_extracted, lab_name, collected_at, status,
        error_message, raw_text, owner_id,
    ]], column_names=[
        "id", "uploaded_at", "source_file", "file_size_bytes", "pages",
        "biomarkers_extracted", "lab_name", "collected_at", "status",
        "error_message", "raw_text", "owner_id",
    ])


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


def query_for_llm_context(question: str, owner_id: str = "524605979") -> str:
    stats = query_summary_stats(owner_id)
    profile = query_health_profile(owner_id)
    digests = query_recent_digests(7, owner_id)

    if stats["total_records"] == 0 and not profile:
        return "(Нет данных в базе. Загрузите анализы через PDF.)"

    client = get_client()

    # ── All results (no hard limit — user has real history) ──
    latest = client.query(
        f"SELECT collected_at, category, biomarker, value, unit, ref_low, ref_high, is_abnormal "
        f"FROM lab_results WHERE {_own(owner_id)} "
        f"ORDER BY collected_at DESC, biomarker LIMIT 500"
    )
    latest_lines = []
    for row in latest.result_rows:
        flag = " [!!!]" if row[7] else ""
        ref = ""
        if row[5] is not None or row[6] is not None:
            ref = f" (норма: {row[5] or '?'}–{row[6] or '?'})"
        latest_lines.append(f"  {row[0]} | {row[1]} | {row[2]}: {row[3]} {row[4]}{ref}{flag}")

    # ── Dynamics: biomarkers with ≥2 measurements, show trend ──
    dynamics = client.query(
        f"SELECT biomarker, "
        f"  groupArray(collected_at) as dates, "
        f"  groupArray(value) as vals, "
        f"  groupArray(unit) as units, "
        f"  any(ref_low) as ref_low, "
        f"  any(ref_high) as ref_high, "
        f"  count() as cnt "
        f"FROM lab_results WHERE {_own(owner_id)} "
        f"GROUP BY biomarker HAVING cnt >= 2 "
        f"ORDER BY biomarker"
    )
    dynamics_lines = []
    for row in dynamics.result_rows:
        bm, dates, vals, units, ref_low, ref_high, cnt = row
        unit = units[0] if units else ""
        ref = ""
        if ref_low is not None or ref_high is not None:
            ref = f" (норма: {ref_low or '?'}–{ref_high or '?'})"

        # Build trend line: date1=val1 → date2=val2 → ...
        points = sorted(zip(dates, vals))
        trend_parts = [f"{d}={v}" for d, v in points]

        # Direction
        first_val, last_val = points[0][1], points[-1][1]
        diff = last_val - first_val
        pct = (diff / first_val * 100) if first_val != 0 else 0
        arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"

        dynamics_lines.append(
            f"  {bm} {unit}{ref}: {' → '.join(trend_parts)} "
            f"[{arrow} {diff:+.1f} ({pct:+.0f}%), {cnt} замеров]"
        )

    abnormal = client.query(
        f"SELECT collected_at, biomarker, value, unit, ref_low, ref_high "
        f"FROM lab_results WHERE {_own(owner_id)} AND is_abnormal = true "
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
    if profile:
        sections.append(f"=== HEALTH PROFILE ===\n{profile}")
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
    """Insert a training entry. Returns entry ID."""
    client = get_client()
    entry_id = str(uuid.uuid4())
    client.insert("training_entries", [[
        entry_id, owner_id, datetime.now(), workout_date, entry_type,
        training_day, muscle_groups, cycle_number, cycle_weeks,
        body_weight_kg, raw_text, parsed_json,
        unknown_terms or [], source,
    ]], column_names=[
        "id", "owner_id", "created_at", "workout_date", "entry_type",
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
        f"ORDER BY workout_date DESC, created_at DESC LIMIT {{lim:UInt32}}",
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
