"""Inline keyboards and the typed callback data they carry.

Telegram allows 64 bytes of callback data, so each payload is a short prefix and a
key (`r:42:s`), never JSON. A picker keeps its selection in the keyboard itself (a
check mark on the button), which leaves the bot with no per-chat state to lose on
restart.
"""

from collections.abc import Mapping
from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from seawork.app.cards import Card
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


class CountryCb(CallbackData, prefix="c"):
    key: str


class ReactCb(CallbackData, prefix="r"):
    opportunity_id: int
    reaction: Reaction


class SimilarCb(CallbackData, prefix="sim"):
    opportunity_id: int


class Change(StrEnum):
    STAGE = "stage"
    DIRECTIONS = "directions"
    COUNTRY = "country"


class ChangeCb(CallbackData, prefix="p"):
    what: Change


class DeleteCb(CallbackData, prefix="del"):
    confirmed: bool


type PickCb = DirectionCb | CountryCb


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
    kind: type[PickCb], options: Mapping[str, str], selected: frozenset[str], skip_label: str
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{texts.CHECK}{label}" if key in selected else label,
            callback_data=kind(key=key).pack(),
        )
        for key, label in options.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(text=texts.DONE, callback_data=kind(key=DONE).pack()),
            InlineKeyboardButton(text=skip_label, callback_data=kind(key=SKIP).pack()),
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


def card_keyboard(card: Card) -> InlineKeyboardMarkup:
    rows = [[_open(str(card.url))], _reactions(card.opportunity_id)]
    if card.similar_count:
        rows.append([_similar(card.opportunity_id, card.similar_count)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reacted(
    markup: InlineKeyboardMarkup, opportunity_id: int, reaction: Reaction
) -> InlineKeyboardMarkup:
    """The same card with its reaction row replaced by what the reaction did."""
    kept = [row for row in markup.inline_keyboard if not _is_reaction_row(row)]
    match reaction:
        case Reaction.REOPEN:
            row = _reactions(opportunity_id)
        case Reaction.SAVE:
            row = [_note(texts.SAVED)]
        case Reaction.DISMISS:
            row = [
                _note(texts.DISMISSED),
                InlineKeyboardButton(
                    text=texts.TAKE_BACK,
                    callback_data=ReactCb(
                        opportunity_id=opportunity_id, reaction=Reaction.REOPEN
                    ).pack(),
                ),
            ]
    kept.insert(1, row)
    return InlineKeyboardMarkup(inline_keyboard=kept)


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=ChangeCb(what=what).pack())]
            for what, label in (
                (Change.STAGE, texts.PROFILE_CHANGE_STAGE),
                (Change.DIRECTIONS, texts.PROFILE_CHANGE_DIRECTIONS),
                (Change.COUNTRY, texts.PROFILE_CHANGE_COUNTRY),
            )
        ]
    )


def delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.DELETE_YES, callback_data=DeleteCb(confirmed=True).pack()
                ),
                InlineKeyboardButton(
                    text=texts.DELETE_NO, callback_data=DeleteCb(confirmed=False).pack()
                ),
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


def _open(url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=texts.OPEN_VACANCY, url=url)


def _reactions(opportunity_id: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=texts.INTERESTING,
            callback_data=ReactCb(opportunity_id=opportunity_id, reaction=Reaction.SAVE).pack(),
        ),
        InlineKeyboardButton(
            text=texts.NOT_FOR_ME,
            callback_data=ReactCb(opportunity_id=opportunity_id, reaction=Reaction.DISMISS).pack(),
        ),
    ]


def _similar(opportunity_id: int, count: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=texts.SIMILAR.format(count=count),
        callback_data=SimilarCb(opportunity_id=opportunity_id).pack(),
    )


def _note(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=NOTE)


def _is_reaction_row(row: list[InlineKeyboardButton]) -> bool:
    return any(
        button.callback_data is not None
        and (
            button.callback_data == NOTE
            or button.callback_data.startswith(f"{ReactCb.__prefix__}:")
        )
        for button in row
    )
