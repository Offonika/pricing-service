from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from app.core.config import get_settings
from app.services.bank_payments_bitrix import BitrixDiskClient, BitrixDiskError, BitrixDiskFile
from app.workers.bank_payments import run_bank_payments_bitrix_disk_sync

OWN_ACCOUNT = "40702810000000000001"
OWN_INN = "7723456789"


class FakeHTTPResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        if content is None:
            content = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        self._content = content
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._content
        return self._content[:size]


def test_bitrix_disk_client_list_download_upload_happy_path() -> None:
    requests: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout: int):
        payload = json.loads((request.data or b"{}").decode("utf-8"))
        requests.append({"url": request.full_url, "payload": payload, "timeout": timeout})
        if request.full_url.endswith("/disk.folder.getchildren.json"):
            return FakeHTTPResponse(
                {
                    "result": [
                        {
                            "ID": "10",
                            "NAME": "statement.csv",
                            "TYPE": "file",
                            "GLOBAL_CONTENT_VERSION": "2",
                            "SIZE": "4",
                            "UPDATE_TIME": "2026-05-11T10:00:00+03:00",
                        },
                        {"ID": "11", "NAME": "nested", "TYPE": "folder"},
                    ]
                }
            )
        if request.full_url.endswith("/disk.file.get.json"):
            return FakeHTTPResponse({"result": {"DOWNLOAD_URL": "https://download.invalid/file"}})
        if request.full_url == "https://download.invalid/file":
            return FakeHTTPResponse(content=b"test", headers={"Content-Length": "4"})
        if request.full_url.endswith("/disk.folder.uploadfile.json"):
            file_name, encoded = payload["fileContent"]
            assert file_name == "out.txt"
            assert base64.b64decode(encoded.encode("ascii")) == b"result"
            return FakeHTTPResponse({"result": {"ID": "99", "NAME": "out.txt"}})
        raise AssertionError(request.full_url)

    client = BitrixDiskClient("https://bitrix.example/rest/1/token", urlopen=fake_urlopen)

    files = client.list_files(100, limit=10)
    content = client.download_file(files[0].file_id, max_bytes=10)
    uploaded = client.upload_file(200, filename="out.txt", content=b"result")

    assert files == [
        BitrixDiskFile(
            file_id="10",
            name="statement.csv",
            version="2",
            updated_at="2026-05-11T10:00:00+03:00",
            size=4,
        )
    ]
    assert content == b"test"
    assert uploaded["ID"] == "99"
    assert requests[0]["payload"]["order"] == {"UPDATE_TIME": "ASC"}


def test_bitrix_disk_client_rejects_too_large_file() -> None:
    def fake_urlopen(request, timeout: int):
        if request.full_url.endswith("/disk.file.get.json"):
            return FakeHTTPResponse({"result": {"DOWNLOAD_URL": "https://download.invalid/file"}})
        if request.full_url == "https://download.invalid/file":
            return FakeHTTPResponse(content=b"too-large", headers={"Content-Length": "9"})
        raise AssertionError(request.full_url)

    client = BitrixDiskClient("https://bitrix.example/rest/1/token", urlopen=fake_urlopen)

    with pytest.raises(BitrixDiskError, match="exceeds max size"):
        client.download_file("10", max_bytes=5)


def test_bitrix_disk_client_rest_error_is_controlled() -> None:
    def fake_urlopen(request, timeout: int):
        return FakeHTTPResponse(
            {"error": "ERROR_ARGUMENT", "error_description": "Invalid folder id"}
        )

    client = BitrixDiskClient("https://bitrix.example/rest/1/token", urlopen=fake_urlopen)

    with pytest.raises(BitrixDiskError, match="ERROR_ARGUMENT"):
        client.list_files(100)


class FakeDiskClient:
    def __init__(self, files: list[BitrixDiskFile], downloads: dict[str, bytes]):
        self.files = files
        self.downloads = downloads
        self.uploads: list[dict[str, Any]] = []

    def list_files(self, folder_id: int, *, limit: int = 50) -> list[BitrixDiskFile]:
        self.list_folder_id = folder_id
        self.list_limit = limit
        return self.files[:limit]

    def download_file(
        self,
        file_id: str,
        *,
        max_bytes: int,
        download_url: str | None = None,
    ) -> bytes:
        content = self.downloads[file_id]
        if len(content) > max_bytes:
            raise BitrixDiskError("too large")
        return content

    def upload_file(self, folder_id: int, *, filename: str, content: bytes) -> dict[str, Any]:
        payload = {
            "folder_id": folder_id,
            "filename": filename,
            "content": content,
            "ID": f"upload-{len(self.uploads) + 1}",
        }
        self.uploads.append(payload)
        return payload


def _configure_b24(monkeypatch, tmp_path):
    monkeypatch.setenv("BANK_PAYMENTS_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BANK_PAYMENTS_OWN_ACCOUNTS", OWN_ACCOUNT)
    monkeypatch.setenv("BANK_PAYMENTS_B24_WEBHOOK_URL", "https://bitrix.example/rest/1/token")
    monkeypatch.setenv("BANK_PAYMENTS_B24_INPUT_FOLDER_ID", "111")
    monkeypatch.setenv("BANK_PAYMENTS_B24_READY_FOLDER_ID", "222")
    monkeypatch.setenv("BANK_PAYMENTS_B24_ERROR_FOLDER_ID", "333")
    monkeypatch.setenv("BANK_PAYMENTS_B24_STATE_FILE", str(tmp_path / "state.json"))
    get_settings.cache_clear()
    return get_settings()


def _csv_bytes() -> bytes:
    source = (
        "Дата;Номер;Сумма;Направление;Плательщик;ПлательщикИНН;ПлательщикСчет;"
        "ПлательщикБанк;Получатель;ПолучательИНН;ПолучательСчет;ПолучательБанк;"
        "НазначениеПлатежа\n"
        "11.05.2026;7;2500,50;Поступление;Т-Банк;7712345678;40802810000000000003;"
        f"Т-Банк;ООО Мастер Мобайл;{OWN_INN};{OWN_ACCOUNT};Наш банк;СБП оплата\n"
    )
    return source.encode("utf-8")


def test_bitrix_disk_worker_uploads_ready_file_and_skips_processed(monkeypatch, tmp_path) -> None:
    settings = _configure_b24(monkeypatch, tmp_path)
    file_item = BitrixDiskFile(
        file_id="10",
        name="statement.csv",
        version="1",
        updated_at="2026-05-11T10:00:00+03:00",
    )
    client = FakeDiskClient([file_item], {"10": _csv_bytes()})

    result = run_bank_payments_bitrix_disk_sync(settings=settings, client=client)
    second_result = run_bank_payments_bitrix_disk_sync(settings=settings, client=client)

    assert result == {"processed": 1, "ready": 1, "errors": 0, "skipped": 0, "last_error": None}
    assert second_result == {
        "processed": 0,
        "ready": 0,
        "errors": 0,
        "skipped": 1,
        "last_error": None,
    }
    assert client.list_folder_id == 111
    ready_uploads = [item for item in client.uploads if item["folder_id"] == 222]
    assert len(ready_uploads) == 2
    normalized_upload = next(
        item for item in ready_uploads if item["filename"].endswith("-1c-client-bank.txt")
    )
    assert normalized_upload["content"].decode("cp1251").startswith("1CClientBankExchange")


def test_bitrix_disk_worker_routes_unknown_format_to_error_folder(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _configure_b24(monkeypatch, tmp_path)
    file_item = BitrixDiskFile(
        file_id="10",
        name=f"statement_{OWN_ACCOUNT}_{OWN_INN}.txt",
        version="1",
    )
    client = FakeDiskClient([file_item], {"10": b"not a statement"})

    result = run_bank_payments_bitrix_disk_sync(settings=settings, client=client)

    assert result["processed"] == 1
    assert result["ready"] == 0
    assert result["errors"] == 1
    error_uploads = [item for item in client.uploads if item["folder_id"] == 333]
    assert len(error_uploads) == 1
    report = error_uploads[0]["content"].decode("utf-8")
    assert "Статус: manual_review" in report
    assert OWN_ACCOUNT not in report
    assert OWN_INN not in report
