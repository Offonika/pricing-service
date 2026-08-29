from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Base
from app.models.customer_settlement import (
    CustomerAccount,
    CustomerAccountSiteBinding,
    CustomerAccountSourceBinding,
    CustomerSettlementMappingRevision,
    CustomerSettlementPilotAccess,
)
from app.services.customer_settlement_source import (
    CustomerSettlementSourceError,
    ManualCustomerSettlementControl,
)
from app.services.customer_settlements import onec_ref_to_guid
from app.workers.customer_settlements import run_customer_settlement_mapping_sync
from tasks import import_customer_settlement_mappings as importer

ORG_REF = "0x" + "a" * 32
CP_REF = "0x" + "1" * 32
ORG_GUID = onec_ref_to_guid(ORG_REF)
CP_GUID = onec_ref_to_guid(CP_REF)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        customer_settlements_organization_ref=ORG_REF,
        customer_settlements_organization_guid=ORG_GUID,
        customer_settlements_counterparty_inn_field="_Fld611",
        customer_settlements_correlation_salt="synthetic-audit-salt",
    )


def _row(
    *,
    expected_name: str = "Пилот",
    expected_inn: str = "1234567890",
) -> importer.ManualMappingRow:
    return importer.ManualMappingRow(
        site_user_id="101",
        counterparty_guid=CP_GUID,
        organization_guid=ORG_GUID,
        source_system="ut103",
        expected_code="РБ000001",
        expected_name=expected_name,
        expected_inn=expected_inn,
    )


def _control(
    *,
    name: str = "Пилот",
    inn: str = "1234567890",
    currencies: tuple[str, ...] = ("643",),
) -> ManualCustomerSettlementControl:
    return ManualCustomerSettlementControl(
        counterparty_ref=CP_REF,
        counterparty_guid=CP_GUID,
        counterparty_code="РБ000001",
        counterparty_name=name,
        counterparty_inn=inn,
        active_contract_currency_codes=currencies,
    )


def _engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_manual_import_dry_run_rolls_back_and_apply_materializes_account(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        importer,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (_control(),),
    )
    engine = _engine()
    try:
        with Session(engine) as session:
            dry_run = importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=False,
                approved_by=None,
            )
            assert dry_run["status"] == "validated"
            assert dry_run["row_count"] == 1
            assert dry_run["inn_control_count"] == 1
            assert len(str(dry_run["input_hash"])) == 64
            assert session.scalar(select(func.count()).select_from(CustomerAccount)) == 0

        with Session(engine) as session:
            applied = importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=True,
                approved_by="finance-owner",
                approved_input_hash=str(dry_run["input_hash"]),
                approved_controls_hash=str(dry_run["controls_hash"]),
            )
            assert applied["status"] == "applied"
            assert len(str(applied["approval_hash"])) == 64
            assert applied["approval_hash"] != importer._hash_payload("finance-owner")
            assert session.scalar(select(func.count()).select_from(CustomerAccount)) == 1
            assert session.scalar(select(func.count()).select_from(CustomerAccountSiteBinding)) == 1
            assert (
                session.scalar(select(func.count()).select_from(CustomerAccountSourceBinding)) == 1
            )
            active = session.scalar(
                select(CustomerSettlementMappingRevision).where(
                    CustomerSettlementMappingRevision.status == "active"
                )
            )
            assert active is not None
            assert active.source_name == "manual_confirmed_pilot"
            assert (
                session.scalar(select(func.count()).select_from(CustomerSettlementPilotAccess)) == 0
            )

        settings = _settings()
        settings.customer_settlements_shadow_enabled = True
        monkeypatch.setattr(
            "app.workers.customer_settlements.get_application_session_factory",
            lambda: lambda: Session(engine),
        )
        monkeypatch.setattr(
            "app.workers.customer_settlements.assert_expected_application_database",
            lambda *_args, **_kwargs: None,
        )
        sync_result = run_customer_settlement_mapping_sync(settings=settings)
        assert sync_result["status"] == "unchanged"
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(CustomerSettlementMappingRevision))
                == 1
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize(
    ("input_hash", "controls_hash"),
    [
        (None, None),
        ("0" * 64, "0" * 64),
    ],
)
def test_manual_import_apply_requires_matching_dry_run_hashes(
    monkeypatch,
    input_hash: str | None,
    controls_hash: str | None,
) -> None:
    monkeypatch.setattr(
        importer,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (_control(),),
    )
    engine = _engine()
    try:
        with Session(engine) as session, pytest.raises(importer.ManualMappingImportError) as exc:
            importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=True,
                approved_by="finance-owner",
                approved_input_hash=input_hash,
                approved_controls_hash=controls_hash,
            )
        expected_code = (
            "approved_dry_run_hashes_required"
            if input_hash is None
            else "approved_dry_run_hash_mismatch"
        )
        assert exc.value.code == expected_code
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CustomerAccount)) == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_manual_import_apply_requires_salted_audit_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importer,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (_control(),),
    )
    settings = _settings()
    settings.customer_settlements_correlation_salt = None
    engine = _engine()
    try:
        with (
            Session(engine) as session,
            pytest.raises(
                importer.ManualMappingImportError,
                match="correlation_salt_not_configured",
            ),
        ):
            importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=settings,
                apply=True,
                approved_by="finance-owner",
                approved_input_hash="a" * 64,
                approved_controls_hash="b" * 64,
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_manual_import_reports_unknown_commit_state_after_committed_connection_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importer,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (_control(),),
    )
    engine = _engine()

    class CommitUnknownSession(Session):
        def commit(self) -> None:
            super().commit()
            raise RuntimeError("synthetic connection loss after commit")

    try:
        with Session(engine) as session:
            dry_run = importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=False,
                approved_by=None,
            )
        with (
            CommitUnknownSession(engine) as session,
            pytest.raises(
                importer.ManualMappingImportError,
                match="mapping_commit_state_unknown",
            ),
        ):
            importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=True,
                approved_by="finance-owner",
                approved_input_hash=str(dry_run["input_hash"]),
                approved_controls_hash=str(dry_run["controls_hash"]),
            )
        with Session(engine) as session:
            assert (
                session.scalar(select(func.count()).select_from(CustomerSettlementMappingRevision))
                == 1
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize(
    ("row", "control", "error_code"),
    [
        (_row(expected_name="Ожидалось"), _control(name="Фактически"), "identity_control_mismatch"),
        (_row(), _control(currencies=("643", "840")), "counterparty_has_non_rub_contract"),
    ],
)
def test_manual_import_rejects_control_mismatch_and_non_rub(
    monkeypatch,
    row: importer.ManualMappingRow,
    control: ManualCustomerSettlementControl,
    error_code: str,
) -> None:
    monkeypatch.setattr(
        importer,
        "fetch_manual_customer_settlement_controls",
        lambda *_args, **_kwargs: (control,),
    )
    engine = _engine()
    try:
        with Session(engine) as session, pytest.raises(importer.ManualMappingImportError) as exc:
            importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(row,),
                settings=_settings(),
                apply=False,
                approved_by=None,
            )
        assert exc.value.code == error_code
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(CustomerAccount)) == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_manual_import_allows_missing_inn_but_checks_it_when_provided(
    monkeypatch,
) -> None:
    engine = _engine()
    try:
        monkeypatch.setattr(
            importer,
            "fetch_manual_customer_settlement_controls",
            lambda *_args, **_kwargs: (_control(inn=""),),
        )
        with Session(engine) as session:
            result = importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(expected_inn=""),),
                settings=_settings(),
                apply=False,
                approved_by=None,
            )
        assert result["status"] == "validated"
        assert result["inn_control_count"] == 0

        monkeypatch.setattr(
            importer,
            "fetch_manual_customer_settlement_controls",
            lambda *_args, **_kwargs: (_control(inn="9999999999"),),
        )
        with Session(engine) as session:
            applied = importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(expected_inn=""),),
                settings=_settings(),
                apply=True,
                approved_by="finance-owner",
                approved_input_hash=str(result["input_hash"]),
                approved_controls_hash=str(result["controls_hash"]),
            )
        assert applied["status"] == "applied"

        monkeypatch.setattr(
            importer,
            "fetch_manual_customer_settlement_controls",
            lambda *_args, **_kwargs: (_control(inn="9999999999"),),
        )
        with Session(engine) as session, pytest.raises(importer.ManualMappingImportError) as exc:
            importer.import_manual_customer_settlement_mappings(
                session,
                object(),
                rows=(_row(),),
                settings=_settings(),
                apply=False,
                approved_by=None,
            )
        assert exc.value.code == "identity_control_mismatch"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_manual_mapping_csv_has_exact_contract_and_ten_row_limit(tmp_path: Path) -> None:
    csv_path = tmp_path / "pilot.csv"
    csv_path.write_text(
        ",".join(importer.CSV_FIELDS)
        + "\n"
        + f"101,{CP_GUID},{ORG_GUID},ut103,РБ000001,Пилот,1234567890\n",
        encoding="utf-8",
    )
    assert importer.load_manual_mapping_csv(csv_path) == (_row(),)

    csv_path.write_text(
        ",".join(importer.CSV_FIELDS) + "\n" + f"101,{CP_GUID},{ORG_GUID},ut103,РБ000001,Пилот,\n",
        encoding="utf-8",
    )
    assert importer.load_manual_mapping_csv(csv_path) == (_row(expected_inn=""),)

    csv_path.write_text(
        ",".join(importer.CSV_FIELDS)
        + "\n"
        + f"101,{CP_GUID},{ORG_GUID},ut103,РБ000001,Пилот,12345\n",
        encoding="utf-8",
    )
    with pytest.raises(importer.ManualMappingImportError) as exc:
        importer.load_manual_mapping_csv(csv_path)
    assert exc.value.code == "invalid_identity_controls"

    csv_path.write_text(
        ",".join(importer.CSV_FIELDS)
        + "\n"
        + "".join(
            f"{100 + index},{CP_GUID},{ORG_GUID},ut103,РБ000001,Пилот,1234567890\n"
            for index in range(11)
        ),
        encoding="utf-8",
    )
    with pytest.raises(importer.ManualMappingImportError) as exc:
        importer.load_manual_mapping_csv(csv_path)
    assert exc.value.code == "manual_mapping_batch_limit_exceeded"


def test_manual_import_cli_masks_source_driver_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        importer,
        "get_settings",
        lambda: Settings(_env_file=None, onec_database_url="mssql+pyodbc://synthetic"),
    )
    monkeypatch.setattr(importer, "load_manual_mapping_csv", lambda path: (_row(),))

    def fail_engine(*args, **kwargs):
        raise CustomerSettlementSourceError("customer_settlement_query_timeout")

    monkeypatch.setattr(importer, "build_onec_engine", fail_engine)

    assert importer.main([str(tmp_path / "private-pilot.csv")]) == 2
    output = capsys.readouterr().out
    assert "customer_settlement_query_timeout" in output
    assert "private-pilot.csv" not in output
