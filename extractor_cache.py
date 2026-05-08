"""SHA-256-keyed cache for biomarker extraction.

Re-uploads of the same PDF (cross-device sharing, retesting the bot) hit
``extract_biomarkers`` repeatedly, each time burning ~30s and Opus 4.7
tokens. We hash the post-pdfplumber raw text and short-circuit on hits.

Backing store: ``data/extractor_cache.json``. Atomic writes via tmp+rename
to survive concurrent processes (the bot heartbeat + ad-hoc scripts).

TTL: 90 days. Stale entries are still returned, but logged so the caller
can decide. Extraction is deterministic enough that a stale hit is almost
always still correct — the prompt is the only thing that changes, and a
prompt change is the operator's decision to invalidate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("health-bot")

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_PATH = DATA_DIR / "extractor_cache.json"

TTL_DAYS = 90


def hash_text(text: str) -> str:
    """SHA-256 of the FULL extracted text (pre-truncation).

    Hashing the full text — not the 8000-char prompt slice — means cache
    keys remain stable even if we later widen/narrow the prompt window.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("extractor_cache: malformed root, resetting")
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("extractor_cache: read failed (%s), resetting", exc)
        return {}


def _atomic_write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".extractor_cache.", suffix=".tmp", dir=str(DATA_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, CACHE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def cache_get(text_hash: str) -> dict | None:
    """Return cached extraction result for ``text_hash`` or None.

    Stale entries (>TTL_DAYS old) are still returned but logged as
    ``stale-cache-hit``.
    """
    data = _load()
    entry = data.get(text_hash)
    if not entry:
        return None
    result = entry.get("result")
    if not isinstance(result, dict):
        return None
    ts_raw = entry.get("timestamp")
    try:
        ts = datetime.fromisoformat(ts_raw) if ts_raw else None
    except (TypeError, ValueError):
        ts = None
    if ts is not None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        if age > timedelta(days=TTL_DAYS):
            log.info(
                "extractor_cache: stale-cache-hit hash=%s age_days=%d",
                text_hash[:12], age.days,
            )
    return result


def cache_put(text_hash: str, result: dict) -> None:
    """Persist ``result`` keyed by ``text_hash``. Atomic."""
    data = _load()
    data[text_hash] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    try:
        _atomic_write(data)
    except OSError as exc:
        log.warning("extractor_cache: write failed (%s)", exc)
