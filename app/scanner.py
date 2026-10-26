"""Сканер истории: постраничный обход каналов и веток с сохранением в SQLite."""
from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Awaitable, Callable, Iterable

from . import archive, db
from .discord_api import DiscordClient, ForbiddenError
from .config import PAGE_SIZE

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]

MODES = {"auto", "full", "rebuild"}


class ScanBusy(RuntimeError):
    pass


class Scanner:
    """Единственный активный проход по серверу (иначе Discord быстро отдаст 429)."""

    def __init__(self, client: DiscordClient, broadcast: Broadcast) -> None:
        self.client = client
        self.broadcast = broadcast
        self.task: asyncio.Task[None] | None = None
        self._stop = False
        self._lock = asyncio.Lock()
        self.state: dict[str, Any] = {"status": "idle"}
        self._channels_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # публичное API
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict[str, Any]:
        return dict(self.state)

    async def start(
        self,
        guild_id: str,
        channel_ids: Iterable[str] | None = None,
        *,
        mode: str = "auto",
        include_threads: bool = True,
        fetch_members: bool = True,
    ) -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"Неизвестный режим: {mode}")
        if self.running:
            raise ScanBusy("Сканирование уже идёт")

        async with self._lock:
            if self.running:
                raise ScanBusy("Сканирование уже идёт")

            channels = await db.list_channels(guild_id)
            if not channels:
                raise ValueError("Каналы не загружены — сначала выберите сервер")

            # категории и голосовые каналы истории не имеют
            channels = [c for c in channels if int(c.get("type") or 0) not in (2, 4, 13)]
            selected = set(map(str, channel_ids)) if channel_ids else None
            if selected is not None:
                channels = [c for c in channels if str(c["id"]) in selected]
            else:
                channels = [c for c in channels if not c["is_thread"]]

            if mode == "rebuild":
                await db.remove_guild_data(guild_id)
                await db.upsert_channels(guild_id, [
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "type": c["type"],
                        "parent_id": c["parent_id"],
                        "position": c["position"],
                        "topic": c["topic"],
                        "nsfw": c["nsfw"],
                    }
                    for c in channels
                ])
                await db.ensure_scan_states(guild_id, channels)
            elif mode == "full":
                await db.reset_scan_state(guild_id, [str(c["id"]) for c in channels])
            else:
                await db.ensure_scan_states(guild_id, channels)

            self._stop = False
            self.state = {
                "status": "running",
                "guild_id": guild_id,
                "mode": mode,
                "started_at": int(time.time()),
                "finished_at": None,
                "channels_total": len(channels),
                "channels_done": 0,
                "current_channel": None,
                "messages_fetched": 0,
                "messages_new": 0,
                "pages": 0,
                "errors": [],
                "message": "Подготовка…",
            }
            self.task = asyncio.create_task(
                self._run(guild_id, channels, mode, include_threads, fetch_members)
            )
            await self.broadcast({"type": "scan", "data": self.status()})
            return self.status()

    async def stop(self) -> dict[str, Any]:
        if self.running:
            self._stop = True
            self.state["message"] = "Останавливаюсь после текущей страницы…"
            await self.broadcast({"type": "scan", "data": self.status()})
        return self.status()

    # ------------------------------------------------------------------ #
    # внутреннее
    # ------------------------------------------------------------------ #
    async def _emit(self) -> None:
        await self.broadcast({"type": "scan", "data": self.status()})

    async def _run(
        self,
        guild_id: str,
        channels: list[dict[str, Any]],
        mode: str,
        include_threads: bool,
        fetch_members: bool,
    ) -> None:
        try:
            guild = await self.client.get_guild(guild_id)
            guild.setdefault("approximate_member_count", None)
            await db.upsert_guild(guild)

            if include_threads:
                channels = await self._expand_threads(guild_id, channels)
                self.state["channels_total"] = len(channels)
                await self._emit()

            if fetch_members and self.client.auth_mode == "bot":
                await self._fetch_members(guild_id)

            for index, channel in enumerate(channels):
                if self._stop:
                    break
                self.state["current_channel"] = channel["name"]
                self.state["channel_index"] = index
                self.state["message"] = f"Канал #{channel['name']}"
                await self._emit()
                await self._scan_channel(guild_id, channel, mode)
                self.state["channels_done"] = index + 1
                await self._emit()

            self.state["status"] = "stopped" if self._stop else "done"
            self.state["message"] = (
                "Остановлено пользователем" if self._stop else "Сканирование завершено"
            )
            await self._save_archive(guild_id)
        except asyncio.CancelledError:
            self.state["status"] = "stopped"
            self.state["message"] = "Задача отменена"
            raise
        except Exception as exc:  # noqa: BLE001 — показываем ошибку в панели
            self.state["status"] = "error"
            self.state["message"] = f"Ошибка: {exc}"
            self.state["errors"].append({"channel": None, "error": str(exc)})
            traceback.print_exc()
        finally:
            self.state["finished_at"] = int(time.time())
            self.state["current_channel"] = None
            await self._emit()
            await self.broadcast({"type": "stats_stale", "data": {"guild_id": guild_id}})

    async def _save_archive(self, guild_id: str) -> None:
        """Сохраняет итоги парсинга в текстовые файлы по каналам (папка Archive)."""
        try:
            if not await archive.auto_enabled():
                return
            self.state["message"] = "Сохраняю архив: файлы по каналам…"
            await self._emit()
            result = await archive.build_guild_archive(guild_id)
            payload = result.to_dict()
            self.state["archive"] = {
                "zip_path": payload["zip_path"],
                "zip_name": payload["zip_name"],
                "files": payload["files"],
                "messages": payload["messages"],
            }
            self.state["message"] = (
                f"Готово: {payload['zip_name']} — {payload['files']} файлов, "
                f"{payload['messages']} сообщений"
            )
            await self.broadcast({"type": "archive", "data": payload})
        except Exception as exc:  # noqa: BLE001 — архив не должен ломать скан
            self.state["errors"].append({"channel": "архив", "error": str(exc)})

    async def _expand_threads(
        self, guild_id: str, channels: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Добавляет активные и архивные ветки к списку каналов."""
        threads: list[dict[str, Any]] = []
        try:
            threads.extend(await self.client.get_active_threads(guild_id))
        except ForbiddenError:
            pass
        except Exception:  # noqa: BLE001
            pass

        parents = [
            c for c in channels
            if c["type"] in (0, 5, 15) and not c["is_thread"]
        ]
        for parent in parents:
            if self._stop:
                break
            try:
                archived = await self.client.get_archived_threads(str(parent["id"]))
            except Exception:  # noqa: BLE001
                archived = []
            threads.extend(archived)

        if threads:
            await db.upsert_channels(guild_id, threads)
            await db.ensure_scan_states(guild_id, threads)
            existing = {str(c["id"]) for c in channels}
            channels = channels + [t for t in threads if str(t["id"]) not in existing]
        return channels

    async def _fetch_members(self, guild_id: str) -> None:
        try:
            members = await self.client.get_guild_members(guild_id)
        except Exception:  # noqa: BLE001 — нужен привилегированный интент
            return
        payload = []
        for m in members:
            user = m.get("user") or {}
            if not user.get("id"):
                continue
            payload.append(
                {
                    **user,
                    "nick": m.get("nick"),
                    "roles": m.get("roles") or [],
                    "joined_at_ts": db._iso_to_ms(m["joined_at"]) if m.get("joined_at") else None,
                }
            )
        await db.upsert_authors(payload)
        await self.broadcast({"type": "members", "data": {"count": len(payload)}})

    async def _scan_channel(
        self, guild_id: str, channel: dict[str, Any], mode: str
    ) -> None:
        cid = str(channel["id"])
        name = channel.get("name") or cid
        state = await db.get_scan_state(cid)
        complete = bool(state and state["complete"])
        oldest_known = state["oldest_id"] if state else None

        # auto: незавершённый канал продолжаем с места остановки,
        # завершённый — сверху вниз до первого известного сообщения.
        before: str | None = None
        incremental = False
        if mode == "auto":
            if complete:
                incremental = True
            elif oldest_known:
                before = oldest_known

        await db.update_scan_state(cid, status="running", last_error=None,
                                   started_at=int(time.time()))
        await self._emit()

        fetched = 0
        new = 0
        pages = 0
        seen_oldest = oldest_known
        seen_newest = state["newest_id"] if state else None

        try:
            while True:
                if self._stop:
                    await db.update_scan_state(cid, status="partial")
                    break

                batch = await self.client.get_messages(cid, before=before, limit=PAGE_SIZE)
                if not batch:
                    await db.update_scan_state(
                        cid,
                        status="done",
                        complete=1,
                        oldest_id=seen_oldest,
                        newest_id=seen_newest,
                        last_run_at=int(time.time()),
                        finished_at=int(time.time()),
                    )
                    break

                rows = [db.message_to_row(m, guild_id) for m in batch]
                inserted = await db.insert_messages(rows)
                await db.touch_message_aggregates(rows)
                await db.upsert_authors(
                    [{**(m.get("author") or {})} for m in batch if m.get("author")]
                )

                fetched += len(batch)
                new += inserted
                pages += 1
                ids = [str(m["id"]) for m in batch]
                oldest_in_page = min(ids, key=int)
                newest_in_page = max(ids, key=int)
                seen_oldest = min(filter(None, [seen_oldest, oldest_in_page]), key=int)
                seen_newest = max(filter(None, [seen_newest, newest_in_page]), key=int)

                self.state["messages_fetched"] += len(batch)
                self.state["messages_new"] += inserted
                self.state["pages"] += 1
                self.state["message"] = (
                    f"#{name}: собрано {fetched} сообщений (+{new} новых)"
                )
                await db.update_scan_state(
                    cid,
                    oldest_id=seen_oldest,
                    newest_id=seen_newest,
                    fetched=(state["fetched"] if state and mode != "rebuild" else 0) + fetched,
                )
                await self._emit()

                # В инкрементальном режиме дошли до уже сохранённой истории.
                if incremental and inserted < len(batch):
                    await db.update_scan_state(
                        cid,
                        status="done",
                        complete=1,
                        last_run_at=int(time.time()),
                        finished_at=int(time.time()),
                    )
                    break

                if len(batch) < PAGE_SIZE:
                    await db.update_scan_state(
                        cid,
                        status="done",
                        complete=1,
                        last_run_at=int(time.time()),
                        finished_at=int(time.time()),
                    )
                    break

                before = oldest_in_page
                # небольшая пауза, чтобы не злить rate-limit на плотных каналах
                await asyncio.sleep(0.15)

        except ForbiddenError:
            await db.update_scan_state(
                cid, status="error", last_error="Нет доступа к каналу", finished_at=int(time.time())
            )
            self.state["errors"].append({"channel": name, "error": "Нет доступа (403)"})
        except Exception as exc:  # noqa: BLE001
            await db.update_scan_state(
                cid, status="error", last_error=str(exc), finished_at=int(time.time())
            )
            self.state["errors"].append({"channel": name, "error": str(exc)})
            if self.state["status"] == "running":
                self.state["message"] = f"#{name}: ошибка — {exc}"
            await self._emit()
        finally:
            self.state["current_channel_new"] = None

        if mode in ("full", "rebuild") and not self._stop:
            await db.update_scan_state(cid, complete=1)


scanner: Scanner | None = None


def init_scanner(client: DiscordClient, broadcast: Broadcast) -> Scanner:
    global scanner
    scanner = Scanner(client, broadcast)
    return scanner
