from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres123@postgres-primary:5432/edumanage"
    redis_url: str = "redis://redis:6379/0"

    # CDC: PG 逻辑复制 → Redis Stream
    cdc_pg_dsn: str = "host=postgres-primary port=5432 dbname=edumanage user=postgres password=postgres123"
    cdc_publication: str = "cdc_pub"
    cdc_slot: str = "redis_cdc_slot"
    cdc_stream_key: str = "cdc:events"
    cdc_stream_maxlen: int = 10000
    cdc_consumer_group: str = "kpi-consumer-group"
    secret_key: str = "edu-manage-secret-key-change-in-production-2024"
    access_token_expire_minutes: int = 1440
    algorithm: str = "HS256"

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
