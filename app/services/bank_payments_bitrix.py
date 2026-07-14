from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class BitrixDiskError(RuntimeError):
    pass


@dataclass(frozen=True)
class BitrixDiskFile:
    file_id: str
    name: str
    version: str = ""
    updated_at: str = ""
    size: int | None = None
    download_url: str | None = None
    detail_url: str | None = None

    @property
    def state_key(self) -> str:
        marker = self.version or self.updated_at or "current"
        return f"{self.file_id}:{marker}"


class BitrixDiskClient:
    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: int = 60,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ):
        self.webhook_url = webhook_url.rstrip("/")
        self.timeout = timeout
        self._urlopen = urlopen

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.webhook_url}/{method}.json",
            data=json.dumps(params or {}, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with self._urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("error"):
            message = (
                f"Bitrix24 {method}: {payload['error']} " f"{payload.get('error_description', '')}"
            ).strip()
            raise BitrixDiskError(message)
        return payload

    def list_files(self, folder_id: int, *, limit: int = 50) -> list[BitrixDiskFile]:
        files: list[BitrixDiskFile] = []
        start = 0
        target = max(limit, 0)
        while len(files) < target:
            response = self.call(
                "disk.folder.getchildren",
                {
                    "id": folder_id,
                    "order": {"UPDATE_TIME": "ASC"},
                    "start": start,
                },
            )
            result = response.get("result") or []
            if not isinstance(result, list):
                raise BitrixDiskError("Bitrix24 disk.folder.getchildren returned invalid payload")
            for item in result:
                if len(files) >= target:
                    break
                file_item = _disk_file_from_item(item)
                if file_item is not None:
                    files.append(file_item)
            next_start = response.get("next")
            if next_start is None or not result:
                break
            try:
                start = int(next_start)
            except (TypeError, ValueError):
                break
        return files

    def get_download_url(self, file_id: str) -> str:
        response = self.call("disk.file.get", {"id": file_id})
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise BitrixDiskError("Bitrix24 disk.file.get returned invalid payload")
        url = _clean_string(
            result.get("DOWNLOAD_URL")
            or result.get("downloadUrl")
            or result.get("urlMachine")
            or result.get("url")
        )
        if not url:
            raise BitrixDiskError(
                f"Bitrix24 disk.file.get returned empty download URL for {file_id}"
            )
        return url

    def download_file(
        self,
        file_id: str,
        *,
        max_bytes: int,
        download_url: str | None = None,
    ) -> bytes:
        url = download_url or self.get_download_url(file_id)
        request = urllib.request.Request(url, method="GET")
        with self._urlopen(request, timeout=self.timeout) as response:
            content_length = _int_or_none(response.headers.get("Content-Length"))
            if content_length is not None and content_length > max_bytes:
                raise BitrixDiskError(f"Bitrix file {file_id} exceeds max size {max_bytes} bytes")
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise BitrixDiskError(f"Bitrix file {file_id} exceeds max size {max_bytes} bytes")
        return content

    def upload_file(
        self,
        folder_id: int,
        *,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        response = self.call(
            "disk.folder.uploadfile",
            {
                "id": folder_id,
                "data": {"NAME": filename},
                "fileContent": [filename, base64.b64encode(content).decode("ascii")],
                "generateUniqueName": True,
            },
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise BitrixDiskError("Bitrix24 disk.folder.uploadfile returned invalid payload")
        return result


def _disk_file_from_item(item: Any) -> BitrixDiskFile | None:
    if not isinstance(item, dict):
        return None
    if _clean_string(item.get("TYPE")).casefold() != "file":
        return None
    if _clean_string(item.get("DELETED_TYPE")) not in {"", "0"}:
        return None
    file_id = _clean_string(item.get("ID") or item.get("id") or item.get("REAL_OBJECT_ID"))
    name = _clean_string(item.get("NAME") or item.get("name"))
    if not file_id or not name:
        return None
    return BitrixDiskFile(
        file_id=file_id,
        name=name,
        version=_clean_string(item.get("GLOBAL_CONTENT_VERSION")),
        updated_at=_clean_string(item.get("UPDATE_TIME")),
        size=_int_or_none(item.get("SIZE")),
        download_url=_clean_string(item.get("DOWNLOAD_URL") or item.get("downloadUrl")) or None,
        detail_url=_clean_string(item.get("DETAIL_URL") or item.get("detailUrl")) or None,
    )


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BitrixDiskClient",
    "BitrixDiskError",
    "BitrixDiskFile",
]
