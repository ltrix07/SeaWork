"""The morning notice against a real database and a Telegram that only records what it is asked."""

import datetime
import hashlib
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, InlineKeyboardMarkup, Message
from pydantic import HttpUrl, SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from seawork import cli
from seawork.app import DAILY_LIMIT, Names, SeaWorkService
from seawork.bot import texts
from seawork.bot.delivery import DigestResult, send_digest
from seawork.bot.keyboards import Nav, NavCb
from seawork.config import Settings
from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, HiringScope, NormalizedOpportunity
from seawork.domain.profile import CareerStage
from seawork.ingestion.base import RawItem
from seawork.storage.models import ChannelAccountRecord
from seawork.storage.profile_vocabulary import ProfileVocabulary
from seawork.storage.repository import Repository

REFERENCE = Path("data/reference")
NOW = datetime.datetime(2026, 10, 12, 7, 0, tzinfo=datetime.UTC)
CHANNEL = "telegram"
DAY = datetime.timedelta(days=1)


class Telegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []
        self.forbidden: set[int] = set()
        self.broken: set[int] = set()
        self.flood: dict[int, int] = {}

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        assert isinstance(method, SendMessage)
        chat_id = int(method.chat_id)
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        if self.flood.get(chat_id, 0) > 0:
            self.flood[chat_id] -= 1
            raise TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=7)
        if chat_id in self.broken:
            raise TelegramNetworkError(method=method, message="down")
        self.sent.append(method)
        message = Message(message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"), text="x")
        return message  # type: ignore[return-value]

    async def stream_content(  # pragma: no cover
        self,
        url: str,
        headers: Any = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    def to(self, chat_id: int) -> list[SendMessage]:
        return [call for call in self.sent if int(call.chat_id) == chat_id]


@pytest.fixture
def test_schema() -> str:
    return "seawork_delivery_test"


@pytest.fixture
def sessions(session: Session) -> sessionmaker[Session]:
    return sessionmaker(bind=session.get_bind())


@pytest.fixture
def service(sessions: sessionmaker[Session]) -> SeaWorkService:
    return SeaWorkService(
        sessions, ProfileVocabulary.from_directory(REFERENCE), Names.load(REFERENCE)
    )


@pytest.fixture
def telegram() -> Telegram:
    return Telegram()


@pytest.fixture
def bot(telegram: Telegram) -> Bot:
    return Bot("42:TEST", session=telegram)


async def no_sleep(_: float) -> None:
    return None


def store(session: Session, external_id: str, countries: list[str] | None = None) -> None:
    raw = Repository(session).save_raw(
        RawItem(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            fetched_at=NOW,
            payload=external_id,
            content_type="application/xml",
            content_hash=hashlib.sha256(external_id.encode()).hexdigest(),
            http_status=200,
        )
    )
    stored = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title=f"Vacancy {external_id}",
            employer=f"Employer {external_id}",
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        hiring_scope=None
        if countries is None
        else Inferred(
            value=HiringScope(countries=countries), provenance=Provenance.RULE, confidence=0.8
        ),
        quality_score=1.0,
        quality_flags=[],
    )
    Repository(session).upsert_opportunity(
        raw.id, stored, SourceTrust.PRIMARY, OpportunityStatus.ACTIVE, keep_closed=False
    )


def join(service: SeaWorkService, external_id: str) -> uuid.UUID:
    user_id = service.start(CHANNEL, external_id).user_id
    service.answer_stage(user_id, CareerStage.WORKING, onboarding=False)
    return user_id


def account(session: Session, external_id: str) -> ChannelAccountRecord:
    session.rollback()
    return session.execute(
        select(ChannelAccountRecord).where(ChannelAccountRecord.external_id == external_id)
    ).scalar_one()


def deliver_all(service: SeaWorkService, external_id: str, now: datetime.datetime) -> None:
    user_id = service.touch(CHANNEL, external_id)
    assert user_id is not None
    prepared = service.prepare_batch(user_id, now)
    service.confirm_delivered(user_id, [p.recommendation for p in prepared], CHANNEL, now)


def button_of(call: SendMessage) -> str:
    markup = call.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    ((button,),) = markup.inline_keyboard
    assert button.callback_data is not None
    return button.callback_data


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "нашлась 1 новая вакансия"),
        (2, "нашлись 2 новые вакансии"),
        (4, "нашлись 4 новые вакансии"),
        (5, "нашлось 5 новых вакансий"),
        (11, "нашлось 11 новых вакансий"),
        (21, "нашлась 21 новая вакансия"),
    ],
)
def test_the_notice_agrees_with_the_number(count: int, expected: str) -> None:
    assert expected in texts.digest_notice(count)


async def test_a_person_with_news_gets_one_notice_that_opens_the_carousel(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    for number in range(3):
        store(session, str(number))
    join(service, "100")

    result = await send_digest(bot, service, NOW, sleep=no_sleep)

    (call,) = telegram.to(100)
    assert call.text == texts.digest_notice(3)
    assert button_of(call) == NavCb(to=Nav.FEED).pack()
    assert result.notified == 1
    assert account(session, "100").digest_notified_at == NOW


async def test_the_notice_promises_no_more_than_the_limit(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    for number in range(DAILY_LIMIT + 4):
        store(session, str(number))
    join(service, "100")

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.to(100)[0].text == texts.digest_notice(DAILY_LIMIT)


async def test_the_number_is_what_is_left_of_todays_limit(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    for number in range(DAILY_LIMIT + 4):
        store(session, str(number))
    user_id = join(service, "100")
    prepared = service.prepare_batch(user_id, NOW - datetime.timedelta(hours=1))[:3]
    service.confirm_delivered(
        user_id, [p.recommendation for p in prepared], CHANNEL, NOW - datetime.timedelta(hours=1)
    )

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.to(100)[0].text == texts.digest_notice(DAILY_LIMIT - 3)


async def test_a_second_run_on_the_same_day_sends_nothing(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    await send_digest(bot, service, NOW, sleep=no_sleep)

    again = await send_digest(bot, service, NOW + datetime.timedelta(hours=5), sleep=no_sleep)

    assert len(telegram.to(100)) == 1
    assert again.notified == 0


async def test_the_next_day_brings_a_new_notice(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    await send_digest(bot, service, NOW, sleep=no_sleep)
    store(session, "2")

    await send_digest(bot, service, NOW + DAY, sleep=no_sleep)

    assert len(telegram.to(100)) == 2


async def test_nothing_new_means_no_message_and_no_mark(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    join(service, "100")

    result = await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.sent == []
    assert result.notified == 0
    assert account(session, "100").digest_notified_at is None


async def test_an_exhausted_limit_means_no_message(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    for number in range(DAILY_LIMIT + 2):
        store(session, str(number))
    join(service, "100")
    deliver_all(service, "100", NOW - datetime.timedelta(hours=1))

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.sent == []


async def test_a_paused_account_is_not_written_to(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    service.pause(join(service, "100"), NOW)

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.sent == []


async def test_a_person_who_blocked_the_bot_is_marked_and_the_others_still_get_theirs(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    join(service, "200")
    telegram.forbidden.add(100)

    result = await send_digest(bot, service, NOW, sleep=no_sleep)

    assert result.blocked == 1
    assert result.notified == 1
    assert result.failures == 0
    assert account(session, "100").blocked_at == NOW
    assert account(session, "100").digest_notified_at is None
    assert len(telegram.to(200)) == 1


async def test_a_blocked_account_is_not_tried_again(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    telegram.forbidden.add(100)
    await send_digest(bot, service, NOW, sleep=no_sleep)
    telegram.forbidden.clear()

    await send_digest(bot, service, NOW + DAY, sleep=no_sleep)

    assert telegram.sent == []


async def test_a_person_who_comes_back_is_written_to_again(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    telegram.forbidden.add(100)
    await send_digest(bot, service, NOW, sleep=no_sleep)
    telegram.forbidden.clear()
    service.touch(CHANNEL, "100")

    await send_digest(bot, service, NOW + DAY, sleep=no_sleep)

    assert len(telegram.to(100)) == 1


async def test_one_failure_is_reported_and_costs_the_others_nothing(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    join(service, "200")
    telegram.broken.add(100)

    result = await send_digest(bot, service, NOW, sleep=no_sleep)

    assert result.notified == 1
    assert result.failures == 1
    assert len(telegram.to(200)) == 1
    assert account(session, "100").digest_notified_at is None
    assert account(session, "100").blocked_at is None


async def test_a_notice_that_failed_is_sent_by_the_next_run(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    telegram.broken.add(100)
    await send_digest(bot, service, NOW, sleep=no_sleep)
    telegram.broken.clear()

    await send_digest(bot, service, NOW + datetime.timedelta(hours=1), sleep=no_sleep)

    assert len(telegram.to(100)) == 1


async def test_the_morning_is_one_message_even_when_the_country_is_due(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1", countries=["PH"])
    join(service, "100")
    deliver_all(service, "100", NOW - DAY)
    store(session, "2", countries=["PH"])

    await send_digest(bot, service, NOW, sleep=no_sleep)

    (notice,) = telegram.to(100)
    assert notice.text == texts.digest_notice(1)


async def test_looking_asks_the_country_to_whom_it_is_due(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1", countries=["PH"])
    user_id = join(service, "100")
    deliver_all(service, "100", NOW - DAY)
    store(session, "2", countries=["PH"])

    page = service.feed_page(user_id, 0, NOW, entering=True)

    assert page.ask_hiring_country


async def test_no_question_without_news(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1", countries=["PH"])
    join(service, "100")
    deliver_all(service, "100", NOW - DAY)

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.sent == []


async def test_the_run_pauses_between_people(
    service: SeaWorkService, session: Session, bot: Bot
) -> None:
    store(session, "1")
    join(service, "100")
    join(service, "200")
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    await send_digest(bot, service, NOW, sleep=record)

    assert len(pauses) == 2


async def test_a_person_without_a_stage_gets_no_notice(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    service.start(CHANNEL, "100")

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.sent == []


async def test_a_flood_wait_is_waited_out_and_the_notice_sent_once_more(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    telegram.flood[100] = 1
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    result = await send_digest(bot, service, NOW, sleep=record)

    assert pauses[0] == 7
    assert len(telegram.to(100)) == 1
    assert result.notified == 1
    assert result.failures == 0


async def test_a_second_flood_wait_is_a_failure_and_the_notice_stays_unsent(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    store(session, "1")
    join(service, "100")
    telegram.flood[100] = 2

    result = await send_digest(bot, service, NOW, sleep=no_sleep)

    assert result.failures == 1
    assert telegram.sent == []
    assert account(session, "100").digest_notified_at is None


async def test_refused_cards_do_not_lower_the_number(
    service: SeaWorkService, session: Session, bot: Bot, telegram: Telegram
) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = join(service, "100")
    refused = service.prepare_batch(user_id, NOW)[0].recommendation
    service.confirm_delivered(user_id, [refused], CHANNEL, NOW, rejected=True)

    await send_digest(bot, service, NOW, sleep=no_sleep)

    assert telegram.to(100)[0].text == texts.digest_notice(2)


def test_the_command_refuses_to_run_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: Settings.model_construct(telegram_bot_token=None))

    result = CliRunner().invoke(cli.app, ["deliver", "digest"])

    assert result.exit_code == 1


@pytest.mark.parametrize(("failures", "code"), [(0, 0), (1, 1)])
def test_the_exit_code_follows_the_failures(
    monkeypatch: pytest.MonkeyPatch, failures: int, code: int
) -> None:
    async def run(settings: Settings, token: str) -> DigestResult:
        return DigestResult(notified=1, failures=failures)

    monkeypatch.setattr(
        cli, "Settings", lambda: Settings.model_construct(telegram_bot_token=SecretStr("x"))
    )
    monkeypatch.setattr("seawork.bot.main.run_digest", run)

    result = CliRunner().invoke(cli.app, ["deliver", "digest"])

    assert result.exit_code == code
