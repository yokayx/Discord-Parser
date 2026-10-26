"""Минимальный клиент Discord REST API с обработкой rate-limit."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import DISCORD_API, PAGE_SIZE


class DiscordError(Exception):
    """Базовая ошибка Discord API."""

    def __init__(self, message: str, status: int = 0, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthError(DiscordError):
    """Токен не принят."""


class ForbiddenError(DiscordError):
    """Нет доступа к объекту (канал/сервер)."""


class DiscordClient:
    """Обёртка над Discord REST API.

    Работает только с токеном бота: заголовок `Authorization: Bot <token>`.
    Пользовательские токены отклоняются при подключении.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self.token: str | None = None
        self.auth_mode: str | None = None  # всегда "bot"
        self.me: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # служебное
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCORD_API,
                timeout=httpx.Timeout(30.0, connect=15.0),
                headers={
                    "User-Agent": "DiscordParser/1.0 (+local dashboard)",
                    "Accept": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def connected(self) -> bool:
        return bool(self.token and self.me)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise AuthError("Токен не задан")
        return {"Authorization": f"Bot {self.token}"}

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        auth_mode: str | None = None,
    ) -> httpx.Response:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bot {self.token}"
        return await self.client.request(method, path, params=params, headers=headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> Any:
        """Запрос с повторами на 429/5xx."""
        if not self.token:
            raise AuthError("Токен не задан")

        for attempt in range(retries + 1):
            try:
                resp = await self.client.request(
                    method, path, params=params, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise DiscordError(f"Сетевая ошибка: {exc}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 429:
                wait = self._retry_after(resp)
                if attempt >= retries:
                    raise DiscordError("Discord ограничил запросы (429)", 429)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt >= retries:
                    raise DiscordError(f"Discord вернул {resp.status_code}", resp.status_code)
                await asyncio.sleep(2 * (attempt + 1))
                continue

            if resp.status_code == 401:
                raise AuthError("Токен недействителен или истёк", 401)
            if resp.status_code == 403:
                details = self._error_message(resp)
                raise ForbiddenError(details or "Нет доступа", 403)
            if resp.status_code == 404:
                raise DiscordError("Объект не найден", 404)
            if resp.status_code >= 400:
                raise DiscordError(
                    self._error_message(resp) or f"Ошибка Discord {resp.status_code}",
                    resp.status_code,
                )

            await self._respect_bucket(resp)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        raise DiscordError("Запрос не удался после повторов")

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("retry_after") is not None:
                return float(data["retry_after"]) + 0.15
        except ValueError:
            pass
        header = resp.headers.get("Retry-After")
        try:
            return float(header) + 0.15 if header else 1.0
        except ValueError:
            return 1.0

    @staticmethod
    def _error_message(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return ""
        if isinstance(data, dict):
            return str(data.get("message") or "")
        return ""

    @staticmethod
    async def _respect_bucket(resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset:
            try:
                wait = float(reset) - time.time()
            except ValueError:
                return
            if 0 < wait < 5:
                await asyncio.sleep(wait + 0.05)

    # ------------------------------------------------------------------ #
    # авторизация
    # ------------------------------------------------------------------ #
    async def connect(self, token: str) -> dict[str, Any]:
        """Проверяет токен бота и запоминает его. Пользовательские токены не принимаются."""
        token = token.strip()
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        if not token:
            raise AuthError("Пустой токен")

        self.token, self.auth_mode = token, "bot"
        try:
            response = await self._raw_request("GET", "/users/@me", auth_mode="bot")
        except httpx.HTTPError as exc:
            self.token, self.auth_mode = None, None
            raise AuthError(f"Сетевая ошибка при проверке токена: {exc}") from exc

        if response.status_code != 200:
            self.token, self.auth_mode = None, None
            if response.status_code == 401:
                raise AuthError(
                    "Токен бота не принят. Скопируйте актуальный токен во вкладке Bot "
                    "в Discord Developer Portal (Reset Token)."
                )
            raise AuthError(f"Discord ответил HTTP {response.status_code} на проверку токена")

        me = response.json()
        if not me.get("bot"):
            self.token, self.auth_mode, self.me = None, None, None
            raise AuthError(
                "Это пользовательский токен. Инструмент работает только с токеном бота: "
                "создайте приложение на discord.com/developers/applications → Bot → Reset Token."
            )

        self.me = me
        return me

    async def restore(self, token: str) -> dict[str, Any] | None:
        """Тихое восстановление сессии при старте сервера."""
        try:
            return await self.connect(token)
        except DiscordError:
            return None

    def disconnect(self) -> None:
        self.token, self.auth_mode, self.me = None, None, None

    # ------------------------------------------------------------------ #
    # данные
    # ------------------------------------------------------------------ #
    async def get_guilds(self) -> list[dict[str, Any]]:
        guilds = await self.request("GET", "/users/@me/guilds", params={"limit": 200})
        return guilds or []

    async def get_guild(self, guild_id: str) -> dict[str, Any]:
        try:
            return await self.request("GET", f"/guilds/{guild_id}", params={"with_counts": "true"})
        except DiscordError:
            # Пользовательский токен может не иметь доступа к /guilds/{id}
            return {"id": guild_id, "name": guild_id}

    async def get_guild_channels(self, guild_id: str) -> list[dict[str, Any]]:
        channels = await self.request("GET", f"/guilds/{guild_id}/channels")
        return channels or []

    async def get_active_threads(self, guild_id: str) -> list[dict[str, Any]]:
        data = await self.request("GET", f"/guilds/{guild_id}/threads/active")
        return (data or {}).get("threads") or []

    async def get_archived_threads(self, channel_id: str) -> list[dict[str, Any]]:
        """Архивные ветки канала (по одной странице на тип)."""
        out: list[dict[str, Any]] = []
        for path in (
            f"/channels/{channel_id}/threads/archived/public",
            f"/channels/{channel_id}/threads/archived/private",
        ):
            try:
                data = await self.request("GET", path, params={"limit": 100})
            except DiscordError:
                continue
            threads = (data or {}).get("threads") or []
            for t in threads:
                t.setdefault("parent_id", channel_id)
            out.extend(threads)
        return out

    async def get_messages(
        self,
        channel_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        limit: int = PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        messages = await self.request("GET", f"/channels/{channel_id}/messages", params=params)
        return messages or []

    async def get_guild_members(self, guild_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        members = await self.request(
            "GET", f"/guilds/{guild_id}/members", params={"limit": limit}
        )
        return members or []

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/channels/{channel_id}")


client = DiscordClient()
