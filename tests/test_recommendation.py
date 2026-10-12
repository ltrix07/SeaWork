"""Recommendation Engine: selection from candidates, and the delivery history behind it.

The engine tests need no database; the last section follows test_profile.py and is
skipped when no PostgreSQL is reachable.
"""

import datetime
import hashlib
import uuid
from pathlib import Path

import pytest
from pydantic import HttpUrl
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.domain.profile import (
    BehaviourAction,
    BehaviourEvent,
    Fact,
    ProfileFactSource,
    UserProfile,
)
from seawork.domain.recommendation import Candidate, FeedHistory, Recommendation
from seawork.ingestion.base import RawItem
from seawork.recommendation import history_from, recommend
from seawork.scoring import compute_match_score
from seawork.storage.models import DeliveryRecord, UserProfileRecord
from seawork.storage.profile_repository import ProfileRepository
from seawork.storage.profile_vocabulary import ProfileVocabulary
from seawork.storage.repository import Repository

NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)
NO_HISTORY = FeedHistory(delivered_ids=frozenset(), dismissed_ids=frozenset())


def stated[T](value: T) -> Fact[T]:
    return Fact(value=value, source=ProfileFactSource.STATED, confidence=1.0, stated_at=NOW)


def person(
    professions: list[str] | None = None, directions: list[str] | None = None
) -> UserProfile:
    return UserProfile(
        user_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
        professions=None if professions is None else stated(professions),
        directions=None if directions is None else stated(directions),
    )


def inferred(value: str) -> Inferred[str]:
    return Inferred(value=value, provenance=Provenance.RULE, confidence=0.8)


def opportunity(
    identifier: int, profession: str | None, direction: str | None
) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=str(identifier),
            url=HttpUrl(f"https://example.com/job/{identifier}"),
            title="Vacancy",
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        profession=None if profession is None else inferred(profession),
        direction=None if direction is None else inferred(direction),
        quality_score=1.0,
        quality_flags=[],
    )


def candidate(
    identifier: int,
    profession: str | None = "chef",
    direction: str | None = None,
    employer: str | None = None,
    day: int = 1,
) -> Candidate:
    return Candidate(
        opportunity_id=identifier,
        opportunity=opportunity(identifier, profession, direction),
        employer=employer,
        freshness=datetime.datetime(2026, 10, day, tzinfo=datetime.UTC),
    )


def ids(recommendations: list[Recommendation]) -> list[int]:
    return [r.opportunity_id for r in recommendations]


def pick(
    profile: UserProfile,
    candidates: list[Candidate],
    history: FeedHistory = NO_HISTORY,
    limit: int = 5,
) -> list[Recommendation]:
    return recommend(profile, candidates, history, limit=limit, now=NOW)


def event(
    opportunity_id: int, action: BehaviourAction, minute: int, user_id: uuid.UUID | None = None
) -> BehaviourEvent:
    return BehaviourEvent(
        user_id=user_id or uuid.uuid4(),
        opportunity_id=opportunity_id,
        action=action,
        occurred_at=NOW + datetime.timedelta(minutes=minute),
    )


# --- history --------------------------------------------------------------------


def test_dismissed_and_delivered_are_not_returned() -> None:
    history = FeedHistory(delivered_ids=frozenset({1}), dismissed_ids=frozenset({2}))
    result = pick(person(["chef"]), [candidate(1), candidate(2), candidate(3)], history)
    assert ids(result) == [3]


def test_reopened_after_dismissed_brings_the_vacancy_back() -> None:
    events = [event(7, BehaviourAction.DISMISSED, 0), event(7, BehaviourAction.REOPENED, 1)]
    assert history_from(events, []).dismissed_ids == frozenset()


def test_dismissed_after_reopened_stays_dismissed() -> None:
    events = [
        event(7, BehaviourAction.DISMISSED, 0),
        event(7, BehaviourAction.REOPENED, 1),
        event(7, BehaviourAction.DISMISSED, 2),
    ]
    assert history_from(events, []).dismissed_ids == frozenset({7})


def test_history_does_not_depend_on_the_order_events_are_passed_in() -> None:
    events = [event(7, BehaviourAction.REOPENED, 1), event(7, BehaviourAction.DISMISSED, 0)]
    assert history_from(events, []).dismissed_ids == frozenset()


def test_equal_times_resolve_by_the_order_given() -> None:
    dismissed_last = [event(7, BehaviourAction.REOPENED, 0), event(7, BehaviourAction.DISMISSED, 0)]
    reopened_last = [event(7, BehaviourAction.DISMISSED, 0), event(7, BehaviourAction.REOPENED, 0)]
    assert history_from(dismissed_last, []).dismissed_ids == frozenset({7})
    assert history_from(reopened_last, []).dismissed_ids == frozenset()


def test_viewing_is_not_dismissing() -> None:
    events = [event(7, BehaviourAction.VIEWED, 0), event(8, BehaviourAction.SAVED, 0)]
    assert history_from(events, []).dismissed_ids == frozenset()


def test_delivered_ids_are_passed_through() -> None:
    assert history_from([], [3, 4]).delivered_ids == frozenset({3, 4})


# --- filtering ------------------------------------------------------------------


def test_a_full_miss_is_dropped_and_an_unknown_is_kept() -> None:
    miss = candidate(1, profession="deckhand")
    unknown = candidate(2, profession=None)
    hit = candidate(3, profession="chef")
    assert compute_match_score(person(["chef"]), miss.opportunity, now=NOW).value == 0.0
    assert compute_match_score(person(["chef"]), unknown.opportunity, now=NOW).value is None
    assert ids(pick(person(["chef"]), [miss, unknown, hit])) == [3, 2]


# --- ordering: one test per level of the key ------------------------------------


def test_a_known_score_goes_before_an_unknown_one() -> None:
    result = pick(person(["chef"]), [candidate(1, profession=None, day=9), candidate(2)])
    assert ids(result) == [2, 1]


def test_higher_value_goes_first() -> None:
    profile = person(["chef"], ["culinary"])
    half = candidate(1, direction="deck", day=9)
    full = candidate(2, direction="culinary", day=1)
    assert ids(pick(profile, [half, full])) == [2, 1]


def test_more_comparable_factors_win_at_equal_value() -> None:
    profile = person(["chef"], ["culinary"])
    thin = candidate(1, direction=None, day=9)
    solid = candidate(2, direction="culinary", day=1)
    scores = {r.opportunity_id: r.score for r in pick(profile, [thin, solid])}
    assert scores[1].value == scores[2].value == 1.0
    assert scores[1].comparable_factors < scores[2].comparable_factors
    assert ids(pick(profile, [thin, solid])) == [2, 1]


def test_fresher_goes_first_at_equal_score() -> None:
    result = pick(person(["chef"]), [candidate(1, day=1), candidate(2, day=5)])
    assert ids(result) == [2, 1]


def test_id_breaks_the_last_tie() -> None:
    result = pick(person(["chef"]), [candidate(9), candidate(4)])
    assert ids(result) == [4, 9]


def test_rank_is_one_based_and_follows_the_order() -> None:
    result = pick(person(["chef"]), [candidate(1, day=1), candidate(2, day=5)])
    assert [r.rank for r in result] == [1, 2]


# --- grouping -------------------------------------------------------------------


def test_same_employer_and_profession_make_one_card() -> None:
    cards = [
        candidate(1, employer="Princess Cruises", day=1),
        candidate(2, employer="princess  cruises", day=5),
        candidate(3, employer="Princess Cruises", day=3),
    ]
    result = pick(person(["chef"]), cards)
    assert ids(result) == [2]
    assert result[0].similar_ids == (3, 1)


def test_a_card_without_employer_or_profession_is_never_grouped() -> None:
    cards = [
        candidate(1, employer=None),
        candidate(2, employer=None),
        candidate(3, profession=None, employer="Acme"),
        candidate(4, profession=None, employer="Acme"),
    ]
    result = pick(person(["chef"]), cards)
    assert sorted(ids(result)) == [1, 2, 3, 4]
    assert all(r.similar_ids == () for r in result)


def test_a_group_takes_one_place_in_the_limit() -> None:
    cards = [candidate(i, employer="Acme", day=i) for i in range(1, 5)]
    cards.append(candidate(9, profession="chef", employer=None, day=1))
    result = pick(person(["chef"]), cards, limit=2)
    assert ids(result) == [4, 9]
    assert result[0].similar_ids == (3, 2, 1)


def test_a_dismissed_best_member_hands_the_card_to_the_next() -> None:
    cards = [candidate(i, employer="Acme", day=i) for i in (1, 2, 3)]
    history = FeedHistory(delivered_ids=frozenset(), dismissed_ids=frozenset({3}))
    result = pick(person(["chef"]), cards, history)
    assert ids(result) == [2]
    assert result[0].similar_ids == (1,)


def test_a_full_miss_member_does_not_join_the_group() -> None:
    profile = person(directions=["culinary"])
    cards = [
        candidate(1, direction="culinary", employer="Acme", day=1),
        candidate(2, direction="culinary", employer="Acme", day=5),
        candidate(3, direction="deck", employer="Acme", day=9),
    ]
    assert compute_match_score(profile, cards[2].opportunity, now=NOW).value == 0.0
    result = pick(profile, cards)
    assert ids(result) == [2]
    assert result[0].similar_ids == (1,)


def test_different_professions_of_one_employer_are_different_cards() -> None:
    cards = [
        candidate(1, profession="chef", employer="Acme"),
        candidate(2, profession="baker", employer="Acme"),
    ]
    assert sorted(ids(pick(person(["chef", "baker"]), cards))) == [1, 2]


# --- variety --------------------------------------------------------------------


def test_directions_take_turns_before_one_repeats() -> None:
    cards = [
        *(candidate(i, direction="cruise", day=10 - i) for i in range(1, 5)),
        candidate(5, direction="diving", day=1),
        candidate(6, direction="yachts", day=1),
    ]
    result = pick(person(["chef"]), cards, limit=5)
    by_id = {c.opportunity_id: c.opportunity.direction for c in cards}
    first_three = [by_id[i] for i in ids(result)[:3]]
    assert {d.value for d in first_three if d is not None} == {"cruise", "diving", "yachts"}
    assert len(result) == 5


def test_a_card_without_direction_is_its_own_basket() -> None:
    cards = [
        candidate(1, direction="cruise", day=9),
        candidate(2, direction="cruise", day=8),
        candidate(3, direction=None, day=1),
    ]
    assert ids(pick(person(["chef"]), cards, limit=2)) == [1, 3]


# --- limit and cold start -------------------------------------------------------


def test_fewer_candidates_than_the_limit_give_a_short_list() -> None:
    assert len(pick(person(["chef"]), [candidate(1), candidate(2)], limit=5)) == 2


def test_no_candidates_give_an_empty_list() -> None:
    assert pick(person(["chef"]), []) == []


def test_empty_profile_is_ordered_by_freshness_and_varied() -> None:
    cards = [
        candidate(1, direction="cruise", day=9),
        candidate(2, direction="cruise", day=8),
        candidate(3, direction="diving", day=1),
    ]
    result = pick(person(), cards, limit=3)
    assert all(r.score.value is None for r in result)
    assert ids(result) == [1, 3, 2]


# --- storage --------------------------------------------------------------------


@pytest.fixture
def test_schema() -> str:
    return "seawork_recommendation_test"


VOCABULARY = ProfileVocabulary.from_directory(Path("data/reference"))


def store(session: Session, external_id: str, employer: str | None, profession: str) -> int:
    payload = external_id
    raw = Repository(session).save_raw(
        RawItem(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            fetched_at=NOW,
            payload=payload,
            content_type="application/xml",
            content_hash=hashlib.sha256(payload.encode()).hexdigest(),
            http_status=200,
        )
    )
    stored = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title="Vacancy",
            employer=employer,
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        profession=inferred(profession),
        quality_score=1.0,
        quality_flags=[],
    )
    Repository(session).upsert_opportunity(
        raw.id, stored, SourceTrust.PRIMARY, OpportunityStatus.ACTIVE, keep_closed=False
    )
    return next(
        c.opportunity_id
        for c in Repository(session).list_feed_candidates()
        if c.opportunity.normalized.external_id == external_id
    )


def feed(
    session: Session, user_id: uuid.UUID, repository: ProfileRepository
) -> list[Recommendation]:
    profile = repository.get_profile(user_id)
    assert profile is not None
    history = history_from(repository.list_events(user_id), repository.delivered_ids(user_id))
    return recommend(profile, Repository(session).list_feed_candidates(), history, limit=5, now=NOW)


def test_a_delivered_feed_is_not_returned_again(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated(["galley"]))
    grouped = [store(session, str(i), "Acme", "galley") for i in (1, 2)]
    single = store(session, "3", "Other", "galley")

    first = feed(session, user_id, repository)
    assert len(first) == 2
    assert {i for r in first for i in (r.opportunity_id, *r.similar_ids)} == {*grouped, single}
    repository.record_deliveries(user_id, first, "telegram", NOW)

    assert feed(session, user_id, repository) == []
    assert repository.delivered_ids(user_id) == frozenset({*grouped, single})


def test_a_dismissed_vacancy_stays_out_until_reopened(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated(["galley"]))
    vacancy = store(session, "1", None, "galley")
    repository.record_event(event(vacancy, BehaviourAction.DISMISSED, 0, user_id))
    assert feed(session, user_id, repository) == []
    repository.record_event(event(vacancy, BehaviourAction.REOPENED, 1, user_id))
    assert ids(feed(session, user_id, repository)) == [vacancy]


def test_deliveries_are_exported_and_deleted_with_the_person(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated(["galley"]))
    store(session, "1", None, "galley")
    shown = feed(session, user_id, repository)[0]
    repository.record_deliveries(user_id, [shown], "telegram", NOW)

    exported = repository.export_user(user_id)
    assert exported is not None
    assert [d.opportunity_id for d in exported.deliveries] == [shown.opportunity_id]
    assert exported.deliveries[0].score_value == shown.score.value
    assert exported.deliveries[0].channel == "telegram"

    assert repository.delete_user(user_id)
    assert repository.delivered_ids(user_id) == frozenset()


def test_deleting_the_profile_in_sql_cascades_to_deliveries(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated(["galley"]))
    store(session, "1", None, "galley")
    repository.record_deliveries(user_id, feed(session, user_id, repository), "telegram", NOW)
    assert session.scalar(select(func.count()).select_from(DeliveryRecord)) == 1

    session.execute(delete(UserProfileRecord).where(UserProfileRecord.user_id == user_id))
    session.commit()

    assert session.scalar(select(func.count()).select_from(DeliveryRecord)) == 0


def test_a_failed_batch_records_nothing(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated(["galley"]))
    store(session, "1", None, "galley")
    store(session, "2", "Other", "galley")
    batch = feed(session, user_id, repository)
    assert len(batch) == 2

    with pytest.raises(IntegrityError):
        repository.record_deliveries(uuid.uuid4(), batch, "telegram", NOW)

    assert session.scalar(select(func.count()).select_from(DeliveryRecord)) == 0
