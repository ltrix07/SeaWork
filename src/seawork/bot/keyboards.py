"""Inline keyboards and the typed callback data they carry.

Telegram allows 64 bytes of callback data, so each payload is a short prefix and a
key (`r:42:s:3`), never JSON. A picker keeps its selection in the keyboard itself (a
check mark on the button), and the carousel carries its page number in every button,
which leaves the bot with no per-chat state to lose on restart.
"""

from collections.abc import Mapping
from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import Field

from seawork.app import FeedPage, ReactionState
from seawork.bot import texts
from seawork.domain.profile import BehaviourAction, CareerStage

NOTE = "noop"
DONE = "ok"
SKIP = "skip"


class Reaction(StrEnum):
    SAVE = "s"
    DISMISS = "d"
    REOPEN = "o"

    @property
    def action(self) -> BehaviourAction:
        return _ACTIONS[self]


_ACTIONS = {
    Reaction.SAVE: BehaviourAction.SAVED,
    Reaction.DISMISS: BehaviourAction.DISMISSED,
    Reaction.REOPEN: BehaviourAction.REOPENED,
}


class StageCb(CallbackData, prefix="st"):
    stage: CareerStage
    onboarding: bool


class DirectionCb(CallbackData, prefix="d"):
    key: str
    onboarding: bool


class CountryCb(CallbackData, prefix="c"):
    key: str
    onboarding: bool


class PageCb(CallbackData, prefix="p"):
    index: int = Field(ge=0)


class ReactCb(CallbackData, prefix="r"):
    opportunity_id: int
    reaction: Reaction
    index: int = Field(ge=0)


class SimilarCb(CallbackData, prefix="sim"):
    opportunity_id: int
    index: int = Field(ge=0)


class Nav(StrEnum):
    FEED = "feed"
    MENU = "menu"
    PROFILE = "profile"
    NOTIFY = "notify"
    DATA = "data"
    HELP = "help"
    EXPORT = "export"
    DELETE = "delete"


class NavCb(CallbackData, prefix="nav"):
    to: Nav


class Change(StrEnum):
    STAGE = "stage"
    DIRECTIONS = "directions"
    COUNTRY = "country"


class ChangeCb(CallbackData, prefix="chg"):
    what: Change


class DeleteCb(CallbackData, prefix="del"):
    confirmed: bool


type PickCb = DirectionCb | CountryCb


def menu_keyboard(*, notifications_on: bool) -> InlineKeyboardMarkup:
    notify = texts.MENU_NOTIFY_ON if notifications_on else texts.MENU_NOTIFY_OFF
    return _column(
        _button(texts.MENU_FEED, NavCb(to=Nav.FEED).pack()),
        _button(texts.MENU_PROFILE, NavCb(to=Nav.PROFILE).pack()),
        _button(notify, NavCb(to=Nav.NOTIFY).pack()),
        _button(texts.MENU_DATA, NavCb(to=Nav.DATA).pack()),
        _button(texts.MENU_HELP, NavCb(to=Nav.HELP).pack()),
    )


def menu_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_menu_row()])


def stage_keyboard(*, onboarding: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=StageCb(stage=stage, onboarding=onboarding).pack()
                )
            ]
            for stage, label in texts.STAGE_BUTTONS.items()
        ]
    )


def picker_keyboard(
    kind: type[PickCb],
    options: Mapping[str, str],
    selected: frozenset[str],
    skip_label: str,
    *,
    onboarding: bool,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{texts.CHECK}{label}" if key in selected else label,
            callback_data=kind(key=key, onboarding=onboarding).pack(),
        )
        for key, label in options.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            _button(texts.DONE, kind(key=DONE, onboarding=onboarding).pack()),
            _button(skip_label, kind(key=SKIP, onboarding=onboarding).pack()),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def picked(markup: InlineKeyboardMarkup, kind: type[PickCb]) -> frozenset[str]:
    return frozenset(
        kind.unpack(button.callback_data).key
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None and button.text.startswith(texts.CHECK)
    )


def with_toggled(
    markup: InlineKeyboardMarkup, kind: type[PickCb], key: str
) -> InlineKeyboardMarkup:
    current = picked(markup, kind)
    selected = current - {key} if key in current else current | {key}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_checked(button, kind, selected) for button in row] for row in markup.inline_keyboard
        ]
    )


def page_keyboard(page: FeedPage) -> InlineKeyboardMarkup:
    previous = (
        [_button(texts.PREVIOUS, PageCb(index=page.index - 1).pack())] if page.has_prev else []
    )
    following = [_button(texts.NEXT, PageCb(index=page.index + 1).pack())] if page.has_next else []
    card = page.card
    if card is None:
        return InlineKeyboardMarkup(inline_keyboard=[row for row in (previous, _menu_row()) if row])
    similar = (
        [
            _button(
                texts.SIMILAR,
                SimilarCb(opportunity_id=card.opportunity_id, index=page.index).pack(),
            )
        ]
        if card.similar_count
        else []
    )
    rows = [
        [InlineKeyboardButton(text=texts.OPEN_VACANCY, url=str(card.url))],
        _reaction_row(card.opportunity_id, page.index, page.reaction),
        [*previous, *similar, *following],
        _menu_row(),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def similar_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(texts.SIMILAR_BACK, PageCb(index=index).pack())], _menu_row()]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(label, ChangeCb(what=what).pack())]
            for what, label in (
                (Change.STAGE, texts.PROFILE_CHANGE_STAGE),
                (Change.DIRECTIONS, texts.PROFILE_CHANGE_DIRECTIONS),
                (Change.COUNTRY, texts.PROFILE_CHANGE_COUNTRY),
            )
        ]
        + [_menu_row()]
    )


def data_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(texts.DATA_EXPORT, NavCb(to=Nav.EXPORT).pack())],
            [_button(texts.DATA_DELETE, NavCb(to=Nav.DELETE).pack())],
            _menu_row(),
        ]
    )


def delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(texts.DELETE_YES, DeleteCb(confirmed=True).pack()),
                _button(texts.DELETE_NO, DeleteCb(confirmed=False).pack()),
            ]
        ]
    )


def _checked(
    button: InlineKeyboardButton, kind: type[PickCb], selected: frozenset[str]
) -> InlineKeyboardButton:
    if button.callback_data is None:
        return button
    key = kind.unpack(button.callback_data).key
    if key in {DONE, SKIP}:
        return button
    label = button.text.removeprefix(texts.CHECK)
    return button.model_copy(update={"text": f"{texts.CHECK}{label}" if key in selected else label})


def _reaction_row(
    opportunity_id: int, index: int, state: ReactionState
) -> list[InlineKeyboardButton]:
    match state:
        case ReactionState.NONE:
            return [
                _button(
                    texts.INTERESTING,
                    ReactCb(
                        opportunity_id=opportunity_id, reaction=Reaction.SAVE, index=index
                    ).pack(),
                ),
                _button(
                    texts.NOT_FOR_ME,
                    ReactCb(
                        opportunity_id=opportunity_id, reaction=Reaction.DISMISS, index=index
                    ).pack(),
                ),
            ]
        case ReactionState.SAVED:
            return [_button(texts.SAVED, NOTE)]
        case ReactionState.DISMISSED:
            return [
                _button(texts.DISMISSED, NOTE),
                _button(
                    texts.TAKE_BACK,
                    ReactCb(
                        opportunity_id=opportunity_id, reaction=Reaction.REOPEN, index=index
                    ).pack(),
                ),
            ]


def _menu_row() -> list[InlineKeyboardButton]:
    return [_button(texts.BACK_TO_MENU, NavCb(to=Nav.MENU).pack())]


def _column(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)
