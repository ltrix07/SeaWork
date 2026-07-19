from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from seawork.domain.enums import OpportunityStatus, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.base import Enricher
from seawork.ingestion.quality import check_quality
from seawork.storage.models import RawItemRecord
from seawork.storage.repository import Repository


class ProcessingSource(Protocol):
    source_id: str
    trust: SourceTrust

    def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]: ...

    def normalize(self, item: RawItem) -> NormalizedOpportunity: ...


@dataclass(frozen=True)
class PipelineResult:
    fetched: int
    processed: int


class IngestionPipeline:
    def __init__(
        self,
        repository: Repository,
        source: ProcessingSource,
        enricher: Enricher,
        classification_rules: Path,
    ) -> None:
        self._repository = repository
        self._source = source
        self._enricher = enricher
        self._classification_rules = classification_rules

    async def run(self, limit: int | None = None) -> PipelineResult:
        fetched = 0
        processed = 0
        async for item in self._source.collect():
            if limit is not None and fetched >= limit:
                break
            raw_record = self._repository.save_raw(item)  # Required before normalization (§3.1).
            fetched += 1
            self._process(item, raw_record.id)
            processed += 1
        return PipelineResult(fetched=fetched, processed=processed)

    async def refetch(self, limit: int | None = None) -> PipelineResult:
        fetched = 0
        async for item in self._source.collect():
            if limit is not None and fetched >= limit:
                break
            self._repository.save_raw(item)
            fetched += 1
        return PipelineResult(fetched=fetched, processed=0)

    def reprocess(self) -> PipelineResult:
        processed = 0
        for raw_record in self._repository.iter_latest_raw(self._source.source_id):
            item = raw_from_record(raw_record)
            self._process(item, raw_record.id)
            processed += 1
        return PipelineResult(fetched=0, processed=processed)

    def _process(self, item: RawItem, raw_item_id: int) -> None:
        normalized = self._source.normalize(item)
        duplicate = self._repository.has_duplicate_content(
            item.source_id, item.external_id, item.content_hash
        )
        quality = check_quality(normalized, duplicate_content=duplicate)
        classification = classify(normalized, self._classification_rules)
        enriched = self._enricher.enrich(normalized, classification, quality)
        status = OpportunityStatus.ACTIVE if quality.accepted else OpportunityStatus.REJECTED
        self._repository.upsert_opportunity(raw_item_id, enriched, self._source.trust, status)


def raw_from_record(record: RawItemRecord) -> RawItem:
    return RawItem.model_validate(
        {
            "source_id": record.source_id,
            "external_id": record.external_id,
            "url": record.url,
            "fetched_at": record.fetched_at,
            "payload": record.payload,
            "content_type": record.content_type,
            "content_hash": record.content_hash,
            "http_status": record.http_status,
        }
    )
