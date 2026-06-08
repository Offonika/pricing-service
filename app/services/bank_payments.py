from __future__ import annotations

import csv
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.schemas.bank_payments import (
    BankPaymentClassifyRequest,
    BankPaymentClassifyResponse,
    BankPaymentNormalizeCounts,
    BankPaymentNormalizeResponse,
)

ONEC_FORMAT = "1c_client_bank_exchange"
GENERIC_CSV_FORMAT = "generic_bank_csv"
UNKNOWN_FORMAT = "unknown"
NORMALIZED_FILENAME = "normalized_1c_client_bank_exchange.txt"
REPORT_FILENAME = "report.txt"
METADATA_FILENAME = "metadata.json"

_ACCOUNT_RE = re.compile(r"\d{16,25}")
_INN_RE = re.compile(r"(?<!\d)\d{10}(?:\d{2})?(?!\d)")
_SAFE_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class BankPaymentsSourceConfigurationError(RuntimeError):
    pass


@dataclass
class NormalizedPaymentLine:
    direction: str
    document_kind: str
    payment_date: date
    number: str
    amount: Decimal
    payer_name: str = ""
    payer_inn: str = ""
    payer_kpp: str = ""
    payer_account: str = ""
    payer_bank: str = ""
    payer_bic: str = ""
    payer_correspondent_account: str = ""
    recipient_name: str = ""
    recipient_inn: str = ""
    recipient_kpp: str = ""
    recipient_account: str = ""
    recipient_bank: str = ""
    recipient_bic: str = ""
    recipient_correspondent_account: str = ""
    purpose: str = ""
    payment_purpose_code: str = ""
    priority: str = ""
    payer_account_is_own: bool = False
    recipient_account_is_own: bool = False
    existing_document_found: bool = False
    source_type: str = ""
    source_bank: str = ""
    source_id: str = ""
    source_account: str = ""
    scenario: str = ""
    operation_code: str = ""
    cash_flow_article_name: str = ""
    contract_code: str = ""
    physical_person_name: str = ""
    skip_auto_contract_fill: bool = False
    skip_payment_fill: bool = False
    should_load: bool = True
    quality_flags: list[str] | None = None
    acquiring_merchant_id: str = ""
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    commission_amount: Decimal | None = None


@dataclass
class ParsedBankStatement:
    detected_format: str
    lines: list[NormalizedPaymentLine]
    source_lines: int
    issues: list[str]


def normalize_upload(
    content: bytes,
    *,
    filename: str,
    source_bank: str,
    settings: Settings,
) -> BankPaymentNormalizeResponse:
    upload_id = uuid.uuid4().hex
    upload_dir = Path(settings.bank_payments_artifact_dir) / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "source.bin").write_bytes(content)

    parsed = parse_bank_statement(content, settings=settings, source_bank=source_bank)
    classifications = classify_and_mark_lines(parsed.lines, settings=settings)
    manual_review_count = sum(1 for item in classifications if item.scenario == "manual_review")

    issues = list(parsed.issues)
    export_lines = list(parsed.lines)
    status = "ready" if export_lines else "manual_review"
    download_url: str | None = None
    report_url: str | None = f"/api/v1/bank-payments/normalize/{upload_id}/report"
    exported_count = len(export_lines)
    if not export_lines:
        status = "manual_review"
    else:
        download_url = f"/api/v1/bank-payments/normalize/{upload_id}/download"
        export_text = export_1c_client_bank_exchange(export_lines)
        (upload_dir / NORMALIZED_FILENAME).write_bytes(
            export_text.encode("cp1251", errors="replace")
        )

    counts = BankPaymentNormalizeCounts(
        source_lines=parsed.source_lines,
        payments=len(parsed.lines),
        classified=len(classifications),
        manual_review=manual_review_count,
        exported=exported_count,
    )
    response = BankPaymentNormalizeResponse(
        upload_id=upload_id,
        status=status,
        detected_format=parsed.detected_format,
        counts=counts,
        issues=[mask_financial_text(issue) for issue in issues],
        download_url=download_url,
        report_url=report_url,
    )
    _write_report(upload_dir, response=response, classifications=classifications)
    _write_metadata(
        upload_dir,
        response=response,
        filename=filename,
        source_bank=source_bank,
        classifications=classifications,
    )
    return response


def get_normalized_file_path(upload_id: str, settings: Settings) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise FileNotFoundError(upload_id)
    path = Path(settings.bank_payments_artifact_dir) / upload_id / NORMALIZED_FILENAME
    if not path.exists():
        raise FileNotFoundError(upload_id)
    return path


def get_report_file_path(upload_id: str, settings: Settings) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise FileNotFoundError(upload_id)
    path = Path(settings.bank_payments_artifact_dir) / upload_id / REPORT_FILENAME
    if not path.exists():
        raise FileNotFoundError(upload_id)
    return path


def classify_and_mark_lines(
    lines: list[NormalizedPaymentLine],
    *,
    settings: Settings,
) -> list[BankPaymentClassifyResponse]:
    classifications: list[BankPaymentClassifyResponse] = []
    for line in lines:
        classification = classify_line(line, settings=settings)
        _apply_classification(line, classification)
        classifications.append(classification)
    return classifications


def export_sber_raw_statement(
    *,
    date_from: date,
    date_to: date,
    account_numbers: list[str] | None,
    settings: Settings,
    engine: Engine | None = None,
) -> BankPaymentNormalizeResponse:
    if date_to < date_from:
        raise BankPaymentsSourceConfigurationError(
            "date_to must be greater than or equal to date_from"
        )
    if not settings.bank_payments_source_database_url and engine is None:
        raise BankPaymentsSourceConfigurationError(
            "BANK_PAYMENTS_SOURCE_DATABASE_URL is not configured"
        )

    upload_id = uuid.uuid4().hex
    upload_dir = Path(settings.bank_payments_artifact_dir) / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    source_engine = engine or create_engine(settings.bank_payments_source_database_url or "")
    rows = fetch_sber_raw_statement_rows(
        source_engine,
        schema=settings.bank_payments_source_schema,
        date_from=date_from,
        date_to=date_to,
        account_numbers=account_numbers,
    )
    lines = [line_from_sber_raw_row(row, settings=settings) for row in rows]
    classifications = classify_and_mark_lines(lines, settings=settings)
    manual_review_count = sum(1 for item in classifications if item.scenario == "manual_review")
    export_lines = list(lines)
    issues = []
    if manual_review_count:
        issues.append(f"Строк на ручную проверку: {manual_review_count}.")

    download_url = None
    if export_lines:
        export_text = export_1c_client_bank_exchange(export_lines)
        (upload_dir / NORMALIZED_FILENAME).write_bytes(
            export_text.encode("cp1251", errors="replace")
        )
        download_url = f"/api/v1/bank-payments/normalize/{upload_id}/download"

    counts = BankPaymentNormalizeCounts(
        source_lines=len(rows),
        payments=len(lines),
        classified=len(classifications),
        manual_review=manual_review_count,
        exported=len(export_lines),
    )
    response = BankPaymentNormalizeResponse(
        upload_id=upload_id,
        status="ready" if export_lines else "manual_review",
        detected_format="sber_api_raw",
        counts=counts,
        issues=[mask_financial_text(issue) for issue in issues],
        download_url=download_url,
        report_url=f"/api/v1/bank-payments/normalize/{upload_id}/report",
    )
    _write_report(upload_dir, response=response, classifications=classifications)
    _write_metadata(
        upload_dir,
        response=response,
        filename=f"sber-api-raw-{date_from.isoformat()}-{date_to.isoformat()}",
        source_bank="sber_api_raw",
        classifications=classifications,
    )
    return response


def parse_bank_statement(
    content: bytes,
    *,
    settings: Settings,
    source_bank: str = "auto",
) -> ParsedBankStatement:
    text = _decode_bank_file(content)
    source_lines = len([line for line in text.splitlines() if line.strip()])
    if "1CClientBankExchange" in text[:200]:
        return _parse_1c_client_bank_exchange(text, settings=settings, source_lines=source_lines)

    csv_result = _parse_generic_bank_csv(text, settings=settings, source_lines=source_lines)
    if csv_result.lines:
        return csv_result

    issue = (
        f"Формат банка {source_bank!r} не распознан. "
        "Добавьте redacted fixture и parser перед экспортом в 1С."
    )
    return ParsedBankStatement(
        detected_format=UNKNOWN_FORMAT,
        lines=[],
        source_lines=source_lines,
        issues=[issue],
    )


def fetch_sber_raw_statement_rows(
    engine: Engine,
    *,
    schema: str,
    date_from: date,
    date_to: date,
    account_numbers: list[str] | None = None,
) -> list[dict[str, Any]]:
    safe_schema = _safe_sql_identifier(schema)
    params: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
    account_filter = ""
    statement = text(f"""
        SELECT
          transaction_key,
          operation_id,
          account_number,
          statement_date,
          operation_datetime,
          operation_date,
          document_date,
          value_date,
          direction,
          amount,
          amount_rub,
          bank_doc_number,
          operation_code,
          counterparty_account,
          counterparty_name,
          counterparty_inn,
          payment_purpose,
          is_acquiring_candidate,
          acquiring_merchant_id,
          source_payload_hash
        FROM {safe_schema}.raw_sber_statement_transaction
        WHERE statement_date >= :date_from
          AND statement_date <= :date_to
          {account_filter}
        ORDER BY statement_date, operation_datetime, bank_doc_number, amount
        """)
    if account_numbers:
        account_filter = "AND account_number IN :account_numbers"
        params["account_numbers"] = tuple(account_numbers)
        statement = text(f"""
            SELECT
              transaction_key,
              operation_id,
              account_number,
              statement_date,
              operation_datetime,
              operation_date,
              document_date,
              value_date,
              direction,
              amount,
              amount_rub,
              bank_doc_number,
              operation_code,
              counterparty_account,
              counterparty_name,
              counterparty_inn,
              payment_purpose,
              is_acquiring_candidate,
              acquiring_merchant_id,
              source_payload_hash
            FROM {safe_schema}.raw_sber_statement_transaction
            WHERE statement_date >= :date_from
              AND statement_date <= :date_to
              {account_filter}
            ORDER BY statement_date, operation_datetime, bank_doc_number, amount
            """).bindparams(bindparam("account_numbers", expanding=True))
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(statement, params).mappings().all()]


def line_from_sber_raw_row(
    row: dict[str, Any],
    *,
    settings: Settings,
) -> NormalizedPaymentLine:
    raw_direction = _norm(str(row.get("direction") or ""))
    direction = "outgoing" if raw_direction in {"debit", "дебет", "out", "outgoing"} else "incoming"
    amount = abs(_to_decimal(row.get("amount")))
    account_number = str(row.get("account_number") or "").strip()
    counterparty_name = str(row.get("counterparty_name") or "").strip()
    counterparty_inn = str(row.get("counterparty_inn") or "").strip()
    counterparty_account = str(row.get("counterparty_account") or "").strip()
    payment_date = (
        _date_from_any(row.get("operation_date"))
        or _date_from_any(row.get("operation_datetime"))
        or _date_from_any(row.get("statement_date"))
        or date.today()
    )
    source_id = str(
        row.get("transaction_key")
        or row.get("operation_id")
        or row.get("source_payload_hash")
        or ""
    ).strip()
    purpose = str(row.get("payment_purpose") or "").strip()
    quality_flags: list[str] = []
    if row.get("is_acquiring_candidate") and not (
        str(row.get("acquiring_merchant_id") or "").strip()
        or _extract_acquiring_merchant_id(purpose)
    ):
        quality_flags.append("merchant_mapping_review")

    if direction == "incoming":
        payer_requisites = _sber_counterparty_requisites(
            counterparty_name,
            counterparty_account,
            purpose,
        )
        payer_name = counterparty_name
        payer_inn = counterparty_inn
        payer_account = counterparty_account
        recipient_name = settings.bank_payments_own_name
        recipient_inn = settings.bank_payments_own_inn
        recipient_account = account_number
        payer_bank = payer_requisites["bank_name"] or counterparty_name
        recipient_bank = settings.bank_payments_own_bank_name
        payer_bic = payer_requisites["bic"]
        recipient_bic = settings.bank_payments_own_bank_bic
        payer_correspondent_account = payer_requisites["correspondent_account"]
        recipient_correspondent_account = settings.bank_payments_own_bank_correspondent_account
        payer_kpp = payer_requisites["kpp"]
        recipient_kpp = settings.bank_payments_own_kpp
    else:
        recipient_requisites = _sber_counterparty_requisites(
            counterparty_name,
            counterparty_account,
            purpose,
        )
        payer_name = settings.bank_payments_own_name
        payer_inn = settings.bank_payments_own_inn
        payer_account = account_number
        recipient_name = counterparty_name
        recipient_inn = counterparty_inn
        recipient_account = counterparty_account
        payer_bank = settings.bank_payments_own_bank_name
        recipient_bank = recipient_requisites["bank_name"] or counterparty_name
        payer_bic = settings.bank_payments_own_bank_bic
        recipient_bic = recipient_requisites["bic"]
        payer_correspondent_account = settings.bank_payments_own_bank_correspondent_account
        recipient_correspondent_account = recipient_requisites["correspondent_account"]
        payer_kpp = settings.bank_payments_own_kpp
        recipient_kpp = recipient_requisites["kpp"]

    return NormalizedPaymentLine(
        direction=direction,
        document_kind=_document_kind(direction),
        payment_date=payment_date,
        number=str(row.get("bank_doc_number") or row.get("operation_id") or "").strip(),
        amount=amount,
        payer_name=payer_name,
        payer_inn=payer_inn,
        payer_kpp=payer_kpp,
        payer_account=payer_account,
        payer_bank=payer_bank,
        payer_bic=payer_bic,
        payer_correspondent_account=payer_correspondent_account,
        recipient_name=recipient_name,
        recipient_inn=recipient_inn,
        recipient_kpp=recipient_kpp,
        recipient_account=recipient_account,
        recipient_bank=recipient_bank,
        recipient_bic=recipient_bic,
        recipient_correspondent_account=recipient_correspondent_account,
        purpose=purpose,
        payment_purpose_code="1" if _is_sber_salary_registry_purpose(purpose) else "",
        priority="3" if _is_sber_salary_registry_purpose(purpose) else "",
        payer_account_is_own=direction == "outgoing"
        or _is_own_account(payer_account, settings.bank_payments_own_accounts),
        recipient_account_is_own=direction == "incoming"
        or _is_own_account(recipient_account, settings.bank_payments_own_accounts),
        source_type="sber_api_raw",
        source_bank="sber",
        source_id=source_id,
        source_account=account_number,
        quality_flags=quality_flags,
        acquiring_merchant_id=str(row.get("acquiring_merchant_id") or "").strip()
        or _extract_acquiring_merchant_id(purpose),
        net_amount=amount,
    )


def classify_request(
    payload: BankPaymentClassifyRequest,
    *,
    settings: Settings,
) -> BankPaymentClassifyResponse:
    line = NormalizedPaymentLine(
        direction=payload.direction
        or _infer_direction(
            payload.payer_account,
            payload.recipient_account,
            settings.bank_payments_own_accounts,
            payload.payer_account_is_own,
            payload.recipient_account_is_own,
        ),
        document_kind=payload.document_kind or "",
        payment_date=payload.payment_date or date.today(),
        number=payload.number or "",
        amount=_to_decimal(payload.amount),
        payer_name=payload.payer_name or "",
        payer_inn=payload.payer_inn or "",
        payer_kpp=payload.payer_kpp or "",
        payer_account=payload.payer_account or "",
        payer_bank=payload.payer_bank or "",
        recipient_name=payload.recipient_name or "",
        recipient_inn=payload.recipient_inn or "",
        recipient_kpp=payload.recipient_kpp or "",
        recipient_account=payload.recipient_account or "",
        recipient_bank=payload.recipient_bank or "",
        purpose=payload.purpose or "",
        payment_purpose_code=payload.payment_purpose_code or "",
        priority=payload.priority or "",
        payer_account_is_own=payload.payer_account_is_own,
        recipient_account_is_own=payload.recipient_account_is_own,
        existing_document_found=payload.existing_document_found,
    )
    return classify_line(line, settings=settings)


def classify_line(
    line: NormalizedPaymentLine, *, settings: Settings
) -> BankPaymentClassifyResponse:
    purpose = _norm(line.purpose)
    payer = _norm(" ".join([line.payer_name, line.payer_bank]))
    recipient = _norm(" ".join([line.recipient_name, line.recipient_bank]))
    direction = line.direction
    payer_own = line.payer_account_is_own or _is_own_account(
        line.payer_account, settings.bank_payments_own_accounts
    )
    recipient_own = line.recipient_account_is_own or _is_own_account(
        line.recipient_account, settings.bank_payments_own_accounts
    )

    if direction == "outgoing" and recipient_own:
        return BankPaymentClassifyResponse(
            scenario="internal_transfer_out",
            operation_code="ПереводНаДругойСчет",
            cash_flow_article_name="Внутреннее перемещение денежных средств",
            skip_auto_contract_fill=True,
            should_load=True,
            existing_document_policy="default",
            confidence=0.98,
            reasons=["получатель является собственным счетом"],
        )

    if direction == "incoming" and payer_own:
        return BankPaymentClassifyResponse(
            scenario="internal_transfer_in",
            operation_code="ПрочееПоступлениеБезналичныхДенежныхСредств",
            cash_flow_article_name="Внутреннее перемещение денежных средств",
            skip_auto_contract_fill=True,
            skip_payment_fill=True,
            should_load=False,
            existing_document_policy="skip_duplicate_internal_in",
            confidence=0.98,
            reasons=["плательщик является собственным счетом"],
        )

    if _is_salary_registry(line, purpose, recipient):
        return BankPaymentClassifyResponse(
            scenario="salary_registry",
            operation_code="осиВыплатаЗаработнойПлаты",
            cash_flow_article_name="Оплата труда (аванс)",
            physical_person_name=settings.bank_payments_salary_person_name,
            skip_auto_contract_fill=True,
            should_load=True,
            existing_document_policy="manual_check_existing",
            confidence=0.91,
            reasons=["назначение похоже на зарплатный реестр"],
        )

    counterparty = recipient if direction == "outgoing" else payer
    founder_text = " ".join([counterparty, purpose])
    counterparty_has_founder_name = "ахмедов" in counterparty and "эльдар" in counterparty
    personal_entrepreneur_funds = (
        direction == "outgoing"
        and "ахмедов" in founder_text
        and "эльдар" in founder_text
        and "личные средства" in purpose
        and "предпринимател" in purpose
    )
    if counterparty_has_founder_name or personal_entrepreneur_funds:
        operation_code = (
            "ПрочееСписаниеБезналичныхДенежныхСредств"
            if direction == "outgoing"
            else "ПрочиеРасчетыСКонтрагентами"
        )
        reasons = ["сторона платежа похожа на учредителя"]
        if personal_entrepreneur_funds:
            reasons = ["назначение похоже на личные средства предпринимателя"]
        return BankPaymentClassifyResponse(
            scenario="founder",
            operation_code=operation_code,
            cash_flow_article_name="Расчеты с учредителями",
            skip_auto_contract_fill=True,
            should_load=True,
            existing_document_policy="manual_check_existing",
            confidence=0.88,
            reasons=reasons,
        )

    if _is_card_payment(direction, purpose, payer):
        scenario = "card_payment"
        reasons = ["назначение или плательщик похожи на СБП/эквайринг"]
        if line.source_type == "sber_api_raw" or line.source_bank == "sber":
            scenario = "card_payment_sber_acquiring"
            reasons = ["строка Sber API похожа на эквайринг"]
        return BankPaymentClassifyResponse(
            scenario=scenario,
            operation_code="ПоступлениеОплатыПоПлатежнымКартам",
            cash_flow_article_name="Оплата от покупателя (за товар)",
            contract_code=settings.bank_payments_acquiring_contract_code,
            skip_auto_contract_fill=False,
            should_load=True,
            existing_document_policy="allow_card_payment_rewrite",
            confidence=0.94,
            reasons=reasons,
        )

    if _is_bank_return(direction, purpose, payer):
        return BankPaymentClassifyResponse(
            scenario="bank_return",
            operation_code="ПрочиеРасчетыСКонтрагентами",
            cash_flow_article_name="Возвраты банка / неисполненные платежи",
            should_load=True,
            existing_document_policy="manual_check_existing",
            confidence=0.86,
            reasons=["назначение похоже на возврат или неисполненный платеж банка"],
        )

    if direction == "incoming" and recipient_own and not payer_own:
        return BankPaymentClassifyResponse(
            scenario="customer_payment",
            operation_code="ОплатаПокупателя",
            cash_flow_article_name="Оплата от покупателей",
            should_load=True,
            existing_document_policy="default",
            confidence=0.78,
            reasons=["внешний плательщик перечислил деньги на собственный счет"],
        )

    if direction == "outgoing" and payer_own and not recipient_own:
        return BankPaymentClassifyResponse(
            scenario="supplier_payment",
            operation_code="ОплатаПоставщику",
            cash_flow_article_name="Оплата поставщикам",
            should_load=True,
            existing_document_policy="default",
            confidence=0.78,
            reasons=["платеж с собственного счета внешнему получателю"],
        )

    return BankPaymentClassifyResponse(
        scenario="manual_review",
        should_load=False,
        existing_document_policy="manual_review",
        confidence=0.35,
        reasons=["нет уверенного правила классификации"],
    )


def export_1c_client_bank_exchange(lines: list[NormalizedPaymentLine]) -> str:
    if not lines:
        return "1CClientBankExchange\r\nКонецФайла\r\n"

    start = min(line.payment_date for line in lines)
    end = max(line.payment_date for line in lines)
    account = _primary_own_account(lines)
    incoming_total = sum(
        (line.amount for line in lines if line.direction == "incoming"), Decimal("0")
    )
    outgoing_total = sum(
        (line.amount for line in lines if line.direction == "outgoing"), Decimal("0")
    )

    output = [
        "1CClientBankExchange",
        "ВерсияФормата=1.03",
        "Кодировка=Windows",
        "Отправитель=pricing-service",
        "Получатель=1С Клиент-Банк",
        f"ДатаСоздания={_format_1c_date(date.today())}",
        f"ВремяСоздания={datetime.now().strftime('%H:%M:%S')}",
        f"ДатаНачала={_format_1c_date(start)}",
        f"ДатаКонца={_format_1c_date(end)}",
    ]
    if account:
        output.append(f"РасчСчет={account}")
        output.extend(
            [
                "СекцияРасчСчет",
                f"ДатаНачала={_format_1c_date(start)}",
                f"ДатаКонца={_format_1c_date(end)}",
                f"РасчСчет={account}",
                "НачальныйОстаток=0.00",
                f"ВсегоПоступило={_format_amount(incoming_total)}",
                f"ВсегоСписано={_format_amount(outgoing_total)}",
                "КонечныйОстаток=0.00",
                "КонецРасчСчет",
            ]
        )

    for line in lines:
        output.extend(_export_document(line))
    output.append("КонецФайла")
    return "\r\n".join(output) + "\r\n"


def _write_metadata(
    upload_dir: Path,
    *,
    response: BankPaymentNormalizeResponse,
    filename: str,
    source_bank: str,
    classifications: list[BankPaymentClassifyResponse],
) -> None:
    metadata = {
        "upload_id": response.upload_id,
        "source_filename": Path(filename).name,
        "source_bank": source_bank,
        "status": response.status,
        "detected_format": response.detected_format,
        "counts": response.counts.model_dump(),
        "issues": response.issues,
        "classifications": [item.model_dump() for item in classifications],
    }
    (upload_dir / METADATA_FILENAME).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_report(
    upload_dir: Path,
    *,
    response: BankPaymentNormalizeResponse,
    classifications: list[BankPaymentClassifyResponse],
) -> None:
    by_scenario: dict[str, int] = {}
    for classification in classifications:
        by_scenario[classification.scenario] = by_scenario.get(classification.scenario, 0) + 1
    lines = [
        "Банковские платежи",
        f"Upload ID: {response.upload_id}",
        f"Статус: {response.status}",
        f"Формат: {response.detected_format}",
        f"Строк исходника: {response.counts.source_lines}",
        f"Платежей: {response.counts.payments}",
        f"Классифицировано: {response.counts.classified}",
        f"Ручная проверка: {response.counts.manual_review}",
        f"Экспортировано: {response.counts.exported}",
    ]
    if by_scenario:
        lines.append("Сценарии:")
        for scenario, count in sorted(by_scenario.items()):
            lines.append(f"- {scenario}: {count}")
    if response.issues:
        lines.append("Проблемы:")
        lines.extend(f"- {issue}" for issue in response.issues)
    (upload_dir / REPORT_FILENAME).write_text(
        mask_financial_text("\n".join(lines) + "\n"),
        encoding="utf-8",
    )


def _apply_classification(
    line: NormalizedPaymentLine,
    classification: BankPaymentClassifyResponse,
) -> None:
    line.scenario = classification.scenario
    line.operation_code = classification.operation_code or ""
    line.cash_flow_article_name = classification.cash_flow_article_name or ""
    line.contract_code = classification.contract_code or ""
    line.physical_person_name = classification.physical_person_name or ""
    line.skip_auto_contract_fill = classification.skip_auto_contract_fill
    line.skip_payment_fill = classification.skip_payment_fill
    line.should_load = classification.should_load
    if classification.scenario == "manual_review":
        line.should_load = False


def _parse_1c_client_bank_exchange(
    text: str,
    *,
    settings: Settings,
    source_lines: int,
) -> ParsedBankStatement:
    current_account = ""
    fields: dict[str, str] | None = None
    result: list[NormalizedPaymentLine] = []
    issues: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if fields is None and line.startswith("РасчСчет="):
            current_account = line.split("=", 1)[1].strip()
            continue
        if line.startswith("СекцияДокумент="):
            fields = {"СекцияДокумент": line.split("=", 1)[1].strip()}
            continue
        if line == "КонецДокумента":
            if fields is not None:
                parsed = _line_from_1c_fields(fields, current_account, settings)
                if parsed is None:
                    issues.append("Одна строка 1CClientBankExchange пропущена: нет даты или суммы.")
                else:
                    result.append(parsed)
            fields = None
            continue
        if fields is not None and "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()

    return ParsedBankStatement(
        detected_format=ONEC_FORMAT,
        lines=result,
        source_lines=source_lines,
        issues=issues,
    )


def _parse_generic_bank_csv(
    text: str,
    *,
    settings: Settings,
    source_lines: int,
) -> ParsedBankStatement:
    sample = text[:2000]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.DictReader(StringIO(text), delimiter=delimiter)
    if not reader.fieldnames or len(reader.fieldnames) < 3:
        return ParsedBankStatement(GENERIC_CSV_FORMAT, [], source_lines, [])

    rows: list[NormalizedPaymentLine] = []
    issues: list[str] = []
    for index, row in enumerate(reader, start=2):
        normalized = {_norm_header(key): value for key, value in row.items() if key is not None}
        payment_date = _parse_date(
            _pick(normalized, "дата", "датаоперации", "датаплатежа", "date", "paymentdate")
        )
        amount = _parse_amount(_pick(normalized, "сумма", "amount", "поступило", "списано", "sum"))
        if payment_date is None or amount is None:
            issues.append(f"Строка {index}: пропущена, нет даты или суммы.")
            continue
        direction = _direction_from_csv(normalized, amount)
        payer_account = _pick(normalized, "плательщиксчет", "счетплательщика", "payeraccount")
        recipient_account = _pick(
            normalized, "получательсчет", "счетполучателя", "recipientaccount"
        )
        rows.append(
            NormalizedPaymentLine(
                direction=direction,
                document_kind=_document_kind(direction),
                payment_date=payment_date,
                number=_pick(normalized, "номер", "номердокумента", "number", "docnumber"),
                amount=abs(amount),
                payer_name=_pick(normalized, "плательщик", "payer", "payername"),
                payer_inn=_pick(normalized, "плательщикинн", "иннплательщика", "payerinn"),
                payer_kpp=_pick(normalized, "плательщиккпп", "кппплательщика", "payerkpp"),
                payer_account=payer_account,
                payer_bank=_pick(normalized, "плательщикбанк", "банкплательщика", "payerbank"),
                payer_bic=_pick(normalized, "плательщикбик", "бикплательщика", "payerbic"),
                recipient_name=_pick(normalized, "получатель", "recipient", "recipientname"),
                recipient_inn=_pick(normalized, "получательинн", "иннполучателя", "recipientinn"),
                recipient_kpp=_pick(normalized, "получателькпп", "кппполучателя", "recipientkpp"),
                recipient_account=recipient_account,
                recipient_bank=_pick(
                    normalized, "получательбанк", "банкполучателя", "recipientbank"
                ),
                recipient_bic=_pick(normalized, "получательбик", "бикполучателя", "recipientbic"),
                purpose=_pick(normalized, "назначение", "назначениеплатежа", "purpose"),
                payment_purpose_code=_pick(
                    normalized, "кодназначенияплатежа", "кодназплатежа", "paymentpurposecode"
                ),
                priority=_pick(normalized, "очередность", "priority"),
                payer_account_is_own=_is_own_account(
                    payer_account, settings.bank_payments_own_accounts
                ),
                recipient_account_is_own=_is_own_account(
                    recipient_account, settings.bank_payments_own_accounts
                ),
            )
        )
    if not rows:
        issues = []
    return ParsedBankStatement(GENERIC_CSV_FORMAT, rows, source_lines, issues)


def _line_from_1c_fields(
    fields: dict[str, str],
    current_account: str,
    settings: Settings,
) -> NormalizedPaymentLine | None:
    payment_date = _parse_date(fields.get("Дата"))
    amount = _parse_amount(fields.get("Сумма"))
    if payment_date is None or amount is None:
        return None
    payer_account = fields.get("ПлательщикСчет", "")
    recipient_account = fields.get("ПолучательСчет", "")
    direction = _infer_direction(
        payer_account,
        recipient_account,
        [*settings.bank_payments_own_accounts, current_account],
    )
    return NormalizedPaymentLine(
        direction=direction,
        document_kind=_document_kind(direction),
        payment_date=payment_date,
        number=fields.get("Номер", ""),
        amount=abs(amount),
        payer_name=fields.get("Плательщик") or fields.get("Плательщик1", ""),
        payer_inn=fields.get("ПлательщикИНН", ""),
        payer_kpp=fields.get("ПлательщикКПП", ""),
        payer_account=payer_account,
        payer_bank=fields.get("ПлательщикБанк1", ""),
        payer_bic=fields.get("ПлательщикБИК", ""),
        payer_correspondent_account=fields.get("ПлательщикКорсчет", ""),
        recipient_name=fields.get("Получатель") or fields.get("Получатель1", ""),
        recipient_inn=fields.get("ПолучательИНН", ""),
        recipient_kpp=fields.get("ПолучательКПП", ""),
        recipient_account=recipient_account,
        recipient_bank=fields.get("ПолучательБанк1", ""),
        recipient_bic=fields.get("ПолучательБИК", ""),
        recipient_correspondent_account=fields.get("ПолучательКорсчет", ""),
        purpose=_join_purpose(fields),
        payment_purpose_code=fields.get("КодНазПлатежа", ""),
        priority=fields.get("Очередность", ""),
        payer_account_is_own=_is_own_account(
            payer_account, [*settings.bank_payments_own_accounts, current_account]
        ),
        recipient_account_is_own=_is_own_account(
            recipient_account, [*settings.bank_payments_own_accounts, current_account]
        ),
        scenario=fields.get("СценарийИмпортаMM", ""),
        operation_code=fields.get("ВидОперацииMM", ""),
        skip_auto_contract_fill=_parse_1c_bool(fields.get("НеЗаполнятьДоговорMM")),
        skip_payment_fill=_parse_1c_bool(fields.get("НеЗаполнятьОплатуMM")),
    )


def _export_document(line: NormalizedPaymentLine) -> list[str]:
    section = _document_section(line)
    operation_date = _format_1c_date(line.payment_date)
    result = [
        f"СекцияДокумент={section}",
        f"Номер={line.number}",
        f"Дата={_format_1c_date(line.payment_date)}",
        f"Сумма={_format_amount(line.amount)}",
        f"ПлательщикСчет={line.payer_account}",
        f"ДатаСписано={operation_date if line.direction == 'outgoing' else ''}",
        f"Плательщик={line.payer_name}",
        f"ПлательщикИНН={line.payer_inn}",
        f"ПлательщикКПП={line.payer_kpp}",
        f"ПлательщикРасчСчет={line.payer_account}",
        f"ПлательщикБанк1={line.payer_bank}",
        f"ПлательщикБИК={line.payer_bic}",
        f"ПлательщикКорсчет={line.payer_correspondent_account}",
        f"ПолучательСчет={line.recipient_account}",
        f"ДатаПоступило={operation_date if line.direction == 'incoming' else ''}",
        f"Получатель={line.recipient_name}",
        f"ПолучательИНН={line.recipient_inn}",
        f"ПолучательКПП={line.recipient_kpp}",
        f"ПолучательРасчСчет={line.recipient_account}",
        f"ПолучательБанк1={line.recipient_bank}",
        f"ПолучательБИК={line.recipient_bic}",
        f"ПолучательКорсчет={line.recipient_correspondent_account}",
        "ВидПлатежа=электронно",
        f"ВидОплаты={_payment_type_code(section)}",
        f"Код={_payment_code(line, section)}",
        "СтатусСоставителя=",
        "ПоказательКБК=",
        "ОКАТО=",
        "ПоказательОснования=",
        "ПоказательПериода=",
        "ПоказательНомера=",
        "ПоказательДаты=",
        "ПоказательТипа=",
        f"Очередность={line.priority or '5'}",
        f"НазначениеПлатежа={line.purpose}",
    ]
    if line.payment_purpose_code:
        result.append(f"КодНазПлатежа={line.payment_purpose_code}")
    result.append("КонецДокумента")
    return result


def _decode_bank_file(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _date_from_any(value: Any) -> date | None:
    parsed = _parse_date(value)
    if parsed:
        return parsed
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_amount(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _to_decimal(value: float | int | str | None) -> Decimal:
    parsed = _parse_amount(value)
    return parsed if parsed is not None else Decimal("0")


def _format_1c_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _format_amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _format_1c_bool(value: bool) -> str:
    return "Истина" if value else "Ложь"


def _document_section(line: NormalizedPaymentLine) -> str:
    if line.source_type == "sber_api_raw" and _is_bank_order(line):
        return "Банковский ордер"
    return "Платежное поручение"


def _is_bank_order(line: NormalizedPaymentLine) -> bool:
    purpose = _norm(line.purpose)
    counterparty = _norm(
        " ".join([line.payer_name, line.recipient_name, line.payer_bank, line.recipient_bank])
    )
    return (
        "комиссия за" in purpose
        or "комиссия согласно" in purpose
        or "комиссия банка" in purpose
        or ("ведение счета" in purpose and "сбер" in counterparty)
    )


def _payment_type_code(section: str) -> str:
    return "17" if section == "Банковский ордер" else "01"


def _payment_code(line: NormalizedPaymentLine, section: str) -> str:
    if line.operation_code in {
        "ПоступлениеОплатыПоПлатежнымКартам",
        "осиВыплатаЗаработнойПлаты",
        "ПереводНаДругойСчет",
        "ПрочееСписаниеБезналичныхДенежныхСредств",
    }:
        return f"MMOP:{line.operation_code}"
    return "" if section == "Банковский ордер" else "0"


def _parse_1c_bool(value: str | None) -> bool:
    return _norm(value) in {"истина", "true", "1", "да"}


def _norm(value: str | None) -> str:
    return (value or "").casefold().replace("ё", "е")


def _norm_header(value: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", _norm(value))


def _pick(row: dict[str, str | None], *keys: str) -> str:
    for key in keys:
        value = row.get(_norm_header(key))
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _direction_from_csv(row: dict[str, str | None], amount: Decimal) -> str:
    raw = _norm(_pick(row, "направление", "direction", "тип", "operationtype"))
    if any(token in raw for token in ("поступ", "приход", "incoming", "credit", "in")):
        return "incoming"
    if any(token in raw for token in ("спис", "расход", "outgoing", "debit", "out")):
        return "outgoing"
    credited = _parse_amount(_pick(row, "поступило", "credit"))
    debited = _parse_amount(_pick(row, "списано", "debit"))
    if credited and credited > 0:
        return "incoming"
    if debited and debited > 0:
        return "outgoing"
    return "incoming" if amount >= 0 else "outgoing"


def _infer_direction(
    payer_account: str | None,
    recipient_account: str | None,
    own_accounts: list[str],
    payer_is_own: bool = False,
    recipient_is_own: bool = False,
) -> str:
    if payer_is_own or _is_own_account(payer_account, own_accounts):
        return "outgoing"
    if recipient_is_own or _is_own_account(recipient_account, own_accounts):
        return "incoming"
    return "incoming"


def _document_kind(direction: str) -> str:
    if direction == "outgoing":
        return "ПлатежноеПоручениеИсходящее"
    return "ПлатежноеПоручениеВходящее"


def _digits(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _is_own_account(account: str | None, own_accounts: list[str]) -> bool:
    digits = _digits(account)
    if not digits:
        return False
    return digits in {_digits(item) for item in own_accounts if _digits(item)}


def _safe_sql_identifier(value: str) -> str:
    if not _SAFE_SQL_IDENTIFIER_RE.fullmatch(value):
        raise BankPaymentsSourceConfigurationError("BANK_PAYMENTS_SOURCE_SCHEMA has invalid value")
    return value


def _sber_counterparty_requisites(
    name: str,
    account: str,
    purpose: str,
) -> dict[str, str]:
    normalized_name = _norm(name)
    normalized_purpose = _norm(purpose)
    account_digits = _digits(account)
    if "сбер" not in normalized_name:
        return {"bank_name": "", "kpp": "", "bic": "", "correspondent_account": ""}
    if "юго-запад" in normalized_name or account_digits.startswith("302"):
        return {
            "bank_name": "ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК",
            "kpp": "616143001",
            "bic": "040702615",
            "correspondent_account": "30101810907020000615",
        }
    if account_digits.startswith(("706", "474")) or "комиссия согласно" in normalized_purpose:
        return {
            "bank_name": "Ставропольское отделение №5230",
            "kpp": "263443001",
            "bic": "040702615",
            "correspondent_account": "30101810907020000615",
        }
    return {
        "bank_name": "ПАО Сбербанк",
        "kpp": "773601001",
        "bic": "044525225",
        "correspondent_account": "30101810400000000225",
    }


def _is_sber_salary_registry_purpose(purpose: str) -> bool:
    normalized = _norm(purpose)
    return "заработная плата" in normalized and "реестр" in normalized


def _extract_acquiring_merchant_id(value: str) -> str:
    match = re.search(r"мерчант\s*[№#Nn]?\s*(\d{6,})", value or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _is_acquiring_bank(value: str) -> bool:
    tokens = (
        "тбанк",
        "т-банк",
        "тинькофф",
        "русский стандарт",
        "сбер",
        "втб",
        "альфа",
        "газпромбанк",
        "райффайзен",
        "росбанк",
        "открытие",
        "промсвязьбанк",
        "псб",
        "мкб",
        "московский кредитный банк",
        "модульбанк",
        "точка",
        "юкасса",
        "yookassa",
        "юмани",
        "yoomoney",
        "яндекс касса",
        "яндекс.деньги",
        "яндекс деньги",
    )
    return any(token in value for token in tokens)


def _is_return_purpose(purpose: str) -> bool:
    return any(token in purpose for token in ("возврат", "неверн", "неисполн", "отказ"))


def _is_dry_acquiring_purpose(purpose: str) -> bool:
    has_contract = "договор" in purpose or "дог." in purpose
    return (
        "перевод средств по договор" in purpose
        or "перевод средств по дог." in purpose
        or ("перечислен" in purpose and has_contract)
        or ("реестр" in purpose and has_contract)
    )


def _is_card_payment(direction: str, purpose: str, payer: str) -> bool:
    explicit = (
        "эквайр" in purpose
        or "сбп" in purpose
        or ("реестр операц" in purpose and "комисс" in purpose)
        or ("сумма операц" in purpose and "комисс" in purpose)
    )
    dry = (
        _is_acquiring_bank(payer)
        and not _is_return_purpose(purpose)
        and _is_dry_acquiring_purpose(purpose)
    )
    return direction == "incoming" and (explicit or dry)


def _is_bank_return(direction: str, purpose: str, payer: str) -> bool:
    payer_is_bank = any(token in payer for token in ("банк", "втб", "сбер", "альфа"))
    return direction == "incoming" and payer_is_bank and _is_return_purpose(purpose)


def _is_salary_registry(line: NormalizedPaymentLine, purpose: str, recipient: str) -> bool:
    if line.direction != "outgoing":
        return False
    salary_purpose = (
        "зачисления на банковские счета" in purpose and "реестр" in purpose
    ) or _is_sber_salary_registry_purpose(purpose)
    recipient_bank = "банк" in recipient or "втб" in recipient
    code_matches = line.payment_purpose_code.strip() == "1" or line.priority.strip() == "3"
    return salary_purpose and recipient_bank and code_matches


def _join_purpose(fields: dict[str, str]) -> str:
    if fields.get("НазначениеПлатежа"):
        return fields["НазначениеПлатежа"]
    chunks = [
        fields.get(f"НазначениеПлатежа{index}", "")
        for index in range(1, 7)
        if fields.get(f"НазначениеПлатежа{index}", "")
    ]
    return " ".join(chunks)


def _primary_own_account(lines: list[NormalizedPaymentLine]) -> str:
    for line in lines:
        if line.direction == "incoming" and line.recipient_account:
            return line.recipient_account
        if line.direction == "outgoing" and line.payer_account:
            return line.payer_account
    return ""


def mask_financial_text(value: str) -> str:
    value = _ACCOUNT_RE.sub(lambda match: f"{match.group(0)[:4]}…{match.group(0)[-4:]}", value)
    return _INN_RE.sub(lambda match: f"{match.group(0)[:2]}…{match.group(0)[-2:]}", value)


__all__ = [
    "GENERIC_CSV_FORMAT",
    "ONEC_FORMAT",
    "UNKNOWN_FORMAT",
    "BankPaymentsSourceConfigurationError",
    "NormalizedPaymentLine",
    "classify_and_mark_lines",
    "classify_line",
    "classify_request",
    "export_1c_client_bank_exchange",
    "export_sber_raw_statement",
    "fetch_sber_raw_statement_rows",
    "get_normalized_file_path",
    "get_report_file_path",
    "line_from_sber_raw_row",
    "mask_financial_text",
    "normalize_upload",
    "parse_bank_statement",
]
