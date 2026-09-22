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


@dataclass(frozen=True)
class Disagreement:
    external_id: str
    field: str  # "experience" | "certificates"
    kind: str  # "false_fill" | "invalid_quote" | "wrong_value"
    gold: object
    predicted: object
    quote: str | None


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


def _predict_rows(
    client: LLMClient, gold_path: Path, certificate_keys: list[str]
) -> list[dict[str, object]]:
    loaded = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    records = cast(list[dict[str, object]], loaded["records"])
    rows: list[dict[str, object]] = []
    for record in records:
        text = cast(str, record["text"])
        prediction = _run(client.extract(build_prompt(text, certificate_keys), response_schema()))
        rows.append({**record, "prediction": prediction})
    return rows


def _metrics_from_rows(rows: list[dict[str, object]]) -> dict[str, FieldMetrics]:
    no_signal = [r for r in rows if cast(str, r["stratum"]).endswith("no-signal")]
    return {
        "experience_level": _field_metrics(rows, "experience"),
        "required_certificates": _field_metrics(rows, "certificates"),
        "no-signal / experience_level": _field_metrics(no_signal, "experience"),
        "no-signal / required_certificates": _field_metrics(no_signal, "certificates"),
    }


def evaluate_enrichment(
    client: LLMClient, gold_path: Path, certificate_keys: list[str]
) -> dict[str, FieldMetrics]:
    """Evaluate one provider. Clients are injectable, so tests never use a network."""
    return _metrics_from_rows(_predict_rows(client, gold_path, certificate_keys))


def diagnose_enrichment(
    client: LLMClient, gold_path: Path, certificate_keys: list[str]
) -> tuple[dict[str, FieldMetrics], list[Disagreement]]:
    """One model pass returning both the metrics and the per-record disagreements."""
    rows = _predict_rows(client, gold_path, certificate_keys)
    return _metrics_from_rows(rows), disagreements_from_rows(rows, certificate_keys)


def disagreements_from_rows(
    rows: list[dict[str, object]], certificate_keys: list[str]
) -> list[Disagreement]:
    found: list[Disagreement] = []
    for row in rows:
        ext = str(row["external_id"])
        text = cast(str, row["text"])
        pred = row["prediction"] if isinstance(row["prediction"], dict) else {}

        gold_level = row["experience_level"]
        item = pred.get("experience_level")
        pred_level = item.get("value") if isinstance(item, dict) else None
        pred_quote = item.get("quote") if isinstance(item, dict) else None
        quote = pred_quote if isinstance(pred_quote, str) else None
        if pred_level is not None:
            if gold_level is None:
                found.append(Disagreement(ext, "experience", "false_fill", None, pred_level, quote))
            elif pred_level != gold_level:
                found.append(
                    Disagreement(ext, "experience", "wrong_value", gold_level, pred_level, quote)
                )
            if not _quote_ok(text, pred_quote):
                found.append(
                    Disagreement(ext, "experience", "invalid_quote", gold_level, pred_level, quote)
                )

        gold_certs = sorted(cast(list[str], row["required_certificates"] or []))
        items = pred.get("required_certificates")
        entries = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        pred_keys: list[str] = []
        for entry in entries:
            key = entry.get("key")
            if isinstance(key, str):
                pred_keys.append(key)
        pred_keys = sorted(set(pred_keys))
        if pred_keys:
            if not gold_certs:
                found.append(
                    Disagreement(ext, "certificates", "false_fill", gold_certs, pred_keys, None)
                )
            elif pred_keys != gold_certs:
                found.append(
                    Disagreement(ext, "certificates", "wrong_value", gold_certs, pred_keys, None)
                )
        for entry in entries:
            key = entry.get("key")
            entry_quote = entry.get("quote")
            if isinstance(key, str) and not _quote_ok(text, entry_quote):
                found.append(
                    Disagreement(
                        ext,
                        "certificates",
                        "invalid_quote",
                        None,
                        key,
                        entry_quote if isinstance(entry_quote, str) else None,
                    )
                )
    return found


def format_disagreements(disagreements: list[Disagreement]) -> str:
    if not disagreements:
        return "расхождений не найдено"
    tally: dict[str, int] = {}
    for disagreement in disagreements:
        tally[f"{disagreement.field}/{disagreement.kind}"] = (
            tally.get(f"{disagreement.field}/{disagreement.kind}", 0) + 1
        )
    lines = [
        "Расхождения — сводка: "
        + ", ".join(f"{key}: {value}" for key, value in sorted(tally.items()))
    ]
    lines.append("id | поле | тип | эталон -> модель | цитата")
    for disagreement in sorted(
        disagreements, key=lambda value: (value.field, value.kind, value.external_id)
    ):
        lines.append(
            f"  {disagreement.external_id:10} {disagreement.field:11} {disagreement.kind:13} "
            f"{disagreement.gold!r} -> {disagreement.predicted!r}  quote={disagreement.quote!r}"
        )
    return "\n".join(lines)


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
