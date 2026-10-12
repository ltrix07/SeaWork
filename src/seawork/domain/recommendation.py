"""What the Recommendation Engine reads and returns.

Contract: docs/modules/recommendation-engine.md.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from seawork.domain.models import EnrichedOpportunity


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: int
    opportunity: EnrichedOpportunity
    employer: str | None
    freshness: datetime
