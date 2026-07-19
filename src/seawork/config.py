from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SEAWORK_", env_file=".env")

    database_url: str = "postgresql+psycopg://seawork:seawork@localhost:5432/seawork"
    user_agent: str = "SeaWork ingestion/0.1 (contact: engineering@seawork.example)"
    request_timeout_seconds: float = 30.0
    request_interval_seconds: float = 1.0
    reference_dir: Path = Path("data/reference")
    source_registry: Path = Path("sources/registry.yaml")
