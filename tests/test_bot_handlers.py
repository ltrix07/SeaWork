"""The bot against a real database and a Telegram that only records what it is asked.

Whole conversations go through the real dispatcher, router and service; the only fake is
the HTTP session, so what is asserted is what the person would have received.
"""

import datetime
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from pydantic import HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from seawork.app import Names, SeaWorkService
from seawork.bot import keyboards, texts
from seawork.bot.keyboards import CountryCb, DirectionCb, ReactCb, Reaction
from seawork.bot.main import build_dispatcher
from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, HiringScope, NormalizedOpportunity
from seawork.domain.profile import BehaviourAction, CareerStage
from seawork.ingestion.base import RawItem
from seawork.storage.models import (
    Base,
    BehaviourEventRecord,
    ChannelAccountRecord,
    DeliveryRecord,
    UserProfileRecord,
)
from seawork.storage.profile_vocabulary import ProfileVocabulary
from seawork.storage.repository import Repository

REFERENCE = Path("data/reference")
NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)
CHAT = 100


class Telegram(BaseSession):
    """Records every call; answers like the real API would, with an echo of the text."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.refuse_after: int | None = None
        self.reject_containing: str | None = None
        self._ids = 1000

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, SendMessage) and self.refuse_after is not None:
            if len(self.sent(SendMessage)) >= self.refuse_after:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked")
        if (
            isinstance(method, SendMessage)
            and self.reject_containing is not None
            and self.reject_containing in method.text
        ):
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")
        self.calls.append(method)
        if isinstance(method, SendMessage | SendDocument):
            self._ids += 1
            message = Message(
                message_id=self._ids, date=NOW, chat=Chat(id=CHAT, type="private"), text="x"
            )
            return message  # type: ignore[return-value]
        return True  # type: ignore[return-value]

    async def stream_content(  # pragma: no cover
        self,
        url: str,
        headers: Any = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    def sent[M: TelegramMethod[Any]](self, kind: type[M]) -> list[M]:
        return [call for call in self.calls if isinstance(call, kind)]

    def texts(self) -> list[str]:
        return [str(call.text) for call in self.sent(SendMessage)]


class Chatter:
    def __init__(self, dispatcher: Dispatcher, telegram: Telegram) -> None:
        self.dispatcher = dispatcher
        self.telegram = telegram
        self.bot = Bot("42:TEST", session=telegram)
        self._update_id = 0

    async def say(self, text: str) -> None:
        message = Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=CHAT, type="private"),
            from_user=User(id=CHAT, is_bot=False, first_name="t"),
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]  # type: ignore[list-item]
            if text.startswith("/")
            else None,
        )
        await self._feed(Update(update_id=self._next(), message=message))

    async def tap(self, data: str, markup: InlineKeyboardMarkup | None, chat: int = CHAT) -> None:
        message = Message(
            message_id=50,
            date=NOW,
            chat=Chat(id=chat, type="private" if chat > 0 else "group"),
            text="card",
            reply_markup=markup,
        )
        query = CallbackQuery(
            id="1",
            from_user=User(id=chat, is_bot=False, first_name="t"),
            chat_instance="c",
            message=message,
            data=data,
        )
        await self._feed(Update(update_id=self._next(), callback_query=query))

    def last_markup(self, offset: int = -1) -> InlineKeyboardMarkup:
        markup = self.telegram.sent(SendMessage)[offset].reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup

    def card_markups(self) -> list[InlineKeyboardMarkup]:
        return [
            m
            for call in self.telegram.sent(SendMessage)
            if isinstance(m := call.reply_markup, InlineKeyboardMarkup)
            and m.inline_keyboard[0][0].url is not None
        ]

    def alerts(self) -> list[str | None]:
        return [call.text for call in self.telegram.sent(AnswerCallbackQuery)]

    async def _feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    def _next(self) -> int:
        self._update_id += 1
        return self._update_id


@pytest.fixture
def test_schema() -> str:
    return "seawork_bot_test"


@pytest.fixture
def sessions(session: Session) -> sessionmaker[Session]:
    return sessionmaker(bind=session.get_bind())


@pytest.fixture
def service(sessions: sessionmaker[Session]) -> SeaWorkService:
    return SeaWorkService(
        sessions, ProfileVocabulary.from_directory(REFERENCE), Names.load(REFERENCE)
    )


@pytest.fixture
def clock() -> list[datetime.datetime]:
    return [NOW]


@pytest.fixture
def telegram() -> Telegram:
    return Telegram()


@pytest.fixture
def chat(service: SeaWorkService, telegram: Telegram, clock: list[datetime.datetime]) -> Chatter:
    return Chatter(build_dispatcher(service, lambda: clock[0]), telegram)


def store(
    session: Session,
    external_id: str,
    countries: list[str] | None = None,
    employer: str | None = None,
    title: str | None = None,
) -> int:
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
            title=title or f"Vacancy {external_id}",
            employer=employer or f"Employer {external_id}",
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
    return Repository(session).list_feed_candidates()[-1].opportunity_id


def rows(session: Session, record: type[Base]) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(record)).scalar_one()


async def onboard(chat: Chatter, stage: keyboards.StageCb | None = None) -> None:
    await chat.say("/start")
    question = chat.last_markup()
    await chat.tap(
        (stage or keyboards.StageCb(stage=CareerStage.WORKING, onboarding=True)).pack(), question
    )


async def test_first_start_asks_exactly_one_question_and_shows_no_vacancy(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")

    await chat.say("/start")

    assert telegram.texts() == [texts.WELCOME, texts.STAGE_QUESTION]
    assert len(chat.last_markup().inline_keyboard) == len(texts.STAGE_BUTTONS)
    assert rows(session, UserProfileRecord) == 1
    assert rows(session, DeliveryRecord) == 0


async def test_the_answer_is_followed_by_the_first_batch_and_it_is_recorded(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    store(session, "2")

    await onboard(chat)

    assert len(chat.card_markups()) == 2
    assert rows(session, DeliveryRecord) == 2
    assert texts.DIRECTIONS_QUESTION not in telegram.texts()


async def test_someone_who_wants_in_is_asked_for_directions_after_the_batch(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")

    await onboard(chat, keyboards.StageCb(stage=CareerStage.ENTRY, onboarding=True))

    assert telegram.texts()[-1] == texts.DIRECTIONS_QUESTION
    assert len(chat.card_markups()) == 1


async def test_not_for_me_keeps_the_vacancy_out_and_take_back_lets_it_return(
    chat: Chatter, telegram: Telegram, session: Session, clock: list[datetime.datetime]
) -> None:
    first = store(session, "1")
    await onboard(chat)
    card_markup = chat.card_markups()[0]

    await chat.tap(ReactCb(opportunity_id=first, reaction=Reaction.DISMISS).pack(), card_markup)

    edited = telegram.sent(EditMessageReplyMarkup)[-1].reply_markup
    assert edited is not None
    assert [b.text for b in edited.inline_keyboard[1]] == [texts.DISMISSED, texts.TAKE_BACK]
    event = session.execute(select(BehaviourEventRecord)).scalar_one()
    assert event.action == BehaviourAction.DISMISSED.value

    clock[0] = NOW + datetime.timedelta(days=1)
    await chat.say("/feed")
    assert texts.EMPTY_FEED in telegram.texts()
    assert len(chat.card_markups()) == 1

    await chat.tap(ReactCb(opportunity_id=first, reaction=Reaction.REOPEN).pack(), edited)
    assert session.execute(select(func.count()).select_from(BehaviourEventRecord)).scalar_one() == 2


async def test_interesting_is_journalled_and_confirmed_on_the_card(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1")
    await onboard(chat)

    await chat.tap(
        ReactCb(opportunity_id=first, reaction=Reaction.SAVE).pack(), chat.card_markups()[0]
    )

    edited = telegram.sent(EditMessageReplyMarkup)[-1].reply_markup
    assert edited is not None
    assert [b.text for b in edited.inline_keyboard[1]] == [texts.SAVED]
    assert session.execute(select(BehaviourEventRecord)).scalar_one().action == "saved"


async def test_an_empty_feed_says_so(chat: Chatter, telegram: Telegram) -> None:
    await onboard(chat)

    assert telegram.texts()[-1] == texts.EMPTY_FEED


async def test_the_daily_limit_is_respected_by_feed(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(8):
        store(session, str(number))
    await onboard(chat)

    await chat.say("/feed")

    assert len(chat.card_markups()) == 5
    assert texts.EMPTY_FEED in telegram.texts()


async def test_only_what_was_sent_is_recorded_when_sending_fails(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    await chat.say("/start")
    question = chat.last_markup()
    telegram.refuse_after = 3

    with pytest.raises(TelegramForbiddenError):
        await chat.tap(
            keyboards.StageCb(stage=CareerStage.WORKING, onboarding=True).pack(), question
        )

    assert rows(session, DeliveryRecord) == 1


async def test_directions_are_stored_only_after_done_and_skip_stores_nothing(
    chat: Chatter, telegram: Telegram, session: Session, service: SeaWorkService
) -> None:
    await onboard(chat, keyboards.StageCb(stage=CareerStage.ENTRY, onboarding=True))
    picker = chat.last_markup()

    await chat.tap(DirectionCb(key=keyboards.DONE).pack(), picker)
    assert telegram.sent(AnswerCallbackQuery)[-1].show_alert
    before = service.export(_user(service))
    assert before is not None
    assert before.profile.directions is None

    await chat.tap(DirectionCb(key="cruise").pack(), picker)
    toggled = telegram.sent(EditMessageReplyMarkup)[-1].reply_markup
    assert toggled is not None
    await chat.tap(DirectionCb(key=keyboards.DONE).pack(), toggled)

    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.directions is not None
    assert stored.profile.directions.value == ["cruise"]
    assert stored.profile.directions.source.value == "stated"


async def test_skipping_directions_writes_nothing(
    chat: Chatter, telegram: Telegram, service: SeaWorkService
) -> None:
    await onboard(chat, keyboards.StageCb(stage=CareerStage.ENTRY, onboarding=True))

    await chat.tap(DirectionCb(key=keyboards.SKIP).pack(), chat.last_markup())

    assert telegram.sent(DeleteMessage)
    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.directions is None


async def test_the_hiring_country_is_asked_once_on_the_second_day_before_the_batch(
    chat: Chatter,
    telegram: Telegram,
    session: Session,
    clock: list[datetime.datetime],
    service: SeaWorkService,
) -> None:
    store(session, "1", countries=["GB"])
    store(session, "2", countries=["GB"])
    await onboard(chat)
    assert texts.COUNTRY_QUESTION not in telegram.texts()

    clock[0] = NOW + datetime.timedelta(days=1)
    store(session, "3", countries=["PH"])
    await chat.say("/feed")

    sent = telegram.texts()
    assert sent.count(texts.COUNTRY_QUESTION) == 1
    question = sent.index(texts.COUNTRY_QUESTION)
    assert question < len(sent) - 1
    picker = chat.last_markup(
        next(
            i
            for i, call in enumerate(telegram.sent(SendMessage))
            if call.text == texts.COUNTRY_QUESTION
        )
    )
    await chat.tap(CountryCb(key="GB").pack(), picker)
    toggled = telegram.sent(EditMessageReplyMarkup)[-1].reply_markup
    assert toggled is not None
    await chat.tap(CountryCb(key=keyboards.DONE).pack(), toggled)
    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.hiring_country is not None
    assert stored.profile.hiring_country.value == ["GB"]

    await chat.say("/feed")
    assert telegram.texts().count(texts.COUNTRY_QUESTION) == 1


async def test_pause_and_resume_take_the_person_in_and_out_of_the_digest(
    chat: Chatter, service: SeaWorkService
) -> None:
    await chat.say("/start")
    await chat.say("/pause")
    assert service.digest_recipients("telegram") == []

    await chat.say("/resume")
    assert [external for external, _ in service.digest_recipients("telegram")] == [str(CHAT)]


async def test_export_sends_the_data_as_a_json_document(chat: Chatter, telegram: Telegram) -> None:
    await onboard(chat)

    await chat.say("/export")

    document = telegram.sent(SendDocument)[0].document
    assert isinstance(document, BufferedInputFile)
    assert document.filename == texts.EXPORT_FILENAME
    assert json.loads(document.data)["accounts"][0]["external_id"] == str(CHAT)


async def test_delete_needs_confirmation_and_removes_everything(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)

    await chat.say("/delete")
    confirm = chat.last_markup()
    await chat.tap(keyboards.DeleteCb(confirmed=False).pack(), confirm)
    assert rows(session, UserProfileRecord) == 1

    await chat.tap(keyboards.DeleteCb(confirmed=True).pack(), confirm)
    assert telegram.sent(EditMessageText)[-1].text == texts.DELETED
    for record in (UserProfileRecord, ChannelAccountRecord, DeliveryRecord, BehaviourEventRecord):
        assert rows(session, record) == 0


async def test_old_buttons_after_delete_say_the_data_is_gone_and_create_nobody(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1")
    await onboard(chat)
    card_markup = chat.card_markups()[0]
    await chat.say("/delete")
    await chat.tap(keyboards.DeleteCb(confirmed=True).pack(), chat.last_markup())

    await chat.tap(ReactCb(opportunity_id=first, reaction=Reaction.DISMISS).pack(), card_markup)
    await chat.tap(keyboards.SimilarCb(opportunity_id=first).pack(), card_markup)
    await chat.tap(keyboards.StageCb(stage=CareerStage.WORKING, onboarding=True).pack(), None)

    assert chat.alerts()[-3:] == [texts.PRESS_START] * 3
    assert rows(session, ChannelAccountRecord) == 0
    assert rows(session, UserProfileRecord) == 0
    assert rows(session, BehaviourEventRecord) == 0


async def test_similar_of_an_old_card_after_a_new_start_says_the_data_is_gone(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1")
    await chat.say("/start")

    await chat.tap(keyboards.SimilarCb(opportunity_id=first).pack(), None)

    assert chat.alerts()[-1] == texts.GONE
    assert telegram.sent(AnswerCallbackQuery)[-1].show_alert


async def test_a_group_chat_is_not_served(chat: Chatter, telegram: Telegram) -> None:
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=-5, type="group"),
        from_user=User(id=CHAT, is_bot=False, first_name="t"),
        text="/start",
        entities=[{"type": "bot_command", "offset": 0, "length": 6}],  # type: ignore[list-item]
    )

    await chat.dispatcher.feed_update(chat.bot, Update(update_id=1, message=message))

    assert telegram.calls == []


def _user(service: SeaWorkService) -> uuid.UUID:
    found = service.touch("telegram", str(CHAT))
    assert found is not None
    return found


async def test_feed_and_export_after_delete_create_nobody(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    await chat.say("/delete")
    await chat.tap(keyboards.DeleteCb(confirmed=True).pack(), chat.last_markup())

    for command in ("/feed", "/export", "/profile", "/pause", "/resume"):
        await chat.say(command)

    assert telegram.texts()[-5:] == [texts.PRESS_START] * 5
    assert not telegram.sent(SendDocument)
    assert rows(session, UserProfileRecord) == 0
    assert rows(session, ChannelAccountRecord) == 0


async def test_feed_before_start_asks_for_start(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")

    await chat.say("/feed")

    assert telegram.texts() == [texts.PRESS_START]
    assert rows(session, UserProfileRecord) == 0
    assert rows(session, DeliveryRecord) == 0


async def test_a_stage_changed_from_the_profile_sends_no_batch_and_asks_nothing(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat, keyboards.StageCb(stage=CareerStage.ENTRY, onboarding=True))
    await chat.tap(DirectionCb(key=keyboards.SKIP).pack(), chat.last_markup())
    sent_before = len(telegram.sent(SendMessage))

    await chat.say("/profile")
    await chat.tap(keyboards.ChangeCb(what=keyboards.Change.STAGE).pack(), chat.last_markup())
    await chat.tap(
        keyboards.StageCb(stage=CareerStage.ENTRY, onboarding=False).pack(), chat.last_markup()
    )

    new = telegram.texts()[sent_before:]
    assert len(new) == 2
    assert new[1] == texts.STAGE_QUESTION
    assert rows(session, DeliveryRecord) == 1
    assert telegram.sent(EditMessageText)[-1].text == texts.PICKED_STAGE.format(
        stage=texts.STAGE_NAMES[CareerStage.ENTRY]
    )


async def test_the_profile_shows_what_is_known_and_offers_three_changes(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1", countries=["GB"])
    await onboard(chat)

    await chat.say("/profile")
    profile_text = telegram.texts()[-1]
    profile_markup = chat.last_markup()

    assert texts.STAGE_NAMES[CareerStage.WORKING] in profile_text
    assert len(profile_markup.inline_keyboard) == 3

    await chat.tap(keyboards.ChangeCb(what=keyboards.Change.STAGE).pack(), profile_markup)
    assert telegram.texts()[-1] == texts.STAGE_QUESTION
    assert chat.last_markup().inline_keyboard[0][0].callback_data == (
        keyboards.StageCb(stage=CareerStage.WORKING, onboarding=False).pack()
    )

    await chat.tap(keyboards.ChangeCb(what=keyboards.Change.DIRECTIONS).pack(), profile_markup)
    assert telegram.texts()[-1] == texts.DIRECTIONS_QUESTION

    await chat.tap(keyboards.ChangeCb(what=keyboards.Change.COUNTRY).pack(), profile_markup)
    assert telegram.texts()[-1] == texts.COUNTRY_QUESTION
    assert [b.text for row in chat.last_markup().inline_keyboard[:-1] for b in row] == [
        "United Kingdom"
    ]


async def test_a_group_reaches_the_deliveries_with_its_members_and_opens_through_similar(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1", employer="Acme", title="Chef de Partie")
    second = store(session, "2", employer="Acme", title="Chef de Partie")
    await onboard(chat)

    (card_markup,) = chat.card_markups()
    delivery = session.execute(select(DeliveryRecord)).scalar_one()
    assert {delivery.opportunity_id, *delivery.similar_ids} == {first, second}
    assert len(delivery.similar_ids) == 1
    assert card_markup.inline_keyboard[2][0].text == texts.SIMILAR.format(count=1)

    await chat.tap(keyboards.SimilarCb(opportunity_id=delivery.opportunity_id).pack(), card_markup)

    listing = telegram.texts()[-1]
    assert texts.SIMILAR_HEADER in listing
    assert listing.count("<a href=") == 1


async def test_a_card_telegram_rejects_is_skipped_and_counted_as_delivered(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(3):
        store(session, str(number), title=f"Title {number}")
    telegram.reject_containing = "Title 1"
    await chat.say("/start")

    await chat.tap(
        keyboards.StageCb(stage=CareerStage.WORKING, onboarding=True).pack(), chat.last_markup()
    )

    assert len(chat.card_markups()) == 2
    assert rows(session, DeliveryRecord) == 3


async def test_a_dead_button_gets_an_answer(chat: Chatter, telegram: Telegram) -> None:
    await chat.tap("garbage", None)

    assert chat.alerts() == [texts.STALE_BUTTON]


async def test_buttons_of_other_chats_than_private_are_not_served(
    chat: Chatter, telegram: Telegram
) -> None:
    await chat.tap("garbage", None, chat=-5)

    assert telegram.sent(AnswerCallbackQuery) == []
