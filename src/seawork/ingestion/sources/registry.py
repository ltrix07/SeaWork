from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from seawork.config import Settings
from seawork.domain.enums import SourceTrust
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource


class SourceConfig(BaseModel):
    platform: str
    account: str
    source_id: str
    trust: SourceTrust
    direction: str | None = None
    workplace_type_hint: str | None = None


@dataclass(frozen=True)
class RegisteredSource:
    source: PinpointSource
    config: SourceConfig


def load_registry(path: Path) -> list[SourceConfig]:
    import yaml

    with path.open(encoding="utf-8") as stream:
        raw: Any = yaml.safe_load(stream)
    if not isinstance(raw, list):
        raise ValueError("Source registry must contain a list")
    return TypeAdapter(list[SourceConfig]).validate_python(raw)


def build_source(source_id: str, settings: Settings) -> RegisteredSource:
    configs = load_registry(settings.source_registry)
    config = next((row for row in configs if row.source_id == source_id), None)
    if config is None:
        raise KeyError(f"Unknown source: {source_id}")
    if config.platform != "pinpoint":
        raise ValueError(f"Unsupported platform: {config.platform}")
    source = PinpointSource(
        account=config.account,
        source_id=config.source_id,
        trust=config.trust,
        user_agent=settings.user_agent,
        timeout_seconds=settings.request_timeout_seconds,
        interval_seconds=settings.request_interval_seconds,
    )
    return RegisteredSource(source=source, config=config)
