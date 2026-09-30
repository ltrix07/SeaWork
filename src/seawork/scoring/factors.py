"""Match Score factors: one small function each (contract 5).

A factor takes a profile and an opportunity and returns one `FactorVerdict`. To add a
factor, write a function and append it to `FACTORS`; the engine does not change.

Two rules hold in every function here and are the reason this module exists:

* Missing data on EITHER side is NOT_COMPARABLE, never MISMATCH (3.1, 3.2). Scoring
  the absence of a country as a miss would drop the 605 of 887 vacancies that have
  none, and would punish detailed postings for being checkable.
* `None` and an empty list differ (user-profile 3.2). Where "known to be empty" is
  itself information - a person who holds no certificate - it is used as such.
  Where an empty list would only mean "no preference", it is treated as unknown.

Interests are NOT a factor in v1, on purpose: user-profile 6.1 leaves open how many
events make an interest, and any count chosen now would be a guess passed off as a
measurement. This is a decision, not an omission; see `INTERESTS_EXCLUDED_REASON`.
"""

from collections.abc import Callable, Collection
from typing import cast

from seawork.domain.enums import ExperienceLevel
from seawork.domain.inferred import Inferred
from seawork.domain.match import FactorOutcome, FactorVerdict
from seawork.domain.models import EnrichedOpportunity
from seawork.domain.profile import CareerStage, Fact, UserProfile
from seawork.scoring.weights import factor_weight

INTERESTS_EXCLUDED_REASON = (
    "Interests are not scored in v1: how many events make an interest is an open "
    "question (user-profile 6.1) that only real usage can answer."
)

# Caveat keys; machine-readable on purpose, the words belong to AI Summary.
CAVEAT_TRAINING_SIGNAL_MISSING = "training_offer_signal_unavailable"
CAVEAT_HIRING_OFFICE_NOT_NORMALISED = "hiring_office_not_normalised"

# Which career stages accept which experience levels. PROVISIONAL and unmeasured: the
# contract names both fields but never says how they relate. Neighbouring levels are
# allowed so that the edge of a stage is not a cliff. UNDECIDED has no entry because
# it is an answer that says nothing about level.
_STAGE_LEVELS: dict[CareerStage, frozenset[ExperienceLevel]] = {
    CareerStage.ENTRY: frozenset({ExperienceLevel.ENTRY, ExperienceLevel.JUNIOR}),
    CareerStage.WORKING: frozenset({ExperienceLevel.JUNIOR, ExperienceLevel.MID}),
    CareerStage.EXPERIENCED: frozenset(
        {ExperienceLevel.MID, ExperienceLevel.SENIOR, ExperienceLevel.LEAD}
    ),
}


def _fold(values: Collection[str]) -> set[str]:
    return {value.casefold() for value in values}


def _cite[T](inferred: Inferred[T]) -> str:
    # Provenance rides inside the evidence string: the reader needs to know whether a
    # value came from the source, a rule or a model before trusting it (3.4).
    value: object = inferred.value
    if isinstance(value, list):
        text = ", ".join(str(item) for item in cast(list[object], value))
    else:
        text = str(value)
    return f"{text} [{inferred.provenance.value}]"


def _list_text(fact: Fact[list[str]]) -> str:
    return ", ".join(fact.value)


def _verdict(
    factor: str,
    outcome: FactorOutcome,
    *,
    opportunity_confidence: float | None = None,
    profile_confidence: float | None = None,
    opportunity_evidence: str | None = None,
    profile_evidence: str | None = None,
    caveats: list[str] | None = None,
) -> FactorVerdict:
    return FactorVerdict(
        factor=factor,
        outcome=outcome,
        weight=factor_weight(outcome, opportunity_confidence, profile_confidence),
        opportunity_evidence=opportunity_evidence,
        profile_evidence=profile_evidence,
        opportunity_confidence=opportunity_confidence,
        profile_confidence=profile_confidence,
        caveats=caveats or [],
    )


def _not_comparable(
    factor: str,
    *,
    opportunity_evidence: str | None = None,
    profile_evidence: str | None = None,
    caveats: list[str] | None = None,
) -> FactorVerdict:
    # Evidence is still reported on the side that has it: "the vacancy names Spain,
    # the person named nothing" explains why the factor sat out.
    return _verdict(
        factor,
        FactorOutcome.NOT_COMPARABLE,
        opportunity_evidence=opportunity_evidence,
        profile_evidence=profile_evidence,
        caveats=caveats,
    )


def _membership(
    factor: str,
    opportunity: Inferred[str] | None,
    profile: Fact[list[str]] | None,
) -> FactorVerdict:
    """The vacancy's single value against the person's list of acceptable ones."""
    opportunity_text = _cite(opportunity) if opportunity is not None else None
    # An empty list of preferences means "no preference", not "nothing is acceptable";
    # reading it as the latter would mismatch every vacancy for a person who
    # answered the question with "any".
    if opportunity is None or profile is None or not profile.value:
        return _not_comparable(
            factor,
            opportunity_evidence=opportunity_text,
            profile_evidence=_list_text(profile) if profile is not None else None,
        )
    hit = opportunity.value.casefold() in _fold(profile.value)
    return _verdict(
        factor,
        FactorOutcome.MATCH if hit else FactorOutcome.MISMATCH,
        opportunity_confidence=opportunity.confidence,
        profile_confidence=profile.confidence,
        opportunity_evidence=opportunity_text,
        profile_evidence=_list_text(profile),
    )


def profession_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    return _membership("profession", opportunity.profession, profile.professions)


def direction_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    return _membership("direction", opportunity.direction, profile.directions)


def country_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    # Only shore vacancies have a country of work; a vessel contract has none by the
    # nature of the trade (ingestion 4.3.1), and lands in NOT_COMPARABLE here.
    return _membership("country", opportunity.country, profile.preferred_countries)


def hiring_country_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    """Where the person can be employed against the vacancy's hiring office.

    Always NOT_COMPARABLE in v1, and deliberately so. The vacancy side does not exist
    yet: `recruitment_office_raw` is free text, and task A4 must turn it into a
    country, a region, or "anywhere" (user-profile 5.2). Comparing the raw string
    would mismatch all 130 `Global` offices, which mean "hires everywhere" and must
    match everyone. The factor stays in the list so that the axis is visible in every
    explanation and starts working when A4 lands.
    """
    return _not_comparable(
        "hiring_country",
        opportunity_evidence=opportunity.normalized.recruitment_office_raw,
        profile_evidence=_list_text(profile.hiring_country) if profile.hiring_country else None,
        caveats=[CAVEAT_HIRING_OFFICE_NOT_NORMALISED],
    )


def _held_all(
    factor: str,
    required: Inferred[list[str]] | None,
    held: Fact[list[str]] | None,
    caveats: list[str] | None = None,
) -> FactorVerdict:
    """Every requirement must be held: a shortfall of one is a mismatch."""
    required_text = _cite(required) if required is not None else None
    # Nothing required is nothing to compare, not a free MATCH: rewarding a vacancy
    # for asking little would let two-line postings outscore detailed ones.
    if required is None or not required.value or held is None:
        return _not_comparable(
            factor,
            opportunity_evidence=required_text,
            profile_evidence=_list_text(held) if held is not None else None,
        )
    # `held.value == []` is a real answer here ("I have none") and falls through to
    # MISMATCH, which is the distinction None-versus-empty was introduced to keep.
    missing = _fold(required.value) - _fold(held.value)
    return _verdict(
        factor,
        FactorOutcome.MISMATCH if missing else FactorOutcome.MATCH,
        opportunity_confidence=required.confidence,
        profile_confidence=held.confidence,
        opportunity_evidence=required_text,
        profile_evidence=_list_text(held) or "none",
        caveats=caveats,
    )


def language_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    # Only REQUIRED languages are compared. A preferred one may not raise the score in
    # v1 and its absence must never lower it (models.py); leaving the field out keeps
    # the two behaviours from blurring.
    return _held_all("languages", opportunity.required_languages, profile.languages)


def certificate_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    # Contract 3.7: a missing certificate is a MISMATCH unless the vacancy offers
    # training, in which case it should be NOT_COMPARABLE. The data has no "training
    # offered" signal yet (task E2.2), so the exception cannot fire. The caveat says
    # so aloud: a newcomer seeing a low score on a vacancy built for newcomers must be
    # able to learn that the rule lacked the information, not that they are unfit.
    verdict = _held_all(
        "certificates",
        opportunity.required_certificates,
        profile.certificates,
        caveats=[CAVEAT_TRAINING_SIGNAL_MISSING],
    )
    if verdict.outcome is FactorOutcome.MISMATCH:
        return verdict
    return verdict.model_copy(update={"caveats": []})


def experience_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    level = opportunity.experience_level
    stage = profile.career_stage
    level_text = _cite(level) if level is not None else None
    stage_text = stage.value.value if stage is not None else None
    if level is None or stage is None or stage.value is CareerStage.UNDECIDED:
        return _not_comparable(
            "experience", opportunity_evidence=level_text, profile_evidence=stage_text
        )
    hit = level.value in _STAGE_LEVELS[stage.value]
    return _verdict(
        "experience",
        FactorOutcome.MATCH if hit else FactorOutcome.MISMATCH,
        opportunity_confidence=level.confidence,
        profile_confidence=stage.confidence,
        opportunity_evidence=level_text,
        profile_evidence=stage_text,
    )


def goal_factor(profile: UserProfile, opportunity: EnrichedOpportunity) -> FactorVerdict:
    """The career goal, coarsely (contract 5): only the goal fields that are filled.

    A goal aims at what the person is heading for, so it is a separate axis from what
    they currently hold. It overlaps profession/direction/country when the two agree,
    which counts that agreement twice; the contract accepts a rough treatment here and
    the overlap is left for calibration to judge. Comparable parts must all match.
    """
    fact = profile.career_goal
    if fact is None:
        return _not_comparable("career_goal")
    goal = fact.value
    pairs = (
        ("profession", goal.profession, opportunity.profession),
        ("direction", goal.direction, opportunity.direction),
        ("country", goal.country, opportunity.country),
    )
    compared = [
        (name, wanted, actual)
        for name, wanted, actual in pairs
        if wanted is not None and actual is not None
    ]
    goal_text = ", ".join(f"{name}={wanted}" for name, wanted, _ in pairs if wanted is not None)
    if not compared:
        return _not_comparable("career_goal", profile_evidence=goal_text)
    all_match = all(wanted.casefold() == actual.value.casefold() for _, wanted, actual in compared)
    # The vacancy side is the weakest link among the compared fields, so its
    # confidence is what the weight should follow.
    weakest = min((actual for _, _, actual in compared), key=lambda item: item.confidence)
    return _verdict(
        "career_goal",
        FactorOutcome.MATCH if all_match else FactorOutcome.MISMATCH,
        opportunity_confidence=weakest.confidence,
        profile_confidence=fact.confidence,
        opportunity_evidence=", ".join(f"{name}={actual.value}" for name, _, actual in compared),
        profile_evidence=goal_text,
    )


Factor = Callable[[UserProfile, EnrichedOpportunity], FactorVerdict]

# The v1 list of contract 5, plus the coarse goal. Order is the order of the
# explanation. Adding a factor is adding a line here.
FACTORS: tuple[Factor, ...] = (
    profession_factor,
    direction_factor,
    language_factor,
    experience_factor,
    certificate_factor,
    country_factor,
    hiring_country_factor,
    goal_factor,
)
