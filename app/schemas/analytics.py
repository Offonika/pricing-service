from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class ModelDemandItem(BaseModel):
    device_model_id: int
    brand: str
    model_name: str
    variant: str | None = None
    region: str | None = None
    impressions: float | None = None
    clicks: float | None = None
    keywords_count: int | None = None
    last_updated_at: datetime | None = None


class ModelDemandTimeseriesItem(BaseModel):
    date: date
    device_model_id: int
    brand: str
    model_name: str
    variant: str | None = None
    region: str | None = None
    impressions: float | None = None
    clicks: float | None = None
    keywords_count: int | None = None
    last_updated_at: datetime | None = None
