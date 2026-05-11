"""Document classification + PDF processing pipeline."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db import insert_document, insert_upload_log
from extractor import classify_document
from pdf_parser import extract_text
from tg_transport import download_file, send_typing

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
MSK_TZ = timezone(timedelta(hours=3))

log = logging.getLogger("health-bot")


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

    log.info("Downloading PDF: %s → %s", file_name, local_path)
    download_file(file_id, local_path)
    file_size = local_path.stat().st_size
    log.info("Downloaded %d bytes", file_size)

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

    if doc_class == "lab_results":
        # Lazy import — tg_extraction imports from tg_documents for _save_as_document.
        from tg_extraction import _process_lab_results
        return _process_lab_results(raw_text, safe_name, file_name, file_size,
                                    page_count, doc_lab, doc_date, chat_id, owner_id)

    _save_as_document(safe_name, doc_title, raw_text, doc_date, doc_lab, doc_type, owner_id)
    insert_upload_log(safe_name, file_size, page_count, 0, doc_lab, doc_date,
                      "document", f"class={doc_class}", raw_text[:10000], owner_id=owner_id)
    log.info("Saved as document: class=%s type=%s", doc_class, doc_type)

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
