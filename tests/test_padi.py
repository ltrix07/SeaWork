import hashlib
from datetime import UTC
from pathlib import Path

import httpx
import pytest
import respx

from seawork.config import Settings
from seawork.domain.enums import SourceTrust
from seawork.domain.models import EnrichedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import check_quality
from seawork.ingestion.sources.registry import build_source
from seawork.ingestion.sources.sites.padi import PadiSource

SNAPSHOT = Path(__file__).parent / "fixtures" / "padi" / "jobs.xml"
REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"
FEED_URL = "https://divejobs.padi.com/?feed=job_feed&sh_atts=job_per_page:300"


def make_source(client: httpx.AsyncClient | None = None) -> PadiSource:
    return PadiSource(
        source_id="padi",
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
            with respx.mock(base_url="https://divejobs.padi.com") as router:
                router.get("/robots.txt").mock(
                    return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
                )
                router.get(url=FEED_URL).mock(
                    return_value=httpx.Response(200, content=SNAPSHOT.read_bytes())
                )
                return [item async for item in source.collect()]

    import asyncio

    return asyncio.run(collect())


@pytest.fixture(scope="module")
def enriched(raw_items: list[RawItem]) -> list[EnrichedOpportunity]:
    source = make_source()
    enricher = RulesEnricher(REFERENCE_DIR, direction="diving", workplace="shore")
    results: list[EnrichedOpportunity] = []
    for raw in raw_items:
        normalized = source.normalize(raw)
        quality = check_quality(normalized, duplicate_content=False)
        classification = classify(normalized, REFERENCE_DIR / "opportunity_types.yaml")
        results.append(enricher.enrich(normalized, classification, quality))
    return results


def test_snapshot_size_and_identifiers(raw_items: list[RawItem]) -> None:
    assert len(raw_items) == 282
    assert len({item.external_id for item in raw_items}) == 282
    assert all(item.content_type == "text/xml" for item in raw_items)


@pytest.mark.asyncio
async def test_registry_builds_padi() -> None:
    settings = Settings(source_registry=Path("sources/registry.yaml"))
    padi = build_source("padi", settings).source
    try:
        assert isinstance(padi, PadiSource)
    finally:
        await padi.close()


def test_normalize_is_offline_and_deterministic(raw_items: list[RawItem]) -> None:
    source = make_source()
    first = raw_items[0]
    assert source.normalize(first) == source.normalize(first)
    assert first.content_hash == hashlib.sha256(first.payload.encode()).hexdigest()
    assert first.fetched_at.tzinfo is UTC


def test_description_has_no_html_left(enriched: list[EnrichedOpportunity]) -> None:
    for item in enriched:
        description = item.normalized.description
        if description is None:
            continue
        assert "<" not in description
        assert ">" not in description
        # A double-escaped source: entities survive the CDATA boundary unresolved and
        # must come out as real characters, not as literal "&#038;" text (§3.4 was
        # documented for `location`; title/description carry the same encoding bug).
        assert "&#" not in description


def test_snapshot_coverage(enriched: list[EnrichedOpportunity]) -> None:
    """Locks in the coverage measured against the 22.09.2026 snapshot, per contract §6.

    Several of these numbers correct the contract's own predictions -- see the report
    handed back with this change for the reasoning. In particular:
    - `country` is 264/282 (93.6%) after countries.yaml learned the 34 nations this
      board posts from. The 11 records with a location but no country state a street
      and a postcode and never name the country ("40 Marsh Street", "Hyatt Place");
      leaving those unknown is correct. `location_raw` is 275/282, matching §3.4.
    - `profession` is filled for 232/282 (82%) after the diving vocabulary was added:
      dive_instructor 166, divemaster 43, dive_technician 7, master 7, retail 6,
      guest_services 3. The seven `master` records are genuine boat captains, from the
      "Boat Captain" sector -- down from 57 wrong ones before the matching fix and the
      vocabulary. What is still unmatched is mostly the "Manager" sector, which has no
      key in professions.yaml for any source.
    - `required_certificates` is 25/282, and deliberately so. The bare `padi` pattern
      matched 206 records, almost all of them the brand in marketing copy, a course
      price or what the centre teaches. Narrowing it to ranks does not help either:
      "Requirements: PADI Instructor certification" is a requirement, "Position: PADI
      Instructor" is a job title, and "We offer: FOC IDC/OWSI training" is the opposite
      of one. That distinction is semantic, and the LLM enricher is the layer measured
      on it. What remains here is what the rules can state plainly: passport 18, ssi 5,
      stcw 2.
    - parsed `salary` is 55/82 of the raw values. The 27 left alone are 24 reading
      "Negotiable", which states no number, and 3 in "Rs" - the rupee of India, Sri
      Lanka and Pakistan alike, and this corpus holds both Indian and Sri Lankan dive
      centres. Forty of the 55 carry confidence 0.6 rather than 1.0: a bare "$" is
      read as USD, and these postings sit in Greece and Italy as often as in the
      United States.
    """
    total = len(enriched)
    assert total == 282
    assert sum(item.normalized.title != "" for item in enriched) == total
    assert sum(item.normalized.posted_at is not None for item in enriched) == total
    assert sum(item.normalized.employer is not None for item in enriched) == total
    # Two image-only postings' descriptions strip to nothing; description is not the
    # 100% the contract predicted from "the field is always present in the feed".
    assert sum(item.normalized.description is not None for item in enriched) == 276
    assert sum(item.normalized.location_raw is not None for item in enriched) == 275
    assert sum(item.normalized.salary_raw is not None for item in enriched) == 82
    assert sum(item.normalized.declared_type is not None for item in enriched) == 268
    assert sum(item.country is not None for item in enriched) == 264
    assert sum(item.profession is not None for item in enriched) == 232
    assert sum(1 for item in enriched if item.profession and item.profession.value == "master") == 7
    assert sum(item.required_certificates is not None for item in enriched) == 25
    assert sum(item.salary is not None for item in enriched) == 55
    # The currency is a guess only where the symbol is bare; CA$, R$, Rp, SR, THB and
    # EUR name themselves and keep full confidence.
    assert sum(1 for item in enriched if item.salary and item.salary.confidence == 0.6) == 40
    assert sum(item.experience_level is not None for item in enriched) == 40
    assert sum(item.required_languages is not None for item in enriched) == 122
    assert sum(item.preferred_languages is not None for item in enriched) == 23
    # embarkation_port/operating_region are the vessel-side geography fields (ingestion
    # 4.3.1); PADI is the first shore source and must leave them empty, not guess.
    assert sum(item.embarkation_port is not None for item in enriched) == 0
    assert sum(item.operating_region is not None for item in enriched) == 0
    assert (
        sum(
            item.workplace_type_hint is not None and item.workplace_type_hint.value == "shore"
            for item in enriched
        )
        == total
    )


def test_trashed_records_are_kept_and_flagged(enriched: list[EnrichedOpportunity]) -> None:
    """§3.3: __trashed posts stay live in the feed and must not be dropped silently."""
    trashed = [item for item in enriched if item.normalized.source_fields.get("trashed") == "true"]
    assert len(trashed) == 17
    for item in trashed:
        assert "__trashed" in str(item.normalized.url)
        assert item.normalized.title
    not_trashed = [
        item for item in enriched if item.normalized.source_fields.get("trashed") == "false"
    ]
    assert len(not_trashed) == 282 - 17


def test_recuiter_job_number_is_read_by_its_misspelled_name(raw_items: list[RawItem]) -> None:
    """§3.2: the source's own field is `RecuiterJobNumber`, and it is unique per item."""
    ids = [item.external_id for item in raw_items]
    assert len(ids) == len(set(ids)) == 282
    assert all(external_id.isdigit() for external_id in ids)


@pytest.mark.asyncio
async def test_missing_recuiter_job_number_fails_loudly() -> None:
    """§3.2: a record without the field must break collection, not vanish quietly."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
<title><![CDATA[Missing identifier]]></title>
<link><![CDATA[https://divejobs.padi.com/job/missing-id/]]></link>
<PostDate>Mon, 01 Jan 2024 00:00:00 +0000</PostDate>
<expiryDate>Mon, 01 Jan 2030 00:00:00 +0000</expiryDate>
<featured><![CDATA[no]]></featured>
<salary><![CDATA[]]></salary>
<employer><![CDATA[Test Dive Centre]]></employer>
<location><![CDATA[Thailand]]></location>
<sector><![CDATA[PADI Divemaster]]></sector>
<type><![CDATA[Full time]]></type>
<description><![CDATA[<p>Test description.</p>]]></description>
</item>
</channel></rss>"""
    async with httpx.AsyncClient() as client:
        source = make_source(client)
        with respx.mock(base_url="https://divejobs.padi.com") as router:
            router.get("/robots.txt").mock(
                return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
            )
            router.get(url=FEED_URL).mock(return_value=httpx.Response(200, text=xml))
            with pytest.raises(ValueError, match="RecuiterJobNumber"):
                async for _ in source.collect():
                    pass


def _sector_item(sector: str) -> RawItem:
    xml = f"""<item>
<RecuiterJobNumber><![CDATA[999999]]></RecuiterJobNumber>
<title><![CDATA[Test Role]]></title>
<link><![CDATA[https://divejobs.padi.com/job/test-role/]]></link>
<PostDate>Mon, 01 Jan 2024 00:00:00 +0000</PostDate>
<expiryDate>Mon, 01 Jan 2030 00:00:00 +0000</expiryDate>
<featured><![CDATA[no]]></featured>
<salary><![CDATA[]]></salary>
<employer><![CDATA[Test Dive Centre]]></employer>
<location><![CDATA[Thailand]]></location>
<sector><![CDATA[{sector}]]></sector>
<type><![CDATA[Full time]]></type>
<description><![CDATA[<p>Test description.</p>]]></description>
</item>"""
    return RawItem.model_validate(
        {
            "source_id": "padi",
            "external_id": "999999",
            "url": "https://divejobs.padi.com/job/test-role/",
            "fetched_at": "2026-09-22T00:00:00Z",
            "payload": xml,
            "content_type": "text/xml",
            "content_hash": hashlib.sha256(xml.encode()).hexdigest(),
            "http_status": 200,
        }
    )


def test_sector_numeric_prefix_is_stripped_before_matching() -> None:
    """§3.1: the leading "N. " is a retired numbering scheme, not a priority or code.

    professions.yaml has no diving vocabulary yet (§5, deferred to after this
    measurement), so profession itself stays None for both variants; the trap this
    guards against is `source_fields["sector"]` carrying the dead prefix into
    whatever consumes it later, prefixed and unprefixed forms must be identical.
    """
    source = make_source()
    prefixed = source.normalize(_sector_item("1. Teaching / Guiding"))
    unprefixed = source.normalize(_sector_item("Teaching / Guiding"))
    assert prefixed.source_fields["sector"] == "Teaching / Guiding"
    assert prefixed.source_fields["sector"] == unprefixed.source_fields["sector"]


def test_location_country_is_stable_across_segment_count(
    raw_items: list[RawItem],
) -> None:
    """§3.4: the first comma segment is the country, however many segments follow.

    Deliberately not using record 762135 ("Indonesia, Jl Silayukti 6, Padangbai,
    80871, Manggis, Karangasem, Bali, Indonesia", 8 segments) here: `RulesEnricher.
    _country` matches any country name as a bare substring of the whole string, in
    file order, not the first segment specifically -- and "Silayukti" contains "uk",
    which is one of GB's aliases and sits earlier in countries.yaml than ID. That
    record resolves to "GB", not "ID". This is a real defect in the shared, generic
    `_country` matcher (not touched here per the task's scope), exposed by PADI
    because it is the first source with real free-text street addresses; it is
    reported separately rather than routed around by picking a location where it
    happens not to fire.
    """
    source = make_source()
    enricher = RulesEnricher(REFERENCE_DIR, direction="diving", workplace="shore")
    one_segment = next(item for item in raw_items if item.external_id == "747120")
    five_segments = next(item for item in raw_items if item.external_id == "758895")
    assert source.normalize(one_segment).location_raw == "Indonesia"
    normalized_five = source.normalize(five_segments)
    assert normalized_five.location_raw == "Indonesia, Buleleng, Bali, Indonesia, 81119"

    def country(item: RawItem) -> str | None:
        normalized = source.normalize(item)
        classification = classify(normalized, REFERENCE_DIR / "opportunity_types.yaml")
        quality = check_quality(normalized, duplicate_content=False)
        result = enricher.enrich(normalized, classification, quality)
        return result.country.value if result.country else None

    assert country(one_segment) == "ID"
    assert country(five_segments) == "ID"


def test_na_placeholder_is_dropped_from_location(raw_items: list[RawItem]) -> None:
    """§3.4: the source's literal "NA" segment is a placeholder, not a place."""
    source = make_source()
    item = next(item for item in raw_items if item.external_id == "768644")
    normalized = source.normalize(item)
    assert normalized.location_raw == "Dominican Republic, EPS R-1543"
