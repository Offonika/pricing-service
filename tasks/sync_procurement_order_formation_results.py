from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db.engines import build_engine
from app.services.bitrix_order_formation import reflect_classifications_from_bitrix
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)
from app.services.exporters.ut103_nomenclature_properties import (
    list_property_update_exchange_results,
)
from app.services.exporters.ut103_procurement_orders import (
    ProcurementSupplierOrderExchangeResult,
    list_procurement_supplier_order_exchange_results,
)
from app.services.procurement_order_formation import (
    record_order_exchange_result,
    record_property_update_exchange_result,
)
from app.services.procurement_order_formation_workspace import (
    record_lifecycle_property_update_exchange_result,
)
from app.services.procurement_order_process_link import (
    record_procurement_process_sync_failure,
)
from tasks.sync_procurement_order_registry import (
    is_confirmed_process_sync_failure,
    sync_onec_order_process_by_ref,
)


def record_and_sync_order_result(
    db: Session,
    result: ProcurementSupplierOrderExchangeResult,
    *,
    settings: Settings,
) -> tuple[int | None, str | None]:
    order = record_order_exchange_result(db, result)
    if order is None:
        return None, None
    onec_ref = str(order.onec_document_ref or "").strip()
    if order.onec_status != "transmitted" or not onec_ref:
        return order.id, None
    try:
        outcome = sync_onec_order_process_by_ref(
            db,
            order_id=order.id,
            onec_ref=onec_ref,
            settings=settings,
        )
        state = str(outcome.get("state") or "pending")
        return order.id, state if state in {"linked", "pending", "broken"} else "pending"
    except Exception as exc:
        # The readback fact was committed before the immediate sync started.
        # Clear a failed SQLAlchemy transaction (for example, a uniqueness race)
        # so the deferred/broken outcome itself can always be audited.
        db.rollback()
        confirmed_broken = is_confirmed_process_sync_failure(exc)
        record_procurement_process_sync_failure(
            db,
            order.id,
            exc,
            confirmed_broken=confirmed_broken,
        )
        return order.id, "broken" if confirmed_broken else "pending"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read 1C order/property results and optionally verify CommerceML readback."
    )
    parser.add_argument("--exchange-root")
    parser.add_argument("--commerce-ml-readback", action="store_true")
    parser.add_argument(
        "--sync-bitrix-cards",
        action="store_true",
        help="Deprecated compatibility flag; canonical process 1056 sync now runs after readback.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_ut103_env_file()
    args = parse_args()
    settings = get_settings()
    exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    engine = build_engine(settings.database_url)
    order_ids: list[int] = []
    process_sync = {"linked": 0, "pending": 0, "broken": 0}
    property_ids: list[int] = []
    transition_ids: list[int] = []
    with Session(engine) as db:
        for result in list_procurement_supplier_order_exchange_results(exchange_root):
            order_id, state = record_and_sync_order_result(db, result, settings=settings)
            if order_id is not None:
                order_ids.append(order_id)
            if state is not None:
                process_sync[state] += 1
        for result in list_property_update_exchange_results(exchange_root):
            proposal = record_property_update_exchange_result(db, result)
            if proposal is not None:
                property_ids.append(proposal.id)
            transition_ids.extend(
                item.id for item in record_lifecycle_property_update_exchange_result(db, result)
            )
        readback = (
            reflect_classifications_from_bitrix(db, settings=settings)
            if args.commerce_ml_readback
            else {"reflected": 0, "pending": 0, "missing": 0}
        )
    payload = {
        "order_results_applied": len(set(order_ids)),
        "property_results_applied": len(set(property_ids)),
        "lifecycle_transition_results_applied": len(set(transition_ids)),
        "commerce_ml_readback": readback,
        "bitrix_cards_synced": process_sync["linked"],
        "process_sync": process_sync,
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=args.json,
            indent=None if args.json else 2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
