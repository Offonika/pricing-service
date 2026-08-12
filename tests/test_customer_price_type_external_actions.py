from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.domains.customer_price_types import (
    ContractFact,
    CustomerPriceTypeAccessScope,
    CustomerPriceTypeFacts,
)
from app.models import Base
from app.models.customer_price_type import (
    CustomerPriceTypeCase,
    CustomerPriceTypeExternalAction,
    CustomerPriceTypeOneCContractAction,
    CustomerPriceTypeReview,
    CustomerPriceTypeSnapshot,
)
from app.services.customer_price_type_external_actions import (
    run_customer_price_type_external_actions_once,
    sync_customer_price_type_bitrix_completions_once,
)
from app.services.customer_price_type_reviews import CustomerPriceTypeReviewService
from app.services.customer_price_types import CustomerPriceTypeRunService

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def _ref(value: int) -> str:
    return f"0x{value:032x}"


def _facts(value: int, *, quality: bool = False) -> CustomerPriceTypeFacts:
    return CustomerPriceTypeFacts(
        counterparty_ref=_ref(value),
        counterparty_code=f"РБ{value:06d}",
        counterparty_name=f"Клиент {value}",
        snapshot_month=date(2026, 7, 1),
        contracts=(
            ContractFact(
                contract_ref=_ref(1000 + value),
                contract_name="Основной рабочий договор",
                price_type_name="2.Бронзовый",
                sale_document_count_12m=3,
                sales_amount_12m=Decimal("50000"),
                is_working=True,
            ),
        ),
        monthly_sales={
            "2026-05": Decimal("120000"),
            "2026-06": Decimal("120000"),
            "2026-07": Decimal("120000"),
        },
        source_statuses={
            "contracts": "ready",
            "sales_history": "ready",
            "ledger_reconciliation": "ready",
            "master_data": "ready",
        },
        owner_ref=_ref(2000 + value),
        owner_name="Менеджер",
        department_ref="department-1",
        department_name="Розничная сеть",
        history_coverage_months=12,
        direct_onec_total_3m=Decimal("360000"),
        ledger_total_3m=Decimal("360000"),
        economics_status="ok",
        economics={"status": "ok"},
        return_review_type="quality" if quality else None,
    )


def _factory(tmp_path: Path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _save_live_price_review(factory) -> tuple[int, str, str]:
    CustomerPriceTypeRunService(factory).execute(
        [_facts(10)], source_statuses={"contracts": "ready"}
    )
    with factory() as db:
        snapshot_id = db.scalar(select(CustomerPriceTypeSnapshot.id))
    settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_onec_actions_enabled=True,
        customer_price_type_onec_enabled_directions=["bronze_to_silver"],
    )
    with factory() as db:
        snapshot = db.get(CustomerPriceTypeSnapshot, snapshot_id)
        CustomerPriceTypeReviewService(db, settings=settings).save(
            snapshot_id=snapshot_id,
            review_kind="price_type",
            result="confirm",
            corrected_value=None,
            comment=None,
            expected_version=0,
            snapshot_hash=snapshot.snapshot_hash,
            access=CustomerPriceTypeAccessScope(
                actor="arsen", role="network_head", can_view_money=True
            ),
        )
        action = db.scalar(select(CustomerPriceTypeExternalAction))
        line = db.scalar(select(CustomerPriceTypeOneCContractAction))
        return action.id, action.idempotency_key, line.idempotency_key


def _write_result(
    root: Path,
    *,
    action_id: int,
    review_id: int,
    line_key: str,
    contract_ref: str,
    phase: str,
    result: str,
    readback: str,
    all_ready: bool,
    atomically: bool,
) -> Path:
    message_id = f"customer-price-type-review-{review_id}-{phase}"
    result_dir = root / "from_1c" / "new"
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / f"customer_price_types_{message_id}.result.xml"
    path.write_text(
        f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <MessageId>{message_id}</MessageId>
  <Schema>customer_price_type_updates.v2</Schema>
  <Status>success</Status><ProcessedAt>2026-08-12T12:00:00+03:00</ProcessedAt>
  <Loaded>1</Loaded><Failed>0</Failed><Errors></Errors>
  <AllReady>{str(all_ready).lower()}</AllReady>
  <AppliedAtomically>{str(atomically).lower()}</AppliedAtomically>
  <RequiresTechnicalReview>false</RequiresTechnicalReview>
  <ItemResults><ItemResult>
    <DecisionId>{review_id}</DecisionId><IdempotencyKey>{line_key}</IdempotencyKey>
    <ContractRef>{contract_ref}</ContractRef><Result>{result}</Result><Message>Готово</Message>
    <ReadbackPriceType>{readback}</ReadbackPriceType>
  </ItemResult></ItemResults>
</ExchangeResult>""",
        encoding="windows-1251",
    )
    return path


def test_onec_worker_preflights_applies_and_reads_back_once(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path, "onec-worker.db")
    action_id, _, line_key = _save_live_price_review(factory)
    settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_onec_actions_enabled=True,
        customer_price_type_onec_enabled_directions=["bronze_to_silver"],
    )
    exchange_root = tmp_path / "exchange"
    with factory() as db:
        first = run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        action = db.get(CustomerPriceTypeExternalAction, action_id)
        review = db.get(CustomerPriceTypeReview, action.review_id)
        line = db.scalar(
            select(CustomerPriceTypeOneCContractAction).where(
                CustomerPriceTypeOneCContractAction.external_action_id == action_id
            )
        )
        assert first["advanced"] == 1
        assert action.status == "preflight"
        assert (exchange_root / "to_1c" / "new").glob("*.ready.xml")
        review_id = review.id
        contract_ref = line.contract_ref

    _write_result(
        exchange_root,
        action_id=action_id,
        review_id=review_id,
        line_key=line_key,
        contract_ref=contract_ref,
        phase="dry-run",
        result="ready",
        readback="2.Бронзовый",
        all_ready=True,
        atomically=False,
    )
    with factory() as db:
        second = run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        assert second["advanced"] == 1
        assert db.get(CustomerPriceTypeExternalAction, action_id).status == "ready_to_apply"
    with factory() as db:
        third = run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        assert third["advanced"] == 1
        assert db.get(CustomerPriceTypeExternalAction, action_id).status == "applying"

    _write_result(
        exchange_root,
        action_id=action_id,
        review_id=review_id,
        line_key=line_key,
        contract_ref=contract_ref,
        phase="apply",
        result="applied",
        readback="3.Серебряный",
        all_ready=True,
        atomically=True,
    )
    with factory() as db:
        fourth = run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        action = db.get(CustomerPriceTypeExternalAction, action_id)
        line = db.scalar(
            select(CustomerPriceTypeOneCContractAction).where(
                CustomerPriceTypeOneCContractAction.external_action_id == action_id
            )
        )
        case = db.get(CustomerPriceTypeCase, action.case_id)
        assert fourth["applied"] == 1
        assert action.status == "applied"
        assert line.status == "applied"
        assert line.actual_price_type == "3.Серебряный"
        assert case.stage == "CLOSED_CHANGED"
        assert case.onec_readback_status == "confirmed"
        assert (
            run_customer_price_type_external_actions_once(
                db, exchange_root=exchange_root, settings=settings, now=NOW
            )["scanned"]
            == 0
        )
    engine.dispose()


def test_partial_onec_result_stops_automatic_retries(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path, "onec-partial.db")
    action_id, _, line_key = _save_live_price_review(factory)
    settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_onec_actions_enabled=True,
        customer_price_type_onec_enabled_directions=["bronze_to_silver"],
    )
    exchange_root = tmp_path / "exchange"
    with factory() as db:
        run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        action = db.get(CustomerPriceTypeExternalAction, action_id)
        line = db.scalar(
            select(CustomerPriceTypeOneCContractAction).where(
                CustomerPriceTypeOneCContractAction.external_action_id == action_id
            )
        )
        review_id = action.review_id
        contract_ref = line.contract_ref
    result = _write_result(
        exchange_root,
        action_id=action_id,
        review_id=review_id,
        line_key=line_key,
        contract_ref=contract_ref,
        phase="dry-run",
        result="blocked",
        readback="2.Бронзовый",
        all_ready=False,
        atomically=False,
    )
    with factory() as db:
        summary = run_customer_price_type_external_actions_once(
            db, exchange_root=exchange_root, settings=settings, now=NOW
        )
        action = db.get(CustomerPriceTypeExternalAction, action_id)
        case = db.get(CustomerPriceTypeCase, action.case_id)
        assert summary["technical_review"] == 1
        assert action.status == "technical_review"
        assert case.stage == "READY_FOR_1C"
        assert not result.exists()
        assert (
            run_customer_price_type_external_actions_once(
                db, exchange_root=exchange_root, settings=settings, now=NOW
            )["scanned"]
            == 0
        )
    engine.dispose()


class FakeBitrixGateway:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.calls = 0

    def upsert_case(self, *, idempotency_key: str, fields: dict) -> str:
        self.calls += 1
        item = self.items.setdefault(idempotency_key, {"id": str(len(self.items) + 1)})
        item.update(fields)
        return str(item["id"])

    def read_case(self, *, item_id: str) -> dict:
        return next(item for item in self.items.values() if str(item["id"]) == str(item_id))


def test_bitrix_case_is_idempotent_and_has_responsible_stage_and_sla(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path, "bitrix-worker.db")
    CustomerPriceTypeRunService(factory).execute(
        [_facts(20, quality=True)], source_statuses={"contracts": "ready"}
    )
    save_settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_bitrix_case_actions_enabled=True,
    )
    with factory() as db:
        snapshot = db.scalar(select(CustomerPriceTypeSnapshot))
        CustomerPriceTypeReviewService(db, settings=save_settings).save(
            snapshot_id=snapshot.id,
            review_kind="client_action",
            result="confirm",
            corrected_value=None,
            comment=None,
            expected_version=0,
            snapshot_hash=snapshot.snapshot_hash,
            access=CustomerPriceTypeAccessScope(
                actor="arsen", role="network_head", can_view_money=True
            ),
        )
        action_id = db.scalar(select(CustomerPriceTypeExternalAction.id))
    worker_settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_bitrix_case_actions_enabled=True,
        customer_price_type_bitrix_category_id=77,
        customer_price_type_bitrix_stage_map={"quality": "DT1188_77:QUALITY_CHECK"},
        customer_price_type_bitrix_field_map={
            "stable_key": "stableKey",
            "counterparty_ref": "counterpartyRef",
            "snapshot_month": "snapshotMonth",
            "action": "clientAction",
        },
        customer_price_type_bitrix_quality_user_id=42,
    )
    gateway = FakeBitrixGateway()
    with factory() as db:
        first = run_customer_price_type_external_actions_once(
            db, settings=worker_settings, bitrix_gateway=gateway, now=NOW
        )
        action = db.get(CustomerPriceTypeExternalAction, action_id)
        case = db.get(CustomerPriceTypeCase, action.case_id)
        assert first["applied"] == 1
        assert action.status == "applied"
        assert case.stage == "QUALITY_CHECK"
        assert len(gateway.items) == 1
        item = next(iter(gateway.items.values()))
        assert item["stageId"] == "DT1188_77:QUALITY_CHECK"
        assert item["assignedById"] == 42
        assert item["closedate"] == "2026-08-17T09:00:00"

        # Simulate a crash after the external upsert but before the local status
        # commit: the same client/month key updates the existing item.
        action.status = "pending"
        db.commit()
        second = run_customer_price_type_external_actions_once(
            db, settings=worker_settings, bitrix_gateway=gateway, now=NOW
        )
        assert second["applied"] == 1
        assert len(gateway.items) == 1
        assert gateway.calls == 2
    engine.dispose()


def test_bitrix_completion_requires_configured_terminal_readback(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path, "bitrix-completion-isolate.db")
    isolate_facts = _facts(22)
    isolate_facts = replace(
        isolate_facts,
        monthly_sales={
            "2026-05": Decimal("1"),
            "2026-06": Decimal("1"),
            "2026-07": Decimal("1"),
        },
        direct_onec_total_3m=Decimal("3"),
        ledger_total_3m=Decimal("3"),
    )
    CustomerPriceTypeRunService(factory).execute(
        [isolate_facts], source_statuses={"contracts": "ready"}
    )
    settings = Settings(
        _env_file=None,
        customer_price_type_external_actions_enabled=True,
        customer_price_type_bitrix_case_actions_enabled=True,
        customer_price_type_bitrix_category_id=77,
        customer_price_type_bitrix_stage_map={"isolate": "DT1188_77:ISOLATE_1M"},
        customer_price_type_bitrix_field_map={"stable_key": "stableKey"},
        customer_price_type_bitrix_internal_user_id=42,
        customer_price_type_bitrix_completed_stage_ids=["DT1188_77:CLOSED_KEEP"],
    )
    with factory() as db:
        snapshot = db.scalar(select(CustomerPriceTypeSnapshot))
        CustomerPriceTypeReviewService(db, settings=settings).save(
            snapshot_id=snapshot.id,
            review_kind="client_action",
            result="confirm",
            corrected_value=None,
            comment=None,
            expected_version=0,
            snapshot_hash=snapshot.snapshot_hash,
            access=CustomerPriceTypeAccessScope(
                actor="arsen", role="network_head", can_view_money=True
            ),
        )
    gateway = FakeBitrixGateway()
    with factory() as db:
        delivered = run_customer_price_type_external_actions_once(
            db, settings=settings, bitrix_gateway=gateway, now=NOW
        )
        assert delivered["applied"] == 1, delivered
        waiting = sync_customer_price_type_bitrix_completions_once(
            db, settings=settings, bitrix_gateway=gateway, now=NOW
        )
        assert waiting["waiting"] == 1
        case = db.scalar(select(CustomerPriceTypeCase))
        assert case.manager_action_completeness == {}
        item = next(iter(gateway.items.values()))
        item["stageId"] = "DT1188_77:CLOSED_KEEP"
        completed = sync_customer_price_type_bitrix_completions_once(
            db, settings=settings, bitrix_gateway=gateway, now=NOW
        )
        assert completed["completed"] == 1
        case = db.scalar(select(CustomerPriceTypeCase))
        assert case.manager_action_completeness["source"] == "bitrix_readback"
        assert case.manager_action_completeness["action"] == "isolate"
        assert case.stage == "CLOSED_KEEP"
    engine.dispose()
