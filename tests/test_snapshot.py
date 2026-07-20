"""Checks against full snapshots of the live Pinpoint API.

These tests are not about code coverage. They guard against silent degradation of
parsing and enrichment when the source format changes, and they pin the observed
fill rates: a mismatch means a regression, not a reason to edit the numbers.

Every trap runs against both accounts. That is the whole claim of a platform
adapter — the contract holds for any employer on Pinpoint, not just the one it
was written against — and an assertion that only ever sees one account cannot
tell whether the claim is true.
"""

import datetime
import hashlib
import json
from functools import cache
from pathlib import Path
from typing import cast

import pytest

from seawork.domain.enums import ExperienceLevel, Provenance, SourceTrust
from seawork.domain.models import EnrichedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import check_quality
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource, canonical_json

FIXTURES = Path(__file__).parent / "fixtures" / "pinpoint"
REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"
CLASSIFICATION_RULES = REFERENCE_DIR / "opportunity_types.yaml"

# Fill rates measured on the snapshots and pinned deliberately. Where the two
# accounts differ sharply the reason is known and documented:
# profession is lower on Princess because the reference files grew out of Holland
# America's department taxonomy (docs/sources-research.md, section on B3).
ACCOUNTS: dict[str, dict[str, int]] = {
    "hollandamericagroup": {
        "total": 93,
        "profession": 92,
        "experience_level": 66,
        "required_certificates": 45,
    },
    "princesscruises": {
        "total": 354,
        "profession": 348,
        "experience_level": 124,
        "required_certificates": 80,
    },
}


def _raw(account: str, posting: dict[str, object]) -> RawItem:
    payload = canonical_json(posting)
    return RawItem.model_validate(
        {
            "source_id": f"pinpoint:{account}",
            "external_id": str(posting["id"]),
            "url": str(posting["url"]),
            "fetched_at": datetime.datetime.now(datetime.UTC),
            "payload": payload,
            "content_type": "application/json",
            "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "http_status": 200,
        }
    )


@cache
def _postings(account: str) -> tuple[dict[str, object], ...]:
    document = cast(dict[str, object], json.loads((FIXTURES / f"{account}.json").read_text()))
    return tuple(cast(list[dict[str, object]], document["data"]))


@cache
def _enriched(account: str) -> tuple[EnrichedOpportunity, ...]:
    source = PinpointSource(
        account=account,
        source_id=f"pinpoint:{account}",
        trust=SourceTrust.PRIMARY,
        user_agent="SeaWork test (contact: test@example.com)",
        interval_seconds=0,
    )
    enricher = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    results: list[EnrichedOpportunity] = []
    for posting in _postings(account):
        normalized = source.normalize(_raw(account, posting))
        results.append(
            enricher.enrich(
                normalized,
                classify(normalized, CLASSIFICATION_RULES),
                check_quality(normalized, duplicate_content=False),
            )
        )
    return tuple(results)


accounts = pytest.mark.parametrize("account", sorted(ACCOUNTS))


@accounts
def test_snapshot_size(account: str) -> None:
    assert len(_postings(account)) == ACCOUNTS[account]["total"]


@accounts
def test_recruitment_office_never_becomes_job_location(account: str) -> None:
    """Pinpoint trap (contract 3.1): location is the hiring office, not the workplace.

    The source fills location on 100% of records, so mapping it into country/city is
    tempting. Doing so would produce confidently wrong geography: a cook posting with
    a Mumbai office is shipboard work.
    """
    for item in _enriched(account):
        assert item.country is None
        assert item.city is None
        # The office itself is not lost; it lives in a dedicated field.
        assert item.normalized.recruitment_office_raw


@accounts
def test_posted_at_absent(account: str) -> None:
    """Pinpoint exposes no publication timestamp (contract 3.2)."""
    for item in _enriched(account):
        assert item.normalized.posted_at is None


@accounts
def test_certificates_absent_means_unknown(account: str) -> None:
    """Not found means None, never an empty list (ingestion 3.3).

    An empty list at confidence 0.9 would read as "no certificates required" and let
    Match Score build a false explanation per SPEC 10.7.
    """
    for item in _enriched(account):
        certificates = item.required_certificates
        if certificates is not None:
            assert certificates.value, "an empty result must be represented as None"
            assert certificates.provenance is Provenance.RULE


@accounts
def test_experience_ignores_recertification_periods(account: str) -> None:
    """A bare "N years" does not count; it must sit next to the word experience.

    Qualification text also carries recertification cycles such as
    "Basic Food Hygiene course every 2 years", which are not required tenure.
    """
    for item in _enriched(account):
        experience = item.experience_level
        if experience is None:
            continue
        assert isinstance(experience.value, ExperienceLevel)
        assert "experience" in RulesEnricher.qualification_text(item.normalized).lower()


@accounts
def test_coverage_matches_contract(account: str) -> None:
    """Fill rates pinned from docs/modules/source-pinpoint.md section 6."""
    enriched = _enriched(account)
    expected = ACCOUNTS[account]
    # Neither account states a country, a salary or a publication date. That is a
    # property of shipboard hiring, not a parsing defect — see the contract.
    assert sum(item.country is not None for item in enriched) == 0
    assert sum(item.salary is not None for item in enriched) == 0
    assert sum(item.direction is not None for item in enriched) == len(enriched)
    assert sum(item.profession is not None for item in enriched) == expected["profession"]
    assert (
        sum(item.experience_level is not None for item in enriched) == expected["experience_level"]
    )
    assert (
        sum(item.required_certificates is not None for item in enriched)
        == expected["required_certificates"]
    )


@accounts
def test_normalize_is_deterministic(account: str) -> None:
    """The same payload must always yield the same content hash (3.6)."""
    postings = _postings(account)
    source = PinpointSource(
        account=account,
        source_id=f"pinpoint:{account}",
        trust=SourceTrust.PRIMARY,
        user_agent="SeaWork test (contact: test@example.com)",
        interval_seconds=0,
    )
    assert [canonical_json(p) for p in postings] == [canonical_json(p) for p in postings]
    identifiers = {source.normalize(_raw(account, p)).external_id for p in postings}
    assert len(identifiers) == ACCOUNTS[account]["total"]
