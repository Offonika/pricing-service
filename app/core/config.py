from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pricing Service"
    environment: str = "development"
    debug: bool = True
    app_port: int = 8000

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/pricing"
    redis_url: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", env_nested_delimiter="__")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
