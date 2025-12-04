from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pricing Service"
    environment: str = "development"
    debug: bool = True
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/pricing"
    redis_url: str = "redis://localhost:6379/0"

    competitor_source_mode: str = "zenno"  # zenno | internal
    competitor_parse_limit: int = 10
    proxy_api_url: Optional[str] = None
    proxy_api_token: Optional[str] = None
    proxy_timeout_seconds: float = 10.0
    proxy_max_retries: int = 3
    proxy_rps_limit: Optional[float] = None
    competitor_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
    )
    competitor_accept_language: str = "ru,en;q=0.9"
    competitor_cookies: Optional[str] = None
    zenlogs_import_enabled: bool = True
    zenlogs_moba_url: Optional[str] = None
    zenlogs_sources: Optional[str] = None
    zenlogs_http_timeout_sec: float = 30.0
    zenlogs_verify_ssl: bool = True
    competitor_ftp_import_enabled: bool = False
    competitor_ftp_host: Optional[str] = None
    competitor_ftp_port: int = 21
    competitor_ftp_user: Optional[str] = None
    competitor_ftp_password: Optional[str] = None
    competitor_ftp_tls: bool = False
    competitor_ftp_timeout_sec: float = 30.0
    competitor_ftp_sources: Optional[str] = None  # name:directory:pattern with {date}, comma-separated
    competitor_ftp_max_files_per_source: int = 2
    captcha_provider: str = "2captcha"
    captcha_api_key: Optional[str] = None

    # LLM / OpenAI
    openai_api_key: Optional[str] = None
    openai_api_base: Optional[str] = None
    openai_model: str = "gpt-4o-mini"

    # Smartphone releases / news ingestion
    smartphone_releases_enabled: bool = False
    smartphone_news_api_base_url: Optional[str] = "https://newsapi.org/v2/everything"
    smartphone_news_api_key: Optional[str] = None
    smartphone_news_language: str = "ru,en"
    smartphone_news_query: str = '"смартфон" OR "smartphone" OR "phone launch"'
    smartphone_news_days_back: int = 5
    smartphone_news_page_size: int = 10
    smartphone_news_max_pages: int = 1
    smartphone_news_max_items: Optional[int] = 40
    smartphone_release_request_delay_seconds: float = 0.25
    smartphone_release_llm_model: Optional[str] = None
    smartphone_gsmarena_enabled: bool = False
    smartphone_gsmarena_rss_url: str = "https://www.gsmarena.com/rss-news-reviews.php"
    smartphone_gsmarena_max_items: Optional[int] = 40

    # TopControl categories filter (comma-separated ids)
    topcontrol_category_whitelist: Optional[str] = None
    match_subject_whitelist: Optional[str] = None

    # Yandex Direct / demand
    yandex_direct_api_token: Optional[str] = None
    yandex_direct_api_base_url: str = "https://api.direct.yandex.ru/json/v5/keywordsresearch"
    yandex_default_region: str = "225"  # Russia (пример кода региона)
    yandex_direct_timeout: float = 10.0
    yandex_direct_batch_size: int = 100
    yandex_direct_rps_limit: Optional[float] = None
    yandex_direct_client_login: Optional[str] = None
    yandex_demand_days_window: int = 30
    yandex_demand_update_limit: int = 200
    yandex_demand_staleness_days: int = 7
    feature_yandex_demand_enabled: bool = False
    yandex_wordstat_enabled: bool = False
    yandex_wordstat_base_url: str = "https://api.wordstat.yandex.net"
    yandex_wordstat_devices: str = "all"

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", env_nested_delimiter="__", extra="ignore"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
