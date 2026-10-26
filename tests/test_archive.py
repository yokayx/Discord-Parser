"""Тесты архива: формат записей, файлы по каналам, автосохранение после скана."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="dp-arch-")
os.environ["DP_DB_PATH"] = str(Path(TMP) / "test.db")
os.environ["DP_ARCHIVE_DIR"] = str(Path(TMP) / "archive")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import archive, db  # noqa: E402
from app.scanner import Scanner  # noqa: E402

GUILD = "910000000000000001"


def ts(text: str) -> int:
    # 26.10.2026 19:52 по локальному времени сервера
    from datetime import datetime

    return int(datetime(2026, 10, 26, 19, 52).timestamp() * 1000)


def msg_row(mid: str, channel: str, content: str, author: str = "800000000000000001") -> tuple:
    return (
        mid, GUILD, channel, author, content, ts(content), None, 0, None,
        "[]", "[]", "[]", "[]", 0, "[]", content.lower(),
    )


async def prepare() -> None:
    await db.init_db()
    await db.remove_guild_data(GUILD)
    await db.upsert_guild({"id": GUILD, "name": "Тестовый Сервер"})
    await db.upsert_channels(
        GUILD,
        [
            {"id": "710000000000000001", "name": "general", "type": 0, "position": 0},
            {"id": "710000000000000002", "name": "флудилка", "type": 0, "position": 1},
        ],
    )
    await db.ensure_scan_states(GUILD, await db.list_channels(GUILD))
    await db.upsert_authors(
        [
            {"id": "800000000000000001", "username": "nightowl", "global_name": "Ночная Сова"},
            {"id": "800000000000000002", "username": "bot", "global_name": "Helper", "bot": True},
        ]
    )
    await db.insert_messages(
        [
            msg_row("720000000000000001", "710000000000000001", "привет"),
            msg_row("720000000000000002", "710000000000000001", "как дела?", "800000000000000002"),
            msg_row("720000000000000003", "710000000000000002", "тут кириллица в имени"),
        ]
    )


def test_format_message_signature_and_separator() -> None:
    options = archive.ArchiveOptions()
    row = {
        "id": "1",
        "timestamp": ts(""),
        "author_name": "Ночная Сова",
        "username": "nightowl",
        "content": "привет",
        "attachments": "[]",
        "bot": 0,
    }
    line = archive.format_message(row, options)
    assert line.startswith("[26.10.2026 19:52] Ночная Сова (@nightowl): привет")

    block = archive.format_message(row, archive.ArchiveOptions(format="block"))
    assert block == "[26.10.2026 19:52] Ночная Сова (@nightowl)\nпривет"


def test_attachments_and_empty_content() -> None:
    row = {
        "id": "2",
        "timestamp": ts(""),
        "author_name": "Сова",
        "username": "nightowl",
        "content": "",
        "attachments": '[{"filename": "photo.png", "url": "https://cdn/x"}]',
        "bot": 0,
    }
    line = archive.format_message(row, archive.ArchiveOptions())
    assert "(без текста)" in line
    assert "[вложения: photo.png]" in line


def test_build_archive_files_per_channel() -> None:
    async def run() -> None:
        await prepare()
        result = await archive.build_guild_archive(GUILD, archive.ArchiveOptions())
        target = Path(result.path)
        assert target.exists()
        names = {f["file"] for f in result.files}
        assert names == {"general.txt", "флудилка.txt", "_authors.txt"}
        assert result.messages == 3
        assert (target / "_index.txt").exists()

        authors = (target / "_authors.txt").read_text(encoding="utf-8")
        assert "Ночная Сова (@nightowl): 2 сообщ." in authors
        assert "Helper (@bot) [бот]: 1 сообщ." in authors

        text = (target / "general.txt").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        assert len(lines) == 2
        assert lines[0].startswith("[26.10.2026 19:52] Ночная Сова (@nightowl): привет")
        assert "[26.10.2026 19:52] Helper (@bot, бот)" in lines[1]

    asyncio.run(run())


def test_separator_and_time_presets() -> None:
    async def run() -> None:
        await prepare()
        options = archive.ArchiveOptions.from_dict(
            {"separator": "\\n\\n", "time_format": "iso", "format": "line"}
        )
        result = await archive.build_guild_archive(GUILD, options)
        text = (Path(result.path) / "general.txt").read_text(encoding="utf-8")
        assert options.sep == "\n\n"
        assert "2026-10-26T19:52" in text
        # между двумя сообщениями канала ровно пустая строка
        body = text.split("\n\n")
        assert any("привет" in part for part in body)
        assert any("как дела?" in part for part in body)

    asyncio.run(run())


def test_zip_download() -> None:
    async def run() -> None:
        await prepare()
        await archive.build_guild_archive(GUILD)
        path = archive.build_zip(GUILD, "Тестовый Сервер")
        assert path.exists()
        import zipfile

        with zipfile.ZipFile(path) as zf:
            assert any(n.endswith("general.txt") for n in zf.namelist())

    asyncio.run(run())


def test_unsafe_names_are_slugified() -> None:
    assert archive.slugify("канал/с*плохими:символами?") == "канал_с_плохими_символами"
    assert archive.slugify("") == "channel"


def test_auto_archive_after_scan() -> None:
    async def run() -> None:
        await prepare()

        class FakeClient:
            auth_mode = "bot"

            async def get_guild(self, guild_id):
                return {"id": guild_id, "name": "Тестовый Сервер"}

            async def get_active_threads(self, guild_id):
                return []

            async def get_archived_threads(self, channel_id):
                return []

            async def get_guild_members(self, guild_id, limit=1000):
                return []

            async def get_messages(self, channel_id, *, before=None, after=None, limit=100):
                return []

        events: list[dict] = []

        async def broadcast(payload):
            events.append(payload)

        await db.reset_scan_state(GUILD)
        await archive.set_auto(True)
        scanner = Scanner(FakeClient(), broadcast)  # type: ignore[arg-type]
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        assert scanner.task is not None
        await asyncio.wait_for(scanner.task, 20)

        assert scanner.state["status"] == "done"
        saved = scanner.state.get("archive")
        assert saved and saved["messages"] == 3
        assert any(e["type"] == "archive" for e in events)

        # автосохранение можно выключить
        await archive.set_auto(False)
        assert (await archive.auto_enabled()) is False

    asyncio.run(run())
