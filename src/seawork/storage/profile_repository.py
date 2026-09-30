"""Storage for people, kept apart from `Repository` on purpose.

`Repository` holds public postings that the pipeline rewrites freely. This holds
personal data with its own rules: deletion and export must be complete (contract
3.6), and the journal is never edited. Keeping them in one class named for the
other would make "what touches personal data" a question answered by reading it all.
"""

import uuid
from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seawork.domain.profile import (
    FACT_FIELDS,
    BehaviourAction,
    BehaviourEvent,
    Fact,
    InterestCounts,
    ProfileFactSource,
    UserDataExport,
    UserProfile,
)
from seawork.storage.models import BehaviourEventRecord, OpportunityRecord, UserProfileRecord
from seawork.storage.profile_vocabulary import ProfileVocabulary

# This layer is PostgreSQL-only, like `repository.py` beside it: both express their
# idempotency with INSERT ... ON CONFLICT, and `with_for_update()` is silently a
# no-op on SQLite, which would drop the single-document write protection without a
# signal. The sqlite variants in models.py cover the column types, not the dialect.


class StatedFactProtectedError(Exception):
    """A non-stated fact tried to replace something the person said themselves."""


class ProfileRepository:
    def __init__(self, session: Session, vocabulary: ProfileVocabulary) -> None:
        self._session = session
        # Required, not defaulted: a caller that forgot it would silently store keys
        # that no vacancy can ever match.
        self._vocabulary = vocabulary

    def get_or_create_profile(self, user_id: uuid.UUID | None = None) -> UserProfile:
        """Create an empty profile on first contact; return the existing one after that."""
        identifier = user_id or uuid.uuid4()
        # ON CONFLICT rather than get-then-insert: two first messages from the same
        # person arriving together must not turn one of them into an error.
        self._session.execute(
            insert(UserProfileRecord)
            .values(user_id=identifier, facts={})
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        self._session.commit()
        return self._to_profile(self._session.get_one(UserProfileRecord, identifier))

    def get_profile(self, user_id: uuid.UUID) -> UserProfile | None:
        record = self._session.get(UserProfileRecord, user_id)
        return None if record is None else self._to_profile(record)

    def set_fact(self, user_id: uuid.UUID, field: str, fact: Fact[Any]) -> UserProfile:
        """Store one fact, leaving every other fact untouched.

        Raises `StatedFactProtectedError` when the fact is not itself stated and the
        person has already stated this one: an observation may not silently overwrite
        an answer (contract 3.3). What to do about the disagreement is left open by
        contract 6.2, so refusing is the only choice that loses nothing.

        Raises `UnknownReferenceKeyError` for a key missing from the shared reference
        files. It is an error and not a silent drop: this is a person's answer, and
        losing it unnoticed is worse than refusing it loudly.
        """
        self._require_fact_field(field)
        # Country codes are upper-cased before the check: see ProfileVocabulary.
        fact = fact.model_copy(update={"value": self._vocabulary.canonicalize(field, fact.value)})
        self._vocabulary.check(field, fact.value)
        with self._locked_for_write(user_id) as record:
            existing = record.facts.get(field)
            if (
                existing is not None
                and existing["source"] == ProfileFactSource.STATED.value
                and fact.source is not ProfileFactSource.STATED
            ):
                raise StatedFactProtectedError(field)
            facts = dict(record.facts)
        facts[field] = fact.model_dump(mode="json")
        return self._save(record, facts)

    def clear_fact(self, user_id: uuid.UUID, field: str, source: ProfileFactSource) -> UserProfile:
        """Return a fact to "unknown". Not the same as setting it to an empty list.

        `source` says who is clearing, and only a STATED caller may remove a STATED
        fact: a person may withdraw their own answer, background analytics may not
        (contract 3.3, the mirror of `set_fact`).
        """
        self._require_fact_field(field)
        with self._locked_for_write(user_id) as record:
            existing = record.facts.get(field)
            if (
                existing is not None
                and existing["source"] == ProfileFactSource.STATED.value
                and source is not ProfileFactSource.STATED
            ):
                raise StatedFactProtectedError(field)
            if field not in record.facts:
                # Nothing to clear. Saving anyway would move `updated_at`, and that
                # column is the only record of when we last learned something about
                # this person - a no-op must not look like news.
                return self._to_profile(record)
            facts = {name: value for name, value in record.facts.items() if name != field}
            return self._save(record, facts)

    def record_event(self, event: BehaviourEvent) -> None:
        """Append to the journal. There is deliberately no way to change or remove one."""
        self._session.add(
            BehaviourEventRecord(
                user_id=event.user_id,
                opportunity_id=event.opportunity_id,
                action=event.action.value,
                dwell_seconds=event.dwell_seconds,
                occurred_at=event.occurred_at,
            )
        )
        self._session.commit()

    def list_events(self, user_id: uuid.UUID) -> list[BehaviourEvent]:
        statement = (
            select(BehaviourEventRecord)
            .where(BehaviourEventRecord.user_id == user_id)
            .order_by(BehaviourEventRecord.occurred_at, BehaviourEventRecord.id)
        )
        return [
            BehaviourEvent(
                user_id=row.user_id,
                opportunity_id=row.opportunity_id,
                action=BehaviourAction(row.action),
                dwell_seconds=row.dwell_seconds,
                occurred_at=row.occurred_at,
            )
            for row in self._session.scalars(statement)
        ]

    def interest_counts(self, user_id: uuid.UUID) -> InterestCounts:
        """Count actions overall and per direction and profession, nothing more."""
        direction = OpportunityRecord.enriched["direction"]["value"].as_string()
        profession = OpportunityRecord.enriched["profession"]["value"].as_string()
        # Outer join: an event on an opportunity that has since gone must still count
        # as something the person did.
        statement = (
            select(
                BehaviourEventRecord.action,
                direction,
                profession,
                func.count(),
            )
            .select_from(BehaviourEventRecord)
            .outerjoin(
                OpportunityRecord, OpportunityRecord.id == BehaviourEventRecord.opportunity_id
            )
            .where(BehaviourEventRecord.user_id == user_id)
            .group_by(BehaviourEventRecord.action, direction, profession)
        )
        counts = InterestCounts()
        by_direction: dict[str | None, dict[BehaviourAction, int]] = defaultdict(dict)
        by_profession: dict[str | None, dict[BehaviourAction, int]] = defaultdict(dict)
        for action_value, direction_key, profession_key, total in self._session.execute(statement):
            action = BehaviourAction(action_value)
            counts.by_action[action] = counts.by_action.get(action, 0) + total
            by_direction[direction_key][action] = by_direction[direction_key].get(action, 0) + total
            by_profession[profession_key][action] = (
                by_profession[profession_key].get(action, 0) + total
            )
        counts.by_direction = dict(by_direction)
        counts.by_profession = dict(by_profession)
        return counts

    def delete_user(self, user_id: uuid.UUID) -> bool:
        """Remove the profile and the journal. Returns whether anyone was there."""
        # Explicit as well as ON DELETE CASCADE: the guarantee should not depend on a
        # database that was created some other way than by our migration.
        self._session.execute(
            delete(BehaviourEventRecord).where(BehaviourEventRecord.user_id == user_id)
        )
        removed = self._session.execute(
            delete(UserProfileRecord)
            .where(UserProfileRecord.user_id == user_id)
            .returning(UserProfileRecord.user_id)
        ).scalar_one_or_none()
        self._session.commit()
        return removed is not None

    def export_user(self, user_id: uuid.UUID) -> UserDataExport | None:
        profile = self.get_profile(user_id)
        if profile is None:
            return None
        return UserDataExport(profile=profile, events=self.list_events(user_id))

    @staticmethod
    def _require_fact_field(field: str) -> None:
        # Guards the JSON document: without it a typo would be stored happily and
        # then silently ignored when the profile is read back.
        if field not in FACT_FIELDS:
            raise ValueError(f"Unknown profile fact: {field!r}")

    @contextmanager
    def _locked_for_write(self, user_id: uuid.UUID) -> Generator[UserProfileRecord]:
        """Hold the row lock only while a write may still happen.

        Every success path commits, but a refusal used to propagate with the
        `FOR UPDATE` still held inside an open transaction. In a client holding a
        long-lived session - a bot, for instance - one rejected background
        observation then blocked every further write for that user until the
        session happened to close. The lock has to be released by whoever took it.
        """
        try:
            yield self._locked(user_id)
        except Exception:
            self._session.rollback()
            raise

    def _locked(self, user_id: uuid.UUID) -> UserProfileRecord:
        # FOR UPDATE because facts is one document: two writers changing different
        # facts would otherwise each write back a copy missing the other's change.
        record = self._session.execute(
            select(UserProfileRecord).where(UserProfileRecord.user_id == user_id).with_for_update()
        ).scalar_one_or_none()
        if record is None:
            raise LookupError(f"No profile for user {user_id}")
        return record

    def _save(self, record: UserProfileRecord, facts: dict[str, Any]) -> UserProfile:
        # Validated before it is written, so a value of the wrong shape for its
        # field fails here and not on some later read.
        UserProfile.model_validate(
            {
                "user_id": record.user_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            | facts
        )
        record.facts = facts
        record.updated_at = func.now()
        self._session.commit()
        self._session.refresh(record)
        return self._to_profile(record)

    @staticmethod
    def _to_profile(record: UserProfileRecord) -> UserProfile:
        return UserProfile.model_validate(
            {
                "user_id": record.user_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            | record.facts
        )
