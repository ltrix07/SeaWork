"""Storage layer: idempotency, versioning and what survives a reprocess.

This layer was the only one with no tests of its own. Its guarantees were exercised
only through the whole pipeline, where a broken one shows up as a wrong number in a
coverage report rather than as a failure with a name.
"""

import datetime
import hashlib
import uuid
from decimal import Decimal

import pytest
from pydantic import HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from seawork.domain.enums import (
    SOURCE_TRASHED_NOTE,
    ExperienceLevel,
    OpportunityStatus,
    OpportunityType,
    Provenance,
    SourceTrust,
)
from seawork.domain.inferred import Inferred
from seawork.domain.match import FactorOutcome
from seawork.domain.models import (
    EnrichedOpportunity,
    HiringScope,
    NormalizedOpportunity,
    SalaryRange,
)
from seawork.domain.profile import CareerStage, Fact, ProfileFactSource, UserProfile
from seawork.ingestion.base import RawItem
from seawork.scoring import compute_match_score
from seawork.storage.models import OpportunityRecord, RawItemRecord
from seawork.storage.repository import Repository

# Schema owned by this module; the shared `session` fixture in conftest.py creates it.


@pytest.fixture
def test_schema() -> str:
    return "seawork_test"


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


NOW = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC)


def inferred[T](
    value: T,
    provenance: Provenance = Provenance.RULE,
    confidence: float = 0.8,
    quotes: dict[str, str] | None = None,
) -> Inferred[T]:
    return Inferred(value=value, provenance=provenance, confidence=confidence, quotes=quotes)


def stated[T](value: T) -> Fact[T]:
    return Fact(value=value, source=ProfileFactSource.STATED, confidence=1.0, stated_at=NOW)


def full_opportunity(
    external_id: str = "1",
    office: str | None = None,
    scope: HiringScope | None = None,
    notes: list[str] | None = None,
) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title="Dive Instructor",
            description="x" * 200,
            employer="Blue Fin",
            recruitment_office_raw=office,
            posted_at=NOW,
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        country=inferred("TH", Provenance.SOURCE, 1.0),
        city=inferred("Koh Lanta"),
        embarkation_port=inferred("Phuket"),
        operating_region=inferred("Andaman Sea"),
        hiring_scope=inferred(scope or HiringScope(countries=["TH"], unresolved=["Caribbean"])),
        profession=inferred("dive_instructor", quotes={"dive_instructor": "Dive Instructor"}),
        direction=inferred("diving"),
        required_certificates=inferred(["padi_idc"], quotes={"padi_idc": "PADI IDC required"}),
        required_languages=inferred(["en"]),
        preferred_languages=inferred(["de"]),
        experience_level=inferred(ExperienceLevel.MID),
        salary=inferred(
            SalaryRange(
                minimum=Decimal("1234.50"),
                maximum=Decimal("2000.75"),
                currency="USD",
                period="month",
            )
        ),
        workplace_type_hint=inferred("onboard"),
        quality_score=0.9,
        quality_flags=[],
        quality_notes=notes or [],
    )


def scoring_profile() -> UserProfile:
    return UserProfile(
        user_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
        career_stage=stated(CareerStage.WORKING),
        professions=stated(["dive_instructor"]),
        directions=stated(["diving"]),
        certificates=stated(["padi_idc"]),
        languages=stated(["en"]),
        preferred_countries=stated(["TH"]),
        hiring_country=stated(["TH"]),
    )


def store(session: Session, opportunity: EnrichedOpportunity, status: OpportunityStatus) -> None:
    repository = Repository(session)
    record = repository.save_raw(raw_item(opportunity.normalized.external_id))
    repository.upsert_opportunity(record.id, opportunity, SourceTrust.PRIMARY, status)


def score_shape(
    person: UserProfile, opportunity: EnrichedOpportunity
) -> tuple[float | None, int, list[tuple[str, FactorOutcome, float]]]:
    score = compute_match_score(person, opportunity, now=NOW)
    return (
        score.value,
        score.comparable_factors,
        [(v.factor, v.outcome, v.weight) for v in score.verdicts],
    )


def hiring_evidence(person: UserProfile, opportunity: EnrichedOpportunity) -> str | None:
    score = compute_match_score(person, opportunity, now=NOW)
    return next(v for v in score.verdicts if v.factor == "hiring_country").opportunity_evidence


def test_candidate_scores_like_the_opportunity_it_was_stored_from(session: Session) -> None:
    original = full_opportunity()
    store(session, original, OpportunityStatus.ACTIVE)
    candidate = Repository(session).list_feed_candidates()[0]
    person = scoring_profile()
    # opportunity_fingerprint of a stored candidate may differ from the one at ingest.
    assert score_shape(person, candidate.opportunity) == score_shape(person, original)
    assert candidate.opportunity == original


def test_office_text_is_not_kept_and_only_the_evidence_notices(session: Session) -> None:
    """Not stored in the record; recoverable only by re-normalising the payload, which
    reading the feed deliberately does not do."""
    scope = HiringScope(countries=[], unresolved=["Caribbean"])
    original = full_opportunity(office="Caribbean - Blue Fin", scope=scope)
    store(session, original, OpportunityStatus.ACTIVE)
    candidate = Repository(session).list_feed_candidates()[0]
    person = scoring_profile()
    assert score_shape(person, candidate.opportunity) == score_shape(person, original)
    assert hiring_evidence(person, original) == "Caribbean - Blue Fin"
    assert hiring_evidence(person, candidate.opportunity) is None
    assert candidate.opportunity.normalized.recruitment_office_raw is None


def test_candidate_carries_employer_and_freshness(session: Session) -> None:
    store(session, full_opportunity(), OpportunityStatus.ACTIVE)
    candidate = Repository(session).list_feed_candidates()[0]
    assert candidate.employer == "Blue Fin"
    assert candidate.freshness == NOW


def test_freshness_falls_back_to_first_seen_when_not_posted(session: Session) -> None:
    store(session, enriched(), OpportunityStatus.ACTIVE)
    candidate = Repository(session).list_feed_candidates()[0]
    stored = session.scalars(select(OpportunityRecord)).one()
    assert candidate.freshness == stored.first_seen_at


def test_feed_excludes_rejected_and_source_trashed(session: Session) -> None:
    store(session, full_opportunity("kept"), OpportunityStatus.ACTIVE)
    store(session, full_opportunity("rejected"), OpportunityStatus.REJECTED)
    store(
        session,
        full_opportunity("trashed", notes=[SOURCE_TRASHED_NOTE]),
        OpportunityStatus.ACTIVE,
    )
    candidates = Repository(session).list_feed_candidates()
    assert [c.opportunity.normalized.external_id for c in candidates] == ["kept"]
