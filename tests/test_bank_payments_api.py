from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.schemas.bank_payments import BankPaymentNormalizeCounts, BankPaymentNormalizeResponse

OWN_ACCOUNT = "40702810000000000001"


def _headers(token: str = "bank-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure(monkeypatch, tmp_path, token: str = "bank-token") -> dict[str, str]:
    monkeypatch.setenv("BANK_PAYMENTS_INTERNAL_API_TOKEN", token)
    monkeypatch.setenv("BANK_PAYMENTS_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("BANK_PAYMENTS_OWN_ACCOUNTS", OWN_ACCOUNT)
    monkeypatch.delenv("MANAGEMENT_INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()
    return _headers(token)


def _csv_bytes() -> bytes:
    source = (
        "Дата;Номер;Сумма;Направление;Плательщик;ПлательщикИНН;ПлательщикСчет;"
        "ПлательщикБанк;Получатель;ПолучательИНН;ПолучательСчет;ПолучательБанк;"
        "НазначениеПлатежа\n"
        "11.05.2026;7;2500,50;Поступление;Т-Банк;7712345678;40802810000000000003;"
        f"Т-Банк;ООО Мастер Мобайл;7723456789;{OWN_ACCOUNT};Наш банк;СБП оплата\n"
    )
    return source.encode("utf-8")


def test_bank_payments_upload_requires_bearer_token(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/normalize",
        files={"file": ("statement.csv", _csv_bytes(), "text/csv")},
    )

    assert response.status_code == 401


def test_bank_payments_normalize_upload_and_download(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/normalize",
        files={"file": ("statement.csv", _csv_bytes(), "text/csv")},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["detected_format"] == "generic_bank_csv"
    assert payload["counts"]["payments"] == 1
    assert payload["download_url"]
    assert "40702810000000000001" not in response.text
    assert "7723456789" not in response.text

    download = client.get(payload["download_url"], headers=headers)

    assert download.status_code == 200
    assert "bank-payments-" in download.headers["content-disposition"]
    assert download.content.decode("cp1251").startswith("1CClientBankExchange")
    assert f"ПолучательСчет={OWN_ACCOUNT}" in download.content.decode("cp1251")


def test_bank_payments_unknown_format_has_no_download(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/normalize",
        files={"file": ("unknown.txt", b"not a statement", "text/plain")},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "manual_review"
    assert payload["detected_format"] == "unknown"
    assert payload["download_url"] is None


def test_bank_payments_classify_endpoint(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/classify",
        json={
            "direction": "incoming",
            "amount": 1000,
            "payer_name": "Т-Банк",
            "payer_bank": "Т-Банк",
            "recipient_account": OWN_ACCOUNT,
            "purpose": "СБП оплата",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["scenario"] == "card_payment"


def test_bank_payments_sber_export_requires_bearer_token(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/sber/export",
        json={"date_from": "2026-05-08", "date_to": "2026-05-08"},
    )

    assert response.status_code == 401


def test_bank_payments_sber_export_reports_missing_config(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/sber/export",
        json={"date_from": "2026-05-08", "date_to": "2026-05-08"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "BANK_PAYMENTS_SOURCE_DATABASE_URL" in response.json()["detail"]


def test_bank_payments_sber_export_endpoint_returns_counts(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BANK_PAYMENTS_SOURCE_DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()

    def fake_export(**_kwargs):
        return BankPaymentNormalizeResponse(
            upload_id="a" * 32,
            status="ready",
            detected_format="sber_api_raw",
            counts=BankPaymentNormalizeCounts(
                source_lines=2,
                payments=2,
                classified=2,
                manual_review=0,
                exported=2,
            ),
            issues=[],
            download_url="/api/v1/bank-payments/normalize/" + "a" * 32 + "/download",
            report_url="/api/v1/bank-payments/normalize/" + "a" * 32 + "/report",
        )

    monkeypatch.setattr(
        "app.api.bank_payments.bank_payment_service.export_sber_raw_statement",
        fake_export,
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/bank-payments/sber/export",
        json={"date_from": "2026-05-08", "date_to": "2026-05-08"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["detected_format"] == "sber_api_raw"
    assert payload["counts"]["exported"] == 2
    assert payload["report_url"]


def test_bank_payments_bitrix_sync_requires_bearer_token(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post("/api/v1/bank-payments/bitrix/sync")

    assert response.status_code == 401


def test_bank_payments_bitrix_sync_reports_missing_config(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)
    client = TestClient(app)

    response = client.post("/api/v1/bank-payments/bitrix/sync", headers=headers)

    assert response.status_code == 400
    assert "BANK_PAYMENTS_B24_WEBHOOK_URL" in response.json()["detail"]


def test_bank_payments_bitrix_sync_endpoint_returns_counts(monkeypatch, tmp_path) -> None:
    headers = _configure(monkeypatch, tmp_path)

    def fake_sync():
        return {"processed": 2, "ready": 1, "errors": 1, "skipped": 3, "last_error": "ошибка"}

    monkeypatch.setattr(
        "app.api.bank_payments.bank_payment_worker.run_bank_payments_bitrix_disk_sync",
        fake_sync,
    )
    client = TestClient(app)

    response = client.post("/api/v1/bank-payments/bitrix/sync", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "processed": 2,
        "ready": 1,
        "errors": 1,
        "skipped": 3,
        "last_error": "ошибка",
    }
