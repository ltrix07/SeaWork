"""Who stands behind a channel's account, and what the channel knows about them."""

import uuid
from datetime import datetime

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seawork.storage.models import ChannelAccountRecord, UserProfileRecord


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_or_create(self, channel: str, external_id: str) -> tuple[uuid.UUID, bool]:
        """The profile and the account appear together or not at all.

        Two first messages from one person race on the unique index, not on a
        get-then-insert: the loser's insert does nothing, its transaction (and the
        profile it had just inserted) is rolled back, and it reads the winner's row.
        """
        existing = self.find(channel, external_id)
        if existing is not None:
            return existing, False
        user_id = uuid.uuid4()
        self._session.execute(insert(UserProfileRecord).values(user_id=user_id, facts={}))
        created = self._session.execute(
            insert(ChannelAccountRecord)
            .values(channel=channel, external_id=external_id, user_id=user_id)
            .on_conflict_do_nothing(index_elements=["channel", "external_id"])
            .returning(ChannelAccountRecord.id)
        ).scalar_one_or_none()
        if created is None:
            self._session.rollback()
            return self._session.execute(_by_key(channel, external_id)).scalar_one(), False
        self._session.commit()
        return user_id, True

    def unblock(self, channel: str, external_id: str) -> None:
        self._session.execute(
            update(ChannelAccountRecord)
            .where(
                ChannelAccountRecord.channel == channel,
                ChannelAccountRecord.external_id == external_id,
                ChannelAccountRecord.blocked_at.is_not(None),
            )
            .values(blocked_at=None)
        )
        self._session.commit()

    def mark_blocked(self, channel: str, external_id: str, now: datetime) -> None:
        self._session.execute(
            update(ChannelAccountRecord)
            .where(
                ChannelAccountRecord.channel == channel,
                ChannelAccountRecord.external_id == external_id,
                ChannelAccountRecord.blocked_at.is_(None),
            )
            .values(blocked_at=now)
        )
        self._session.commit()

    def pause(self, user_id: uuid.UUID, now: datetime) -> None:
        self._session.execute(
            update(ChannelAccountRecord)
            .where(
                ChannelAccountRecord.user_id == user_id, ChannelAccountRecord.paused_at.is_(None)
            )
            .values(paused_at=now)
        )
        self._session.commit()

    def resume(self, user_id: uuid.UUID) -> None:
        self._session.execute(
            update(ChannelAccountRecord)
            .where(ChannelAccountRecord.user_id == user_id)
            .values(paused_at=None)
        )
        self._session.commit()

    def mark_hiring_country_asked(self, user_id: uuid.UUID, now: datetime) -> None:
        self._session.execute(
            update(ChannelAccountRecord)
            .where(
                ChannelAccountRecord.user_id == user_id,
                ChannelAccountRecord.hiring_country_asked_at.is_(None),
            )
            .values(hiring_country_asked_at=now)
        )
        self._session.commit()

    def hiring_country_asked(self, user_id: uuid.UUID) -> bool:
        return (
            self._session.execute(
                select(ChannelAccountRecord.id).where(
                    ChannelAccountRecord.user_id == user_id,
                    ChannelAccountRecord.hiring_country_asked_at.is_not(None),
                )
            ).first()
            is not None
        )

    def digest_recipients(self, channel: str) -> list[tuple[str, uuid.UUID]]:
        statement = (
            select(ChannelAccountRecord.external_id, ChannelAccountRecord.user_id)
            .where(
                ChannelAccountRecord.channel == channel,
                ChannelAccountRecord.paused_at.is_(None),
                ChannelAccountRecord.blocked_at.is_(None),
            )
            .order_by(ChannelAccountRecord.id)
        )
        return [(external_id, user_id) for external_id, user_id in self._session.execute(statement)]

    def find(self, channel: str, external_id: str) -> uuid.UUID | None:
        return self._session.execute(_by_key(channel, external_id)).scalar_one_or_none()


def _by_key(channel: str, external_id: str) -> Select[uuid.UUID]:
    return select(ChannelAccountRecord.user_id).where(
        ChannelAccountRecord.channel == channel,
        ChannelAccountRecord.external_id == external_id,
    )
