"""The agreement report on a hand-made gold set where every metric has a known answer."""

import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import HttpUrl
from typer.testing import CliRunner

from seawork.cli import app
from seawork.domain.enums import OpportunityType, Provenance
from seawork.domain.models import EnrichedOpportunity, Inferred, NormalizedOpportunity
from seawork.domain.recommendation import Candidate
from seawork.reporting.match_eval import (
    AgreementReport,
    OrderStats,
    evaluate_agreement,
    format_agreement_report,
    format_disagreements,
    format_log_entry,
)
from seawork.reporting.match_gold import (
    GoldPair,
    Label,
    ProfileSpec,
    StaleLabelTargetError,
    UnknownProfileError,
    load_profiles,
    write_gold,
)

ROOT = Path(__file__).parents[1]
PROFILES = ROOT / "tests" / "fixtures" / "eval" / "match_profiles.yaml"


def fake_candidate(
    identifier: int,
    profession: str | None,
    direction: str | None,
    languages: list[str] | None = None,
) -> Candidate:
    def inferred[T](value: T | None) -> Inferred[T] | None:
        if value is None:
            return None
        return Inferred(value=value, provenance=Provenance.RULE, confidence=0.8)

    return Candidate(
        opportunity_id=identifier,
        opportunity=EnrichedOpportunity(
            normalized=NormalizedOpportunity(
                source_id="s",
                external_id=str(identifier),
                url=HttpUrl(f"https://example.com/{identifier}"),
                title=f"v{identifier}",
            ),
            type=OpportunityType.JOB,
            type_confidence=0.9,
            profession=inferred(profession),
            direction=inferred(direction),
            required_languages=inferred(languages),
            quality_score=1.0,
            quality_flags=[],
        ),
        employer=None,
        freshness=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
    )


FACTS: dict[str, Any] = {
    "professions": ["chef"],
    "directions": ["cruise"],
    "languages": ["en"],
}
ONBOARDING = ProfileSpec(id="a", kind="onboarding", description="d", facts=FACTS)
EXTENDED = ProfileSpec(id="b", kind="extended", description="d", facts=FACTS)
SECOND_EXTENDED = ProfileSpec(id="c", kind="extended", description="d", facts=FACTS)

# For FACTS: 1.0 over three factors, 2/3, 1/3, 0.0 and None.
FULL = fake_candidate(1, "chef", "cruise", ["en"])
TWO_THIRDS = fake_candidate(2, "chef", "cruise", ["de"])
ONE_THIRD = fake_candidate(3, "chef", "yachts", ["de"])
ZERO = fake_candidate(4, "pilot", "yachts", ["de"])
NOTHING = fake_candidate(5, None, None)


def gold_pair(
    spec: ProfileSpec, candidate: Candidate, label: Label | None, note: str | None = None
) -> GoldPair:
    return GoldPair(
        profile_id=spec.id,
        profile_hash=spec.facts_hash(),
        opportunity_id=candidate.opportunity_id,
        stratum="random",
        employer=None,
        description=None,
        opportunity=candidate.opportunity,
        label=label,
        note=note,
    )


def hand_made() -> list[GoldPair]:
    fit, no_fit, unsure = Label.FIT, Label.NO_FIT, Label.UNSURE
    return [
        gold_pair(ONBOARDING, FULL, fit),
        gold_pair(ONBOARDING, TWO_THIRDS, no_fit),
        gold_pair(ONBOARDING, ONE_THIRD, fit),
        gold_pair(ONBOARDING, ZERO, fit),
        gold_pair(ONBOARDING, NOTHING, fit),
        gold_pair(ONBOARDING, fake_candidate(6, "chef", "cruise", ["en"]), no_fit),
        gold_pair(ONBOARDING, fake_candidate(10, "pilot", "yachts", ["de"]), no_fit),
        gold_pair(ONBOARDING, fake_candidate(7, "chef", "cruise", ["de"]), unsure),
        gold_pair(ONBOARDING, fake_candidate(8, None, None), unsure),
        gold_pair(ONBOARDING, fake_candidate(9, "chef", "cruise", ["en"]), None),
        gold_pair(EXTENDED, FULL, fit),
        gold_pair(EXTENDED, ONE_THIRD, no_fit),
        gold_pair(SECOND_EXTENDED, ZERO, no_fit),
    ]


def report() -> AgreementReport:
    return evaluate_agreement([ONBOARDING, EXTENDED, SECOND_EXTENDED], hand_made())


def test_order_counts_correct_ties_and_wrong_inside_the_profile() -> None:
    # In the feed: fit 1.0, 1/3, None; no_fit 2/3, 1.0. 1.0>2/3 right, 1.0=1.0 tie, the rest wrong.
    order = report().profiles["a"].order
    assert order == OrderStats(compared=6, correct=1, ties=1, wrong=4)
    assert order.accuracy == 0.25


def test_pairs_of_different_profiles_are_never_compared() -> None:
    # b has fit 1.0, c has no_fit 0.0: compared across profiles that would be one more correct.
    assert report().groups["extended"].order == OrderStats(compared=1, correct=1)


def test_fit_with_none_and_zero_are_counted_apart_from_order() -> None:
    metrics = report().groups["onboarding"]
    assert (metrics.fit, metrics.no_fit, metrics.unsure) == (4, 3, 2)
    assert (metrics.fit_none, metrics.fit_none_rate) == (1, 0.25)
    assert (metrics.fit_zero, metrics.fit_zero_rate) == (1, 0.25)
    assert (metrics.no_fit_zero, metrics.no_fit_zero_rate) == (1, 1 / 3)


def test_unsure_is_kept_out_of_the_binary_metrics_and_shown_apart() -> None:
    metrics = report().groups["onboarding"]
    assert (metrics.unsure_none, metrics.unsure_zero) == (1, 0)
    assert metrics.order.compared == 6


def test_unlabelled_pairs_are_counted_and_not_measured() -> None:
    result = report()
    assert result.unlabelled == 1
    assert result.total == 13


def test_groups_are_split_by_the_profile_kind() -> None:
    result = report()
    assert result.groups["extended"].fit == 1
    assert result.groups["extended"].no_fit == 2
    assert result.groups["extended"].no_fit_zero == 1
    assert result.profile_kinds == {"a": "onboarding", "b": "extended", "c": "extended"}


def test_metric_without_data_is_none_not_zero() -> None:
    result = evaluate_agreement([ONBOARDING], [gold_pair(ONBOARDING, FULL, Label.NO_FIT)])
    metrics = result.groups["onboarding"]
    assert metrics.order.accuracy is None
    assert metrics.fit_none_rate is None
    assert metrics.fit_zero_rate is None
    empty_group = result.groups["extended"]
    assert (empty_group.fit, empty_group.order.accuracy, empty_group.fit_none_rate) == (
        0,
        None,
        None,
    )
    assert "порядок «подходит» выше «не подходит»: —" in format_agreement_report(result)


def test_disagreements_list_each_kind_with_factors() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [
            gold_pair(ONBOARDING, ZERO, Label.FIT, note="по сути подходит"),
            gold_pair(ONBOARDING, NOTHING, Label.FIT),
            gold_pair(ONBOARDING, TWO_THIRDS, Label.NO_FIT),
            gold_pair(ONBOARDING, ONE_THIRD, Label.FIT),
        ],
    )
    kinds = sorted(item.kind for item in result.disagreements)
    assert kinds == ["fit_none", "fit_zero", "inversion", "inversion"]
    text = format_disagreements(result)
    assert "подходит, но value=None: 1" in text
    assert "подходит, но value=0.0" in text
    assert "«подходит» обогнали «не подходит» (выдача ставит их выше): 2" in text
    assert "обогнала, не подходит: s/2" in text
    assert "value=0.000" in text
    assert "profession-" in text
    assert "languages-" in text
    assert "заметка: по сути подходит" in text


def test_no_disagreements_when_the_formula_agrees() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [gold_pair(ONBOARDING, FULL, Label.FIT), gold_pair(ONBOARDING, ONE_THIRD, Label.NO_FIT)],
    )
    assert result.disagreements == []
    assert format_disagreements(result) == "Расхождений нет."


def test_report_text_shows_both_groups() -> None:
    text = format_agreement_report(report())
    assert "форма онбординга" in text
    assert "расширенный профиль" in text
    assert (
        "порядок «подходит» выше «не подходит»: 25.0% (сравнений 6: верно 1, ничьих 1, наоборот 4)"
        in text
    )
    assert "«не подходит» верно 1 (33.3%); «подходит» ошибочно 1 (25.0%)" in text
    assert "«подходит» с value=None (профили с фактами): 1 (25.0%)" in text
    assert "  a: подходит 4, не подходит 3, спорно 2; порядок 25.0% (сравнений 6" in text


def test_log_entry_is_a_dated_table_with_the_disagreements() -> None:
    entry = format_log_entry(report(), "11.11.2026")
    assert entry.startswith("## 11.11.2026")
    assert "| форма онбординга" in entry
    assert "```text" in entry


def test_stale_profile_hash_stops_the_report() -> None:
    stale = gold_pair(ONBOARDING, FULL, Label.FIT).model_copy(update={"profile_hash": "old"})
    with pytest.raises(StaleLabelTargetError):
        evaluate_agreement([ONBOARDING], [stale])


def test_unknown_profile_stops_the_report() -> None:
    with pytest.raises(UnknownProfileError):
        evaluate_agreement([EXTENDED], [gold_pair(ONBOARDING, FULL, Label.FIT)])


def test_shipped_profiles_split_into_the_two_kinds() -> None:
    kinds = {spec.id: spec.kind for spec in load_profiles(PROFILES)}
    onboarding = {profile_id for profile_id, kind in kinds.items() if kind == "onboarding"}
    assert onboarding == {"empty", "browsing", "newcomer_cruise", "onboarded_working"}
    assert len(kinds) > len(onboarding)


def test_cli_report_prints_without_writing_the_log(tmp_path: Path) -> None:
    empty = next(spec for spec in load_profiles(PROFILES) if spec.id == "empty")
    path = tmp_path / "gold.yaml"
    write_gold(path, [gold_pair(empty, FULL, Label.FIT)])
    result = CliRunner().invoke(app, ["eval", "match", "report", str(path)])
    assert result.exit_code == 0
    assert "None структурный" in result.output
    assert "Расхождений нет." in result.output
    assert "Записано" not in result.output


def test_tie_in_value_is_decided_by_comparable_factors() -> None:
    partial = fake_candidate(10, "chef", None, None)
    result = evaluate_agreement(
        [ONBOARDING],
        [gold_pair(ONBOARDING, FULL, Label.FIT), gold_pair(ONBOARDING, partial, Label.NO_FIT)],
    )
    assert result.profiles["a"].order == OrderStats(compared=1, correct=1)
    flipped = evaluate_agreement(
        [ONBOARDING],
        [gold_pair(ONBOARDING, partial, Label.FIT), gold_pair(ONBOARDING, FULL, Label.NO_FIT)],
    )
    assert flipped.profiles["a"].order == OrderStats(compared=1, wrong=1)


def test_equal_value_and_equal_comparable_factors_is_a_tie() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [
            gold_pair(ONBOARDING, FULL, Label.FIT),
            gold_pair(ONBOARDING, fake_candidate(10, "chef", "cruise", ["en"]), Label.NO_FIT),
        ],
    )
    assert result.profiles["a"].order == OrderStats(compared=1, ties=1)
    assert result.profiles["a"].order.accuracy == 0.5


def test_fit_without_value_below_positive_no_fit_is_an_inversion() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [
            gold_pair(ONBOARDING, NOTHING, Label.FIT),
            gold_pair(ONBOARDING, ONE_THIRD, Label.NO_FIT),
        ],
    )
    assert result.profiles["a"].order == OrderStats(compared=1, wrong=1)
    inversion = next(item for item in result.disagreements if item.kind == "inversion")
    assert inversion.fit.value is None
    assert [other.pair.external_id for other in inversion.others] == ["3"]


def test_inversions_are_grouped_by_the_fit_vacancy() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [
            gold_pair(ONBOARDING, ONE_THIRD, Label.FIT),
            gold_pair(ONBOARDING, FULL, Label.NO_FIT),
            gold_pair(ONBOARDING, TWO_THIRDS, Label.NO_FIT),
        ],
    )
    inversions = [item for item in result.disagreements if item.kind == "inversion"]
    assert len(inversions) == 1
    assert len(inversions[0].others) == 2
    assert result.profiles["a"].order.wrong == 2


def test_correct_exclusion_is_counted_and_stays_out_of_the_order() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [gold_pair(ONBOARDING, FULL, Label.FIT), gold_pair(ONBOARDING, ZERO, Label.NO_FIT)],
    )
    metrics = result.profiles["a"]
    assert (metrics.no_fit_zero, metrics.no_fit_zero_rate) == (1, 1.0)
    assert metrics.order == OrderStats()
    assert result.disagreements == []


def test_zero_value_is_not_in_the_order_and_is_not_an_inversion() -> None:
    result = evaluate_agreement(
        [ONBOARDING],
        [gold_pair(ONBOARDING, ZERO, Label.FIT), gold_pair(ONBOARDING, ONE_THIRD, Label.NO_FIT)],
    )
    assert result.profiles["a"].order == OrderStats()
    assert [item.kind for item in result.disagreements] == ["fit_zero"]


def test_none_of_a_structural_profile_is_not_a_mistake() -> None:
    empty = ProfileSpec(id="e", kind="onboarding", structural_none=True, description="d", facts={})
    result = evaluate_agreement(
        [empty, ONBOARDING],
        [gold_pair(empty, FULL, Label.FIT), gold_pair(ONBOARDING, FULL, Label.FIT)],
    )
    assert result.disagreements == []
    assert result.profiles["e"].fit == 1
    assert result.profiles["e"].fit_none_rate is None
    assert result.groups["onboarding"].fit_none_rate == 0.0
    assert "e: подходит 1" in format_agreement_report(result)
    assert "None структурный" in format_agreement_report(result)


def test_nothing_labelled_is_said_aloud_not_reported_as_agreement() -> None:
    result = evaluate_agreement([ONBOARDING], [gold_pair(ONBOARDING, FULL, None)])
    assert result.unlabelled == 1
    assert format_disagreements(result) == "Размеченных пар нет, расхождений считать не из чего."
