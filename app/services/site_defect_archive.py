from __future__ import annotations

import base64
import csv
import html
import json
import mimetypes
import re
import time as time_module
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models import (
    SiteDefectArchiveCase,
    SiteDefectArchiveFile,
    SiteDefectArchiveMessage,
)
from app.services.expertise_bitrix import BitrixRestClient

SOURCE = "old_bitrix_chat"
DEFAULT_DIALOG_ID = "chat69465"
ARCHIVE_STATUS = "archive"

PROBLEM_TYPE_MODEL_MISMATCH = "model_mismatch"
PROBLEM_TYPE_RETURN = "return"
PROBLEM_TYPE_MONEY_REFUND = "money_refund"
PROBLEM_TYPE_DELIVERY = "delivery"
PROBLEM_TYPE_EXPERTISE = "expertise"
PROBLEM_TYPE_OTHER = "other"

PROBLEM_TYPE_LABELS = {
    PROBLEM_TYPE_MODEL_MISMATCH: "перепутали модель",
    PROBLEM_TYPE_RETURN: "возврат",
    PROBLEM_TYPE_MONEY_REFUND: "деньги",
    PROBLEM_TYPE_DELIVERY: "доставка",
    PROBLEM_TYPE_EXPERTISE: "экспертиза",
    PROBLEM_TYPE_OTHER: "прочее",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
URLISH_KEYS = {
    "url",
    "href",
    "link",
    "cookie",
    "cookies",
    "token",
    "auth",
    "authorization",
    "urldownload",
    "urlpreview",
    "urlshow",
    "downloadurl",
    "previewurl",
    "showurl",
    "download_url",
    "preview_url",
    "show_url",
}
MULTIPART_UPLOAD_THRESHOLD_BYTES = 100 * 1024 * 1024

NUMBER_PATTERNS = (
    re.compile(r"(?i)\b[А-ЯA-Z]{2,6}\d{5,}\b"),
    re.compile(r"(?<!\d)\d{5,8}(?!\d)"),
)
USER_TAG_RE = re.compile(r"\[USER=[^\]]+\](.*?)\[/USER\]", re.IGNORECASE | re.DOTALL)
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
LINE_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class ImportedArchiveMessage:
    source_message_id: str
    source_chat_id: str | None
    message_kind: str
    message_at: datetime | None
    author_id: str | None
    author_name: str | None
    text: str
    file_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportedArchiveFile:
    source_file_id: str | None
    source_message_id: str | None
    name: str
    storage_path: str | None
    content_type: str | None
    extension: str | None
    size: int | None


@dataclass(frozen=True)
class ImportedArchiveCase:
    idempotency_key: str
    source_dialog_id: str
    source_post_message_id: str
    source_comment_chat_id: str | None
    posted_at: datetime | None
    author_id: str | None
    author_name: str | None
    title: str
    summary: str
    problem_type: str
    extracted_numbers: list[str]
    search_text: str
    messages: list[ImportedArchiveMessage]
    files: list[ImportedArchiveFile]

    @property
    def comment_count(self) -> int:
        return len([message for message in self.messages if message.message_kind == "comment"])

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class SiteDefectArchiveBitrixConfig:
    webhook_url: str | None
    entity_type_id: int | None
    archive_category_id: int | None
    archive_stage_id: str | None
    root_folder_id: int | None
    field_map: dict[str, str]

    @classmethod
    def from_settings(cls, settings: Settings) -> SiteDefectArchiveBitrixConfig:
        return cls(
            webhook_url=(
                settings.site_defect_archive_bitrix_webhook_url
                or settings.expertise_bitrix_webhook_url
            ),
            entity_type_id=settings.site_defect_archive_bitrix_entity_type_id,
            archive_category_id=settings.site_defect_archive_bitrix_archive_category_id,
            archive_stage_id=settings.site_defect_archive_bitrix_archive_stage_id,
            root_folder_id=settings.site_defect_archive_bitrix_root_folder_id,
            field_map=dict(settings.site_defect_archive_bitrix_field_map or {}),
        )

    @property
    def can_sync_items(self) -> bool:
        return bool(self.webhook_url and self.entity_type_id)

    @property
    def can_sync_disk(self) -> bool:
        return bool(self.webhook_url and self.root_folder_id)


@dataclass(frozen=True)
class SiteDefectArchiveFilters:
    query: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    author: str | None = None
    problem_type: str | None = None
    number: str | None = None
    has_file: bool | None = None
    has_photo: bool | None = None
    has_video: bool | None = None
    has_linked_expertise: bool | None = None
    limit: int = 50
    offset: int = 0


def parse_archive_export(
    source: str | Path,
    *,
    limit: int | None = None,
    dialog_id: str = DEFAULT_DIALOG_ID,
) -> list[ImportedArchiveCase]:
    source_path = Path(source)
    raw_path = source_path / "comments-store-raw.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"comments-store-raw.json not found in {source_path}")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    threads = _extract_threads(payload)
    file_map = _load_download_log(source_path)
    cases: list[ImportedArchiveCase] = []
    for thread in threads:
        if limit is not None and len(cases) >= limit:
            break
        parsed_case = _parse_thread(
            thread, source_path=source_path, file_map=file_map, dialog_id=dialog_id
        )
        if parsed_case is not None:
            cases.append(parsed_case)
    return cases


def import_archive_export(
    session: Session,
    source: str | Path,
    *,
    dry_run: bool,
    limit: int | None = None,
    apply_bitrix: bool = False,
    settings: Settings | None = None,
    bitrix_client: BitrixRestClient | None = None,
) -> dict[str, Any]:
    source_path = Path(source)
    imported_cases = parse_archive_export(source_path, limit=limit)
    source_posts = _source_posts_count(source_path)
    reported_posts = source_posts or len(imported_cases)
    if limit is not None:
        reported_posts = min(reported_posts, len(imported_cases))
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "apply_bitrix": apply_bitrix,
        "source": str(source_path),
        "posts": reported_posts,
        "source_posts_total": source_posts,
        "importable_posts": len(imported_cases),
        "comment_threads": sum(1 for item in imported_cases if item.source_comment_chat_id),
        "comment_messages": sum(item.comment_count for item in imported_cases),
        "files": sum(item.file_count for item in imported_cases),
        "created": 0,
        "updated": 0,
        "bitrix_synced": 0,
        "disk_synced": 0,
        "bitrix_skipped": 0,
        "disk_skipped": 0,
    }
    if dry_run:
        summary["sample"] = [
            {
                "title": item.title,
                "post_id": item.source_post_message_id,
                "summary": item.summary,
                "numbers": item.extracted_numbers,
                "problem_type": item.problem_type,
                "comments": item.comment_count,
                "files": item.file_count,
            }
            for item in imported_cases[:10]
        ]
        return summary

    config = SiteDefectArchiveBitrixConfig.from_settings(settings or get_settings())
    client = bitrix_client or (
        BitrixRestClient(config.webhook_url) if apply_bitrix and config.webhook_url else None
    )
    for imported in imported_cases:
        if apply_bitrix:
            existing_synced_case = _load_fully_synced_bitrix_case(session, imported, config)
            if existing_synced_case is not None:
                summary["bitrix_skipped"] += 1
                if config.can_sync_disk:
                    summary["disk_skipped"] += 1
                continue
        case_row, created = upsert_archive_case(session, imported)
        if created:
            summary["created"] += 1
        else:
            summary["updated"] += 1
        session.flush()
        if apply_bitrix:
            if client is None or not config.can_sync_items:
                raise RuntimeError(
                    "Bitrix archive sync is requested but Bitrix settings are incomplete"
                )
            sync_result = sync_case_to_bitrix(
                case_row,
                imported,
                source_path=source_path,
                config=config,
                client=client,
            )
            if sync_result.get("item_synced"):
                summary["bitrix_synced"] += 1
            if sync_result.get("disk_synced"):
                summary["disk_synced"] += 1
        session.commit()
    return summary


def _load_fully_synced_bitrix_case(
    session: Session,
    imported: ImportedArchiveCase,
    config: SiteDefectArchiveBitrixConfig,
) -> SiteDefectArchiveCase | None:
    case_row = session.scalar(
        select(SiteDefectArchiveCase)
        .where(SiteDefectArchiveCase.idempotency_key == imported.idempotency_key)
        .options(selectinload(SiteDefectArchiveCase.files))
    )
    if case_row is None or not case_row.bitrix_entity_id:
        return None
    if config.can_sync_disk and not case_row.bitrix_disk_folder_id:
        return None
    expected_files = {
        (file_item.source_file_id or "", file_item.name)
        for file_item in imported.files
        if file_item.storage_path
    }
    if config.can_sync_disk and expected_files:
        uploaded_files = {
            (file_item.source_file_id or "", file_item.name)
            for file_item in case_row.files
            if file_item.bitrix_disk_file_id
        }
        if not expected_files.issubset(uploaded_files):
            return None
    return case_row


def upsert_archive_case(
    session: Session,
    imported: ImportedArchiveCase,
) -> tuple[SiteDefectArchiveCase, bool]:
    existing = session.scalar(
        select(SiteDefectArchiveCase).where(
            SiteDefectArchiveCase.idempotency_key == imported.idempotency_key
        )
    )
    created = existing is None
    case_row = existing or SiteDefectArchiveCase(idempotency_key=imported.idempotency_key)
    case_row.source = SOURCE
    case_row.source_dialog_id = imported.source_dialog_id
    case_row.source_post_message_id = imported.source_post_message_id
    case_row.source_comment_chat_id = imported.source_comment_chat_id
    case_row.posted_at = imported.posted_at
    case_row.author_id = imported.author_id
    case_row.author_name = imported.author_name
    case_row.title = imported.title
    case_row.summary = imported.summary
    case_row.problem_type = imported.problem_type
    case_row.status = ARCHIVE_STATUS
    case_row.search_text = imported.search_text
    case_row.extracted_numbers = list(imported.extracted_numbers)
    case_row.extracted_numbers_text = " ".join(imported.extracted_numbers)[:1000] or None
    case_row.comment_count = imported.comment_count
    case_row.file_count = imported.file_count
    case_row.payload = {
        "source": SOURCE,
        "source_dialog_id": imported.source_dialog_id,
        "source_post_message_id": imported.source_post_message_id,
        "source_comment_chat_id": imported.source_comment_chat_id,
        "history_filename": "history.md",
        "metadata_filename": "metadata.json",
    }
    if created:
        session.add(case_row)
        session.flush()
    else:
        session.flush()
        session.execute(
            delete(SiteDefectArchiveMessage).where(SiteDefectArchiveMessage.case_id == case_row.id)
        )
        session.execute(
            delete(SiteDefectArchiveFile).where(SiteDefectArchiveFile.case_id == case_row.id)
        )
        session.flush()
    case_row.messages = []
    case_row.files = []

    for message in imported.messages:
        case_row.messages.append(
            SiteDefectArchiveMessage(
                source_message_id=message.source_message_id,
                source_chat_id=message.source_chat_id,
                message_kind=message.message_kind,
                message_at=message.message_at,
                author_id=message.author_id,
                author_name=message.author_name,
                text=message.text,
                file_ids=list(message.file_ids) or None,
                payload={
                    "source": SOURCE,
                    "source_message_id": message.source_message_id,
                    "source_chat_id": message.source_chat_id,
                },
            )
        )
    for file_item in imported.files:
        case_row.files.append(
            SiteDefectArchiveFile(
                source_file_id=file_item.source_file_id,
                source_message_id=file_item.source_message_id,
                name=file_item.name,
                storage_path=file_item.storage_path,
                content_type=file_item.content_type,
                extension=file_item.extension,
                size=file_item.size,
                payload={
                    "source": SOURCE,
                    "source_file_id": file_item.source_file_id,
                    "source_message_id": file_item.source_message_id,
                },
            )
        )
    session.flush()
    return case_row, created


def search_archive_cases(
    session: Session,
    filters: SiteDefectArchiveFilters,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(SiteDefectArchiveCase)
    count_stmt = select(func.count(SiteDefectArchiveCase.id))
    conditions = _build_search_conditions(filters)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    stmt = (
        stmt.options(selectinload(SiteDefectArchiveCase.files))
        .order_by(
            SiteDefectArchiveCase.posted_at.desc().nullslast(), SiteDefectArchiveCase.id.desc()
        )
        .limit(max(1, min(filters.limit, 200)))
        .offset(max(0, filters.offset))
    )
    total = int(session.scalar(count_stmt) or 0)
    rows = list(session.scalars(stmt).all())
    return [_case_to_list_item(row, filters.query or filters.number) for row in rows], total


def get_archive_case(session: Session, case_id: int) -> dict[str, Any] | None:
    case_row = session.scalar(
        select(SiteDefectArchiveCase)
        .where(SiteDefectArchiveCase.id == case_id)
        .options(
            selectinload(SiteDefectArchiveCase.messages),
            selectinload(SiteDefectArchiveCase.files),
        )
    )
    if case_row is None:
        return None
    return {
        **_case_to_list_item(case_row, None),
        "search_text": case_row.search_text,
        "messages": [
            {
                "id": message.id,
                "source_message_id": message.source_message_id,
                "message_kind": message.message_kind,
                "message_at": _dt_to_iso(message.message_at),
                "author_name": message.author_name,
                "text": message.text,
                "file_ids": message.file_ids or [],
            }
            for message in case_row.messages
        ],
        "files": [
            {
                "id": file_item.id,
                "source_file_id": file_item.source_file_id,
                "source_message_id": file_item.source_message_id,
                "name": file_item.name,
                "storage_path": file_item.storage_path,
                "content_type": file_item.content_type,
                "extension": file_item.extension,
                "size": file_item.size,
                "bitrix_disk_file_id": file_item.bitrix_disk_file_id,
                "bitrix_disk_url": file_item.bitrix_disk_url,
            }
            for file_item in case_row.files
        ],
    }


def render_history_markdown(imported: ImportedArchiveCase) -> str:
    lines = [
        f"# {imported.title}",
        "",
        f"- Источник: {SOURCE}",
        f"- Диалог: {imported.source_dialog_id}",
        f"- Публикация: {imported.source_post_message_id}",
        f"- Дата: {_dt_to_iso(imported.posted_at) or '-'}",
        f"- Автор: {imported.author_name or '-'}",
        f"- Тип проблемы: {PROBLEM_TYPE_LABELS.get(imported.problem_type, imported.problem_type)}",
        f"- Найденные номера: {', '.join(imported.extracted_numbers) if imported.extracted_numbers else '-'}",
        "",
        "## Публикация",
        "",
    ]
    parent = next(
        (message for message in imported.messages if message.message_kind == "post"),
        None,
    )
    if parent is not None:
        lines.extend(_message_markdown(parent))
    lines.extend(["", "## Комментарии", ""])
    comments = [message for message in imported.messages if message.message_kind == "comment"]
    if comments:
        for message in comments:
            lines.extend(_message_markdown(message))
            lines.append("")
    else:
        lines.append("_Комментариев нет._")
        lines.append("")
    lines.extend(["## Файлы", ""])
    if imported.files:
        for file_item in imported.files:
            marker = (
                f"fileId={file_item.source_file_id}" if file_item.source_file_id else "fileId=-"
            )
            size = f", {file_item.size} bytes" if file_item.size is not None else ""
            lines.append(f"- `{file_item.name}` ({marker}{size})")
    else:
        lines.append("_Файлов нет._")
    return "\n".join(lines).rstrip() + "\n"


def render_metadata(imported: ImportedArchiveCase) -> dict[str, Any]:
    return {
        "idempotency_key": imported.idempotency_key,
        "source": SOURCE,
        "source_dialog_id": imported.source_dialog_id,
        "source_post_message_id": imported.source_post_message_id,
        "source_comment_chat_id": imported.source_comment_chat_id,
        "posted_at": _dt_to_iso(imported.posted_at),
        "author_id": imported.author_id,
        "author_name": imported.author_name,
        "title": imported.title,
        "summary": imported.summary,
        "problem_type": imported.problem_type,
        "extracted_numbers": imported.extracted_numbers,
        "comment_count": imported.comment_count,
        "file_count": imported.file_count,
        "messages": [
            {
                "source_message_id": message.source_message_id,
                "source_chat_id": message.source_chat_id,
                "message_kind": message.message_kind,
                "message_at": _dt_to_iso(message.message_at),
                "author_id": message.author_id,
                "author_name": message.author_name,
                "file_ids": message.file_ids,
            }
            for message in imported.messages
        ],
        "files": [
            {
                "source_file_id": file_item.source_file_id,
                "source_message_id": file_item.source_message_id,
                "name": file_item.name,
                "storage_path": file_item.storage_path,
                "content_type": file_item.content_type,
                "extension": file_item.extension,
                "size": file_item.size,
            }
            for file_item in imported.files
        ],
    }


def sync_case_to_bitrix(
    case_row: SiteDefectArchiveCase,
    imported: ImportedArchiveCase,
    *,
    source_path: Path,
    config: SiteDefectArchiveBitrixConfig,
    client: BitrixRestClient,
) -> dict[str, Any]:
    disk_synced = False
    if config.can_sync_disk:
        folder_id, folder_url, uploaded_files = _sync_disk_folder(
            imported,
            source_path=source_path,
            config=config,
            client=client,
        )
        case_row.bitrix_disk_folder_id = folder_id
        case_row.bitrix_disk_folder_url = folder_url
        for file_row in case_row.files:
            upload = uploaded_files.get(file_row.name)
            if upload:
                file_row.bitrix_disk_file_id = upload.get("id")
                file_row.bitrix_disk_url = upload.get("url")
        disk_synced = True

    item_id, detail_url = _sync_smart_process_item(case_row, imported, config=config, client=client)
    case_row.bitrix_entity_id = item_id
    case_row.bitrix_detail_url = detail_url or _build_bitrix_item_url(config, item_id)
    return {"item_synced": True, "disk_synced": disk_synced}


def _parse_thread(
    thread: dict[str, Any],
    *,
    source_path: Path,
    file_map: dict[str, dict[str, Any]],
    dialog_id: str,
) -> ImportedArchiveCase | None:
    parent = _as_dict(
        thread.get("parent")
        or thread.get("post")
        or thread.get("message")
        or thread.get("parentMessage")
        or {}
    )
    if not parent:
        parent = thread
    post_id = _clean_string(
        _first_present(
            parent,
            "id",
            "ID",
            "messageId",
            "message_id",
            "MESSAGE_ID",
            fallback=_first_present(thread, "parentMessageId", "parent_message_id"),
        )
    )
    if not post_id:
        return None
    comment = _as_dict(thread.get("comment") or thread.get("comments") or {})
    comment_chat_id = (
        _clean_string(
            _first_present(
                comment,
                "chatId",
                "chat_id",
                "CHAT_ID",
                fallback=_first_present(thread, "commentChatId", "comment_chat_id"),
            )
        )
        or None
    )
    parent_text = _clean_text(
        _first_present(parent, "text", "message", "MESSAGE", "html", "body", fallback="")
    )
    parent_message = ImportedArchiveMessage(
        source_message_id=post_id,
        source_chat_id=_clean_string(_first_present(parent, "chatId", "chat_id")) or dialog_id,
        message_kind="post",
        message_at=_parse_datetime(_first_present(parent, "date", "dateCreate", "createdAt")),
        author_id=_clean_string(_first_present(parent, "authorId", "author_id", "AUTHOR_ID"))
        or None,
        author_name=_clean_string(
            _first_present(parent, "authorName", "author_name", "AUTHOR_NAME", "author")
        )
        or None,
        text=parent_text,
        file_ids=_extract_file_ids(parent),
    )
    comments: list[ImportedArchiveMessage] = []
    for raw_message in _as_list(
        thread.get("loadedMessages")
        or thread.get("commentMessages")
        or thread.get("messages")
        or thread.get("items")
    ):
        message = _as_dict(raw_message)
        if not message:
            continue
        message_id = _clean_string(
            _first_present(
                message,
                "id",
                "ID",
                "messageId",
                "message_id",
                "MESSAGE_ID",
            )
        )
        if not message_id:
            continue
        comments.append(
            ImportedArchiveMessage(
                source_message_id=message_id,
                source_chat_id=_clean_string(_first_present(message, "chatId", "chat_id"))
                or comment_chat_id,
                message_kind="comment",
                message_at=_parse_datetime(
                    _first_present(message, "date", "dateCreate", "createdAt")
                ),
                author_id=_clean_string(
                    _first_present(message, "authorId", "author_id", "AUTHOR_ID")
                )
                or None,
                author_name=_clean_string(
                    _first_present(
                        message,
                        "authorName",
                        "author_name",
                        "AUTHOR_NAME",
                        "author",
                    )
                )
                or None,
                text=_clean_text(
                    _first_present(
                        message, "text", "message", "MESSAGE", "html", "body", fallback=""
                    )
                ),
                file_ids=_extract_file_ids(message),
            )
        )

    messages = [parent_message, *comments]
    files = _extract_files(thread, messages=messages, source_path=source_path, file_map=file_map)
    search_text = _build_search_text(parent_message=parent_message, comments=comments, files=files)
    numbers = _extract_numbers(search_text)
    problem_type = _classify_problem_type(search_text)
    title = _build_title(post_id=post_id, posted_at=parent_message.message_at, numbers=numbers)
    summary = _build_summary(
        parent_text=parent_text, comments=comments, numbers=numbers, problem_type=problem_type
    )
    return ImportedArchiveCase(
        idempotency_key=f"old_bitrix:{dialog_id}:post:{post_id}",
        source_dialog_id=dialog_id,
        source_post_message_id=post_id,
        source_comment_chat_id=comment_chat_id,
        posted_at=parent_message.message_at,
        author_id=parent_message.author_id,
        author_name=parent_message.author_name,
        title=title,
        summary=summary,
        problem_type=problem_type,
        extracted_numbers=numbers,
        search_text=search_text,
        messages=messages,
        files=files,
    )


def _extract_threads(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_as_dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("threads", "items", "posts", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [_as_dict(item) for item in value if isinstance(item, dict)]
    return []


def _source_posts_count(source_path: Path) -> int | None:
    raw_path = source_path / "comments-store-raw.json"
    if not raw_path.exists():
        return None
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        value = _int_or_none(payload.get("sourcePosts"))
        if value is not None:
            return value
    return None


def _load_download_log(source_path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in ("comment-files-download-log.csv", "files-download-log.csv"):
        csv_path = source_path / name
        if not csv_path.exists():
            continue
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                file_id = _clean_string(
                    row.get("fileId") or row.get("file_id") or row.get("id") or row.get("ID")
                )
                if not file_id:
                    continue
                saved_as = _clean_string(
                    row.get("savedAs")
                    or row.get("saved_as")
                    or row.get("savedPath")
                    or row.get("path")
                    or row.get("localPath")
                )
                if saved_as and not saved_as.startswith("files/"):
                    saved_as = f"files/{Path(saved_as).name}"
                result[file_id] = {
                    "storage_path": saved_as or None,
                    "size": _int_or_none(
                        row.get("size") or row.get("bytes") or row.get("contentLength")
                    ),
                }
    return result


def _extract_files(
    thread: dict[str, Any],
    *,
    messages: list[ImportedArchiveMessage],
    source_path: Path,
    file_map: dict[str, dict[str, Any]],
) -> list[ImportedArchiveFile]:
    raw_files: list[dict[str, Any]] = []
    containers = [thread, _as_dict(thread.get("parent") or thread.get("post") or {})]
    containers.extend(_as_dict(item) for item in _as_list(thread.get("loadedMessages")))
    for container in containers:
        for key in ("files", "fileList", "attachedFiles", "attachments"):
            raw_files.extend(_as_dict(item) for item in _as_list(container.get(key)))
    message_by_file_id: dict[str, str] = {}
    for message in messages:
        for file_id in message.file_ids:
            message_by_file_id[file_id] = message.source_message_id
    deduped: dict[tuple[str | None, str], ImportedArchiveFile] = {}
    for raw_file in raw_files:
        file_id = (
            _clean_string(_first_present(raw_file, "fileId", "file_id", "id", "ID", "sourceFileId"))
            or None
        )
        name = _clean_string(
            _first_present(raw_file, "name", "fileName", "filename", "NAME", "title")
        )
        if not name and file_id:
            name = f"file-{file_id}"
        if not name:
            continue
        info = file_map.get(file_id or "", {})
        storage_path = _clean_string(info.get("storage_path")) or _find_local_file_path(
            source_path,
            name=name,
            file_id=file_id,
        )
        size = _int_or_none(info.get("size"))
        if size is None and storage_path:
            path = source_path / storage_path
            if path.exists():
                size = path.stat().st_size
        extension = Path(name).suffix.lower() or None
        content_type = (
            _clean_string(
                _first_present(raw_file, "contentType", "content_type", "mimeType", "type")
            )
            or mimetypes.guess_type(name)[0]
        )
        key = (file_id, name)
        deduped[key] = ImportedArchiveFile(
            source_file_id=file_id,
            source_message_id=message_by_file_id.get(file_id or "")
            or _clean_string(_first_present(raw_file, "messageId", "message_id"))
            or None,
            name=name,
            storage_path=storage_path,
            content_type=content_type,
            extension=extension,
            size=size,
        )
    for file_id in message_by_file_id:
        if any(existing.source_file_id == file_id for existing in deduped.values()):
            continue
        info = file_map.get(file_id, {})
        storage_path = _clean_string(info.get("storage_path")) or None
        name = Path(storage_path).name if storage_path else f"file-{file_id}"
        path = source_path / storage_path if storage_path else None
        size = _int_or_none(info.get("size"))
        if size is None and path is not None and path.exists():
            size = path.stat().st_size
        deduped[(file_id, name)] = ImportedArchiveFile(
            source_file_id=file_id,
            source_message_id=message_by_file_id.get(file_id) or None,
            name=name,
            storage_path=storage_path,
            content_type=mimetypes.guess_type(name)[0],
            extension=Path(name).suffix.lower() or None,
            size=size,
        )
    return sorted(
        deduped.values(), key=lambda item: (item.name.casefold(), item.source_file_id or "")
    )


def _find_local_file_path(source_path: Path, *, name: str, file_id: str | None) -> str | None:
    files_dir = source_path / "files"
    if not files_dir.exists():
        return None
    direct = files_dir / name
    if direct.exists():
        return f"files/{name}"
    if file_id:
        matches = sorted(files_dir.glob(f"{file_id}*"))
        if matches:
            return f"files/{matches[0].name}"
    return None


def _extract_file_ids(message: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("fileIds", "file_ids", "filesIds", "FILES", "files"):
        value = message.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    file_id = _clean_string(_first_present(item, "fileId", "file_id", "id", "ID"))
                else:
                    file_id = _clean_string(item)
                if file_id and file_id not in result:
                    result.append(file_id)
        elif isinstance(value, dict):
            for item_key, item_value in value.items():
                file_id = _clean_string(
                    _first_present(_as_dict(item_value), "fileId", "file_id", "id", "ID")
                ) or _clean_string(item_key)
                if file_id and file_id not in result:
                    result.append(file_id)
    return result


def _build_search_text(
    *,
    parent_message: ImportedArchiveMessage,
    comments: list[ImportedArchiveMessage],
    files: list[ImportedArchiveFile],
) -> str:
    chunks: list[str] = [
        parent_message.text,
        parent_message.author_name or "",
    ]
    for message in comments:
        chunks.extend([message.text, message.author_name or ""])
    for file_item in files:
        chunks.extend([file_item.name, file_item.source_file_id or ""])
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _extract_numbers(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for pattern in NUMBER_PATTERNS:
        for match in pattern.findall(text):
            normalized = match.strip().upper()
            if normalized:
                seen[normalized] = None
    return list(seen.keys())[:50]


def extract_site_defect_numbers(text: str) -> list[str]:
    return _extract_numbers(text)


def _classify_problem_type(text: str) -> str:
    normalized = text.casefold()
    if any(marker in normalized for marker in ("деньг", "вернуть деньги", "возврат денег")):
        return PROBLEM_TYPE_MONEY_REFUND
    if any(marker in normalized for marker in ("перепут", "не та модель", "модель не")):
        return PROBLEM_TYPE_MODEL_MISMATCH
    if any(marker in normalized for marker in ("доставк", "сдэк", "курьер", "почта", "самовывоз")):
        return PROBLEM_TYPE_DELIVERY
    if any(marker in normalized for marker in ("возврат", "верните", "вернуть", "обратно")):
        return PROBLEM_TYPE_RETURN
    if "экспертиз" in normalized:
        return PROBLEM_TYPE_EXPERTISE
    return PROBLEM_TYPE_OTHER


def classify_site_defect_problem_type(text: str) -> str:
    return _classify_problem_type(text)


def _build_title(
    *,
    post_id: str,
    posted_at: datetime | None,
    numbers: list[str],
) -> str:
    marker = numbers[0] if numbers else f"post-{post_id}"
    date_part = posted_at.strftime("%Y-%m-%d") if posted_at else "без даты"
    return f"Брак сайта {marker} / {date_part}"[:255]


def _build_summary(
    *,
    parent_text: str,
    comments: list[ImportedArchiveMessage],
    numbers: list[str],
    problem_type: str,
) -> str:
    candidates = [_first_meaningful_line(parent_text)]
    candidates.extend(_first_meaningful_line(message.text) for message in comments[:3])
    first_line = next((item for item in candidates if item), "Старая публикация чата без текста")
    number_tail = f" Номера: {', '.join(numbers[:5])}." if numbers else ""
    label = PROBLEM_TYPE_LABELS.get(problem_type, problem_type)
    return f"{first_line[:700]} Тип: {label}.{number_tail}"[:1000]


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        normalized = line.strip(" -\t")
        if len(normalized) >= 3:
            return normalized
    return ""


def _build_search_conditions(filters: SiteDefectArchiveFilters) -> list[Any]:
    conditions: list[Any] = []
    query = _clean_string(filters.query)
    if query:
        like = f"%{query}%"
        conditions.append(
            or_(
                SiteDefectArchiveCase.title.ilike(like),
                SiteDefectArchiveCase.summary.ilike(like),
                SiteDefectArchiveCase.search_text.ilike(like),
                SiteDefectArchiveCase.extracted_numbers_text.ilike(like),
            )
        )
    number = _clean_string(filters.number)
    if number:
        like = f"%{number}%"
        conditions.append(
            or_(
                SiteDefectArchiveCase.extracted_numbers_text.ilike(like),
                SiteDefectArchiveCase.search_text.ilike(like),
            )
        )
    if filters.date_from:
        conditions.append(
            SiteDefectArchiveCase.posted_at >= datetime.combine(filters.date_from, time.min)
        )
    if filters.date_to:
        conditions.append(
            SiteDefectArchiveCase.posted_at <= datetime.combine(filters.date_to, time.max)
        )
    author = _clean_string(filters.author)
    if author:
        conditions.append(SiteDefectArchiveCase.author_name.ilike(f"%{author}%"))
    problem_type = _clean_string(filters.problem_type)
    if problem_type:
        conditions.append(SiteDefectArchiveCase.problem_type == problem_type)
    if filters.has_file is True:
        conditions.append(SiteDefectArchiveCase.file_count > 0)
    elif filters.has_file is False:
        conditions.append(SiteDefectArchiveCase.file_count == 0)
    if filters.has_linked_expertise is True:
        conditions.append(SiteDefectArchiveCase.linked_expertise_case_id.is_not(None))
    elif filters.has_linked_expertise is False:
        conditions.append(SiteDefectArchiveCase.linked_expertise_case_id.is_(None))
    if filters.has_photo is not None:
        photo_exists = exists().where(
            SiteDefectArchiveFile.case_id == SiteDefectArchiveCase.id,
            SiteDefectArchiveFile.extension.in_(IMAGE_EXTENSIONS),
        )
        conditions.append(photo_exists if filters.has_photo else ~photo_exists)
    if filters.has_video is not None:
        video_exists = exists().where(
            SiteDefectArchiveFile.case_id == SiteDefectArchiveCase.id,
            SiteDefectArchiveFile.extension.in_(VIDEO_EXTENSIONS),
        )
        conditions.append(video_exists if filters.has_video else ~video_exists)
    return conditions


def _case_to_list_item(
    case_row: SiteDefectArchiveCase,
    snippet_query: str | None,
) -> dict[str, Any]:
    return {
        "id": case_row.id,
        "idempotency_key": case_row.idempotency_key,
        "source_dialog_id": case_row.source_dialog_id,
        "source_post_message_id": case_row.source_post_message_id,
        "source_comment_chat_id": case_row.source_comment_chat_id,
        "posted_at": _dt_to_iso(case_row.posted_at),
        "author_name": case_row.author_name,
        "title": case_row.title,
        "summary": case_row.summary,
        "problem_type": case_row.problem_type,
        "problem_type_label": PROBLEM_TYPE_LABELS.get(case_row.problem_type, case_row.problem_type),
        "status": case_row.status,
        "extracted_numbers": case_row.extracted_numbers or [],
        "comment_count": case_row.comment_count,
        "file_count": case_row.file_count,
        "has_photo": any(
            (file_item.extension or "").lower() in IMAGE_EXTENSIONS for file_item in case_row.files
        ),
        "has_video": any(
            (file_item.extension or "").lower() in VIDEO_EXTENSIONS for file_item in case_row.files
        ),
        "bitrix_entity_id": case_row.bitrix_entity_id,
        "bitrix_detail_url": case_row.bitrix_detail_url,
        "bitrix_disk_folder_id": case_row.bitrix_disk_folder_id,
        "bitrix_disk_folder_url": case_row.bitrix_disk_folder_url,
        "linked_expertise_case_id": case_row.linked_expertise_case_id,
        "snippets": _make_snippets(case_row.search_text, snippet_query),
    }


def _make_snippets(text: str, query: str | None, *, limit: int = 3) -> list[str]:
    cleaned = " ".join(_clean_text(text).split())
    if not cleaned:
        return []
    query = _clean_string(query)
    if not query:
        return [cleaned[:240]]
    lowered = cleaned.casefold()
    needle = query.casefold()
    snippets: list[str] = []
    start = 0
    while len(snippets) < limit:
        idx = lowered.find(needle, start)
        if idx < 0:
            break
        left = max(0, idx - 80)
        right = min(len(cleaned), idx + len(query) + 120)
        snippet = cleaned[left:right]
        if left > 0:
            snippet = "..." + snippet
        if right < len(cleaned):
            snippet = snippet + "..."
        snippets.append(snippet)
        start = idx + len(query)
    return snippets or [cleaned[:240]]


def _sync_disk_folder(
    imported: ImportedArchiveCase,
    *,
    source_path: Path,
    config: SiteDefectArchiveBitrixConfig,
    client: BitrixRestClient,
) -> tuple[str, str | None, dict[str, dict[str, str | None]]]:
    if not config.root_folder_id:
        raise RuntimeError("Bitrix root disk folder is not configured")
    current_folder_id = str(config.root_folder_id)
    folder_url: str | None = None
    for part in (
        "Архив",
        "Браки сайт",
        imported.source_dialog_id,
        f"post-{imported.source_post_message_id}",
    ):
        found = _find_child(
            client, parent_folder_id=current_folder_id, name=part, item_type="folder"
        )
        if found is None:
            current_folder_id, folder_url = client.add_subfolder(
                parent_folder_id=int(current_folder_id),
                name=part,
            )
        else:
            current_folder_id, folder_url = found["id"], found.get("url")
    files_folder = _find_child(
        client, parent_folder_id=current_folder_id, name="files", item_type="folder"
    )
    if files_folder is None:
        files_folder_id, _ = client.add_subfolder(
            parent_folder_id=int(current_folder_id), name="files"
        )
    else:
        files_folder_id = files_folder["id"]

    uploaded: dict[str, dict[str, str | None]] = {}
    _upload_text_once(
        client,
        folder_id=current_folder_id,
        filename="history.md",
        content=render_history_markdown(imported),
        uploaded=uploaded,
    )
    _upload_text_once(
        client,
        folder_id=current_folder_id,
        filename="metadata.json",
        content=json.dumps(render_metadata(imported), ensure_ascii=False, indent=2),
        uploaded=uploaded,
    )
    for file_item in imported.files:
        if not file_item.storage_path:
            continue
        path = source_path / file_item.storage_path
        if not path.exists() or not path.is_file():
            continue
        _upload_file_once(
            client,
            folder_id=files_folder_id,
            filename=file_item.name,
            content=path.read_bytes(),
            uploaded=uploaded,
        )
    return current_folder_id, folder_url, uploaded


def _sync_smart_process_item(
    case_row: SiteDefectArchiveCase,
    imported: ImportedArchiveCase,
    *,
    config: SiteDefectArchiveBitrixConfig,
    client: BitrixRestClient,
) -> tuple[str, str | None]:
    if not config.entity_type_id:
        raise RuntimeError("Bitrix smart-process entity type id is not configured")
    fields: dict[str, Any] = {
        "title": case_row.title,
    }
    if config.archive_category_id is not None:
        fields["categoryId"] = config.archive_category_id
    if config.archive_stage_id:
        fields["stageId"] = config.archive_stage_id

    logical_values = {
        "source": SOURCE,
        "old_dialog_id": imported.source_dialog_id,
        "old_post_message_id": imported.source_post_message_id,
        "old_comment_chat_id": imported.source_comment_chat_id,
        "post_date": imported.posted_at,
        "author": imported.author_name,
        "summary": imported.summary,
        "search_text": imported.search_text[:12000],
        "numbers": ", ".join(imported.extracted_numbers),
        "problem_type": PROBLEM_TYPE_LABELS.get(imported.problem_type, imported.problem_type),
        "archive_status": "Архив",
        "folder_url": case_row.bitrix_disk_folder_url,
        "comment_count": imported.comment_count,
        "file_count": imported.file_count,
        "backend_case_id": case_row.id,
        "idempotency_key": imported.idempotency_key,
    }
    for logical_key, field_name in config.field_map.items():
        if logical_key in logical_values and field_name:
            fields[field_name] = logical_values[logical_key]

    existing: list[dict[str, Any]] = []
    if config.field_map.get("idempotency_key"):
        existing = client.list_items_by_ref(
            entity_type_id=config.entity_type_id,
            ref_field=config.field_map["idempotency_key"],
            ref_value=imported.idempotency_key,
        )
    elif case_row.bitrix_entity_id:
        existing = [{"id": case_row.bitrix_entity_id, "detailUrl": case_row.bitrix_detail_url}]

    if existing:
        item_id = str(existing[0].get("id"))
        client.update_smart_process_item(
            entity_type_id=config.entity_type_id,
            item_id=item_id,
            fields=fields,
        )
        return item_id, existing[0].get("detailUrl") or case_row.bitrix_detail_url
    return client.add_smart_process_item(entity_type_id=config.entity_type_id, fields=fields)


def _build_bitrix_item_url(
    config: SiteDefectArchiveBitrixConfig,
    item_id: str,
) -> str | None:
    if not config.webhook_url or not config.entity_type_id or not item_id:
        return None
    parsed = urlparse(config.webhook_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/crm/type/{config.entity_type_id}/details/{item_id}/"


def _find_child(
    client: BitrixRestClient,
    *,
    parent_folder_id: str,
    name: str,
    item_type: str | None = None,
) -> dict[str, str | None] | None:
    response: dict[str, Any] | None = None
    for attempt in range(1, 4):
        try:
            response = client.call(
                "disk.folder.getchildren",
                [("id", str(parent_folder_id)), ("filter[NAME]", name)],
            )
            break
        except RuntimeError:
            if attempt >= 3:
                raise
            time_module.sleep(attempt * 2)
    if response is None:
        return None
    result = response.get("result") or []
    if not isinstance(result, list):
        return None
    for item in result:
        if not isinstance(item, dict):
            continue
        found_type = _clean_string(item.get("TYPE") or item.get("type")).casefold()
        if item_type and found_type != item_type.casefold():
            continue
        item_id = _clean_string(item.get("ID") or item.get("id") or item.get("REAL_OBJECT_ID"))
        if not item_id:
            continue
        return {
            "id": item_id,
            "url": item.get("DETAIL_URL") or item.get("detailUrl") or None,
        }
    return None


def _upload_text_once(
    client: BitrixRestClient,
    *,
    folder_id: str,
    filename: str,
    content: str,
    uploaded: dict[str, dict[str, str | None]],
) -> None:
    _upload_file_once(
        client,
        folder_id=folder_id,
        filename=filename,
        content=content.encode("utf-8"),
        uploaded=uploaded,
    )


def _upload_file_once(
    client: BitrixRestClient,
    *,
    folder_id: str,
    filename: str,
    content: bytes,
    uploaded: dict[str, dict[str, str | None]],
) -> None:
    found = _find_child(client, parent_folder_id=folder_id, name=filename, item_type="file")
    if found is not None:
        uploaded[filename] = found
        return
    response: dict[str, Any] | None = None
    upload_timeout = _upload_timeout_seconds(content)
    for attempt in range(1, 4):
        try:
            if len(content) >= MULTIPART_UPLOAD_THRESHOLD_BYTES:
                response = _upload_file_multipart(
                    client,
                    folder_id=folder_id,
                    filename=filename,
                    content=content,
                    timeout=upload_timeout,
                )
            else:
                response = client.call_json(
                    "disk.folder.uploadfile",
                    {
                        "id": folder_id,
                        "data": {"NAME": filename},
                        "fileContent": [filename, base64.b64encode(content).decode("ascii")],
                        "generateUniqueName": True,
                    },
                    timeout=upload_timeout,
                )
            break
        except RuntimeError:
            if attempt >= 3:
                raise
            time_module.sleep(attempt * 2)
            found_after_error = _find_child(
                client,
                parent_folder_id=folder_id,
                name=filename,
                item_type="file",
            )
            if found_after_error is not None:
                uploaded[filename] = found_after_error
                return
    if response is None:
        return
    result = response.get("result") or {}
    if not isinstance(result, dict):
        return
    uploaded[filename] = {
        "id": _clean_string(result.get("ID") or result.get("id")) or None,
        "url": result.get("DETAIL_URL") or result.get("detailUrl") or None,
    }


def _upload_file_multipart(
    client: BitrixRestClient,
    *,
    folder_id: str,
    filename: str,
    content: bytes,
    timeout: int,
) -> dict[str, Any]:
    upload_info = client.call_json(
        "disk.folder.uploadfile",
        {
            "id": folder_id,
            "data": {"NAME": filename},
            "generateUniqueName": True,
        },
    )
    result = upload_info.get("result") or {}
    upload_url = _clean_string(result.get("uploadUrl") or result.get("UploadUrl"))
    field_name = _clean_string(result.get("field")) or "file"
    if not upload_url:
        raise RuntimeError("Bitrix24 disk.folder.uploadfile returned empty upload URL")
    return _post_multipart_file(
        upload_url=upload_url,
        field_name=field_name,
        filename=filename,
        content=content,
        timeout=timeout,
    )


def _post_multipart_file(
    *,
    upload_url: str,
    field_name: str,
    filename: str,
    content: bytes,
    timeout: int,
) -> dict[str, Any]:
    boundary = f"----mm-site-defect-{time_module.time_ns()}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe_filename = filename.replace("\\", "_").replace('"', '\\"')
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{safe_filename}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        upload_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bitrix24 disk upload: HTTP {error.code} {body_text[:500]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Bitrix24 disk upload: network error {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError("Bitrix24 disk upload: network timeout") from error
    if payload.get("error"):
        raise RuntimeError(
            f"Bitrix24 disk upload: {payload['error']} {payload.get('error_description', '')}"
        )
    return payload


def _upload_timeout_seconds(content: bytes) -> int:
    size_mb = max(1, len(content) // (1024 * 1024))
    return max(120, min(900, 60 + size_mb * 20))


def _message_markdown(message: ImportedArchiveMessage) -> list[str]:
    head = f"### {_dt_to_iso(message.message_at) or '-'} / {message.author_name or '-'} / {message.source_message_id}"
    lines = [head, ""]
    lines.append(message.text or "_Без текста._")
    if message.file_ids:
        lines.append("")
        lines.append("Файлы: " + ", ".join(message.file_ids))
    return lines


def _clean_text(value: Any) -> str:
    text = _clean_string(value)
    if not text:
        return ""
    text = USER_TAG_RE.sub(r"\1", text)
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = SPACE_RE.sub(" ", text)
    text = LINE_RE.sub("\n\n", text)
    return text.strip()


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.isdigit():
            parsed = datetime.fromtimestamp(int(raw), tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _dt_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(sep="T", timespec="seconds")


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _first_present(mapping: dict[str, Any], *keys: str, fallback: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return fallback


def sanitize_export_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).replace("-", "_").casefold()
            if normalized_key in URLISH_KEYS or any(
                marker in normalized_key for marker in URLISH_KEYS
            ):
                continue
            result[str(key)] = sanitize_export_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_export_payload(item) for item in value]
    return value


__all__ = [
    "ARCHIVE_STATUS",
    "DEFAULT_DIALOG_ID",
    "PROBLEM_TYPE_LABELS",
    "SiteDefectArchiveBitrixConfig",
    "SiteDefectArchiveFilters",
    "get_archive_case",
    "import_archive_export",
    "parse_archive_export",
    "render_history_markdown",
    "render_metadata",
    "sanitize_export_payload",
    "search_archive_cases",
    "sync_case_to_bitrix",
    "upsert_archive_case",
]
