#!/usr/bin/env python3
"""
Health Analytics Telegram Bot — @Petrovich_bio_bot

Long-running listener: accepts PDF lab results, stores in ClickHouse,
answers health questions with LLM + full biomarker history.

Only responds to OWNER_CHAT_ID — everyone else is silently ignored.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import requests
from dotenv import load_dotenv

from pdf_parser import extract_text
from extractor import classify_document, extract_biomarkers, validate_results
from db import (
    insert_lab_results,
    insert_upload_log,
    insert_document,
    insert_chat_message,
    query_recent_chat,
    query_biomarker_trend,
    query_latest_results,
    query_abnormal,
    query_all_biomarkers,
    query_summary_stats,
    query_for_llm_context,
    query_fulltext_search,
    query_documents_search,
    query_all_documents,
    query_spc_data,
)
from spc import compute_xmr, format_spc_report
from charts import render_trend_chart
from report_pdf import generate_report
from nutrition import (
    calc_bmr, calc_tdee, calc_macros, calc_weekly_rate,
    format_goal_summary,
)
from workout import (
    looks_like_workout_data, workout_score,
    process_workout_data,
)
from glossary import (
    learn_from_lab_results, learn_from_workout,
    learn_from_document, glossary_context_for_prompt,
)
from reminders import (
    get_reminders, get_all_reminders, add_reminder, delete_reminder,
    format_reminders_list, check_due_reminders, mark_sent,
    parse_reminder_command, looks_like_reminder, parse_natural_reminder,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
LOGS_DIR = PROJECT_DIR / "logs"

MSK_TZ = timezone(timedelta(hours=3))
POLL_TIMEOUT_SEC = 30
ERROR_BACKOFF_SEC = 10
MAX_MSG_LEN = 3800
LLM_PRIMARY = "claude-opus-4-6"
LLM_FALLBACK = "claude-sonnet-4-6"
LLM_TIMEOUT = 120

load_dotenv(PROJECT_DIR / ".env")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
USERS_YAML_PATH = PROJECT_DIR / "users.yaml"

# ─── User management ────────────────────────────────────────────────────────
import yaml as _yaml

def load_users() -> dict:
    """Load users.yaml → {username_lower: {name, role, chat_id}}."""
    if not USERS_YAML_PATH.exists():
        return {}
    data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
    users = {}
    for u in data.get("users", []):
        uname = u.get("username", "").lower().lstrip("@")
        if uname:
            users[uname] = {
                "name": u.get("name", uname),
                "role": u.get("role", "user"),
                "chat_id": str(u.get("chat_id", "")),
            }
    return users


def resolve_user(message: dict) -> dict | None:
    """Check if message sender is authorized. Returns user dict with owner_id or None."""
    users = load_users()
    from_user = message.get("from", {})
    username = (from_user.get("username") or "").lower()
    chat_id = str(message.get("chat", {}).get("id", ""))

    # Check by username
    if username in users:
        user = users[username]
        # Auto-save chat_id on first contact
        if not user["chat_id"] and chat_id:
            _save_chat_id(username, chat_id)
        return {"owner_id": chat_id, "name": user["name"], "role": user["role"]}

    # Check by chat_id (fallback for existing data)
    for uname, u in users.items():
        if u["chat_id"] == chat_id:
            return {"owner_id": chat_id, "name": u["name"], "role": u["role"]}

    return None


def _save_chat_id(username: str, chat_id: str) -> None:
    """Auto-save resolved chat_id back to users.yaml."""
    try:
        data = _yaml.safe_load(USERS_YAML_PATH.read_text(encoding="utf-8"))
        for u in data.get("users", []):
            if u.get("username", "").lower().lstrip("@") == username:
                u["chat_id"] = int(chat_id)
                break
        USERS_YAML_PATH.write_text(
            _yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        logging.getLogger("health-bot").info("Saved chat_id=%s for @%s", chat_id, username)
    except Exception as exc:
        logging.getLogger("health-bot").warning("Failed to save chat_id: %s", exc)

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ── Pending actions per user (dialog state) ──
# Key: owner_id, Value: {"action": "feedback|goal_type|goal_weight|...", "data": {}}
_pending: dict[str, dict] = {}


def setup_logging() -> logging.Logger:
    log_file = LOGS_DIR / f"bot-{datetime.now(MSK_TZ).strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )
    return logging.getLogger("health-bot")


log = setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────
def tg_api(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=kwargs, timeout=POLL_TIMEOUT_SEC + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def get_updates(offset: int | None = None) -> list[dict]:
    params: dict = {"timeout": POLL_TIMEOUT_SEC, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return tg_api("getUpdates", **params).get("result", [])


def send_message(chat_id: str, text: str) -> None:
    for chunk in split_message(text):
        tg_api("sendMessage", chat_id=chat_id, text=chunk,
               parse_mode="HTML", disable_web_page_preview=True)


def send_photo(chat_id: str, photo_bytes: bytes, caption: str = "") -> None:
    """Send a photo (PNG bytes) to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    resp = requests.post(url, data=data, files=files, timeout=30)
    if resp.status_code != 200:
        log.error("sendPhoto failed: %s", resp.text[:200])


def send_document_file(chat_id: str, doc_bytes: bytes, filename: str, caption: str = "") -> None:
    """Send a document file to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    files = {"document": (filename, doc_bytes, "application/pdf")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        data["parse_mode"] = "HTML"
    resp = requests.post(url, data=data, files=files, timeout=30)
    if resp.status_code != 200:
        log.error("sendDocument failed: %s", resp.text[:200])


def download_photo(file_id: str, dest_path: Path) -> None:
    """Download a photo from Telegram."""
    file_info = tg_api("getFile", file_id=file_id)
    file_path = file_info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)


def send_typing(chat_id: str) -> None:
    try:
        tg_api("sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass


def split_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        addition = block if not current else "\n\n" + block
        if len(current) + len(addition) > limit:
            if current:
                parts.append(current)
            current = block
        else:
            current += addition
    if current:
        parts.append(current)
    return parts or [text[:limit]]


def download_file(file_id: str, dest_path: Path) -> None:
    """Download a file from Telegram to local disk."""
    file_info = tg_api("getFile", file_id=file_id)
    file_path = file_info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    dest_path.write_bytes(resp.content)


# ─────────────────────────────────────────────────────────────────────────────
# PDF processing pipeline
# ─────────────────────────────────────────────────────────────────────────────
def _guess_doc_type(file_name: str, text: str) -> str:
    """Guess medical document type from filename and content."""
    combined = (file_name + " " + text[:500]).lower()
    if any(k in combined for k in ("ээг", "электроэнцефалограф", "eeg")):
        return "eeg"
    if any(k in combined for k in ("холтер", "holter", "мониторирование ритма")):
        return "holter"
    if any(k in combined for k in ("мрт", "mri", "магнитно-резонанс")):
        return "mri"
    if any(k in combined for k in ("узи", "ультразвук", "эхо", "ultrasound")):
        return "ultrasound"
    if any(k in combined for k in ("экг", "электрокардиограмм", "ecg", "ekg")):
        return "ecg"
    if any(k in combined for k in ("рентген", "x-ray", "флюорограф", "кт ", "компьютерная томограф")):
        return "xray_ct"
    if any(k in combined for k in ("консультация", "осмотр", "заключение врача", "приём")):
        return "consultation"
    if any(k in combined for k in ("выписка", "эпикриз", "история болезни")):
        return "discharge"
    return "other"


def _save_as_document(
    source_file: str, title: str, raw_text: str,
    collected_at: date, lab_name: str = "", doc_type: str = "other",
    owner_id: str = "524605979",
) -> None:
    """Save full text as a medical document in CH."""
    insert_document(
        collected_at=collected_at,
        doc_type=doc_type,
        title=title,
        source_file=source_file,
        full_text=raw_text,
        lab_name=lab_name,
        owner_id=owner_id,
    )


def process_pdf(file_id: str, file_name: str, chat_id: str, owner_id: str = "524605979") -> str:
    """Full pipeline: download → parse → classify → extract/store → report."""
    timestamp = datetime.now(MSK_TZ).strftime("%Y%m%d_%H%M%S")
    safe_name = file_name.replace("/", "_").replace(" ", "_")
    local_path = DATA_DIR / f"{timestamp}_{safe_name}"

    # 1. Download
    log.info("Downloading PDF: %s → %s", file_name, local_path)
    download_file(file_id, local_path)
    file_size = local_path.stat().st_size
    log.info("Downloaded %d bytes", file_size)

    # 2. Extract text
    try:
        raw_text, page_count = extract_text(local_path)
    except Exception as exc:
        log.error("PDF parse failed: %s", exc)
        insert_upload_log(safe_name, file_size, 0, 0, "", date.today(), "error", str(exc), owner_id=owner_id)
        return f"Ошибка чтения PDF: {exc}"

    if not raw_text.strip():
        insert_upload_log(safe_name, file_size, page_count, 0, "", date.today(), "error",
                          "Empty text after extraction", owner_id=owner_id)
        return (
            "PDF не содержит извлекаемого текста. "
            "Возможно это скан — попробуй сделать фото анализов и отправить как изображение."
        )

    log.info("Extracted %d chars from %d pages", len(raw_text), page_count)

    # 3. Classify document FIRST (Haiku, cheap)
    send_typing(chat_id)
    classification = classify_document(raw_text)
    doc_class = classification.get("doc_class", "other")
    doc_type = classification.get("doc_type", "other")
    doc_title = classification.get("title") or file_name
    doc_summary = classification.get("summary", "")
    doc_lab = classification.get("lab_name") or ""
    doc_date = date.today()
    if classification.get("collected_at"):
        try:
            doc_date = date.fromisoformat(classification["collected_at"])
        except (ValueError, TypeError):
            pass

    log.info("Classified: class=%s type=%s title=%s", doc_class, doc_type, doc_title)

    # 4. Route based on classification
    if doc_class == "lab_results":
        return _process_lab_results(raw_text, safe_name, file_name, file_size,
                                    page_count, doc_lab, doc_date, chat_id, owner_id)

    # Everything else — save as document with smart response
    _save_as_document(safe_name, doc_title, raw_text, doc_date, doc_lab, doc_type, owner_id)
    insert_upload_log(safe_name, file_size, page_count, 0, doc_lab, doc_date,
                      "document", f"class={doc_class}", raw_text[:10000], owner_id=owner_id)
    log.info("Saved as document: class=%s type=%s", doc_class, doc_type)

    # Build response based on doc class
    class_labels = {
        "estimate": "План/смета анализов",
        "prescription": "Назначение врача",
        "referral": "Направление",
        "consultation": "Заключение врача",
        "research": "Результат исследования",
        "certificate": "Справка/выписка",
        "other": "Медицинский документ",
    }
    label = class_labels.get(doc_class, doc_class)

    report = (
        f"<b>{label}</b>\n"
        f"Файл: {file_name}\n"
    )
    if doc_lab:
        report += f"Клиника: {doc_lab}\n"
    if doc_date != date.today():
        report += f"Дата: {doc_date}\n"
    report += f"Текст: {len(raw_text)} символов\n"
    if doc_summary:
        report += f"\n{doc_summary}\n"
    report += "\nСохранено. Доступно для поиска и вопросов."

    return report


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

    count = insert_lab_results(valid_rows, owner_id)
    insert_upload_log(safe_name, file_size, page_count, count, lab_name, collected_at,
                      "ok", "", raw_text[:10000], owner_id=owner_id)
    log.info("Stored %d biomarkers from %s (lab=%s, date=%s, owner=%s)", count, safe_name, lab_name, collected_at, owner_id)

    # Global glossary: learn biomarker names
    try:
        from db import get_client as _gc
        _ch = _gc()
        for r in valid_rows:
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f"{r.get('ref_low', '?')}–{r.get('ref_high', '?')}"
            learn_from_lab_results(_ch, r["biomarker"], r.get("category", ""), r.get("unit", ""), ref)
    except Exception as exc:
        log.warning("Glossary learn from lab failed: %s", exc)

    abnormal = [r for r in valid_rows
                if (r.get("ref_low") is not None and r["value"] < r["ref_low"])
                or (r.get("ref_high") is not None and r["value"] > r["ref_high"])]

    report_lines = [
        f"<b>Анализы: {file_name}</b>",
        f"Лаборатория: {lab_name}",
        f"Дата: {collected_at}",
        f"Показателей: <b>{count}</b>",
    ]

    if abnormal:
        report_lines.append(f"\n<b>Вне нормы ({len(abnormal)}):</b>")
        for r in abnormal:
            ref = f"норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')}"
            report_lines.append(f"  🔴 {r['biomarker']}: <b>{r['value']}</b> {r['unit']} ({ref})")
    else:
        report_lines.append("\nВсе показатели в норме.")

    if warnings:
        report_lines.append(f"\nПредупреждения:")
        for w in warnings[:5]:
            report_lines.append(f"  ⚠️ {w}")

    return "\n".join(report_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Text lab results processing
# ─────────────────────────────────────────────────────────────────────────────
import re as _re

# Units commonly found in lab results
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

    # Both below threshold — it's a question
    if med < 6 and wrk < 8:
        return "question"

    # Winner takes all
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
        # No numeric biomarkers — save as medical document
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

    count = insert_lab_results(valid_rows, owner_id)
    insert_upload_log(source_name, len(text), 1, count, lab_name, collected_at,
                      "ok", "", text[:10000], owner_id=owner_id)
    log.info("Stored %d biomarkers from text input (lab=%s, date=%s, owner=%s)", count, lab_name, collected_at, owner_id)

    abnormal = [r for r in valid_rows
                if (r.get("ref_low") is not None and r["value"] < r["ref_low"])
                or (r.get("ref_high") is not None and r["value"] > r["ref_high"])]

    report_lines = [
        f"<b>Текст обработан</b>",
        f"Лаборатория: {lab_name}",
        f"Дата: {collected_at}",
        f"Показателей: <b>{count}</b>",
    ]

    if abnormal:
        report_lines.append(f"\n<b>Вне нормы ({len(abnormal)}):</b>")
        for r in abnormal:
            ref = f"норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')}"
            report_lines.append(f"  🔴 {r['biomarker']}: <b>{r['value']}</b> {r['unit']} ({ref})")
    else:
        report_lines.append("\nВсе показатели в норме.")

    if warnings:
        report_lines.append(f"\nПредупреждения:")
        for w in warnings[:5]:
            report_lines.append(f"  ⚠️ {w}")

    return "\n".join(report_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────
def handle_command(text: str, owner_id: str = "524605979") -> str | None:
    """Handle known commands. Returns response or None."""
    cmd = text.strip().lower()
    parts = text.strip().split(maxsplit=1)
    cmd_name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd_name in ("/start", "/help"):
        return (
            "🏥 <b>Health Analytics Bot</b>\n\n"
            "Отправь PDF, фото или текст анализов — я извлеку показатели и сохраню.\n\n"
            "<b>Команды:</b>\n"
            "/last — последние результаты\n"
            "/trend &lt;показатель&gt; — тренд (напр. /trend гемоглобин)\n"
            "/search &lt;текст&gt; — полнотекстовый поиск по всем анализам\n"
            "/abnormal — показатели вне нормы\n"
            "/biomarkers — список всех показателей в базе\n"
            "/spc — SPC-анализ (контрольные карты биомаркеров)\n"
            "/correlations — корреляции по системам органов\n"
            "/remind — напоминания о приёме препаратов\n"
            "/report — PDF-отчёт для врача\n"
            "/stats — статистика базы\n"
            "/summary — общая оценка здоровья (LLM)\n"
            "/train — последние тренировки\n"
            "/progress &lt;упражнение&gt; — прогресс (напр. /progress подтягивания)\n\n"
            "<b>Ввод данных:</b>\n"
            "📎 PDF файл — парсинг и сохранение\n"
            "📸 Фото анализов — OCR распознавание\n"
            "📝 Текст анализов — автораспознавание\n"
            "🏋️ Текст тренировки — автораспознавание\n\n"
            "Любой другой текст — вопрос о здоровье.\n\n"
            "💬 Нашёл баг или есть идея? /feedback твоё сообщение"
        )

    if cmd_name == "/last":
        rows = query_latest_results(30, owner_id)
        if not rows:
            return "В базе пока нет анализов. Отправь PDF."
        current_date = None
        lines = ["<b>Последние результаты:</b>"]
        for r in rows:
            d = str(r["collected_at"])
            if d != current_date:
                current_date = d
                lines.append(f"\n<b>{d}</b> ({r.get('lab_name', '')})")
            flag = " 🔴" if r.get("is_abnormal") else ""
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
            lines.append(f"  {r['biomarker']}: <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        return "\n".join(lines)

    if cmd_name == "/trend":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "trend"}
            return "📈 Какой показатель посмотреть? Например: <i>гемоглобин</i>, <i>АЛТ</i>, <i>железо</i>"
        return ("__trend__", arg, owner_id)

    if cmd_name == "/abnormal":
        rows = query_abnormal(owner_id=owner_id)
        if not rows:
            return "Все показатели в норме! 🎉"
        lines = ["<b>Показатели вне нормы:</b>\n"]
        for r in rows:
            ref = f"норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')}"
            lines.append(f"🔴 {r['collected_at']} | {r['biomarker']}: <b>{r['value']}</b> {r['unit']} ({ref})")
        return "\n".join(lines)

    if cmd_name == "/biomarkers":
        markers = query_all_biomarkers(owner_id)
        if not markers:
            return "В базе пока нет показателей."
        return "<b>Показатели в базе:</b>\n\n" + "\n".join(f"  • {m}" for m in markers)

    if cmd_name == "/stats":
        s = query_summary_stats(owner_id)
        return (
            f"<b>Статистика базы</b>\n\n"
            f"Записей: {s['total_records']}\n"
            f"Период: {s['earliest_date']} — {s['latest_date']}\n"
            f"Уникальных показателей: {s['unique_biomarkers']}\n"
            f"Файлов загружено: {s['unique_files']}"
        )

    if cmd_name == "/search":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "search"}
            return "🔍 Что ищем? Напиши название показателя, препарата или диагноза"
        # Search in lab results
        rows = query_fulltext_search(arg, owner_id=owner_id)
        # Search in documents
        doc_rows = query_documents_search(arg, owner_id=owner_id)
        if not rows and not doc_rows:
            return f"По запросу '{arg}' ничего не найдено."
        lines = [f"<b>Поиск: {arg}</b>\n"]
        if rows:
            lines.append(f"<b>Анализы ({len(rows)}):</b>")
            for r in rows:
                flag = " 🔴" if r.get("is_abnormal") else ""
                ref = ""
                if r.get("ref_low") is not None or r.get("ref_high") is not None:
                    ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
                lines.append(f"  {r['collected_at']} | {r['biomarker']}: <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        if doc_rows:
            lines.append(f"\n<b>Документы ({len(doc_rows)}):</b>")
            for d in doc_rows:
                snippet = d.get("context_snippet", "")[:150].replace("\n", " ")
                lines.append(f"  {d['collected_at']} | {d['doc_type']} | {d['title']}\n    ...{snippet}...")
        return "\n".join(lines)

    if cmd_name == "/goal":
        _pending[owner_id] = {"ts": time.time(), "action": "goal_type"}
        return (
            "🎯 <b>Какая у тебя цель?</b>\n\n"
            "1️⃣ Набор мышечной массы\n"
            "2️⃣ Снижение жира\n"
            "3️⃣ Рекомпозиция (и то и то)\n"
            "4️⃣ Выносливость\n"
            "5️⃣ Долголетие\n"
            "6️⃣ Общее здоровье"
        )
        # flow continues in _handle_pending
        parts_goal = arg.split()
        if len(parts_goal) < 5:
            return "Нужно 5 параметров: тип вес рост возраст активность"
        try:
            goal_type = parts_goal[0]
            weight = float(parts_goal[1])
            height = float(parts_goal[2])
            age = int(parts_goal[3])
            activity = parts_goal[4]
            bmr = calc_bmr(weight, height, age)
            tdee = calc_tdee(bmr, activity)
            macros = calc_macros(weight, tdee, goal_type, on_trt=True)
            rate = calc_weekly_rate(goal_type, weight)
            # Save to CH
            from db import get_client
            import uuid
            ch = get_client()
            ch.insert("goals", [[
                str(uuid.uuid4()), owner_id, None, True,
                goal_type, "", None, None, weight, height, age, "male", activity,
                round(bmr), round(tdee), macros["target_calories"],
                macros["protein_g"], macros["fat_g"], macros["carbs_g"],
                macros["leucine_daily_g"], "[]",
            ]], column_names=[
                "id", "owner_id", "created_at", "active",
                "goal_type", "description", "target_weight_kg", "target_date",
                "current_weight_kg", "height_cm", "age", "sex", "activity_level",
                "bmr", "tdee", "target_calories",
                "protein_g", "fat_g", "carbs_g", "leucine_target_g", "medications",
            ])
            return format_goal_summary(goal_type, weight, height, age, activity, bmr, tdee, macros, rate)
        except (ValueError, IndexError) as exc:
            return f"Ошибка: {exc}. Формат: /goal muscle_gain 87 183 44 active"

    if cmd_name == "/weight":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "weight"}
            return "⚖️ Сколько весишь сегодня?"
        try:
            w = float(arg.replace(",", ".").replace("кг", "").strip())
            from db import get_client
            ch = get_client()
            from datetime import datetime
            ch.insert("body_log", [[
                owner_id, datetime.now(), w, None, "",
            ]], column_names=["owner_id", "ts", "weight_kg", "body_fat_pct", "notes"])
            recent = ch.query(
                f"SELECT ts, weight_kg FROM body_log WHERE owner_id = '{owner_id}' "
                f"ORDER BY ts DESC LIMIT 5"
            )
            if len(recent.result_rows) >= 2:
                prev = recent.result_rows[1][1]
                diff = w - prev
                icon = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
                return f"⚖️ Вес: <b>{w} кг</b> ({icon} {diff:+.1f} кг)\n\n💬 Спроси <i>подробнее</i> для динамики"
            return f"⚖️ Вес: <b>{w} кг</b> — записано ✅"
        except ValueError:
            return "⚖️ Не понял. Напиши просто число, например: 87.5"

    if cmd_name == "/eat":
        if not arg:
            _pending[owner_id] = {"ts": time.time(), "action": "eat"}
            return (
                "🍽 <b>Что ты ел?</b>\n\n"
                "📸 Отправь фото тарелки\n"
                "✍️ Или напиши текстом, например:\n"
                "<i>куриная грудка 200г, рис 150г, огурец</i>"
            )
        return ("__eat__", arg, owner_id)

    if cmd_name == "/week":
        return ("__week__", owner_id)

    if cmd_name == "/spc":
        series = query_spc_data(owner_id)
        if not series:
            return "Недостаточно данных для SPC (нужно ≥2 измерения одного показателя). Загрузи ещё анализов."
        results = []
        for bm, points in series.items():
            r = compute_xmr(bm, points)
            if r:
                results.append(r)
        results.sort(key=lambda x: len(x.alerts), reverse=True)
        return format_spc_report(results)

    if cmd_name == "/docs":
        docs = query_all_documents(owner_id=owner_id)
        if not docs:
            return "Нет сохранённых документов."
        lines = ["<b>Медицинские документы:</b>\n"]
        for d in docs:
            lines.append(f"  {d['collected_at']} | {d['doc_type']} | {d['title']} ({d['text_len']} символов)")
        return "\n".join(lines)

    if cmd_name == "/report":
        return ("__report__", owner_id)

    if cmd_name == "/correlations":
        return ("__correlations__", owner_id)

    if cmd_name == "/train":
        from db import query_recent_workouts
        rows = query_recent_workouts(10, owner_id)
        if not rows:
            return "Нет записей о тренировках. Скопируй текст тренировки из заметок — я распознаю."
        lines = ["<b>Последние тренировки:</b>\n"]
        for r in rows:
            muscles = ", ".join(r.get("muscle_groups", []))
            bw = f" | {r['body_weight_kg']} кг" if r.get("body_weight_kg") else ""
            cycle = f" | Цикл #{r['cycle_number']}" if r.get("cycle_number") else ""
            lines.append(
                f"  {r['workout_date']} | <b>{r.get('training_day', '?')}</b>"
                f"{cycle}{bw}"
            )
            # Show exercise count from parsed_json
            try:
                import json as _json
                pj = _json.loads(r.get("parsed_json", "{}"))
                ex_count = len(pj.get("exercises", []))
                if ex_count:
                    lines[-1] += f" | {ex_count} упражнений"
            except Exception:
                pass
        return "\n".join(lines)

    if cmd_name == "/progress":
        if not arg:
            return ("Укажи упражнение: <code>/progress подтягивания</code>\n"
                    "или <code>/progress тяга</code>")
        from db import query_exercise_progress
        rows = query_exercise_progress(arg, owner_id)
        if not rows:
            return f"'{arg}' не найдено в тренировках."
        lines = [f"<b>Прогресс: {arg}</b>\n"]
        for r in rows:
            try:
                import json as _json
                pj = _json.loads(r.get("parsed_json", "{}"))
                for ex in pj.get("exercises", []):
                    if arg.lower() in ex.get("name", "").lower():
                        for w in ex.get("weeks", []):
                            week_num = w.get("week", "?")
                            sets_info = []
                            for s in w.get("sets", []):
                                wt = s.get("weight_kg", "")
                                reps = s.get("reps", "")
                                eff = s.get("effort", "")
                                info = ""
                                if wt:
                                    info += f"{wt}кг"
                                if reps:
                                    info += f"x{reps}"
                                if eff:
                                    info += f" [{eff}]"
                                if info:
                                    sets_info.append(info)
                            if sets_info:
                                lines.append(
                                    f"  Цикл#{r.get('cycle_number', '?')} "
                                    f"Нед.{week_num}: {'; '.join(sets_info)}"
                                )
            except Exception:
                pass
        if len(lines) == 1:
            lines.append("  Данные найдены, но без числовой прогрессии")
        return "\n".join(lines)

    if cmd_name == "/glossary":
        from glossary import glossary_stats, search_glossary
        from db import get_client
        ch = get_client()
        if arg:
            results = search_glossary(ch, arg)
            if not results:
                return f"'{arg}' не найдено в глоссарии."
            lines = [f"<b>Глоссарий: {arg}</b>\n"]
            for r in results:
                status_icon = {"trusted": "", "verified": "", "candidate": ""}
                icon = status_icon.get(r.get("status", ""), "")
                lines.append(f"  {icon} <b>{r['term']}</b> [{r['domain']}] — {r.get('definition', '')}")
            return "\n".join(lines)
        stats = glossary_stats(ch)
        lines = [f"<b>Глоссарий бота</b> ({stats['total']} терминов)\n"]
        for domain, statuses in stats.get("domains", {}).items():
            total = sum(statuses.values())
            trusted = statuses.get("trusted", 0)
            verified = statuses.get("verified", 0)
            lines.append(f"  <b>{domain}</b>: {total} (trusted: {trusted}, verified: {verified})")
        lines.append(f"\nПоиск: <code>/glossary слово</code>")
        return "\n".join(lines)

    if cmd_name == "/summary":
        return None  # handled via LLM path

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Protocol knowledge base
# ─────────────────────────────────────────────────────────────────────────────
_PROTOCOLS_CACHE: str | None = None

def _load_protocols() -> str:
    """Load protocols.yaml as text for LLM context (cached)."""
    global _PROTOCOLS_CACHE
    if _PROTOCOLS_CACHE is None:
        p = PROJECT_DIR / "protocols.yaml"
        if p.exists():
            _PROTOCOLS_CACHE = p.read_text(encoding="utf-8")
        else:
            _PROTOCOLS_CACHE = ""
    return _PROTOCOLS_CACHE


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


# ─────────────────────────────────────────────────────────────────────────────
# LLM Q&A
# ─────────────────────────────────────────────────────────────────────────────
def ask_llm(question: str, owner_id: str = "524605979") -> str:
    now_msk = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    ch_context = query_for_llm_context(question, owner_id)

    # L0: recent chat history for conversation continuity
    recent_chat = query_recent_chat(10, owner_id)
    chat_block = ""
    if recent_chat:
        chat_lines = []
        for msg in recent_chat:
            ts = msg["ts"].strftime("%H:%M") if hasattr(msg["ts"], "strftime") else str(msg["ts"])[-8:-3]
            role_label = "Ты" if msg["role"] == "bot" else "Пользователь"
            chat_lines.append(f"[{ts}] {role_label}: {msg['text'][:300]}")
        chat_block = "\n=== НЕДАВНИЙ ДИАЛОГ ===\n" + "\n".join(chat_lines)

    prompt = f"""Ты — персональный медицинский ассистент-диагност. У тебя есть полная история анализов и медицинских документов пользователя, а также история вашего общения.

ПРАВИЛА:
1. Отвечай на русском, кратко и по делу
2. Ссылайся на КОНКРЕТНЫЕ показатели и даты из данных ниже
3. При обсуждении отклонений — указывай значение, норму и дату
4. Называй возможные причины отклонений, вероятные состояния, что стоит проверить дополнительно. НИКОГДА не пиши "рекомендую обсудить с врачом" или "обратитесь к специалисту" — это бесполезный шум. Давай прямые выводы.
5. Если данных недостаточно — честно скажи чего не хватает и что сдать
6. Выделяй тренды: улучшение/ухудшение/стабильность
7. Ищи cross-domain корреляции: связи между разными показателями и документами
8. Помни контекст предыдущих сообщений — пользователь может продолжать тему
9. Если вопрос не связан со здоровьем/анализами — вежливо скажи что ты только по здоровью
10. КОНФИДЕНЦИАЛЬНОСТЬ: НИКОГДА не раскрывай технические детали — какая модель используется, где хранятся данные, на каком сервере работаешь, какие технологии внутри (ClickHouse, Claude, Opus и т.д.). Если спрашивают — отвечай просто "я медицинский ассистент". Не упоминай JSON, промпты, базы данных, LLM.
11. ИЗОЛЯЦИЯ ДАННЫХ: ты видишь ТОЛЬКО данные текущего пользователя. Если спрашивают про данные других людей, других пользователей бота, семьи — отвечай "я вижу только твои персональные данные". НИКОГДА не упоминай что есть другие пользователи.

Время: {now_msk}

{ch_context}
{chat_block}
{_build_protocol_context(question)}
{_build_glossary_context()}
=== ВОПРОС ===
{question}

ФОРМАТ ОТВЕТА:
- КРАТКО: 2-5 предложений
- Используй эмодзи для структуры: 🔬 анализы, 💊 препараты, ⚠️ предупреждения, 📈 тренды, 💡 рекомендации, ✅ норма, 🔴 отклонение
- Разделяй блоки пустыми строками для удобства чтения с телефона
- Ключевые цифры выделяй <b>жирным</b>
- В конце: "💬 Скажи <i>подробнее</i> для развёрнутого ответа" — если тема позволяет"""

    for model in [LLM_PRIMARY, LLM_FALLBACK]:
        try:
            log.info("LLM Q&A model=%s question='%s'", model, question[:80])
            result = subprocess.run(
                ["claude", "-p", "--model", model, prompt],
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT,
            )
            if result.returncode != 0:
                log.warning("claude CLI failed (rc=%d) model=%s", result.returncode, model)
                continue
            answer = result.stdout.strip()
            if answer:
                log.info("LLM answer (%d chars) model=%s", len(answer), model)
                return answer
        except subprocess.TimeoutExpired:
            log.warning("LLM timeout model=%s", model)
        except Exception as exc:
            log.warning("LLM error model=%s: %s", model, exc)

    return "Не удалось получить ответ. Попробуй через пару минут."


# ─────────────────────────────────────────────────────────────────────────────
# Message processing
# ─────────────────────────────────────────────────────────────────────────────
def _handle_pending(chat_id: str, owner_id: str, text: str, user: dict) -> bool:
    """Handle reply to a pending dialog. Returns True if handled."""
    pending = _pending.get(owner_id)
    if not pending:
        return False

    action = pending.get("action", "")

    # ── Feedback ──
    if action == "feedback":
        del _pending[owner_id]
        try:
            resp = requests.post(
                "https://api.github.com/repos/petrovich-opendev/petrovich-health/issues",
                headers={
                    "Authorization": f"token {os.getenv('GITHUB_TOKEN', '')}",
                    "Content-Type": "application/json",
                },
                json={
                    "title": text[:100],
                    "body": f"**From:** {user['name']}\n\n{text}",
                    "labels": ["feedback"],
                },
                timeout=10,
            )
            if resp.status_code == 201:
                url = resp.json().get("html_url", "")
                send_message(chat_id, f"✅ Спасибо! Отзыв отправлен.")
            else:
                send_message(chat_id, "⚠️ Не удалось отправить. Напиши @Petrovoch_mobile")
        except Exception:
            send_message(chat_id, "⚠️ Ошибка. Напиши @Petrovoch_mobile")
        return True

    # ── Goal: step 1 — type ──
    if action == "goal_type":
        goal_map = {"1": "muscle_gain", "2": "fat_loss", "3": "recomp",
                     "4": "endurance", "5": "longevity", "6": "health"}
        # Also accept Russian text
        text_map = {
            "набор": "muscle_gain", "масса": "muscle_gain", "массу": "muscle_gain",
            "похуд": "fat_loss", "сушк": "fat_loss", "жир": "fat_loss", "сброс": "fat_loss",
            "рекомп": "recomp",
            "выносл": "endurance", "кардио": "endurance",
            "долголет": "longevity",
            "здоров": "health",
        }
        goal_type = goal_map.get(text.strip())
        if not goal_type:
            t = text.strip().lower()
            for key, val in text_map.items():
                if key in t:
                    goal_type = val
                    break
        if not goal_type:
            send_message(chat_id, "🤔 Не понял. Напиши цифру 1-6 или опиши цель")
            return True
        _pending[owner_id] = {"ts": time.time(), "action": "goal_weight", "data": {"goal_type": goal_type}}
        goal_labels = {"muscle_gain": "Набор массы", "fat_loss": "Снижение жира",
                       "recomp": "Рекомпозиция", "endurance": "Выносливость",
                       "longevity": "Долголетие", "health": "Здоровье"}
        send_message(chat_id, f"✅ Цель: <b>{goal_labels.get(goal_type, goal_type)}</b>\n\n⚖️ Сколько весишь? (кг)")
        return True

    # ── Goal: step 2 — weight ──
    if action == "goal_weight":
        try:
            w = float(text.replace(",", ".").replace("кг", "").strip())
            pending["data"]["weight"] = w
            pending["action"], pending["ts"] = "goal_height", time.time()
            send_message(chat_id, f"✅ Вес: <b>{w} кг</b>\n\n📏 Рост? (см)")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 87")
            return True

    # ── Goal: step 3 — height ──
    if action == "goal_height":
        try:
            h = float(text.replace(",", ".").replace("см", "").strip())
            pending["data"]["height"] = h
            pending["action"], pending["ts"] = "goal_age", time.time()
            send_message(chat_id, f"✅ Рост: <b>{h} см</b>\n\n🎂 Возраст?")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 183")
            return True

    # ── Goal: step 4 — age ──
    if action == "goal_age":
        try:
            age = int(text.replace("лет", "").replace("год", "").strip())
            pending["data"]["age"] = age
            pending["action"], pending["ts"] = "goal_activity", time.time()
            send_message(chat_id,
                f"✅ Возраст: <b>{age}</b>\n\n"
                f"🏃 Уровень активности?\n\n"
                f"1️⃣ Сидячий (офис, мало движения)\n"
                f"2️⃣ Лёгкий (1-2 тренировки в неделю)\n"
                f"3️⃣ Средний (3-4 тренировки)\n"
                f"4️⃣ Высокий (5-6 тренировок)\n"
                f"5️⃣ Очень высокий (ежедневно + физическая работа)")
            return True
        except ValueError:
            send_message(chat_id, "🤔 Напиши число, например: 44")
            return True

    # ── Goal: step 5 — activity → calculate ──
    if action == "goal_activity":
        act_map = {"1": "sedentary", "2": "light", "3": "moderate", "4": "active", "5": "very_active"}
        act_text = {"сидяч": "sedentary", "офис": "sedentary", "лёг": "light", "легк": "light",
                    "средн": "moderate", "умерен": "moderate", "высок": "active", "интенс": "active",
                    "очень": "very_active", "ежедн": "very_active"}
        activity = act_map.get(text.strip())
        if not activity:
            t = text.strip().lower()
            for key, val in act_text.items():
                if key in t:
                    activity = val
                    break
        if not activity:
            send_message(chat_id, "🤔 Напиши цифру 1-5")
            return True

        d = pending["data"]
        del _pending[owner_id]

        bmr = calc_bmr(d["weight"], d["height"], d["age"])
        tdee = calc_tdee(bmr, activity)
        macros = calc_macros(d["weight"], tdee, d["goal_type"], on_trt=True)
        rate = calc_weekly_rate(d["goal_type"], d["weight"])

        # Save to CH
        from db import get_client
        import uuid as _uuid
        ch = get_client()
        ch.insert("goals", [[
            str(_uuid.uuid4()), owner_id, None, True,
            d["goal_type"], "", None, None, d["weight"], d["height"], d["age"],
            "male", activity, round(bmr), round(tdee), macros["target_calories"],
            macros["protein_g"], macros["fat_g"], macros["carbs_g"],
            macros["leucine_daily_g"], "[]",
        ]], column_names=[
            "id", "owner_id", "created_at", "active",
            "goal_type", "description", "target_weight_kg", "target_date",
            "current_weight_kg", "height_cm", "age", "sex", "activity_level",
            "bmr", "tdee", "target_calories",
            "protein_g", "fat_g", "carbs_g", "leucine_target_g", "medications",
        ])

        send_message(chat_id, format_goal_summary(
            d["goal_type"], d["weight"], d["height"], d["age"],
            activity, bmr, tdee, macros, rate))
        return True

    # ── Weight ──
    if action == "weight":
        del _pending[owner_id]
        # Reuse handle_command logic
        cmd_resp = handle_command(f"/weight {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Search ──
    if action == "search":
        del _pending[owner_id]
        cmd_resp = handle_command(f"/search {text}", owner_id)
        if cmd_resp and not isinstance(cmd_resp, tuple):
            send_message(chat_id, cmd_resp)
        return True

    # ── Trend ──
    if action == "trend":
        del _pending[owner_id]
        cmd_resp = handle_command(f"/trend {text}", owner_id)
        if isinstance(cmd_resp, tuple):
            _handle_rich_command(cmd_resp, chat_id, owner_id, user)
        elif cmd_resp:
            send_message(chat_id, cmd_resp)
        return True

    # ── Eat ──
    if action == "eat":
        del _pending[owner_id]
        _process_eat(chat_id, owner_id, text)
        return True

    # Unknown pending — clear and fall through
    del _pending[owner_id]
    return False


def _handle_rich_command(cmd: tuple, chat_id: str, owner_id: str, user: dict) -> None:
    """Handle commands that need to send photos/documents."""
    action = cmd[0]

    if action == "__trend__":
        arg = cmd[1]
        if not arg:
            send_message(chat_id, "Укажи показатель: /trend гемоглобин")
            return
        rows = query_biomarker_trend(arg, owner_id=owner_id)
        if not rows:
            send_message(chat_id, f"'{arg}' не найден. Попробуй /biomarkers")
            return
        # Text summary
        lines = [f"<b>Тренд: {arg}</b>\n"]
        for r in rows:
            flag = " 🔴" if r.get("is_abnormal") else " ✅"
            ref = ""
            if r.get("ref_low") is not None or r.get("ref_high") is not None:
                ref = f" (норма: {r.get('ref_low', '?')}–{r.get('ref_high', '?')})"
            lines.append(f"{r['collected_at']} — <b>{r['value']}</b> {r['unit']}{ref}{flag}")
        send_message(chat_id, "\n".join(lines))
        # Chart if ≥2 points
        if len(rows) >= 2:
            send_typing(chat_id)
            try:
                from spc import compute_xmr, SPCPoint
                spc_points = [SPCPoint(r["collected_at"], r["value"], r.get("unit", ""),
                                       r.get("ref_low"), r.get("ref_high")) for r in rows]
                spc_r = compute_xmr(arg, spc_points)
                chart = render_trend_chart(
                    biomarker=arg,
                    dates=[r["collected_at"] for r in rows],
                    values=[r["value"] for r in rows],
                    unit=rows[0].get("unit", ""),
                    ref_low=rows[0].get("ref_low"),
                    ref_high=rows[0].get("ref_high"),
                    ucl=spc_r.ucl if spc_r else None,
                    lcl=spc_r.lcl if spc_r and spc_r.lcl > 0 else None,
                    mean=spc_r.mean if spc_r else None,
                )
                send_photo(chat_id, chart, f"{arg} — тренд")
            except Exception as exc:
                log.warning("Chart render failed: %s", exc)

    elif action == "__report__":
        send_typing(chat_id)
        send_message(chat_id, "Генерирую PDF-отчёт...")
        try:
            from db import query_health_profile, query_all_documents
            results = query_latest_results(200, owner_id)
            docs = query_all_documents(owner_id=owner_id)
            profile = query_health_profile(owner_id)
            pdf_bytes = generate_report(
                owner_name=user.get("name", "Пациент"),
                lab_results=results,
                documents=docs,
                profile_text=profile,
            )
            filename = f"health_report_{datetime.now(MSK_TZ).strftime('%Y%m%d')}.pdf"
            send_document_file(chat_id, pdf_bytes, filename, "Медицинский отчёт")
        except Exception as exc:
            log.error("Report generation failed: %s", exc)
            send_message(chat_id, f"Ошибка генерации отчёта: {exc}")

    elif action == "__eat__":
        food_text = cmd[1] if len(cmd) > 1 else ""
        if not food_text:
            send_message(chat_id,
                "🍽 <b>Записать приём пищи</b>\n\n"
                "📸 Отправь <b>фото</b> тарелки — распознаю автоматически\n\n"
                "✍️ Или напиши что ел:\n"
                "<code>/eat куриная грудка 200г, рис 150г, огурец</code>"
            )
            return
        send_typing(chat_id)
        _process_eat(chat_id, owner_id, food_text)

    elif action == "__week__":
        send_typing(chat_id)
        send_message(chat_id, "Готовлю еженедельный отчёт...")
        answer = ask_llm(
            "Сделай еженедельный ревью моего здоровья: сравни факт питания с протоколом, "
            "динамику веса, активность за неделю, предложи корректировки. Кратко, по пунктам.",
            owner_id,
        )
        send_message(chat_id, answer)

    elif action == "__correlations__":
        send_typing(chat_id)
        _show_correlations(chat_id, owner_id)


# ── Eat processing ──
def _process_eat(chat_id: str, owner_id: str, food_text: str) -> None:
    """Process food input via LLM → structured nutrition data → CH."""
    prompt = f"""Ты — нутрициолог. Пользователь описал приём пищи. Рассчитай нутриентный состав.

ПРАВИЛА:
- Считай на указанный вес порции. Если вес не указан — бери стандартную порцию
- Все 9 незаменимых аминокислот обязательны (г)
- Микронутриенты: железо, цинк, кальций, магний, витамин D (если есть)
- DIAAS score общего приёма (0-1.5)
- Определи тип приёма: breakfast/lunch/dinner/snack

Верни СТРОГО JSON:
{{"meal_type": "lunch", "description": "краткое описание", "items": ["курица 200г", "рис 150г"],
"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0, "fiber_g": 0,
"leucine_g": 0, "isoleucine_g": 0, "valine_g": 0, "lysine_g": 0,
"methionine_g": 0, "threonine_g": 0, "tryptophan_g": 0, "phenylalanine_g": 0, "histidine_g": 0,
"diaas_score": 0,
"micronutrients": {{"iron_mg": 0, "zinc_mg": 0, "calcium_mg": 0, "magnesium_mg": 0}},
"warnings": ["если есть проблемы: лейцин ниже порога, плохой DIAAS, и т.д."]}}

ПРИЁМ ПИЩИ: {food_text}"""

    try:
        import subprocess
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-sonnet-4-6", prompt],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            send_message(chat_id, "Ошибка анализа. Попробуй описать подробнее.")
            return

        import re as _re2
        m = _re2.search(r"\{.*\}", result.stdout, _re2.DOTALL)
        if not m:
            send_message(chat_id, "Не удалось разобрать ответ. Попробуй ещё раз.")
            return
        data = json.loads(m.group(0))

        # Save to CH
        from db import get_client
        ch = get_client()
        micro_json = json.dumps(data.get("micronutrients", {}), ensure_ascii=False)
        ch.insert("nutrition_log", [[
            str(__import__("uuid").uuid4()), owner_id, datetime.now(),
            data.get("meal_type", ""), data.get("description", ""),
            data.get("calories", 0), data.get("protein_g", 0),
            data.get("fat_g", 0), data.get("carbs_g", 0), data.get("fiber_g", 0),
            data.get("leucine_g", 0), data.get("isoleucine_g", 0),
            data.get("valine_g", 0), data.get("lysine_g", 0),
            data.get("methionine_g", 0), data.get("threonine_g", 0),
            data.get("tryptophan_g", 0), data.get("phenylalanine_g", 0),
            data.get("histidine_g", 0), data.get("diaas_score", 0),
            micro_json, "text", food_text,
        ]], column_names=[
            "id", "owner_id", "ts", "meal_type", "description",
            "calories", "protein_g", "fat_g", "carbs_g", "fiber_g",
            "leucine_g", "isoleucine_g", "valine_g", "lysine_g",
            "methionine_g", "threonine_g", "tryptophan_g", "phenylalanine_g",
            "histidine_g", "diaas_score", "micronutrients", "source", "raw_input",
        ])

        # Format response
        leu = data.get("leucine_g", 0)
        leu_status = "✅" if leu >= 2.5 else f"⚠️ ниже порога mTOR ({leu:.1f}г < 2.5г)"
        diaas = data.get("diaas_score", 0)
        diaas_status = "✅" if diaas >= 1.0 else f"⚠️ неполный профиль ({diaas:.2f})"

        warnings = data.get("warnings", [])

        msg = (
            f"<b>{data.get('description', food_text)}</b>\n"
            f"\n{data.get('calories', 0)} ккал | "
            f"Б {data.get('protein_g', 0)}г | "
            f"Ж {data.get('fat_g', 0)}г | "
            f"У {data.get('carbs_g', 0)}г\n"
            f"\nЛейцин: {leu:.1f}г {leu_status}"
            f"\nDIAAS: {diaas:.2f} {diaas_status}"
        )
        if warnings:
            msg += "\n\n" + "\n".join(f"⚠️ {w}" for w in warnings[:3])

        # Day totals
        today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
        totals = ch.query(
            f"SELECT sum(calories), sum(protein_g), sum(leucine_g), count() "
            f"FROM nutrition_log WHERE owner_id = '{owner_id}' "
            f"AND toDate(ts) = '{today}'"
        )
        if totals.result_rows:
            t = totals.result_rows[0]
            msg += f"\n\n<b>Итого за день:</b> {t[0]:.0f} ккал | Белок {t[1]:.0f}г | Лейцин {t[2]:.1f}г | Приёмов: {t[3]}"

        msg += "\n\nПодробнее: спроси 'подробнее'"
        send_message(chat_id, msg)

    except Exception as exc:
        log.error("Eat processing failed: %s", exc)
        send_message(chat_id, f"Ошибка: {exc}")


# ── Correlations ──
BIOMARKER_SYSTEMS = {
    "Печень": ["АЛТ", "АСТ", "ГГТ", "Билирубин общий", "Билирубин прямой", "Билирубин непрямой", "Щелочная фосфатаза"],
    "Почки": ["Креатинин", "Мочевина", "Мочевая кислота", "Цистатин С"],
    "Железо": ["Железо сывороточное", "Ферритин", "Трансферрин", "ОЖСС", "Коэффициент насыщения трансферрина"],
    "Липиды": ["Холестерин общий", "ЛПВП", "ЛПНП", "Триглицериды"],
    "Гормоны": ["Тестостерон общий", "Тестостерон свободный", "Эстрадиол", "ТТГ", "Т3", "Т4", "Пролактин", "Кортизол", "ИФР-1", "Инсулин"],
    "Кровь (ОАК)": ["Гемоглобин", "Эритроциты", "Лейкоциты", "Тромбоциты", "Гематокрит", "СОЭ"],
    "Воспаление": ["С-реактивный белок", "СОЭ", "Лейкоциты", "Фибриноген"],
    "Метаболизм": ["Глюкоза", "HbA1c", "Белок общий", "Альбумин"],
    "Иммунология": ["АТ к фосфолипидам IgG", "АТ к фосфолипидам IgM", "Иммуноглобулин G", "Иммуноглобулин M"],
}


def _show_correlations(chat_id: str, owner_id: str) -> None:
    from db import get_client
    ch = get_client()
    # Get all unique biomarkers for this user
    result = ch.query(
        f"SELECT DISTINCT biomarker FROM lab_results WHERE owner_id = '{owner_id}'"
    )
    user_markers = {row[0] for row in result.result_rows}

    lines = ["<b>Корреляции по системам</b>\n"]
    found_any = False

    for system, markers in BIOMARKER_SYSTEMS.items():
        # Find which markers user has
        matched = [m for m in markers if any(m.lower() in um.lower() for um in user_markers)]
        if len(matched) < 2:
            continue
        found_any = True
        lines.append(f"\n<b>{system}</b> ({len(matched)} показателей):")

        # Get latest values
        for m in matched:
            latest = ch.query(
                f"SELECT collected_at, value, unit, ref_low, ref_high, is_abnormal "
                f"FROM lab_results WHERE owner_id = '{owner_id}' "
                f"AND biomarker ILIKE '%{m}%' "
                f"ORDER BY collected_at DESC LIMIT 1"
            )
            if latest.result_rows:
                r = latest.result_rows[0]
                flag = " 🔴" if r[5] else ""
                ref = f" (норма: {r[3] or '?'}–{r[4] or '?'})" if r[3] or r[4] else ""
                lines.append(f"  {m}: <b>{r[1]}</b> {r[2]}{ref}{flag} [{r[0]}]")

    if not found_any:
        send_message(chat_id, "Недостаточно данных для корреляций. Нужно ≥2 показателя из одной системы.")
        return

    send_message(chat_id, "\n".join(lines))


def process_message(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    username = (message.get("from", {}).get("username") or "?")

    # Security: only authorized users from users.yaml
    user = resolve_user(message)
    if not user:
        log.info("Ignored message from @%s (chat_id=%s) — not in users.yaml", username, chat_id)
        return

    owner_id = user["owner_id"]

    # Check for document (PDF)
    document = message.get("document")
    if document:
        file_name = document.get("file_name", "unknown.pdf")
        mime = document.get("mime_type", "")
        file_id = document.get("file_id")

        if not file_name.lower().endswith(".pdf") and "pdf" not in mime.lower():
            send_message(chat_id, "Отправь PDF-файл с результатами анализов.")
            return

        log.info("Received PDF from %s: %s (%s, %d bytes)", user["name"], file_name, mime, document.get("file_size", 0))
        send_message(chat_id, f"Обрабатываю <b>{file_name}</b>...")
        send_typing(chat_id)

        try:
            report = process_pdf(file_id, file_name, chat_id, owner_id)
        except Exception as exc:
            log.error("PDF processing failed: %s\n%s", exc, traceback.format_exc())
            report = f"Ошибка обработки PDF: {exc}"

        send_message(chat_id, report)
        return

    # Photo → OCR via Claude Vision
    if message.get("photo"):
        photos = message["photo"]
        best = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = best["file_id"]

        log.info("Received photo from %s (%d bytes)", user["name"], best.get("file_size", 0))
        send_message(chat_id, "Распознаю фото...")
        send_typing(chat_id)

        try:
            timestamp = datetime.now(MSK_TZ).strftime("%Y%m%d_%H%M%S")
            photo_path = DATA_DIR / f"{timestamp}_photo.jpg"
            download_photo(file_id, photo_path)

            # Use Claude Vision to extract text
            import base64
            img_b64 = base64.b64encode(photo_path.read_bytes()).decode()
            vision_prompt = (
                "Извлеки весь текст с этого фото медицинского документа/анализа. "
                "Сохрани структуру: названия показателей, значения, единицы, нормы. "
                "Верни чистый текст как есть, без интерпретации."
            )
            # claude CLI with image
            result = subprocess.run(
                ["claude", "-p", "--model", "claude-sonnet-4-6",
                 f"[image: data:image/jpeg;base64,{img_b64}] {vision_prompt}"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                send_message(chat_id, f"Ошибка распознавания: {result.stderr[:200]}")
                return

            ocr_text = result.stdout.strip()
            if not ocr_text or len(ocr_text) < 20:
                send_message(chat_id, "Не удалось распознать текст на фото. Попробуй с лучшим освещением или отправь PDF.")
                return

            log.info("OCR extracted %d chars from photo", len(ocr_text))

            # Process as text input
            from extractor import classify_document, extract_biomarkers, validate_results
            classification = classify_document(ocr_text)
            doc_class = classification.get("doc_class", "other")

            if doc_class == "lab_results":
                extracted = extract_biomarkers(ocr_text)
                valid_rows, warnings = validate_results(extracted)
                if valid_rows:
                    lab_name = extracted.get("lab_name") or "photo"
                    collected_at = valid_rows[0]["collected_at"]
                    source_name = f"photo_{timestamp}.jpg"
                    for row in valid_rows:
                        row["source_file"] = source_name
                        row["raw_text"] = ocr_text[:10000]
                    count = insert_lab_results(valid_rows, owner_id)
                    insert_upload_log(source_name, best.get("file_size", 0), 1, count,
                                      lab_name, collected_at, "ok", "", ocr_text[:10000], owner_id=owner_id)
                    abnormal = [r for r in valid_rows
                                if (r.get("ref_low") is not None and r["value"] < r["ref_low"])
                                or (r.get("ref_high") is not None and r["value"] > r["ref_high"])]
                    report = f"<b>Фото обработано</b>\nЛаборатория: {lab_name}\nДата: {collected_at}\nПоказателей: <b>{count}</b>"
                    if abnormal:
                        report += f"\n\n<b>Вне нормы ({len(abnormal)}):</b>"
                        for r in abnormal:
                            report += f"\n  🔴 {r['biomarker']}: <b>{r['value']}</b> {r['unit']}"
                    send_message(chat_id, report)
                    return

            # Not lab results or no numeric values — save as document
            doc_type = classification.get("doc_type", "other")
            doc_date_str = classification.get("collected_at")
            doc_date = date.today()
            if doc_date_str:
                try:
                    doc_date = date.fromisoformat(doc_date_str)
                except (ValueError, TypeError):
                    pass
            _save_as_document(f"photo_{timestamp}.jpg", classification.get("title", "Фото"), ocr_text,
                              doc_date, classification.get("lab_name", ""), doc_type, owner_id)
            send_message(chat_id,
                         f"<b>Фото сохранено как документ</b>\n"
                         f"Тип: {doc_type}\nДата: {doc_date}\nТекст: {len(ocr_text)} символов")

        except Exception as exc:
            log.error("Photo processing failed: %s", exc)
            send_message(chat_id, f"Ошибка обработки фото: {exc}")
        return

    # Text message
    text = (message.get("text") or "").strip()
    if not text:
        return

    log.info("Owner message: '%s'", text[:200])
    msg_id = message.get("message_id", 0)

    # L0: save user message to chat log
    insert_chat_message("user", text, msg_id, owner_id)

    # ── Handle pending dialog actions (user replied to a previous question) ──
    if owner_id in _pending and not text.startswith("/"):
        # Cancel keywords
        if text.strip().lower() in ("отмена", "отменить", "cancel", "стоп", "нет"):
            del _pending[owner_id]
            send_message(chat_id, "��� Отменено")
            return
        # Timeout: pending older than 5 minutes → clear silently
        pending_ts = _pending[owner_id].get("ts", 0)
        if time.time() - pending_ts > 300:
            del _pending[owner_id]
            # Fall through to normal processing
        else:
            handled = _handle_pending(chat_id, owner_id, text, user)
            if handled:
                return

    # Feedback → GitHub Issue (dialog mode)
    if text.strip().lower().startswith("/feedback"):
        _pending[owner_id] = {"ts": time.time(), "action": "feedback"}
        send_message(chat_id, "💬 Напиши что не так или что хочешь улучшить:")
        return

    # Reminders — handled here because needs chat_id
    if text.strip().lower().startswith("/remind"):
        from db import get_client
        ch = get_client()
        parsed = parse_reminder_command(text)
        if not parsed:
            send_message(chat_id,
                "<b>Напоминания</b>\n\n"
                "<b>Добавить:</b>\n"
                "<code>/remind 09:00 Принять BPC-157</code>\n"
                "<code>/remind 21:00 пн,ср,пт Укол тестостерона</code>\n\n"
                "<b>Список:</b> /remind список\n"
                "<b>Удалить:</b> <code>/remind удалить abc123</code>\n\n"
                "Дни: пн, вт, ср, чт, пт, сб, вс (без дней = каждый день)")
            return
        if parsed["action"] == "list":
            reminders = get_all_reminders(ch, owner_id)
            send_message(chat_id, format_reminders_list(reminders))
        elif parsed["action"] == "delete":
            result = delete_reminder(ch, owner_id, parsed["id"])
            send_message(chat_id, result)
        elif parsed["action"] == "add":
            result = add_reminder(ch, owner_id, chat_id,
                                  parsed["text"], parsed["hour"], parsed["minute"],
                                  parsed.get("days"))
            send_message(chat_id, result)
        return

    # Try commands
    cmd_response = handle_command(text, owner_id)
    if cmd_response:
        # Special tuple commands that need chat_id
        if isinstance(cmd_response, tuple):
            _handle_rich_command(cmd_response, chat_id, owner_id, user)
            return
        send_message(chat_id, cmd_response)
        return

    # ── Natural language reminders (before LLM Q&A) ──
    if looks_like_reminder(text):
        parsed_rem = parse_natural_reminder(text)
        if parsed_rem:
            log.info("Natural reminder: %s", parsed_rem)
            from db import get_client
            ch = get_client()
            result = add_reminder(
                ch, owner_id, chat_id,
                parsed_rem["text"],
                parsed_rem["hour"],
                parsed_rem["minute"],
                target_date=parsed_rem.get("target_date"),
            )
            send_message(chat_id, result)
            insert_chat_message("bot", result, owner_id=owner_id)
            return

    # Classify long text input: medical data, workout log, or question
    input_type = classify_input_type(text)

    if input_type == "medical":
        log.info("Text classified as medical data (%d chars)", len(text))
        send_message(chat_id, "Распознаю медицинские данные...")
        result = process_text_lab_data(text, chat_id, owner_id)
        if result:
            send_message(chat_id, result)
            return
        log.info("No medical data in text, falling through to Q&A")

    elif input_type == "workout":
        log.info("Text classified as workout data (%d chars)", len(text))
        send_message(chat_id, "Распознаю тренировку...")
        result = process_workout_data(text, chat_id, owner_id,
                                      send_typing_fn=send_typing)
        if result:
            send_message(chat_id, result)
            # L0: save bot response to chat log
            insert_chat_message("bot", result, owner_id=owner_id)
            return
        log.info("Workout parse failed, falling through to Q&A")

    # Free-form question or /summary → LLM
    send_typing(chat_id)
    if text.strip().lower() == "/summary":
        text = "Дай общую оценку моего здоровья на основе всех имеющихся анализов. Выдели главные проблемы и положительные тренды."

    try:
        answer = ask_llm(text, owner_id)
    except Exception as exc:
        log.error("LLM Q&A error: %s", exc)
        answer = f"Ошибка: {exc}"

    send_message(chat_id, answer)

    # L0: save bot response to chat log
    insert_chat_message("bot", answer, owner_id=owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────
def _reminder_loop() -> None:
    """Background thread: check and send due reminders every 60 seconds."""
    import threading
    from db import get_client
    while True:
        try:
            ch = get_client()
            due = check_due_reminders(ch)
            for r in due:
                now_msk = datetime.now(MSK_TZ).strftime("%H:%M")
                msg = f"💊 <b>Напоминание</b> ({now_msk})\n\n{r['text']}"
                try:
                    send_message(r["chat_id"], msg)
                    mark_sent(ch, r["id"], deactivate=r.get("is_oneshot", False))
                    log.info("Reminder sent to %s: %s (oneshot=%s)",
                             r["owner_id"], r["text"][:50], r.get("is_oneshot"))
                except Exception as exc:
                    log.error("Failed to send reminder %s: %s", r["id"][:8], exc)
        except Exception as exc:
            log.error("Reminder loop error: %s", exc)
        time.sleep(60)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    users = load_users()
    log.info("=== HEALTH-BOT STARTED (users: %s) ===", ", ".join(f"@{u}" for u in users))

    # Seed global glossary from workout_glossary.yaml (idempotent)
    try:
        from glossary import seed_from_workout_glossary
        from db import get_client
        count = seed_from_workout_glossary(get_client())
        if count:
            log.info("Glossary seeded: %d terms", count)
    except Exception as exc:
        log.warning("Glossary seed failed: %s", exc)

    # Start reminder checker in background
    import threading
    reminder_thread = threading.Thread(target=_reminder_loop, daemon=True)
    reminder_thread.start()
    log.info("Reminder thread started")

    offset: int | None = None

    # Skip pending messages on startup
    try:
        pending = get_updates(offset=None)
        if pending:
            offset = pending[-1]["update_id"] + 1
            log.info("Skipped %d pending updates", len(pending))
    except Exception as exc:
        log.warning("Failed to skip pending: %s", exc)

    while True:
        try:
            updates = get_updates(offset=offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    process_message(message)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as exc:
            log.error("Poll error: %s\n%s", exc, traceback.format_exc())
            time.sleep(ERROR_BACKOFF_SEC)


if __name__ == "__main__":
    main()
