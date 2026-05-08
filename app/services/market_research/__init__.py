"""Market research (device models, keywords, demand) service layer."""

from app.services.market_research.demand_provider import MarketDemandProvider
from app.services.market_research.device_models import DeviceModelService
from app.services.market_research.keyword_generation import KeywordGenerationService
from app.services.market_research.matching import MatchResult, ProductModelMatcher
from app.services.market_research.repositories import KeywordRepository
from app.services.market_research.wordstat import WordstatClient
from app.services.market_research.yandex_direct import (
    DemandService,
    YandexDirectClient,
    YandexKeywordStat,
    build_demand_service,
)

__all__ = [
    "DeviceModelService",
    "KeywordGenerationService",
    "MarketDemandProvider",
    "ProductModelMatcher",
    "MatchResult",
    "KeywordRepository",
    "DemandService",
    "YandexDirectClient",
    "YandexKeywordStat",
    "build_demand_service",
    "WordstatClient",
]
