from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from app.api.dependencies import get_db, require_site_defect_archive_internal_token
from app.main import app
from app.models import SiteDefectArchiveCase, SiteDefectArchiveFile, SiteDefectArchiveMessage
from app.services.site_defect_archive import (
    SiteDefectArchiveFilters,
    import_archive_export,
    parse_archive_export,
    render_metadata,
    search_archive_cases,
)


def _write_sample_export(path: Path) -> Path:
    files_dir = path / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "photo.png").write_bytes(b"img")
    (files_dir / "MOV_20260521_174251_944.mp4").write_bytes(b"video")
    payload = {
        "threads": [
            {
                "parent": {
                    "id": "1001",
                    "chatId": "chat69465",
                    "date": "2026-05-21T14:00:00+03:00",
                    "authorId": "11",
                    "authorName": "Иван",
                    "text": "Заказ 062493, клиент пишет: верните новым качеством",
                    "fileIds": ["f1"],
                    "files": [
                        {
                            "fileId": "f1",
                            "name": "photo.png",
                            "urlDownload": "https://old-cloud.example/download/f1",
                        }
                    ],
                },
                "comment": {"chatId": "chat70001"},
                "loadedMessages": [
                    {
                        "id": "2001",
                        "chatId": "chat70001",
                        "date": "2026-05-21T14:10:00+03:00",
                        "authorId": "12",
                        "authorName": "Мария",
                        "text": "РБГУ0097541, возврат в пути",
                        "fileIds": ["f2"],
                        "files": [
                            {
                                "fileId": "f2",
                                "name": "MOV_20260521_174251_944.mp4",
                                "urlPreview": "https://old-cloud.example/preview/f2",
                            }
                        ],
                    },
                    {
                        "id": "2002",
                        "chatId": "chat70001",
                        "date": "2026-05-21T14:20:00+03:00",
                        "authorName": "ОКК",
                        "text": "Надо просто разобраться, экспертиза пока не нужна",
                    },
                ],
            }
        ]
    }
    (path / "comments-store-raw.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (path / "comment-files-download-log.csv").write_text(
        "fileId,savedAs,size\n" "f1,photo.png,3\n" "f2,MOV_20260521_174251_944.mp4,5\n",
        encoding="utf-8",
    )
    return path


def test_parse_archive_export_builds_case_without_cloud_urls(tmp_path) -> None:
    source = _write_sample_export(tmp_path / "export")

    cases = parse_archive_export(source)

    assert len(cases) == 1
    case = cases[0]
    assert case.idempotency_key == "old_bitrix:chat69465:post:1001"
    assert case.comment_count == 2
    assert case.file_count == 2
    assert "062493" in case.extracted_numbers
    assert "РБГУ0097541" in case.extracted_numbers
    assert case.problem_type == "return"
    metadata = json.dumps(render_metadata(case), ensure_ascii=False)
    assert "old-cloud.example" not in metadata
    assert "urlDownload" not in metadata
    assert "MOV_20260521_174251_944.mp4" in metadata


def test_import_archive_export_dry_run_and_idempotent_apply(db_session, tmp_path) -> None:
    source = _write_sample_export(tmp_path / "export")

    dry_run = import_archive_export(db_session, source, dry_run=True)
    assert dry_run["posts"] == 1
    assert dry_run["comment_threads"] == 1
    assert dry_run["comment_messages"] == 2
    assert dry_run["files"] == 2

    first = import_archive_export(db_session, source, dry_run=False)
    second = import_archive_export(db_session, source, dry_run=False)

    assert first["created"] == 1
    assert second["updated"] == 1
    assert db_session.scalar(select(func.count(SiteDefectArchiveCase.id))) == 1
    assert db_session.scalar(select(func.count(SiteDefectArchiveMessage.id))) == 3
    assert db_session.scalar(select(func.count(SiteDefectArchiveFile.id))) == 2


def test_archive_search_finds_numbers_phrase_and_file_name(db_session, tmp_path) -> None:
    source = _write_sample_export(tmp_path / "export")
    import_archive_export(db_session, source, dry_run=False)

    for query in (
        "062493",
        "РБГУ0097541",
        "MOV_20260521_174251_944.mp4",
        "верните новым качеством",
    ):
        items, total = search_archive_cases(db_session, SiteDefectArchiveFilters(query=query))
        assert total == 1
        assert items[0]["source_post_message_id"] == "1001"
        assert items[0]["snippets"]

    video_items, video_total = search_archive_cases(
        db_session,
        SiteDefectArchiveFilters(has_video=True),
    )
    assert video_total == 1
    assert video_items[0]["has_video"] is True


def test_archive_api_search(client, db_session, tmp_path) -> None:
    source = _write_sample_export(tmp_path / "export")
    import_archive_export(db_session, source, dry_run=False)

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_site_defect_archive_internal_token] = lambda: "ok"
    try:
        response = client.get("/api/site-defects/archive?q=062493")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source_post_message_id"] == "1001"
