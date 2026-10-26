"""ZIP-архив итогов парсинга: сервер → папки категорий → файлы каналов.

Структура внутри архива:

    <Сервер>.zip
    └── <Сервер>/
        ├── general.txt              # канал без категории
        ├── Текстовые каналы/        # папка категории
        │   ├── флудилка.txt         # файл канала
        │   └── dev-chat.txt
        ├── _authors.txt
        └── _index.txt

Каждое сообщение — отдельная запись с подписью `[дата время] Автор (@username): текст`,
между записями разделитель `\\n` (настраивается).
"""
from __future__ import annotations

import json
import re
import shutil
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import db
from .config import DATA_DIR

import os

ARCHIVE_DIR = Path(os.getenv("DP_ARCHIVE_DIR", DATA_DIR / "archive"))
BUILD_DIR = DATA_DIR / ".archive_build"

SETTING_FORMAT = "archive_format"
SETTING_SEPARATOR = "archive_separator"
SETTING_TIME_FORMAT = "archive_time_format"
SETTING_AUTO = "archive_auto"

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

CATEGORY_TYPE = 4
TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12, 15}
UNCATEGORIZED = "Без категории"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------------------- #
# имена
# --------------------------------------------------------------------------- #
def slugify(value: str, fallback: str = "channel", limit: int = 60) -> str:
    """Безопасное имя файла/папки с сохранением кириллицы."""
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
    """Русские буквы -> латиница (для имён файлов и HTTP-заголовков)."""
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
    """ASCII-имя файла: zip и заголовки HTTP не любят кириллицу."""
    normalized = unicodedata.normalize("NFKD", transliterate(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = slugify(normalized, fallback=fallback, limit=limit).strip("-_")
    return cleaned if cleaned and cleaned.isascii() else fallback


# --------------------------------------------------------------------------- #
# настройки формата
# --------------------------------------------------------------------------- #
@dataclass
class ArchiveOptions:
    """Параметры формирования текста внутри файлов архива."""

    format: str = DEFAULT_FORMAT          # line | block
    separator: str = DEFAULT_SEPARATOR    # escape-строка "\n" или готовый разделитель
    time_format: str = DEFAULT_TIME_FORMAT
    include_attachments: bool = True
    include_ids: bool = False
    header: bool = True

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
    def from_dict(cls, data: dict[str, Any] | None) -> "ArchiveOptions":
        data = data or {}
        fmt = data.get("format") if data.get("format") in FORMATS else DEFAULT_FORMAT
        sep = data.get("separator") or DEFAULT_SEPARATOR
        if not isinstance(sep, str):
            sep = DEFAULT_SEPARATOR
        time_format = data.get("time_format") or DEFAULT_TIME_FORMAT
        time_format = TIME_PRESETS.get(time_format, time_format)
        return cls(
            format=fmt,
            separator=sep,
            time_format=time_format,
            include_attachments=bool(data.get("include_attachments", True)),
            include_ids=bool(data.get("include_ids", False)),
            header=bool(data.get("header", True)),
        )


@dataclass
class ArchiveResult:
    guild_id: str
    zip_path: str
    zip_name: str
    root_folder: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    messages: int = 0
    authors: int = 0
    bytes: int = 0
    skipped_channels: int = 0
    options: dict[str, Any] = field(default_factory=dict)
    generated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "zip_path": self.zip_path,
            "zip_name": self.zip_name,
            "root_folder": self.root_folder,
            "entries": self.entries,
            "files": sum(1 for e in self.entries if e["kind"] == "file"),
            "folders": sum(1 for e in self.entries if e["kind"] == "folder"),
            "messages": self.messages,
            "authors": self.authors,
            "bytes": self.bytes,
            "skipped_channels": self.skipped_channels,
            "options": self.options,
            "generated_at": self.generated_at,
        }


async def load_options() -> ArchiveOptions:
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
    return await db.get_setting(SETTING_AUTO) in (None, "1", "true", "True")


async def set_auto(value: bool) -> None:
    await db.set_setting(SETTING_AUTO, "1" if value else "0")


# --------------------------------------------------------------------------- #
# текст сообщений
# --------------------------------------------------------------------------- #
def _format_time(ms: int | None, time_format: str) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime(time_format)


def _sep_display(sep: str) -> str:
    return f"'{sep.replace(chr(10), chr(92) + 'n')}'"


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

    content = (row.get("content") or "").strip() or "(без текста)"

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


def render_channel(
    channel: dict[str, Any],
    guild: dict[str, Any] | None,
    rows: Iterable[dict[str, Any]],
    options: ArchiveOptions,
    folder: str = "",
) -> tuple[str, int]:
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
        if folder:
            title += f" · категория «{folder}»"
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
# пути архива
# --------------------------------------------------------------------------- #
def guild_zip_path(guild_id: str, guild_name: str | None = None) -> Path:
    """Один сервер = один zip-архив."""
    name = ascii_slug(guild_name or guild_id, fallback=f"guild-{guild_id}", limit=48)
    suffix = guild_id[-4:] if len(guild_id) > 4 else guild_id
    return ARCHIVE_DIR / f"{name}-{suffix}.zip"


def guild_root_folder(guild_id: str, guild_name: str | None = None) -> str:
    """Имя корневой папки сервера внутри zip."""
    return slugify(guild_name or guild_id, fallback=f"guild-{guild_id}")


# --------------------------------------------------------------------------- #
# сборка
# --------------------------------------------------------------------------- #
def _channel_folder(
    channel: dict[str, Any], channels_by_id: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """Возвращает (папка категории, имя файла) для канала или ветки."""
    name = slugify(channel.get("name") or channel["id"])
    parent_id = str(channel.get("parent_id") or "")

    if channel.get("is_thread"):
        parent = channels_by_id.get(parent_id)
        if parent:
            name = f"{slugify(parent.get('name') or parent['id'])}--{name}"
            parent_id = str(parent.get("parent_id") or "")

    parent = channels_by_id.get(parent_id)
    if parent and int(parent.get("type") or 0) == CATEGORY_TYPE:
        return slugify(parent.get("name") or parent["id"]), name
    return "", name


async def build_guild_archive(
    guild_id: str, options: ArchiveOptions | None = None
) -> ArchiveResult:
    """Собирает итоги парсинга в ZIP: папки категорий, файлы каналов."""
    options = options or await load_options()
    guild = await db.get_guild_row(guild_id) or {"id": guild_id, "name": guild_id}
    guild_name = guild.get("name") or guild_id

    root_folder = guild_root_folder(guild_id, guild_name)
    staging = BUILD_DIR / f"{guild_id}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    channels = await db.list_channels(guild_id)
    channels_by_id = {str(c["id"]): c for c in channels}

    result = ArchiveResult(
        guild_id=guild_id,
        zip_path="",
        zip_name="",
        root_folder=root_folder,
        options=options.to_dict(),
        generated_at=int(time.time() * 1000),
    )
    used_paths: set[str] = set()

    for channel in channels:
        ctype = int(channel.get("type") or 0)
        if ctype not in TEXT_CHANNEL_TYPES:
            continue

        rows = [row async for row in db.iter_channel_messages(str(channel["id"]))]
        if not rows:
            result.skipped_channels += 1
            continue

        folder, base = _channel_folder(channel, channels_by_id)
        name = base
        counter = 2
        rel_key = f"{folder}/{name}".lower()
        while rel_key in used_paths:
            name = f"{base}-{counter}"
            rel_key = f"{folder}/{name}".lower()
            counter += 1
        used_paths.add(rel_key)

        content, count = render_channel(channel, guild, rows, options, folder)
        directory = staging / folder if folder else staging
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / f"{name}.txt"
        file_path.write_text(content, encoding="utf-8")

        result.messages += count
        result.entries.append(
            {
                "kind": "file",
                "path": f"{root_folder}/{folder}/{name}.txt" if folder
                else f"{root_folder}/{name}.txt",
                "folder": folder,
                "channel": channel.get("name") or channel["id"],
                "channel_id": channel["id"],
                "messages": count,
                "bytes": file_path.stat().st_size,
            }
        )

    # авторы сервера
    author_lines = [f"# Авторы сервера {guild_name}", ""]
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
    result.authors = author_count
    if author_count:
        authors_path = staging / "_authors.txt"
        authors_path.write_text("\n".join(author_lines) + "\n", encoding="utf-8")
        result.entries.append(
            {
                "kind": "file",
                "path": f"{root_folder}/_authors.txt",
                "folder": "",
                "channel": "авторы",
                "channel_id": None,
                "messages": author_count,
                "bytes": authors_path.stat().st_size,
            }
        )

    # индекс архива
    index_lines = [
        f"Архив сервера {guild_name}",
        f"создан: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"сообщений: {result.messages} · авторов: {result.authors}",
        f"разделитель сообщений: {_sep_display(options.sep)} · формат: {options.format}",
        "",
    ]
    current_folder = None
    for item in result.entries:
        if item["channel_id"] is None:
            index_lines.append(f"{item['path']} — {item['messages']} авторов")
            continue
        folder = item["folder"]
        if folder != current_folder:
            current_folder = folder
            index_lines.append("")
            index_lines.append(f"[{folder or UNCATEGORIZED}]")
        index_lines.append(f"  {item['path'].split('/', 1)[-1]} — {item['messages']} сообщ.")
    (staging / "_index.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    # упаковка
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = guild_zip_path(guild_id, guild_name)
    tmp_zip = zip_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_dir():
                continue
            arcname = f"{root_folder}/{path.relative_to(staging).as_posix()}"
            zf.write(path, arcname=arcname)
    tmp_zip.replace(zip_path)
    shutil.rmtree(staging, ignore_errors=True)

    result.zip_path = str(zip_path)
    result.zip_name = zip_path.name
    result.bytes = zip_path.stat().st_size

    await db.set_setting(
        f"archive_last_{guild_id}", json.dumps(result.to_dict(), ensure_ascii=False)
    )
    return result


# --------------------------------------------------------------------------- #
# чтение
# --------------------------------------------------------------------------- #
async def last_result(guild_id: str) -> dict[str, Any] | None:
    raw = await db.get_setting(f"archive_last_{guild_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def list_entries(zip_path: Path) -> list[dict[str, Any]]:
    """Содержимое zip: папки и файлы в порядке дерева."""
    if not zip_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    seen_dirs: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        infos = sorted(zf.infolist(), key=lambda i: i.filename)
        for info in infos:
            parts = info.filename.rstrip("/").split("/")
            for depth in range(1, len(parts)):
                folder = "/".join(parts[:depth])
                if folder not in seen_dirs:
                    seen_dirs.add(folder)
                    entries.append(
                        {"kind": "folder", "path": folder, "name": parts[depth - 1], "bytes": 0}
                    )
            if info.is_dir():
                continue
            entries.append(
                {
                    "kind": "file",
                    "path": info.filename,
                    "name": parts[-1],
                    "bytes": info.file_size,
                    "packed": info.compress_size,
                }
            )
    folder_paths = {e["path"] for e in entries if e["kind"] == "folder"}

    def tree_key(entry: dict[str, Any]) -> tuple:
        parts = entry["path"].split("/")
        key: list[tuple[int, str]] = []
        for index, part in enumerate(parts):
            prefix = "/".join(parts[: index + 1])
            is_folder = prefix in folder_paths
            key.append((0 if is_folder else 1, part.lower()))
        return tuple(key)

    entries.sort(key=tree_key)
    return entries


async def archive_info(guild_id: str, guild_name: str | None = None) -> dict[str, Any]:
    """Состояние zip-архива сервера + настройки."""
    options = await load_options()
    guild = await db.get_guild_row(guild_id)
    name = guild_name or (guild or {}).get("name")
    zip_path = guild_zip_path(guild_id, name)
    entries = list_entries(zip_path)
    return {
        "guild_id": guild_id,
        "zip_path": str(zip_path),
        "zip_name": zip_path.name,
        "exists": zip_path.exists(),
        "bytes": zip_path.stat().st_size if zip_path.exists() else 0,
        "root_folder": guild_root_folder(guild_id, name),
        "entries": entries,
        "auto": await auto_enabled(),
        "options": options.to_dict(),
        "last": await last_result(guild_id),
    }


async def read_entry(
    guild_id: str, entry_path: str, guild_name: str | None = None, limit: int = 200_000
) -> dict[str, Any]:
    """Читает файл из zip для предпросмотра."""
    if not guild_name:
        guild = await db.get_guild_row(guild_id)
        guild_name = (guild or {}).get("name")
    zip_path = guild_zip_path(guild_id, guild_name)
    if not zip_path.exists():
        raise FileNotFoundError("Архив ещё не создан")
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        target = entry_path.strip("/")
        if target not in names:
            # допускаем короткое имя файла без папок
            matches = [n for n in names if n.rsplit("/", 1)[-1] == target]
            if not matches:
                raise FileNotFoundError(entry_path)
            target = matches[0]
        info = zf.getinfo(target)
        data = zf.read(target)
    text = data.decode("utf-8", errors="replace")
    return {
        "path": target,
        "name": info.filename.rsplit("/", 1)[-1],
        "bytes": info.file_size,
        "truncated": len(text) > limit,
        "content": text[:limit],
    }


def delete_archive(guild_id: str, guild_name: str | None = None) -> None:
    guild_zip_path(guild_id, guild_name).unlink(missing_ok=True)
