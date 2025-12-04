from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.analytics import ModelDemandItem, ModelDemandTimeseriesItem


class ModelDemandService:
    """Сервис для выборки витрин спроса по моделям телефонов."""

    def __init__(self, db: Session):
        self.db = db

    def get_top_models(
        self,
        days: int = 30,
        limit: int = 50,
        brand: Optional[str] = None,
        region: Optional[str] = None,
    ) -> List[ModelDemandItem]:
        # Используем агрегированное view за 30 дней; параметр days оставлен на будущее расширение.
        sql = """
            SELECT
                device_model_id,
                brand,
                model_name,
                variant,
                region,
                impressions_30d AS impressions,
                clicks_30d AS clicks,
                keyword_demands_count AS keywords_count,
                last_date AS last_updated_at
            FROM vw_bi_model_demand_30d
            WHERE 1=1
        """
        params = {}
        if brand:
            sql += " AND brand = :brand"
            params["brand"] = brand
        if region:
            sql += " AND region = :region"
            params["region"] = region
        sql += " ORDER BY impressions_30d DESC NULLS LAST LIMIT :limit"
        params["limit"] = limit
        rows = self.db.execute(text(sql), params).mappings().all()
        return [ModelDemandItem(**row) for row in rows]

    def get_timeseries(
        self,
        device_model_id: int,
        date_from: date,
        date_to: date,
        region: Optional[str] = None,
    ) -> List[ModelDemandTimeseriesItem]:
        sql = """
            SELECT
                date,
                device_model_id,
                brand,
                model_name,
                variant,
                region,
                impressions,
                clicks,
                keywords_count,
                last_updated_at
            FROM vw_bi_model_demand_daily
            WHERE device_model_id = :device_model_id
              AND date BETWEEN :date_from AND :date_to
        """
        params = {"device_model_id": device_model_id, "date_from": date_from, "date_to": date_to}
        if region:
            sql += " AND (region = :region)"
            params["region"] = region
        sql += " ORDER BY date ASC"
        rows = self.db.execute(text(sql), params).mappings().all()
        return [ModelDemandTimeseriesItem(**row) for row in rows]
