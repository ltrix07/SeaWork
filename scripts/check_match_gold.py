"""Validate the Match Score gold set before it is used to judge anything.

Checks what needs no judgement: profile vocabularies against data/reference, vacancy
dumps against `EnrichedOpportunity`, labels against the allowed set, known profiles
and absence of duplicate pairs. The gold file is optional until it has been sampled.

Usage:  .venv/bin/python scripts/check_match_gold.py
"""

import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from seawork.reporting.match_gold import load_gold, load_profiles, validate_gold
from seawork.storage.profile_vocabulary import ProfileVocabulary

ROOT = Path(__file__).parents[1]
PROFILES = ROOT / "tests" / "fixtures" / "eval" / "match_profiles.yaml"
GOLD = ROOT / "tests" / "fixtures" / "eval" / "match_gold.yaml"
REFERENCE = ROOT / "data" / "reference"


def main() -> int:
    try:
        profiles = load_profiles(PROFILES)
        pairs = load_gold(GOLD) if GOLD.exists() else []
    except ValidationError as error:
        print(error)
        return 1

    problems = validate_gold(profiles, pairs, ProfileVocabulary.from_directory(REFERENCE))

    print(f"profiles: {len(profiles)}")
    print(f"pairs: {len(pairs)}" + ("" if GOLD.exists() else " (no gold file yet)"))
    print("labels:", dict(Counter(str(pair.label) for pair in pairs)))

    if problems:
        print(f"\nproblems: {len(problems)}")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nchecks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
