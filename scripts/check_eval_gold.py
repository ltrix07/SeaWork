"""Validate the hand-labelled gold set before it is used to judge anything.

The gold set decides whether the LLM enricher passes the milestone, so an error
here is worse than an error in the enricher: it is invisible and it propagates.
Everything checkable without judgement is checked here — enum membership,
vocabulary membership, internal consistency, and above all whether each quote is
a real substring of the text it claims to come from.

A quote that cannot be found means the label rests on something other than the
text the model will receive, which is the exact failure that produced the first
draft of this file.

Usage:  .venv/bin/python scripts/check_eval_gold.py
"""

import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
GOLD = ROOT / "tests" / "fixtures" / "eval" / "enrichment_gold.yaml"
CERTS = ROOT / "data" / "reference" / "certificates.yaml"

LEVELS = {"entry", "junior", "mid", "senior", "lead"}
BASES = {"stated", "inferred"}


def normalise(text: str) -> str:
    """Fold the differences a human cannot be expected to reproduce by hand.

    Typographic quotes, non-breaking spaces and runs of whitespace differ between
    the source and anything that has been through a chat window. Case and
    punctuation beyond that are NOT folded: a quote is meant to be copied, and
    loosening this check any further would stop it from catching invention.
    """
    for old, new in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(old, new)
    return " ".join(text.replace(" ", " ").split())


def main() -> int:
    records = yaml.safe_load(GOLD.read_text(encoding="utf-8"))["records"]
    vocabulary = {row["key"] for row in yaml.safe_load(CERTS.read_text())["certificates"]}
    problems: list[str] = []

    for record in records:
        where = f"{record['source_id']} {record['external_id']}"
        text = normalise(record["text"])
        level = record["experience_level"]
        basis = record["experience_basis"]
        quote = record["experience_quote"]
        certificates = record["required_certificates"] or []
        quotes = record["certificate_quotes"] or {}

        if level is not None and level not in LEVELS:
            problems.append(f"{where}: неизвестный уровень {level!r}")
        if basis is not None and basis not in BASES:
            problems.append(f"{where}: неизвестный basis {basis!r}")

        if (level is None) != (basis is None):
            problems.append(f"{where}: уровень {level!r} и basis {basis!r} несогласованы")
        if (level is None) != (quote is None):
            problems.append(f"{where}: уровень {level!r} и цитата {'есть' if quote else 'нет'}")
        if quote and normalise(quote) not in text:
            problems.append(f"{where}: цитата опыта не найдена в тексте — {quote!r}")

        for key in certificates:
            if key not in vocabulary:
                problems.append(f"{where}: {key!r} отсутствует в certificates.yaml")
            if key not in quotes:
                problems.append(f"{where}: для {key!r} нет цитаты")
        for key, cert_quote in quotes.items():
            if key not in certificates:
                problems.append(f"{where}: цитата для {key!r}, которого нет в списке")
            elif not cert_quote or normalise(str(cert_quote)) not in text:
                problems.append(f"{where}: цитата {key!r} не найдена в тексте — {cert_quote!r}")

    print(f"записей: {len(records)}")
    print("уровни:", dict(Counter(str(r["experience_level"]) for r in records)))
    no_signal = [r for r in records if r["stratum"].endswith("no-signal")]
    filled = sum(1 for r in no_signal if r["experience_level"] is not None)
    print(f"страта no-signal: {len(no_signal)}, из них с уровнем: {filled}")
    print(f"записей с сертификатами: {sum(1 for r in records if r['required_certificates'])}")
    print(f"с заметками: {sum(1 for r in records if r['notes'])}")

    if problems:
        print(f"\nпроблем: {len(problems)}")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nпроверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
