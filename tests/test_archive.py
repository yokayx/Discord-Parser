"""Тесты ZIP-архива: структура «сервер → категории → каналы», формат записей."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import zipfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="dp-arch-")
os.environ["DP_DB_PATH"] = str(Path(TMP) / "test.db")
os.environ["DP_ARCHIVE_DIR"] = str(Path(TMP) / "archive")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import archive, db  # noqa: E402
from app.scanner import Scanner  # noqa: E402

GUILD = "910000000000000001"
CATEGORY_ID = "710000000000000009"


def ts() -> int:
    from datetime import datetime

    return int(datetime(2026, 10, 26, 19, 52).timestamp() * 1000)


def msg_row(mid: str, channel: str, content: str, author: str = "800000000000000001") -> tuple:
    return (
        mid, GUILD, channel, author, content, ts(), None, 0, None,
        "[]", "[]", "[]", "[]", 0, "[]", content.lower(),
    )


async def prepare() -> None:
    await db.init_db()
    await db.remove_guild_data(GUILD)
    await db.upsert_guild({"id": GUILD, "name": "Тестовый Сервер"})
    await db.upsert_channels(
        GUILD,
        [
            {"id": CATEGORY_ID, "name": "Текстовые каналы", "type": 4, "position": 0},
            {
                "id": "710000000000000001",
                "name": "general",
                "type": 0,
                "position": 1,
                "parent_id": CATEGORY_ID,
            },
            {
                "id": "710000000000000002",
                "name": "флудилка",
                "type": 0,
                "position": 2,
                "parent_id": CATEGORY_ID,
            },
            {"id": "710000000000000003", "name": "объявления", "type": 5, "position": 3},
            {"id": "710000000000000004", "name": "Голосовой", "type": 2, "position": 4},
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
            msg_row("720000000000000004", "710000000000000003", "объявление"),
        ]
    )


def test_format_message_signature_and_separator() -> None:
    options = archive.ArchiveOptions()
    row = {
        "id": "1",
        "timestamp": ts(),
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
        "timestamp": ts(),
        "author_name": "Сова",
        "username": "nightowl",
        "content": "",
        "attachments": '[{"filename": "photo.png", "url": "https://cdn/x"}]',
        "bot": 0,
    }
    line = archive.format_message(row, archive.ArchiveOptions())
    assert "(без текста)" in line
    assert "[вложения: photo.png]" in line


def test_zip_structure_server_category_channel() -> None:
    async def run() -> None:
        await prepare()
        result = await archive.build_guild_archive(GUILD, archive.ArchiveOptions())

        assert result.zip_name.endswith(".zip")
        assert Path(result.zip_path).exists()
        assert result.messages == 4
        # голосовой канал пропускается, категория не считается каналом
        assert result.skipped_channels == 0

        with zipfile.ZipFile(result.zip_path) as zf:
            names = set(zf.namelist())
            root = result.root_folder
            assert f"{root}/Текстовые_каналы/general.txt" in names
            assert f"{root}/Текстовые_каналы/флудилка.txt" in names
            assert f"{root}/объявления.txt" in names  # канал без категории — корень сервера
            assert f"{root}/_authors.txt" in names
            assert f"{root}/_index.txt" in names
            assert not any("Голосовой" in n for n in names)

            text = zf.read(f"{root}/Текстовые_каналы/general.txt").decode("utf-8")
            lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
            assert lines[0].startswith("[26.10.2026 19:52] Ночная Сова (@nightowl): привет")
            assert "Helper (@bot, бот)" in lines[1]

            index = zf.read(f"{root}/_index.txt").decode("utf-8")
            assert "[Текстовые_каналы]" in index
            assert "[Без категории]" in index

    asyncio.run(run())


def test_entries_and_preview_from_zip() -> None:
    async def run() -> None:
        await prepare()
        await archive.build_guild_archive(GUILD)
        info = await archive.archive_info(GUILD)
        assert info["exists"] is True
        assert info["bytes"] > 0

        folders = [e for e in info["entries"] if e["kind"] == "folder"]
        files = [e for e in info["entries"] if e["kind"] == "file"]
        assert any(e["name"] == "Текстовые_каналы" for e in folders)
        assert len(files) == 5  # 3 канала + авторы + индекс

        entry = next(e for e in files if e["name"] == "general.txt")
        data = await archive.read_entry(GUILD, entry["path"])
        assert "привет" in data["content"]
        assert data["path"] == entry["path"]

        # поиск по короткому имени тоже работает
        short = await archive.read_entry(GUILD, "general.txt")
        assert short["name"] == "general.txt"

    asyncio.run(run())


def test_separator_and_time_presets() -> None:
    async def run() -> None:
        await prepare()
        options = archive.ArchiveOptions.from_dict(
            {"separator": "\\n\\n", "time_format": "iso", "format": "line"}
        )
        result = await archive.build_guild_archive(GUILD, options)
        assert options.sep == "\n\n"

        with zipfile.ZipFile(result.zip_path) as zf:
            text = zf.read(f"{result.root_folder}/Текстовые_каналы/general.txt").decode("utf-8")
        assert "2026-10-26T19:52" in text
        body = [part for part in text.split("\n\n") if part.strip()]
        assert any("привет" in part for part in body)
        assert any("как дела?" in part for part in body)

    asyncio.run(run())


def test_default_separator_is_single_newline() -> None:
    async def run() -> None:
        await prepare()
        result = await archive.build_guild_archive(GUILD, archive.ArchiveOptions())
        with zipfile.ZipFile(result.zip_path) as zf:
            text = zf.read(f"{result.root_folder}/Текстовые_каналы/general.txt").decode("utf-8")
        body = text.split("\n\n", 1)[-1].rstrip("\n").splitlines()
        assert len(body) == 2  # два сообщения канала, каждое на своей строке

    asyncio.run(run())


def test_slug_and_transliteration() -> None:
    assert archive.slugify("канал/с*плохими:символами?") == "канал_с_плохими_символами"
    assert archive.slugify("") == "channel"
    assert archive.ascii_slug("Демо-сервер") == "Demo-server"
    assert archive.ascii_slug("") == "guild"
    assert archive.guild_zip_path(GUILD, "Тестовый Сервер").name.isascii()


def test_zip_filename_is_server_specific() -> None:
    a = archive.guild_zip_path("910000000000000001", "Сервер")
    b = archive.guild_zip_path("920000000000000002", "Сервер")
    assert a != b
    assert "0001" in a.name and "0002" in b.name


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
        assert saved and saved["messages"] == 4
        assert saved["zip_name"].endswith(".zip")
        assert any(e["type"] == "archive" for e in events)

        await archive.set_auto(False)
        assert (await archive.auto_enabled()) is False

    asyncio.run(run())
