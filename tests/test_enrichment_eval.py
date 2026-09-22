"""Offline smoke test for the eval pipeline, so a paid D3 run cannot crash on a
structural bug. Clients are injectable, so no network is touched."""

from pathlib import Path

import yaml

from seawork.reporting.enrichment_eval import (
    FieldMetrics,
    evaluate_enrichment,
    format_enrichment_report,
)

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "tests" / "fixtures" / "eval" / "enrichment_gold.yaml"
REPORT_KEYS = {
    "experience_level",
    "required_certificates",
    "no-signal / experience_level",
    "no-signal / required_certificates",
}


def cert_keys() -> list[str]:
    data = yaml.safe_load((ROOT / "data" / "reference" / "certificates.yaml").read_text())
    return [row["key"] for row in data["certificates"]]


class NullClient:
    """A model that always abstains — the correct answer for the no-signal stratum."""

    model_id = "null-model"

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        return {"experience_level": None, "required_certificates": []}


class InventClient:
    """A model that always claims mid with a quote that is never in the text."""

    model_id = "invent-model"

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        return {
            "experience_level": {"value": "mid", "basis": "stated", "quote": "￿not-in-text"},
            "required_certificates": [],
        }


def test_evaluate_enrichment_runs_offline_over_real_gold() -> None:
    report = evaluate_enrichment(NullClient(), GOLD, cert_keys())
    assert set(report) == REPORT_KEYS
    assert all(isinstance(metric, FieldMetrics) for metric in report.values())
    # A model that never fills anything cannot false-fill, anywhere.
    assert report["experience_level"].false_fill_rate == 0.0
    assert report["no-signal / experience_level"].false_fill_rate == 0.0
    text = format_enrichment_report("null-model", report)
    assert "null-model" in text
    assert "ложные заполнения" in text


def test_false_fill_and_invalid_quote_are_registered() -> None:
    report = evaluate_enrichment(InventClient(), GOLD, cert_keys())
    # Predicting mid on every record must count as a false fill on the null no-signal gold.
    false_fills = report["no-signal / experience_level"].false_fill_rate
    assert false_fills is not None and false_fills > 0.0
    # The quote is never a substring of the text, so every filled quote is invalid.
    invalid_quotes = report["experience_level"].invalid_quote_rate
    assert invalid_quotes is not None and invalid_quotes > 0.0


def test_diagnose_reports_false_fills_and_invalid_quotes() -> None:
    from seawork.reporting.enrichment_eval import diagnose_enrichment

    report, errors = diagnose_enrichment(InventClient(), GOLD, cert_keys())
    false_fills = report["no-signal / experience_level"].false_fill_rate
    assert false_fills is not None and false_fills > 0.0
    kinds = {(disagreement.field, disagreement.kind) for disagreement in errors}
    assert ("experience", "false_fill") in kinds
    assert ("experience", "invalid_quote") in kinds


def test_aggregate_reports_mean_and_range() -> None:
    from seawork.reporting.enrichment_eval import FieldMetrics, aggregate_runs

    def report(precision: float, false_fills: float) -> dict[str, FieldMetrics]:
        return {
            "experience_level": FieldMetrics(
                precision=precision,
                recall=None,
                false_fill_rate=false_fills,
                invalid_quote_rate=0.0,
                by_basis={},
            )
        }

    spreads = aggregate_runs([report(0.8, 0.02), report(0.6, 0.04), report(0.7, 0.03)])
    experience = spreads["experience_level"]
    assert experience.precision.minimum == 0.6
    assert experience.precision.maximum == 0.8
    assert experience.precision.mean is not None
    assert abs(experience.precision.mean - 0.7) < 1e-9
    # A metric that was never measurable stays unmeasurable; it must not become zero.
    assert experience.recall.mean is None
