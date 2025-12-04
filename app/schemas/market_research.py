from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


class ScreenInfo(BaseModel):
    size_inch: Optional[float] = Field(default=None, description="Диагональ экрана в дюймах")
    technology: Optional[str] = Field(default=None, description="Тип матрицы (AMOLED/OLED/LCD)")
    refresh_rate_hz: Optional[int] = Field(default=None, description="Частота обновления экрана")


class DeviceModelCreate(BaseModel):
    brand: str
    model_name: str
    variant: Optional[str] = None
    announce_date: Optional[date] = None
    release_date: Optional[date] = None
    screen: Optional[ScreenInfo] = None


class DeviceModelResponse(DeviceModelCreate):
    id: int
    screen_size_inch: Optional[float] = None
    screen_technology: Optional[str] = None
    screen_refresh_rate_hz: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class KeywordBulkCreate(BaseModel):
    phone_model_id: int
    phrases: List[str]
    language: Optional[str] = None
    category: Optional[str] = Field(default="display", description="Категория запчасти")
    source: Optional[str] = Field(default="agent", description="Источник генерации фраз")


class KeywordResponse(BaseModel):
    id: int
    phrase: str
    phone_model_id: int
    language: Optional[str] = None
    category: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class KeywordDemandResponse(BaseModel):
    keyword_id: int
    phrase: Optional[str] = None
    region: Optional[str] = None
    date: date
    impressions: Optional[int] = None
    clicks: Optional[int] = None
    ctr: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)
