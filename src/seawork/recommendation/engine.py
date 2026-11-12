from collections.abc import Iterable, Sequence
from datetime import datetime
from itertools import groupby

from seawork.domain.match import MatchScore
from seawork.domain.profile import BehaviourAction, BehaviourEvent, UserProfile
from seawork.domain.recommendation import Candidate, FeedHistory, Recommendation
from seawork.scoring import compute_match_score

type Scored = tuple[Candidate, MatchScore]


def history_from(events: Sequence[BehaviourEvent], delivered: Iterable[int]) -> FeedHistory:
    """The last of dismissed/reopened decides; events with equal times keep their order."""
    dismissed: set[int] = set()
    for event in sorted(events, key=lambda e: e.occurred_at):
        if event.action is BehaviourAction.DISMISSED:
            dismissed.add(event.opportunity_id)
        elif event.action is BehaviourAction.REOPENED:
            dismissed.discard(event.opportunity_id)
    return FeedHistory(delivered_ids=frozenset(delivered), dismissed_ids=frozenset(dismissed))


def recommend(
    profile: UserProfile,
    candidates: Sequence[Candidate],
    history: FeedHistory,
    *,
    limit: int,
    now: datetime,
) -> list[Recommendation]:
    excluded = history.delivered_ids | history.dismissed_ids
    scored: list[Scored] = []
    for candidate in candidates:
        if candidate.opportunity_id in excluded:
            continue
        score = compute_match_score(profile, candidate.opportunity, now=now)
        if score.value == 0.0:
            continue
        scored.append((candidate, score))
    scored.sort(key=_order)
    groups = _group(scored)
    chosen = _round_robin(groups)[:limit]
    return [
        Recommendation(
            opportunity_id=candidate.opportunity_id,
            score=score,
            rank=rank,
            similar_ids=tuple(similar.opportunity_id for similar, _ in rest),
        )
        for rank, ((candidate, score), rest) in enumerate(chosen, start=1)
    ]


def _order(pair: Scored) -> tuple[bool, float, int, float, int]:
    candidate, score = pair
    return (
        score.value is None,
        -(score.value or 0.0),
        -score.comparable_factors,
        -candidate.freshness.timestamp(),
        candidate.opportunity_id,
    )


def _normalise(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(text.casefold().split()) or None


_BRAND_CODES = frozenset({"HAL", "SBN"})


def _position(title: str) -> str | None:
    parts = title.split(" - ")
    brand_prefix = len(parts) == 2 and parts[0] in _BRAND_CODES
    chosen = parts[1] if len(parts) >= 3 or brand_prefix else parts[0]
    return _normalise(chosen)


def _group_key(candidate: Candidate) -> tuple[str, str] | None:
    employer = _normalise(candidate.employer)
    position = _position(candidate.opportunity.normalized.title)
    if employer is None or position is None:
        return None
    return employer, position


type Group = tuple[Scored, list[Scored]]


def _group(ordered: Sequence[Scored]) -> list[Group]:
    groups: list[Group] = []
    by_key: dict[tuple[str, str], Group] = {}
    for pair in ordered:
        key = _group_key(pair[0])
        if key is not None and key in by_key:
            by_key[key][1].append(pair)
            continue
        group: Group = (pair, [])
        groups.append(group)
        if key is not None:
            by_key[key] = group
    return groups


def _tier(group: Group) -> tuple[bool, float | None, int]:
    score = group[0][1]
    return score.value is None, score.value, score.comparable_factors


def _round_robin(groups: Sequence[Group]) -> list[Group]:
    result: list[Group] = []
    for _, tier in groupby(groups, key=_tier):
        result.extend(_take_turns(list(tier)))
    return result


def _take_turns(tier: Sequence[Group]) -> list[Group]:
    buckets: dict[str | None, list[Group]] = {}
    for group in tier:
        direction = group[0][0].opportunity.direction
        buckets.setdefault(None if direction is None else direction.value, []).append(group)
    queues = list(buckets.values())
    result: list[Group] = []
    depth = 0
    while any(depth < len(queue) for queue in queues):
        result.extend(queue[depth] for queue in queues if depth < len(queue))
        depth += 1
    return result
