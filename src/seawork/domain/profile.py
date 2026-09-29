"""The person side of the product: what a user said, and what we saw them do.

Contract: docs/modules/user-profile.md. Two rules from it shape every type here.
An absent fact (`None`) means "unknown" and an empty list means "known to be
empty"; they must stay distinguishable, because matching treats them differently
(3.2). And a fact always carries where it came from (3.3).
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileFactSource(StrEnum):
    STATED = "stated"
    OBSERVED = "observed"
    IMPORTED = "imported"


class CareerStage(StrEnum):
    UNDECIDED = "undecided"
    ENTRY = "entry"
    WORKING = "working"
    EXPERIENCED = "experienced"


class BehaviourAction(StrEnum):
    VIEWED = "viewed"
    SAVED = "saved"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    REOPENED = "reopened"


class CareerGoal(BaseModel):
    """Where the person is heading, as opposed to what they are looking at (3.5).

    The contract names this type but never defines it. Every field is optional and
    holds a key from the shared reference vocabularies, so a goal can be as vague as
    "Norway" or as specific as "captain on a research vessel". SPEC.md 2.8 defers the
    detail of a goal, so this shape is provisional and has not met a real user.
    """

    model_config = ConfigDict(frozen=True)

    profession: str | None = Field(default=None, min_length=1)
    direction: str | None = Field(default=None, min_length=1)
    country: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def has_content(self) -> "CareerGoal":
        # A goal with nothing in it is not a goal, and matching would read the
        # emptiness as a constraint instead of as "no goal stated".
        if self.profession is None and self.direction is None and self.country is None:
            raise ValueError("CareerGoal needs at least one of profession, direction, country")
        return self


class Fact[T](BaseModel):
    """A value together with its origin, the same device as `Inferred` for vacancies."""

    model_config = ConfigDict(frozen=True)

    value: T
    source: ProfileFactSource
    confidence: float = Field(ge=0.0, le=1.0)
    stated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def stated_is_certain(self) -> "Fact[T]":
        # A person's own answer is not a guess; a lower number here would let later
        # code treat it as one and overwrite it as if it were an inference.
        if self.source is ProfileFactSource.STATED and self.confidence != 1.0:
            raise ValueError("STATED facts must have confidence=1.0")
        return self


class UserProfile(BaseModel):
    """Every fact is optional: an empty profile is a working state (3.1)."""

    user_id: UUID
    career_stage: Fact[CareerStage] | None = None
    career_goal: Fact[CareerGoal] | None = None
    professions: Fact[list[str]] | None = None
    directions: Fact[list[str]] | None = None
    certificates: Fact[list[str]] | None = None
    languages: Fact[list[str]] | None = None
    residence_country: Fact[str] | None = None
    citizenship: Fact[str] | None = None
    work_authorization: Fact[list[str]] | None = None
    preferred_countries: Fact[list[str]] | None = None
    hiring_country: Fact[list[str]] | None = None
    created_at: datetime
    updated_at: datetime


# Every profile field that holds a fact. Derived, not typed out, so a new fact added
# to the model cannot be forgotten by the storage layer.
FACT_FIELDS: frozenset[str] = frozenset(
    name for name in UserProfile.model_fields if name not in {"user_id", "created_at", "updated_at"}
)


class BehaviourEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    opportunity_id: int
    action: BehaviourAction
    dwell_seconds: int | None = Field(default=None, ge=0)
    occurred_at: datetime


class InterestCounts(BaseModel):
    """Raw counts of what a person did, deliberately not a score.

    How many views make an interest, and how sure we are of it, is an open question
    (contract 6.1) that only real usage can answer. Any weight put here now would be
    a guess that later code mistakes for a measurement.

    A `None` key collects events on opportunities that state no direction or
    profession, or that no longer exist: they were seen, but say nothing about taste.
    """

    by_action: dict[BehaviourAction, int] = Field(default_factory=dict[BehaviourAction, int])
    by_direction: dict[str | None, dict[BehaviourAction, int]] = Field(
        default_factory=dict[str | None, dict[BehaviourAction, int]]
    )
    by_profession: dict[str | None, dict[BehaviourAction, int]] = Field(
        default_factory=dict[str | None, dict[BehaviourAction, int]]
    )


class UserDataExport(BaseModel):
    """Everything held about one person, for a data-access request."""

    profile: UserProfile
    events: list[BehaviourEvent]
