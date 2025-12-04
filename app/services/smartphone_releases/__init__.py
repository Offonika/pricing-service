"""Smartphone releases ingestion (news + LLM normalization + persistence)."""

from app.services.smartphone_releases.news_client import SmartphoneNewsClient, build_news_client_from_settings
from app.services.smartphone_releases.normalizer import SmartphoneReleaseNormalizer, build_normalizer_from_settings
from app.services.smartphone_releases.service import (
    SmartphoneReleaseRepository,
    SmartphoneReleaseService,
    build_release_service,
)
from app.services.smartphone_releases.gsmarena_client import GSMArenaClient, build_gsmarena_client_from_settings
from app.services.smartphone_releases.types import RawNewsItem, NormalizedReleaseCandidate

__all__ = [
    "SmartphoneNewsClient",
    "SmartphoneReleaseNormalizer",
    "SmartphoneReleaseRepository",
    "SmartphoneReleaseService",
    "RawNewsItem",
    "NormalizedReleaseCandidate",
    "build_news_client_from_settings",
    "build_normalizer_from_settings",
    "build_release_service",
    "GSMArenaClient",
    "build_gsmarena_client_from_settings",
]
