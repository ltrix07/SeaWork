from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from seawork.domain.enums import SourceTier, SourceTrust


class RawItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    external_id: str
    url: HttpUrl
    fetched_at: datetime
    payload: str
    content_type: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    http_status: int


class Source(Protocol):
    source_id: str
    tier: SourceTier
    trust: SourceTrust

    def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]: ...
