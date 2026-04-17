"""PDF text extraction using pdfplumber — universal, no lab-specific logic."""
from __future__ import annotations

from pathlib import Path

import pdfplumber


def extract_text(pdf_path: str | Path) -> tuple[str, int]:
    """
    Extract all text from a PDF file.

    Returns:
        (full_text, page_count)
    """
    pdf_path = Path(pdf_path)
    pages_text: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            text = page.extract_text() or ""
            # Also try extracting tables as text
            tables = page.extract_tables() or []
            for table in tables:
                for row in table:
                    if row:
                        cells = [str(c).strip() if c else "" for c in row]
                        text += "\n" + " | ".join(cells)
            pages_text.append(text)

    full_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text)
    return full_text, page_count
