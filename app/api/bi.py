from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas.bi import (
    BICanonicalizationSummary,
    BICompetitorPrice,
    BIDailySalesKPI,
    BIPhoneModelLink,
    BIProduct,
    BIReceivableCase,
    BIReceivableContractBalance,
    BIReceivableCurrent,
    BIReceivablesManagerSummary,
    BIRecommendation,
    BIUnresolvedCompatibility,
    BIWeeklySalesKPI,
)
from app.services import bi as bi_service

router = APIRouter()


@router.get("/products", response_model=List[BIProduct])
def bi_products(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_products_dataset(db, limit=limit)


@router.get("/recommendations", response_model=List[BIRecommendation])
def bi_recommendations(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_latest_recommendations(db, limit=limit)


@router.get("/competitor-prices", response_model=List[BICompetitorPrice])
def bi_competitor_prices(limit: int = 100, db: Session = Depends(get_db)):
    return bi_service.get_competitor_prices(db, limit=limit)


@router.get("/phone-model-links", response_model=List[BIPhoneModelLink])
def bi_phone_model_links(
    phone_model_id: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    return bi_service.get_phone_model_links(db, phone_model_id=phone_model_id, limit=limit)


@router.get("/compatibility-unresolved", response_model=List[BIUnresolvedCompatibility])
def bi_unresolved_compatibilities(
    limit: int = 100,
    ambiguous_only: bool = False,
    db: Session = Depends(get_db),
):
    return bi_service.get_unresolved_compatibilities(db, limit=limit, ambiguous_only=ambiguous_only)


@router.get("/canonicalization-summary", response_model=BICanonicalizationSummary)
def bi_canonicalization_summary(db: Session = Depends(get_db)):
    return bi_service.get_canonicalization_summary(db)


@router.get("/receivables-current", response_model=List[BIReceivableCurrent])
def bi_receivables_current(
    date_value: date = Query(alias="date"),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    return bi_service.get_receivables_current(db, snapshot_date=date_value, limit=limit)


@router.get("/receivable-cases", response_model=List[BIReceivableCase])
def bi_receivable_cases(
    date_value: date = Query(alias="date"),
    segment: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    return bi_service.get_receivable_cases_dataset(
        db,
        snapshot_date=date_value,
        segment=segment,
        limit=limit,
    )


@router.get("/receivables-manager-summary", response_model=List[BIReceivablesManagerSummary])
def bi_receivables_manager_summary(
    date_value: date = Query(alias="date"),
    db: Session = Depends(get_db),
):
    return bi_service.get_receivables_manager_summary_dataset(db, snapshot_date=date_value)


@router.get("/receivables-contract-balances", response_model=List[BIReceivableContractBalance])
def bi_receivables_contract_balances(
    date_value: date = Query(alias="date"),
    limit: int | None = Query(default=None, ge=1),
    buyers_rub_only: bool = Query(
        default=False,
        description=(
            "Режим буквальной сверки с отчетом 1С: regular-контур, "
            "контрагенты из группы 'ПОКУПАТЕЛИ', рублевый срез"
        ),
    ),
    db: Session = Depends(get_db),
):
    return bi_service.get_receivables_contract_balances(
        db,
        snapshot_date=date_value,
        limit=limit,
        buyers_rub_only=buyers_rub_only,
    )


@router.get("/sales-daily-kpi", response_model=List[BIDailySalesKPI])
def bi_sales_daily_kpi(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    manager_ref: str | None = Query(default=None),
    store_ref: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    return bi_service.get_daily_sales_kpi_dataset(
        db,
        date_from=date_from,
        date_to=date_to,
        manager_ref=manager_ref,
        store_ref=store_ref,
        limit=limit,
    )


@router.get("/sales-weekly-kpi", response_model=List[BIWeeklySalesKPI])
def bi_sales_weekly_kpi(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    manager_ref: str | None = Query(default=None),
    store_ref: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    return bi_service.get_weekly_sales_kpi_dataset(
        db,
        date_from=date_from,
        date_to=date_to,
        manager_ref=manager_ref,
        store_ref=store_ref,
        limit=limit,
    )
