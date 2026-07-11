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
