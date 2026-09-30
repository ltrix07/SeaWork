"""The result of comparing one person with one opportunity.

Contract: docs/modules/match-score.md. Nothing here is stored (3.6): a `MatchScore`
is computed on demand and carries fingerprints of its inputs, so a cached copy can
never be mistaken for "the score of this vacancy".
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FactorOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    # Not a soft mismatch: one side has nothing to compare (3.1). Such a factor
    # leaves the calculation entirely (3.2).
    NOT_COMPARABLE = "not_comparable"


class FactorVerdict(BaseModel):
    """One factor's outcome together with the ground it stands on (3.4)."""

    model_config = ConfigDict(frozen=True)

    factor: str
    outcome: FactorOutcome
    # 0 for NOT_COMPARABLE, by construction of the engine and not by convention.
    weight: float = Field(ge=0.0)
    opportunity_evidence: str | None = None
    profile_evidence: str | None = None
    opportunity_confidence: float | None = None
    profile_confidence: float | None = None
    # Machine-readable reasons a reader must know about, not prose (the wording is
    # AI Summary's job). The contract's 3.7 demands that a known limitation of a
    # verdict be stated aloud, and the four fields above have nowhere to put it.
    caveats: list[str] = Field(default_factory=list[str])


class MatchScore(BaseModel):
    """`value` must never be read without `comparable_factors` (contract 4)."""

    model_config = ConfigDict(frozen=True)

    # None means "nothing could be compared", never a middle score. A number here
    # would be indistinguishable from a genuine half match, and for a newcomer with
    # an empty profile it would rank every vacancy identically. What to show such a
    # person is Recommendation Engine's question, and None forces it to answer.
    value: float | None = Field(ge=0.0, le=1.0)
    comparable_factors: int = Field(ge=0)
    verdicts: list[FactorVerdict]
    computed_at: datetime
    profile_fingerprint: str
    opportunity_fingerprint: str
