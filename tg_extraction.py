"""Lab extraction confirmation flow + text-input classification + lab-results processing."""
from __future__ import annotations

import logging
import re as _re
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone

from db import insert_lab_results, insert_upload_log
from extractor import extract_biomarkers, validate_results
from glossary import learn_from_lab_results
from tg_documents import _guess_doc_type, _save_as_document
from tg_transport import send_and_log, send_message, send_typing, tg_api
from tg_users import resolve_user
from workout import workout_score

MSK_TZ = timezone(timedelta(hours=3))

log = logging.getLogger("health-bot")

# ── Pending extractions awaiting user confirm (PDF / photo / text labs) ──
# Key: token (8-char uuid prefix). Value:
#   {"owner_id": str, "rows": [valid_rows], "warnings": [...],
#    "doc_meta": {"lab_name","collected_at","source_file","kind",
#                 "file_size","page_count","raw_text"},
#    "ts": datetime}
# Stored extractions never auto-insert; user must press ✅ Принять in TG.
_PENDING_EXTRACTIONS: dict[str, dict] = {}
_PENDING_EXTRACTION_TTL_SEC = 3600


def _prune_pending_extractions() -> None:
    """Drop entries older than _PENDING_EXTRACTION_TTL_SEC. Cheap, called inline."""
    now = datetime.now()
    expired = [
        tok for tok, e in _PENDING_EXTRACTIONS.items()
        if (now - e.get("ts", now)).total_seconds() > _PENDING_EXTRACTION_TTL_SEC
    ]
    for tok in expired:
        log.info("Pruning stale pending extraction token=%s owner=%s",
                 tok, _PENDING_EXTRACTIONS[tok].get("owner_id"))
        del _PENDING_EXTRACTIONS[tok]


def _stash_extraction(owner_id: str, rows: list[dict], warnings: list[str],
                      doc_meta: dict) -> str:
    """Save extraction in pending dict, return short token for callback_data.

    Token must fit Telegram's 64-byte callback_data limit alongside the
    "extract_accept:" prefix → 8-char uuid prefix is plenty.
    """
    _prune_pending_extractions()
    token = _uuid.uuid4().hex[:8]
    _PENDING_EXTRACTIONS[token] = {
        "owner_id": owner_id,
        "rows": rows,
        "warnings": warnings or [],
        "doc_meta": doc_meta,
        "ts": datetime.now(),
    }
    return token


def _format_extraction_preview(rows: list[dict], warnings: list[str],
                                doc_meta: dict) -> str:
    """Render preview shown before user confirms the insert.

    Highlights abnormal rows so the user can spot a hallucinated unit/value
    at a glance (the whole point of this gate).
    """
    lab_name = doc_meta.get("lab_name", "?")
    collected_at = doc_meta.get("collected_at", "?")
    n = len(rows)
    lines = [f"📋 <b>Извлечено {n} показателей</b> ({lab_name}, {collected_at})"]
    lines.append("")
    for r in rows[:25]:
        bm = r.get("biomarker", "?")
        val = r.get("value", "?")
        unit = r.get("unit", "")
        ref_low = r.get("ref_low")
        ref_high = r.get("ref_high")
        ref = ""
        if ref_low is not None or ref_high is not None:
            ref = f" ({ref_low if ref_low is not None else '?'}-"
            ref += f"{ref_high if ref_high is not None else '?'})"
        is_abnormal = False
        try:
            v = float(val)
            if ref_low is not None and v < ref_low:
                is_abnormal = True
            if ref_high is not None and v > ref_high:
                is_abnormal = True
        except (TypeError, ValueError):
            pass
        flag = " <b>[ABNORMAL]</b>" if is_abnormal else ""
        lines.append(f"  • {bm}: {val} {unit}{ref}{flag}")
    if n > 25:
        lines.append(f"  ... и ещё {n - 25} показателей")

    sc = doc_meta.get("self_check_discrepancies") or []
    if sc:
        lines.append("")
        lines.append(f"⚠️ <b>Self-check предупреждения ({len(sc)}):</b>")
        for d in sc[:5]:
            field = d.get("field", "?")
            extracted = str(d.get("extracted", ""))[:80]
            lines.append(f"  • {field}: {extracted}")

    if warnings:
        lines.append("")
        lines.append("⚠️ <b>Валидация:</b>")
        for w in warnings[:5]:
            lines.append(f"  • {w}")

    lines.append("")
    lines.append("Что делать с этими данными?")
    return "\n".join(lines)


def _extraction_keyboard(token: str) -> dict:
    """Inline keyboard with Accept / Edit / Cancel for a pending extraction."""
    return {
        "inline_keyboard": [[
            {"text": "✅ Принять", "callback_data": f"extract_accept:{token}"},
            {"text": "✏️ Изменить", "callback_data": f"extract_edit:{token}"},
            {"text": "❌ Отменить", "callback_data": f"extract_cancel:{token}"},
        ]]
    }


def _send_extraction_preview(chat_id: str, owner_id: str, rows: list[dict],
                              warnings: list[str], doc_meta: dict) -> None:
    """Stash pending extraction + send preview with inline keyboard."""
    token = _stash_extraction(owner_id, rows, warnings, doc_meta)
    text = _format_extraction_preview(rows, warnings, doc_meta)
    try:
        send_message(chat_id, text, reply_markup=_extraction_keyboard(token))
    except Exception as exc:
        log.error("Extraction preview send failed: %s", exc)
        send_message(chat_id, f"{text}\n\n(token={token})")


def _commit_pending_extraction(token: str) -> tuple[bool, str]:
    """Apply a previously-stashed extraction to ClickHouse.

    Returns (success, user_message). On success the entry is popped.
    Idempotent on missing token (already accepted/cancelled): returns False.
    """
    entry = _PENDING_EXTRACTIONS.pop(token, None)
    if entry is None:
        return False, ("Это подтверждение уже использовано или истекло "
                       "(данные не сохранены).")
    rows = entry.get("rows") or []
    owner_id = entry["owner_id"]
    doc_meta = entry.get("doc_meta") or {}
    if not rows:
        return False, "Нечего сохранять — список показателей пуст."

    try:
        count = insert_lab_results(rows, owner_id)
    except Exception as exc:
        log.error("insert_lab_results from pending failed: %s", exc)
        _PENDING_EXTRACTIONS[token] = entry
        return False, f"Ошибка вставки в БД: {exc}"

    try:
        insert_upload_log(
            doc_meta.get("source_file", ""),
            doc_meta.get("file_size", 0),
            doc_meta.get("page_count", 0),
            count,
            doc_meta.get("lab_name", ""),
            doc_meta.get("collected_at") or date.today(),
            "ok", "",
            (doc_meta.get("raw_text") or "")[:10000],
            owner_id=owner_id,
        )
    except Exception as exc:
        log.warning("upload_log insert from pending failed: %s", exc)

    try:
        from db import get_client as _gc
        _ch = _gc()
        for r in rows:
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f"{r.get('ref_low', '?')}-{r.get('ref_high', '?')}"
            learn_from_lab_results(_ch, r["biomarker"], r.get("category", ""),
                                   r.get("unit", ""), ref)
    except Exception as exc:
        log.warning("Glossary learn from pending lab failed: %s", exc)

    return True, f"✅ Сохранено {count} показателей"


def _handle_extraction_callback(callback: dict) -> None:
    """Route ``extract_*:<token>`` callback_data to accept/cancel/edit."""
    cb_id = callback.get("id", "")
    data = callback.get("data", "") or ""
    msg = callback.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    try:
        tg_api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", exc)

    user = resolve_user({
        "from": callback.get("from", {}),
        "chat": msg.get("chat", {}),
    })
    if not user:
        send_message(chat_id, "Доступ запрещён.")
        return
    owner_id = user["owner_id"]

    if ":" not in data:
        return
    action, _, token = data.partition(":")
    entry = _PENDING_EXTRACTIONS.get(token)

    if entry and entry.get("owner_id") != owner_id:
        # Cross-user token — should never happen with Telegram's binding,
        # but defend against a token leak / replay anyway.
        log.warning("Cross-user callback: token owner=%s, user=%s",
                    entry.get("owner_id"), owner_id)
        send_message(chat_id, "Токен не твой.")
        return

    if action == "extract_accept":
        ok, reply = _commit_pending_extraction(token)
        send_and_log(chat_id, reply, owner_id)
        return

    if action == "extract_cancel":
        if _PENDING_EXTRACTIONS.pop(token, None) is None:
            send_and_log(chat_id, "Это подтверждение уже истекло.", owner_id)
        else:
            send_and_log(chat_id, "❌ Отменено", owner_id)
        return

    if action == "extract_edit":
        send_and_log(
            chat_id,
            "✏️ Функция изменения в разработке.\n\n"
            "Пока — нажми ❌ Отменить и перезалей анализы или попроси "
            "поправить вручную в чате.",
            owner_id,
        )
        return

    log.warning("Unknown callback action: %s", action)


def _handle_medication_callback(callback: dict) -> None:
    """Route ``med_<status>:<reminder_uuid>`` callbacks from reminder buttons.

    Inserts a medication_events_v1 row mapped to the reminder's drug/dose
    so the /adherence PDC pipeline can score the take. The button itself
    is then cleared (editMessageReplyMarkup with empty markup) — without
    this the user could double-tap and over-count taken events.
    """
    from db import get_client
    from adherence import record_medication_event

    cb_id = callback.get("id", "")
    data = callback.get("data", "") or ""
    msg = callback.get("message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    msg_id = msg.get("message_id")
    if not chat_id:
        return

    try:
        tg_api("answerCallbackQuery", callback_query_id=cb_id)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", exc)

    user = resolve_user({
        "from": callback.get("from", {}),
        "chat": msg.get("chat", {}),
    })
    if not user:
        send_message(chat_id, "Доступ запрещён.")
        return
    owner_id = user["owner_id"]

    if ":" not in data:
        return
    action, _, reminder_id = data.partition(":")
    status_map = {
        "med_taken":   ("taken",   "✅ Записано: принял"),
        "med_skipped": ("skipped", "⏭️ Записано: пропустил"),
        "med_forgot":  ("forgot",  "🤔 Записано: забыл"),
    }
    if action not in status_map:
        log.warning("Unknown med callback action: %s", action)
        return
    status, ack = status_map[action]

    ch = get_client()
    rows = ch.query(
        "SELECT drug_name, drug_inn, dose, text FROM reminders WHERE id={i:UUID} "
        "AND owner_id={o:String} LIMIT 1",
        parameters={"i": reminder_id, "o": owner_id},
    ).result_rows
    if not rows:
        log.warning("med callback: reminder %s not found for owner %s",
                    reminder_id[:8], owner_id)
        send_and_log(chat_id, "Не нашёл это напоминание.", owner_id)
        return
    drug_name, drug_inn, dose, _ = rows[0]
    if not drug_name:
        send_and_log(chat_id, "У этого напоминания нет препарата.", owner_id)
        return

    try:
        record_medication_event(
            ch, owner_id, drug_name, drug_inn, dose,
            status=status, source="reminder_button", reminder_id=reminder_id,
        )
    except Exception as exc:
        log.error("record_medication_event failed: %s", exc)
        send_and_log(chat_id, f"Не смог записать: {exc}", owner_id)
        return

    if msg_id:
        try:
            tg_api("editMessageReplyMarkup", chat_id=chat_id,
                   message_id=msg_id, reply_markup={"inline_keyboard": []})
        except Exception as exc:
            log.warning("editMessageReplyMarkup failed: %s", exc)
    send_and_log(chat_id, ack, owner_id)


def _process_lab_results(raw_text: str, safe_name: str, file_name: str,
                         file_size: int, page_count: int,
                         lab_name: str, collected_at: date, chat_id: str,
                         owner_id: str = "524605979") -> str:
    """Extract and store numeric lab results."""
    send_typing(chat_id)
    try:
        extracted = extract_biomarkers(raw_text)
    except Exception as exc:
        log.error("LLM extraction failed: %s", exc)
        _save_as_document(safe_name, file_name, raw_text, collected_at, lab_name, owner_id=owner_id)
        insert_upload_log(safe_name, file_size, page_count, 0, lab_name, collected_at,
                          "error", str(exc), raw_text[:5000], owner_id=owner_id)
        return f"Ошибка извлечения, текст сохранён как документ: {exc}"

    valid_rows, warnings = validate_results(extracted)
    lab_name = extracted.get("lab_name") or lab_name or "unknown"
    if valid_rows:
        collected_at = valid_rows[0]["collected_at"]

    if not valid_rows:
        _save_as_document(safe_name, file_name, raw_text, collected_at, lab_name, owner_id=owner_id)
        insert_upload_log(safe_name, file_size, page_count, 0, lab_name, collected_at,
                          "document", "Classified as lab but no numeric values", raw_text[:10000], owner_id=owner_id)
        return (
            f"<b>Документ сохранён</b>\n"
            f"Файл: {file_name}\n"
            f"Числовых показателей не найдено, но текст сохранён для поиска."
        )

    for row in valid_rows:
        row["source_file"] = safe_name
        row["raw_text"] = raw_text[:10000]

    doc_meta = {
        "lab_name": lab_name, "collected_at": collected_at,
        "source_file": safe_name, "kind": "PDF",
        "file_size": file_size, "page_count": page_count,
        "raw_text": raw_text[:10000],
    }
    _send_extraction_preview(chat_id, owner_id, valid_rows, warnings, doc_meta)
    log.info("PDF preview sent for confirmation (%d rows, lab=%s, date=%s, owner=%s)",
             len(valid_rows), lab_name, collected_at, owner_id)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Text-input classification heuristics
# ─────────────────────────────────────────────────────────────────────────────
_LAB_UNITS_RE = _re.compile(
    r'(?:г/л|г/дл|мг/л|мг/дл|ммоль/л|мкмоль/л|нмоль/л|пмоль/л|мкг/л|нг/мл|пг/мл|'
    r'мЕд/мл|Ед/л|Ед/мл|МЕ/мл|МЕ/л|мМЕ/л|мкМЕ/мл|тыс/мкл|млн/мкл|фл|пг|%|‰|'
    r'г/дл|mg/dl|mg/l|mmol/l|umol/l|nmol/l|ng/ml|pg/ml|u/l|iu/ml|miu/l|g/l|'
    r'10\^9/л|10\^12/л|\*10\^|×10\^|10\*9|10\*12|сек|мм/ч|кл/мкл)',
    _re.IGNORECASE,
)

_LAB_KEYWORDS_RE = _re.compile(
    r'(?:гемоглобин|эритроцит|лейкоцит|тромбоцит|глюкоз|холестерин|билирубин|'
    r'креатинин|мочевин|АЛТ|АСТ|ГГТ|щелочная|ферритин|железо|трансферрин|'
    r'тиреотроп|Т3|Т4|ТТГ|тестостерон|кортизол|инсулин|витамин|фолиев|'
    r'СОЭ|СРБ|фибриноген|протромбин|АЧТВ|МНО|антител|иммуноглобулин|'
    r'hemoglobin|glucose|cholesterol|creatinin|bilirubin|ALT|AST|TSH|'
    r'результат|референс|норма|показатель|исследование|анализ|биохимич|'
    r'клинический.*анализ|общий.*анализ|гематолог|коагулограмм)',
    _re.IGNORECASE,
)


_MEDICAL_DOC_RE = _re.compile(
    r'(?:заключение|диагноз|рекомендац|назначен|осмотр|жалоб|анамнез|'
    r'пациент|обследован|выявлен|патологи|'
    r'ээг|электроэнцефалограф|холтер|мониторирование|'
    r'мрт|узи|ультразвук|экг|электрокардиограмм|'
    r'рентген|флюорограф|томограф|эндоскоп|'
    r'биопси|гистолог|цитолог)',
    _re.IGNORECASE,
)


def medical_score(text: str) -> int:
    """Calculate medical data likelihood score."""
    units_found = len(_LAB_UNITS_RE.findall(text))
    keywords_found = len(_LAB_KEYWORDS_RE.findall(text))
    medical_found = len(_MEDICAL_DOC_RE.findall(text))
    numbers_found = len(_re.findall(r'\d+[.,]\d+', text))
    return units_found * 3 + keywords_found * 2 + medical_found * 2 + numbers_found


def looks_like_medical_data(text: str) -> bool:
    """Heuristic: is this pasted medical data (lab results or document) or a question?"""
    if len(text) < 150:
        return False
    return medical_score(text) >= 6


def classify_input_type(text: str) -> str:
    """Classify long text input: 'medical', 'workout', or 'question'.

    Uses scoring from both domains — highest score wins.
    Minimum threshold to avoid false positives.
    """
    if len(text) < 80:
        return "question"

    med = medical_score(text)
    wrk = workout_score(text)

    log.info("Input classification: medical=%d, workout=%d (%d chars)", med, wrk, len(text))

    if med < 6 and wrk < 8:
        return "question"

    if wrk > med:
        return "workout"
    return "medical"


def process_text_lab_data(text: str, chat_id: str, owner_id: str = "524605979") -> str:
    """Process pasted text as lab results: extract → validate → store."""
    log.info("Processing text as lab data (%d chars)", len(text))
    send_typing(chat_id)

    try:
        extracted = extract_biomarkers(text)
    except Exception as exc:
        log.error("LLM extraction from text failed: %s", exc)
        return f"Не удалось извлечь показатели из текста: {exc}"

    valid_rows, warnings = validate_results(extracted)
    lab_name = extracted.get("lab_name") or "text_input"
    collected_at = valid_rows[0]["collected_at"] if valid_rows else date.today()

    source_name = f"text_{datetime.now(MSK_TZ).strftime('%Y%m%d_%H%M%S')}.txt"

    if not valid_rows:
        doc_collected = date.today()
        if extracted.get("collected_at"):
            try:
                doc_collected = date.fromisoformat(extracted["collected_at"])
            except (ValueError, TypeError):
                pass
        doc_type = _guess_doc_type(source_name, text)
        _save_as_document(source_name, f"text_input_{doc_type}", text, doc_collected, lab_name, doc_type, owner_id)
        insert_upload_log(source_name, len(text), 0, 0, lab_name, doc_collected,
                          "document", "Saved as text document", text[:10000], owner_id=owner_id)
        log.info("Text saved as document type=%s", doc_type)
        return (
            f"<b>Текст сохранён как документ</b>\n"
            f"Тип: {doc_type}\n"
            f"Дата: {doc_collected}\n"
            f"Символов: {len(text)}\n\n"
            f"Числовых показателей не найдено. "
            f"Полный текст доступен для поиска и вопросов."
        )

    for row in valid_rows:
        row["source_file"] = source_name
        row["raw_text"] = text[:10000]

    doc_meta = {
        "lab_name": lab_name, "collected_at": collected_at,
        "source_file": source_name, "kind": "text",
        "file_size": len(text), "page_count": 1,
        "raw_text": text[:10000],
    }
    _send_extraction_preview(chat_id, owner_id, valid_rows, warnings, doc_meta)
    log.info("Text-paste preview sent for confirmation (%d rows, owner=%s)",
             len(valid_rows), owner_id)
    return ""
