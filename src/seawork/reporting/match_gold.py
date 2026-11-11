"""Format of the Match Score gold set: fixed profiles, frozen vacancies, human labels.

A pair is a fixed profile against a vacancy frozen as the feed scores it
(`Candidate.opportunity`), so the gold set does not depend on the live database. It
also freezes the enrichment mistakes: Match Score is measured over the data the feed
sees, not over ideal data.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from seawork.domain.models import EnrichedOpportunity
from seawork.domain.profile import FACT_FIELDS, Fact, ProfileFactSource, UserProfile
from seawork.ingestion.references import load_yaml
from seawork.storage.profile_vocabulary import ProfileVocabulary, UnknownReferenceKeyError

_PROFILE_NAMESPACE = uuid.UUID("5ea0c0de-0000-4000-8000-000000000001")
_PROFILE_TIME = datetime(2026, 11, 1, tzinfo=UTC)


class Label(StrEnum):
    FIT = "fit"
    NO_FIT = "no_fit"
    UNSURE = "unsure"


class ProfileSpec(BaseModel):
    """A profile as the owner reads it: plain values, every one stated by the person."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str
    facts: dict[str, Any]

    @model_validator(mode="after")
    def facts_are_profile_facts(self) -> Self:
        unknown = sorted(set(self.facts) - FACT_FIELDS)
        if unknown:
            raise ValueError(f"profile {self.id}: not profile facts: {unknown}")
        self.to_profile()
        return self

    def facts_hash(self) -> str:
        canonical = json.dumps(self.facts, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def to_profile(self) -> UserProfile:
        return UserProfile.model_validate(
            {
                "user_id": uuid.uuid5(_PROFILE_NAMESPACE, self.id),
                "created_at": _PROFILE_TIME,
                "updated_at": _PROFILE_TIME,
                **{
                    name: Fact(
                        value=value,
                        source=ProfileFactSource.STATED,
                        confidence=1.0,
                        stated_at=_PROFILE_TIME,
                    )
                    for name, value in self.facts.items()
                },
            }
        )


Stratum = Literal["top", "middle", "bottom", "none", "random"]


class GoldPair(BaseModel):
    """The vacancy identity lives in the dump only; the readable fields cannot drift from it.

    `profile_hash` pins the facts the label was given against, so editing a profile
    after labelling is reported instead of silently invalidating the labels. The
    current score and the factors are deliberately not stored: the labeller must not
    see them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_hash: str
    opportunity_id: int
    stratum: Stratum
    employer: str | None
    description: str | None
    opportunity: EnrichedOpportunity
    label: Label | None = None
    note: str | None = None

    @property
    def source_id(self) -> str:
        return self.opportunity.normalized.source_id

    @property
    def external_id(self) -> str:
        return self.opportunity.normalized.external_id

    @property
    def url(self) -> str:
        return str(self.opportunity.normalized.url)

    @property
    def title(self) -> str:
        return self.opportunity.normalized.title


class _ProfilesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[ProfileSpec]


class _GoldFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairs: list[GoldPair]


def load_profiles(path: Path) -> list[ProfileSpec]:
    return _ProfilesFile.model_validate(load_yaml(path)).profiles


def load_gold(path: Path) -> list[GoldPair]:
    return _GoldFile.model_validate(load_yaml(path)).pairs


def validate_gold(
    profiles: list[ProfileSpec], pairs: list[GoldPair], vocabulary: ProfileVocabulary
) -> list[str]:
    problems: list[str] = []

    hashes: dict[str, str] = {}
    for spec in profiles:
        if spec.id in hashes:
            problems.append(f"profile {spec.id}: duplicate id")
        hashes[spec.id] = spec.facts_hash()
        for field, value in spec.facts.items():
            try:
                vocabulary.check(field, value)
            except UnknownReferenceKeyError as error:
                problems.append(f"profile {spec.id}: {error}")

    seen_pairs: set[tuple[str, str, str]] = set()
    for pair in pairs:
        where = f"{pair.profile_id} x {pair.source_id}/{pair.external_id}"
        if pair.profile_id not in hashes:
            problems.append(f"{where}: unknown profile")
        elif pair.profile_hash != hashes[pair.profile_id]:
            problems.append(f"{where}: profile facts changed since the pair was sampled")
        key = (pair.profile_id, pair.source_id, pair.external_id)
        if key in seen_pairs:
            problems.append(f"{where}: duplicate pair")
        seen_pairs.add(key)
    return problems
