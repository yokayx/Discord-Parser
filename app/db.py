"""Слой хранения: SQLite (aiosqlite) + все запросы панели."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS guilds (
    id            TEXT PRIMARY KEY,
    name          TEXT,
    icon          TEXT,
    description   TEXT,
    owner_id      TEXT,
    member_count  INTEGER,
    fetched_at    INTEGER
);

CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    guild_id    TEXT,
    name        TEXT,
    type        INTEGER,
    parent_id   TEXT,
    position    INTEGER,
    topic       TEXT,
    nsfw        INTEGER DEFAULT 0,
    is_thread   INTEGER DEFAULT 0,
    archived    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id);

CREATE TABLE IF NOT EXISTS authors (
    id                TEXT PRIMARY KEY,
    username          TEXT,
    global_name       TEXT,
    discriminator     TEXT,
    avatar            TEXT,
    bot               INTEGER DEFAULT 0,
    system            INTEGER DEFAULT 0,
    account_created_at INTEGER,
    nickname          TEXT,
    joined_at         INTEGER,
    name_lower        TEXT,
    roles             TEXT,
    first_message_at  INTEGER,
    last_message_at   INTEGER,
    updated_at        INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id                    TEXT PRIMARY KEY,
    guild_id              TEXT,
    channel_id            TEXT,
    author_id             TEXT,
    content               TEXT,
    content_lower         TEXT,
    timestamp             INTEGER,
    edited_timestamp      INTEGER,
    type                  INTEGER,
    reference_message_id  TEXT,
    attachments           TEXT,
    embeds                TEXT,
    reactions             TEXT,
    mentions              TEXT,
    pinned                INTEGER DEFAULT 0,
    sticker_items         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_guild_ts  ON messages(guild_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_channel   ON messages(channel_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_author    ON messages(guild_id, author_id);
CREATE INDEX IF NOT EXISTS idx_messages_content   ON messages(content);

CREATE TABLE IF NOT EXISTS scan_state (
    channel_id    TEXT PRIMARY KEY,
    guild_id      TEXT,
    name          TEXT,
    status        TEXT DEFAULT 'pending',
    oldest_id     TEXT,
    newest_id     TEXT,
    fetched       INTEGER DEFAULT 0,
    complete      INTEGER DEFAULT 0,
    last_error    TEXT,
    started_at    INTEGER,
    finished_at   INTEGER,
    last_run_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scan_guild ON scan_state(guild_id);
"""

# Типы каналов Discord, историю которых имеет смысл читать.
TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12, 15}  # text, announcement, threads, forum
VOICE_CHANNEL_TYPES = {2, 13}                # voice, stage


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Новое соединение с БД (WAL позволяет параллельные чтения)."""
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    async with connect() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
        await _migrate(conn)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Догоняющие миграции для баз, созданных ранними версиями."""
    cur = await conn.execute("PRAGMA table_info(messages)")
    message_cols = {r["name"] for r in await cur.fetchall()}
    cur = await conn.execute("PRAGMA table_info(authors)")
    author_cols = {r["name"] for r in await cur.fetchall()}

    if "content_lower" not in message_cols:
        await conn.execute("ALTER TABLE messages ADD COLUMN content_lower TEXT")
    if "name_lower" not in author_cols:
        await conn.execute("ALTER TABLE authors ADD COLUMN name_lower TEXT")
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_authors_name_lower ON authors(name_lower)"
    )

    # бэкфилл нормализованных полей (нужны для поиска с кириллицей)
    cur = await conn.execute("SELECT COUNT(*) FROM messages WHERE content_lower IS NULL")
    if (await cur.fetchone())[0]:
        cur = await conn.execute("SELECT id, content FROM messages WHERE content_lower IS NULL")
        while True:
            rows = await cur.fetchmany(5000)
            if not rows:
                break
            await conn.executemany(
                "UPDATE messages SET content_lower = ? WHERE id = ?",
                [((r["content"] or "").lower(), r["id"]) for r in rows],
            )
            await conn.commit()

    cur = await conn.execute("SELECT COUNT(*) FROM authors WHERE name_lower IS NULL")
    if (await cur.fetchone())[0]:
        cur = await conn.execute(
            "SELECT id, username, global_name FROM authors WHERE name_lower IS NULL"
        )
        rows = await cur.fetchall()
        await conn.executemany(
            "UPDATE authors SET name_lower = ? WHERE id = ?",
            [
                (
                    f"{r['username'] or ''} {r['global_name'] or ''}".strip().lower(),
                    r["id"],
                )
                for r in rows
            ],
        )
        await conn.commit()


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
async def set_setting(key: str, value: str | None) -> None:
    async with connect() as conn:
        if value is None:
            await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        await conn.commit()


async def get_setting(key: str) -> str | None:
    async with connect() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None


# --------------------------------------------------------------------------- #
# guilds / channels
# --------------------------------------------------------------------------- #
async def upsert_guild(guild: dict[str, Any]) -> None:
    async with connect() as conn:
        await conn.execute(
            """
            INSERT INTO guilds(id, name, icon, description, owner_id, member_count, fetched_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                icon = excluded.icon,
                description = excluded.description,
                owner_id = excluded.owner_id,
                member_count = excluded.member_count,
                fetched_at = excluded.fetched_at
            """,
            (
                str(guild["id"]),
                guild.get("name"),
                guild.get("icon"),
                guild.get("description"),
                str(guild["owner_id"]) if guild.get("owner_id") else None,
                guild.get("approximate_member_count") or guild.get("member_count"),
                int(time.time()),
            ),
        )
        await conn.commit()


async def list_guilds() -> list[dict[str, Any]]:
    async with connect() as conn:
        cur = await conn.execute(
            """
            SELECT g.*,
                   (SELECT COUNT(*) FROM channels c WHERE c.guild_id = g.id AND c.is_thread = 0) AS channel_count,
                   (SELECT COUNT(*) FROM messages m WHERE m.guild_id = g.id) AS message_count,
                   (SELECT COUNT(DISTINCT m.author_id) FROM messages m WHERE m.guild_id = g.id) AS author_count
            FROM guilds g
            ORDER BY message_count DESC, g.name COLLATE NOCASE
            """
        )
        return [dict(r) for r in await cur.fetchall()]


async def upsert_channels(guild_id: str, channels: Iterable[dict[str, Any]]) -> int:
    rows = []
    for ch in channels:
        ctype = ch.get("type", 0)
        is_thread = ctype in (10, 11, 12) or bool(ch.get("thread_metadata"))
        rows.append(
            (
                str(ch["id"]),
                guild_id,
                ch.get("name") or "",
                ctype,
                str(ch["parent_id"]) if ch.get("parent_id") else None,
                ch.get("position") or 0,
                ch.get("topic"),
                1 if ch.get("nsfw") else 0,
                1 if is_thread else 0,
                1 if ch.get("thread_metadata", {}).get("archived") else 0,
            )
        )
    if not rows:
        return 0
    async with connect() as conn:
        await conn.executemany(
            """
            INSERT INTO channels(id, guild_id, name, type, parent_id, position, topic, nsfw, is_thread, archived)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                guild_id = excluded.guild_id,
                name = excluded.name,
                type = excluded.type,
                parent_id = excluded.parent_id,
                position = excluded.position,
                topic = excluded.topic,
                nsfw = excluded.nsfw,
                is_thread = excluded.is_thread,
                archived = excluded.archived
            """,
            rows,
        )
        await conn.commit()
    return len(rows)


async def list_channels(guild_id: str) -> list[dict[str, Any]]:
    async with connect() as conn:
        cur = await conn.execute(
            """
            SELECT c.*,
                   COALESCE(s.status, 'pending') AS scan_status,
                   COALESCE(s.fetched, 0) AS fetched,
                   COALESCE(s.complete, 0) AS complete,
                   (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS message_count,
                   (SELECT COUNT(DISTINCT m.author_id) FROM messages m WHERE m.channel_id = c.id) AS author_count
            FROM channels c
            LEFT JOIN scan_state s ON s.channel_id = c.id
            WHERE c.guild_id = ?
            ORDER BY c.is_thread, c.position, c.name COLLATE NOCASE
            """,
            (guild_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def remove_guild_data(guild_id: str) -> None:
    """Полная очистка данных сервера перед повторным сканированием."""
    async with connect() as conn:
        await conn.execute("DELETE FROM messages WHERE guild_id = ?", (guild_id,))
        await conn.execute("DELETE FROM scan_state WHERE guild_id = ?", (guild_id,))
        await conn.execute("DELETE FROM channels WHERE guild_id = ?", (guild_id,))
        await conn.commit()


# --------------------------------------------------------------------------- #
# scan state
# --------------------------------------------------------------------------- #
async def ensure_scan_states(guild_id: str, channels: Sequence[dict[str, Any]]) -> None:
    rows = [(str(ch["id"]), guild_id, ch.get("name") or "") for ch in channels]
    if not rows:
        return
    async with connect() as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO scan_state(channel_id, guild_id, name) VALUES(?, ?, ?)",
            rows,
        )
        await conn.commit()


async def get_scan_state(channel_id: str) -> dict[str, Any] | None:
    async with connect() as conn:
        cur = await conn.execute("SELECT * FROM scan_state WHERE channel_id = ?", (channel_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_scan_state(channel_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with connect() as conn:
        await conn.execute(
            f"UPDATE scan_state SET {cols} WHERE channel_id = ?",
            (*fields.values(), channel_id),
        )
        await conn.commit()


async def reset_scan_state(guild_id: str, channel_ids: Sequence[str] | None = None) -> None:
    async with connect() as conn:
        if channel_ids:
            marks = ",".join("?" * len(channel_ids))
            await conn.execute(
                f"""UPDATE scan_state SET status='pending', oldest_id=NULL, newest_id=NULL,
                    fetched=0, complete=0, last_error=NULL
                    WHERE guild_id = ? AND channel_id IN ({marks})""",
                (guild_id, *channel_ids),
            )
        else:
            await conn.execute(
                """UPDATE scan_state SET status='pending', oldest_id=NULL, newest_id=NULL,
                   fetched=0, complete=0, last_error=NULL WHERE guild_id = ?""",
                (guild_id,),
            )
        await conn.commit()


# --------------------------------------------------------------------------- #
# authors / messages
# --------------------------------------------------------------------------- #
def _snowflake_time(snowflake: str | int | None) -> int | None:
    """Время создания объекта Discord из snowflake (мс)."""
    try:
        return (int(snowflake) >> 22) + 1420070400000
    except (TypeError, ValueError):
        return None


async def upsert_authors(authors: Iterable[dict[str, Any]], guild_id: str | None = None) -> None:
    rows = []
    now = int(time.time())
    for a in authors:
        uid = str(a["id"])
        rows.append(
            (
                uid,
                a.get("username") or "",
                a.get("global_name"),
                a.get("discriminator") or "0",
                a.get("avatar"),
                1 if a.get("bot") else 0,
                1 if a.get("system") else 0,
                _snowflake_time(uid),
                a.get("nick"),
                a.get("joined_at_ts"),
                json.dumps(a["roles"], ensure_ascii=False) if a.get("roles") is not None else None,
                f"{a.get('username') or ''} {a.get('global_name') or ''}".strip().lower(),
                now,
            )
        )
    if not rows:
        return
    async with connect() as conn:
        await conn.executemany(
            """
            INSERT INTO authors(id, username, global_name, discriminator, avatar, bot, system,
                                account_created_at, nickname, joined_at, roles, name_lower, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                global_name = COALESCE(excluded.global_name, authors.global_name),
                discriminator = excluded.discriminator,
                avatar = COALESCE(excluded.avatar, authors.avatar),
                bot = excluded.bot,
                system = excluded.system,
                account_created_at = COALESCE(authors.account_created_at, excluded.account_created_at),
                nickname = COALESCE(excluded.nickname, authors.nickname),
                joined_at = COALESCE(excluded.joined_at, authors.joined_at),
                roles = COALESCE(excluded.roles, authors.roles),
                name_lower = excluded.name_lower,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        await conn.commit()


def message_to_row(msg: dict[str, Any], guild_id: str) -> tuple:
    author = msg.get("author") or {}
    ref = msg.get("referenced_message") or {}
    reference_id = (msg.get("message_reference") or {}).get("message_id") or ref.get("id")
    return (
        str(msg["id"]),
        guild_id,
        str(msg.get("channel_id") or ""),
        str(author.get("id") or "0"),
        msg.get("content") or "",
        _iso_to_ms(msg["timestamp"]) if msg.get("timestamp") else (_snowflake_time(msg["id"]) or 0),
        _iso_to_ms(msg["edited_timestamp"]) if msg.get("edited_timestamp") else None,
        msg.get("type", 0),
        str(reference_id) if reference_id else None,
        json.dumps(msg.get("attachments") or [], ensure_ascii=False),
        json.dumps(msg.get("embeds") or [], ensure_ascii=False),
        json.dumps(msg.get("reactions") or [], ensure_ascii=False),
        json.dumps(msg.get("mentions") or [], ensure_ascii=False),
        1 if msg.get("pinned") else 0,
        json.dumps(msg.get("sticker_items") or [], ensure_ascii=False),
        (msg.get("content") or "").lower(),
    )


def _iso_to_ms(value: str) -> int:
    """ISO-8601 из Discord -> epoch ms."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def insert_messages(rows: Sequence[tuple]) -> int:
    """Вставка сообщений; возвращает число реально новых записей."""
    if not rows:
        return 0
    normalized = [
        row if len(row) >= 16 else (*row, str(row[4] or "").lower()) for row in rows
    ]
    async with connect() as conn:
        before = conn.total_changes
        await conn.executemany(
            """
            INSERT OR IGNORE INTO messages(id, guild_id, channel_id, author_id, content, timestamp,
                edited_timestamp, type, reference_message_id, attachments, embeds, reactions,
                mentions, pinned, sticker_items, content_lower)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized,
        )
        inserted = conn.total_changes - before
        await conn.commit()
    return inserted


async def touch_message_aggregates(rows: Sequence[tuple]) -> None:
    """Обновляет first/last_message_at у авторов по вставленной пачке."""
    agg: dict[str, list[int]] = {}
    for row in rows:
        author_id, ts = row[3], row[5]
        bounds = agg.get(author_id)
        if bounds is None:
            agg[author_id] = [ts, ts]
        else:
            bounds[0] = min(bounds[0], ts)
            bounds[1] = max(bounds[1], ts)
    if not agg:
        return
    async with connect() as conn:
        await conn.executemany(
            """
            UPDATE authors SET
                first_message_at = MIN(COALESCE(first_message_at, ?), ?),
                last_message_at  = MAX(COALESCE(last_message_at, ?), ?)
            WHERE id = ?
            """,
            [(b[0], b[0], b[1], b[1], aid) for aid, b in agg.items()],
        )
        await conn.commit()


# --------------------------------------------------------------------------- #
# запросы панели
# --------------------------------------------------------------------------- #
async def stats(guild_id: str | None = None) -> dict[str, Any]:
    where, params = ("WHERE guild_id = ?", [guild_id]) if guild_id else ("", [])
    msg_where = f"{where} AND content <> ''" if where else "WHERE content <> ''"

    async with connect() as conn:
        async def scalar(sql: str, args: Sequence[Any] = ()) -> Any:
            cur = await conn.execute(sql, args)
            row = await cur.fetchone()
            return row[0] if row else 0

        total_messages = await scalar(f"SELECT COUNT(*) FROM messages {where}", params)
        total_authors = await scalar(
            f"SELECT COUNT(DISTINCT author_id) FROM messages {where}", params
        )
        total_channels = await scalar(
            f"SELECT COUNT(DISTINCT channel_id) FROM messages {where}", params
        )
        with_attachments = await scalar(
            f"SELECT COUNT(*) FROM messages {where + ' AND' if where else 'WHERE'} attachments <> '[]'",
            params,
        )
        guild_clause = "AND m.guild_id = ?" if guild_id else ""
        bots = await scalar(
            "SELECT COUNT(*) FROM messages m JOIN authors a ON a.id = m.author_id "
            f"WHERE a.bot = 1 {guild_clause}",
            params,
        )

        cur = await conn.execute(
            f"""SELECT date(timestamp / 1000, 'unixepoch') AS day, COUNT(*) AS c
                FROM messages {where}
                GROUP BY day ORDER BY day""",
            params,
        )
        per_day = [{"day": r["day"], "count": r["c"]} for r in await cur.fetchall() if r["day"]]

        cur = await conn.execute(
            f"""SELECT CAST(strftime('%H', timestamp / 1000, 'unixepoch') AS INTEGER) AS h,
                       COUNT(*) AS c
                FROM messages {where} GROUP BY h ORDER BY h""",
            params,
        )
        per_hour = [{"hour": r["h"], "count": r["c"]} for r in await cur.fetchall()]

        cur = await conn.execute(
            f"""SELECT m.channel_id, COALESCE(c.name, m.channel_id) AS name,
                       COUNT(*) AS c, COUNT(DISTINCT m.author_id) AS authors
                FROM messages m LEFT JOIN channels c ON c.id = m.channel_id
                {where.replace('guild_id', 'm.guild_id') if where else ''}
                GROUP BY m.channel_id ORDER BY c DESC LIMIT 100""",
            params,
        )
        per_channel = [dict(r) for r in await cur.fetchall()]

        cur = await conn.execute(
            f"""SELECT a.id, a.username, a.global_name, a.discriminator, a.avatar, a.bot,
                       COUNT(*) AS count
                FROM messages m JOIN authors a ON a.id = m.author_id
                {where.replace('guild_id', 'm.guild_id') if where else ''}
                GROUP BY a.id ORDER BY count DESC LIMIT 15""",
            params,
        )
        top_authors = [dict(r) for r in await cur.fetchall()]

        cur = await conn.execute(
            f"SELECT COUNT(*) FROM messages {msg_where}", params
        )
        text_messages = (await cur.fetchone())[0]

        cur = await conn.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM messages {where}", params
        )
        bounds = await cur.fetchone()

        scanned_complete = await scalar(
            "SELECT COUNT(*) FROM scan_state WHERE complete = 1"
            + (" AND guild_id = ?" if guild_id else ""),
            params,
        )
        scanned_total = await scalar(
            "SELECT COUNT(*) FROM scan_state" + (" WHERE guild_id = ?" if guild_id else ""),
            params,
        )

    return {
        "messages": total_messages,
        "text_messages": text_messages,
        "authors": total_authors,
        "channels": total_channels,
        "attachments": with_attachments,
        "bot_messages": bots,
        "per_day": per_day,
        "per_hour": per_hour,
        "per_channel": per_channel,
        "top_authors": top_authors,
        "first_message_at": bounds[0] if bounds else None,
        "last_message_at": bounds[1] if bounds else None,
        "scan": {"complete": scanned_complete, "total": scanned_total},
    }


MESSAGE_SORTS = {
    "newest": "m.timestamp DESC",
    "oldest": "m.timestamp ASC",
    "author": "a.username COLLATE NOCASE ASC, m.timestamp DESC",
}


async def query_messages(
    guild_id: str | None = None,
    *,
    search: str | None = None,
    author_id: str | None = None,
    channel_id: str | None = None,
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    clauses, params = [], []
    if guild_id:
        clauses.append("m.guild_id = ?")
        params.append(guild_id)
    if channel_id:
        clauses.append("m.channel_id = ?")
        params.append(channel_id)
    if author_id:
        clauses.append("m.author_id = ?")
        params.append(author_id)
    if search:
        clauses.append("(m.content_lower LIKE ? OR a.name_lower LIKE ?)")
        like = f"%{search.strip().lower()}%"
        params.extend([like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = MESSAGE_SORTS.get(sort, MESSAGE_SORTS["newest"])

    async with connect() as conn:
        cur = await conn.execute(
            f"SELECT COUNT(*) FROM messages m LEFT JOIN authors a ON a.id = m.author_id {where}",
            params,
        )
        total = (await cur.fetchone())[0]

        cur = await conn.execute(
            f"""
            SELECT m.id, m.content, m.timestamp, m.edited_timestamp, m.channel_id,
                   COALESCE(c.name, m.channel_id) AS channel_name,
                   m.author_id, a.username, a.global_name, a.discriminator, a.avatar, a.bot,
                   m.attachments, m.embeds, m.reactions, m.reference_message_id, m.pinned
            FROM messages m
            LEFT JOIN authors a ON a.id = m.author_id
            LEFT JOIN channels c ON c.id = m.channel_id
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        items = []
        for r in await cur.fetchall():
            item = dict(r)
            for key in ("attachments", "embeds", "reactions"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else []
                except (TypeError, ValueError):
                    item[key] = []
            items.append(item)

    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def query_authors(
    guild_id: str | None = None,
    *,
    search: str | None = None,
    sort: str = "messages",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Список авторов с агрегатами по сообщениям выбранного сервера."""
    inner_where = "WHERE guild_id = ?" if guild_id else ""
    inner_params: list[Any] = [guild_id] if guild_id else []

    outer, outer_params = [], []
    if search:
        outer.append("(a.name_lower LIKE ? OR a.id = ?)")
        like = f"%{search.strip().lower()}%"
        outer_params.extend([like, search.strip()])
    outer_where = ("WHERE " + " AND ".join(outer)) if outer else ""

    order = {
        "messages": "message_count DESC",
        "name": "COALESCE(a.global_name, a.username) COLLATE NOCASE ASC",
        "recent": "last_message_at DESC",
        "created": "a.account_created_at ASC",
    }.get(sort, "message_count DESC")

    base = f"""
        FROM authors a
        JOIN (
            SELECT author_id, COUNT(*) AS message_count,
                   MIN(timestamp) AS first_message_at, MAX(timestamp) AS last_message_at
            FROM messages {inner_where}
            GROUP BY author_id
        ) m ON m.author_id = a.id
        {outer_where}
    """
    params = [*inner_params, *outer_params]
    async with connect() as conn:
        cur = await conn.execute(f"SELECT COUNT(*) {base}", params)
        total = (await cur.fetchone())[0]
        cur = await conn.execute(
            f"""
            SELECT a.id, a.username, a.global_name, a.discriminator, a.avatar, a.bot, a.system,
                   a.account_created_at, a.nickname, a.joined_at,
                   m.message_count, m.first_message_at, m.last_message_at
            {base}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        items = [dict(r) for r in await cur.fetchall()]
    return {"total": total, "limit": limit, "offset": offset, "items": items}


async def get_author(author_id: str, guild_id: str | None = None) -> dict[str, Any] | None:
    """Карточка автора: агрегаты, каналы, активность по дням, последние сообщения."""
    guild_clause = "AND m.guild_id = ?" if guild_id else ""
    extra = [guild_id] if guild_id else []

    async with connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM authors WHERE id = ?", (author_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        author = dict(row)
        author["roles"] = json.loads(author["roles"]) if author.get("roles") else []

        cur = await conn.execute(
            f"""SELECT COUNT(*) AS message_count,
                       MIN(m.timestamp) AS first_message_at,
                       MAX(m.timestamp) AS last_message_at
                FROM messages m WHERE m.author_id = ? {guild_clause}""",
            [author_id, *extra],
        )
        agg = await cur.fetchone()
        author.update(dict(agg) if agg else {})

        cur = await conn.execute(
            f"""SELECT m.channel_id, COALESCE(c.name, m.channel_id) AS name, COUNT(*) AS count
                FROM messages m LEFT JOIN channels c ON c.id = m.channel_id
                WHERE m.author_id = ? {guild_clause}
                GROUP BY m.channel_id ORDER BY count DESC LIMIT 20""",
            [author_id, *extra],
        )
        author["channels"] = [dict(r) for r in await cur.fetchall()]

        cur = await conn.execute(
            f"""SELECT date(timestamp / 1000, 'unixepoch') AS day, COUNT(*) AS count
                FROM messages m WHERE m.author_id = ? {guild_clause}
                GROUP BY day ORDER BY day""",
            [author_id, *extra],
        )
        author["per_day"] = [dict(r) for r in await cur.fetchall() if r["day"]]

        cur = await conn.execute(
            f"""SELECT id, content, timestamp, channel_id FROM messages m
                WHERE m.author_id = ? {guild_clause}
                ORDER BY timestamp DESC LIMIT 10""",
            [author_id, *extra],
        )
        author["recent"] = [dict(r) for r in await cur.fetchall()]

    return author


async def get_message(message_id: str) -> dict[str, Any] | None:
    """Одно сообщение с автором и каналом."""
    async with connect() as conn:
        cur = await conn.execute(
            """
            SELECT m.*, COALESCE(c.name, m.channel_id) AS channel_name,
                   COALESCE(a.global_name, a.username, m.author_id) AS author_name,
                   a.username, a.avatar, a.bot
            FROM messages m
            LEFT JOIN authors a ON a.id = m.author_id
            LEFT JOIN channels c ON c.id = m.channel_id
            WHERE m.id = ?
            """,
            (message_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        item = dict(row)
        for key in ("attachments", "embeds", "reactions", "mentions", "sticker_items"):
            try:
                item[key] = json.loads(item[key]) if item[key] else []
            except (TypeError, ValueError):
                item[key] = []
        item["timestamp_iso"] = item["timestamp"]
        return item


async def iter_messages_export(guild_id: str | None, chunk: int = 1000):
    """Асинхронный генератор строк для экспорта сообщений."""
    where, params = ("WHERE m.guild_id = ?", [guild_id]) if guild_id else ("", [])
    async with connect() as conn:
        offset = 0
        while True:
            cur = await conn.execute(
                f"""
                SELECT m.id, m.timestamp, m.channel_id,
                       COALESCE(c.name, m.channel_id) AS channel_name,
                       m.author_id,
                       COALESCE(a.global_name, a.username, m.author_id) AS author_name,
                       a.username, a.discriminator, a.bot,
                       m.content, m.attachments, m.reactions, m.edited_timestamp,
                       m.reference_message_id
                FROM messages m
                LEFT JOIN authors a ON a.id = m.author_id
                LEFT JOIN channels c ON c.id = m.channel_id
                {where}
                ORDER BY m.timestamp ASC
                LIMIT ? OFFSET ?
                """,
                (*params, chunk, offset),
            )
            rows = await cur.fetchall()
            if not rows:
                break
            for r in rows:
                yield dict(r)
            offset += len(rows)


async def iter_authors_export(guild_id: str | None, chunk: int = 1000):
    where, params = ("WHERE m.guild_id = ?", [guild_id]) if guild_id else ("", [])
    async with connect() as conn:
        cur = await conn.execute(
            f"""
            SELECT a.id, a.username, a.global_name, a.discriminator, a.bot,
                   a.account_created_at, a.joined_at,
                   COUNT(m.id) AS message_count,
                   MIN(m.timestamp) AS first_message_at,
                   MAX(m.timestamp) AS last_message_at
            FROM authors a JOIN messages m ON m.author_id = a.id
            {where}
            GROUP BY a.id ORDER BY message_count DESC
            """,
            params,
        )
        for r in await cur.fetchall():
            yield dict(r)


async def top_media(guild_id: str | None, limit: int = 12) -> list[dict[str, Any]]:
    """Самые частые домены во вложениях — быстрый «чем живёт сервер»."""
    where, params = ("AND guild_id = ?", [guild_id]) if guild_id else ("", [])
    async with connect() as conn:
        cur = await conn.execute(
            f"""SELECT attachments FROM messages
                WHERE attachments <> '[]' {where} ORDER BY timestamp DESC LIMIT 5000""",
            params,
        )
        rows = await cur.fetchall()
    hosts: dict[str, int] = {}
    for r in rows:
        try:
            for att in json.loads(r["attachments"]):
                url = att.get("proxy_url") or att.get("url") or ""
                host = url.split("/")[2] if "://" in url else "other"
                hosts[host] = hosts.get(host, 0) + 1
        except (TypeError, ValueError, IndexError):
            continue
    return [{"host": h, "count": c} for h, c in sorted(hosts.items(), key=lambda x: -x[1])[:limit]]


async def scan_overview(guild_id: str | None = None) -> dict[str, Any]:
    where, params = ("WHERE guild_id = ?", [guild_id]) if guild_id else ("", [])
    async with connect() as conn:
        cur = await conn.execute(
            f"""SELECT status, COUNT(*) AS count, SUM(fetched) AS fetched, SUM(complete) AS complete
                FROM scan_state {where} GROUP BY status""",
            params,
        )
        by_status = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            f"""SELECT channel_id, name, status, fetched, complete, last_error, last_run_at
                FROM scan_state {where} ORDER BY fetched DESC LIMIT 200""",
            params,
        )
        channels = [dict(r) for r in await cur.fetchall()]
    return {"by_status": by_status, "channels": channels}
