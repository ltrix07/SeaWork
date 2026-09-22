from collections.abc import Iterator
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityStatus, SourceTrust
from seawork.domain.models import EnrichedOpportunity
from seawork.ingestion.base import RawItem
from seawork.storage.models import OpportunityRecord, RawItemRecord


class Repository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_raw(self, item: RawItem) -> RawItemRecord:
        statement = (
            insert(RawItemRecord)
            .values(
                source_id=item.source_id,
                external_id=item.external_id,
                url=str(item.url),
                fetched_at=item.fetched_at,
                payload=item.payload,
                content_type=item.content_type,
                content_hash=item.content_hash,
                http_status=item.http_status,
            )
            .on_conflict_do_nothing(index_elements=["source_id", "external_id", "content_hash"])
            .returning(RawItemRecord.id)
        )
        record_id = self._session.execute(statement).scalar_one_or_none()
        if record_id is None:
            record_id = self._session.execute(
                select(RawItemRecord.id).where(
                    RawItemRecord.source_id == item.source_id,
                    RawItemRecord.external_id == item.external_id,
                    RawItemRecord.content_hash == item.content_hash,
                )
            ).scalar_one()
        self._session.commit()
        return self._session.get_one(RawItemRecord, record_id)

    def upsert_opportunity(
        self,
        raw_item_id: int,
        opportunity: EnrichedOpportunity,
        trust: SourceTrust,
        status: OpportunityStatus,
    ) -> None:
        normalized = opportunity.normalized
        excluded = {
            "normalized",
            "type",
            "type_confidence",
            "quality_score",
            "quality_flags",
            "quality_notes",
        }
        enriched = opportunity.model_dump(mode="json", exclude=excluded)
        values: dict[str, Any] = {
            "raw_item_id": raw_item_id,
            "source_id": normalized.source_id,
            "source_trust": trust.value,
            "external_id": normalized.external_id,
            "url": str(normalized.url),
            "title": normalized.title,
            "description": normalized.description,
            "employer": normalized.employer,
            "posted_at": normalized.posted_at,
            "type": opportunity.type.value,
            "type_confidence": opportunity.type_confidence,
            "enriched": enriched,
            "quality_score": opportunity.quality_score,
            "quality_flags": opportunity.quality_flags,
            "quality_notes": opportunity.quality_notes,
            "status": status.value,
        }
        statement = insert(OpportunityRecord).values(**values)
        update_values = dict(values)
        update_values.pop("source_id")
        update_values.pop("external_id")
        statement = statement.on_conflict_do_update(
            index_elements=["source_id", "external_id"], set_=update_values
        )
        self._session.execute(statement)
        self._session.commit()

    def iter_latest_raw(self, source_id: str) -> Iterator[RawItemRecord]:
        ranked = (
            select(
                RawItemRecord.id,
                func.row_number()
                .over(
                    partition_by=(RawItemRecord.source_id, RawItemRecord.external_id),
                    order_by=(RawItemRecord.fetched_at.desc(), RawItemRecord.id.desc()),
                )
                .label("row_number"),
            )
            .where(RawItemRecord.source_id == source_id)
            .subquery()
        )
        statement = (
            select(RawItemRecord)
            .join(ranked, ranked.c.id == RawItemRecord.id)
            .where(ranked.c.row_number == 1)
        )
        yield from self._session.scalars(statement)

    def has_duplicate_content(self, source_id: str, external_id: str, content_hash: str) -> bool:
        statement: Select[tuple[int]] = select(RawItemRecord.id).where(
            RawItemRecord.source_id == source_id,
            RawItemRecord.external_id != external_id,
            RawItemRecord.content_hash == content_hash,
        )
        return self._session.execute(statement.limit(1)).scalar_one_or_none() is not None
