"""Match Score: the three outcomes, and the ways a score can lie quietly.

No database needed. Every case builds its own profile and vacancy.
"""

import datetime
import uuid
from typing import Any

import pytest
from pydantic import HttpUrl

from seawork.domain.enums import ExperienceLevel, OpportunityType, Provenance
from seawork.domain.inferred import Inferred
from seawork.domain.match import FactorOutcome, FactorVerdict
from seawork.domain.models import EnrichedOpportunity, HiringScope, NormalizedOpportunity
from seawork.domain.profile import CareerGoal, CareerStage, Fact, ProfileFactSource, UserProfile
from seawork.scoring import compute_match_score
from seawork.scoring.factors import (
    CAVEAT_HIRING_OFFICE_NOT_NORMALISED,
    CAVEAT_TRAINING_SIGNAL_MISSING,
    FACTORS,
)
from seawork.scoring.weights import BASE_WEIGHT

NOW = datetime.datetime(2026, 9, 30, tzinfo=datetime.UTC)


def stated(value: Any) -> Fact[Any]:
    return Fact(value=value, source=ProfileFactSource.STATED, confidence=1.0, stated_at=NOW)


def inferred(
    value: Any, confidence: float = 0.9, provenance: Provenance = Provenance.RULE
) -> Inferred[Any]:
    return Inferred(value=value, provenance=provenance, confidence=confidence)


def profile(**facts: object) -> UserProfile:
    return UserProfile.model_validate(
        {"user_id": uuid.uuid4(), "created_at": NOW, "updated_at": NOW, **facts}
    )


def vacancy(**fields: object) -> EnrichedOpportunity:
    normalized = NormalizedOpportunity(
        source_id="s", external_id="1", url=HttpUrl("https://example.com/1"), title="t"
    )
    return EnrichedOpportunity.model_validate(
        {
            "normalized": normalized,
            "type": OpportunityType.JOB,
            "type_confidence": 1.0,
            "quality_score": 1.0,
            "quality_flags": [],
            **fields,
        }
    )


def close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-9


def verdict(score_factors: list[FactorVerdict], name: str) -> FactorVerdict:
    return next(v for v in score_factors if v.factor == name)


def outcomes(p: UserProfile, o: EnrichedOpportunity) -> dict[str, FactorOutcome]:
    return {v.factor: v.outcome for v in compute_match_score(p, o, now=NOW).verdicts}


# --- the three outcomes -------------------------------------------------------------


def test_match_mismatch_and_not_comparable_are_distinct() -> None:
    p = profile(professions=stated(["captain"]), directions=stated(["yachting"]))
    o = vacancy(profession=inferred("captain"), direction=inferred("cruise"))
    result = outcomes(p, o)
    assert result["profession"] is FactorOutcome.MATCH
    assert result["direction"] is FactorOutcome.MISMATCH
    assert result["languages"] is FactorOutcome.NOT_COMPARABLE


def test_not_comparable_factor_has_zero_weight() -> None:
    score = compute_match_score(profile(), vacancy(profession=inferred("captain")), now=NOW)
    assert all(v.weight == 0.0 for v in score.verdicts)


# --- trap 2: absence must not leak into the value ----------------------------------


def test_country_present_and_matching_equals_country_absent() -> None:
    p = profile(professions=stated(["captain"]), preferred_countries=stated(["NO"]))
    base = {"profession": inferred("captain")}
    without = compute_match_score(p, vacancy(**base), now=NOW)
    with_country = compute_match_score(p, vacancy(**base, country=inferred("NO")), now=NOW)
    assert without.value == with_country.value == 1.0
    assert without.comparable_factors == 1
    assert with_country.comparable_factors == 2


def test_detailed_vacancy_is_not_punished_for_its_details() -> None:
    # The profile leaves languages and certificates unknown; a vacancy listing both
    # must score exactly like one that lists neither.
    p = profile(professions=stated(["captain"]))
    plain = vacancy(profession=inferred("captain"))
    detailed = vacancy(
        profession=inferred("captain"),
        required_languages=inferred(["en", "es"]),
        required_certificates=inferred(["stcw"]),
    )
    assert compute_match_score(p, plain, now=NOW).value == 1.0
    assert compute_match_score(p, detailed, now=NOW).value == 1.0


def test_value_is_share_of_comparable_weight() -> None:
    p = profile(professions=stated(["captain"]), directions=stated(["yachting"]))
    o = vacancy(profession=inferred("captain", 1.0), direction=inferred("cruise", 1.0))
    score = compute_match_score(p, o, now=NOW)
    assert score.comparable_factors == 2
    assert score.value is not None
    assert close(score.value, 0.5)


# --- trap 3: empty profile ---------------------------------------------------------


def test_empty_profile_gets_verdicts_and_no_value_not_an_error_or_zero() -> None:
    o = vacancy(
        profession=inferred("captain"),
        direction=inferred("cruise"),
        country=inferred("NO"),
        required_languages=inferred(["en"]),
        experience_level=inferred(ExperienceLevel.SENIOR),
        required_certificates=inferred(["stcw"]),
    )
    score = compute_match_score(profile(), o, now=NOW)
    assert score.comparable_factors == 0
    assert score.value is None
    assert len(score.verdicts) == len(FACTORS)
    assert all(v.outcome is FactorOutcome.NOT_COMPARABLE for v in score.verdicts)


def test_empty_profile_and_empty_vacancy() -> None:
    score = compute_match_score(profile(), vacancy(), now=NOW)
    assert score.comparable_factors == 0
    assert score.value is None


def test_zero_confidence_everywhere_does_not_divide_by_zero() -> None:
    p = profile(professions=stated(["captain"]))
    score = compute_match_score(p, vacancy(profession=inferred("captain", 0.0)), now=NOW)
    assert score.value is None
    assert score.comparable_factors == 1


# --- None versus empty list --------------------------------------------------------


def test_no_certificates_stated_is_a_mismatch_but_unknown_is_not() -> None:
    o = vacancy(required_certificates=inferred(["stcw"]))
    assert outcomes(profile(certificates=stated([])), o)["certificates"] is FactorOutcome.MISMATCH
    assert outcomes(profile(), o)["certificates"] is FactorOutcome.NOT_COMPARABLE


def test_empty_preference_list_means_no_preference() -> None:
    p = profile(professions=stated([]))
    assert outcomes(p, vacancy(profession=inferred("captain")))["profession"] is (
        FactorOutcome.NOT_COMPARABLE
    )


def test_vacancy_requiring_nothing_is_not_a_free_match() -> None:
    p = profile(certificates=stated(["stcw"]), languages=stated(["en"]))
    o = vacancy(required_certificates=inferred([]), required_languages=inferred([]))
    result = outcomes(p, o)
    assert result["certificates"] is FactorOutcome.NOT_COMPARABLE
    assert result["languages"] is FactorOutcome.NOT_COMPARABLE


# --- individual factors ------------------------------------------------------------


def test_all_required_languages_must_be_held() -> None:
    o = vacancy(required_languages=inferred(["en", "es"]))
    assert outcomes(profile(languages=stated(["EN", "es", "de"])), o)["languages"] is (
        FactorOutcome.MATCH
    )
    assert outcomes(profile(languages=stated(["en"])), o)["languages"] is FactorOutcome.MISMATCH


def test_preferred_languages_are_ignored() -> None:
    o = vacancy(preferred_languages=inferred(["nl"]))
    assert outcomes(profile(languages=stated(["en"])), o)["languages"] is (
        FactorOutcome.NOT_COMPARABLE
    )


def test_certificate_evidence_comes_from_the_vacancy_and_names_provenance() -> None:
    o = vacancy(required_certificates=inferred(["stcw"], 0.7, Provenance.LLM))
    v = verdict(
        compute_match_score(profile(certificates=stated([])), o, now=NOW).verdicts, "certificates"
    )
    assert v.opportunity_evidence == "stcw [llm]"
    assert v.opportunity_confidence == 0.7
    assert v.profile_evidence == "none"


def test_missing_certificate_carries_training_caveat_until_the_signal_exists() -> None:
    o = vacancy(required_certificates=inferred(["stcw"]))
    mismatch = verdict(
        compute_match_score(profile(certificates=stated([])), o, now=NOW).verdicts, "certificates"
    )
    assert mismatch.outcome is FactorOutcome.MISMATCH
    assert CAVEAT_TRAINING_SIGNAL_MISSING in mismatch.caveats
    match = verdict(
        compute_match_score(profile(certificates=stated(["stcw"])), o, now=NOW).verdicts,
        "certificates",
    )
    assert match.outcome is FactorOutcome.MATCH
    assert match.caveats == []


@pytest.mark.parametrize(
    ("stage", "level", "expected"),
    [
        (CareerStage.ENTRY, ExperienceLevel.ENTRY, FactorOutcome.MATCH),
        (CareerStage.ENTRY, ExperienceLevel.SENIOR, FactorOutcome.MISMATCH),
        (CareerStage.EXPERIENCED, ExperienceLevel.LEAD, FactorOutcome.MATCH),
        (CareerStage.EXPERIENCED, ExperienceLevel.ENTRY, FactorOutcome.MISMATCH),
        (CareerStage.UNDECIDED, ExperienceLevel.SENIOR, FactorOutcome.NOT_COMPARABLE),
    ],
)
def test_experience_against_career_stage(
    stage: CareerStage, level: ExperienceLevel, expected: FactorOutcome
) -> None:
    result = outcomes(
        profile(career_stage=stated(stage)), vacancy(experience_level=inferred(level))
    )
    assert result["experience"] is expected


def test_country_matches_case_insensitively() -> None:
    p = profile(preferred_countries=stated(["no", "SE"]))
    assert outcomes(p, vacancy(country=inferred("NO")))["country"] is FactorOutcome.MATCH
    assert outcomes(p, vacancy(country=inferred("DK")))["country"] is FactorOutcome.MISMATCH


def _scope(
    *,
    anywhere: bool = False,
    countries: list[str] | None = None,
    unresolved: list[str] | None = None,
) -> Inferred[HiringScope]:
    scope = HiringScope(anywhere=anywhere, countries=countries or [], unresolved=unresolved or [])
    return Inferred(value=scope, provenance=Provenance.RULE, confidence=1.0)


def test_a_global_office_matches_everyone() -> None:
    """130 of 447 offices read "Global", and that is an answer, not a gap.

    The employer hires from anywhere, so the axis matches whatever the person
    answered. Scoring it as a mismatch would reject a third of the corpus for
    saying yes to everyone, and scoring it NOT_COMPARABLE would throw away a
    statement the source actually made.
    """
    p = profile(hiring_country=stated(["PH"]))
    v = verdict(
        compute_match_score(p, vacancy(hiring_scope=_scope(anywhere=True)), now=NOW).verdicts,
        "hiring_country",
    )
    assert v.outcome is FactorOutcome.MATCH


def test_a_named_office_country_is_compared() -> None:
    p = profile(hiring_country=stated(["PH", "ID"]))
    matched = verdict(
        compute_match_score(p, vacancy(hiring_scope=_scope(countries=["ID"])), now=NOW).verdicts,
        "hiring_country",
    )
    missed = verdict(
        compute_match_score(p, vacancy(hiring_scope=_scope(countries=["MX"])), now=NOW).verdicts,
        "hiring_country",
    )
    assert matched.outcome is FactorOutcome.MATCH
    assert missed.outcome is FactorOutcome.MISMATCH


def test_an_unresolved_region_stays_incomparable_and_says_so() -> None:
    """ "Caribbean" is not a country and must not be guessed into one.

    Spreading a region over its countries would decide who sees the vacancy on the
    strength of a table nobody wrote. The caveat keeps that visible instead.
    """
    p = profile(hiring_country=stated(["PH"]))
    v = verdict(
        compute_match_score(
            p, vacancy(hiring_scope=_scope(unresolved=["Caribbean"])), now=NOW
        ).verdicts,
        "hiring_country",
    )
    assert v.outcome is FactorOutcome.NOT_COMPARABLE
    assert CAVEAT_HIRING_OFFICE_NOT_NORMALISED in v.caveats


def test_hiring_country_without_a_profile_answer_is_incomparable() -> None:
    v = verdict(
        compute_match_score(
            profile(), vacancy(hiring_scope=_scope(countries=["ID"])), now=NOW
        ).verdicts,
        "hiring_country",
    )
    assert v.outcome is FactorOutcome.NOT_COMPARABLE


def test_career_goal_uses_only_filled_and_comparable_fields() -> None:
    goal = profile(career_goal=stated(CareerGoal(country="NO", profession="captain")))
    # Goal names a country; vacancy names none: that part sits out, profession decides.
    assert outcomes(goal, vacancy(profession=inferred("captain")))["career_goal"] is (
        FactorOutcome.MATCH
    )
    assert outcomes(goal, vacancy(profession=inferred("captain"), country=inferred("SE")))[
        "career_goal"
    ] is (FactorOutcome.MISMATCH)
    assert outcomes(goal, vacancy())["career_goal"] is FactorOutcome.NOT_COMPARABLE
    assert outcomes(profile(), vacancy(country=inferred("NO")))["career_goal"] is (
        FactorOutcome.NOT_COMPARABLE
    )


def test_interests_are_not_a_factor() -> None:
    assert not any("interest" in getattr(f, "__name__", "") for f in FACTORS)
    assert "interest" not in {v.factor for v in compute_match_score(profile(), vacancy()).verdicts}


# --- confidence in the weight ------------------------------------------------------


def test_lower_confidence_lowers_weight() -> None:
    p = profile(professions=stated(["captain"]))
    sure = compute_match_score(p, vacancy(profession=inferred("captain", 1.0)), now=NOW)
    unsure = compute_match_score(p, vacancy(profession=inferred("captain", 0.6)), now=NOW)
    assert verdict(sure.verdicts, "profession").weight == BASE_WEIGHT
    assert close(verdict(unsure.verdicts, "profession").weight, 0.6 * BASE_WEIGHT)


def test_uncertain_mismatch_costs_less_than_a_sure_one() -> None:
    p = profile(professions=stated(["captain"]), directions=stated(["cruise"]))
    good = {"direction": inferred("cruise", 1.0)}
    sure = compute_match_score(p, vacancy(profession=inferred("chef", 1.0), **good), now=NOW)
    unsure = compute_match_score(p, vacancy(profession=inferred("chef", 0.3), **good), now=NOW)
    assert unsure.value is not None
    assert sure.value is not None
    assert unsure.value > sure.value


def test_observed_profile_fact_weighs_less_than_stated() -> None:
    observed = Fact(value=["captain"], source=ProfileFactSource.OBSERVED, confidence=0.5)
    score = compute_match_score(
        profile(professions=observed), vacancy(profession=inferred("captain", 1.0)), now=NOW
    )
    v = verdict(score.verdicts, "profession")
    assert close(v.weight, 0.5)
    assert v.profile_confidence == 0.5


# --- structure and fingerprints ----------------------------------------------------


def test_every_factor_reports_a_verdict_in_a_fixed_order() -> None:
    score = compute_match_score(profile(), vacancy(), now=NOW)
    assert [v.factor for v in score.verdicts] == [
        "profession",
        "direction",
        "languages",
        "experience",
        "certificates",
        "country",
        "hiring_country",
        "career_goal",
    ]


def test_fingerprints_follow_inputs() -> None:
    o = vacancy(profession=inferred("captain"))
    a = compute_match_score(profile(professions=stated(["captain"])), o, now=NOW)
    same = compute_match_score(a_profile := profile(professions=stated(["captain"])), o, now=NOW)
    changed = compute_match_score(
        a_profile.model_copy(update={"professions": stated(["chef"])}), o, now=NOW
    )
    # Distinct user ids make the first two differ; compare like with like.
    assert a.opportunity_fingerprint == same.opportunity_fingerprint
    assert same.profile_fingerprint != changed.profile_fingerprint
    other = compute_match_score(a_profile, vacancy(profession=inferred("chef")), now=NOW)
    assert other.opportunity_fingerprint != same.opportunity_fingerprint
    assert other.profile_fingerprint == same.profile_fingerprint


def test_custom_factor_list_needs_no_engine_change() -> None:
    def always(_p: UserProfile, _o: EnrichedOpportunity) -> FactorVerdict:
        return FactorVerdict(factor="always", outcome=FactorOutcome.MATCH, weight=1.0)

    score = compute_match_score(profile(), vacancy(), factors=[always], now=NOW)
    assert score.value == 1.0
    assert score.comparable_factors == 1


def test_an_internship_does_not_penalise_a_missing_certificate() -> None:
    """Contract 3.7's exception, carried by the type rather than by the text.

    An internship trains by definition, so a candidate who lacks the certificate is
    not a worse fit for it - the employer is closing that gap. Scoring this as a
    MISMATCH would hide exactly the postings a newcomer exists for, which is the
    opposite of what the product promises.
    """
    internship = vacancy(
        type=OpportunityType.INTERNSHIP,
        required_certificates=inferred(["stcw"]),
    )
    job = vacancy(type=OpportunityType.JOB, required_certificates=inferred(["stcw"]))
    holder_of_nothing = profile(certificates=stated([]))

    on_internship = compute_match_score(holder_of_nothing, internship)
    on_job = compute_match_score(holder_of_nothing, job)

    internship_verdict = next(v for v in on_internship.verdicts if v.factor == "certificates")
    job_verdict = next(v for v in on_job.verdicts if v.factor == "certificates")
    assert internship_verdict.outcome is FactorOutcome.NOT_COMPARABLE
    assert job_verdict.outcome is FactorOutcome.MISMATCH
    # And the exception must leave the fraction rather than count as a match: the
    # internship is not credited for a certificate nobody has.
    assert on_internship.comparable_factors == on_job.comparable_factors - 1


def test_the_training_caveat_is_absent_where_the_type_answers_it() -> None:
    """The caveat says "we lack the signal" - it must not appear where we have it."""
    internship = vacancy(
        type=OpportunityType.INTERNSHIP,
        required_certificates=inferred(["stcw"]),
    )
    score = compute_match_score(profile(certificates=stated([])), internship)
    verdict = next(v for v in score.verdicts if v.factor == "certificates")
    assert CAVEAT_TRAINING_SIGNAL_MISSING not in verdict.caveats


def test_evidence_shows_the_words_when_a_quote_survived() -> None:
    """The point of verifying a quote is being able to show it.

    The LLM layer checks every quote against the source text and, until E2.3, threw
    it away - leaving an explanation able to say "stcw" but not why. A person reading
    "we recommend this because you lack a valid STCW certificate" is being told
    something; "because you lack stcw" is being shown our internal key.
    """
    quoted = vacancy(
        required_certificates=Inferred(
            value=["stcw"],
            provenance=Provenance.LLM,
            confidence=0.9,
            quotes={"stcw": "valid STCW documents"},
        )
    )
    score = compute_match_score(profile(certificates=stated([])), quoted)
    verdict = next(v for v in score.verdicts if v.factor == "certificates")
    assert verdict.opportunity_evidence is not None
    assert "valid STCW documents" in verdict.opportunity_evidence


def test_evidence_falls_back_to_the_value_without_a_quote() -> None:
    """Rule-derived fields carry no quote yet, and must still explain themselves."""
    unquoted = vacancy(required_certificates=inferred(["stcw"]))
    score = compute_match_score(profile(certificates=stated([])), unquoted)
    verdict = next(v for v in score.verdicts if v.factor == "certificates")
    assert verdict.opportunity_evidence is not None and "stcw" in verdict.opportunity_evidence


def test_an_internship_still_credits_a_certificate_the_person_holds() -> None:
    """The training exception may only help a record that was failing the factor.

    Applied before the comparison it did the opposite of its purpose: a candidate
    holding every required certificate lost the MATCH on an internship, so the same
    vacancy scored 0.5 as a job and 0.0 as an internship. An exception that lowers
    the score of the people it was written for is worse than no exception.
    """
    holder = profile(certificates=stated(["stcw"]), directions=stated(["yachting"]))
    as_job = compute_match_score(
        holder,
        vacancy(
            type=OpportunityType.JOB,
            required_certificates=inferred(["stcw"]),
            direction=inferred("cruise"),
        ),
        now=NOW,
    )
    as_internship = compute_match_score(
        holder,
        vacancy(
            type=OpportunityType.INTERNSHIP,
            required_certificates=inferred(["stcw"]),
            direction=inferred("cruise"),
        ),
        now=NOW,
    )
    assert verdict(as_internship.verdicts, "certificates").outcome is FactorOutcome.MATCH
    assert as_internship.value == as_job.value


def test_an_empty_hiring_country_means_anywhere_suits_me() -> None:
    """An empty list is an answer, and the answer is "no preference".

    `_membership` already reads an empty preference that way. Without the same rule
    here, a person who cleared the field got a hard miss on every office-bound
    vacancy - the exact opposite of what clearing it says.
    """
    anywhere_suits = profile(hiring_country=stated([]))
    score = compute_match_score(
        anywhere_suits, vacancy(hiring_scope=_scope(countries=["ID"])), now=NOW
    )
    assert verdict(score.verdicts, "hiring_country").outcome is FactorOutcome.NOT_COMPARABLE


def test_a_partly_unread_office_keeps_its_caveat() -> None:
    """A country decides the verdict, but an unread region must stay visible."""
    score = compute_match_score(
        profile(hiring_country=stated(["ID"])),
        vacancy(hiring_scope=_scope(countries=["ID"], unresolved=["Caribbean"])),
        now=NOW,
    )
    hiring = verdict(score.verdicts, "hiring_country")
    assert hiring.outcome is FactorOutcome.MATCH
    assert CAVEAT_HIRING_OFFICE_NOT_NORMALISED in hiring.caveats
