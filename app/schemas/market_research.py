from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ScreenInfo(BaseModel):
    size_inch: float | None = Field(default=None, description="Диагональ экрана в дюймах")
    technology: str | None = Field(default=None, description="Тип матрицы (AMOLED/OLED/LCD)")
    refresh_rate_hz: int | None = Field(default=None, description="Частота обновления экрана")


class DeviceModelCreate(BaseModel):
    brand: str
    model_name: str
    variant: str | None = None
    source: str | None = Field(default="news_agent", description="Источник модели устройства")
    announce_date: date | None = None
    release_date: date | None = None
    screen: ScreenInfo | None = None


class DeviceModelResponse(DeviceModelCreate):
    id: int
    screen_size_inch: float | None = None
    screen_technology: str | None = None
    screen_refresh_rate_hz: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class KeywordBulkCreate(BaseModel):
    phone_model_id: int
    phrases: list[str]
    language: str | None = None
    category: str | None = Field(default="display", description="Категория запчасти")
    source: str | None = Field(default="agent", description="Источник генерации фраз")


class KeywordResponse(BaseModel):
    id: int
    phrase: str
    phone_model_id: int
    language: str | None = None
    category: str | None = None

    model_config = ConfigDict(from_attributes=True)


class KeywordDemandResponse(BaseModel):
    keyword_id: int
    phrase: str | None = None
    region: str | None = None
    date: date
    impressions: int | None = None
    clicks: int | None = None
    ctr: float | None = None

    model_config = ConfigDict(from_attributes=True)
