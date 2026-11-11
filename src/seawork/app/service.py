"""Use cases of the product. A channel calls these and decides nothing itself.

Contract: docs/modules/telegram-bot.md. Every call opens its own `Session`: the
storage is synchronous and a session kept across a day of idle chat dies with
"connection closed".
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from seawork.app.cards import Card, PreparedCard, card_from
from seawork.app.names import Names
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
HIRING_COUNTRY_OPTIONS = 8

REACTIONS = frozenset({BehaviourAction.SAVED, BehaviourAction.DISMISSED, BehaviourAction.REOPENED})


class StartResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    is_new: bool
    stage_known: bool


class StageAnswered(BaseModel):
    model_config = ConfigDict(frozen=True)

    show_batch: bool
    ask_directions: bool


class ReactionState(StrEnum):
    NONE = "none"
    SAVED = "saved"
    DISMISSED = "dismissed"


class FeedPage(BaseModel):
    """One screen of today's carousel. `card` is None past the last card.

    `fresh` is set when the card has not been delivered yet: whoever shows it must
    call `confirm_delivered` with it once it is on the screen. `ask_hiring_country`
    replaces the whole page: the channel asks that question first and opens the
    carousel afterwards.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    total: int
    card: Card | None
    fresh: Recommendation | None
    reaction: ReactionState
    has_prev: bool
    has_next: bool
    ask_hiring_country: bool = False


class SeaWorkService:
    def __init__(
        self, sessions: sessionmaker[Session], vocabulary: ProfileVocabulary, names: Names
    ) -> None:
        self._sessions = sessions
        self._vocabulary = vocabulary
        self._names = names

    def names(self) -> Names:
        return self._names

    def start(self, channel: str, external_id: str) -> StartResult:
        with self._sessions() as session:
            accounts = AccountRepository(session)
            user_id, is_new = accounts.find_or_create(channel, external_id)
            accounts.unblock(channel, external_id)
            profile = _profile(ProfileRepository(session, self._vocabulary), user_id)
            return StartResult(
                user_id=user_id, is_new=is_new, stage_known=profile.career_stage is not None
            )

    def touch(self, channel: str, external_id: str) -> uuid.UUID | None:
        """Any message but the first: finds the person, never creates one."""
        with self._sessions() as session:
            accounts = AccountRepository(session)
            user_id = accounts.find(channel, external_id)
            if user_id is not None:
                accounts.unblock(channel, external_id)
            return user_id

    def answer_stage(
        self, user_id: uuid.UUID, stage: CareerStage, *, onboarding: bool
    ) -> StageAnswered:
        self._state(user_id, "career_stage", stage)
        if not onboarding:
            return StageAnswered(show_batch=False, ask_directions=False)
        with self._sessions() as session:
            profile = _profile(ProfileRepository(session, self._vocabulary), user_id)
        return StageAnswered(
            show_batch=True,
            ask_directions=stage is CareerStage.ENTRY and profile.directions is None,
        )

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

    def hiring_country_options(self) -> list[str]:
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
        return [code for code, _ in ranked[:HIRING_COUNTRY_OPTIONS]]

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
        *,
        rejected: bool = False,
    ) -> None:
        """`rejected`: the channel refused the cards themselves; they are not offered again."""
        with self._sessions() as session:
            ProfileRepository(session, self._vocabulary).record_deliveries(
                user_id, recommendations, channel, now, rejected=rejected
            )

    def feed_page(
        self, user_id: uuid.UUID, index: int, now: datetime, *, entering: bool
    ) -> FeedPage:
        """Today's carousel, rebuilt from the deliveries on every call: no state in the channel.

        Positions below the number of today's deliveries are those cards again, scored
        by the profile as it is now; the next position is the best undelivered one while
        the daily limit lasts; anything past that is the end. `entering` is the person
        opening the carousel (not paging, not reacting): only then may it ask for the
        country of hiring instead.
        """
        if entering and self.should_ask_hiring_country(user_id, now):
            return FeedPage(
                index=0,
                total=0,
                card=None,
                fresh=None,
                reaction=ReactionState.NONE,
                has_prev=False,
                has_next=False,
                ask_hiring_country=True,
            )
        with self._sessions() as session:
            profiles = ProfileRepository(session, self._vocabulary)
            profile = _profile(profiles, user_id)
            candidates = Repository(session).list_feed_candidates()
            deliveries = profiles.list_deliveries(user_id)
            events = profiles.list_events(user_id)
        by_id = {candidate.opportunity_id: candidate for candidate in candidates}
        day_start = _day_start(now)
        today = [
            delivery
            for delivery in deliveries
            if not delivery.rejected
            and day_start <= delivery.delivered_at < day_start + timedelta(days=1)
        ]
        shown = [delivery for delivery in today if delivery.opportunity_id in by_id]
        remaining = DAILY_LIMIT - len(today)
        fresh = (
            recommend(
                profile,
                candidates,
                history_from(events, _delivered_ids(deliveries)),
                limit=remaining,
                now=now,
            )
            if remaining > 0
            else []
        )
        total = len(shown) + len(fresh)
        position = min(index, len(shown))
        card: Card | None = None
        new: Recommendation | None = None
        reaction = ReactionState.NONE
        if position < len(shown):
            delivery = shown[position]
            candidate = by_id[delivery.opportunity_id]
            card = card_from(
                candidate,
                compute_match_score(profile, candidate.opportunity, now=now),
                len(delivery.similar_ids),
            )
            reaction = _reaction(events, delivery.opportunity_id)
        elif fresh:
            new = fresh[0]
            card = card_from(by_id[new.opportunity_id], new.score, len(new.similar_ids))
        return FeedPage(
            index=position,
            total=total,
            card=card,
            fresh=new,
            reaction=reaction,
            has_prev=position > 0,
            has_next=position + 1 < total,
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

    def notifications_enabled(self, user_id: uuid.UUID) -> bool:
        with self._sessions() as session:
            return not AccountRepository(session).paused(user_id)

    def toggle_notifications(self, user_id: uuid.UUID, now: datetime) -> bool:
        """Returns whether notifications are on after the switch."""
        with self._sessions() as session:
            accounts = AccountRepository(session)
            if accounts.paused(user_id):
                accounts.resume(user_id)
                return True
            accounts.pause(user_id, now)
            return False

    def mark_blocked(self, channel: str, external_id: str, now: datetime) -> None:
        with self._sessions() as session:
            AccountRepository(session).mark_blocked(channel, external_id, now)

    def digest_recipients(self, channel: str) -> list[tuple[str, uuid.UUID]]:
        """Not paused, not blocked, and past the first question: no stage, no notice."""
        with self._sessions() as session:
            profiles = ProfileRepository(session, self._vocabulary)
            return [
                (external_id, user_id)
                for external_id, user_id in AccountRepository(session).digest_recipients(channel)
                if _profile(profiles, user_id).career_stage is not None
            ]

    def digest_notified_today(self, user_id: uuid.UUID, now: datetime) -> bool:
        with self._sessions() as session:
            return AccountRepository(session).digest_notified_since(user_id, _day_start(now))

    def digest_count(self, user_id: uuid.UUID, now: datetime) -> int:
        """Cards the morning notice promises: what is left of today's limit, or what is new."""
        return len(self.prepare_batch(user_id, now))

    def mark_digest_notified(self, user_id: uuid.UUID, now: datetime) -> None:
        with self._sessions() as session:
            AccountRepository(session).mark_digest_notified(user_id, now)

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


def _delivered_ids(deliveries: Sequence[Delivery]) -> frozenset[int]:
    return frozenset(
        identifier
        for delivery in deliveries
        for identifier in (delivery.opportunity_id, *delivery.similar_ids)
    )


def _reaction(events: Sequence[BehaviourEvent], opportunity_id: int) -> ReactionState:
    state = ReactionState.NONE
    for event in events:
        if event.opportunity_id != opportunity_id:
            continue
        if event.action is BehaviourAction.SAVED:
            state = ReactionState.SAVED
        elif event.action is BehaviourAction.DISMISSED:
            state = ReactionState.DISMISSED
        elif event.action is BehaviourAction.REOPENED:
            state = ReactionState.NONE
    return state


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
