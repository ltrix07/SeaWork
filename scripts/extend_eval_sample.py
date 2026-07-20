"""Add a source's records to the hand-labelled gold set without disturbing it.

The gold set is built in layers, one per source, because labelling is human work
and regenerating the whole sample would throw it away. scripts/build_eval_sample.py
created the first layer from Holland America and Crewplanet; this adds a layer for
a source that joined later.

Why a layer rather than a fresh stratified draw over the whole population: a new
draw would pick a different 50, and the records already labelled would mostly fall
out of the sample. The layers are less elegant than one clean sample and cost an
hour less of a domain expert's time.

Existing records are copied verbatim, labels included. Records already present are
never added twice, so re-running is safe.

Usage:  .venv/bin/python scripts/extend_eval_sample.py <account> [count]
        .venv/bin/python scripts/extend_eval_sample.py princesscruises 25
"""

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from seawork.domain.enums import SourceTrust  # noqa: E402
from seawork.ingestion.classify import classify  # noqa: E402
from seawork.ingestion.enrich.rules import RulesEnricher  # noqa: E402
from seawork.ingestion.quality import check_quality  # noqa: E402
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource  # noqa: E402
from tests.test_snapshot import _postings, _raw  # noqa: E402

REFERENCE_DIR = ROOT / "data" / "reference"
RULES = REFERENCE_DIR / "opportunity_types.yaml"
GOLD = ROOT / "tests" / "fixtures" / "eval" / "enrichment_gold.yaml"

SEED = 20260720
MIN_TEXT = 20

EXPERIENCE_HINT = re.compile(
    r"experience|years?|months?|rank|опыт|стаж|контракт|должност", re.IGNORECASE
)
CERT_HINT = re.compile(
    r"certificat|licen[cs]e|STCW|COC|endorsement|training|diploma|qualif"
    r"|сертификат|диплом|удостоверен|документ",
    re.IGNORECASE,
)


def stratum(short: str, text: str) -> str:
    has_experience = bool(EXPERIENCE_HINT.search(text))
    has_certificate = bool(CERT_HINT.search(text))
    if not has_experience and not has_certificate:
        return f"{short}/no-signal"
    if has_experience and has_certificate:
        return f"{short}/both"
    return f"{short}/exp-only" if has_experience else f"{short}/cert-only"


def candidates(account: str) -> list[tuple[str, str, str, str]]:
    """Return (external_id, url, title, text) for records the LLM will see."""
    source = PinpointSource(
        account=account,
        source_id=f"pinpoint:{account}",
        trust=SourceTrust.PRIMARY,
        user_agent="eval",
        interval_seconds=0,
    )
    enricher = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    rows: list[tuple[str, str, str, str]] = []
    for posting in _postings(account):
        normalized = source.normalize(_raw(account, posting))
        enriched = enricher.enrich(
            normalized,
            classify(normalized, RULES),
            check_quality(normalized, duplicate_content=False),
        )
        if enriched.experience_level is not None and enriched.required_certificates is not None:
            continue
        text = RulesEnricher.qualification_text(normalized)
        if len(text.strip()) < MIN_TEXT:
            continue
        rows.append((normalized.external_id, str(normalized.url), normalized.title, text))
    return rows


def block(source_id: str, external_id: str, key: str, url: str, title: str, text: str) -> list[str]:
    body = "\n".join("      " + line for line in text.splitlines())
    return [
        f"  - source_id: {source_id}",
        f'    external_id: "{external_id}"',
        f"    stratum: {key}",
        f"    url: {url}",
        f"    title: {json.dumps(title, ensure_ascii=False)}",
        "    text: |",
        body,
        "    # --- заполнить вручную ---",
        "    experience_level: null",
        "    experience_basis: null",
        "    experience_quote: null",
        "    required_certificates: []",
        "    certificate_quotes: {}",
        '    notes: ""',
        "",
    ]


def main() -> None:
    account = sys.argv[1] if len(sys.argv) > 1 else "princesscruises"
    wanted = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    source_id = f"pinpoint:{account}"
    short = "pinpoint" if account else account

    existing_text = GOLD.read_text(encoding="utf-8")
    existing = yaml.safe_load(existing_text)["records"]
    already = {(r["source_id"], str(r["external_id"])) for r in existing}

    rows = [r for r in candidates(account) if (source_id, r[0]) not in already]
    buckets: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for row in rows:
        buckets[stratum(short, row[3])].append(row)

    generator = random.Random(SEED)
    for bucket in buckets.values():
        generator.shuffle(bucket)

    # Same shape as the first layer: the no-signal stratum is over-sampled because
    # it is the false-fill test, and false fills are the metric that decides the
    # milestone.
    quotas: dict[str, int] = {}
    no_signal = f"{short}/no-signal"
    budget = wanted
    if buckets.get(no_signal):
        quotas[no_signal] = min(len(buckets[no_signal]), max(1, wanted // 4))
        budget -= quotas[no_signal]
    rest = [k for k in buckets if k != no_signal and buckets[k]]
    for index, key in enumerate(rest):
        share = budget // len(rest) + (1 if index < budget % len(rest) else 0)
        quotas[key] = min(len(buckets[key]), share)

    shortfall = wanted - sum(quotas.values())
    while shortfall > 0:
        options = [k for k, v in buckets.items() if v and quotas.get(k, 0) < len(v)]
        if not options:
            break
        options.sort(key=lambda k: len(buckets[k]) - quotas.get(k, 0), reverse=True)
        quotas[options[0]] = quotas.get(options[0], 0) + 1
        shortfall -= 1

    added: list[str] = []
    counts: dict[str, int] = {}
    for key, count in sorted(quotas.items()):
        for row in buckets[key][:count]:
            added += block(source_id, row[0], key, row[1], row[2], row[3])
            counts[key] = counts.get(key, 0) + 1

    banner = [
        "",
        f"  # --- Слой {source_id} ---",
        "  # Источник подключён после первой разметки. Записи выше не тронуты.",
        "",
    ]
    GOLD.write_text(existing_text.rstrip("\n") + "\n" + "\n".join(banner + added), encoding="utf-8")

    print(f"кандидатов у {account}: {len(rows)} (уже в эталоне: {len(already)})")
    print(f"добавлено: {sum(counts.values())}")
    for key in sorted(counts):
        print(f"  {key:26} {counts[key]:3}   (доступно {len(buckets[key])})")
    print(f"\nвсего в эталоне: {len(existing) + sum(counts.values())}")


if __name__ == "__main__":
    main()
