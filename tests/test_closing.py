"""Closing vacancies that left their source, and what must never close them."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import typer
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from seawork.cli import _show_result  # pyright: ignore[reportPrivateUsage]
from seawork.domain.enums import OpportunityStatus, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.pipeline import IngestionPipeline, PipelineResult
from seawork.ingestion.sources.sites.padi import PadiSource
from seawork.reporting.coverage import build_coverage_reports
from seawork.storage.models import OpportunityRecord
from seawork.storage.repository import Repository

REFERENCE_DIR = Path("data/reference")
SNAPSHOT = Path("tests/fixtures/padi/jobs.xml")
LONG_TEXT = "Looks after guests and crew on board. " * 5
FEED_BODY = SNAPSHOT.read_bytes()


def text_for(external_id: str) -> str:
    return f"{external_id}: {LONG_TEXT}"


SHORT_TEXT = "too short"


@pytest.fixture
def test_schema() -> str:
    return "seawork_closing_test"


class Boom(Exception):
    pass


class FakeSource:
    source_id = "fake"
    trust = SourceTrust.PRIMARY

    def __init__(self, feed: dict[str, str], fail_after: int | None = None) -> None:
        self.feed = feed
        self.fail_after = fail_after

    async def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        del since
        for index, (external_id, text) in enumerate(self.feed.items()):
            if self.fail_after is not None and index >= self.fail_after:
                raise Boom
            yield RawItem(
                source_id=self.source_id,
                external_id=external_id,
                url=HttpUrl(f"https://example.com/job/{external_id}"),
                fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
                payload=text,
                content_type="text/plain",
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                http_status=200,
            )

    def normalize(self, item: RawItem) -> NormalizedOpportunity:
        return NormalizedOpportunity(
            source_id=item.source_id,
            external_id=item.external_id,
            url=item.url,
            title=f"Vacancy {item.external_id}",
            description=item.payload,
        )

    def quality_notes(self, opportunity: NormalizedOpportunity) -> tuple[str, ...]:
        return ()

    async def close(self) -> None:
        return None


def pipeline(session: Session, source: FakeSource) -> IngestionPipeline:
    return IngestionPipeline(
        Repository(session),
        source,
        RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="ship"),
        REFERENCE_DIR / "opportunity_types.yaml",
    )


def feed(*external_ids: str) -> dict[str, str]:
    return {external_id: text_for(external_id) for external_id in external_ids}


def record(session: Session, external_id: str) -> OpportunityRecord:
    session.expire_all()
    return session.scalars(
        select(OpportunityRecord).where(OpportunityRecord.external_id == external_id)
    ).one()


async def seed(session: Session, *external_ids: str) -> None:
    await pipeline(session, FakeSource(feed(*external_ids))).run()


async def test_run_without_a_vacancy_closes_it(session: Session) -> None:
    await seed(session, "1", "2", "3")

    result = await pipeline(session, FakeSource(feed("1", "2"))).run()

    assert result.closed == 1
    assert result.closing_refused is None
    assert record(session, "3").status == OpportunityStatus.CLOSED.value
    assert record(session, "3").closed_at is not None
    assert record(session, "1").status == OpportunityStatus.ACTIVE.value


async def test_run_that_sees_a_closed_vacancy_revives_it(session: Session) -> None:
    await seed(session, "1", "2", "3")
    await pipeline(session, FakeSource(feed("1", "2"))).run()

    await pipeline(session, FakeSource(feed("1", "2", "3"))).run()

    revived = record(session, "3")
    assert revived.status == OpportunityStatus.ACTIVE.value
    assert revived.closed_at is None


async def test_repeated_run_does_not_move_closed_at(session: Session) -> None:
    await seed(session, "1", "2", "3")
    await pipeline(session, FakeSource(feed("1", "2"))).run()
    first = record(session, "3").closed_at

    result = await pipeline(session, FakeSource(feed("1", "2"))).run()

    assert result.closed == 0
    assert record(session, "3").closed_at == first


async def test_limited_run_closes_nothing(session: Session) -> None:
    await seed(session, "1", "2", "3")

    result = await pipeline(session, FakeSource(feed("1", "2", "3"))).run(limit=1)

    assert result.closed == 0
    assert result.closing_refused is None
    assert record(session, "2").status == OpportunityStatus.ACTIVE.value
    assert record(session, "3").status == OpportunityStatus.ACTIVE.value


async def test_failure_midway_closes_nothing(session: Session) -> None:
    await seed(session, "1", "2", "3", "4")

    with pytest.raises(Boom):
        await pipeline(session, FakeSource(feed("1", "2", "3"), fail_after=2)).run()

    assert record(session, "4").status == OpportunityStatus.ACTIVE.value


async def test_empty_feed_closes_nothing_and_says_so(session: Session) -> None:
    await seed(session, "1", "2")

    result = await pipeline(session, FakeSource({})).run()

    assert result.closed == 0
    assert result.closing_refused is not None
    assert record(session, "1").status == OpportunityStatus.ACTIVE.value


async def test_feed_below_the_threshold_closes_nothing_and_says_so(session: Session) -> None:
    await seed(session, "1", "2", "3", "4", "5")

    result = await pipeline(session, FakeSource(feed("1", "2"))).run()

    assert result.closed == 0
    assert result.closing_refused is not None
    assert record(session, "5").status == OpportunityStatus.ACTIVE.value


async def test_empty_feed_of_a_source_without_active_records_is_refused(
    session: Session,
) -> None:
    result = await pipeline(session, FakeSource({})).run()

    assert result.closed == 0
    assert result.closing_refused is not None


async def test_new_vacancies_do_not_help_a_truncated_feed_pass(session: Session) -> None:
    await seed(session, "1", "2", "3", "4")

    result = await pipeline(session, FakeSource(feed("1", "n1", "n2", "n3", "n4"))).run()

    assert result.closed == 0
    assert result.closing_refused is not None
    assert record(session, "4").status == OpportunityStatus.ACTIVE.value


async def test_feed_at_the_threshold_still_closes(session: Session) -> None:
    await seed(session, "1", "2", "3", "4")

    result = await pipeline(session, FakeSource(feed("1", "2"))).run()

    assert result.closed == 2


async def test_reprocess_does_not_revive_a_closed_vacancy(session: Session) -> None:
    await seed(session, "1", "2", "3")
    await pipeline(session, FakeSource(feed("1", "2"))).run()
    closed_at = record(session, "3").closed_at

    pipeline(session, FakeSource({})).reprocess()

    gone = record(session, "3")
    assert gone.status == OpportunityStatus.CLOSED.value
    assert gone.closed_at == closed_at
    assert record(session, "1").status == OpportunityStatus.ACTIVE.value


async def test_rejected_vacancy_is_not_closed(session: Session) -> None:
    await pipeline(
        session, FakeSource({"1": text_for("1"), "2": text_for("2"), "3": SHORT_TEXT})
    ).run()
    assert record(session, "3").status == OpportunityStatus.REJECTED.value

    await pipeline(session, FakeSource(feed("1", "2"))).run()

    rejected = record(session, "3")
    assert rejected.status == OpportunityStatus.REJECTED.value
    assert rejected.closed_at is None


async def test_closed_vacancy_is_not_a_feed_candidate(session: Session) -> None:
    await seed(session, "1", "2", "3")
    await pipeline(session, FakeSource(feed("1", "2"))).run()

    ids = {
        candidate.opportunity.normalized.external_id
        for candidate in Repository(session).list_feed_candidates()
    }

    assert ids == {"1", "2"}


async def test_coverage_counts_closed_but_not_rejected(session: Session) -> None:
    await pipeline(session, FakeSource({**feed("1", "2", "3"), "4": SHORT_TEXT})).run()
    await pipeline(session, FakeSource({**feed("1", "2"), "4": SHORT_TEXT})).run()

    (report,) = build_coverage_reports(session, "fake")

    assert report.published == 3
    assert report.rejected == 1


def test_refused_closing_makes_the_command_fail() -> None:
    with pytest.raises(typer.Exit) as raised:
        _show_result(PipelineResult(fetched=1, processed=1, closing_refused="truncated"))

    assert raised.value.exit_code == 1


async def test_padi_fails_on_a_feed_that_may_be_truncated() -> None:
    async with httpx.AsyncClient() as client:
        source = PadiSource(
            source_id="padi",
            trust=SourceTrust.PRIMARY,
            user_agent="SeaWork test (contact: test@example.com)",
            interval_seconds=0,
            job_per_page=282,
            client=client,
        )
        with respx.mock(base_url="https://divejobs.padi.com") as router:
            router.get("/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
            )
            router.get(
                url="https://divejobs.padi.com/?feed=job_feed&sh_atts=job_per_page:282"
            ).mock(return_value=httpx.Response(200, content=FEED_BODY))
            with pytest.raises(ValueError, match="truncated"):
                _ = [item async for item in source.collect()]
