"""
Konfigurasi bot: pakej kredit, admin, dan free starting credits.
Semua boleh override guna environment variables kat Railway.
"""

import os

# Berapa kredit percuma bila user pertama kali /start
FREE_STARTING_CREDITS = int(os.environ.get("FREE_STARTING_CREDITS", "10"))

# Sistem referral: bonus untuk orang yang refer, dan extra bonus untuk kawan yang baru join
REFERRAL_BONUS_REFERRER = int(os.environ.get("REFERRAL_BONUS_REFERRER", "25"))
REFERRAL_BONUS_REFEREE_EXTRA = int(os.environ.get("REFERRAL_BONUS_REFEREE_EXTRA", "5"))

# Telegram user ID admin (boleh guna /addcredits untuk bagi kredit manual)
# Contoh set kat Railway: ADMIN_USER_IDS=123456789,987654321
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

# Pakej kredit untuk topup — edit ikut suka (harga dalam RM)
PACKAGES = [
    {"id": "starter", "name": "Starter", "credits": 50, "price_myr": 5.00},
    {"id": "popular", "name": "Popular", "credits": 120, "price_myr": 10.00},
    {"id": "power", "name": "Power", "credits": 300, "price_myr": 20.00},
]


def get_package(package_id: str):
    for p in PACKAGES:
        if p["id"] == package_id:
            return p
    return None
