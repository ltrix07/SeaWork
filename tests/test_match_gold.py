"""The gold set format and its validator: one test per rule the validator enforces."""

import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import HttpUrl, ValidationError
from typer.testing import CliRunner

from seawork.domain.enums import OpportunityType, Provenance
from seawork.domain.models import EnrichedOpportunity, Inferred, NormalizedOpportunity
from seawork.domain.recommendation import Candidate
from seawork.reporting.match_gold import (
    _PROFILE_TIME,  # pyright: ignore[reportPrivateUsage]
    GoldPair,
    Label,
    ProfileSpec,
    UnknownProfileError,
    load_gold,
    load_profiles,
    sample_pairs,
    validate_gold,
    write_gold,
)
from seawork.scoring.engine import compute_match_score
from seawork.storage.profile_vocabulary import ProfileVocabulary

ROOT = Path(__file__).parents[1]
PROFILES = ROOT / "tests" / "fixtures" / "eval" / "match_profiles.yaml"
VOCABULARY = ProfileVocabulary.from_directory(ROOT / "data" / "reference")


def spec(profile_id: str = "p", **facts: object) -> ProfileSpec:
    return ProfileSpec(id=profile_id, description="d", facts=facts)


def opportunity_dump() -> dict[str, Any]:
    opportunity = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="s", external_id="1", url=HttpUrl("https://example.com/1"), title="t"
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        quality_score=0.8,
        quality_flags=[],
    )
    return opportunity.model_dump(mode="json")


def pair_data(profile_id: str = "p", external_id: str = "1", **overrides: Any) -> dict[str, Any]:
    dump = opportunity_dump()
    dump["normalized"]["external_id"] = external_id
    return {
        "profile_id": profile_id,
        "profile_hash": spec(profile_id).facts_hash(),
        "opportunity_id": 1,
        "stratum": "top",
        "employer": None,
        "description": None,
        "opportunity": dump,
        **overrides,
    }


def pair(profile_id: str = "p", external_id: str = "1", **overrides: Any) -> GoldPair:
    return GoldPair.model_validate(pair_data(profile_id, external_id, **overrides))


def test_shipped_profiles_pass_the_validator() -> None:
    profiles = load_profiles(PROFILES)
    assert 8 <= len(profiles) <= 10
    assert validate_gold(profiles, [], VOCABULARY) == []


def test_shipped_profiles_cover_empty_and_partial_cases() -> None:
    by_id = {profile.id: profile for profile in load_profiles(PROFILES)}
    assert by_id["empty"].to_profile().career_stage is None
    assert by_id["browsing"].facts == {"career_stage": "undecided"}
    assert by_id["newcomer_cruise"].facts == {"career_stage": "entry", "directions": ["cruise"]}
    assert by_id["onboarded_working"].facts == {
        "career_stage": "working",
        "directions": ["cruise"],
        "hiring_country": ["PH"],
    }


def test_facts_become_stated_with_full_confidence() -> None:
    profile = spec(career_stage="entry", languages=["en"]).to_profile()
    assert profile.career_stage is not None
    assert profile.career_stage.confidence == 1.0
    assert profile.career_stage.source.value == "stated"
    assert profile.career_stage.stated_at == _PROFILE_TIME


def test_profile_id_gives_the_same_user_id() -> None:
    assert spec("a").to_profile().user_id == spec("a").to_profile().user_id
    assert spec("a").to_profile().user_id != spec("b").to_profile().user_id


def test_unknown_vocabulary_key_is_reported() -> None:
    problems = validate_gold([spec(professions=["nope"])], [], VOCABULARY)
    assert len(problems) == 1
    assert "professions" in problems[0]
    assert "nope" in problems[0]


def test_unknown_key_inside_career_goal_is_reported() -> None:
    problems = validate_gold([spec(career_goal={"direction": "nope"})], [], VOCABULARY)
    assert len(problems) == 1
    assert "career_goal.direction" in problems[0]


def test_malformed_country_code_is_reported() -> None:
    problems = validate_gold([spec(hiring_country=["Ukraine"])], [], VOCABULARY)
    assert len(problems) == 1
    assert "hiring_country" in problems[0]


def test_field_that_is_not_a_profile_fact_is_refused() -> None:
    with pytest.raises(ValidationError, match="not profile facts"):
        spec(favourite_colour="blue")


def test_fact_of_wrong_shape_is_refused() -> None:
    with pytest.raises(ValidationError):
        spec(career_stage="captain")


def test_duplicate_profile_id_is_reported() -> None:
    problems = validate_gold([spec("p"), spec("p")], [], VOCABULARY)
    assert problems == ["profile p: duplicate id"]


def test_pair_with_unknown_profile_is_reported() -> None:
    problems = validate_gold([spec("p")], [pair("ghost")], VOCABULARY)
    assert len(problems) == 1
    assert "unknown profile" in problems[0]


def test_duplicate_pair_is_reported() -> None:
    problems = validate_gold([spec("p")], [pair("p"), pair("p")], VOCABULARY)
    assert len(problems) == 1
    assert "duplicate pair" in problems[0]


def test_same_vacancy_for_different_profiles_is_not_a_duplicate() -> None:
    problems = validate_gold([spec("p"), spec("q")], [pair("p"), pair("q")], VOCABULARY)
    assert problems == []


def test_label_must_be_allowed() -> None:
    with pytest.raises(ValidationError):
        pair(label="maybe")


@pytest.mark.parametrize("label", [None, "fit", "no_fit", "unsure"])
def test_allowed_labels_are_accepted(label: str | None) -> None:
    assert pair(label=label).label == (Label(label) if label else None)


def test_opportunity_dump_must_validate() -> None:
    broken = opportunity_dump()
    del broken["normalized"]
    with pytest.raises(ValidationError):
        GoldPair.model_validate(pair_data(opportunity=broken))


def test_gold_round_trips_through_yaml(tmp_path: Path) -> None:
    path = tmp_path / "gold.yaml"
    path.write_text(
        yaml.safe_dump({"pairs": [pair_data(label="fit", note="ok")]}), encoding="utf-8"
    )
    loaded = load_gold(path)
    assert loaded == [pair(label="fit", note="ok")]
    assert validate_gold([spec("p")], loaded, VOCABULARY) == []


def test_identity_is_read_from_the_dump() -> None:
    gold = pair(external_id="42")
    assert gold.source_id == "s"
    assert gold.external_id == "42"
    assert gold.url == "https://example.com/1"
    assert gold.title == "t"


def test_identity_fields_cannot_be_stored_beside_the_dump() -> None:
    with pytest.raises(ValidationError):
        pair(external_id="1", source_id="other")


def test_duplicate_is_decided_by_the_dump() -> None:
    first = pair("p", "1")
    second = pair("p", "2")
    renamed = second.model_copy(update={"opportunity": first.opportunity})
    problems = validate_gold([spec("p")], [first, renamed], VOCABULARY)
    assert len(problems) == 1
    assert "duplicate pair" in problems[0]


def test_stratum_must_be_allowed() -> None:
    with pytest.raises(ValidationError):
        pair(stratum="best")


@pytest.mark.parametrize("stratum", ["top", "zero", "low", "middle", "none", "random"])
def test_allowed_strata_are_accepted(stratum: str) -> None:
    assert pair(stratum=stratum).stratum == stratum


def test_changed_profile_facts_are_reported() -> None:
    sampled = pair("p")
    changed = spec("p", career_stage="entry")
    problems = validate_gold([changed], [sampled], VOCABULARY)
    assert len(problems) == 1
    assert "profile facts changed" in problems[0]


def test_hash_ignores_key_order_and_follows_values() -> None:
    assert (
        spec(directions=["cruise"], career_stage="entry").facts_hash()
        == spec(career_stage="entry", directions=["cruise"]).facts_hash()
    )
    assert spec(career_stage="entry").facts_hash() != spec(career_stage="working").facts_hash()


def test_profile_id_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        ProfileSpec(id="", description="d", facts={})


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
                description=f"d{identifier}",
            ),
            type=OpportunityType.JOB,
            type_confidence=0.9,
            profession=inferred(profession),
            direction=inferred(direction),
            required_languages=inferred(languages),
            quality_score=1.0,
            quality_flags=[],
        ),
        employer="E",
        freshness=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
    )


COOK_FACTS: dict[str, Any] = {
    "professions": ["chef"],
    "directions": ["cruise"],
    "languages": ["en"],
}
COOK = spec("cook", **COOK_FACTS)


def corpus() -> list[Candidate]:
    """Value for COOK: 1-10 are 1.0 over three factors, 19 is 1.0 over one, 11-12 are 2/3,
    13-14 are 1/3, 15-16 are 0.0, 17-18 are None."""
    return [
        *(fake_candidate(i, "chef", "cruise", ["en"]) for i in range(1, 11)),
        *(fake_candidate(i, "chef", "cruise", ["de"]) for i in (11, 12)),
        *(fake_candidate(i, "chef", "yachts", ["de"]) for i in (13, 14)),
        *(fake_candidate(i, "pilot", "yachts", ["de"]) for i in (15, 16)),
        fake_candidate(17, None, None),
        fake_candidate(18, None, None),
        fake_candidate(19, "chef", None),
    ]


def strata(pairs: list[GoldPair]) -> dict[str, int]:
    return {p.stratum: p.opportunity_id for p in pairs}


def value_of(pair: GoldPair) -> float | None:
    return compute_match_score(COOK.to_profile(), pair.opportunity).value


def make_pair(candidate: Candidate, stratum: str, **overrides: Any) -> GoldPair:
    return GoldPair(
        profile_id="cook",
        profile_hash=COOK.facts_hash(),
        opportunity_id=candidate.opportunity_id,
        stratum=stratum,  # pyright: ignore[reportArgumentType]
        employer=None,
        description=None,
        opportunity=candidate.opportunity,
        **overrides,
    )


def test_every_stratum_is_filled_and_none_is_picked() -> None:
    pairs = sample_pairs([COOK], corpus(), []).pairs
    by_stratum = strata(pairs)
    assert sorted(by_stratum) == ["low", "middle", "none", "random", "top", "zero"]
    assert by_stratum["top"] in range(1, 11)
    assert by_stratum["zero"] in (15, 16)
    assert by_stratum["low"] in (13, 14)
    assert by_stratum["middle"] in (11, 12)
    assert by_stratum["none"] in (17, 18)
    assert len({p.opportunity_id for p in pairs}) == 6
    assert all(p.label is None and p.profile_hash == COOK.facts_hash() for p in pairs)


def test_top_prefers_more_comparable_factors_and_middle_differs_from_top() -> None:
    by_pair = {p.stratum: p for p in sample_pairs([COOK], corpus(), []).pairs}
    assert by_pair["top"].opportunity_id != 19
    values = {stratum: value_of(pair) for stratum, pair in by_pair.items()}
    assert values["top"] == 1.0
    assert values["zero"] == 0.0
    assert values["low"] is not None and 0.0 < values["low"] < 0.5
    assert values["middle"] is not None and values["low"] < values["middle"] < 1.0


def test_zero_is_separate_from_low() -> None:
    zero_only = [
        fake_candidate(1, "pilot", "yachts", ["de"]),
        fake_candidate(2, "chef", "cruise", ["en"]),
    ]
    by_stratum = strata(sample_pairs([COOK], zero_only, []).pairs)
    assert by_stratum["zero"] == 1
    assert "low" not in by_stratum


def test_middle_is_empty_without_a_value_between_low_and_top() -> None:
    without_middle = [c for c in corpus() if c.opportunity_id not in (11, 12)]
    by_stratum = strata(sample_pairs([COOK], without_middle, []).pairs)
    assert "middle" not in by_stratum
    assert {"top", "zero", "low", "none", "random"} <= set(by_stratum)


def test_profile_without_none_candidates_skips_that_stratum() -> None:
    valued = [c for c in corpus() if c.opportunity_id not in (17, 18)]
    assert "none" not in strata(sample_pairs([COOK], valued, []).pairs)


def test_few_candidates_do_not_repeat_a_vacancy() -> None:
    pairs = sample_pairs([COOK], corpus()[:2], []).pairs
    assert sorted(p.opportunity_id for p in pairs) == [1, 2]


def test_empty_profile_gets_only_none_and_random() -> None:
    pairs = sample_pairs([spec("empty")], corpus(), []).pairs
    assert sorted(p.stratum for p in pairs) == ["none", "random"]


def test_pick_inside_a_stratum_is_not_always_the_first_or_last_id() -> None:
    tops = {
        strata(sample_pairs([COOK], corpus(), [], seed=seed).pairs)["top"] for seed in range(30)
    }
    assert len(tops) > 2
    assert tops != {1, 10}


def test_profiles_with_the_same_facts_do_not_share_vacancies() -> None:
    twin = spec("twin", **COOK_FACTS)
    pairs = sample_pairs([COOK, twin], corpus(), []).pairs
    mine = {p.opportunity_id for p in pairs if p.profile_id == "cook"}
    theirs = {p.opportunity_id for p in pairs if p.profile_id == "twin"}
    assert len(mine) == len(theirs) == 6
    assert mine.isdisjoint(theirs)


def test_a_vacancy_is_reused_when_the_stratum_has_no_alternative() -> None:
    single = [fake_candidate(1, "chef", "cruise", ["en"])]
    pairs = sample_pairs([COOK, spec("twin", **COOK_FACTS)], single, []).pairs
    assert [p.opportunity_id for p in pairs] == [1, 1]


def test_sampling_is_deterministic() -> None:
    first = sample_pairs([COOK, spec("empty")], corpus(), []).pairs
    second = sample_pairs([COOK, spec("empty")], list(reversed(corpus())), []).pairs
    assert first == second


def test_resampling_keeps_labels_and_the_file_is_identical(tmp_path: Path) -> None:
    path = tmp_path / "gold.yaml"
    first = sample_pairs([COOK], corpus(), []).pairs
    labelled = [first[0].model_copy(update={"label": Label.FIT, "note": "ok"}), *first[1:]]
    write_gold(path, labelled)
    before = path.read_text(encoding="utf-8")

    again = sample_pairs([COOK], corpus(), load_gold(path))
    write_gold(path, again.pairs)
    assert again.dropped_labels == []
    assert path.read_text(encoding="utf-8") == before
    assert load_gold(path)[0].label is Label.FIT


def test_label_survives_when_the_fresh_draw_did_not_pick_the_pair() -> None:
    fresh_ids = {p.opportunity_id for p in sample_pairs([COOK], corpus(), []).pairs}
    unpicked = next(c for c in corpus() if c.opportunity_id not in fresh_ids)
    kept = make_pair(unpicked, "random", label=Label.NO_FIT)
    result = sample_pairs([COOK], corpus(), [kept])
    assert unpicked.opportunity_id not in fresh_ids
    assert kept in result.pairs


def test_fresh_pair_of_a_stratum_with_a_labelled_pair_is_not_added() -> None:
    first = strata(sample_pairs([COOK], corpus(), []).pairs)
    other_top = next(
        c for c in corpus() if c.opportunity_id in range(1, 11) and c.opportunity_id != first["top"]
    )
    kept = make_pair(other_top, "top", label=Label.FIT)
    pairs = sample_pairs([COOK], corpus(), [kept]).pairs
    assert [p for p in pairs if p.stratum == "top"] == [kept]
    assert len(pairs) == 6


def test_label_is_dropped_when_profile_facts_changed() -> None:
    stale = make_pair(corpus()[0], "top", label=Label.FIT)
    changed = spec("cook", professions=["chef"], directions=["yachts"])
    result = sample_pairs([changed], corpus(), [stale])
    assert result.dropped_labels == [stale]
    assert all(p.label is None for p in result.pairs)


def test_labelled_pair_of_unknown_profile_is_an_error() -> None:
    orphan = make_pair(corpus()[0], "top", label=Label.FIT).model_copy(
        update={"profile_id": "gone"}
    )
    with pytest.raises(UnknownProfileError, match="gone"):
        sample_pairs([COOK], corpus(), [orphan])


def test_unlabelled_pair_of_unknown_profile_is_just_resampled() -> None:
    orphan = make_pair(corpus()[0], "top").model_copy(update={"profile_id": "gone"})
    assert sample_pairs([COOK], corpus(), [orphan]).dropped_labels == []


def test_written_file_starts_with_the_header_and_loads(tmp_path: Path) -> None:
    path = tmp_path / "gold.yaml"
    pairs = sample_pairs([COOK], corpus(), []).pairs
    write_gold(path, pairs)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Gold set of")
    assert "enrichment\n# mistakes included" in text
    assert '"I would show this vacancy to this person"' in text
    assert load_gold(path) == pairs


@pytest.fixture
def cli_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRepository:
        def __init__(self, session: object) -> None:
            pass

        def list_feed_candidates(self) -> list[Candidate]:
            return corpus()

    monkeypatch.setenv("SEAWORK_DATABASE_URL", "sqlite://")
    monkeypatch.setattr("seawork.cli.Repository", FakeRepository)


def stale_gold(path: Path) -> str:
    empty = next(p for p in load_profiles(PROFILES) if p.id == "empty")
    stale = make_pair(corpus()[0], "top", label=Label.FIT, note="checked twice").model_copy(
        update={"profile_id": empty.id, "profile_hash": "0" * 16}
    )
    write_gold(path, [stale])
    return path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("cli_corpus")
def test_stale_labels_block_the_write_without_the_flag(tmp_path: Path) -> None:
    from seawork.cli import app

    path = tmp_path / "gold.yaml"
    before = stale_gold(path)
    result = CliRunner().invoke(app, ["eval", "match", "sample", "--out", str(path)])
    assert result.exit_code == 1
    assert "empty" in result.output
    assert "label=fit" in result.output
    assert "note=checked twice" in result.output
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.usefixtures("cli_corpus")
def test_stale_labels_are_dropped_with_the_flag(tmp_path: Path) -> None:
    from seawork.cli import app

    path = tmp_path / "gold.yaml"
    stale_gold(path)
    result = CliRunner().invoke(
        app, ["eval", "match", "sample", "--out", str(path), "--drop-stale"]
    )
    assert result.exit_code == 0
    assert all(pair.label is None for pair in load_gold(path))


@pytest.mark.usefixtures("cli_corpus")
def test_unknown_profile_blocks_the_write(tmp_path: Path) -> None:
    from seawork.cli import app

    path = tmp_path / "gold.yaml"
    orphan = make_pair(corpus()[0], "top", label=Label.FIT).model_copy(
        update={"profile_id": "gone"}
    )
    write_gold(path, [orphan])
    before = path.read_text(encoding="utf-8")
    result = CliRunner().invoke(app, ["eval", "match", "sample", "--out", str(path)])
    assert result.exit_code == 1
    assert "gone" in result.output
    assert path.read_text(encoding="utf-8") == before
