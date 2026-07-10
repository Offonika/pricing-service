from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.bitrix_order_formation import (
    create_or_update_bitrix_card,
    reflect_classifications_from_bitrix,
)
from app.services.exporters.ut103_exchange import (
    load_ut103_env_file,
    resolve_ut103_exchange_root,
)
from app.services.exporters.ut103_nomenclature_properties import (
    list_property_update_exchange_results,
)
from app.services.exporters.ut103_procurement_orders import (
    list_procurement_supplier_order_exchange_results,
)
from app.services.procurement_order_formation import (
    record_order_exchange_result,
    record_property_update_exchange_result,
)
from app.services.procurement_order_formation_workspace import (
    record_lifecycle_property_update_exchange_result,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read 1C order/property results and optionally verify CommerceML readback."
    )
    parser.add_argument("--exchange-root")
    parser.add_argument("--commerce-ml-readback", action="store_true")
    parser.add_argument(
        "--sync-bitrix-cards",
        action="store_true",
        help="Apply resulting stage/connector fields in existing Bitrix cards.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_ut103_env_file()
    args = parse_args()
    settings = get_settings()
    exchange_root = resolve_ut103_exchange_root(args.exchange_root)
    engine = create_engine(settings.database_url)
    order_ids: list[int] = []
    property_ids: list[int] = []
    transition_ids: list[int] = []
    with Session(engine) as db:
        for result in list_procurement_supplier_order_exchange_results(exchange_root):
            order = record_order_exchange_result(db, result)
            if order is not None:
                order_ids.append(order.id)
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
        synced_bitrix = 0
        if args.sync_bitrix_cards:
            for order_id in sorted(set(order_ids)):
                create_or_update_bitrix_card(
                    db,
                    order_id,
                    apply=True,
                    settings=settings,
                )
                synced_bitrix += 1
    payload = {
        "order_results_applied": len(set(order_ids)),
        "property_results_applied": len(set(property_ids)),
        "lifecycle_transition_results_applied": len(set(transition_ids)),
        "commerce_ml_readback": readback,
        "bitrix_cards_synced": synced_bitrix,
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
