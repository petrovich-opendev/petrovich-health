"""Per-owner concurrency guard + daily cost log for `claude -p` invocations.

Background: a user dropping 5 PDFs in a row used to trigger
~15× concurrent Opus 4.7 calls (vision OCR + extraction + post-save
analysis × 5 files). No rate limit, no debouncing — token budget evaporates.

This module funnels every user-facing `claude -p` call through a
per-owner `threading.Lock`: at most ONE inflight LLM call per user.
Background pipelines (digests, self-check) still bypass it — they
don't queue on user actions.

Daily cost log lives at logs/claude-cost-YYYY-MM-DD.log. One line per
call: ISO timestamp + owner + model + duration_ms + prompt_chars +
output_chars + ok|fail. Cheap to grep for "who burned the budget today".
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("health-bot")

_LOGS_DIR = Path(__file__).resolve().parent / "logs"

_owner_locks: dict[str, threading.Lock] = {}
_owner_locks_master = threading.Lock()


def _lock_for(owner_id: str) -> threading.Lock:
    """Lazy-create one Lock per owner. The master lock protects the dict
    itself — getting/creating a per-owner lock is rare enough that this
    extra acquire is invisible."""
    lock = _owner_locks.get(owner_id)
    if lock is not None:
        return lock
    with _owner_locks_master:
        lock = _owner_locks.get(owner_id)
        if lock is None:
            lock = threading.Lock()
            _owner_locks[owner_id] = lock
    return lock


def _log_cost(owner_id: str, model: str, duration_ms: int,
              prompt_chars: int, output_chars: int, status: str) -> None:
    """Append one CSV-ish line to today's cost log. Best-effort — a
    failed cost log must never block the actual LLM call."""
    try:
        _LOGS_DIR.mkdir(exist_ok=True)
        fname = _LOGS_DIR / f"claude-cost-{datetime.now().strftime('%Y-%m-%d')}.log"
        line = (
            f"{datetime.now().isoformat()}\t{owner_id}\t{model}\t"
            f"{duration_ms}\t{prompt_chars}\t{output_chars}\t{status}\n"
        )
        with fname.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        log.warning("claude cost log failed: %s", exc)


def run_claude(prompt: str, model: str, owner_id: str,
               timeout: int = 120, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run `claude -p --model <model> <extra_args> <prompt>` with the
    per-owner inflight guard and a cost-log entry.

    Returns the CompletedProcess directly (callers parse stdout / check
    returncode as they did before). Timeout/Exception propagate; we
    still log the cost entry with status=fail before re-raising.
    """
    lock = _lock_for(owner_id)
    cmd = ["claude", "-p", "--model", model]
    if extra_args:
        cmd.extend(extra_args)
    # `--` ends option parsing — without it, a prompt starting with "-" or
    # "--flag" gets interpreted as a CLI flag by claude. User-controlled PDF
    # text reaches here, so this matters even though we use list-form
    # subprocess.run (no shell). Keep this at the very end of cmd.
    cmd.extend(["--", prompt])

    start = time.time()
    status = "fail"
    output_chars = 0
    try:
        with lock:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output_chars = len(result.stdout or "")
        status = "ok" if result.returncode == 0 else f"rc={result.returncode}"
        return result
    finally:
        duration_ms = int((time.time() - start) * 1000)
        _log_cost(owner_id, model, duration_ms, len(prompt), output_chars, status)
