"""
Database layer (Postgres via Neon) — simpan kredit user & rekod transaksi topup.
Guna asyncpg (async, sepadan dengan bot yang async) dengan connection pool.

Kenapa Postgres/Neon dan bukan SQLite:
- Neon free tier data PERMANENT (bukan trial), tak hilang bila service redeploy/restart
  macam filesystem container biasa.
- Sesuai untuk data penting macam kredit/duit user.
"""

import os
import logging
from typing import Optional
import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Buat connection pool & pastikan table wujud. Panggil sekali masa bot start."""
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                credits INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                bill_code TEXT UNIQUE NOT NULL,
                package_id TEXT,
                package_name TEXT,
                credits INTEGER,
                amount_myr NUMERIC(10, 2),
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                paid_at TIMESTAMPTZ
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referee_id BIGINT NOT NULL UNIQUE,
                bonus_given INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
            """
        )
    logger.info("Database initialized (Postgres/Neon).")


async def ensure_user(user_id: int, username: str, free_credits: int) -> bool:
    """Daftar user baru dengan free credits. Return True kalau user baru."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if row is None:
            await conn.execute(
                "INSERT INTO users (user_id, username, credits) VALUES ($1, $2, $3)",
                user_id, username, free_credits,
            )
            return True
        await conn.execute("UPDATE users SET username=$1 WHERE user_id=$2", username, user_id)
        return False


async def get_credits(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM users WHERE user_id=$1", user_id)
        return row["credits"] if row else 0


async def try_deduct_credit(user_id: int, amount: int = 1) -> bool:
    """Cuba tolak kredit atomically. Return True kalau berjaya (cukup kredit)."""
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET credits = credits - $1 WHERE user_id=$2 AND credits >= $1",
            amount, user_id,
        )
        # asyncpg execute() return string macam "UPDATE 1" / "UPDATE 0"
        return result.endswith(" 1")


async def add_credits(user_id: int, amount: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET credits = credits + $1 WHERE user_id=$2", amount, user_id
        )


async def create_transaction(user_id: int, bill_code: str, package: dict):
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO transactions
               (user_id, bill_code, package_id, package_name, credits, amount_myr, status)
               VALUES ($1, $2, $3, $4, $5, $6, 'pending')""",
            user_id, bill_code, package["id"], package["name"],
            package["credits"], package["price_myr"],
        )


async def get_transaction(bill_code: str):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM transactions WHERE bill_code=$1", bill_code)


async def mark_transaction_paid(bill_code: str):
    """Tandakan transaksi 'paid' & tambah kredit ke user, dalam satu DB transaction supaya
    atomic. Idempotent — selamat kalau callback ToyyibPay hantar berkali-kali."""
    async with _pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM transactions WHERE bill_code=$1 FOR UPDATE", bill_code
            )
            if row is None or row["status"] == "paid":
                return None  # tak wujud atau dah diproses sebelum ni

            await conn.execute(
                "UPDATE transactions SET status='paid', paid_at=NOW() WHERE bill_code=$1",
                bill_code,
            )
            await conn.execute(
                "UPDATE users SET credits = credits + $1 WHERE user_id=$2",
                row["credits"], row["user_id"],
            )
            return row


async def record_referral(referrer_id: int, referee_id: int, bonus_given: int) -> bool:
    """Rekod satu referral. Return True kalau berjaya (referee belum pernah direfer
    sebelum ni, dan referrer != referee). Return False kalau abuse/duplicate."""
    if referrer_id == referee_id:
        return False
    async with _pool.acquire() as conn:
        try:
            await conn.execute(
                """INSERT INTO referrals (referrer_id, referee_id, bonus_given)
                   VALUES ($1, $2, $3)""",
                referrer_id, referee_id, bonus_given,
            )
            return True
        except asyncpg.UniqueViolationError:
            # referee_id ni dah pernah direfer sebelum ni — elak abuse (contoh /start berkali-kali)
            return False


async def get_referral_count(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id=$1", user_id
        )
        return row["cnt"] if row else 0


async def get_stats() -> dict:
    """Kumpul angka ringkas untuk admin: jumlah user, user baru, kredit,
    transaksi/revenue, referral, dan tiket terbuka."""
    async with _pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        new_today = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at >= date_trunc('day', NOW())"
        )
        new_7d = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'"
        )
        total_credits = await conn.fetchval("SELECT COALESCE(SUM(credits), 0) FROM users")
        paid_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_myr), 0) AS total "
            "FROM transactions WHERE status='paid'"
        )
        total_referrals = await conn.fetchval("SELECT COUNT(*) FROM referrals")
        open_tickets = await conn.fetchval(
            "SELECT COUNT(*) FROM support_tickets WHERE status='open'"
        )
        return {
            "total_users": total_users,
            "new_today": new_today,
            "new_7d": new_7d,
            "total_credits": total_credits,
            "paid_tx_count": paid_row["cnt"],
            "paid_tx_total_myr": paid_row["total"],
            "total_referrals": total_referrals,
            "open_tickets": open_tickets,
        }


async def create_support_ticket(user_id: int, username: str, message: str) -> int:
    """Cipta support ticket baru, return ticket ID."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO support_tickets (user_id, username, message)
               VALUES ($1, $2, $3) RETURNING id""",
            user_id, username, message,
        )
        return row["id"]


async def get_open_tickets(limit: int = 20):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """SELECT * FROM support_tickets WHERE status='open'
               ORDER BY created_at ASC LIMIT $1""",
            limit,
        )


async def get_ticket(ticket_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1", ticket_id)


async def resolve_ticket(ticket_id: int):
    """Tandakan ticket sebagai resolved. Return row ticket (untuk tahu user_id dia)
    atau None kalau ticket tak wujud / dah resolved."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM support_tickets WHERE id=$1 AND status='open'", ticket_id
        )
        if row is None:
            return None
        await conn.execute(
            "UPDATE support_tickets SET status='resolved', resolved_at=NOW() WHERE id=$1",
            ticket_id,
        )
        return row
