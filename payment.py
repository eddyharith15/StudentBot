"""
Wrapper untuk ToyyibPay API — create bill (payment link) & verify status.

Docs rasmi: https://toyyibpay.com/apireference/

Env vars diperlukan:
- TOYYIBPAY_SECRET_KEY   : dari dashboard ToyyibPay > User Profile
- TOYYIBPAY_CATEGORY_CODE: dari dashboard ToyyibPay > Category (create category dulu)
- TOYYIBPAY_BASE_URL     : https://toyyibpay.com (production) atau https://dev.toyyibpay.com (sandbox)
- PUBLIC_BASE_URL        : URL public app kau (contoh: https://studybot.up.railway.app)
"""

import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

TOYYIBPAY_BASE_URL = os.environ.get("TOYYIBPAY_BASE_URL", "https://toyyibpay.com").rstrip("/")
TOYYIBPAY_SECRET_KEY = os.environ["TOYYIBPAY_SECRET_KEY"]
TOYYIBPAY_CATEGORY_CODE = os.environ["TOYYIBPAY_CATEGORY_CODE"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")


async def create_bill(user_id: int, package: dict) -> dict:
    """Create satu ToyyibPay bill untuk pakej yang dipilih user.
    Return: {"bill_code": ..., "url": ...}
    """
    payload = {
        "userSecretKey": TOYYIBPAY_SECRET_KEY,
        "categoryCode": TOYYIBPAY_CATEGORY_CODE,
        "billName": f"StudyBot {package['name']}"[:30],
        "billDescription": f"{package['credits']} kredit StudyBot"[:100],
        "billPriceSetting": 1,
        "billPayorInfo": 1,
        "billAmount": str(int(round(package["price_myr"] * 100))),  # dalam sen
        "billReturnUrl": f"{PUBLIC_BASE_URL}/payment-return",
        "billCallbackUrl": f"{PUBLIC_BASE_URL}/toyyibpay-callback",
        "billExternalReferenceNo": f"u{user_id}-{package['id']}",
        "billTo": f"TelegramUser{user_id}",
        "billEmail": f"user{user_id}@studybot.local",
        "billPhone": "0100000000",
        "billSplitPayment": 0,
        "billPaymentChannel": 0,  # 0 = FPX + Credit Card
        "billChargeToCustomer": 1,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{TOYYIBPAY_BASE_URL}/index.php/api/createBill", data=payload
        ) as resp:
            data = await resp.json(content_type=None)

    if not data or not isinstance(data, list) or "BillCode" not in data[0]:
        logger.error(f"ToyyibPay createBill gagal: {data}")
        raise RuntimeError(f"ToyyibPay createBill gagal: {data}")

    bill_code = data[0]["BillCode"]
    return {"bill_code": bill_code, "url": f"{TOYYIBPAY_BASE_URL}/{bill_code}"}


async def get_bill_transactions(bill_code: str) -> list:
    """Semak status sebenar bill terus dari ToyyibPay (jangan percaya callback data
    mentah — verify semula server-side untuk elak spoofing)."""
    payload = {"billCode": bill_code}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{TOYYIBPAY_BASE_URL}/index.php/api/getBillTransactions", data=payload
        ) as resp:
            data = await resp.json(content_type=None)
    return data or []


async def is_bill_paid(bill_code: str) -> bool:
    transactions = await get_bill_transactions(bill_code)
    for txn in transactions:
        # billpaymentStatus: "1" = success, "2" = pending, "3" = fail
        if txn.get("billpaymentStatus") == "1":
            return True
    return False
