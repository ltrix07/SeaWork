"""Turns factor verdicts into a `MatchScore`.

Computed on demand and never stored as truth (contract 3.6). The fingerprints are
hashes of the inputs' full content, because neither side carries a version number;
a cache keyed on them cannot serve an answer to an old question.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from seawork.domain.match import FactorOutcome, FactorVerdict, MatchScore
from seawork.domain.models import EnrichedOpportunity
from seawork.domain.profile import UserProfile
from seawork.scoring.factors import FACTORS, Factor


def _fingerprint(model: BaseModel) -> str:
    # Content hash of the model's JSON. `model_dump_json` emits fields in declaration
    # order and sorts nothing, so equal content hashes equally only while the class
    # keeps its field order. Reordering the model would change every fingerprint,
    # which costs a cache miss and never a wrong answer - the property the key exists
    # for. An earlier comment here claimed sorted keys, which the call does not do.
    payload = model.model_dump_json()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value(verdicts: Sequence[FactorVerdict]) -> float | None:
    comparable = [v for v in verdicts if v.outcome is not FactorOutcome.NOT_COMPARABLE]
    total = sum(v.weight for v in comparable)
    # Zero total weight covers both "no factor compared" and "every compared factor
    # had zero confidence". Neither is evidence, and dividing would raise. The answer
    # is None rather than a placeholder: 0.0 reads "does not suit", 0.5 reads "half
    # matched", and both are claims we cannot make about an empty profile.
    if total <= 0.0:
        return None
    matched = sum(v.weight for v in comparable if v.outcome is FactorOutcome.MATCH)
    # A share of what could be compared, never of what could exist (contract 3.2):
    # NOT_COMPARABLE verdicts are absent from both sides of this fraction.
    return matched / total


def compute_match_score(
    profile: UserProfile,
    opportunity: EnrichedOpportunity,
    *,
    factors: Sequence[Factor] = FACTORS,
    now: datetime | None = None,
) -> MatchScore:
    verdicts = [factor(profile, opportunity) for factor in factors]
    return MatchScore(
        value=_value(verdicts),
        comparable_factors=sum(v.outcome is not FactorOutcome.NOT_COMPARABLE for v in verdicts),
        verdicts=verdicts,
        computed_at=now or datetime.now(UTC),
        profile_fingerprint=_fingerprint(profile),
        opportunity_fingerprint=_fingerprint(opportunity),
    )
