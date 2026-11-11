"""The gold set format and its validator: one test per rule the validator enforces."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import HttpUrl, ValidationError

from seawork.domain.enums import OpportunityType
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.reporting.match_gold import (
    _PROFILE_TIME,  # pyright: ignore[reportPrivateUsage]
    GoldPair,
    Label,
    ProfileSpec,
    load_gold,
    load_profiles,
    validate_gold,
)
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


@pytest.mark.parametrize("stratum", ["top", "middle", "bottom", "none", "random"])
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
