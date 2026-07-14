"""
Bersihkan teks jawapan AI (Gemini) sebelum hantar ke Telegram.

Gemini biasa jana teks dalam format Markdown (**bold**, # heading, `code`,
* bullet, dll). Bot Telegram ni hantar mesej sebagai plain text (tiada
parse_mode), jadi simbol markdown tu terpapar terus kat student sebagai
"simbol pelik" (**, #, `, dsb.) — bukannya format yang cantik.

Fungsi kat sini tukar/buang simbol markdown tu supaya mesej yang student
terima kemas & mudah dibaca, tanpa simbol yang tak sepatutnya ada.
"""

import re


def clean_ai_text(text: str) -> str:
    """Buang/tukar syntax Markdown biasa kepada plain text yang kemas."""
    if not text:
        return text

    # Code block ```...``` -> buang backtick, kekalkan isi
    text = re.sub(r"```[a-zA-Z0-9_\-]*\n?", "", text)
    text = text.replace("```", "")

    # Inline code `code` -> code
    text = re.sub(r"`([^`\n]+)`", r"\1", text)

    # Bold **text** / __text__ -> text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)

    # Garis pemisah macam "---" / "***" / "___" -> buang (buat dulu sebelum
    # bullet/italic supaya "***" tak silap dikira sebagai bullet/italic)
    text = re.sub(r"^[ \t]*([\-*_])\1{2,}[ \t]*$", "", text, flags=re.MULTILINE)

    # Bullet "* item" / "- item" -> "• item" (buat dulu sebelum italic supaya
    # tanda "*" bullet di awal baris tak silap dipadan sebagai italic)
    text = re.sub(r"^([ \t]*)[\*\-][ \t]+", r"\1• ", text, flags=re.MULTILINE)

    # Strikethrough ~~text~~ -> text
    text = re.sub(r"~~(.+?)~~", r"\1", text)

    # Italic *text* / _text_ -> text
    text = re.sub(r"(?<!\*)\*([^\n*]+?)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^\n_]+?)_(?!_)", r"\1", text)

    # Heading: buang # di awal baris ("# Tajuk" -> "Tajuk")
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.MULTILINE)

    # Link markdown [teks](url) -> "teks (url)"
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", r"\1 (\2)", text)

    # Kemaskan baris kosong berlebihan yang tertinggal selepas cleaning
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
