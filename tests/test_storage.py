"""Storage layer: idempotency, versioning and what survives a reprocess.

This layer was the only one with no tests of its own. Its guarantees were exercised
only through the whole pipeline, where a broken one shows up as a wrong number in a
coverage report rather than as a failure with a name.
"""

import datetime
import hashlib
import os
from collections.abc import Iterator

import pytest
from pydantic import HttpUrl
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityStatus, OpportunityType, SourceTrust
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.storage.models import Base, OpportunityRecord, RawItemRecord
from seawork.storage.repository import Repository

# The repository speaks PostgreSQL on purpose - INSERT ... ON CONFLICT is how both
# idempotency guarantees are expressed - so these tests need a real server rather
# than SQLite. CI provides one; locally `docker compose up -d` does.
DATABASE_URL = os.environ.get(
    "SEAWORK_TEST_DATABASE_URL",
    os.environ.get(
        "SEAWORK_DATABASE_URL", "postgresql+psycopg://seawork:seawork@localhost:5432/seawork"
    ),
)


# Everything happens inside a schema this file owns. The default URL points at a
# port that, on a developer machine, may well be someone else's PostgreSQL: creating
# and dropping tables in `public` there would be destructive in a way a test suite
# must never be. Dropping a schema we created ourselves cannot reach anyone's data.
TEST_SCHEMA = "seawork_test"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={TEST_SCHEMA}"})
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}"))
    except OperationalError as error:  # pragma: no cover - depends on the environment
        engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable at {DATABASE_URL}: {error}")
    # Recreated per test: these assertions count rows, and a table left over from a
    # neighbour would make them pass or fail for the wrong reason.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as opened:
        yield opened
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
    engine.dispose()


def raw_item(
    external_id: str = "1", payload: str = "body", fetched_at: str = "2026-09-01"
) -> RawItem:
    return RawItem(
        source_id="padi",
        external_id=external_id,
        url=HttpUrl(f"https://example.com/job/{external_id}"),
        fetched_at=datetime.datetime.fromisoformat(fetched_at).replace(tzinfo=datetime.UTC),
        payload=payload,
        content_type="application/xml",
        content_hash=hashlib.sha256(payload.encode()).hexdigest(),
        http_status=200,
    )


def enriched(
    external_id: str = "1", title: str = "Dive Instructor", notes: list[str] | None = None
) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title=title,
            description="x" * 200,
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        quality_score=1.0,
        quality_flags=[],
        quality_notes=notes or [],
    )


def test_saving_the_same_payload_twice_stores_one_row(session: Session) -> None:
    """Refetching an unchanged posting must not grow the table.

    `ingest refetch` re-reads everything it knows about; without this the raw table
    would gain a copy of the whole corpus on every run.
    """
    repository = Repository(session)
    first = repository.save_raw(raw_item())
    second = repository.save_raw(raw_item())
    assert first.id == second.id
    assert session.execute(select(func.count()).select_from(RawItemRecord)).scalar_one() == 1


def test_changed_payload_is_kept_as_a_new_version(session: Session) -> None:
    """A different body under the same identifier is history, not a duplicate."""
    repository = Repository(session)
    repository.save_raw(raw_item(payload="body"))
    repository.save_raw(raw_item(payload="body, edited"))
    assert session.execute(select(func.count()).select_from(RawItemRecord)).scalar_one() == 2


def test_iter_latest_raw_yields_one_row_per_posting(session: Session) -> None:
    """Reprocess reads the newest version of each posting, not every version.

    Feeding it all versions would enrich the same vacancy several times and let an
    older body overwrite a newer one, depending on row order.
    """
    repository = Repository(session)
    repository.save_raw(raw_item(external_id="1", payload="old", fetched_at="2026-09-01"))
    repository.save_raw(raw_item(external_id="1", payload="new", fetched_at="2026-09-05"))
    repository.save_raw(raw_item(external_id="2", payload="other", fetched_at="2026-09-02"))

    latest = list(repository.iter_latest_raw("padi"))
    assert {record.external_id for record in latest} == {"1", "2"}
    assert next(r.payload for r in latest if r.external_id == "1") == "new"


def test_iter_latest_raw_is_scoped_to_one_source(session: Session) -> None:
    repository = Repository(session)
    repository.save_raw(raw_item())
    other = raw_item().model_copy(update={"source_id": "crewplanet"})
    repository.save_raw(other)
    assert [record.source_id for record in repository.iter_latest_raw("padi")] == ["padi"]


def test_duplicate_content_looks_across_postings_not_at_itself(session: Session) -> None:
    """The same body under a second identifier is a duplicate; under its own is not.

    Comparing a posting with itself would flag every refetched record as duplicate
    content and reject the corpus on the second run.
    """
    repository = Repository(session)
    item = raw_item(external_id="1", payload="identical")
    repository.save_raw(item)
    assert not repository.has_duplicate_content("padi", "1", item.content_hash)
    assert repository.has_duplicate_content("padi", "2", item.content_hash)


def test_reprocessing_updates_in_place(session: Session) -> None:
    """Enriching the same posting again must overwrite, never accumulate.

    `ingest reprocess` re-runs enrichment over stored payloads. A second row per
    posting would silently double every coverage number the project reports.
    """
    repository = Repository(session)
    record = repository.save_raw(raw_item())
    repository.upsert_opportunity(
        record.id, enriched(title="Dive Instructor"), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    repository.upsert_opportunity(
        record.id, enriched(title="Scuba Instructor"), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )

    stored = session.scalars(select(OpportunityRecord)).all()
    assert len(stored) == 1
    assert stored[0].title == "Scuba Instructor"


def test_reprocessing_can_revive_a_rejected_posting(session: Session) -> None:
    """A record rejected once must not stay rejected after the rules improve."""
    repository = Repository(session)
    record = repository.save_raw(raw_item())
    repository.upsert_opportunity(
        record.id, enriched(), SourceTrust.PRIMARY, OpportunityStatus.REJECTED
    )
    repository.upsert_opportunity(
        record.id, enriched(), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    assert session.scalars(select(OpportunityRecord)).one().status == OpportunityStatus.ACTIVE.value


def test_first_seen_at_survives_reprocessing(session: Session) -> None:
    """first_seen_at is our fact, not the source's, and reprocessing must not reset it.

    Pinpoint states no publication date at all (source-pinpoint.md 3.2), so this is
    the only ordering by freshness those postings have.
    """
    repository = Repository(session)
    record = repository.save_raw(raw_item())
    repository.upsert_opportunity(
        record.id, enriched(), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    original = session.scalars(select(OpportunityRecord)).one().first_seen_at

    session.expire_all()
    repository.upsert_opportunity(
        record.id, enriched(title="Renamed"), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    assert session.scalars(select(OpportunityRecord)).one().first_seen_at == original


def test_quality_notes_round_trip(session: Session) -> None:
    """Notes are the reason C5 exists; storing them is half of keeping them."""
    repository = Repository(session)
    record = repository.save_raw(raw_item())
    repository.upsert_opportunity(
        record.id, enriched(notes=["source_trashed"]), SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    stored = session.scalars(select(OpportunityRecord)).one()
    assert stored.quality_notes == ["source_trashed"]
    assert stored.quality_flags == []
