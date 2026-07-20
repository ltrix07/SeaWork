import hashlib
from datetime import UTC
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from seawork.config import Settings
from seawork.domain.enums import Provenance, SourceTrust
from seawork.domain.models import EnrichedOpportunity, SalaryRange
from seawork.ingestion.base import RawItem
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.rules import RulesEnricher, parse_salary
from seawork.ingestion.quality import check_quality
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource
from seawork.ingestion.sources.registry import build_source, load_registry
from seawork.ingestion.sources.sites.crewplanet import CrewplanetSource

SNAPSHOT = Path(__file__).parent / "fixtures" / "crewplanet" / "vacancies.xml"
REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"


def make_source(client: httpx.AsyncClient | None = None) -> CrewplanetSource:
    return CrewplanetSource(
        source_id="crewplanet",
        trust=SourceTrust.PRIMARY,
        user_agent="SeaWork test (contact: test@example.com)",
        interval_seconds=0,
        client=client,
    )


@pytest.fixture(scope="module")
def raw_items() -> list[RawItem]:
    async def collect() -> list[RawItem]:
        async with httpx.AsyncClient() as client:
            source = make_source(client)
            with respx.mock(base_url="https://www.crewplanet.eu") as router:
                router.get("/robots.txt").mock(
                    return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
                )
                router.get("/v2/en/vacancies/feed/rss").mock(
                    return_value=httpx.Response(200, content=SNAPSHOT.read_bytes())
                )
                return [item async for item in source.collect()]

    import asyncio

    return asyncio.run(collect())


@pytest.fixture(scope="module")
def enriched(raw_items: list[RawItem]) -> list[EnrichedOpportunity]:
    source = make_source()
    enricher = RulesEnricher(REFERENCE_DIR, direction="general_maritime", workplace="vessel")
    results: list[EnrichedOpportunity] = []
    for raw in raw_items:
        normalized = source.normalize(raw)
        quality = check_quality(normalized, duplicate_content=False)
        classification = classify(normalized, REFERENCE_DIR / "opportunity_types.yaml")
        results.append(enricher.enrich(normalized, classification, quality))
    return results


def test_snapshot_size_and_identifiers(raw_items: list[RawItem]) -> None:
    assert len(raw_items) == 158
    assert len({item.external_id for item in raw_items}) == 158
    assert all(item.content_type == "text/xml" for item in raw_items)


@pytest.mark.asyncio
async def test_registry_builds_both_sources() -> None:
    settings = Settings(source_registry=Path("sources/registry.yaml"))
    configs = load_registry(settings.source_registry)
    crewplanet_config = next(row for row in configs if row.source_id == "crewplanet")
    assert crewplanet_config.account is None
    crewplanet = build_source("crewplanet", settings).source
    pinpoint = build_source("pinpoint:hollandamericagroup", settings).source
    try:
        assert isinstance(crewplanet, CrewplanetSource)
        assert isinstance(pinpoint, PinpointSource)
    finally:
        await crewplanet.close()
        await pinpoint.close()


def test_double_salary_uses_structured_tail(enriched: list[EnrichedOpportunity]) -> None:
    item = next(row for row in enriched if row.normalized.external_id == "79130")
    assert item.normalized.salary_raw == "8 000 USD"
    assert item.normalized.description
    assert "Salary: Competitive, based on experience." in item.normalized.description
    assert "Join date:" not in item.normalized.description
    assert "cv12879130@gmail.com" not in item.normalized.description


@pytest.mark.parametrize(
    ("raw", "minimum", "maximum", "currency", "period", "confidence"),
    [
        ("8 000 USD", Decimal("8000"), Decimal("8000"), "USD", "month", 0.8),
        ("12 600 - 13 100 USD", Decimal("12600"), Decimal("13100"), "USD", "month", 0.8),
        ("350 USD per day", Decimal("350"), Decimal("350"), "USD", "day", 1.0),
        ("205 EUR per day", Decimal("205"), Decimal("205"), "EUR", "day", 1.0),
        ("up to 7 000 EUR", None, Decimal("7000"), "EUR", "month", 0.8),
    ],
)
def test_salary_formats(
    raw: str,
    minimum: Decimal | None,
    maximum: Decimal,
    currency: str,
    period: str,
    confidence: float,
) -> None:
    inferred = parse_salary(raw)
    assert inferred is not None
    assert inferred.value == SalaryRange(
        minimum=minimum, maximum=maximum, currency=currency, period=period
    )
    assert inferred.provenance is Provenance.RULE
    assert inferred.confidence == confidence


def test_unrecognized_salary_stays_unknown() -> None:
    assert parse_salary("Competitive") is None


def test_snapshot_coverage(enriched: list[EnrichedOpportunity]) -> None:
    total = len(enriched)
    assert total == 158
    assert sum(item.normalized.posted_at is not None for item in enriched) == total
    assert sum(item.salary is not None for item in enriched) == 158
    assert sum(item.direction is not None for item in enriched) == total
    assert sum(item.profession is not None for item in enriched) == 158
    assert sum(item.required_certificates is not None for item in enriched) == 76
    assert sum(item.experience_level is not None for item in enriched) == 47
    assert sum(item.country is not None for item in enriched) == 0


def test_normalize_is_offline_and_deterministic(raw_items: list[RawItem]) -> None:
    source = make_source()
    first = raw_items[0]
    assert source.normalize(first) == source.normalize(first)
    assert first.content_hash == hashlib.sha256(first.payload.encode()).hexdigest()
    assert first.fetched_at.tzinfo is UTC
