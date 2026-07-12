"""
Generate fail PDF/Word dari teks (biasanya jawapan terakhir AI) untuk dihantar
balik kepada student — supaya senang simpan/print/share nota.

Ni PERCUMA (tak guna AI generation) — cuma convert teks yang AI dah jana kepada
format fail, guna library biasa (fpdf2, python-docx).
"""

import io
from fpdf import FPDF
from docx import Document


def _sanitize_for_pdf(text: str) -> str:
    """fpdf2 (guna font core Helvetica) tak sokong semua unicode (contoh emoji).
    Tukar/buang character yang tak disokong supaya tak crash masa generate PDF."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


def text_to_pdf_bytes(title: str, body: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(0, 10, _sanitize_for_pdf(title))
    pdf.ln(4)
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 8, _sanitize_for_pdf(body))
    return bytes(pdf.output())


def text_to_docx_bytes(title: str, body: str) -> bytes:
    doc = Document()
    doc.add_heading(title, level=1)
    for para in body.split("\n\n"):
        if para.strip():
            doc.add_paragraph(para.strip())
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
