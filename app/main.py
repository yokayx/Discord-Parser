"""FastAPI-приложение: REST API панели + WebSocket для живых обновлений."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import archive, db
from .config import ENV_TOKEN, STATIC_DIR
from .discord_api import AuthError, DiscordClient, DiscordError
from .scanner import ScanBusy, init_scanner

client = DiscordClient()
ws_clients: set[WebSocket] = set()
scanner = init_scanner(client, lambda payload: broadcast(payload))


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #
async def broadcast(payload: dict[str, Any]) -> None:
    if not ws_clients:
        return
    dead = []
    message = json.dumps(payload, ensure_ascii=False, default=str)
    for ws in list(ws_clients):
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    token = ENV_TOKEN or await db.get_setting("token")
    if token:
        me = await client.restore(token)
        if me and await db.get_setting("remember_token") == "1":
            await db.set_setting("token", token)
    yield
    if scanner.running:
        await scanner.stop()
    await client.close()


app = FastAPI(title="Discord Parser", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
# модели запросов
# --------------------------------------------------------------------------- #
class TokenPayload(BaseModel):
    token: str = Field(min_length=5)
    remember: bool = False


class ArchivePayload(BaseModel):
    guild_id: str
    format: str = "line"
    separator: str = "\\n"
    time_format: str = "%d.%m.%Y %H:%M"
    include_attachments: bool = True
    include_ids: bool = False
    header: bool = True
    clean: bool = False


class ArchiveAutoPayload(BaseModel):
    enabled: bool = True


class ScanPayload(BaseModel):
    guild_id: str
    channel_ids: list[str] | None = None
    mode: str = "auto"
    include_threads: bool = True
    fetch_members: bool = True


# --------------------------------------------------------------------------- #
# служебные хелперы
# --------------------------------------------------------------------------- #
def iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


async def require_client() -> DiscordClient:
    if not client.connected:
        raise HTTPException(401, "Нет активного подключения к Discord")
    return client


async def sync_channels(guild_id: str) -> list[dict[str, Any]]:
    """Загружает каналы сервера (и активные ветки) в локальную БД."""
    dc = await require_client()
    guild = await dc.get_guild(guild_id)
    await db.upsert_guild(guild)

    channels = await dc.get_guild_channels(guild_id)
    await db.upsert_channels(guild_id, channels)

    try:
        threads = await dc.get_active_threads(guild_id)
        if threads:
            await db.upsert_channels(guild_id, threads)
    except DiscordError:
        pass

    await db.ensure_scan_states(guild_id, channels)
    return await db.list_channels(guild_id)


# --------------------------------------------------------------------------- #
# страницы
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# сессия
# --------------------------------------------------------------------------- #
@app.get("/api/session")
async def session() -> dict[str, Any]:
    guilds: list[dict[str, Any]] = await db.list_guilds()
    return {
        "connected": client.connected,
        "me": _public_me(client.me),
        "auth_mode": client.auth_mode,
        "guilds": guilds,
        "token_source": await db.get_setting("token_source") if not client.connected else None,
    }


def _public_me(me: dict[str, Any] | None) -> dict[str, Any] | None:
    if not me:
        return None
    return {
        "id": me.get("id"),
        "username": me.get("username"),
        "global_name": me.get("global_name"),
        "avatar": me.get("avatar"),
        "bot": me.get("bot", False),
    }


@app.post("/api/token")
async def set_token(payload: TokenPayload) -> dict[str, Any]:
    try:
        me = await client.connect(payload.token)
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except DiscordError as exc:
        raise HTTPException(502, str(exc)) from exc

    if payload.remember:
        await db.set_setting("token", client.token)
        await db.set_setting("remember_token", "1")
    else:
        await db.set_setting("token", None)
        await db.set_setting("remember_token", "0")
    return {"connected": True, "me": _public_me(me), "auth_mode": client.auth_mode}


@app.delete("/api/token")
async def drop_token() -> dict[str, bool]:
    client.disconnect()
    await db.set_setting("token", None)
    await db.set_setting("remember_token", "0")
    return {"connected": False}


# --------------------------------------------------------------------------- #
# серверы и каналы
# --------------------------------------------------------------------------- #
@app.get("/api/guilds")
async def guilds(refresh: bool = False) -> dict[str, Any]:
    if refresh and client.connected:
        dc = await require_client()
        remote = await dc.get_guilds()
        for g in remote:
            await db.upsert_guild({**g, "member_count": g.get("approximate_member_count")})
    return {"guilds": await db.list_guilds()}


@app.post("/api/guilds/{guild_id}/sync")
async def guild_sync(guild_id: str) -> dict[str, Any]:
    channels = await sync_channels(guild_id)
    return {"channels": channels, "guilds": await db.list_guilds()}


@app.get("/api/guilds/{guild_id}/channels")
async def guild_channels(guild_id: str) -> dict[str, Any]:
    return {"channels": await db.list_channels(guild_id)}


@app.delete("/api/guilds/{guild_id}/data")
async def guild_clear(guild_id: str) -> dict[str, bool]:
    if scanner.running:
        raise HTTPException(409, "Сначала остановите сканирование")
    await db.remove_guild_data(guild_id)
    return {"cleared": True}


# --------------------------------------------------------------------------- #
# сканирование
# --------------------------------------------------------------------------- #
@app.post("/api/scan")
async def start_scan(payload: ScanPayload) -> dict[str, Any]:
    await require_client()
    try:
        return await scanner.start(
            payload.guild_id,
            payload.channel_ids,
            mode=payload.mode,
            include_threads=payload.include_threads,
            fetch_members=payload.fetch_members,
        )
    except ScanBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/scan/stop")
async def stop_scan() -> dict[str, Any]:
    return await scanner.stop()


@app.get("/api/scan/status")
async def scan_status() -> dict[str, Any]:
    overview = await db.scan_overview()
    return {"scanner": scanner.status(), "database": overview}


# --------------------------------------------------------------------------- #
# статистика и выборки
# --------------------------------------------------------------------------- #
@app.get("/api/stats")
async def stats(guild_id: str | None = None) -> dict[str, Any]:
    data = await db.stats(guild_id)
    data["media_hosts"] = await db.top_media(guild_id)
    data["scan_channels"] = (await db.scan_overview(guild_id))["channels"]
    return data


@app.get("/api/messages")
async def messages(
    guild_id: str | None = None,
    q: str | None = None,
    author_id: str | None = None,
    channel_id: str | None = None,
    sort: str = "newest",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    data = await db.query_messages(
        guild_id,
        search=q,
        author_id=author_id,
        channel_id=channel_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    for item in data["items"]:
        item["timestamp_iso"] = iso(item["timestamp"])
        item["edited_iso"] = iso(item["edited_timestamp"])
        item["author_name"] = item.get("global_name") or item.get("username") or item["author_id"]
    return data


@app.get("/api/authors")
async def authors(
    guild_id: str | None = None,
    q: str | None = None,
    sort: str = "messages",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    data = await db.query_authors(guild_id, search=q, sort=sort, limit=limit, offset=offset)
    for item in data["items"]:
        item["display_name"] = item.get("global_name") or item.get("username")
        item["account_created_iso"] = iso(item.get("account_created_at"))
        item["first_message_iso"] = iso(item.get("first_message_at"))
        item["last_message_iso"] = iso(item.get("last_message_at"))
    return data


@app.get("/api/authors/{author_id}")
async def author_detail(author_id: str, guild_id: str | None = None) -> dict[str, Any]:
    author = await db.get_author(author_id, guild_id)
    if not author:
        raise HTTPException(404, "Автор не найден")
    author["account_created_iso"] = iso(author.get("account_created_at"))
    author["joined_iso"] = iso(author.get("joined_at"))
    for key in ("first_message_at", "last_message_at"):
        author[key.replace("_at", "_iso")] = iso(author.get(key))
    for row in author["recent"]:
        row["timestamp_iso"] = iso(row["timestamp"])
    return author


@app.get("/api/messages/{message_id}")
async def message_detail(message_id: str) -> dict[str, Any]:
    item = await db.get_message(message_id)
    if not item:
        raise HTTPException(404, "Сообщение не найдено")
    item["timestamp_iso"] = iso(item["timestamp"])
    item["edited_iso"] = iso(item.get("edited_timestamp"))
    return item


# --------------------------------------------------------------------------- #
# экспорт
# --------------------------------------------------------------------------- #
MESSAGE_COLUMNS = [
    "id", "timestamp_iso", "author_name", "username", "author_id", "bot",
    "channel_name", "channel_id", "content", "attachment_count", "reaction_count",
    "edited_iso", "reference_message_id",
]
AUTHOR_COLUMNS = [
    "id", "username", "global_name", "discriminator", "bot", "message_count",
    "account_created_iso", "joined_iso", "first_message_iso", "last_message_iso",
]


@app.get("/api/export/messages")
async def export_messages(guild_id: str | None = None, fmt: str = "csv") -> StreamingResponse:
    if fmt == "json":
        async def json_gen() -> AsyncIterator[bytes]:
            yield b'{"messages": ['
            first = True
            async for row in db.iter_messages_export(guild_id):
                payload = _message_export_row(row)
                chunk = ("" if first else ",") + json.dumps(payload, ensure_ascii=False)
                first = False
                yield chunk.encode()
            yield b"]}"
        return StreamingResponse(
            json_gen(),
            media_type="application/json",
            headers={"Content-Disposition": _disposition("messages", "json")},
        )

    async def csv_gen() -> AsyncIterator[bytes]:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=MESSAGE_COLUMNS, delimiter=";")
        writer.writeheader()
        yield buffer.getvalue().encode("utf-8-sig")
        buffer.seek(0)
        buffer.truncate(0)
        async for row in db.iter_messages_export(guild_id):
            writer.writerow(_message_export_row(row))
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        csv_gen(),
        media_type="text/csv",
        headers={"Content-Disposition": _disposition("messages", "csv")},
    )


@app.get("/api/export/authors")
async def export_authors(guild_id: str | None = None, fmt: str = "csv") -> StreamingResponse:
    if fmt == "json":
        async def json_gen() -> AsyncIterator[bytes]:
            yield b'{"authors": ['
            first = True
            async for row in db.iter_authors_export(guild_id):
                payload = _author_export_row(row)
                chunk = ("" if first else ",") + json.dumps(payload, ensure_ascii=False)
                first = False
                yield chunk.encode()
            yield b"]}"
        return StreamingResponse(
            json_gen(),
            media_type="application/json",
            headers={"Content-Disposition": _disposition("authors", "json")},
        )

    async def csv_gen() -> AsyncIterator[bytes]:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=AUTHOR_COLUMNS, delimiter=";")
        writer.writeheader()
        yield buffer.getvalue().encode("utf-8-sig")
        buffer.seek(0)
        buffer.truncate(0)
        async for row in db.iter_authors_export(guild_id):
            writer.writerow(_author_export_row(row))
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        csv_gen(),
        media_type="text/csv",
        headers={"Content-Disposition": _disposition("authors", "csv")},
    )


def _message_export_row(row: dict[str, Any]) -> dict[str, Any]:
    def count(value: str | None) -> int:
        try:
            return len(json.loads(value)) if value else 0
        except (TypeError, ValueError):
            return 0

    return {
        "id": row["id"],
        "timestamp_iso": iso(row["timestamp"]),
        "author_name": row.get("author_name"),
        "username": row.get("username"),
        "author_id": row["author_id"],
        "bot": bool(row.get("bot")),
        "channel_name": row.get("channel_name"),
        "channel_id": row["channel_id"],
        "content": row.get("content") or "",
        "attachment_count": count(row.get("attachments")),
        "reaction_count": sum(
            int(r.get("count", 0)) for r in (json.loads(row["reactions"]) if row.get("reactions") else [])
        ),
        "edited_iso": iso(row.get("edited_timestamp")),
        "reference_message_id": row.get("reference_message_id"),
    }


def _author_export_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row.get("username"),
        "global_name": row.get("global_name"),
        "discriminator": row.get("discriminator"),
        "bot": bool(row.get("bot")),
        "message_count": row.get("message_count"),
        "account_created_iso": iso(row.get("account_created_at")),
        "joined_iso": iso(row.get("joined_at")),
        "first_message_iso": iso(row.get("first_message_at")),
        "last_message_iso": iso(row.get("last_message_at")),
    }


def _disposition(name: str, ext: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M")
    return f'attachment; filename="{name}-{stamp}.{ext}"'


# --------------------------------------------------------------------------- #
# архив
# --------------------------------------------------------------------------- #
@app.get("/api/archive")
async def archive_status(guild_id: str) -> dict[str, Any]:
    """Что уже лежит на диске + текущие настройки формата."""
    return await archive.archive_info(guild_id)


@app.post("/api/archive")
async def archive_build(payload: ArchivePayload) -> dict[str, Any]:
    """Собрать/обновить файлы архива по каналам и сохранить настройки."""
    options = archive.ArchiveOptions.from_dict(payload.model_dump(), clean=payload.clean)
    await archive.save_options(options)
    result = await archive.build_guild_archive(payload.guild_id, options)
    data = result.to_dict()
    await broadcast({"type": "archive", "data": data})
    return data


@app.post("/api/archive/auto")
async def archive_auto(payload: ArchiveAutoPayload) -> dict[str, bool]:
    await archive.set_auto(payload.enabled)
    return {"auto": payload.enabled}


@app.get("/api/archive/download")
async def archive_download(guild_id: str) -> FileResponse:
    guild = await db.get_guild_row(guild_id)
    try:
        path = await asyncio.to_thread(
            archive.build_zip, guild_id, (guild or {}).get("name")
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    # заголовок должен быть ASCII, поэтому имя дублируется в RFC 5987
    pretty = f"archive-{guild_id}.zip"
    header = (
        f'attachment; filename="{path.name}"; '
        f"filename*=UTF-8''{quote(pretty)}"
    )
    return FileResponse(
        path,
        media_type="application/zip",
        headers={"Content-Disposition": header},
    )


@app.get("/api/archive/files")
async def archive_files(guild_id: str) -> dict[str, Any]:
    guild = await db.get_guild_row(guild_id)
    files = archive.archive_files(guild_id, (guild or {}).get("name"))
    return {
        "files": [{"name": p.name, "bytes": p.stat().st_size} for p in files],
    }


@app.get("/api/archive/file")
async def archive_file(guild_id: str, name: str, limit: int = Query(200_000, ge=1)) -> dict[str, Any]:
    """Предпросмотр файла архива (обрезается по limit символов)."""
    guild = await db.get_guild_row(guild_id)
    try:
        path = archive.read_archive_file(guild_id, name, (guild or {}).get("name"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "Файл не найден") from exc
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > limit
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "truncated": truncated,
        "content": text[:limit],
    }


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "hello", "data": {"scanner": scanner.status()}}))
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        ws_clients.discard(ws)


@app.exception_handler(DiscordError)
async def discord_error_handler(request: Request, exc: DiscordError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=exc.status or 502)
