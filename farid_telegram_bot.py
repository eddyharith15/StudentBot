#!/usr/bin/env python3
"""Farid Bot — Marketing Storytelling Agent (Telegram)"""
import os
TOKEN = os.getenv("FARID_BOT_TOKEN", "TOKEN_FARID")
CHANNEL = "-5576470481"
THREADS = "@StudentBotAI"

# Storytelling marketing posts 3x daily: 8am, 2pm, 8pm
# Non-robot tone, student perspective
print(f"Farid Bot ready. Token: {TOKEN[:10]}..., Channel: {CHANNEL}, Threads: {THREADS}")
