"""Use cases of the product. A channel calls these and decides nothing itself.

Contract: docs/modules/telegram-bot.md. Every call opens its own `Session`: the
storage is synchronous and a session kept across a day of idle chat dies with
"connection closed".
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from seawork.app.cards import Card, PreparedCard, card_from
from seawork.domain.profile import (
    BehaviourAction,
    BehaviourEvent,
    CareerStage,
    Fact,
    ProfileFactSource,
    UserDataExport,
    UserProfile,
)
from seawork.domain.recommendation import Delivery, Recommendation
from seawork.recommendation import history_from, recommend
from seawork.scoring import compute_match_score
from seawork.storage.account_repository import AccountRepository
from seawork.storage.profile_repository import ProfileRepository
from seawork.storage.profile_vocabulary import ProfileVocabulary
from seawork.storage.repository import Repository

DAILY_LIMIT = 5

REACTIONS = frozenset({BehaviourAction.SAVED, BehaviourAction.DISMISSED, BehaviourAction.REOPENED})


class StartResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    is_new: bool
    stage_known: bool


class SeaWorkService:
    def __init__(self, sessions: sessionmaker[Session], vocabulary: ProfileVocabulary) -> None:
        self._sessions = sessions
        self._vocabulary = vocabulary

    def start(self, channel: str, external_id: str) -> StartResult:
        with self._sessions() as session:
            accounts = AccountRepository(session)
            user_id, is_new = accounts.find_or_create(channel, external_id)
            accounts.unblock(channel, external_id)
            profile = _profile(ProfileRepository(session, self._vocabulary), user_id)
            return StartResult(
                user_id=user_id, is_new=is_new, stage_known=profile.career_stage is not None
            )

    def answer_stage(self, user_id: uuid.UUID, stage: CareerStage) -> None:
        self._state(user_id, "career_stage", stage)

    def set_directions(self, user_id: uuid.UUID, keys: Sequence[str]) -> None:
        self._state(user_id, "directions", list(keys))

    def set_hiring_countries(self, user_id: uuid.UUID, codes: Sequence[str]) -> None:
        self._state(user_id, "hiring_country", list(codes))

    def mark_hiring_country_asked(self, user_id: uuid.UUID, now: datetime) -> None:
        with self._sessions() as session:
            AccountRepository(session).mark_hiring_country_asked(user_id, now)

    def should_ask_hiring_country(self, user_id: uuid.UUID, now: datetime) -> bool:
        """Unknown, never asked, and the first delivery was before today (UTC)."""
        with self._sessions() as session:
            profiles = ProfileRepository(session, self._vocabulary)
            if _profile(profiles, user_id).hiring_country is not None:
                return False
            if AccountRepository(session).hiring_country_asked(user_id):
                return False
            deliveries = profiles.list_deliveries(user_id)
        return bool(deliveries) and deliveries[0].delivered_at < _day_start(now)

    def hiring_country_options(self, limit: int) -> list[str]:
        """Countries active vacancies hire from, most frequent first."""
        with self._sessions() as session:
            candidates = Repository(session).list_feed_candidates()
        counts = Counter(
            code
            for candidate in candidates
            if (scope := candidate.opportunity.hiring_scope) is not None
            for code in set(scope.value.countries)
        )
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [code for code, _ in ranked[:limit]]

    def prepare_batch(self, user_id: uuid.UUID, now: datetime) -> list[PreparedCard]:
        """Choose, never write: what was shown is recorded by `confirm_delivered`."""
        with self._sessions() as session:
            profiles = ProfileRepository(session, self._vocabulary)
            day_start = _day_start(now)
            delivered_today = profiles.count_deliveries(
                user_id, day_start, day_start + timedelta(days=1)
            )
            remaining = DAILY_LIMIT - delivered_today
            if remaining <= 0:
                return []
            profile = _profile(profiles, user_id)
            candidates = Repository(session).list_feed_candidates()
            history = history_from(profiles.list_events(user_id), profiles.delivered_ids(user_id))
        by_id = {candidate.opportunity_id: candidate for candidate in candidates}
        recommendations = recommend(profile, candidates, history, limit=remaining, now=now)
        return [
            PreparedCard(
                card=card_from(
                    by_id[recommendation.opportunity_id],
                    recommendation.score,
                    len(recommendation.similar_ids),
                ),
                recommendation=recommendation,
            )
            for recommendation in recommendations
        ]

    def confirm_delivered(
        self,
        user_id: uuid.UUID,
        recommendations: Sequence[Recommendation],
        channel: str,
        now: datetime,
    ) -> None:
        with self._sessions() as session:
            ProfileRepository(session, self._vocabulary).record_deliveries(
                user_id, recommendations, channel, now
            )

    def react(
        self, user_id: uuid.UUID, opportunity_id: int, action: BehaviourAction, now: datetime
    ) -> None:
        if action not in REACTIONS:
            raise ValueError(f"Not a reaction to a card: {action.value}")
        with self._sessions() as session:
            ProfileRepository(session, self._vocabulary).record_event(
                BehaviourEvent(
                    user_id=user_id,
                    opportunity_id=opportunity_id,
                    action=action,
                    occurred_at=now,
                )
            )

    def similar(self, user_id: uuid.UUID, opportunity_id: int) -> list[Card]:
        with self._sessions() as session:
            profiles = ProfileRepository(session, self._vocabulary)
            delivery = _last_delivery(profiles, user_id, opportunity_id)
            profile = _profile(profiles, user_id)
            candidates = Repository(session).list_feed_candidates()
        members = set(delivery.similar_ids)
        return [
            card_from(
                candidate, compute_match_score(profile, candidate.opportunity), similar_count=0
            )
            for candidate in candidates
            if candidate.opportunity_id in members
        ]

    def pause(self, user_id: uuid.UUID, now: datetime) -> None:
        with self._sessions() as session:
            AccountRepository(session).pause(user_id, now)

    def resume(self, user_id: uuid.UUID) -> None:
        with self._sessions() as session:
            AccountRepository(session).resume(user_id)

    def mark_blocked(self, channel: str, external_id: str, now: datetime) -> None:
        with self._sessions() as session:
            AccountRepository(session).mark_blocked(channel, external_id, now)

    def digest_recipients(self, channel: str) -> list[tuple[str, uuid.UUID]]:
        with self._sessions() as session:
            return AccountRepository(session).digest_recipients(channel)

    def delete(self, user_id: uuid.UUID) -> bool:
        with self._sessions() as session:
            return ProfileRepository(session, self._vocabulary).delete_user(user_id)

    def export(self, user_id: uuid.UUID) -> UserDataExport | None:
        with self._sessions() as session:
            return ProfileRepository(session, self._vocabulary).export_user(user_id)

    def _state(self, user_id: uuid.UUID, field: str, value: object) -> None:
        with self._sessions() as session:
            ProfileRepository(session, self._vocabulary).set_fact(
                user_id,
                field,
                Fact(value=value, source=ProfileFactSource.STATED, confidence=1.0),
            )


def _profile(profiles: ProfileRepository, user_id: uuid.UUID) -> UserProfile:
    profile = profiles.get_profile(user_id)
    if profile is None:
        raise LookupError(f"No profile for user {user_id}")
    return profile


def _last_delivery(
    profiles: ProfileRepository, user_id: uuid.UUID, opportunity_id: int
) -> Delivery:
    for delivery in reversed(profiles.list_deliveries(user_id)):
        if delivery.opportunity_id == opportunity_id:
            return delivery
    raise LookupError(f"Opportunity {opportunity_id} was never delivered to user {user_id}")


def _day_start(now: datetime) -> datetime:
    moment = now.astimezone(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
