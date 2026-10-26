"""Настройки приложения Discord Parser."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DP_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("DP_DB_PATH", DATA_DIR / "discord_parser.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

DISCORD_API = "https://discord.com/api/v10"

# Токен можно задать заранее через окружение (удобно для Docker/сервера).
ENV_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Discord отдаёт максимум 100 сообщений за запрос.
PAGE_SIZE = 100

# Сколько сообщений хранить в памяти при батч-вставке.
BATCH_SIZE = 500
