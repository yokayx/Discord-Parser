"""Авторизация: принимается только токен бота."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.discord_api import AuthError, DiscordClient  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_bot_token_accepted() -> None:
    async def run() -> None:
        client = DiscordClient()
        calls: list[str] = []

        async def fake_request(method, path, *, params=None, auth_mode=None):
            calls.append(path)
            assert client.token == "BOT_TOKEN", "префикс Bot снимается"
            return FakeResponse(200, {"id": "1", "username": "parser", "bot": True})

        client._raw_request = fake_request  # type: ignore[assignment]
        me = await client.connect("Bot BOT_TOKEN")
        assert me["bot"] is True
        assert client.connected is True
        assert client.auth_mode == "bot"
        assert client._headers() == {"Authorization": "Bot BOT_TOKEN"}
        assert calls == ["/users/@me"]

    asyncio.run(run())


def test_user_token_rejected() -> None:
    async def run() -> None:
        client = DiscordClient()

        async def fake_request(method, path, *, params=None, auth_mode=None):
            return FakeResponse(200, {"id": "2", "username": "human", "bot": False})

        client._raw_request = fake_request  # type: ignore[assignment]
        try:
            await client.connect("USER_TOKEN")
        except AuthError as exc:
            assert "только с токеном бота" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("пользовательский токен должен отклоняться")
        assert client.connected is False
        assert client.token is None

    asyncio.run(run())


def test_invalid_bot_token() -> None:
    async def run() -> None:
        client = DiscordClient()

        async def fake_request(method, path, *, params=None, auth_mode=None):
            return FakeResponse(401, {"message": "401: Unauthorized"})

        client._raw_request = fake_request  # type: ignore[assignment]
        try:
            await client.connect("wrong")
        except AuthError as exc:
            assert "не принят" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("ожидалась ошибка авторизации")
        assert client.connected is False

    asyncio.run(run())


def test_headers_always_use_bot_scheme() -> None:
    client = DiscordClient()
    client.token = "TOKEN"
    client.auth_mode = "bot"
    assert client._headers() == {"Authorization": "Bot TOKEN"}
