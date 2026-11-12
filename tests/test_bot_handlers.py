"""The bot against a real database and a Telegram that only records what it is asked.

Whole conversations go through the real dispatcher, router and service; the only fake is
the HTTP session, so what is asserted is what the person would have received: which
messages were sent, which were edited, and what the one message said at each step.
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
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
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
    InaccessibleMessage,
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
from seawork.bot.keyboards import DirectionCb, SimilarCb
from seawork.bot.main import build_dispatcher
from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, HiringScope, NormalizedOpportunity
from seawork.domain.profile import BehaviourAction, BehaviourEvent, CareerStage
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
        self.reject_containing: str | None = None
        self.edit_error: str | None = None
        self._ids = 1000

    async def close(self) -> None:
        pass

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, EditMessageText) and self.edit_error is not None:
            raise TelegramBadRequest(method=method, message=self.edit_error)
        if isinstance(method, SendMessage):
            self._reject_if_marked(method, method.text)
        if isinstance(method, EditMessageText):
            self._reject_if_marked(method, method.text)
        self.calls.append(method)
        if isinstance(method, SendMessage | SendDocument):
            self._ids += 1
            message = Message(
                message_id=self._ids, date=NOW, chat=Chat(id=CHAT, type="private"), text="x"
            )
            return message  # type: ignore[return-value]
        return True  # type: ignore[return-value]

    def _reject_if_marked(self, method: TelegramMethod[Any], text: str | None) -> None:
        if self.reject_containing is not None and self.reject_containing in str(text):
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")

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

    async def tap(
        self,
        data: str,
        markup: InlineKeyboardMarkup | None,
        chat: int = CHAT,
        *,
        inaccessible: bool = False,
    ) -> None:
        room = Chat(id=chat, type="private" if chat > 0 else "group")
        message = (
            InaccessibleMessage(chat=room, message_id=50, date=0)
            if inaccessible
            else Message(message_id=50, date=NOW, chat=room, text="card", reply_markup=markup)
        )
        query = CallbackQuery(
            id="1",
            from_user=User(id=chat, is_bot=False, first_name="t"),
            chat_instance="c",
            message=message,
            data=data,
        )
        await self._feed(Update(update_id=self._next(), callback_query=query))

    async def press(self, label: str) -> None:
        markup = self.screen_markup()
        (button,) = [b for row in markup.inline_keyboard for b in row if b.text == label]
        assert button.callback_data is not None
        await self.tap(button.callback_data, markup)

    def screen_markup(self) -> InlineKeyboardMarkup:
        for call in reversed(self.telegram.calls):
            if isinstance(call, SendMessage | EditMessageText | EditMessageReplyMarkup):
                assert isinstance(call.reply_markup, InlineKeyboardMarkup)
                return call.reply_markup
        raise AssertionError("nothing was shown")

    def screen_text(self) -> str:
        for call in reversed(self.telegram.calls):
            if isinstance(call, SendMessage | EditMessageText):
                return str(call.text)
        raise AssertionError("nothing was shown")

    def labels(self) -> list[str]:
        return [b.text for row in self.screen_markup().inline_keyboard for b in row]

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


NAMES = Names.load(REFERENCE)
WORKING = texts.STAGE_BUTTONS[CareerStage.WORKING]
ENTRY = texts.STAGE_BUTTONS[CareerStage.ENTRY]
CRUISE = NAMES.directions["cruise"]


def rows(session: Session, record: type[Base]) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(record)).scalar_one()


async def onboard(chat: Chatter, stage: str = WORKING) -> None:
    await chat.say("/start")
    await chat.press(stage)


def header(position: int, total: int) -> str:
    return texts.FEED_HEADER.format(position=position, total=total)


def _user(service: SeaWorkService) -> uuid.UUID:
    found = service.touch("telegram", str(CHAT))
    assert found is not None
    return found


def callback_of(markup: InlineKeyboardMarkup, label: str) -> str:
    (button,) = [b for row in markup.inline_keyboard for b in row if b.text == label]
    assert button.callback_data is not None
    return button.callback_data


async def test_first_start_is_one_message_with_the_welcome_and_the_question(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")

    await chat.say("/start")

    assert telegram.texts() == [texts.ONBOARDING]
    assert len(chat.screen_markup().inline_keyboard) == len(texts.STAGE_BUTTONS)
    assert rows(session, UserProfileRecord) == 1
    assert rows(session, DeliveryRecord) == 0


async def test_the_answer_turns_the_same_message_into_the_first_card(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))

    await onboard(chat)

    assert len(telegram.sent(SendMessage)) == 1
    assert chat.screen_text().startswith(header(1, 3))
    assert texts.PREVIOUS not in chat.labels()
    assert texts.NEXT in chat.labels()
    assert rows(session, DeliveryRecord) == 1


async def test_someone_who_wants_in_is_asked_for_directions_before_the_first_card(
    chat: Chatter, telegram: Telegram, session: Session, service: SeaWorkService
) -> None:
    store(session, "1")

    await onboard(chat, ENTRY)

    assert chat.screen_text() == texts.DIRECTIONS_QUESTION
    assert rows(session, DeliveryRecord) == 0

    await chat.press(texts.SKIP)

    assert chat.screen_text().startswith(header(1, 1))
    assert len(telegram.sent(SendMessage)) == 1
    assert rows(session, DeliveryRecord) == 1
    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.directions is None


async def test_directions_are_stored_only_after_done(
    chat: Chatter, telegram: Telegram, session: Session, service: SeaWorkService
) -> None:
    store(session, "1")
    await onboard(chat, ENTRY)

    await chat.press(texts.DONE)
    assert telegram.sent(AnswerCallbackQuery)[-1].show_alert
    assert chat.screen_text() == texts.DIRECTIONS_QUESTION

    await chat.press(CRUISE)
    await chat.press(texts.DONE)

    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.directions is not None
    assert stored.profile.directions.value == ["cruise"]
    assert stored.profile.directions.source.value == "stated"
    assert chat.screen_text().startswith(header(1, 1))


async def test_a_card_is_delivered_when_it_appears_not_when_it_is_prepared(
    chat: Chatter, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    await onboard(chat)
    assert rows(session, DeliveryRecord) == 1

    await chat.press(texts.NEXT)
    assert chat.screen_text().startswith(header(2, 3))
    assert rows(session, DeliveryRecord) == 2

    await chat.press(texts.PREVIOUS)
    assert chat.screen_text().startswith(header(1, 3))
    assert rows(session, DeliveryRecord) == 2

    await chat.press(texts.NEXT)
    await chat.press(texts.NEXT)
    assert chat.screen_text().startswith(header(3, 3))
    assert texts.NEXT not in chat.labels()
    assert rows(session, DeliveryRecord) == 3


async def test_the_daily_limit_holds_while_paging(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(8):
        store(session, str(number))
    await onboard(chat)

    for _ in range(4):
        await chat.press(texts.NEXT)

    assert chat.screen_text().startswith(header(5, 5))
    assert texts.NEXT not in chat.labels()
    assert rows(session, DeliveryRecord) == 5

    await chat.say("/feed")
    assert chat.screen_text().startswith(header(1, 5))
    assert rows(session, DeliveryRecord) == 5


async def test_going_back_shows_the_same_card_with_its_reaction(
    chat: Chatter, session: Session
) -> None:
    for number in range(2):
        store(session, str(number))
    await onboard(chat)
    first = chat.screen_text()

    await chat.press(texts.INTERESTING)
    assert texts.SAVED in chat.labels()
    assert texts.INTERESTING not in chat.labels()
    await chat.press(texts.NEXT)
    await chat.press(texts.NOT_FOR_ME)
    assert chat.labels().count(texts.DISMISSED) == 1
    assert texts.TAKE_BACK in chat.labels()

    await chat.press(texts.PREVIOUS)

    assert chat.screen_text() == first
    assert texts.SAVED in chat.labels()
    assert [e.action for e in _events(session)] == [
        BehaviourAction.SAVED,
        BehaviourAction.DISMISSED,
    ]
    assert rows(session, DeliveryRecord) == 2


async def test_take_back_restores_the_reaction_row_and_is_journalled(
    chat: Chatter, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    await chat.press(texts.NOT_FOR_ME)

    await chat.press(texts.TAKE_BACK)

    assert texts.INTERESTING in chat.labels()
    assert texts.NOT_FOR_ME in chat.labels()
    assert [e.action for e in _events(session)] == [
        BehaviourAction.DISMISSED,
        BehaviourAction.REOPENED,
    ]


async def test_a_dismissed_vacancy_does_not_come_back_the_next_day(
    chat: Chatter, session: Session, clock: list[datetime.datetime]
) -> None:
    for number in range(2):
        store(session, str(number), title=f"Title {number}")
    await onboard(chat)
    dismissed = chat.screen_text()
    await chat.press(texts.NOT_FOR_ME)
    await chat.press(texts.NEXT)

    clock[0] = NOW + datetime.timedelta(days=1)
    await chat.say("/feed")
    await chat.press(texts.SKIP_COUNTRY)

    assert chat.screen_text() == texts.EMPTY_FEED
    assert dismissed.count("Title") == 1


async def test_an_empty_feed_says_so_and_offers_the_menu(chat: Chatter) -> None:
    await onboard(chat)

    assert chat.screen_text() == texts.EMPTY_FEED
    assert chat.labels() == [texts.BACK_TO_MENU]


async def test_a_card_telegram_refuses_is_never_shown_again_and_does_not_count(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(3):
        store(session, str(number), title=f"Title {number}")
    telegram.reject_containing = "Title 2"
    await chat.say("/start")

    await chat.press(WORKING)

    assert "Title 1" in chat.screen_text()
    assert chat.screen_text().startswith(header(1, 2))
    assert rows(session, DeliveryRecord) == 2

    await chat.press(texts.NEXT)
    await chat.press(texts.PREVIOUS)
    assert "Title 1" in chat.screen_text()
    await chat.say("/feed")
    assert "Title 1" in chat.screen_text()
    assert chat.screen_text().startswith(header(1, 2))
    assert rows(session, DeliveryRecord) == 3


async def test_an_error_that_is_not_about_the_card_is_not_swallowed(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await chat.say("/start")
    telegram.edit_error = "Bad Request: chat not found"

    with pytest.raises(TelegramBadRequest):
        await chat.press(WORKING)

    assert rows(session, DeliveryRecord) == 0


async def test_a_reaction_on_the_first_card_does_not_turn_into_the_country_question(
    chat: Chatter, telegram: Telegram, session: Session, clock: list[datetime.datetime]
) -> None:
    store(session, "1", countries=["GB"])
    store(session, "2", countries=["GB"])
    await onboard(chat)
    yesterdays = chat.screen_markup()
    clock[0] = NOW + datetime.timedelta(days=1)

    await chat.tap(keyboards.PageCb(index=0).pack(), yesterdays)
    assert chat.screen_text().startswith(header(1, 1))
    await chat.press(texts.INTERESTING)

    assert chat.screen_text().startswith(header(1, 1))
    assert texts.COUNTRY_QUESTION not in chat.screen_text()
    assert texts.SAVED in chat.labels()


async def test_a_negative_page_is_a_dead_button(chat: Chatter, session: Session) -> None:
    store(session, "1")
    await onboard(chat)

    await chat.tap("p:-1", chat.screen_markup())

    assert chat.alerts()[-1] == texts.STALE_BUTTON


async def test_a_picker_on_a_message_that_is_no_longer_accessible_is_a_dead_button(
    chat: Chatter,
) -> None:
    await onboard(chat)

    await chat.tap(DirectionCb(key="cruise", onboarding=False).pack(), None, inaccessible=True)

    assert chat.alerts()[-1] == texts.STALE_BUTTON


async def test_feed_before_the_stage_is_answered_still_shows_the_carousel(
    chat: Chatter, session: Session
) -> None:
    store(session, "1")
    await chat.say("/start")

    await chat.say("/feed")

    assert chat.screen_text().startswith(header(1, 1))
    assert rows(session, DeliveryRecord) == 1


@pytest.mark.parametrize(
    "reason",
    ["Bad Request: message can't be edited", "Bad Request: message to edit not found"],
)
async def test_a_message_that_cannot_be_edited_is_replaced_by_a_new_one(
    chat: Chatter, telegram: Telegram, session: Session, reason: str
) -> None:
    store(session, "1")
    await chat.say("/start")
    telegram.edit_error = reason

    await chat.press(WORKING)

    assert len(telegram.sent(SendMessage)) == 2
    assert chat.screen_text().startswith(header(1, 1))
    assert rows(session, DeliveryRecord) == 1


async def test_a_message_too_old_to_be_accessible_is_replaced_by_a_new_one(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    await chat.press(texts.BACK_TO_MENU)
    sent = len(telegram.sent(SendMessage))
    menu_feed = callback_of(chat.screen_markup(), texts.MENU_FEED)

    await chat.tap(menu_feed, None, inaccessible=True)

    assert len(telegram.sent(SendMessage)) == sent + 1
    assert chat.screen_text().startswith(header(1, 1))


async def test_an_unchanged_screen_is_not_an_error_and_sends_nothing(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await chat.say("/start")
    telegram.edit_error = "Bad Request: message is not modified"

    await chat.press(WORKING)

    assert len(telegram.sent(SendMessage)) == 1
    assert rows(session, DeliveryRecord) == 1


async def test_any_other_refusal_of_a_shown_card_is_an_error(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    telegram.edit_error = "Bad Request: can't parse entities"

    with pytest.raises(TelegramBadRequest):
        await chat.press(texts.INTERESTING)


async def test_the_hiring_country_is_asked_once_on_the_second_day_before_the_carousel(
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

    assert chat.screen_text() == texts.COUNTRY_QUESTION
    await chat.press("United Kingdom")
    await chat.press(texts.DONE)

    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.hiring_country is not None
    assert stored.profile.hiring_country.value == ["GB"]
    assert chat.screen_text().startswith(header(1, 1))
    assert len(telegram.sent(SendMessage)) == 2

    await chat.say("/feed")
    assert texts.COUNTRY_QUESTION not in chat.screen_text()
    assert telegram.texts().count(texts.COUNTRY_QUESTION) == 1


async def test_skipping_the_hiring_country_writes_nothing_and_is_not_asked_again(
    chat: Chatter,
    telegram: Telegram,
    session: Session,
    clock: list[datetime.datetime],
    service: SeaWorkService,
) -> None:
    store(session, "1", countries=["GB"])
    await onboard(chat)
    clock[0] = NOW + datetime.timedelta(days=1)
    store(session, "2", countries=["GB"])
    await chat.say("/feed")

    await chat.press(texts.SKIP_COUNTRY)

    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.hiring_country is None
    assert chat.screen_text().startswith(header(1, 1))
    await chat.say("/feed")
    assert telegram.texts().count(texts.COUNTRY_QUESTION) == 1


async def test_notifications_are_switched_from_the_menu(
    chat: Chatter, service: SeaWorkService
) -> None:
    await onboard(chat)
    await chat.press(texts.BACK_TO_MENU)
    assert texts.MENU_NOTIFY_ON in chat.labels()

    await chat.press(texts.MENU_NOTIFY_ON)
    assert texts.MENU_NOTIFY_OFF in chat.labels()
    assert service.digest_recipients("telegram") == []

    await chat.press(texts.MENU_NOTIFY_OFF)
    assert texts.MENU_NOTIFY_ON in chat.labels()
    assert [external for external, _ in service.digest_recipients("telegram")] == [str(CHAT)]


async def test_pause_and_resume_commands_still_work(chat: Chatter, service: SeaWorkService) -> None:
    await onboard(chat)
    await chat.say("/pause")
    assert service.digest_recipients("telegram") == []

    await chat.say("/resume")
    assert [external for external, _ in service.digest_recipients("telegram")] == [str(CHAT)]


async def test_export_is_a_file_from_the_menu_and_from_the_command(
    chat: Chatter, telegram: Telegram
) -> None:
    await onboard(chat)
    await chat.press(texts.BACK_TO_MENU)
    await chat.press(texts.MENU_DATA)
    sent = len(telegram.sent(SendMessage))

    await chat.press(texts.DATA_EXPORT)
    await chat.say("/export")

    documents = telegram.sent(SendDocument)
    assert len(documents) == 2
    for call in documents:
        assert isinstance(call.document, BufferedInputFile)
        assert call.document.filename == texts.EXPORT_FILENAME
        assert json.loads(call.document.data)["accounts"][0]["external_id"] == str(CHAT)
    assert len(telegram.sent(SendMessage)) == sent


async def test_delete_needs_confirmation_and_removes_everything(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    await chat.press(texts.BACK_TO_MENU)
    await chat.press(texts.MENU_DATA)
    await chat.press(texts.DATA_DELETE)
    assert chat.screen_text() == texts.DELETE_CONFIRM

    await chat.press(texts.DELETE_NO)
    assert chat.screen_text() == texts.DELETE_CANCELLED
    assert rows(session, UserProfileRecord) == 1

    await chat.press(texts.BACK_TO_MENU)
    await chat.press(texts.MENU_DATA)
    await chat.press(texts.DATA_DELETE)
    await chat.press(texts.DELETE_YES)

    assert telegram.sent(EditMessageText)[-1].text == texts.DELETED
    for record in (UserProfileRecord, ChannelAccountRecord, DeliveryRecord, BehaviourEventRecord):
        assert rows(session, record) == 0


async def test_old_buttons_after_delete_say_the_data_is_gone_and_create_nobody(
    chat: Chatter, session: Session
) -> None:
    first = store(session, "1")
    await onboard(chat)
    card_markup = chat.screen_markup()
    await chat.say("/delete")
    await chat.press(texts.DELETE_YES)

    await chat.tap(callback_of(card_markup, texts.NOT_FOR_ME), card_markup)
    await chat.tap(SimilarCb(opportunity_id=first, index=0).pack(), card_markup)
    await chat.tap(keyboards.StageCb(stage=CareerStage.WORKING, onboarding=True).pack(), None)
    await chat.tap(keyboards.PageCb(index=0).pack(), card_markup)
    await chat.tap(keyboards.NavCb(to=keyboards.Nav.PROFILE).pack(), card_markup)

    assert chat.alerts()[-5:] == [texts.PRESS_START] * 5
    assert rows(session, ChannelAccountRecord) == 0
    assert rows(session, UserProfileRecord) == 0
    assert rows(session, BehaviourEventRecord) == 0


async def test_similar_of_an_old_card_after_a_new_start_says_the_data_is_gone(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1")
    await chat.say("/start")

    await chat.tap(SimilarCb(opportunity_id=first, index=0).pack(), None)

    assert chat.alerts()[-1] == texts.GONE
    assert telegram.sent(AnswerCallbackQuery)[-1].show_alert


async def test_a_group_opens_through_similar_and_comes_back_to_the_same_card(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    first = store(session, "1", employer="Acme", title="Chef de Partie")
    second = store(session, "2", employer="Acme", title="Chef de Partie")
    await onboard(chat)

    delivery = session.execute(select(DeliveryRecord)).scalar_one()
    assert {delivery.opportunity_id, *delivery.similar_ids} == {first, second}
    assert texts.MORE_SIMILAR.format(count=1) in chat.screen_text()
    card = chat.screen_text()

    await chat.press(texts.SIMILAR)

    assert texts.SIMILAR_HEADER in chat.screen_text()
    assert chat.screen_text().count("<a href=") == 1
    assert chat.labels() == [texts.SIMILAR_BACK, texts.BACK_TO_MENU]
    assert len(telegram.sent(SendMessage)) == 1

    await chat.press(texts.SIMILAR_BACK)

    assert chat.screen_text() == card


async def test_a_stage_changed_from_the_profile_returns_to_the_profile_and_sends_no_batch(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat, ENTRY)
    await chat.press(texts.SKIP)
    await chat.press(texts.BACK_TO_MENU)
    await chat.press(texts.MENU_PROFILE)
    assert texts.STAGE_NAMES[CareerStage.ENTRY] in chat.screen_text()

    await chat.press(texts.PROFILE_CHANGE_STAGE)
    assert chat.screen_text() == texts.STAGE_QUESTION
    await chat.press(WORKING)

    assert chat.screen_text().startswith(texts.PROFILE_HEADER)
    assert texts.STAGE_NAMES[CareerStage.WORKING] in chat.screen_text()
    assert rows(session, DeliveryRecord) == 1
    assert len(telegram.sent(SendMessage)) == 1


async def test_changing_directions_and_country_from_the_profile_returns_to_it(
    chat: Chatter, session: Session, service: SeaWorkService
) -> None:
    store(session, "1", countries=["GB"])
    await onboard(chat)
    await chat.press(texts.BACK_TO_MENU)
    await chat.press(texts.MENU_PROFILE)
    assert len(chat.screen_markup().inline_keyboard) == 4

    await chat.press(texts.PROFILE_CHANGE_DIRECTIONS)
    assert chat.screen_text() == texts.DIRECTIONS_QUESTION
    await chat.press(CRUISE)
    await chat.press(texts.DONE)
    assert chat.screen_text().startswith(texts.PROFILE_HEADER)
    assert CRUISE in chat.screen_text()

    await chat.press(texts.PROFILE_CHANGE_COUNTRY)
    assert chat.screen_text() == texts.COUNTRY_QUESTION
    assert [b for b in chat.labels() if b not in {texts.DONE, texts.SKIP_COUNTRY}] == [
        "United Kingdom"
    ]
    await chat.press("United Kingdom")
    await chat.press(texts.DONE)
    assert chat.screen_text().startswith(texts.PROFILE_HEADER)
    assert "United Kingdom" in chat.screen_text()
    stored = service.export(_user(service))
    assert stored is not None
    assert stored.profile.hiring_country is not None
    assert stored.profile.hiring_country.value == ["GB"]


async def test_the_card_carries_the_emoji_of_its_fields(chat: Chatter, session: Session) -> None:
    store(session, "1", employer="Acme", countries=["GB"])

    await onboard(chat)

    text = chat.screen_text()
    assert "🏢 Acme" in text
    assert "🌍 Офис найма: United Kingdom" in text


async def test_every_button_edits_the_message_and_none_sends_a_new_one(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    for number in range(2):
        store(session, str(number), countries=["GB"])
    store(session, "g1", employer="Acme", title="Chef", countries=["GB"])
    store(session, "g2", employer="Acme", title="Chef", countries=["GB"])
    await onboard(chat)
    path = [
        texts.SIMILAR,
        texts.SIMILAR_BACK,
        texts.NEXT,
        texts.INTERESTING,
        texts.NEXT,
        texts.NOT_FOR_ME,
        texts.TAKE_BACK,
        texts.PREVIOUS,
        texts.BACK_TO_MENU,
        texts.MENU_FEED,
        texts.BACK_TO_MENU,
        texts.MENU_PROFILE,
        texts.PROFILE_CHANGE_STAGE,
        WORKING,
        texts.PROFILE_CHANGE_DIRECTIONS,
        CRUISE,
        texts.DONE,
        texts.PROFILE_CHANGE_COUNTRY,
        texts.SKIP_COUNTRY,
        texts.BACK_TO_MENU,
        texts.MENU_NOTIFY_ON,
        texts.MENU_NOTIFY_OFF,
        texts.MENU_DATA,
        texts.DATA_DELETE,
        texts.DELETE_NO,
        texts.BACK_TO_MENU,
        texts.MENU_HELP,
        texts.BACK_TO_MENU,
    ]
    sent = len(telegram.sent(SendMessage))

    for label in path:
        edited = len(telegram.sent(EditMessageText)) + len(telegram.sent(EditMessageReplyMarkup))
        await chat.press(label)
        assert len(telegram.sent(SendMessage)) == sent, label
        assert (
            len(telegram.sent(EditMessageText)) + len(telegram.sent(EditMessageReplyMarkup))
            == edited + 1
        ), label


async def test_commands_open_their_screen_as_a_new_message(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)

    for command, expected in (
        ("/feed", header(1, 1)),
        ("/profile", texts.PROFILE_HEADER),
        ("/help", texts.HELP),
        ("/delete", texts.DELETE_CONFIRM),
    ):
        sent = len(telegram.sent(SendMessage))
        await chat.say(command)
        assert len(telegram.sent(SendMessage)) == sent + 1
        assert chat.screen_text().startswith(expected)


async def test_start_of_a_known_person_opens_the_menu(chat: Chatter, telegram: Telegram) -> None:
    await onboard(chat)

    await chat.say("/start")

    assert chat.screen_text() == texts.MENU
    assert len(chat.screen_markup().inline_keyboard) == 5


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


async def test_commands_after_delete_create_nobody(
    chat: Chatter, telegram: Telegram, session: Session
) -> None:
    store(session, "1")
    await onboard(chat)
    await chat.say("/delete")
    await chat.press(texts.DELETE_YES)

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


async def test_a_dead_button_gets_an_answer(chat: Chatter) -> None:
    await chat.tap("garbage", None)

    assert chat.alerts() == [texts.STALE_BUTTON]


async def test_buttons_of_other_chats_than_private_are_not_served(
    chat: Chatter, telegram: Telegram
) -> None:
    await chat.tap("garbage", None, chat=-5)

    assert telegram.sent(AnswerCallbackQuery) == []


def _events(session: Session) -> list[BehaviourEvent]:
    session.rollback()
    records = session.execute(
        select(BehaviourEventRecord).order_by(BehaviourEventRecord.id)
    ).scalars()
    return [
        BehaviourEvent(
            user_id=r.user_id,
            opportunity_id=r.opportunity_id,
            action=BehaviourAction(r.action),
            occurred_at=r.occurred_at,
        )
        for r in records
    ]
