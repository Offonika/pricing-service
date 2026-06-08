from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.schemas.bank_payments import BankPaymentClassifyRequest
from app.services.bank_payments import (
    GENERIC_CSV_FORMAT,
    ONEC_FORMAT,
    UNKNOWN_FORMAT,
    NormalizedPaymentLine,
    classify_line,
    classify_request,
    export_1c_client_bank_exchange,
    export_sber_raw_statement,
    fetch_sber_raw_statement_rows,
    line_from_sber_raw_row,
    normalize_upload,
    parse_bank_statement,
)

OWN_ACCOUNT = "40702810000000000001"
SECOND_OWN_ACCOUNT = "40702810000000000002"


def _configure(monkeypatch, tmp_path):
    monkeypatch.setenv("BANK_PAYMENTS_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("BANK_PAYMENTS_OWN_ACCOUNTS", f"{OWN_ACCOUNT},{SECOND_OWN_ACCOUNT}")
    monkeypatch.setenv("BANK_PAYMENTS_ACQUIRING_CONTRACT_CODE", "РБ0022772")
    monkeypatch.setenv("BANK_PAYMENTS_SALARY_PERSON_NAME", "Зарплата")
    monkeypatch.setenv("BANK_PAYMENTS_OWN_NAME", "ИП Ахмедов Эльдар Айдынович")
    monkeypatch.setenv("BANK_PAYMENTS_OWN_INN", "070111355885")
    monkeypatch.setenv("BANK_PAYMENTS_OWN_KPP", "0")
    monkeypatch.setenv(
        "BANK_PAYMENTS_OWN_BANK_NAME",
        "СТАВРОПОЛЬСКОЕ ОТДЕЛЕНИЕ N5230 ПАО СБЕРБАНК",
    )
    monkeypatch.setenv("BANK_PAYMENTS_OWN_BANK_BIC", "040702615")
    monkeypatch.setenv(
        "BANK_PAYMENTS_OWN_BANK_CORRESPONDENT_ACCOUNT",
        "30101810907020000615",
    )
    get_settings.cache_clear()
    return get_settings()


def _line(**overrides) -> NormalizedPaymentLine:
    data = {
        "direction": "incoming",
        "document_kind": "ПлатежноеПоручениеВходящее",
        "payment_date": __import__("datetime").date(2026, 5, 11),
        "number": "1",
        "amount": Decimal("1000.00"),
        "payer_name": "ООО Плательщик",
        "payer_inn": "7712345678",
        "payer_account": "40802810000000000003",
        "payer_bank": "Банк плательщика",
        "recipient_name": "ООО Мастер Мобайл",
        "recipient_inn": "7723456789",
        "recipient_account": OWN_ACCOUNT,
        "recipient_bank": "Наш банк",
        "purpose": "Оплата заказа",
    }
    data.update(overrides)
    return NormalizedPaymentLine(**data)


def test_parse_ready_1c_client_bank_exchange_roundtrip(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)
    source = f"""1CClientBankExchange
ВерсияФормата=1.03
Кодировка=Windows
ДатаНачала=11.05.2026
ДатаКонца=11.05.2026
РасчСчет={OWN_ACCOUNT}
СекцияДокумент=Платежное поручение
Номер=42
Дата=11.05.2026
Сумма=1234.56
Плательщик=Т-Банк
ПлательщикИНН=7712345678
ПлательщикСчет=40802810000000000003
ПлательщикБанк1=Т-Банк
Получатель=ООО Мастер Мобайл
ПолучательИНН=7723456789
ПолучательСчет={OWN_ACCOUNT}
ПолучательБанк1=Наш банк
НазначениеПлатежа=СБП оплата по заказу
КонецДокумента
КонецФайла
"""
    parsed = parse_bank_statement(source.encode("cp1251"), settings=settings)

    assert parsed.detected_format == ONEC_FORMAT
    assert len(parsed.lines) == 1
    assert parsed.lines[0].number == "42"
    assert parsed.lines[0].recipient_account == OWN_ACCOUNT

    exported = export_1c_client_bank_exchange(parsed.lines)
    assert "1CClientBankExchange" in exported
    assert f"ПолучательСчет={OWN_ACCOUNT}" in exported
    assert "НазначениеПлатежа=СБП оплата по заказу" in exported


def test_generic_bank_csv_exports_1c_client_bank_exchange(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)
    source = (
        "Дата;Номер;Сумма;Направление;Плательщик;ПлательщикИНН;ПлательщикСчет;"
        "ПлательщикБанк;Получатель;ПолучательИНН;ПолучательСчет;ПолучательБанк;"
        "НазначениеПлатежа\n"
        "11.05.2026;7;2500,50;Поступление;Т-Банк;7712345678;40802810000000000003;"
        f"Т-Банк;ООО Мастер Мобайл;7723456789;{OWN_ACCOUNT};Наш банк;"
        "Перевод средств по договору эквайринга\n"
    )

    parsed = parse_bank_statement(source.encode("utf-8"), settings=settings)

    assert parsed.detected_format == GENERIC_CSV_FORMAT
    assert len(parsed.lines) == 1
    exported = export_1c_client_bank_exchange(parsed.lines)
    assert "СекцияДокумент=Платежное поручение" in exported
    assert "Сумма=2500.50" in exported
    assert f"РасчСчет={OWN_ACCOUNT}" in exported


def test_unknown_bank_format_is_manual_review_without_lines(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    parsed = parse_bank_statement(b"not a bank statement", settings=settings)

    assert parsed.detected_format == UNKNOWN_FORMAT
    assert parsed.lines == []
    assert parsed.issues


def test_normalize_upload_exports_manual_review_lines(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)
    source = (
        "Дата;Номер;Сумма;Направление;Плательщик;ПлательщикСчет;Получатель;ПолучательСчет;НазначениеПлатежа\n"
        "11.05.2026;99;100,00;Поступление;ООО Ромашка;40802810000000000003;ООО Мастер Мобайл;;Спорная операция\n"
    )

    response = normalize_upload(
        source.encode("utf-8"),
        filename="statement.csv",
        source_bank="test",
        settings=settings,
    )
    exported_path = tmp_path / response.upload_id / "normalized_1c_client_bank_exchange.txt"
    exported = exported_path.read_bytes().decode("cp1251")

    assert response.status == "ready"
    assert response.counts.manual_review == 1
    assert response.counts.exported == 1
    assert response.download_url
    assert "Номер=99" in exported
    assert "ВидОперацииMM=" not in exported
    assert "Код=MMOP:" not in exported
    assert "СценарийИмпортаMM=" not in exported
    assert "ВидОперации=" not in exported


def test_classifier_detects_card_payment_explicit_sbp(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(_line(purpose="СБП оплата по QR"), settings=settings)

    assert result.scenario == "card_payment"
    assert result.operation_code == "ПоступлениеОплатыПоПлатежнымКартам"
    assert result.contract_code == "РБ0022772"
    assert result.skip_auto_contract_fill is False


def test_classifier_detects_sber_acquiring_source(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(
            source_type="sber_api_raw",
            source_bank="sber",
            payer_name="ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК",
            purpose="Зачисление средств по операциям эквайринга. Мерчант №711000421047.",
        ),
        settings=settings,
    )

    assert result.scenario == "card_payment_sber_acquiring"
    assert result.should_load is True


def test_classifier_does_not_mark_own_entrepreneur_name_as_founder(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(
            source_type="sber_api_raw",
            source_bank="sber",
            payer_name='ОАО "Сбербанк России"',
            payer_bank="Сбербанк",
            recipient_name="ИП Ахмедов Эльдар Айдынович",
            recipient_account=OWN_ACCOUNT,
            purpose="Зачисление средств по операциям эквайринга. Мерчант №711000421047.",
        ),
        settings=settings,
    )

    assert result.scenario == "card_payment_sber_acquiring"


def test_classifier_detects_entrepreneur_personal_funds_outgoing(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(
            direction="outgoing",
            document_kind="ПлатежноеПоручениеИсходящее",
            payer_account=OWN_ACCOUNT,
            recipient_name='ОАО "Сбербанк России"',
            recipient_bank="ПАО Сбербанк",
            recipient_account="40802810860100063566",
            purpose=(
                "Перевод на счет Ахмедова Эльдара Айдыновича. "
                "Личные средства предпринимателя. НДС не облагается."
            ),
        ),
        settings=settings,
    )

    assert result.scenario == "founder"
    assert result.operation_code == "ПрочееСписаниеБезналичныхДенежныхСредств"
    assert result.cash_flow_article_name == "Расчеты с учредителями"
    assert result.skip_auto_contract_fill is True


def test_classifier_detects_dry_acquiring_from_bank_payer(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(payer_name="Т-Банк", purpose="Перевод средств по договору 123"),
        settings=settings,
    )

    assert result.scenario == "card_payment"
    assert result.confidence >= 0.9


def test_classifier_detects_internal_transfers(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    outgoing = classify_line(
        _line(
            direction="outgoing",
            payer_account=OWN_ACCOUNT,
            recipient_account=SECOND_OWN_ACCOUNT,
        ),
        settings=settings,
    )
    incoming = classify_line(
        _line(
            direction="incoming",
            payer_account=SECOND_OWN_ACCOUNT,
            recipient_account=OWN_ACCOUNT,
        ),
        settings=settings,
    )

    assert outgoing.scenario == "internal_transfer_out"
    assert outgoing.should_load is True
    assert incoming.scenario == "internal_transfer_in"
    assert incoming.should_load is False


def test_classifier_detects_bank_return(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(payer_name="ВТБ Банк", purpose="Возврат неисполненного платежа"),
        settings=settings,
    )

    assert result.scenario == "bank_return"
    assert result.cash_flow_article_name == "Возвраты банка / неисполненные платежи"


def test_classifier_detects_salary_registry(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(
            direction="outgoing",
            payer_account=OWN_ACCOUNT,
            recipient_account="40802810000000000004",
            recipient_name="ВТБ",
            recipient_bank="ВТБ Банк",
            purpose="Реестр для зачисления на банковские счета сотрудников",
            payment_purpose_code="1",
        ),
        settings=settings,
    )

    assert result.scenario == "salary_registry"
    assert result.operation_code == "осиВыплатаЗаработнойПлаты"
    assert result.physical_person_name == "Зарплата"


def test_classifier_detects_sber_salary_registry_text(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_line(
        _line(
            direction="outgoing",
            payer_account=OWN_ACCOUNT,
            recipient_account="",
            recipient_name='ОАО "Сбербанк России"',
            recipient_bank="ПАО Сбербанк",
            purpose="Заработная плата по реестру №29 от 05.05.2026",
            payment_purpose_code="1",
            priority="3",
        ),
        settings=settings,
    )

    assert result.scenario == "salary_registry"
    assert result.operation_code == "осиВыплатаЗаработнойПлаты"


def test_classifier_detects_customer_payment(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_request(
        BankPaymentClassifyRequest(
            direction="incoming",
            amount=100,
            payer_name="ООО Ромашка",
            recipient_account=OWN_ACCOUNT,
            purpose="Оплата по счету",
        ),
        settings=settings,
    )

    assert result.scenario == "customer_payment"
    assert result.should_load is True


def test_classifier_leaves_uncertain_payment_for_manual_review(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    result = classify_request(
        BankPaymentClassifyRequest(
            direction="incoming",
            amount=100,
            payer_name="ООО Ромашка",
            purpose="Оплата по счету",
        ),
        settings=settings,
    )

    assert result.scenario == "manual_review"
    assert result.should_load is False


def test_exporter_writes_full_legacy_1c_file_without_mm_fields(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)
    ready = _line(purpose="СБП оплата по QR")
    ready_result = classify_line(ready, settings=settings)
    ready.scenario = ready_result.scenario
    ready.operation_code = ready_result.operation_code or ""
    ready.skip_auto_contract_fill = ready_result.skip_auto_contract_fill
    ready.skip_payment_fill = ready_result.skip_payment_fill
    manual = _line(number="2", purpose="Непонятный платеж", recipient_account="")
    manual.scenario = "manual_review"

    exported = export_1c_client_bank_exchange([ready, manual])

    assert "Номер=2" in exported
    assert "СценарийИмпортаMM=" not in exported
    assert "Код=MMOP:ПоступлениеОплатыПоПлатежнымКартам" in exported
    assert "ВидОперацииMM=" not in exported
    assert "ВидОперации=" not in exported
    assert "НеЗаполнятьДоговорMM=" not in exported
    assert "НеЗаполнятьОплатуMM=" not in exported


def test_sber_raw_row_maps_to_normalized_line(monkeypatch, tmp_path) -> None:
    settings = _configure(monkeypatch, tmp_path)

    line = line_from_sber_raw_row(
        {
            "transaction_key": "tx-1",
            "account_number": OWN_ACCOUNT,
            "statement_date": "2026-05-08",
            "operation_date": "2026-05-08",
            "direction": "credit",
            "amount": Decimal("4839.52"),
            "counterparty_name": "ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК",
            "counterparty_inn": "7707083893",
            "payment_purpose": "Зачисление средств по операциям эквайринга. Мерчант №711000421047.",
            "bank_doc_number": "684443",
            "is_acquiring_candidate": True,
        },
        settings=settings,
    )

    assert line.direction == "incoming"
    assert line.recipient_account == OWN_ACCOUNT
    assert line.recipient_name == "ИП Ахмедов Эльдар Айдынович"
    assert line.recipient_inn == "070111355885"
    assert line.recipient_kpp == "0"
    assert line.recipient_bic == "040702615"
    assert line.recipient_correspondent_account == "30101810907020000615"
    assert line.recipient_account_is_own is True
    assert line.source_type == "sber_api_raw"
    assert line.acquiring_merchant_id == "711000421047"


def test_sber_salary_registry_sets_bank_requisites_and_payment_code(
    monkeypatch,
    tmp_path,
) -> None:
    settings = _configure(monkeypatch, tmp_path)

    line = line_from_sber_raw_row(
        {
            "transaction_key": "salary-1",
            "account_number": OWN_ACCOUNT,
            "statement_date": "2026-05-05",
            "operation_date": "2026-05-05",
            "direction": "debit",
            "amount": Decimal("-175576.00"),
            "counterparty_name": "ПАО Сбербанк",
            "counterparty_inn": "7707083893",
            "payment_purpose": "{VO70060} Заработная плата по реестру №30 от 05.05.2026",
            "bank_doc_number": "692418",
        },
        settings=settings,
    )

    assert line.direction == "outgoing"
    assert line.recipient_account == ""
    assert line.recipient_kpp == "773601001"
    assert line.recipient_bic == "044525225"
    assert line.recipient_correspondent_account == "30101810400000000225"
    assert line.payment_purpose_code == "1"
    assert line.priority == "3"


def test_fetch_sber_raw_statement_rows_filters_dates_and_accounts() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE raw_sber_statement_transaction (
                    transaction_key text,
                    operation_id text,
                    account_number text,
                    statement_date date,
                    operation_datetime text,
                    operation_date date,
                    document_date date,
                    value_date date,
                    direction text,
                    amount numeric,
                    amount_rub numeric,
                    bank_doc_number text,
                    operation_code text,
                    counterparty_account text,
                    counterparty_name text,
                    counterparty_inn text,
                    payment_purpose text,
                    is_acquiring_candidate boolean,
                    acquiring_merchant_id text,
                    source_payload_hash text
                )
                """))
        conn.execute(
            text("""
                INSERT INTO raw_sber_statement_transaction (
                    transaction_key, account_number, statement_date, operation_datetime,
                    operation_date, direction, amount, counterparty_name, payment_purpose
                ) VALUES
                ('tx-1', :own_account, '2026-05-08', '2026-05-08 01:20:40',
                 '2026-05-08', 'credit', 100.00, 'Сбер', 'эквайринг'),
                ('tx-2', '40702810000000009999', '2026-05-08', '2026-05-08 01:21:40',
                 '2026-05-08', 'credit', 200.00, 'Сбер', 'эквайринг')
                """),
            {"own_account": OWN_ACCOUNT},
        )

    rows = fetch_sber_raw_statement_rows(
        engine,
        schema="main",
        date_from=date(2026, 5, 8),
        date_to=date(2026, 5, 8),
        account_numbers=[OWN_ACCOUNT],
    )

    assert [row["transaction_key"] for row in rows] == ["tx-1"]


def test_export_sber_raw_statement_creates_only_high_confidence_file(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BANK_PAYMENTS_SOURCE_SCHEMA", "main")
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE raw_sber_statement_transaction (
                    transaction_key text,
                    operation_id text,
                    account_number text,
                    statement_date date,
                    operation_datetime text,
                    operation_date date,
                    document_date date,
                    value_date date,
                    direction text,
                    amount numeric,
                    amount_rub numeric,
                    bank_doc_number text,
                    operation_code text,
                    counterparty_account text,
                    counterparty_name text,
                    counterparty_inn text,
                    payment_purpose text,
                    is_acquiring_candidate boolean,
                    acquiring_merchant_id text,
                    source_payload_hash text
                )
                """))
        conn.execute(
            text("""
                INSERT INTO raw_sber_statement_transaction (
                    transaction_key, account_number, statement_date, operation_datetime,
                    operation_date, direction, amount, bank_doc_number,
                    counterparty_name, counterparty_inn, payment_purpose, is_acquiring_candidate
                ) VALUES
                ('tx-ready', :own_account, '2026-05-08', '2026-05-08 01:20:40',
                 '2026-05-08', 'credit', 4839.52, '684443',
                 'ЮГО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК', '7707083893',
                 'Зачисление средств по операциям эквайринга. Мерчант №711000421047.', 1),
                ('tx-manual', :own_account, '2026-05-08', '2026-05-08 02:20:40',
                 '2026-05-08', 'credit', 100.00, '684444',
                 '', '', 'непонятная операция', 0)
                """),
            {"own_account": OWN_ACCOUNT},
        )

    response = export_sber_raw_statement(
        date_from=date(2026, 5, 8),
        date_to=date(2026, 5, 8),
        account_numbers=[OWN_ACCOUNT],
        settings=settings,
        engine=engine,
    )

    assert response.status == "ready"
    assert response.detected_format == "sber_api_raw"
    assert response.counts.source_lines == 2
    assert response.counts.exported == 2
    assert response.download_url
