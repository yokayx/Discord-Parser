"""Наполняет базу демо-данными (без обращения к Discord).

Полезно, чтобы посмотреть панель до подключения реального токена:

    python scripts/seed_demo.py
    ./run.sh
"""
from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import DB_PATH  # noqa: E402

GUILD_ID = "100000000000000001"
AUTHORS = [
    ("200000000000000001", "nightowl", "Ночная Сова", False),
    ("200000000000000002", "kettle", "Чайник", False),
    ("200000000000000003", "dev_masha", "Маша", False),
    ("200000000000000004", "pixelpanda", "Панда", False),
    ("200000000000000005", "helper_bot", "Helper", True),
    ("200000000000000006", "lurker", "Молчун", False),
    ("200000000000000007", "arta", "Арта", False),
]
CHANNELS = [
    ("300000000000000001", "general", 0),
    ("300000000000000002", "флудилка", 0),
    ("300000000000000003", "dev-chat", 0),
    ("300000000000000004", "мемы", 0),
    ("300000000000000005", "объявления", 5),
]
PHRASES = [
    "привет всем, кто ещё не спит?",
    "залил новую сборку, тестируйте",
    "кто-нибудь понял, почему падает на windows?",
    "мем дня подъехал",
    "напоминаю про созвон в 19:00",
    "спасибо, помогло!",
    "а есть документация по api?",
    "сделал рефакторинг, стало в два раза быстрее",
    "го в голосовой",
    "почему у меня опять rate limit",
]


async def main() -> None:
    await db.init_db()
    print(f"База: {DB_PATH}")

    await db.upsert_guild(
        {
            "id": GUILD_ID,
            "name": "Демо-сервер",
            "icon": None,
            "description": "Данные сгенерированы scripts/seed_demo.py",
            "owner_id": AUTHORS[0][0],
            "approximate_member_count": len(AUTHORS),
        }
    )
    await db.upsert_channels(
        GUILD_ID,
        [
            {
                "id": cid,
                "name": name,
                "type": ctype,
                "position": index,
                "topic": f"демо-канал #{name}",
            }
            for index, (cid, name, ctype) in enumerate(CHANNELS)
        ],
    )
    channels = await db.list_channels(GUILD_ID)
    await db.ensure_scan_states(GUILD_ID, channels)

    await db.upsert_authors(
        [
            {
                "id": aid,
                "username": username,
                "global_name": display,
                "discriminator": "0",
                "bot": bot,
                "avatar": None,
            }
            for aid, username, display, bot in AUTHORS
        ]
    )

    now = time.time()
    rows = []
    for i in range(1500):
        author = random.choice(AUTHORS[:4] * 3 + AUTHORS[4:])
        channel_id, channel_name, channel_type = random.choice(CHANNELS)
        ts = int((now - random.randint(0, 45 * 86400)) * 1000)
        content = random.choice(PHRASES) if random.random() > 0.05 else ""
        attachments = (
            '[{"id":"1","filename":"image.png","url":"https://cdn.discordapp.com/attachments/1/1/image.png","proxy_url":"https://media.discordapp.net/attachments/1/1/image.png","size":204800}]'
            if random.random() < 0.08
            else "[]"
        )
        reactions = (
            '[{"emoji":{"name":"🔥"},"count":%d}]' % random.randint(1, 9)
            if random.random() < 0.2
            else "[]"
        )
        rows.append(
            (
                str(400000000000000000 + i),
                GUILD_ID,
                channel_id,
                author[0],
                content,
                ts,
                None,
                0,
                None,
                attachments,
                "[]",
                reactions,
                "[]",
                0,
                "[]",
            )
        )

    rows.sort(key=lambda r: r[5])
    await db.insert_messages(rows)
    await db.touch_message_aggregates(rows)
    for cid, cname, ctype in CHANNELS:
        count = sum(1 for r in rows if r[2] == cid)
        await db.update_scan_state(
            cid,
            status="done",
            complete=1,
            fetched=count,
            oldest_id=rows[0][0],
            newest_id=rows[-1][0],
            last_run_at=int(now),
        )
    print(f"Добавлено сообщений: {len(rows)}")
    print("Готово. Запустите ./run.sh и откройте http://localhost:8000")


if __name__ == "__main__":
    asyncio.run(main())
