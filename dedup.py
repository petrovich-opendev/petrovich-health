"""Pre-insert duplicate guard for CH-backed health-bot tables.

The CH engine itself (ReplacingMergeTree on lab_results / daily_digest /
health_profile / glossary_terms) silently collapses duplicates on merge,
but that is async and value-only — it cannot tell us *before* we insert
that a row already exists, and it does nothing for MergeTree tables
(documents / upload_log / chat_log / training_entries / body_log).

This module sits in front of every insert_* helper in db.py and returns
True when the row should be considered a duplicate. Callers decide
whether to skip+log (the default) or overwrite.

Logical keys (chosen after the 2026-05-11 audit):

  lab_results        (owner_id, biomarker, collected_at, value)
  documents          (owner_id, source_file)
  upload_log         (owner_id, source_file, collected_at)
  chat_log           (owner_id, message_id) if user-role and message_id>0,
                     else SKIP dedup (bot replies share message_id=0)
  training_entries   (owner_id, workout_date, substring(raw_text,1,200))

These are intentionally narrow: false-positive duplicates discard real
data, so we prefer (rare) false-negatives. The substring/owner anchors
match the way the live ingest pipeline writes each row.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

log = logging.getLogger("health-bot")


def _to_iso(d: Any) -> str:
    """Render a date/datetime/str as YYYY-MM-DD (date) or full ISO (datetime).

    Used to format values for SQL string interpolation. Numeric/None passes
    through to str(); the SQL parameterizer in clickhouse-connect handles
    proper quoting for the typed parameters we pass below.
    """
    if isinstance(d, datetime):
        return d.isoformat(sep=" ")
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def is_lab_result_duplicate(ch, owner_id: str, biomarker: str,
                            collected_at: date, value: float) -> bool:
    """True if this exact (biomarker, date, value) is already in lab_results.

    Same key as the ReplacingMergeTree ORDER BY — so engine merge would
    collapse it anyway, but blocking the insert is faster than waiting
    for the next background merge and keeps query counts accurate.
    """
    r = ch.query(
        "SELECT count() FROM lab_results WHERE owner_id={o:String} "
        "AND biomarker={b:String} AND collected_at={d:Date} AND value={v:Float64}",
        parameters={"o": owner_id, "b": biomarker,
                    "d": _to_iso(collected_at), "v": float(value)},
    ).result_rows[0][0]
    return r > 0


def is_document_duplicate(ch, owner_id: str, source_file: str) -> bool:
    """True if a document with this source_file is already stored for owner.

    source_file is the file's user-visible name (rarely empty in practice);
    we ignore the random uuid id since the same physical PDF may be
    reuploaded with a different generated id.
    """
    if not source_file:
        return False
    r = ch.query(
        "SELECT count() FROM documents WHERE owner_id={o:String} "
        "AND source_file={f:String}",
        parameters={"o": owner_id, "f": source_file},
    ).result_rows[0][0]
    return r > 0


def is_upload_log_duplicate(ch, owner_id: str, source_file: str,
                            collected_at: date | None) -> bool:
    """True if this (source_file, collected_at) is already logged successfully.

    Only ``status='ok'`` rows count — failed/partial uploads shouldn't
    block a retry. ``collected_at`` is part of the key because the same
    file name can legitimately recur across different collection dates.
    """
    if not source_file:
        return False
    params: dict[str, Any] = {"o": owner_id, "f": source_file}
    extra = ""
    if collected_at is not None:
        extra = "AND collected_at={d:Date} "
        params["d"] = _to_iso(collected_at)
    r = ch.query(
        f"SELECT count() FROM upload_log WHERE owner_id={{o:String}} "
        f"AND source_file={{f:String}} {extra}AND status='ok'",
        parameters=params,
    ).result_rows[0][0]
    return r > 0


def is_chat_message_duplicate(ch, owner_id: str, role: str,
                              message_id: int) -> bool:
    """True only for user-role messages with a real Telegram message_id.

    Bot replies are not deduped here — they share message_id=0 and the
    bot may legitimately send the same canned reply twice (e.g., two
    distinct user prompts both returning "Извини, не понял").
    """
    if role != "user" or not message_id:
        return False
    r = ch.query(
        "SELECT count() FROM chat_log WHERE owner_id={o:String} "
        "AND role='user' AND message_id={m:UInt64}",
        parameters={"o": owner_id, "m": int(message_id)},
    ).result_rows[0][0]
    return r > 0


def is_training_entry_duplicate(ch, owner_id: str, workout_date: date,
                                raw_text: str) -> bool:
    """True if a training entry for the same date+raw_text prefix exists.

    raw_text is what the user typed; first 200 chars are enough to
    distinguish two distinct workout descriptions while tolerating
    trailing chat noise the parser may have included differently.
    """
    if not raw_text:
        return False
    r = ch.query(
        "SELECT count() FROM training_entries WHERE owner_id={o:String} "
        "AND workout_date={d:Date} "
        "AND substring(raw_text, 1, 200) = substring({t:String}, 1, 200)",
        parameters={"o": owner_id, "d": _to_iso(workout_date), "t": raw_text},
    ).result_rows[0][0]
    return r > 0


def filter_new_lab_results(ch, rows: list[dict], owner_id: str) -> tuple[list[dict], int]:
    """Drop rows already present in lab_results.

    Returns (kept_rows, skipped_count). Logs a single line per skip so
    a flood of dups (e.g., re-running an extract on the same PDF) is
    visible in the bot log without spamming.
    """
    kept: list[dict] = []
    skipped = 0
    for r in rows:
        try:
            val = float(r["value"])
        except (KeyError, ValueError, TypeError):
            kept.append(r)
            continue
        if is_lab_result_duplicate(ch, owner_id, r["biomarker"],
                                    r["collected_at"], val):
            log.info("dedup_skip lab_results owner=%s biomarker=%s date=%s value=%s",
                     owner_id, r["biomarker"], r["collected_at"], val)
            skipped += 1
            continue
        kept.append(r)
    return kept, skipped


__all__ = [
    "is_lab_result_duplicate",
    "is_document_duplicate",
    "is_upload_log_duplicate",
    "is_chat_message_duplicate",
    "is_training_entry_duplicate",
    "filter_new_lab_results",
]
