"""What the Recommendation Engine reads and returns.

Contract: docs/modules/recommendation-engine.md.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from seawork.domain.match import MatchScore
from seawork.domain.models import EnrichedOpportunity


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: int
    opportunity: EnrichedOpportunity
    employer: str | None
    freshness: datetime


class FeedHistory(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivered_ids: frozenset[int]
    dismissed_ids: frozenset[int]


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: int
    score: MatchScore
    rank: int = Field(ge=1)
    similar_ids: tuple[int, ...]


class Delivery(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: int
    similar_ids: tuple[int, ...]
    score_value: float | None
    comparable_factors: int
    channel: str
    delivered_at: datetime
