"""Format of the Match Score gold set: fixed profiles, frozen vacancies, human labels.

A pair is a fixed profile against a vacancy frozen as the feed scores it
(`Candidate.opportunity`), so the gold set does not depend on the live database. It
also freezes the enrichment mistakes: Match Score is measured over the data the feed
sees, not over ideal data.
"""

import hashlib
import json
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from seawork.domain.models import EnrichedOpportunity
from seawork.domain.profile import FACT_FIELDS, Fact, ProfileFactSource, UserProfile
from seawork.domain.recommendation import Candidate
from seawork.ingestion.references import load_yaml
from seawork.scoring.engine import compute_match_score
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


Stratum = Literal["top", "zero", "low", "middle", "none", "random"]


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


SAMPLE_SEED = 20261111
_STRATUM_ORDER: tuple[Stratum, ...] = ("top", "zero", "low", "middle", "none", "random")

_HEADER = """\
# Gold set of "profile x vacancy" pairs for Match Score. Written and rewritten by
# `seawork eval match sample`; only `label` and `note` are edited by hand.
#
# Each vacancy is frozen as the feed sees it (`Candidate.opportunity`), enrichment
# mistakes included: Match Score is measured over the feed's data, not over ideal data.
# The current score and the factors are deliberately not stored: the labeller must
# not see them, or the label would repeat the formula.
#
# Labelling rule: judge by the profile and the vacancy summary as a person sees them.
# fit = "I would show this vacancy to this person"; no_fit = "I would not";
# unsure = "I cannot decide from this data". Not labelled yet: null.
# profile_hash pins a label to the profile facts it was given against; if the profile
# changed, the label is not carried over by the next sampling.
"""


class UnknownProfileError(ValueError):
    pass


@dataclass(frozen=True)
class SampleResult:
    pairs: list[GoldPair]
    dropped_labels: list[GoldPair]


@dataclass(frozen=True)
class _Scored:
    candidate: Candidate
    value: float | None
    comparable: int


def _stratum_pools(scored: list[_Scored]) -> dict[Stratum, list[_Scored]]:
    positive = [item for item in scored if item.value is not None and item.value > 0.0]
    pools: dict[Stratum, list[_Scored]] = {
        "top": [],
        "zero": [item for item in scored if item.value == 0.0],
        "low": [],
        "middle": [],
        "none": [item for item in scored if item.value is None],
    }
    if positive:
        top_value = max(item.value for item in positive if item.value is not None)
        at_top = [item for item in positive if item.value == top_value]
        most_comparable = max(item.comparable for item in at_top)
        pools["top"] = [item for item in at_top if item.comparable == most_comparable]
        below = sorted({item.value for item in positive if item.value is not None} - {top_value})
        if below:
            pools["low"] = [item for item in positive if item.value == below[0]]
            between = below[1:]
            if between:
                median = between[len(between) // 2]
                pools["middle"] = [item for item in positive if item.value == median]
    return pools


def _select_strata(
    scored: list[_Scored],
    seed: str,
    profile_id: str,
    skip: set[Stratum],
    taken: set[int],
    used_elsewhere: set[int],
) -> list[tuple[Stratum, Candidate]]:
    pools = _stratum_pools(scored)
    chosen: list[tuple[Stratum, Candidate]] = []
    for stratum in _STRATUM_ORDER:
        if stratum in skip:
            continue
        if stratum == "random":
            pool = list(scored)
        else:
            pool = pools[stratum]
        free = sorted(
            (item.candidate for item in pool if item.candidate.opportunity_id not in taken),
            key=lambda candidate: candidate.opportunity_id,
        )
        fresh = [c for c in free if c.opportunity_id not in used_elsewhere]
        options = fresh or free
        if not options:
            continue
        picked = random.Random(f"{seed}:{profile_id}:{stratum}").choice(options)
        taken.add(picked.opportunity_id)
        chosen.append((stratum, picked))
    return chosen


def sample_pairs(
    profiles: Sequence[ProfileSpec],
    candidates: Sequence[Candidate],
    previous: Sequence[GoldPair],
    *,
    seed: int = SAMPLE_SEED,
) -> SampleResult:
    """Pick up to six vacancies per profile, stratified by the current score.

    Strata: `top` is the highest positive value (ties go to more comparable factors),
    `zero` is value 0.0, `low` the smallest positive value other than top, `middle` the
    median of the distinct positive values strictly between low and top, `none` is
    value None, `random` a draw from the rest. A stratum with no vacancy stays empty
    and is not filled from another. Inside a stratum the vacancy is a seeded random
    choice, not the first id, and one already picked for another profile is avoided
    when the stratum has an alternative.

    A labelled pair whose profile facts are unchanged is kept as it was labelled, even
    when a fresh draw would not pick it, and its stratum is not sampled again for that
    profile. A label given against changed facts is returned in `dropped_labels`. A
    labelled pair of an unknown profile is an error: dropping it would lose a label.
    """
    known = {spec.id for spec in profiles}
    orphans = [pair for pair in previous if pair.label is not None and pair.profile_id not in known]
    if orphans:
        names = ", ".join(f"{p.profile_id} x {p.source_id}/{p.external_id}" for p in orphans)
        raise UnknownProfileError(f"labelled pairs of unknown profiles: {names}")

    ordered = sorted(candidates, key=lambda candidate: candidate.opportunity_id)
    labelled = [pair for pair in previous if pair.label is not None]
    pairs: list[GoldPair] = []
    dropped: list[GoldPair] = []
    used_elsewhere: set[int] = set()
    for spec in profiles:
        profile = spec.to_profile()
        profile_hash = spec.facts_hash()
        scored: list[_Scored] = []
        for candidate in ordered:
            match = compute_match_score(profile, candidate.opportunity)
            scored.append(_Scored(candidate, match.value, match.comparable_factors))
        own = [pair for pair in labelled if pair.profile_id == spec.id]
        kept = [pair for pair in own if pair.profile_hash == profile_hash]
        dropped.extend(pair for pair in own if pair.profile_hash != profile_hash)
        taken = {pair.opportunity_id for pair in kept}
        fresh = [
            GoldPair(
                profile_id=spec.id,
                profile_hash=profile_hash,
                opportunity_id=candidate.opportunity_id,
                stratum=stratum,
                employer=candidate.employer,
                description=candidate.opportunity.normalized.description,
                opportunity=candidate.opportunity,
            )
            for stratum, candidate in _select_strata(
                scored,
                str(seed),
                spec.id,
                {pair.stratum for pair in kept},
                taken,
                used_elsewhere,
            )
        ]
        used_elsewhere |= {pair.opportunity_id for pair in kept + fresh}
        pairs.extend(
            sorted(
                [*kept, *fresh],
                key=lambda pair: (_STRATUM_ORDER.index(pair.stratum), pair.opportunity_id),
            )
        )
    return SampleResult(pairs=pairs, dropped_labels=dropped)


def write_gold(path: Path, pairs: Sequence[GoldPair]) -> None:
    body = yaml.safe_dump(
        {"pairs": [pair.model_dump(mode="json") for pair in pairs]},
        allow_unicode=True,
        sort_keys=False,
    )
    path.write_text(_HEADER + "\n" + body, encoding="utf-8")
