import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx

from seawork.config import Settings
from seawork.domain.enums import SourceTrust
from seawork.ingestion.base import RawItem
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource, canonical_json
from seawork.ingestion.sources.registry import build_source, load_registry


def make_source(
    client: httpx.AsyncClient | None = None, employer: str | None = None
) -> PinpointSource:
    return PinpointSource(
        account="hollandamericagroup",
        source_id="pinpoint:hollandamericagroup",
        trust=SourceTrust.PRIMARY,
        user_agent="SeaWork test (contact: test@example.com)",
        interval_seconds=0,
        employer=employer,
        client=client,
    )


def make_raw(posting: dict[str, object]) -> RawItem:
    payload = canonical_json(posting)
    return RawItem.model_validate(
        {
            "source_id": "pinpoint:hollandamericagroup",
            "external_id": str(posting["id"]),
            "url": str(posting["url"]),
            "fetched_at": datetime.now(UTC),
            "payload": payload,
            "content_type": "application/json",
            "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "http_status": 200,
        }
    )


def test_registry_employer_fills_a_posting_without_one(
    pinpoint_posting: dict[str, object],
) -> None:
    job = cast(dict[str, object], pinpoint_posting["job"])
    posting = {**pinpoint_posting, "job": {**job, "structure_custom_group_one": None}}
    normalized = make_source(employer="Princess Cruises").normalize(make_raw(posting))
    assert normalized.employer == "Princess Cruises"
    assert make_source().normalize(make_raw(posting)).employer is None


async def test_registry_gives_princess_its_employer() -> None:
    settings = Settings(source_registry=Path("sources/registry.yaml"))
    config = next(
        row
        for row in load_registry(settings.source_registry)
        if row.source_id == "pinpoint:princesscruises"
    )
    assert config.employer == "Princess Cruises"
    source = build_source("pinpoint:princesscruises", settings).source
    try:
        assert isinstance(source, PinpointSource)
        assert source.employer == "Princess Cruises"
    finally:
        await source.close()


def test_employer_in_the_posting_beats_the_registry(
    pinpoint_posting: dict[str, object],
) -> None:
    normalized = make_source(employer="Princess Cruises").normalize(make_raw(pinpoint_posting))
    assert normalized.employer == "Seabourn"


def test_normalize_keeps_recruitment_office_out_of_work_location(
    pinpoint_posting: dict[str, object],
) -> None:
    normalized = make_source().normalize(make_raw(pinpoint_posting))
    assert normalized.recruitment_office_raw == "India - CSSI"
    assert normalized.location_raw is None
    assert normalized.posted_at is None
    assert normalized.employer == "Seabourn"
    assert normalized.description and "Responsibilities" in normalized.description


@pytest.mark.asyncio
async def test_collect_uses_one_postings_request_and_ignores_wrong_content_type(
    pinpoint_posting: dict[str, object],
) -> None:
    async with httpx.AsyncClient() as client:
        source = make_source(client)
        with respx.mock(
            base_url="https://hollandamericagroup.pinpointhq.com", assert_all_called=False
        ) as router:
            router.get("/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
            )
            postings = router.get("/postings.json").mock(
                return_value=httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    json={"data": [pinpoint_posting]},
                )
            )
            items = [item async for item in source.collect()]
        assert len(items) == 1
        assert postings.call_count == 1
        assert json.loads(items[0].payload)["id"] == "43999"


@pytest.mark.asyncio
async def test_collect_honours_robots_txt() -> None:
    async with httpx.AsyncClient() as client:
        source = make_source(client)
        with respx.mock(
            base_url="https://hollandamericagroup.pinpointhq.com", assert_all_called=False
        ) as router:
            router.get("/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nDisallow: /postings.json")
            )
            postings = router.get("/postings.json")
            with pytest.raises(PermissionError):
                _ = [item async for item in source.collect()]
        assert postings.call_count == 0
