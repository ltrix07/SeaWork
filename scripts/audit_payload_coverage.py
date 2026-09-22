"""Find reference keys present in the raw payload but lost before enrichment.

A low coverage number has two possible causes: the source does not state the
fact, or we are not reading the field that states it. Those look identical in a
coverage report and mean opposite things. This script separates them by matching
the reference patterns twice — once against the raw payload, once against the
text the enricher actually receives — and reporting the difference.

Anything listed here is our defect, not a property of the source.

Usage:  .venv/bin/python scripts/audit_payload_coverage.py
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

REFERENCE_DIR = ROOT / "data" / "reference"


def strip_markup(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))


def matched_keys(text: str) -> set[str]:
    rows = yaml.safe_load((REFERENCE_DIR / "certificates.yaml").read_text())["certificates"]
    return {
        row["key"]
        for row in rows
        if any(
            re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", text, re.IGNORECASE)
            for pattern in row["patterns"]
        )
    }


# Keys that appear in the payload but must NOT reach enrichment, with the reason.
# Without this list every run reports the same expected noise, and a real defect
# hides among lines everyone has learned to skip.
EXPECTED_LOSSES: dict[str, dict[str, str]] = {
    "pinpoint:hollandamericagroup": {
        "usph": "duty text: 'in accordance with USPH standards', not a held document",
        "haccp": "duty text: 'following HACCP guidelines'",
        "food_hygiene": "recertification cycle: 'Food Hygiene course every 2 years'",
        "coc": "pointer to an internal document (HRP-1200) we cannot resolve",
    },
    "pinpoint:princesscruises": {
        "usph": "duty text: 'in accordance with USPH standards', not a held document",
        "haccp": "duty text: 'following HACCP guidelines'",
        "food_hygiene": "recertification cycle: 'Food Hygiene course every 2 years'",
        "coc": "pointer to an internal document we cannot resolve",
    },
    "crewplanet": {},
    "padi": {
        # The `padi` key has no patterns any more, so nothing can be lost between the
        # payload and the enricher for it. The word still appears in every payload -
        # divejobs.padi.com is the domain - which is precisely why matching on it was
        # removed; see the comment in certificates.yaml.
        "padi": "brand name in the domain and in marketing copy, never a held certificate",
    },
}


def report(source: str, pairs: list[tuple[str, str]]) -> int:
    """pairs: (text the enricher reads, full raw payload) per record. Returns defects."""
    lost: dict[str, int] = defaultdict(int)
    for readable, payload in pairs:
        for key in matched_keys(strip_markup(payload)) - matched_keys(readable):
            lost[key] += 1

    expected = EXPECTED_LOSSES.get(source, {})
    unexplained = {key: count for key, count in lost.items() if key not in expected}

    print(f"\n{source}: {len(pairs)} записей")
    if not unexplained:
        print("  дефектов нет")
    for key, count in sorted(unexplained.items(), key=lambda item: -item[1]):
        print(f"  ДЕФЕКТ  {key:20} {count:4}  есть в payload, не доходит до обогащения")
    for key, count in sorted(lost.items(), key=lambda item: -item[1]):
        if key in expected:
            print(f"  ожидаемо {key:20} {count:4}  {expected[key]}")
    return len(unexplained)


def audit_pinpoint() -> int:
    """Audit every Pinpoint account, not only the one the adapter was written for.

    Employers fill the same schema differently: a field that carries requirements
    for one of them may be empty or used for something else by another.
    """
    sys.path.insert(0, str(ROOT))
    from seawork.domain.enums import SourceTrust
    from seawork.ingestion.enrich.rules import RulesEnricher
    from seawork.ingestion.sources.platforms.pinpoint import PinpointSource
    from tests.test_snapshot import ACCOUNTS, _postings, _raw

    defects = 0
    for account in sorted(ACCOUNTS):
        source = PinpointSource(
            account=account,
            source_id=f"pinpoint:{account}",
            trust=SourceTrust.PRIMARY,
            user_agent="audit",
            interval_seconds=0,
        )
        pairs: list[tuple[str, str]] = []
        for posting in _postings(account):
            normalized = source.normalize(_raw(account, posting))
            pairs.append((RulesEnricher.qualification_text(normalized), json.dumps(posting)))
        defects += report(f"pinpoint:{account}", pairs)
    return defects


def audit_crewplanet() -> int:
    sys.path.insert(0, str(ROOT))
    import datetime
    import hashlib

    from seawork.domain.enums import SourceTrust
    from seawork.ingestion.base import RawItem
    from seawork.ingestion.enrich.rules import RulesEnricher
    from seawork.ingestion.sources.sites.crewplanet import CrewplanetSource

    source = CrewplanetSource(
        source_id="crewplanet", trust=SourceTrust.PRIMARY, user_agent="audit", interval_seconds=0
    )
    xml = (ROOT / "tests" / "fixtures" / "crewplanet" / "vacancies.xml").read_text(encoding="utf-8")
    pairs: list[tuple[str, str]] = []
    for raw_xml in re.findall(r"<item>.*?</item>", xml, re.S):
        item = RawItem.model_validate(
            {
                "source_id": "crewplanet",
                "external_id": "audit",
                "url": "https://www.crewplanet.eu/",
                "fetched_at": datetime.datetime.now(datetime.UTC),
                "payload": raw_xml,
                "content_type": "application/xml",
                "content_hash": hashlib.sha256(raw_xml.encode()).hexdigest(),
                "http_status": 200,
            }
        )
        normalized = source.normalize(item)
        pairs.append((RulesEnricher.qualification_text(normalized), raw_xml))
    return report("crewplanet", pairs)


def audit_padi() -> int:
    sys.path.insert(0, str(ROOT))
    import datetime
    import hashlib
    import xml.etree.ElementTree as ET

    from seawork.domain.enums import SourceTrust
    from seawork.ingestion.base import RawItem
    from seawork.ingestion.enrich.rules import RulesEnricher
    from seawork.ingestion.sources.sites.padi import PadiSource

    source = PadiSource(
        source_id="padi", trust=SourceTrust.PRIMARY, user_agent="audit", interval_seconds=0
    )
    tree = ET.parse(ROOT / "tests" / "fixtures" / "padi" / "jobs.xml")
    pairs: list[tuple[str, str]] = []
    for element in tree.getroot().findall("./channel/item"):
        raw_xml = ET.tostring(element, encoding="unicode", short_empty_elements=True)
        item = RawItem.model_validate(
            {
                "source_id": "padi",
                "external_id": "audit",
                "url": "https://divejobs.padi.com/",
                "fetched_at": datetime.datetime.now(datetime.UTC),
                "payload": raw_xml,
                "content_type": "text/xml",
                "content_hash": hashlib.sha256(raw_xml.encode()).hexdigest(),
                "http_status": 200,
            }
        )
        normalized = source.normalize(item)
        pairs.append((RulesEnricher.qualification_text(normalized), raw_xml))
    return report("padi", pairs)


if __name__ == "__main__":
    # Non-zero exit so this can guard CI: a new source, or a source that changes its
    # payload, fails the build instead of quietly losing data.
    sys.exit(1 if audit_pinpoint() + audit_crewplanet() + audit_padi() else 0)
