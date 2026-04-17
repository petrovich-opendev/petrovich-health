"""Generate PDF health report for doctor."""
from __future__ import annotations

import io
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

MSK_TZ = timezone(timedelta(hours=3))


def _register_fonts():
    """Try to register a font that supports Cyrillic."""
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    ]:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont("DejaVu", path))
            bold_path = path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
            if Path(bold_path).exists():
                pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold_path))
            return "DejaVu"
    return "Helvetica"


def generate_report(
    owner_name: str,
    lab_results: list[dict],
    documents: list[dict],
    profile_text: str | None = None,
) -> bytes:
    """Generate PDF report, return bytes."""
    font = _register_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("RuTitle", fontName=font, fontSize=16, spaceAfter=10, leading=20))
    styles.add(ParagraphStyle("RuH2", fontName=font, fontSize=12, spaceAfter=6, leading=16, textColor=colors.HexColor("#1565C0")))
    styles.add(ParagraphStyle("RuBody", fontName=font, fontSize=9, leading=12))
    styles.add(ParagraphStyle("RuSmall", fontName=font, fontSize=8, leading=10, textColor=colors.gray))

    elements = []
    now = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M")

    # Title
    elements.append(Paragraph(f"Медицинский отчёт — {owner_name}", styles["RuTitle"]))
    elements.append(Paragraph(f"Сформирован: {now} МСК", styles["RuSmall"]))
    elements.append(Spacer(1, 8*mm))

    # Health Profile summary
    if profile_text:
        elements.append(Paragraph("Текущий профиль здоровья", styles["RuH2"]))
        for line in profile_text.split("\n"):
            line = line.strip()
            if line:
                elements.append(Paragraph(line, styles["RuBody"]))
        elements.append(Spacer(1, 6*mm))

    # Lab results table — group by date
    if lab_results:
        elements.append(Paragraph("Лабораторные анализы", styles["RuH2"]))

        # Group by date
        by_date: dict[str, list] = {}
        for r in lab_results:
            d = str(r.get("collected_at", "?"))
            by_date.setdefault(d, []).append(r)

        for dt in sorted(by_date.keys()):
            rows_for_date = by_date[dt]
            lab_name = rows_for_date[0].get("lab_name", "")
            elements.append(Paragraph(f"{dt} — {lab_name}", styles["RuBody"]))
            elements.append(Spacer(1, 2*mm))

            table_data = [["Показатель", "Результат", "Ед.", "Норма", ""]]
            for r in rows_for_date:
                ref = ""
                if r.get("ref_low") is not None or r.get("ref_high") is not None:
                    ref = f"{r.get('ref_low', '?')} – {r.get('ref_high', '?')}"
                flag = "!!!" if r.get("is_abnormal") else ""
                table_data.append([
                    r.get("biomarker", ""),
                    f"{r.get('value', '')}",
                    r.get("unit", ""),
                    ref,
                    flag,
                ])

            t = Table(table_data, colWidths=[55*mm, 25*mm, 20*mm, 30*mm, 10*mm])
            style = TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, 0), font),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3F2FD")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ])
            # Highlight abnormal rows
            for i, r in enumerate(rows_for_date, start=1):
                if r.get("is_abnormal"):
                    style.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FFEBEE"))
                    style.add("TEXTCOLOR", (1, i), (1, i), colors.red)

            t.setStyle(style)
            elements.append(t)
            elements.append(Spacer(1, 4*mm))

    # Documents list
    if documents:
        elements.append(Paragraph("Медицинские документы", styles["RuH2"]))
        for d in documents:
            elements.append(Paragraph(
                f"{d.get('collected_at', '?')} | {d.get('doc_type', '?')} | {d.get('title', '?')}",
                styles["RuBody"]
            ))
        elements.append(Spacer(1, 4*mm))

    # Footer
    elements.append(Spacer(1, 10*mm))
    elements.append(Paragraph(
        "Данный отчёт сформирован автоматически системой Health Analytics. "
        "Информация носит справочный характер.",
        styles["RuSmall"]
    ))

    doc.build(elements)
    buf.seek(0)
    return buf.read()
