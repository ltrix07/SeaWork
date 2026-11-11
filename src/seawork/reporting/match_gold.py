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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self, cast

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


ProfileKind = Literal["onboarding", "extended"]


class ProfileSpec(BaseModel):
    """A profile as the owner reads it: plain values, every one stated by the person.

    `onboarding` repeats what the bot actually knows about a person after onboarding;
    `extended` describes a future audience with professions, certificates and a goal.
    The report shows the two kinds apart. `structural_none` marks a profile that
    states nothing the formula can compare: its `value=None` does not depend on the
    formula, so the report does not count it as a mistake.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: ProfileKind
    structural_none: bool = False
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


class StaleLabelTargetError(ValueError):
    pass


_FACT_TITLES = {
    "career_stage": "Этап",
    "professions": "Профессии",
    "directions": "Направления",
    "certificates": "Сертификаты",
    "languages": "Языки",
    "preferred_countries": "Желаемые страны",
    "hiring_country": "Страна оформления",
    "career_goal": "Цель",
    "residence_country": "Страна проживания",
    "citizenship": "Гражданство",
    "work_authorization": "Разрешение на работу",
}
DESCRIPTION_LIMIT = 1500
_ANSWERS: dict[str, Label | None] = {
    "y": Label.FIT,
    "n": Label.NO_FIT,
    "?": Label.UNSURE,
    "s": None,
}
_PROMPT = "[y] подходит  [n] не подходит  [?] спорно  [s] пропустить  [q] выйти > "


def _fact_text(value: Any) -> str:
    if isinstance(value, list):
        items = cast(list[Any], value)
        return ", ".join(str(item) for item in items) if items else "нет"
    if isinstance(value, dict):
        parts = cast(dict[str, Any], value)
        return ", ".join(f"{key}: {item}" for key, item in parts.items())
    return str(value)


def trim_description(text: str, limit: int = DESCRIPTION_LIMIT) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)
    if len(compact) <= limit:
        return compact
    cut = compact[:limit].rsplit(None, 1)[0]
    return f"{cut} …\n(описание обрезано, полный текст по ссылке)"


def render_pair(spec: ProfileSpec, pair: GoldPair, position: int, total: int, done: int) -> str:
    facts = [
        f"  {_FACT_TITLES[name]}: {_fact_text(value)}" for name, value in spec.facts.items()
    ] or ["  (ничего не указал)"]
    description = pair.description
    lines = [
        "",
        f"=== Пара {position} из {total} (размечено {done}) ===",
        "",
        "ПРОФИЛЬ",
        f"  {spec.description}",
        "Факты:",
        *facts,
        "",
        "ВАКАНСИЯ",
        f"  {pair.title}",
        f"  Работодатель: {pair.employer or 'не указан'}",
        f"  Источник: {pair.source_id}",
        f"  {pair.url}",
        "",
        trim_description(description) if description else "(описания нет)",
        "",
    ]
    return "\n".join(lines)


def presentation_order(pairs: Sequence[GoldPair]) -> list[int]:
    """Indices of `pairs` in the order they are shown.

    The file is sorted by profile and stratum, so showing it as is would give the
    stratum away. The order is shuffled by a seeded key of each pair's identity: the
    same on every run, and a profile's pairs do not come as a block.
    """

    def key(index: int) -> float:
        pair = pairs[index]
        identity = f"{SAMPLE_SEED}:{pair.profile_id}:{pair.source_id}:{pair.external_id}"
        return random.Random(identity).random()

    return sorted(range(len(pairs)), key=key)


def profile_of_pair(specs: Mapping[str, ProfileSpec], pair: GoldPair) -> ProfileSpec:
    if pair.profile_id not in specs:
        raise UnknownProfileError(f"{pair.profile_id}: no such profile")
    spec = specs[pair.profile_id]
    if pair.profile_hash != spec.facts_hash():
        raise StaleLabelTargetError(
            f"{pair.profile_id}: profile facts changed since sampling; run `eval match sample`"
        )
    return spec


def label_pairs(
    path: Path,
    profiles: Sequence[ProfileSpec],
    ask: Callable[[str], str],
    show: Callable[[str], None],
) -> int:
    """Ask for a label of every unlabelled pair in `presentation_order`, saving after each answer.

    Returns how many labels were given in this run. `q` ends the run; an interrupt
    raised by `ask` loses nothing already answered, because the file is rewritten
    after every label.
    """
    specs = {spec.id: spec for spec in profiles}
    pairs = load_gold(path)
    for pair in pairs:
        profile_of_pair(specs, pair)
    given = 0
    for rank, index in enumerate(presentation_order(pairs)):
        pair = pairs[index]
        if pair.label is not None:
            continue
        done = sum(1 for item in pairs if item.label is not None)
        show(render_pair(specs[pair.profile_id], pair, rank + 1, len(pairs), done))
        answer = ask(_PROMPT).strip().lower()
        while answer not in _ANSWERS and answer != "q":
            answer = ask("Нужно y, n, ?, s или q > ").strip().lower()
        if answer == "q":
            break
        label = _ANSWERS[answer]
        if label is None:
            continue
        note = ask("Заметка (Enter — без заметки) > ").strip()
        pairs[index] = pair.model_copy(update={"label": label, "note": note or None})
        write_gold(path, pairs)
        given += 1
    return given
