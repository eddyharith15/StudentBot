"""
Student AI Telegram Bot (Google Gemini) — dengan sistem Token/Kredit + ToyyibPay
------------------------------------------------------------------------------
- User baru dapat FREE_STARTING_CREDITS bila /start
- 1 kredit = 1 mesej/soalan/fail
- Bila kredit habis, bot tawar pakej topup (bayar guna ToyyibPay - FPX/kad)
- Admin boleh tambah kredit manual guna /addcredits
- Terima gambar, PDF, dan video (Gemini multimodal)
- /pdf dan /word — export jawapan terakhir AI jadi fail PDF/Word

Setup:
1. pip install -r requirements.txt
2. Set semua environment variables (lihat README.md / .env.example)
3. python bot.py
"""

import os
import io
import time
import logging
import asyncio
import base64

from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from aiohttp import web

import config
import database
import payment
import document_export
from server import create_web_app

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

# Guna beberapa model sebagai fallback — kalau satu kena quota limit / tak available,
# bot automatik cuba model seterusnya. Ni elak bot "mati" bila Google ubah had free-tier
# model tertentu (biasa berlaku, quota free-tier berbeza ikut model & berubah dari semasa
# ke semasa).
GEMINI_MODEL_CANDIDATES = [
    "gemini-flash-lite-latest",  # paling ringan, biasanya quota harian paling tinggi
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
]

SYSTEM_PROMPT = """Kau ialah pembantu AI untuk student (StudyBot). Tugas kau:
- Jawab soalan akademik dengan jelas dan tepat, dalam Bahasa Malaysia atau English ikut bahasa student tanya.
- Bila student minta tolong assignment, JANGAN terus bagi jawapan siap. Terangkan konsep, tunjuk langkah-langkah,
  bagi contoh serupa, supaya student faham dan boleh siapkan sendiri. Kalau student betul-betul stuck lepas
  cuba faham, baru bagi jawapan penuh dengan penjelasan.
- Bila diminta ringkaskan nota, buat ringkasan padat dalam bentuk bullet points, senang diingati untuk exam.
- Guna bahasa mesra, ringkas, dan sesuai untuk pelajar (secondary school / university level).
- Kalau soalan tak jelas subjek/tahap apa, tanya sikit untuk clarify sebelum jawab panjang.
- PENTING - FORMAT JAWAPAN: Jangan sekali-kali guna simbol markdown macam **, __, ##, atau backtick
  dalam jawapan kau — Telegram TAK render simbol ni, jadi ia akan nampak sebagai simbol pelik/literal
  kat student. Untuk bold/tekanan, guna huruf besar sikit atau susun ayat je. Untuk senarai, guna
  simbol "•" atau nombor biasa ("1.", "2."), bukan "*" atau "-". Tulis dalam plain text bersih.
"""

user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10
MAX_FILE_SIZE_MB = 20  # had Telegram Bot API (download fail >20MB tak boleh)

# Rate limiting ringkas — elak satu user spam request dalam masa singkat
# (drain quota Gemini yang dikongsi semua user). Ni bukan pengganti sistem kredit,
# tapi lapisan tambahan untuk elak burst spam.
RATE_LIMIT_MAX_REQUESTS = 8
RATE_LIMIT_WINDOW_SECONDS = 60
_user_request_times: dict[int, list[float]] = {}


def check_rate_limit(user_id: int) -> bool:
    """Return True kalau user masih dalam had (boleh proceed), False kalau kena tunggu."""
    now = time.time()
    timestamps = _user_request_times.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        _user_request_times[user_id] = timestamps
        return False
    timestamps.append(now)
    _user_request_times[user_id] = timestamps
    return True


# ---------- Helpers ----------

def build_gemini_contents(history: list[dict]) -> list[dict]:
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    return contents


def generate_with_fallback(contents: list[dict]):
    """Cuba setiap model dalam GEMINI_MODEL_CANDIDATES sampai satu berjaya.
    Raise error terakhir kalau semua gagal."""
    last_error = None
    for model_name in GEMINI_MODEL_CANDIDATES:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config={"system_instruction": SYSTEM_PROMPT, "max_output_tokens": 1500},
            )
            return response
        except Exception as e:
            logger.warning(f"Model {model_name} gagal ({e}), cuba model seterusnya...")
            last_error = e
    raise last_error


def topup_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"{p['name']} — {p['credits']} kredit (RM{p['price_myr']:.2f})",
                callback_data=f"topup:{p['id']}",
            )
        ]
        for p in config.PACKAGES
    ]
    return InlineKeyboardMarkup(buttons)


def get_last_ai_answer(user_id: int) -> str | None:
    history = user_histories.get(user_id, [])
    for msg in reversed(history):
        if msg["role"] == "assistant":
            return msg["content"]
    return None


# ---------- Command Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new = await database.ensure_user(user.id, user.username or user.first_name, config.FREE_STARTING_CREDITS)
    credits = await database.get_credits(user.id)

    referral_bonus_applied = False
    if is_new and context.args:
        referral_arg = context.args[0]
        if referral_arg.isdigit():
            referrer_id = int(referral_arg)
            recorded = await database.record_referral(
                referrer_id, user.id, config.REFERRAL_BONUS_REFERRER
            )
            if recorded:
                # bonus untuk kawan yang baru join
                await database.add_credits(user.id, config.REFERRAL_BONUS_REFEREE_EXTRA)
                credits = await database.get_credits(user.id)
                referral_bonus_applied = True

                # bonus untuk orang yang refer
                await database.add_credits(referrer_id, config.REFERRAL_BONUS_REFERRER)
                try:
                    referrer_new_balance = await database.get_credits(referrer_id)
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            f"🎉 Kawan kau join StudyBot guna link referral kau!\n"
                            f"Kau dapat +{config.REFERRAL_BONUS_REFERRER} kredit.\n"
                            f"Baki kredit sekarang: {referrer_new_balance}"
                        ),
                    )
                except Exception:
                    pass  # referrer mungkin block bot ke apa2

    if is_new:
        bonus_line = (
            f"Kau dapat {config.FREE_STARTING_CREDITS + config.REFERRAL_BONUS_REFEREE_EXTRA} kredit PERCUMA "
            f"(termasuk +{config.REFERRAL_BONUS_REFEREE_EXTRA} bonus referral) untuk mula 🎉\n\n"
            if referral_bonus_applied
            else f"Kau dapat {config.FREE_STARTING_CREDITS} kredit PERCUMA untuk mula 🎉\n\n"
        )
        await update.message.reply_text(
            f"Hai {user.first_name}! Aku StudyBot 🤖📚\n\n"
            f"{bonus_line}"
            "Aku boleh tolong kau:\n"
            "• Jawab soalan pelajaran\n"
            "• Explain & pandu siapkan assignment\n"
            "• Ringkaskan nota panjang\n"
            "• Hantar gambar nota/soalan, PDF, atau video — aku boleh baca sekali! 📷📄🎥\n"
            "• /pdf /word /ppt /excel — export jawapan terakhir aku jadi fail\n\n"
            "1 kredit = 1 soalan/mesej/fail. Guna /credits untuk semak baki, "
            "/topup untuk tambah kredit, /referral untuk dapat kredit percuma jemput kawan, "
            "/reset untuk mula chat baru.\n\n"
            "Guna /help untuk senarai penuh command, /terms untuk terma & dasar privasi.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await update.message.reply_text(
            f"Hai balik {user.first_name}! Baki kredit kau: {credits} 💳\n"
            "Taip soalan kau, atau /topup kalau nak tambah kredit.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def send_credits_info(user_id: int, username: str, reply_func):
    await database.ensure_user(user_id, username, config.FREE_STARTING_CREDITS)
    credits = await database.get_credits(user_id)
    await reply_func(f"💳 Baki kredit kau: {credits}\n\nGuna /topup untuk tambah kredit.")


async def send_referral_info(user_id: int, username: str, bot_username: str, reply_func):
    await database.ensure_user(user_id, username, config.FREE_STARTING_CREDITS)
    link = f"https://t.me/{bot_username}?start={user_id}"
    count = await database.get_referral_count(user_id)
    total_earned = count * config.REFERRAL_BONUS_REFERRER

    await reply_func(
        "🎁 Jemput kawan, dapat kredit PERCUMA!\n\n"
        f"Setiap kawan yang join guna link kau, kau dapat +{config.REFERRAL_BONUS_REFERRER} kredit "
        f"(dan kawan kau pun dapat +{config.REFERRAL_BONUS_REFEREE_EXTRA} kredit bonus join).\n\n"
        f"Link referral kau:\n{link}\n\n"
        f"📊 Setakat ni: {count} kawan dah join, kau dah dapat {total_earned} kredit dari referral."
    )


async def send_topup_menu(reply_func):
    await reply_func(
        "Pilih pakej kredit yang kau nak beli 👇\n(Bayaran selamat melalui ToyyibPay - FPX/Kad)",
        reply_markup=topup_keyboard(),
    )


async def do_reset(user_id: int, reply_func):
    user_histories.pop(user_id, None)
    await reply_func("Ok, chat history dah clear. Mula baru! 🆕")


async def credits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await send_credits_info(user.id, user.username, update.message.reply_text)


async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await send_referral_info(user.id, user.username, context.bot.username, update.message.reply_text)


async def topup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_topup_menu(update.message.reply_text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_reset(update.effective_user.id, update.message.reply_text)


EXPORT_FORMATS = {
    "pdf": {
        "generator": lambda title, body: document_export.text_to_pdf_bytes(title, body),
        "filename": "studybot_nota.pdf",
        "caption": "📄 Ni PDF dari jawapan terakhir aku!",
        "loading": "Sedang jana fail PDF...",
        "label": "PDF",
    },
    "word": {
        "generator": lambda title, body: document_export.text_to_docx_bytes(title, body),
        "filename": "studybot_nota.docx",
        "caption": "📄 Ni fail Word dari jawapan terakhir aku!",
        "loading": "Sedang jana fail Word...",
        "label": "Word",
    },
    "ppt": {
        "generator": lambda title, body: document_export.text_to_pptx_bytes(title, body),
        "filename": "studybot_nota.pptx",
        "caption": "📊 Ni PowerPoint dari jawapan terakhir aku!",
        "loading": "Sedang jana slide PowerPoint...",
        "label": "PowerPoint",
    },
    "excel": {
        "generator": lambda title, body: document_export.text_to_xlsx_bytes(title, body),
        "filename": "studybot_nota.xlsx",
        "caption": "📈 Ni Excel dari jawapan terakhir aku!",
        "loading": "Sedang jana fail Excel...",
        "label": "Excel",
    },
}


async def do_export(user, context: ContextTypes.DEFAULT_TYPE, fmt: str, args: list[str], reply_text_func, reply_document_func):
    user_id = user.id
    spec = EXPORT_FORMATS[fmt]
    topic = " ".join(args) if args else None

    if topic:
        # student bagi topik terus (contoh "/ppt kitaran hujan") — jana kandungan baru,
        # ni guna 1 kredit sebab panggil AI macam soalan biasa
        await database.ensure_user(user_id, user.username or user.first_name, config.FREE_STARTING_CREDITS)
        if not await database.try_deduct_credit(user_id, amount=1):
            await reply_text_func(
                "⚠️ Kredit kau dah habis!\n\nGuna /topup untuk tambah kredit dan sambung belajar 📚",
                reply_markup=topup_keyboard(),
            )
            return

        await reply_text_func(f"Sedang jana kandungan pasal '{topic}'...")
        try:
            prompt = (
                f"Sediakan nota/kandungan yang lengkap, tersusun, dan senang difahami pasal topik: "
                f"{topic}. Sesuai untuk dijadikan slide pembelajaran atau dokumen rujukan student."
            )
            response = generate_with_fallback([{"role": "user", "parts": [{"text": prompt}]}])
            answer = response.text or None
            if not answer:
                await database.add_credits(user_id, 1)
                await reply_text_func("Alamak, tak dapat jana kandungan. Cuba lagi ye 🙏")
                return

            history = user_histories.get(user_id, [])
            history.append({"role": "user", "content": topic})
            history.append({"role": "assistant", "content": answer})
            user_histories[user_id] = history[-MAX_HISTORY_MESSAGES:]
        except Exception as e:
            logger.error(f"Gagal generate kandungan untuk export: {e}")
            await database.add_credits(user_id, 1)
            await reply_text_func("Alamak, gagal jana kandungan. Cuba lagi sekejap ye 🙏")
            return
    else:
        answer = get_last_ai_answer(user_id)
        if not answer:
            await reply_text_func(
                f"Tak ada jawapan lagi untuk export. Tanya soalan dulu, ATAU terus guna "
                f"/{fmt} <topik> untuk jana terus, contoh:\n/{fmt} kitaran hujan"
            )
            return

    await reply_text_func(spec["loading"])
    try:
        file_bytes = spec["generator"]("StudyBot - Nota", answer)
        await reply_document_func(
            document=io.BytesIO(file_bytes),
            filename=spec["filename"],
            caption=spec["caption"],
        )
    except Exception as e:
        logger.error(f"Gagal generate {spec['label']}: {e}")
        await reply_text_func(f"Alamak, gagal jana fail {spec['label']}. Cuba lagi sekejap ye 🙏")


async def export_pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_export(update.effective_user, context, "pdf", context.args, update.message.reply_text, update.message.reply_document)


async def export_word_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_export(update.effective_user, context, "word", context.args, update.message.reply_text, update.message.reply_document)


async def export_ppt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_export(update.effective_user, context, "ppt", context.args, update.message.reply_text, update.message.reply_document)


async def export_excel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_export(update.effective_user, context, "excel", context.args, update.message.reply_text, update.message.reply_document)


TERMS_TEXT = """📜 TERMA & SYARAT + DASAR PRIVASI (StudyBot)

1. TENTANG STUDYBOT
StudyBot ialah bot bantuan pembelajaran AI untuk student — jawab soalan, tolong assignment, ringkas nota, dan analyze gambar/PDF/video. Bot ni alat bantu belajar, bukan pengganti usaha kau sendiri — guna dengan bijak untuk faham, bukan sekadar salin jawapan.

2. SISTEM KREDIT & BAYARAN
- 1 kredit = 1 soalan/mesej/fail yang diproses
- Kredit dibeli melalui ToyyibPay (FPX/Kad) — pembayaran diproses oleh ToyyibPay, StudyBot tak simpan detail bank/kad kau
- Kredit yang dibeli TIDAK boleh ditukar ke wang tunai, dan tiada tempoh luput melainkan dinyatakan lain

3. DASAR REFUND
- Kalau kredit tak masuk selepas bayaran berjaya (disebabkan bug/error teknikal), hubungi /support serta-merta dengan bukti pembayaran — kami akan siasat & selesaikan
- Refund wang tunai dipertimbangkan kes demi kes untuk pembayaran yang gagal diproses sepenuhnya
- Tiada refund untuk kredit yang sudah digunakan

4. DATA & PRIVASI (selaras PDPA Malaysia)
- Kami simpan: Telegram User ID, username, baki kredit, sejarah transaksi
- Chat/soalan kau dihantar ke Google Gemini untuk diproses — tertakluk pada dasar privasi Google
- Data digunakan semata-mata untuk operasi bot (kredit, sokongan, penambahbaikan) — TIDAK dijual ke pihak ketiga
- Nak padam data kau? Hubungi /support

5. HAD TANGGUNGJAWAB
- AI boleh membuat kesilapan — sentiasa semak & fahami jawapan, jangan hantar terus tanpa verify (terutama untuk assignment dinilai)
- StudyBot disediakan "as-is" tanpa jaminan ketepatan 100%

6. HUBUNGI KAMI
Sebarang isu, pertanyaan, atau bantahan — guna /support, kami akan reply secepat mungkin."""


async def send_terms(reply_func):
    for i in range(0, len(TERMS_TEXT), 4000):
        await reply_func(TERMS_TEXT[i : i + 4000])


async def terms_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_terms(update.message.reply_text)


def help_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💳 Kredit Saya", callback_data="help:credits"),
         InlineKeyboardButton("💰 Topup", callback_data="help:topup")],
        [InlineKeyboardButton("🎁 Referral", callback_data="help:referral"),
         InlineKeyboardButton("🔄 Reset Chat", callback_data="help:reset")],
        [InlineKeyboardButton("📄 Export PDF", callback_data="help:export_pdf"),
         InlineKeyboardButton("📝 Export Word", callback_data="help:export_word")],
        [InlineKeyboardButton("📊 Export PPT", callback_data="help:export_ppt"),
         InlineKeyboardButton("📈 Export Excel", callback_data="help:export_excel")],
        [InlineKeyboardButton("📜 Terma & Privasi", callback_data="help:terms"),
         InlineKeyboardButton("🆘 Hubungi Admin", callback_data="help:support")],
    ]
    return InlineKeyboardMarkup(buttons)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Menu StudyBot\n\n"
        "Klik butang bawah ni untuk akses cepat, atau taip terus soalan kau, "
        "atau hantar gambar/PDF/video untuk aku analyze! 📷📄🎥\n\n"
        "Command penuh: /start /credits /topup /referral /pdf /word /ppt /excel /reset /terms /support",
        reply_markup=help_keyboard(),
    )


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    user = query.from_user
    reply_func = query.message.reply_text

    if action == "credits":
        await send_credits_info(user.id, user.username, reply_func)
    elif action == "topup":
        await send_topup_menu(reply_func)
    elif action == "referral":
        await send_referral_info(user.id, user.username, context.bot.username, reply_func)
    elif action == "reset":
        await do_reset(user.id, reply_func)
    elif action == "terms":
        await send_terms(reply_func)
    elif action == "support":
        await reply_func(
            "Nak hubungi admin? Taip command ni dengan penerangan masalah kau, contoh:\n"
            "/support kredit saya tak masuk lepas bayar tadi"
        )
    elif action.startswith("export_"):
        fmt = action.split("_", 1)[1]
        await do_export(user, context, fmt, [], reply_func, query.message.reply_document)


def ai_draft_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🤖 AI Suggest Reply", callback_data=f"aidraft:{ticket_id}")]]
    )


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message_text = " ".join(context.args) if context.args else ""

    if not message_text.strip():
        await update.message.reply_text(
            "Sila terangkan masalah/soalan kau lepas command ni, contoh:\n"
            "/support kredit saya tak masuk lepas bayar tadi"
        )
        return

    ticket_id = await database.create_support_ticket(user.id, user.username or user.first_name, message_text)

    await update.message.reply_text(
        f"✅ Pesanan kau dah dihantar kepada admin (Ticket #{ticket_id}). "
        "Kami akan reply secepat mungkin!"
    )

    if not config.ADMIN_USER_IDS:
        logger.warning("Tiada ADMIN_USER_IDS di-set — support message tak dapat dihantar ke sesiapa.")
        return

    forward_text = (
        f"📩 Support Ticket #{ticket_id}\n\n"
        f"Dari: {user.first_name} (@{user.username or 'tiada_username'})\n"
        f"User ID: {user.id}\n\n"
        f"Mesej:\n{message_text}\n\n"
        f"— Reply: /reply {user.id} <jawapan kau>\n"
        f"— Lepas settle: /resolve {ticket_id}"
    )
    for admin_id in config.ADMIN_USER_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=forward_text, reply_markup=ai_draft_keyboard(ticket_id)
            )
        except Exception as e:
            logger.error(f"Gagal hantar support ticket ke admin {admin_id}: {e}")


async def aidraft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin klik butang 'AI Suggest Reply' — AI generate cadangan jawapan (bukan auto-hantar,
    admin kena review & guna /reply untuk hantar sebenar)."""
    query = update.callback_query
    caller_id = query.from_user.id

    if caller_id not in config.ADMIN_USER_IDS:
        await query.answer("Command ni untuk admin je 🙅", show_alert=True)
        return

    await query.answer("Sedang jana draft...")

    ticket_id = int(query.data.split(":", 1)[1])
    ticket = await database.get_ticket(ticket_id)
    if ticket is None:
        await query.message.reply_text("Ticket tak jumpa dalam database.")
        return

    draft_prompt = f"""Kau tolong admin StudyBot draft jawapan support untuk student.

Mesej student: "{ticket['message']}"

Tulis SATU draft jawapan yang mesra, membantu, dan profesional dalam Bahasa Malaysia (2-4 ayat je).
Kalau isu berkaitan payment/kredit yang tak masuk, minta student hantar bukti pembayaran
(screenshot resit) tanpa janji spesifik bila akan settle — admin perlu verify dulu.
JANGAN janji refund/kredit tambahan secara automatik — itu keputusan admin, bukan AI.
Jawab dengan draft je, tanpa pembukaan/penutup tambahan."""

    try:
        response = generate_with_fallback([{"role": "user", "parts": [{"text": draft_prompt}]}])
        draft = response.text or "Tak dapat jana draft, cuba lagi."
    except Exception as e:
        logger.error(f"AI draft reply error: {e}")
        draft = "Gagal jana draft (ralat AI). Cuba lagi sekejap, atau taip manual."

    await query.message.reply_text(
        f"🤖 Cadangan draft (edit ikut suka sebelum hantar):\n\n{draft}\n\n"
        f"Hantar guna:\n/reply {ticket['user_id']} <paste/edit draft di atas>"
    )


async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin je boleh guna: /reply <user_id> <mesej>"""
    caller_id = update.effective_user.id
    if caller_id not in config.ADMIN_USER_IDS:
        await update.message.reply_text("Command ni untuk admin je 🙅")
        return

    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text("Format: /reply <user_id> <mesej>")
        return

    target_id = int(args[0])
    reply_message = " ".join(args[1:])

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💬 Balasan dari admin StudyBot:\n\n{reply_message}",
        )
        await update.message.reply_text("Done, mesej dah dihantar ke user.")
    except Exception as e:
        logger.error(f"Gagal hantar reply ke user {target_id}: {e}")
        await update.message.reply_text(f"Gagal hantar mesej: {e}")


async def tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin je boleh guna: /tickets — senarai semua support ticket yang masih open,
    setiap satu dengan butang AI Suggest Reply."""
    caller_id = update.effective_user.id
    if caller_id not in config.ADMIN_USER_IDS:
        await update.message.reply_text("Command ni untuk admin je 🙅")
        return

    tickets = await database.get_open_tickets()
    if not tickets:
        await update.message.reply_text("Takda ticket open buat masa ni. Semua settle! ✅")
        return

    await update.message.reply_text(f"📋 {len(tickets)} ticket masih OPEN:")
    for t in tickets:
        text = (
            f"#{t['id']} — {t['username'] or t['user_id']} ({t['created_at'].strftime('%d/%m %H:%M')})\n"
            f"{t['message']}\n\n"
            f"/reply {t['user_id']} <jawapan> | /resolve {t['id']}"
        )
        await update.message.reply_text(text, reply_markup=ai_draft_keyboard(t["id"]))


async def resolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin je boleh guna: /resolve <ticket_id>"""
    caller_id = update.effective_user.id
    if caller_id not in config.ADMIN_USER_IDS:
        await update.message.reply_text("Command ni untuk admin je 🙅")
        return

    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("Format: /resolve <ticket_id>")
        return

    ticket_id = int(args[0])
    ticket = await database.resolve_ticket(ticket_id)
    if ticket is None:
        await update.message.reply_text("Ticket tak wujud atau dah resolved sebelum ni.")
        return

    await update.message.reply_text(f"✅ Ticket #{ticket_id} ditandakan resolved.")


async def addcredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin je boleh guna: /addcredits <user_id> <amount>"""
    caller_id = update.effective_user.id
    if caller_id not in config.ADMIN_USER_IDS:
        await update.message.reply_text("Command ni untuk admin je 🙅")
        return

    args = context.args
    if len(args) != 2 or not args[0].isdigit() or not args[1].lstrip("-").isdigit():
        await update.message.reply_text("Format: /addcredits <user_id> <amount>")
        return

    target_id, amount = int(args[0]), int(args[1])
    await database.add_credits(target_id, amount)
    new_balance = await database.get_credits(target_id)
    await update.message.reply_text(f"Done. User {target_id} baki kredit sekarang: {new_balance}")

    try:
        await context.bot.send_message(
            chat_id=target_id, text=f"🎁 Kau dapat {amount} kredit dari admin! Baki sekarang: {new_balance}"
        )
    except Exception:
        pass  # user mungkin belum start chat dengan bot


# ---------- Callback Query (topup button) ----------

async def topup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    package_id = query.data.split(":", 1)[1]
    package = config.get_package(package_id)
    if not package:
        await query.edit_message_text("Pakej tak dijumpai, cuba /topup semula.")
        return

    user_id = query.from_user.id
    await query.edit_message_text(f"Sedang jana link pembayaran untuk {package['name']}...")

    try:
        bill = await payment.create_bill(user_id, package)
        await database.create_transaction(user_id, bill["bill_code"], package)
        await query.message.reply_text(
            f"💳 Pakej: {package['name']} — {package['credits']} kredit (RM{package['price_myr']:.2f})\n\n"
            f"Klik link untuk bayar:\n{bill['url']}\n\n"
            "Kredit akan masuk automatik lepas payment berjaya ✅"
        )
    except Exception as e:
        logger.error(f"Gagal create bill: {e}")
        await query.message.reply_text("Alamak, gagal jana link pembayaran. Cuba lagi sekejap ye 🙏")


# ---------- Media Handling (gambar / PDF / video) ----------

async def process_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    mime_type: str,
    default_prompt: str,
):
    """Fungsi kongsi untuk handle gambar/PDF/video — download dari Telegram,
    hantar ke Gemini sebagai inline data, tolak 1 kredit macam mesej teks biasa."""
    user = update.effective_user
    user_id = user.id

    if not check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Woah, slow down sikit! Max {RATE_LIMIT_MAX_REQUESTS} request per minit. "
            "Cuba lagi sekejap ye."
        )
        return

    await database.ensure_user(user_id, user.username or user.first_name, config.FREE_STARTING_CREDITS)

    if not await database.try_deduct_credit(user_id, amount=1):
        await update.message.reply_text(
            "⚠️ Kredit kau dah habis!\n\nGuna /topup untuk tambah kredit dan sambung belajar 📚",
            reply_markup=topup_keyboard(),
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    user_caption = (update.message.caption or "").strip() or default_prompt

    try:
        tg_file = await context.bot.get_file(file_id)

        if tg_file.file_size and tg_file.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await database.add_credits(user_id, 1)  # refund, bukan salah user
            await update.message.reply_text(
                f"⚠️ Fail ni terlalu besar (max {MAX_FILE_SIZE_MB}MB untuk bot Telegram). "
                "Cuba compress atau hantar bahagian yang lebih kecil ye."
            )
            return

        file_bytes = bytes(await tg_file.download_as_bytearray())
        b64_data = base64.b64encode(file_bytes).decode("utf-8")

        contents = [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": b64_data}},
                    {"text": user_caption},
                ],
            }
        ]

        response = generate_with_fallback(contents)
        reply_text = response.text or "Maaf, tak dapat proses fail ni. Cuba hantar semula."

    except Exception as e:
        logger.error(f"Gemini media processing error: {e}")
        reply_text = "Alamak, ada masalah nak proses fail kau. Cuba lagi sekejap ye 🙏"
        await database.add_credits(user_id, 1)  # refund

    # simpan dalam history supaya /pdf & /word boleh export jawapan dari fail juga
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": f"[fail: {mime_type}] {user_caption}"})
    history.append({"role": "assistant", "content": reply_text})
    user_histories[user_id] = history[-MAX_HISTORY_MESSAGES:]

    remaining = await database.get_credits(user_id)
    footer = f"\n\n— 💳 baki kredit: {remaining}"

    for i in range(0, len(reply_text), 4000):
        chunk = reply_text[i : i + 4000]
        is_last = i + 4000 >= len(reply_text)
        await update.message.reply_text(chunk + (footer if is_last else ""))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Telegram hantar beberapa saiz — ambil resolusi paling tinggi (last dalam list)
    photo = update.message.photo[-1]
    await process_media(
        update, context, photo.file_id, "image/jpeg",
        default_prompt=(
            "Ni gambar nota/soalan student. Baca dan jawab/terangkan ikut apa yang "
            "sesuai — kalau soalan, pandu step-by-step; kalau nota, boleh ringkaskan "
            "kalau nampak sesuai."
        ),
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    mime_type = doc.mime_type or "application/octet-stream"

    # Gemini paling stabil untuk PDF; jenis lain (docx, pptx, dll) mungkin tak disokong penuh
    if mime_type != "application/pdf":
        await update.message.reply_text(
            "⚠️ Buat masa ni bot ni sokong penuh untuk fail PDF je. "
            "Untuk jenis fail lain, cuba export/save jadi PDF dulu ye."
        )
        return

    await process_media(
        update, context, doc.file_id, mime_type,
        default_prompt=(
            "Ni fail PDF nota/bahan belajar student. Ringkaskan isi penting dalam bentuk "
            "bullet points yang senang diingati untuk exam, kecuali student minta benda lain "
            "dalam caption."
        ),
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    mime_type = video.mime_type or "video/mp4"

    await update.message.reply_text("🎥 Video diterima, sedang proses (mungkin ambil sedikit masa lebih lama)...")

    await process_media(
        update, context, video.file_id, mime_type,
        default_prompt=(
            "Ni video (contoh rakaman kelas/lecture) yang student hantar. Terangkan/ringkaskan "
            "isi penting video ni, kecuali student minta benda lain dalam caption."
        ),
    )


# ---------- Main Text Message Handler ----------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_text = update.message.text

    if not check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Woah, slow down sikit! Max {RATE_LIMIT_MAX_REQUESTS} request per minit. "
            "Cuba lagi sekejap ye."
        )
        return

    await database.ensure_user(user_id, user.username or user.first_name, config.FREE_STARTING_CREDITS)

    if not await database.try_deduct_credit(user_id, amount=1):
        await update.message.reply_text(
            "⚠️ Kredit kau dah habis!\n\nGuna /topup untuk tambah kredit dan sambung belajar 📚",
            reply_markup=topup_keyboard(),
        )
        return

    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": user_text})
    history = history[-MAX_HISTORY_MESSAGES:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = generate_with_fallback(build_gemini_contents(history))
        reply_text = response.text or "Maaf, tak dapat jana jawapan. Cuba tanya lain cara."
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        reply_text = "Alamak, ada masalah nak proses request kau. Cuba lagi sekejap ye 🙏"
        # refund credit sebab request gagal, bukan salah user
        await database.add_credits(user_id, 1)

    history.append({"role": "assistant", "content": reply_text})
    user_histories[user_id] = history[-MAX_HISTORY_MESSAGES:]

    remaining = await database.get_credits(user_id)
    footer = f"\n\n— 💳 baki kredit: {remaining}"

    for i in range(0, len(reply_text), 4000):
        chunk = reply_text[i : i + 4000]
        is_last = i + 4000 >= len(reply_text)
        await update.message.reply_text(chunk + (footer if is_last else ""))


# ---------- Run bot + web server together ----------

async def main():
    await database.init_db()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("credits", credits_cmd))
    application.add_handler(CommandHandler("referral", referral_cmd))
    application.add_handler(CommandHandler("topup", topup_cmd))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("pdf", export_pdf_cmd))
    application.add_handler(CommandHandler("word", export_word_cmd))
    application.add_handler(CommandHandler("ppt", export_ppt_cmd))
    application.add_handler(CommandHandler("excel", export_excel_cmd))
    application.add_handler(CommandHandler("terms", terms_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("support", support_cmd))
    application.add_handler(CommandHandler("reply", reply_cmd))
    application.add_handler(CommandHandler("tickets", tickets_cmd))
    application.add_handler(CommandHandler("resolve", resolve_cmd))
    application.add_handler(CommandHandler("addcredits", addcredits_cmd))
    application.add_handler(CallbackQueryHandler(topup_callback, pattern=r"^topup:"))
    application.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help:"))
    application.add_handler(CallbackQueryHandler(aidraft_callback, pattern=r"^aidraft:"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    web_app = create_web_app(application.bot)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server (ToyyibPay callback) listening on port {port}")

    async with application:
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram bot polling started...")
        await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
