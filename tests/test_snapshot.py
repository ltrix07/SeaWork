"""Checks against a full snapshot of the live Pinpoint API (93 postings).

These tests are not about code coverage. They guard against silent degradation of
parsing and enrichment when the source format changes, and they pin the observed
fill rates: a mismatch means a regression, not a reason to edit the numbers.
"""

import json
from pathlib import Path
from typing import cast

import pytest

from seawork.domain.enums import ExperienceLevel, Provenance
from seawork.domain.models import EnrichedOpportunity
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import check_quality
from seawork.ingestion.sources.platforms.pinpoint import canonical_json
from tests.test_pinpoint import make_raw, make_source

SNAPSHOT = Path(__file__).parent / "fixtures" / "pinpoint" / "hollandamericagroup.json"
REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"
CLASSIFICATION_RULES = REFERENCE_DIR / "opportunity_types.yaml"


@pytest.fixture(scope="module")
def postings() -> list[dict[str, object]]:
    document = cast(dict[str, object], json.loads(SNAPSHOT.read_text()))
    return cast(list[dict[str, object]], document["data"])


@pytest.fixture(scope="module")
def enriched(postings: list[dict[str, object]]) -> list[EnrichedOpportunity]:
    source = make_source()
    enricher = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    results: list[EnrichedOpportunity] = []
    for posting in postings:
        normalized = source.normalize(make_raw(posting))
        quality = check_quality(normalized, duplicate_content=False)
        classification = classify(normalized, CLASSIFICATION_RULES)
        results.append(enricher.enrich(normalized, classification, quality))
    return results


def test_snapshot_size(postings: list[dict[str, object]]) -> None:
    assert len(postings) == 93


def test_recruitment_office_never_becomes_job_location(
    enriched: list[EnrichedOpportunity],
) -> None:
    """Pinpoint trap (contract 3.1): location is the hiring office, not the workplace.

    The source fills location on 100% of records, so mapping it into country/city is
    tempting. Doing so would produce confidently wrong geography: a cook posting with
    a Mumbai office is shipboard work.
    """
    for item in enriched:
        assert item.country is None
        assert item.city is None
        # The office itself is not lost; it lives in a dedicated field.
        assert item.normalized.recruitment_office_raw


def test_posted_at_absent(enriched: list[EnrichedOpportunity]) -> None:
    """Pinpoint exposes no publication timestamp (contract 3.2)."""
    for item in enriched:
        assert item.normalized.posted_at is None


def test_certificates_absent_means_unknown(enriched: list[EnrichedOpportunity]) -> None:
    """Not found means None, never an empty list (ingestion 3.3).

    An empty list at confidence 0.9 would read as "no certificates required" and let
    Match Score build a false explanation per SPEC 10.7.
    """
    for item in enriched:
        certificates = item.required_certificates
        if certificates is not None:
            assert certificates.value, "an empty result must be represented as None"
            assert certificates.provenance is Provenance.RULE


def test_experience_ignores_recertification_periods(
    enriched: list[EnrichedOpportunity],
) -> None:
    """A bare "N years" does not count; it must sit next to the word experience.

    Qualification text also carries recertification cycles such as
    "Basic Food Hygiene course every 2 years", which are not required tenure.
    """
    for item in enriched:
        experience = item.experience_level
        if experience is None:
            continue
        assert isinstance(experience.value, ExperienceLevel)
        text = item.normalized.source_fields["skills_knowledge_expertise"] or ""
        assert "experience" in text.lower()


def test_coverage_matches_contract(enriched: list[EnrichedOpportunity]) -> None:
    """Fill rates pinned from docs/modules/source-pinpoint.md section 6."""
    total = len(enriched)
    assert sum(item.country is not None for item in enriched) == 0
    assert sum(item.salary is not None for item in enriched) == 0
    assert sum(item.direction is not None for item in enriched) == total
    assert sum(item.profession is not None for item in enriched) == 88
    assert sum(item.experience_level is not None for item in enriched) == 66
    assert sum(item.required_certificates is not None for item in enriched) == 24


def test_normalize_is_deterministic(postings: list[dict[str, object]]) -> None:
    """The same payload must always yield the same content hash (3.6)."""
    source = make_source()
    assert [canonical_json(p) for p in postings] == [canonical_json(p) for p in postings]
    assert len({source.normalize(make_raw(p)).external_id for p in postings}) == 93
