"""Тесты сканера на фейковом Discord-клиенте (без сети)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="dp-test-")
os.environ["DP_DB_PATH"] = str(Path(TMP) / "test.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.scanner import Scanner  # noqa: E402

GUILD = "900000000000000001"


def make_messages(channel_id: str, count: int) -> list[dict]:
    """Сообщения в формате Discord (по убыванию id, как отдаёт API)."""
    out = []
    for i in range(count):
        mid = str(900000000000000000 + (count - i))
        out.append(
            {
                "id": mid,
                "channel_id": channel_id,
                "content": f"message {i}",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "author": {"id": "800000000000000001", "username": "tester", "bot": False},
                "attachments": [],
                "embeds": [],
                "reactions": [],
                "mentions": [],
                "type": 0,
            }
        )
    return out


class FakeClient:
    auth_mode = "bot"

    def __init__(self, total: int = 250) -> None:
        self.messages = make_messages("700000000000000001", total)
        self.calls = 0
        self.forbid: set[str] = set()

    async def get_guild(self, guild_id: str) -> dict:
        return {"id": guild_id, "name": "Тестовый", "owner_id": "800000000000000001"}

    async def get_active_threads(self, guild_id: str) -> list[dict]:
        return []

    async def get_archived_threads(self, channel_id: str) -> list[dict]:
        return []

    async def get_guild_members(self, guild_id: str, limit: int = 1000) -> list[dict]:
        return []

    async def get_guild_channels(self, guild_id: str) -> list[dict]:
        return [{"id": "700000000000000001", "name": "general", "type": 0, "position": 0}]

    async def get_messages(self, channel_id, *, before=None, after=None, limit=100):
        self.calls += 1
        if channel_id in self.forbid:
            from app.discord_api import ForbiddenError

            raise ForbiddenError("Нет доступа", 403)
        pool = [m for m in self.messages if m["channel_id"] == channel_id]
        if before:
            pool = [m for m in pool if int(m["id"]) < int(before)]
        return pool[:limit]


async def _events(_payload):
    return None


async def _prepare(channels: list[dict] | None = None) -> FakeClient:
    await db.init_db()
    await db.remove_guild_data(GUILD)
    await db.upsert_guild({"id": GUILD, "name": "Тестовый"})
    channels = channels or [
        {"id": "700000000000000001", "name": "general", "type": 0, "position": 0}
    ]
    await db.upsert_channels(GUILD, channels)
    await db.ensure_scan_states(GUILD, await db.list_channels(GUILD))
    return FakeClient()


async def _drain(scanner: Scanner, timeout: float = 20) -> None:
    assert scanner.task is not None
    await asyncio.wait_for(scanner.task, timeout)


def test_full_scan_collects_all_pages() -> None:
    async def run() -> None:
        client = await _prepare()
        scanner = Scanner(client, _events)
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        await _drain(scanner)

        assert scanner.state["status"] == "done"
        stats = await db.stats(GUILD)
        assert stats["messages"] == 250
        assert stats["authors"] == 1
        assert client.calls >= 3  # 250 сообщений = 3 страницы

        state = await db.get_scan_state("700000000000000001")
        assert state["complete"] == 1
        assert state["fetched"] == 250

    asyncio.run(run())


def test_incremental_scan_stops_at_known_history() -> None:
    async def run() -> None:
        client = await _prepare()
        scanner = Scanner(client, _events)
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        await _drain(scanner)

        calls_before = client.calls
        # добавляем 2 новых сообщения сверху
        client.messages = make_messages("700000000000000001", 252)
        scanner2 = Scanner(client, _events)
        await scanner2.start(GUILD, mode="auto", include_threads=False, fetch_members=False)
        await _drain(scanner2)

        assert scanner2.state["messages_new"] == 2
        assert client.calls - calls_before == 1  # остановились на первой странице
        stats = await db.stats(GUILD)
        assert stats["messages"] == 252

    asyncio.run(run())


def test_forbidden_channel_is_reported_not_fatal() -> None:
    async def run() -> None:
        channels = [
            {"id": "700000000000000001", "name": "general", "type": 0, "position": 0},
            {"id": "700000000000000002", "name": "closed", "type": 0, "position": 1},
        ]
        client = await _prepare(channels)
        client.forbid = {"700000000000000002"}
        scanner = Scanner(client, _events)
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        await _drain(scanner)

        assert scanner.state["status"] == "done"
        state = await db.get_scan_state("700000000000000002")
        assert state["status"] == "error"
        assert "доступ" in (state["last_error"] or "").lower()
        stats = await db.stats(GUILD)
        assert stats["messages"] == 250

    asyncio.run(run())


def test_rebuild_mode_clears_old_messages() -> None:
    async def run() -> None:
        client = await _prepare()
        scanner = Scanner(client, _events)
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        await _drain(scanner)

        # имитируем «мусорные» старые записи
        await db.insert_messages(
            [
                (
                    "111111111111111111", GUILD, "700000000000000001", "800000000000000001",
                    "старое", 1, None, 0, None, "[]", "[]", "[]", "[]", 0, "[]",
                )
            ]
        )
        assert (await db.stats(GUILD))["messages"] == 251

        scanner2 = Scanner(client, _events)
        await scanner2.start(GUILD, mode="rebuild", include_threads=False, fetch_members=False)
        await _drain(scanner2)
        assert (await db.stats(GUILD))["messages"] == 250

    asyncio.run(run())


def test_stop_request_marks_partial() -> None:
    async def run() -> None:
        client = await _prepare()
        scanner = Scanner(client, _events)

        original = client.get_messages

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.01)
            return await original(*args, **kwargs)

        client.get_messages = slow  # type: ignore[assignment]
        await scanner.start(GUILD, mode="full", include_threads=False, fetch_members=False)
        await asyncio.sleep(0.02)
        await scanner.stop()
        await _drain(scanner)

        assert scanner.state["status"] == "stopped"
        state = await db.get_scan_state("700000000000000001")
        assert state["status"] in ("partial", "done")

    asyncio.run(run())


def test_message_row_parsing() -> None:
    row = db.message_to_row(
        {
            "id": "1234567890123456789",
            "channel_id": "1",
            "content": "hi",
            "timestamp": "2024-05-01T12:00:00.000000+00:00",
            "author": {"id": "42", "username": "u"},
            "attachments": [{"url": "x"}],
            "message_reference": {"message_id": "111"},
        },
        GUILD,
    )
    assert row[3] == "42"
    assert row[8] == "111"
    assert row[5] == db._iso_to_ms("2024-05-01T12:00:00+00:00")
    assert db._iso_to_ms("2024-05-01T12:00:00Z") == row[5]
