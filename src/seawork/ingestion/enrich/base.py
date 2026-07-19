from typing import Protocol

from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.quality import QualityResult


class Enricher(Protocol):
    def enrich(
        self,
        opportunity: NormalizedOpportunity,
        classification: Classification,
        quality: QualityResult,
    ) -> EnrichedOpportunity: ...
