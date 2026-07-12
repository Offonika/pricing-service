from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import and_, exists, func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.infrastructure.db import build_onec_engine_from_settings
from app.models import (
    CompatibilityMappingDecision,
    Competitor,
    CompetitorItem,
    CompetitorItemCompatibility,
    CompetitorPrice,
    OneCSalesDailyKpi,
    PhoneModel,
    PhoneModelAlias,
    PriceRecommendation,
    PricingStrategyVersion,
    Product,
    ProductCompatibility,
    ProductPhoneModel,
    ProductStock,
    ReceivableBalanceSnapshot,
    ReceivableLedgerEvent,
)
from app.services.phone_model_canonicalization import screen_product_phone_compatibility
from app.services.receivables import (
    fetch_counterparty_refs_from_onec_group,
    list_receivable_cases,
    summarize_receivables_by_manager,
)

BUYERS_RUB_SNAPSHOT_TOTAL_ABS_THRESHOLD = Decimal("100000000.00")
BUYERS_CONTRACT_KIND_NAME = "С покупателем"
BUYERS_COUNTERPARTY_GROUP_NAME = "ПОКУПАТЕЛИ"
PCT_QUANT = Decimal("0.0001")


def _purchase_for_product(product: Product) -> float | None:
    stock: ProductStock | None = product.stock
    if stock and stock.purchase_price is not None:
        return float(stock.purchase_price)
    return None


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (numerator / denominator).quantize(PCT_QUANT)


def _is_month_end(value: date) -> bool:
    return (value + timedelta(days=1)).day == 1


def _snapshot_last_event_at(item: ReceivableBalanceSnapshot, snapshot_date: date) -> datetime:
    return max(
        value
        for value in (
            item.last_payment_at,
            item.last_sale_at,
            item.origin_document_date,
            datetime.combine(snapshot_date, time.min),
        )
        if value is not None
    )


def _build_buyers_rub_rows(
    items: Iterable[dict[str, object]],
    *,
    snapshot_date: date,
    limit: int | None = None,
) -> list[dict]:
    rows = sorted(
        items,
        key=lambda item: (
            abs(Decimal(str(item.get("current_balance") or 0))),
            str(item.get("counterparty_ref") or ""),
        ),
        reverse=True,
    )
    if limit is not None:
        rows = rows[:limit]
    rendered: list[dict] = []
    for item in rows:
        rendered.append(
            {
                "snapshot_date": snapshot_date,
                "counterparty_ref": item.get("counterparty_ref"),
                "counterparty_name": item.get("counterparty_name"),
                "contract_ref": None,
                "contract_name": None,
                "contract_kind_ref": None,
                "contract_kind_name": BUYERS_CONTRACT_KIND_NAME,
                "source_layer": "buyers_rub_snapshot",
                "current_balance": item.get("current_balance"),
                "event_count": int(item.get("event_count") or 1),
                "last_event_at": item.get("last_event_at"),
            }
        )
    return rendered


def _buyers_counterparty_refs_from_onec() -> tuple[str, ...] | None:
    settings = get_settings()
    if not settings.onec_database_url:
        return None

    engine = build_onec_engine_from_settings()
    try:
        refs = fetch_counterparty_refs_from_onec_group(
            engine,
            group_name=BUYERS_COUNTERPARTY_GROUP_NAME,
        )
    except Exception:
        return None
    finally:
        engine.dispose()

    return tuple(refs) if refs else None


def _filter_buyers_snapshot_items(
    items: Iterable[ReceivableBalanceSnapshot],
) -> list[ReceivableBalanceSnapshot]:
    buyer_refs = _buyers_counterparty_refs_from_onec()
    rendered = list(items)
    if not buyer_refs:
        return rendered
    buyer_ref_set = set(buyer_refs)
    return [item for item in rendered if item.counterparty_ref in buyer_ref_set]


def _buyers_snapshot_total(session: Session, *, snapshot_date: date) -> Decimal:
    items = _filter_buyers_snapshot_items(
        session.query(ReceivableBalanceSnapshot)
        .filter(
            ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
            ReceivableBalanceSnapshot.current_balance != 0,
        )
        .all()
    )
    total = sum((Decimal(str(item.current_balance)) for item in items), Decimal("0.00"))
    return total


def _latest_precise_buyers_snapshot_date(
    session: Session,
    *,
    snapshot_date: date,
) -> date | None:
    rows = (
        session.query(
            ReceivableBalanceSnapshot.snapshot_date.label("snapshot_date"),
            func.coalesce(func.sum(ReceivableBalanceSnapshot.current_balance), 0).label(
                "total_balance"
            ),
        )
        .filter(
            ReceivableBalanceSnapshot.snapshot_date <= snapshot_date,
            ReceivableBalanceSnapshot.current_balance != 0,
        )
        .group_by(ReceivableBalanceSnapshot.snapshot_date)
        .order_by(ReceivableBalanceSnapshot.snapshot_date.desc())
        .all()
    )
    for row in rows:
        row_date = row.snapshot_date
        total_balance = Decimal(str(row.total_balance or 0))
        if (
            _is_month_end(row_date)
            and abs(total_balance) <= BUYERS_RUB_SNAPSHOT_TOTAL_ABS_THRESHOLD
        ):
            return row_date
    return None


def _direct_buyers_rub_snapshot_rows(
    session: Session,
    *,
    snapshot_date: date,
    limit: int | None = None,
) -> list[dict]:
    query = (
        session.query(ReceivableBalanceSnapshot)
        .filter(
            ReceivableBalanceSnapshot.snapshot_date == snapshot_date,
            ReceivableBalanceSnapshot.current_balance != 0,
        )
        .order_by(
            func.abs(ReceivableBalanceSnapshot.current_balance).desc(),
            ReceivableBalanceSnapshot.counterparty_ref,
        )
    )
    items = _filter_buyers_snapshot_items(query.all())
    return _build_buyers_rub_rows(
        (
            {
                "counterparty_ref": item.counterparty_ref,
                "counterparty_name": item.counterparty_name,
                "current_balance": item.current_balance,
                "event_count": 1,
                "last_event_at": _snapshot_last_event_at(item, snapshot_date),
            }
            for item in items
        ),
        snapshot_date=snapshot_date,
        limit=limit,
    )


def _propagated_buyers_rub_snapshot_rows(
    session: Session,
    *,
    base_snapshot_date: date,
    snapshot_date: date,
    limit: int | None = None,
) -> list[dict]:
    base_items = (
        session.query(ReceivableBalanceSnapshot)
        .filter(
            ReceivableBalanceSnapshot.snapshot_date == base_snapshot_date,
            ReceivableBalanceSnapshot.current_balance != 0,
        )
        .all()
    )
    if not base_items:
        return []

    base_state: dict[str, dict[str, object]] = {}
    for item in base_items:
        base_state[item.counterparty_ref] = {
            "counterparty_ref": item.counterparty_ref,
            "counterparty_name": item.counterparty_name,
            "current_balance": item.current_balance,
            "event_count": 1,
            "last_event_at": _snapshot_last_event_at(item, base_snapshot_date),
        }

    if snapshot_date <= base_snapshot_date:
        return _build_buyers_rub_rows(
            base_state.values(),
            snapshot_date=snapshot_date,
            limit=limit,
        )

    base_end = datetime.combine(base_snapshot_date + timedelta(days=1), time.min)
    snapshot_end = datetime.combine(snapshot_date + timedelta(days=1), time.min)
    delta_events = (
        session.query(ReceivableLedgerEvent)
        .filter(
            ReceivableLedgerEvent.external_document_date >= base_end,
            ReceivableLedgerEvent.external_document_date < snapshot_end,
        )
        .order_by(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.external_document_date,
            ReceivableLedgerEvent.id,
        )
        .all()
    )

    delta_state: dict[str, dict[str, object]] = {}
    for item in delta_events:
        state = delta_state.setdefault(
            item.counterparty_ref,
            {
                "counterparty_ref": item.counterparty_ref,
                "counterparty_name": item.counterparty_name,
                "delta_balance": Decimal("0.00"),
                "event_count": 0,
                "last_event_at": None,
                "has_buyer_contract": False,
            },
        )
        state["delta_balance"] = Decimal(str(state["delta_balance"])) + item.amount_delta
        state["event_count"] = int(state["event_count"]) + 1
        if item.counterparty_name:
            state["counterparty_name"] = item.counterparty_name
        if state["last_event_at"] is None or item.external_document_date > state["last_event_at"]:
            state["last_event_at"] = item.external_document_date
        if (item.contract_kind_name or "").strip() == BUYERS_CONTRACT_KIND_NAME:
            state["has_buyer_contract"] = True

    relevant_refs = set(base_state)
    relevant_refs.update(
        ref for ref, item in delta_state.items() if bool(item.get("has_buyer_contract"))
    )

    propagated_items: list[dict[str, object]] = []
    for counterparty_ref in relevant_refs:
        base_item = base_state.get(counterparty_ref, {})
        delta_item = delta_state.get(counterparty_ref, {})
        current_balance = Decimal(str(base_item.get("current_balance") or 0)) + Decimal(
            str(delta_item.get("delta_balance") or 0)
        )
        if current_balance <= 0:
            continue
        base_last_event_at = base_item.get("last_event_at")
        delta_last_event_at = delta_item.get("last_event_at")
        last_event_candidates = [
            value
            for value in (
                base_last_event_at,
                delta_last_event_at,
                datetime.combine(snapshot_date, time.min),
            )
            if value is not None
        ]
        propagated_items.append(
            {
                "counterparty_ref": counterparty_ref,
                "counterparty_name": delta_item.get("counterparty_name")
                or base_item.get("counterparty_name"),
                "current_balance": current_balance,
                "event_count": int(base_item.get("event_count") or 0)
                + int(delta_item.get("event_count") or 0),
                "last_event_at": max(last_event_candidates),
            }
        )

    return _build_buyers_rub_rows(
        propagated_items,
        snapshot_date=snapshot_date,
        limit=limit,
    )


def get_products_dataset(session: Session, limit: int = 100) -> list[dict]:
    query = session.query(Product).order_by(Product.id)
    if limit:
        query = query.limit(limit)
    products = []
    for product in query.all():
        products.append(
            {
                "article": product.article,
                "fact_sku": product.fact_sku,
                "planned_sku": product.planned_sku,
                "sku_sync_status": product.sku_sync_status,
                "code_1c": product.code_1c,
                "info_system_code": product.info_system_code,
                "name": product.name,
                "brand": product.brand,
                "category": product.category,
                "is_active": product.is_active,
                "is_marked_for_deletion": product.is_marked_for_deletion,
                "stock_quantity": product.stock.quantity if product.stock else None,
                "purchase_price": _purchase_for_product(product),
            }
        )
    return products


def get_latest_recommendations(session: Session, limit: int = 100) -> list[dict]:
    subq = (
        session.query(
            PriceRecommendation.product_id,
            func.max(PriceRecommendation.created_at).label("max_created_at"),
        )
        .group_by(PriceRecommendation.product_id)
        .subquery()
    )
    query = (
        session.query(
            PriceRecommendation,
            Product,
            PricingStrategyVersion,
        )
        .join(Product, PriceRecommendation.product_id == Product.id)
        .outerjoin(
            PricingStrategyVersion,
            PriceRecommendation.strategy_version_id == PricingStrategyVersion.id,
        )
        .join(
            subq,
            (PriceRecommendation.product_id == subq.c.product_id)
            & (PriceRecommendation.created_at == subq.c.max_created_at),
        )
        .order_by(PriceRecommendation.created_at.desc())
    )
    if limit:
        query = query.limit(limit)

    rows: list[dict] = []
    for rec, product, strategy in query.all():
        rows.append(
            {
                "article": product.article,
                "recommended_price": rec.recommended_price,
                "floor_price": rec.floor_price,
                "competitor_min_price": rec.competitor_min_price,
                "min_margin_pct": rec.min_margin_pct,
                "strategy_name": strategy.name if strategy else None,
                "created_at": rec.created_at,
                "reasons": rec.reasons,
            }
        )
    return rows


def get_competitor_prices(session: Session, limit: int = 100) -> list[dict]:
    query = (
        session.query(CompetitorPrice, Product, Competitor)
        .join(Product, CompetitorPrice.product_id == Product.id)
        .join(Competitor, CompetitorPrice.competitor_id == Competitor.id)
        .order_by(CompetitorPrice.collected_at.desc())
    )
    if limit:
        query = query.limit(limit)

    rows: list[dict] = []
    for price, product, competitor in query.all():
        rows.append(
            {
                "article": product.article,
                "competitor": competitor.name,
                "price": price.price,
                "in_stock": price.in_stock,
                "collected_at": price.collected_at,
            }
        )
    return rows


def _phone_model_brand_label(model: PhoneModel) -> str:
    brand = getattr(model, "device_brand", None)
    if brand and brand.display_name:
        return brand.display_name
    return model.brand


def get_phone_model_links(
    session: Session, phone_model_id: int | None = None, limit: int = 100
) -> list[dict]:
    rows: list[dict] = []
    product_query = (
        session.query(ProductPhoneModel, PhoneModel, Product)
        .join(PhoneModel, ProductPhoneModel.phone_model_id == PhoneModel.id)
        .join(Product, ProductPhoneModel.product_id == Product.id)
        .order_by(ProductPhoneModel.id.desc())
    )
    if phone_model_id:
        product_query = product_query.filter(PhoneModel.id == phone_model_id)
    if limit:
        product_query = product_query.limit(limit)
    for _, model, product in product_query.all():
        rows.append(
            {
                "phone_model_id": model.id,
                "brand": _phone_model_brand_label(model),
                "model_name": model.model_name,
                "variant": model.variant,
                "product_article": product.article,
                "product_name": product.name,
            }
        )

    competitor_query = (
        session.query(CompetitorItemCompatibility, PhoneModel, CompetitorItem)
        .join(PhoneModel, CompetitorItemCompatibility.phone_model_id == PhoneModel.id)
        .join(CompetitorItem, CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
        .order_by(CompetitorItemCompatibility.id.desc())
    )
    if phone_model_id:
        competitor_query = competitor_query.filter(PhoneModel.id == phone_model_id)
    if limit:
        competitor_query = competitor_query.limit(limit)
    for _compat, model, item in competitor_query.all():
        rows.append(
            {
                "phone_model_id": model.id,
                "brand": _phone_model_brand_label(model),
                "model_name": model.model_name,
                "variant": model.variant,
                "competitor": item.competitor,
                "competitor_sku": item.external_id,
                "competitor_name": item.name,
            }
        )
    return rows


def _get_product_compatibility_review_rows(
    session: Session,
) -> list[tuple[ProductCompatibility, Product]]:
    return (
        session.query(ProductCompatibility, Product)
        .join(Product, ProductCompatibility.product_id == Product.id)
        .filter(
            ~exists().where(
                and_(
                    ProductPhoneModel.product_id == ProductCompatibility.product_id,
                    ProductPhoneModel.source == ProductCompatibility.source,
                )
            )
        )
        .order_by(ProductCompatibility.id.desc())
        .all()
    )


def get_unresolved_compatibilities(
    session: Session, limit: int = 100, ambiguous_only: bool = False
) -> list[dict]:
    rows: list[dict] = []

    if not ambiguous_only:
        for compat, product in _get_product_compatibility_review_rows(session):
            screen = screen_product_phone_compatibility(
                product,
                compat.value,
                source=compat.source,
            )
            if not screen.eligible_for_phone_canonicalization:
                continue
            rows.append(
                {
                    "source": compat.source,
                    "entity_type": "product",
                    "entity_id": product.id,
                    "raw_value": compat.value,
                    "notes": None,
                }
            )
            if limit and len(rows) >= limit:
                return rows

    competitor_query = (
        session.query(CompetitorItemCompatibility, CompetitorItem)
        .join(CompetitorItem, CompetitorItemCompatibility.competitor_item_id == CompetitorItem.id)
        .filter(CompetitorItemCompatibility.phone_model_id.is_(None))
        .order_by(CompetitorItemCompatibility.id.desc())
    )
    if ambiguous_only:
        competitor_query = competitor_query.filter(
            CompetitorItemCompatibility.notes.ilike("%ambiguous%")
        )
    if limit:
        competitor_query = competitor_query.limit(limit)

    for compat, item in competitor_query.all():
        brand_ref = getattr(compat, "device_brand_ref", None)
        rows.append(
            {
                "source": compat.source or "competitor_parser",
                "entity_type": "competitor_item",
                "entity_id": item.id,
                "raw_value": f"{compat.device_brand} {compat.device_model}".strip(),
                "brand": brand_ref.display_name if brand_ref else compat.device_brand,
                "model_name": compat.device_model,
                "variant": compat.device_variant,
                "notes": compat.notes,
            }
        )
    return rows


def get_canonicalization_summary(session: Session) -> dict[str, int]:
    unresolved_product = 0
    filtered_non_phone_product = 0
    for compat, product in _get_product_compatibility_review_rows(session):
        screen = screen_product_phone_compatibility(product, compat.value, source=compat.source)
        if screen.eligible_for_phone_canonicalization:
            unresolved_product += 1
        else:
            filtered_non_phone_product += 1
    unresolved_competitor = (
        session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.phone_model_id.is_(None))
        .count()
    )
    competitor_ambiguous = (
        session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.phone_model_id.is_(None))
        .filter(CompetitorItemCompatibility.notes.ilike("%ambiguous%"))
        .count()
    )
    blocked_noise = (
        session.query(PhoneModelAlias)
        .filter(PhoneModelAlias.decision_reason.ilike("blocked_%"))
        .count()
        + session.query(CompatibilityMappingDecision)
        .filter(CompatibilityMappingDecision.action == "block")
        .count()
    )
    valid_links = session.query(ProductPhoneModel).count() + (
        session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.phone_model_id.isnot(None))
        .count()
    )
    review_candidates = unresolved_product + unresolved_competitor + competitor_ambiguous

    return {
        "phone_models": session.query(PhoneModel).count(),
        "aliases": session.query(PhoneModelAlias).count(),
        "product_links": session.query(ProductPhoneModel).count(),
        "competitor_links": session.query(CompetitorItemCompatibility)
        .filter(CompetitorItemCompatibility.phone_model_id.isnot(None))
        .count(),
        "unresolved_product_compatibilities": unresolved_product,
        "filtered_non_phone_product_compatibilities": filtered_non_phone_product,
        "unresolved_competitor_compatibilities": unresolved_competitor,
        "valid_canonical_links": valid_links,
        "review_candidates": review_candidates,
        "blocked_noise": blocked_noise,
    }


def get_receivables_current(
    session: Session, *, snapshot_date, limit: int | None = None
) -> list[dict]:
    query = (
        session.query(ReceivableBalanceSnapshot)
        .filter(ReceivableBalanceSnapshot.snapshot_date == snapshot_date)
        .order_by(
            ReceivableBalanceSnapshot.current_balance.desc(),
            ReceivableBalanceSnapshot.counterparty_ref,
        )
    )
    if limit:
        query = query.limit(limit)

    rows: list[dict] = []
    for item in query.all():
        rows.append(
            {
                "snapshot_date": item.snapshot_date,
                "counterparty_ref": item.counterparty_ref,
                "counterparty_name": item.counterparty_name,
                "current_balance": item.current_balance,
                "aged_bucket": item.aged_bucket,
                "activity_segment": item.activity_segment,
                "is_overdue": item.is_overdue,
                "overdue_days": item.overdue_days,
                "due_date": item.due_date,
                "planned_payment_date": item.planned_payment_date,
                "credit_depth_days": item.credit_depth_days,
                "payment_term_source": item.payment_term_source,
                "shipment_ban": item.shipment_ban,
                "origin_document_ref": item.origin_document_ref,
                "origin_document_number": item.origin_document_number,
                "origin_document_date": item.origin_document_date,
                "origin_manager_ref": item.origin_manager_ref,
                "origin_manager_name": item.origin_manager_name,
                "current_manager_ref": item.current_manager_ref,
                "current_manager_name": item.current_manager_name,
                "last_sale_at": item.last_sale_at,
                "last_payment_at": item.last_payment_at,
            }
        )
    return rows


def get_receivable_cases_dataset(
    session: Session,
    *,
    snapshot_date,
    segment: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    items = list_receivable_cases(session, snapshot_date=snapshot_date, segment=segment)
    if limit:
        items = items[:limit]

    rows: list[dict] = []
    for item in items:
        rows.append(
            {
                "snapshot_date": item.snapshot_date,
                "segment": item.segment,
                "owner_type": item.owner_type,
                "recommendation": item.recommendation,
                "counterparty_ref": item.counterparty_ref,
                "counterparty_name": item.counterparty_name,
                "current_balance": item.current_balance,
                "aged_bucket": item.aged_bucket,
                "activity_segment": item.activity_segment,
                "is_overdue": item.is_overdue,
                "overdue_days": item.overdue_days,
                "due_date": item.due_date,
                "planned_payment_date": item.planned_payment_date,
                "credit_depth_days": item.credit_depth_days,
                "payment_term_source": item.payment_term_source,
                "shipment_ban": item.shipment_ban,
                "origin_document_ref": item.origin_document_ref,
                "origin_document_number": item.origin_document_number,
                "origin_document_date": item.origin_document_date,
                "origin_manager_ref": item.origin_manager_ref,
                "origin_manager_name": item.origin_manager_name,
                "current_manager_ref": item.current_manager_ref,
                "current_manager_name": item.current_manager_name,
            }
        )
    return rows


def get_receivables_manager_summary_dataset(session: Session, *, snapshot_date) -> list[dict]:
    rows = summarize_receivables_by_manager(session, snapshot_date=snapshot_date)
    return [{"snapshot_date": snapshot_date, **item} for item in rows]


def _sales_daily_query(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    manager_ref: str | None = None,
    store_ref: str | None = None,
):
    query = session.query(
        OneCSalesDailyKpi.sales_date,
        OneCSalesDailyKpi.manager_ref,
        OneCSalesDailyKpi.manager_name,
        OneCSalesDailyKpi.store_ref,
        OneCSalesDailyKpi.store_name,
        OneCSalesDailyKpi.revenue,
        OneCSalesDailyKpi.sales_count,
        OneCSalesDailyKpi.cost_of_sales,
    ).order_by(OneCSalesDailyKpi.sales_date.desc())

    if date_from is not None:
        query = query.filter(OneCSalesDailyKpi.sales_date >= date_from)
    if date_to is not None:
        query = query.filter(OneCSalesDailyKpi.sales_date <= date_to)
    if manager_ref is not None:
        query = query.filter(OneCSalesDailyKpi.manager_ref == manager_ref)
    if store_ref is not None:
        query = query.filter(OneCSalesDailyKpi.store_ref == store_ref)
    return query


def get_daily_sales_kpi_dataset(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    manager_ref: str | None = None,
    store_ref: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    query = _sales_daily_query(
        session,
        date_from=date_from,
        date_to=date_to,
        manager_ref=manager_ref,
        store_ref=store_ref,
    )

    rows = [
        _with_sales_profitability(
            {
                "sales_date": row.sales_date,
                "manager_ref": row.manager_ref,
                "manager_name": row.manager_name,
                "store_ref": row.store_ref,
                "store_name": row.store_name,
                "revenue": row.revenue,
                "sales_count": row.sales_count,
                "cost_of_sales": row.cost_of_sales,
            }
        )
        for row in query.all()
    ]
    rows.sort(
        key=lambda item: (
            item["sales_date"],
            item["revenue"],
            item["manager_name"] or "",
            item["store_name"] or "",
        ),
        reverse=True,
    )
    if limit:
        rows = rows[:limit]
    return rows


def get_weekly_sales_kpi_dataset(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    manager_ref: str | None = None,
    store_ref: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    query = _sales_daily_query(
        session,
        date_from=date_from,
        date_to=date_to,
        manager_ref=manager_ref,
        store_ref=store_ref,
    )
    grouped: dict[
        tuple[date, str | None, str | None, str | None, str | None],
        dict[str, object],
    ] = {}

    for row in query.all():
        week_start = row.sales_date - timedelta(days=row.sales_date.weekday())
        group_key = (
            week_start,
            row.manager_ref,
            row.manager_name,
            row.store_ref,
            row.store_name,
        )
        if group_key not in grouped:
            grouped[group_key] = {
                "week_start": week_start,
                "week_end": week_start + timedelta(days=6),
                "manager_ref": row.manager_ref,
                "manager_name": row.manager_name,
                "store_ref": row.store_ref,
                "store_name": row.store_name,
                "revenue": Decimal("0.00"),
                "sales_count": Decimal("0.000"),
                "cost_of_sales": Decimal("0.00"),
            }
        grouped[group_key]["revenue"] = grouped[group_key]["revenue"] + row.revenue
        grouped[group_key]["sales_count"] = grouped[group_key]["sales_count"] + row.sales_count
        grouped[group_key]["cost_of_sales"] = (
            grouped[group_key]["cost_of_sales"] + row.cost_of_sales
        )

    rows = [_with_sales_profitability(row) for row in grouped.values()]
    rows.sort(
        key=lambda item: (
            item["week_start"],
            item["revenue"],
            item["manager_name"] or "",
            item["store_name"] or "",
        ),
        reverse=True,
    )
    if limit:
        rows = rows[:limit]
    return rows


def _with_sales_profitability(row: dict[str, object]) -> dict[str, object]:
    revenue = Decimal(str(row.get("revenue") or 0))
    cost_of_sales = Decimal(str(row.get("cost_of_sales") or 0))
    gross_profit = revenue - cost_of_sales
    return {
        **row,
        "gross_profit": gross_profit,
        "margin_pct": _safe_ratio(gross_profit, revenue),
        "profitability_pct": _safe_ratio(gross_profit, cost_of_sales),
    }


def get_receivables_contract_balances(
    session: Session,
    *,
    snapshot_date,
    limit: int | None = None,
    buyers_rub_only: bool = False,
) -> list[dict]:
    if buyers_rub_only:
        return _direct_buyers_rub_snapshot_rows(
            session,
            snapshot_date=snapshot_date,
            limit=limit,
        )

    snapshot_end = datetime.combine(snapshot_date, time.min) + timedelta(days=1)
    query = session.query(
        ReceivableLedgerEvent.counterparty_ref.label("counterparty_ref"),
        ReceivableLedgerEvent.counterparty_name.label("counterparty_name"),
        ReceivableLedgerEvent.contract_ref.label("contract_ref"),
        ReceivableLedgerEvent.contract_name.label("contract_name"),
        ReceivableLedgerEvent.contract_kind_ref.label("contract_kind_ref"),
        ReceivableLedgerEvent.contract_kind_name.label("contract_kind_name"),
        ReceivableLedgerEvent.source_layer.label("source_layer"),
        func.sum(ReceivableLedgerEvent.amount_delta).label("current_balance"),
        func.count(ReceivableLedgerEvent.id).label("event_count"),
        func.max(ReceivableLedgerEvent.external_document_date).label("last_event_at"),
    ).filter(ReceivableLedgerEvent.external_document_date < snapshot_end)
    query = (
        query.group_by(
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.counterparty_name,
            ReceivableLedgerEvent.contract_ref,
            ReceivableLedgerEvent.contract_name,
            ReceivableLedgerEvent.contract_kind_ref,
            ReceivableLedgerEvent.contract_kind_name,
            ReceivableLedgerEvent.source_layer,
        )
        .having(func.sum(ReceivableLedgerEvent.amount_delta) != 0)
        .order_by(
            func.abs(func.sum(ReceivableLedgerEvent.amount_delta)).desc(),
            ReceivableLedgerEvent.counterparty_ref,
            ReceivableLedgerEvent.contract_ref,
        )
    )
    if limit:
        query = query.limit(limit)

    rows: list[dict] = []
    for item in query.all():
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "counterparty_ref": item.counterparty_ref,
                "counterparty_name": item.counterparty_name,
                "contract_ref": item.contract_ref,
                "contract_name": item.contract_name,
                "contract_kind_ref": item.contract_kind_ref,
                "contract_kind_name": item.contract_kind_name,
                "source_layer": item.source_layer,
                "current_balance": item.current_balance,
                "event_count": item.event_count,
                "last_event_at": item.last_event_at,
            }
        )
    return rows
