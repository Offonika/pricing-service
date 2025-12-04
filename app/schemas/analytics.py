from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class ModelDemandItem(BaseModel):
    device_model_id: int
    brand: str
    model_name: str
    variant: Optional[str] = None
    region: Optional[str] = None
    impressions: Optional[float] = None
    clicks: Optional[float] = None
    keywords_count: Optional[int] = None
    last_updated_at: Optional[datetime] = None


class ModelDemandTimeseriesItem(BaseModel):
    date: date
    device_model_id: int
    brand: str
    model_name: str
    variant: Optional[str] = None
    region: Optional[str] = None
    impressions: Optional[float] = None
    clicks: Optional[float] = None
    keywords_count: Optional[int] = None
    last_updated_at: Optional[datetime] = None
