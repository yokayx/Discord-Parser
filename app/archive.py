"""Архив: текстовые файлы по каналам с подписями «автор + время».

Каталог архива по умолчанию: `data/archive/<сервер>/<канал>.txt`.
Каждое сообщение — отдельная строка с подписью `[дата время] Автор (@username): текст`,
между сообщениями — разделитель `\\n` (настраивается).
"""
from __future__ import annotations

import json
import re
import unicodedata
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import db
from .config import DATA_DIR

ARCHIVE_DIR = Path(__import__("os").getenv("DP_ARCHIVE_DIR", DATA_DIR / "archive"))
ZIP_DIR = DATA_DIR / "exports"

SETTING_FORMAT = "archive_format"
SETTING_SEPARATOR = "archive_separator"
SETTING_TIME_FORMAT = "archive_time_format"
SETTING_AUTO = "archive_auto"
SETTING_ATTACHMENTS = "archive_attachments"
SETTING_IDS = "archive_ids"

DEFAULT_FORMAT = "line"
DEFAULT_SEPARATOR = "\\n"
DEFAULT_TIME_FORMAT = "%d.%m.%Y %H:%M"
TIME_PRESETS = {
    "dmy_hm": "%d.%m.%Y %H:%M",
    "ymd_hm": "%Y-%m-%d %H:%M",
    "dmy_hms": "%d.%m.%Y %H:%M:%S",
    "iso": "%Y-%m-%dT%H:%M:%S",
}
SEPARATOR_PRESETS = {"\\n": "\n", "\\n\\n": "\n\n", "---": "\n---\n"}
FORMATS = ("line", "block")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify(value: str, fallback: str = "channel", limit: int = 60) -> str:
    """Безопасное имя файла с сохранением кириллицы."""
    cleaned = _UNSAFE.sub("_", str(value or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = fallback
    return cleaned[:limit].strip("_") or fallback


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate(value: str) -> str:
    """Русские буквы -> латиница (для имён файлов, zip и HTTP-заголовков)."""
    out = []
    for char in str(value or ""):
        lower = char.lower()
        mapped = _TRANSLIT.get(lower)
        if mapped is None:
            out.append(char)
        elif char.isupper():
            out.append(mapped.capitalize())
        else:
            out.append(mapped)
    return "".join(out)


def ascii_slug(value: str, fallback: str = "guild", limit: int = 40) -> str:
    """ASCII-имя файла (заголовки HTTP и zip не любят кириллицу)."""
    normalized = unicodedata.normalize("NFKD", transliterate(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = slugify(normalized, fallback=fallback, limit=limit).strip("-_")
    return cleaned if cleaned and cleaned.isascii() else fallback


@dataclass
class ArchiveOptions:
    """Параметры формирования архива."""

    format: str = DEFAULT_FORMAT          # line | block
    separator: str = DEFAULT_SEPARATOR    # "\n" (в виде escape-строки) или готовый разделитель
    time_format: str = DEFAULT_TIME_FORMAT
    include_attachments: bool = True
    include_ids: bool = False
    header: bool = True
    clean: bool = False                   # удалить прежние файлы сервера

    @property
    def sep(self) -> str:
        return SEPARATOR_PRESETS.get(self.separator, self.separator or "\n")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "separator": self.separator,
            "time_format": self.time_format,
            "include_attachments": self.include_attachments,
            "include_ids": self.include_ids,
            "header": self.header,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, clean: bool = False) -> "ArchiveOptions":
        data = data or {}
        fmt = data.get("format") if data.get("format") in FORMATS else DEFAULT_FORMAT
        sep = data.get("separator") or DEFAULT_SEPARATOR
        if not isinstance(sep, str):
            sep = DEFAULT_SEPARATOR
        time_format = data.get("time_format") or DEFAULT_TIME_FORMAT
        preset = TIME_PRESETS.get(time_format)
        if preset:
            time_format = preset
        return cls(
            format=fmt,
            separator=sep,
            time_format=time_format,
            include_attachments=bool(data.get("include_attachments", True)),
            include_ids=bool(data.get("include_ids", False)),
            header=bool(data.get("header", True)),
            clean=clean,
        )


@dataclass
class ArchiveResult:
    guild_id: str
    path: str
    files: list[dict[str, Any]] = field(default_factory=list)
    messages: int = 0
    bytes_written: int = 0
    skipped_channels: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    generated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "path": self.path,
            "files": self.files,
            "messages": self.messages,
            "bytes": self.bytes_written,
            "skipped_channels": self.skipped_channels,
            "options": self.options,
            "generated_at": self.generated_at,
        }


# --------------------------------------------------------------------------- #
# настройки
# --------------------------------------------------------------------------- #
async def load_options() -> ArchiveOptions:
    """Читает параметры архива из БД (по умолчанию — как просили: \\n и подпись)."""
    raw = await db.get_setting("archive_options")
    data: dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
    if not data:
        data = {
            "format": await db.get_setting(SETTING_FORMAT) or DEFAULT_FORMAT,
            "separator": await db.get_setting(SETTING_SEPARATOR) or DEFAULT_SEPARATOR,
            "time_format": await db.get_setting(SETTING_TIME_FORMAT) or DEFAULT_TIME_FORMAT,
        }
    return ArchiveOptions.from_dict(data)


async def save_options(options: ArchiveOptions) -> None:
    await db.set_setting("archive_options", json.dumps(options.to_dict(), ensure_ascii=False))
    await db.set_setting(SETTING_FORMAT, options.format)
    await db.set_setting(SETTING_SEPARATOR, options.separator)
    await db.set_setting(SETTING_TIME_FORMAT, options.time_format)


async def auto_enabled() -> bool:
    value = await db.get_setting(SETTING_AUTO)
    return value in (None, "1", "true", "True")


async def set_auto(value: bool) -> None:
    await db.set_setting(SETTING_AUTO, "1" if value else "0")


# --------------------------------------------------------------------------- #
# пути
# --------------------------------------------------------------------------- #
def guild_archive_dir(guild_id: str, guild_name: str | None = None) -> Path:
    name = slugify(guild_name or guild_id, fallback=f"guild-{guild_id}")
    suffix = guild_id[-4:] if len(guild_id) > 4 else guild_id
    return ARCHIVE_DIR / f"{name}-{suffix}"


def _format_time(ms: int | None, time_format: str) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime(time_format)


def _sep_display(sep: str) -> str:
    """Разделитель в человекочитаемом виде: реальные переводы строк -> \\n."""
    escaped = sep.replace("\n", "\\n")
    return f"'{escaped}'"


def format_message(row: dict[str, Any], options: ArchiveOptions) -> str:
    """Одна запись архива: подпись автора и времени + текст."""
    author = row.get("author_name") or row.get("username") or "неизвестный"
    username = row.get("username") or ""
    meta = f"@{username}" if username and username != author else ""
    if row.get("bot"):
        meta = f"{meta}, бот" if meta else "бот"
    signature = f"[{_format_time(row['timestamp'], options.time_format)}] {author}"
    if meta:
        signature += f" ({meta})"
    if options.include_ids:
        signature += f" #{row['id']}"
    if row.get("edited_timestamp"):
        signature += " (изменено)"

    content = (row.get("content") or "").strip()
    if not content:
        content = "(без текста)"

    if options.include_attachments:
        try:
            attachments = json.loads(row.get("attachments") or "[]")
        except (TypeError, ValueError):
            attachments = []
        if attachments:
            names = [a.get("filename") or a.get("url") or "файл" for a in attachments]
            content += f" [вложения: {', '.join(names)}]"

    if options.format == "block":
        return f"{signature}{options.sep}{content}"
    return f"{signature}: {content}"


def render_channel(channel: dict[str, Any], guild: dict[str, Any] | None,
                   rows: Iterable[dict[str, Any]], options: ArchiveOptions) -> tuple[str, int]:
    """Собирает содержимое файла канала и число сообщений."""
    entries: list[str] = []
    count = 0
    for row in rows:
        entries.append(format_message(row, options))
        count += 1

    body = options.sep.join(entries)
    if body:
        body += "\n"

    header = ""
    if options.header:
        guild_name = (guild or {}).get("name") or (guild or {}).get("id") or ""
        title = f"#{channel.get('name') or channel.get('id')}"
        if channel.get("is_thread"):
            title = f"{title} (ветка)"
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        header_lines = [
            f"# Архив канала {title} — {guild_name}",
            f"# файл создан: {stamp}",
            f"# сообщений: {count}",
            f"# формат: [время] Автор (@username): текст; "
            f"разделитель сообщений: {_sep_display(options.sep)}",
            "",
        ]
        header = "\n".join(header_lines) + "\n"

    return header + body, count


# --------------------------------------------------------------------------- #
# запись
# --------------------------------------------------------------------------- #
async def build_guild_archive(guild_id: str, options: ArchiveOptions | None = None) -> ArchiveResult:
    """Создаёт/перезаписывает файлы канала для сервера и возвращает отчёт."""
    options = options or await load_options()
    guild = await db.get_guild_row(guild_id) or {"id": guild_id, "name": guild_id}
    target = guild_archive_dir(guild_id, guild.get("name"))
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    if options.clean and target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    channels = await db.list_channels(guild_id)
    result = ArchiveResult(
        guild_id=guild_id,
        path=str(target),
        options=options.to_dict(),
        generated_at=int(time.time() * 1000),
    )
    used_names: set[str] = set()

    for channel in channels:
        messages = [row async for row in db.iter_channel_messages(str(channel["id"]))]
        if not messages:
            result.skipped_channels += 1
            continue

        base = slugify(channel.get("name") or channel["id"])
        if channel.get("is_thread"):
            parent = next(
                (c for c in channels if str(c["id"]) == str(channel.get("parent_id"))), None
            )
            if parent:
                base = f"{slugify(parent.get('name') or parent['id'])}--{base}"
        name = base
        counter = 2
        while name.lower() in used_names:
            name = f"{base}-{counter}"
            counter += 1
        used_names.add(name.lower())

        content, count = render_channel(channel, guild, messages, options)
        file_path = target / f"{name}.txt"
        file_path.write_text(content, encoding="utf-8")

        result.messages += count
        size = file_path.stat().st_size
        result.bytes_written += size
        result.files.append(
            {
                "channel_id": channel["id"],
                "channel": channel.get("name") or channel["id"],
                "file": file_path.name,
                "messages": count,
                "bytes": size,
            }
        )

    # авторы сервера отдельным файлом
    author_lines = ["# Авторы сервера " + (guild.get("name") or guild_id), ""]
    author_count = 0
    async for row in db.iter_authors_export(guild_id):
        author_count += 1
        name = row.get("global_name") or row.get("username") or row["id"]
        handle = f" (@{row['username']})" if row.get("username") else ""
        bot = " [бот]" if row.get("bot") else ""
        author_lines.append(
            f"{name}{handle}{bot}: {row.get('message_count') or 0} сообщ. · "
            f"первое {_format_time(row.get('first_message_at'), options.time_format)} · "
            f"последнее {_format_time(row.get('last_message_at'), options.time_format)} · "
            f"id {row['id']}"
        )
    if author_count:
        authors_path = target / "_authors.txt"
        authors_path.write_text("\n".join(author_lines) + "\n", encoding="utf-8")
        size = authors_path.stat().st_size
        result.bytes_written += size
        result.files.append(
            {
                "channel_id": None,
                "channel": "авторы",
                "file": authors_path.name,
                "messages": author_count,
                "bytes": size,
            }
        )

    # индекс архива
    index_lines = [
        f"Архив сервера {guild.get('name') or guild_id}",
        f"создан: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"файлов: {len(result.files)} · сообщений: {result.messages}",
        f"разделитель сообщений: {_sep_display(options.sep)} · формат: {options.format}",
        "",
    ]
    for item in result.files:
        if item["channel_id"] is None:
            index_lines.append(f"{item['file']} — {item['messages']} авторов")
        else:
            index_lines.append(
                f"{item['file']} — {item['messages']} сообщ. (#{item['channel']})"
            )
    (target / "_index.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    await db.set_setting(
        f"archive_last_{guild_id}", json.dumps(result.to_dict(), ensure_ascii=False)
    )
    return result


async def last_result(guild_id: str) -> dict[str, Any] | None:
    raw = await db.get_setting(f"archive_last_{guild_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def archive_info(guild_id: str, guild_name: str | None = None) -> dict[str, Any]:
    """Сводка по архиву на диске + текущие настройки."""
    options = await load_options()
    guild = await db.get_guild_row(guild_id)
    target = guild_archive_dir(guild_id, guild_name or (guild or {}).get("name"))
    files: list[dict[str, Any]] = []
    total_size = 0
    if target.exists():
        for path in sorted(target.glob("*.txt"), key=lambda p: (p.name == "_index.txt", p.name)):
            stat = path.stat()
            total_size += stat.st_size
            files.append(
                {
                    "file": path.name,
                    "bytes": stat.st_size,
                    "modified_at": int(stat.st_mtime * 1000),
                }
            )
    return {
        "guild_id": guild_id,
        "path": str(target),
        "exists": target.exists(),
        "root": str(ARCHIVE_DIR),
        "files": files,
        "total_bytes": total_size,
        "auto": await auto_enabled(),
        "options": options.to_dict(),
        "last": await last_result(guild_id),
        "time_presets": TIME_PRESETS,
        "separator_presets": {"\\n": "\\n", "\\n\\n": "\\n\\n", "---": "---"},
    }


def build_zip(guild_id: str, guild_name: str | None = None) -> Path:
    """Упаковывает каталог архива сервера в zip и возвращает путь к файлу."""
    source = guild_archive_dir(guild_id, guild_name)
    if not source.exists():
        raise FileNotFoundError("Архив ещё не создан")
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_path = ZIP_DIR / f"archive-{ascii_slug(guild_name or guild_id)}-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*.txt")):
            zf.write(path, arcname=f"{source.name}/{path.name}")
    # подчищаем старые архивы, оставляя 5 последних
    old = sorted(ZIP_DIR.glob("archive-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in old[5:]:
        path.unlink(missing_ok=True)
    return zip_path


def archive_files(guild_id: str, guild_name: str | None = None) -> list[Path]:
    source = guild_archive_dir(guild_id, guild_name)
    if not source.exists():
        return []
    return sorted(source.glob("*.txt"))


def read_archive_file(guild_id: str, filename: str, guild_name: str | None = None) -> Path:
    source = guild_archive_dir(guild_id, guild_name).resolve()
    target = (source / Path(filename).name).resolve()
    if not str(target).startswith(str(source)) or target.suffix != ".txt" or not target.exists():
        raise FileNotFoundError(filename)
    return target
