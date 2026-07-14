"""
Web server kecil (aiohttp) untuk terima callback ToyyibPay bila payment berjaya.
Jalan sekali dengan Telegram bot (polling) dalam process yang sama — lihat bot.py.
"""

import logging
from aiohttp import web

import database
import payment

logger = logging.getLogger(__name__)


def create_web_app(telegram_bot) -> web.Application:
    """telegram_bot: instance `application.bot` supaya boleh hantar notifikasi
    terus ke user Telegram lepas payment berjaya."""

    app = web.Application()
    app["telegram_bot"] = telegram_bot

    async def health(request):
        return web.Response(text="StudyBot OK")

    async def payment_return(request):
        # Page ringkas lepas user balik dari ToyyibPay (bukan sumber kebenaran —
        # kredit sebenar ditambah melalui callback, bukan page ni)
        return web.Response(
            text="Terima kasih! Payment sedang diproses. Boleh tutup tab ni dan balik ke Telegram.",
            content_type="text/plain",
        )

    async def toyyibpay_callback(request):
        try:
            data = await request.post()
        except Exception:
            data = {}

        bill_code = data.get("billcode") or data.get("BillCode")
        status = data.get("status")  # "1" = success (tapi kita verify semula, jangan percaya terus)

        logger.info(f"ToyyibPay callback diterima: billcode={bill_code} status={status}")

        if not bill_code:
            return web.Response(status=400, text="missing billcode")

        # SENTIASA verify balik dengan ToyyibPay API — jangan terus percaya callback payload,
        # sebab endpoint ni public dan boleh kena spoof kalau kita percaya je apa yang dihantar.
        try:
            paid = await payment.is_bill_paid(bill_code)
        except Exception as e:
            logger.error(f"Gagal verify bill {bill_code}: {e}")
            return web.Response(status=500, text="verify failed")

        if not paid:
            logger.info(f"Bill {bill_code} belum/tidak berjaya dibayar.")
            return web.Response(text="ok")

        txn = await database.mark_transaction_paid(bill_code)
        if txn is None:
            # dah diproses sebelum ni (idempotent) atau bill tak wujud dalam DB kita
            return web.Response(text="ok")

        # Notify user dalam Telegram
        bot = request.app["telegram_bot"]
        try:
            new_balance = await database.get_credits(txn["user_id"])
            await bot.send_message(
                chat_id=txn["user_id"],
                text=(
                    f"✅ Payment berjaya!\n\n"
                    f"Pakej: {txn['package_name']} (+{txn['credits']} kredit)\n"
                    f"Baki kredit sekarang: {new_balance}\n\n"
                    f"Terima kasih! Boleh terus tanya soalan kau 📚"
                ),
            )
        except Exception as e:
            logger.error(f"Gagal hantar notifikasi Telegram: {e}")

        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/payment-return", payment_return)
    app.router.add_post("/toyyibpay-callback", toyyibpay_callback)

    return app
