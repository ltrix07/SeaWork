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
    # LLM enrichment is deliberately opt-in.  These settings describe an
    # OpenAI-compatible structured-output endpoint, rather than selecting a
    # vendor in code, so the infrastructure and user-facing AI can use distinct
    # providers.
    llm_provider: str | None = None
    llm_model_id: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Schema-constrained extraction is not a creative task, so run-to-run spread is
    # pure noise: it hides the difference between providers and devalues the cache,
    # where one payload must map to one answer. None omits the field entirely, for
    # providers that reject it.
    llm_temperature: float | None = 0.0
