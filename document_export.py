"""
Generate fail PDF/Word/PowerPoint dari teks (biasanya jawapan terakhir AI)
untuk dihantar balik kepada student — supaya senang simpan/print/share nota.

Ni PERCUMA (tak guna AI generation) — cuma convert teks yang AI dah jana kepada
format fail, guna library biasa (fpdf2, python-docx, python-pptx).
"""

import io
from fpdf import FPDF
from docx import Document
from pptx import Presentation
from pptx.util import Pt

# Berapa baris (bullet) maksimum setiap slide sebelum sambung ke slide baru,
# supaya slide tak terlalu padat/sesak.
MAX_LINES_PER_SLIDE = 6


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


def _split_lines(body: str) -> list[str]:
    """Pecahkan body kepada baris/bullet untuk letak dalam slide."""
    lines = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if line:
            lines.append(line)
    return lines or [body.strip()]


def text_to_pptx_bytes(title: str, body: str) -> bytes:
    """Tukar teks (contoh: jawapan/nota AI) jadi slide PowerPoint ringkas.
    Slide pertama ialah tajuk, slide-slide seterusnya senaraikan isi dalam
    bentuk bullet point, dipecahkan supaya tak terlalu padat satu slide."""
    prs = Presentation()

    # Slide tajuk
    title_slide_layout = prs.slide_layouts[0]
    slide = prs.slides.add_slide(title_slide_layout)
    slide.shapes.title.text = title
    if len(slide.placeholders) > 1:
        slide.placeholders[1].text = "Dijana oleh StudyBot"

    # Slide isi (guna layout "Title and Content")
    content_layout = prs.slide_layouts[1]
    lines = _split_lines(body)

    chunks = [
        lines[i : i + MAX_LINES_PER_SLIDE]
        for i in range(0, len(lines), MAX_LINES_PER_SLIDE)
    ] or [[]]

    for idx, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(content_layout)
        slide.shapes.title.text = title if idx == 0 else f"{title} (samb.)"

        body_placeholder = slide.placeholders[1]
        text_frame = body_placeholder.text_frame
        text_frame.clear()
        text_frame.word_wrap = True

        for j, line in enumerate(chunk):
            paragraph = text_frame.paragraphs[0] if j == 0 else text_frame.add_paragraph()
            paragraph.text = line
            paragraph.font.size = Pt(20)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
