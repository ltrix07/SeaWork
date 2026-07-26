"""Offline-friendly metrics for the hand-labelled LLM enrichment gold set."""
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateUsage=false

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from seawork.ingestion.enrich.llm import (
    LLMClient,
    _run,
    build_prompt,
    normalise_whitespace,
    response_schema,
)


@dataclass(frozen=True)
class FieldMetrics:
    precision: float
    recall: float
    false_fill_rate: float
    invalid_quote_rate: float
    by_basis: dict[str, int]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _quote_ok(text: str, value: object) -> bool:
    return isinstance(value, str) and normalise_whitespace(value) in normalise_whitespace(text)


def _field_metrics(rows: list[dict[str, object]], field: str) -> FieldMetrics:
    correct = filled = gold_filled = false_fills = quoted = invalid_quotes = 0
    bases: dict[str, int] = {}
    for row in rows:
        gold = row["experience_level"] if field == "experience" else row["required_certificates"]
        predicted = row["prediction"]
        if field == "experience":
            item = predicted.get("experience_level") if isinstance(predicted, dict) else None
            value = item.get("value") if isinstance(item, dict) else None
            quote = item.get("quote") if isinstance(item, dict) else None
            basis = item.get("basis") if isinstance(item, dict) else None
            gold_is_filled = gold is not None
            equal = value == gold
        else:
            items = predicted.get("required_certificates") if isinstance(predicted, dict) else None
            entries = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
            value = [x.get("key") for x in entries if isinstance(x.get("key"), str)]
            quote = None
            basis = None
            gold_is_filled = bool(gold)
            equal = set(value) == set(cast(list[str], gold or []))
            for entry in entries:
                quoted += 1
                if not _quote_ok(cast(str, row["text"]), entry.get("quote")):
                    invalid_quotes += 1
                item_basis = entry.get("basis")
                if isinstance(item_basis, str):
                    bases[item_basis] = bases.get(item_basis, 0) + 1
        is_filled = bool(value) if field == "certificates" else value is not None
        if gold_is_filled:
            gold_filled += 1
        if is_filled:
            filled += 1
            if equal:
                correct += 1
            if field == "experience":
                quoted += 1
                if not _quote_ok(cast(str, row["text"]), quote):
                    invalid_quotes += 1
                if isinstance(basis, str):
                    bases[basis] = bases.get(basis, 0) + 1
        if not gold_is_filled and is_filled:
            false_fills += 1
    return FieldMetrics(
        precision=_rate(correct, filled),
        recall=_rate(correct, gold_filled),
        false_fill_rate=_rate(false_fills, len(rows)),
        invalid_quote_rate=_rate(invalid_quotes, quoted),
        by_basis=bases,
    )


def evaluate_enrichment(
    client: LLMClient, gold_path: Path, certificate_keys: list[str]
) -> dict[str, FieldMetrics]:
    """Evaluate one provider. Clients are injectable, so tests never use a network."""
    loaded = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    records = cast(list[dict[str, object]], loaded["records"])
    rows: list[dict[str, object]] = []
    for record in records:
        text = cast(str, record["text"])
        prediction = _run(client.extract(build_prompt(text, certificate_keys), response_schema()))
        rows.append({**record, "prediction": prediction})
    no_signal = [r for r in rows if cast(str, r["stratum"]).endswith("no-signal")]
    return {
        "experience_level": _field_metrics(rows, "experience"),
        "required_certificates": _field_metrics(rows, "certificates"),
        "no-signal / experience_level": _field_metrics(no_signal, "experience"),
        "no-signal / required_certificates": _field_metrics(no_signal, "certificates"),
    }


def format_enrichment_report(provider: str, report: dict[str, FieldMetrics]) -> str:
    lines = [f"Провайдер: {provider}"]
    for field, metric in report.items():
        base_values = (f"{key}: {value}" for key, value in sorted(metric.by_basis.items()))
        bases = ", ".join(base_values) or "—"
        lines.append(
            f"{field}: точность {metric.precision:.1%}; полнота {metric.recall:.1%}; "
            f"ложные заполнения {metric.false_fill_rate:.1%}; невалидные цитаты "
            f"{metric.invalid_quote_rate:.1%}; basis [{bases}]"
        )
    return "\n".join(lines)
