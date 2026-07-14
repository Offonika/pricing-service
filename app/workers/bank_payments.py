from __future__ import annotations

import fcntl
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.services import bank_payments
from app.services.bank_payments_bitrix import BitrixDiskClient, BitrixDiskFile


class BankPaymentsBitrixConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BankPaymentsBitrixDiskConfig:
    webhook_url: str
    input_folder_id: int
    ready_folder_id: int
    error_folder_id: int
    poll_limit: int
    max_file_bytes: int
    state_file: Path


def run_bank_payments_bitrix_disk_sync(
    *,
    settings: Settings | None = None,
    client: BitrixDiskClient | None = None,
    limit: int | None = None,
) -> dict[str, int | str | None]:
    settings = settings or get_settings()
    config = _build_config(settings, limit=limit)
    client = client or BitrixDiskClient(config.webhook_url)
    result: dict[str, int | str | None] = {
        "processed": 0,
        "ready": 0,
        "errors": 0,
        "skipped": 0,
        "last_error": None,
    }

    with _state_lock(config.state_file):
        state = _load_state(config.state_file)
        processed_state = state.setdefault("processed", {})
        files = client.list_files(config.input_folder_id, limit=config.poll_limit)
        for file_item in files:
            state_key = file_item.state_key
            if state_key in processed_state:
                result["skipped"] = int(result["skipped"] or 0) + 1
                continue
            try:
                record = _process_file(
                    client=client,
                    config=config,
                    settings=settings,
                    file_item=file_item,
                )
                processed_state[state_key] = record
                _save_state(config.state_file, state)
                result["processed"] = int(result["processed"] or 0) + 1
                if record["status"] == "ready":
                    result["ready"] = int(result["ready"] or 0) + 1
                else:
                    result["errors"] = int(result["errors"] or 0) + 1
                    result["last_error"] = str(record.get("last_error") or "") or None
            except Exception as exc:
                masked_error = bank_payments.mask_financial_text(str(exc))[:1000]
                result["errors"] = int(result["errors"] or 0) + 1
                result["last_error"] = masked_error
                try:
                    report_upload = _upload_error_report(
                        client=client,
                        config=config,
                        file_item=file_item,
                        error=masked_error,
                    )
                except Exception as report_exc:
                    result["last_error"] = bank_payments.mask_financial_text(
                        f"{masked_error}; failed to upload error report: {report_exc}"
                    )[:1000]
                    continue
                processed_state[state_key] = _state_record(
                    file_item=file_item,
                    status="error",
                    upload_id=None,
                    report_upload=report_upload,
                    last_error=masked_error,
                )
                _save_state(config.state_file, state)
                result["processed"] = int(result["processed"] or 0) + 1
    return result


def _build_config(settings: Settings, *, limit: int | None) -> BankPaymentsBitrixDiskConfig:
    missing: list[str] = []
    if not settings.bank_payments_b24_webhook_url:
        missing.append("BANK_PAYMENTS_B24_WEBHOOK_URL")
    if settings.bank_payments_b24_input_folder_id is None:
        missing.append("BANK_PAYMENTS_B24_INPUT_FOLDER_ID")
    if settings.bank_payments_b24_ready_folder_id is None:
        missing.append("BANK_PAYMENTS_B24_READY_FOLDER_ID")
    if settings.bank_payments_b24_error_folder_id is None:
        missing.append("BANK_PAYMENTS_B24_ERROR_FOLDER_ID")
    if missing:
        raise BankPaymentsBitrixConfigurationError(
            "Bitrix Disk settings are not configured: " + ", ".join(missing)
        )

    poll_limit = limit if limit is not None else settings.bank_payments_b24_poll_limit
    if poll_limit <= 0:
        raise BankPaymentsBitrixConfigurationError("BANK_PAYMENTS_B24_POLL_LIMIT must be positive")
    if settings.bank_payments_b24_max_file_bytes <= 0:
        raise BankPaymentsBitrixConfigurationError(
            "BANK_PAYMENTS_B24_MAX_FILE_BYTES must be positive"
        )

    return BankPaymentsBitrixDiskConfig(
        webhook_url=settings.bank_payments_b24_webhook_url or "",
        input_folder_id=int(settings.bank_payments_b24_input_folder_id or 0),
        ready_folder_id=int(settings.bank_payments_b24_ready_folder_id or 0),
        error_folder_id=int(settings.bank_payments_b24_error_folder_id or 0),
        poll_limit=poll_limit,
        max_file_bytes=settings.bank_payments_b24_max_file_bytes,
        state_file=Path(settings.bank_payments_b24_state_file),
    )


def _process_file(
    *,
    client: BitrixDiskClient,
    config: BankPaymentsBitrixDiskConfig,
    settings: Settings,
    file_item: BitrixDiskFile,
) -> dict[str, Any]:
    content = client.download_file(file_item.file_id, max_bytes=config.max_file_bytes)
    normalized = bank_payments.normalize_upload(
        content,
        filename=file_item.name,
        source_bank="bitrix_disk",
        settings=settings,
    )
    report = _render_normalize_report(file_item, normalized)
    report_upload: dict[str, Any]
    ready_upload: dict[str, Any] | None = None
    if normalized.status == "ready":
        normalized_path = bank_payments.get_normalized_file_path(normalized.upload_id, settings)
        ready_upload = client.upload_file(
            config.ready_folder_id,
            filename=_result_filename(file_item.name, normalized.upload_id),
            content=normalized_path.read_bytes(),
        )
        report_upload = client.upload_file(
            config.ready_folder_id,
            filename=_report_filename(file_item.name, normalized.upload_id),
            content=report.encode("utf-8"),
        )
    else:
        report_upload = client.upload_file(
            config.error_folder_id,
            filename=_report_filename(file_item.name, normalized.upload_id),
            content=report.encode("utf-8"),
        )
    return _state_record(
        file_item=file_item,
        status=normalized.status,
        upload_id=normalized.upload_id,
        ready_upload=ready_upload,
        report_upload=report_upload,
        last_error=None if normalized.status == "ready" else "; ".join(normalized.issues),
    )


def _upload_error_report(
    *,
    client: BitrixDiskClient,
    config: BankPaymentsBitrixDiskConfig,
    file_item: BitrixDiskFile,
    error: str,
) -> dict[str, Any]:
    report = _render_error_report(file_item, error)
    return client.upload_file(
        config.error_folder_id,
        filename=_error_report_filename(file_item.name, file_item.file_id),
        content=report.encode("utf-8"),
    )


def _render_normalize_report(file_item: BitrixDiskFile, response: Any) -> str:
    lines = [
        "Банковская выписка",
        f"Файл Bitrix Disk: {file_item.name}",
        f"Bitrix file id: {file_item.file_id}",
        f"Upload ID: {response.upload_id}",
        f"Статус: {response.status}",
        f"Формат: {response.detected_format}",
        f"Строк исходника: {response.counts.source_lines}",
        f"Платежей: {response.counts.payments}",
        f"Классифицировано: {response.counts.classified}",
        f"Ручная проверка: {response.counts.manual_review}",
        f"Экспортировано: {response.counts.exported}",
    ]
    if response.download_url:
        lines.append(f"Локальный download URL: {response.download_url}")
    if response.issues:
        lines.append("Проблемы:")
        lines.extend(f"- {issue}" for issue in response.issues)
    return bank_payments.mask_financial_text("\n".join(lines) + "\n")


def _render_error_report(file_item: BitrixDiskFile, error: str) -> str:
    lines = [
        "Банковская выписка",
        f"Файл Bitrix Disk: {file_item.name}",
        f"Bitrix file id: {file_item.file_id}",
        "Статус: error",
        f"Ошибка: {error}",
    ]
    return bank_payments.mask_financial_text("\n".join(lines) + "\n")


def _state_record(
    *,
    file_item: BitrixDiskFile,
    status: str,
    upload_id: str | None,
    report_upload: dict[str, Any],
    ready_upload: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    return {
        "file_id": file_item.file_id,
        "name": file_item.name,
        "version": file_item.version,
        "updated_at": file_item.updated_at,
        "status": status,
        "upload_id": upload_id,
        "ready_file_id": _uploaded_file_id(ready_upload),
        "report_file_id": _uploaded_file_id(report_upload),
        "last_error": bank_payments.mask_financial_text(last_error or "")[:1000] or None,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _uploaded_file_id(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    value = (
        payload.get("ID")
        or payload.get("id")
        or payload.get("REAL_OBJECT_ID")
        or payload.get("object", {}).get("ID")
    )
    if value in (None, ""):
        return None
    return str(value)


@contextmanager
def _state_lock(path: Path):
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": {}}
    if not isinstance(payload, dict):
        return {"processed": {}}
    processed = payload.get("processed")
    if not isinstance(processed, dict):
        payload["processed"] = {}
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _safe_filename(value: str, *, fallback: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    name = re.sub(r'[\r\n/:*?"<>|]+', "_", name)
    name = name.strip(" .")
    return name or fallback


def _result_filename(source_name: str, upload_id: str) -> str:
    source = _safe_filename(source_name, fallback="bank-statement")
    stem = Path(source).stem or "bank-statement"
    return f"{stem}-{upload_id}-1c-client-bank.txt"


def _report_filename(source_name: str, upload_id: str) -> str:
    source = _safe_filename(source_name, fallback="bank-statement")
    stem = Path(source).stem or "bank-statement"
    return f"{stem}-{upload_id}-report.txt"


def _error_report_filename(source_name: str, file_id: str) -> str:
    source = _safe_filename(source_name, fallback="bank-statement")
    stem = Path(source).stem or "bank-statement"
    return f"{stem}-{file_id}-error.txt"


__all__ = [
    "BankPaymentsBitrixConfigurationError",
    "run_bank_payments_bitrix_disk_sync",
]
