"""Thin handlers: a tap becomes a call of `SeaWorkService`, its answer becomes a screen.

Nothing here decides what the person should see. The only branching is on what the
person just pressed or what a use case returned. The bot lives in one message: a button
edits it, a command opens a screen as a new message.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from seawork.app import SeaWorkService
from seawork.bot import keyboards, texts
from seawork.bot.keyboards import (
    ChangeCb,
    CountryCb,
    DeleteCb,
    DirectionCb,
    Nav,
    NavCb,
    PageCb,
    PickCb,
    ReactCb,
    SimilarCb,
    StageCb,
)
from seawork.bot.render import render_feed, render_profile, render_similar
from seawork.domain.profile import UserDataExport

CHANNEL = "telegram"

type Clock = Callable[[], datetime]
type Origin = Message | CallbackQuery

logger = logging.getLogger(__name__)

_UNCHANGED = "message is not modified"
_UNEDITABLE = ("message can't be edited", "message to edit not found")
# What Telegram says when it is the card that cannot be shown, not the chat or the message.
_REFUSED_CARD = (
    "can't parse entities",
    "message is too long",
    "button_url_invalid",
    "wrong http url",
)


def build_router() -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.message.register(start, CommandStart())
    router.message.register(feed, Command("feed"))
    router.message.register(profile, Command("profile"))
    router.message.register(pause, Command("pause"))
    router.message.register(resume, Command("resume"))
    router.message.register(export, Command("export"))
    router.message.register(delete, Command("delete"))
    router.message.register(help_, Command("help"))
    router.callback_query.register(navigate, NavCb.filter())
    router.callback_query.register(page, PageCb.filter())
    router.callback_query.register(answer_stage, StageCb.filter())
    router.callback_query.register(pick_direction, DirectionCb.filter())
    router.callback_query.register(pick_country, CountryCb.filter())
    router.callback_query.register(react, ReactCb.filter())
    router.callback_query.register(similar, SimilarCb.filter())
    router.callback_query.register(change, ChangeCb.filter())
    router.callback_query.register(confirm_delete, DeleteCb.filter())
    router.callback_query.register(note, F.data == keyboards.NOTE)
    router.callback_query.register(stale)
    return router


async def start(message: Message, bot: Bot, service: SeaWorkService) -> None:
    result = await asyncio.to_thread(service.start, CHANNEL, str(message.chat.id))
    if result.stage_known:
        await _menu(message, bot, service, result.user_id)
    else:
        await _show(message, bot, texts.ONBOARDING, keyboards.stage_keyboard(onboarding=True))


async def feed(message: Message, bot: Bot, service: SeaWorkService, clock: Clock) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await _feed(message, bot, service, user_id, 0, clock(), entering=True)


async def profile(message: Message, bot: Bot, service: SeaWorkService) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await _profile(message, bot, service, user_id)


async def pause(message: Message, service: SeaWorkService, clock: Clock) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await asyncio.to_thread(service.pause, user_id, clock())
        await message.answer(texts.PAUSED)


async def resume(message: Message, service: SeaWorkService) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await asyncio.to_thread(service.resume, user_id)
        await message.answer(texts.RESUMED)


async def export(message: Message, bot: Bot, service: SeaWorkService) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await _send_export(bot, message.chat.id, service, user_id)


async def delete(message: Message, bot: Bot) -> None:
    await _show(message, bot, texts.DELETE_CONFIRM, keyboards.delete_keyboard())


async def help_(message: Message, bot: Bot) -> None:
    await _show(message, bot, texts.HELP, keyboards.menu_only_keyboard())


async def navigate(
    callback: CallbackQuery,
    callback_data: NavCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await callback.answer()
    match callback_data.to:
        case Nav.MENU:
            await _menu(callback, bot, service, user_id)
        case Nav.PROFILE:
            await _profile(callback, bot, service, user_id)
        case Nav.FEED:
            await _feed(callback, bot, service, user_id, 0, clock(), entering=True)
        case Nav.NOTIFY:
            on = await asyncio.to_thread(service.toggle_notifications, user_id, clock())
            await _show_menu(callback, bot, on=on)
        case Nav.DATA:
            await _show(callback, bot, texts.DATA, keyboards.data_keyboard())
        case Nav.HELP:
            await _show(callback, bot, texts.HELP, keyboards.menu_only_keyboard())
        case Nav.EXPORT:
            await _send_export(bot, _chat_id(callback), service, user_id)
        case Nav.DELETE:
            await _show(callback, bot, texts.DELETE_CONFIRM, keyboards.delete_keyboard())


async def page(
    callback: CallbackQuery,
    callback_data: PageCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await callback.answer()
    await _feed(callback, bot, service, user_id, callback_data.index, clock())


async def answer_stage(
    callback: CallbackQuery,
    callback_data: StageCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    answered = await asyncio.to_thread(
        service.answer_stage,
        user_id,
        callback_data.stage,
        onboarding=callback_data.onboarding,
    )
    await callback.answer()
    if answered.ask_directions:
        await _ask_directions(callback, bot, service, onboarding=True)
    elif answered.show_batch:
        await _feed(callback, bot, service, user_id, 0, clock())
    else:
        await _profile(callback, bot, service, user_id)


async def pick_direction(
    callback: CallbackQuery,
    callback_data: DirectionCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    await _pick(callback, callback_data, bot, service, clock, DirectionCb, service.set_directions)


async def pick_country(
    callback: CallbackQuery,
    callback_data: CountryCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    await _pick(
        callback, callback_data, bot, service, clock, CountryCb, service.set_hiring_countries
    )


async def react(
    callback: CallbackQuery,
    callback_data: ReactCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await asyncio.to_thread(
        service.react,
        user_id,
        callback_data.opportunity_id,
        callback_data.reaction.action,
        clock(),
    )
    await callback.answer(_REACTION_ANSWERS[callback_data.reaction])
    await _feed(callback, bot, service, user_id, callback_data.index, clock())


async def similar(
    callback: CallbackQuery, callback_data: SimilarCb, bot: Bot, service: SeaWorkService
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    try:
        cards = await asyncio.to_thread(service.similar, user_id, callback_data.opportunity_id)
    except LookupError:
        await callback.answer(texts.GONE, show_alert=True)
        return
    await callback.answer()
    await _show(
        callback, bot, render_similar(cards), keyboards.similar_keyboard(callback_data.index)
    )


async def change(
    callback: CallbackQuery, callback_data: ChangeCb, bot: Bot, service: SeaWorkService
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await callback.answer()
    match callback_data.what:
        case keyboards.Change.STAGE:
            await _show(
                callback, bot, texts.STAGE_QUESTION, keyboards.stage_keyboard(onboarding=False)
            )
        case keyboards.Change.DIRECTIONS:
            await _ask_directions(callback, bot, service, onboarding=False)
        case keyboards.Change.COUNTRY:
            await _ask_country(callback, bot, service, onboarding=False)


async def confirm_delete(
    callback: CallbackQuery, callback_data: DeleteCb, bot: Bot, service: SeaWorkService
) -> None:
    if not callback_data.confirmed:
        await callback.answer()
        await _show(callback, bot, texts.DELETE_CANCELLED, keyboards.menu_only_keyboard())
        return
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await asyncio.to_thread(service.delete, user_id)
    await callback.answer()
    await _show(callback, bot, texts.DELETED, None)


async def note(callback: CallbackQuery) -> None:
    await callback.answer()


async def stale(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_BUTTON)


_REACTION_ANSWERS = {
    keyboards.Reaction.SAVE: texts.REACTION_SAVED,
    keyboards.Reaction.DISMISS: texts.REACTION_DISMISSED,
    keyboards.Reaction.REOPEN: texts.REACTION_REOPENED,
}


async def _show(origin: Origin, bot: Bot, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """A command opens the screen as a new message, a button turns the same message into it.

    The only time a button sends instead of editing is a message Telegram no longer lets
    us edit (older than 48 hours, deleted): the person still gets the screen they asked for.
    """
    if isinstance(origin, Message):
        await origin.answer(text, reply_markup=markup)
        return
    message = origin.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as error:
            if _UNCHANGED in error.message:
                return
            if not any(reason in error.message for reason in _UNEDITABLE):
                raise
        else:
            return
    await bot.send_message(_chat_id(origin), text, reply_markup=markup)


async def _menu(origin: Origin, bot: Bot, service: SeaWorkService, user_id: uuid.UUID) -> None:
    on = await asyncio.to_thread(service.notifications_enabled, user_id)
    await _show_menu(origin, bot, on=on)


async def _show_menu(origin: Origin, bot: Bot, *, on: bool) -> None:
    await _show(origin, bot, texts.MENU, keyboards.menu_keyboard(notifications_on=on))


async def _profile(origin: Origin, bot: Bot, service: SeaWorkService, user_id: uuid.UUID) -> None:
    data = await _exported(service, user_id)
    await _show(
        origin, bot, render_profile(data.profile, service.names()), keyboards.profile_keyboard()
    )


async def _feed(
    origin: Origin,
    bot: Bot,
    service: SeaWorkService,
    user_id: uuid.UUID,
    index: int,
    now: datetime,
    *,
    entering: bool = False,
) -> None:
    feed_page = await asyncio.to_thread(service.feed_page, user_id, index, now, entering=entering)
    if feed_page.ask_hiring_country:
        await _ask_country(origin, bot, service, onboarding=True)
        await asyncio.to_thread(service.mark_hiring_country_asked, user_id, now)
        return
    try:
        await _show(
            origin, bot, render_feed(feed_page, service.names()), keyboards.page_keyboard(feed_page)
        )
    except TelegramBadRequest as error:
        if feed_page.fresh is None or not _is_refused_card(error):
            raise
        logger.exception("Telegram refused card %s", feed_page.fresh.opportunity_id)
        # Recorded as refused: never offered again, but neither shown nor counted, so the
        # same position now holds the next card instead of failing on this one forever.
        await asyncio.to_thread(
            service.confirm_delivered, user_id, [feed_page.fresh], CHANNEL, now, rejected=True
        )
        await _feed(origin, bot, service, user_id, index, now)
        return
    if feed_page.fresh is not None:
        await asyncio.to_thread(service.confirm_delivered, user_id, [feed_page.fresh], CHANNEL, now)


def _is_refused_card(error: TelegramBadRequest) -> bool:
    message = error.message.lower()
    return any(reason in message for reason in _REFUSED_CARD)


async def _ask_directions(
    origin: Origin, bot: Bot, service: SeaWorkService, *, onboarding: bool
) -> None:
    await _show(
        origin,
        bot,
        texts.DIRECTIONS_QUESTION,
        keyboards.picker_keyboard(
            DirectionCb, service.names().directions, frozenset(), texts.SKIP, onboarding=onboarding
        ),
    )


async def _ask_country(
    origin: Origin, bot: Bot, service: SeaWorkService, *, onboarding: bool
) -> None:
    countries = service.names().countries
    codes = await asyncio.to_thread(service.hiring_country_options)
    options = {code: countries[code] for code in codes}
    await _show(
        origin,
        bot,
        texts.COUNTRY_QUESTION,
        keyboards.picker_keyboard(
            CountryCb, options, frozenset(), texts.SKIP_COUNTRY, onboarding=onboarding
        ),
    )


async def _pick(
    callback: CallbackQuery,
    choice: PickCb,
    bot: Bot,
    service: SeaWorkService,
    clock: Clock,
    kind: type[PickCb],
    store: Callable[[uuid.UUID, Sequence[str]], None],
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer(texts.STALE_BUTTON)
        return
    if choice.key == keyboards.SKIP:
        await callback.answer()
        await _after_pick(callback, choice, bot, service, user_id, clock())
        return
    markup = cast(InlineKeyboardMarkup, message.reply_markup)
    if choice.key == keyboards.DONE:
        selected = keyboards.picked(markup, kind)
        if not selected:
            await callback.answer(texts.PICK_SOMETHING, show_alert=True)
            return
        await asyncio.to_thread(store, user_id, sorted(selected))
        await callback.answer()
        await _after_pick(callback, choice, bot, service, user_id, clock())
        return
    await callback.answer()
    await message.edit_reply_markup(reply_markup=keyboards.with_toggled(markup, kind, choice.key))


async def _after_pick(
    callback: CallbackQuery,
    choice: PickCb,
    bot: Bot,
    service: SeaWorkService,
    user_id: uuid.UUID,
    now: datetime,
) -> None:
    if choice.onboarding:
        await _feed(callback, bot, service, user_id, 0, now)
    else:
        await _profile(callback, bot, service, user_id)


async def _send_export(bot: Bot, chat_id: int, service: SeaWorkService, user_id: uuid.UUID) -> None:
    data = await _exported(service, user_id)
    document = BufferedInputFile(data.model_dump_json(indent=2).encode(), texts.EXPORT_FILENAME)
    await bot.send_document(chat_id, document)


async def _person(message: Message, service: SeaWorkService) -> uuid.UUID | None:
    user_id = await asyncio.to_thread(service.touch, CHANNEL, str(message.chat.id))
    if user_id is None:
        await message.answer(texts.PRESS_START)
    return user_id


async def _caller(callback: CallbackQuery, service: SeaWorkService) -> uuid.UUID | None:
    user_id = await asyncio.to_thread(service.touch, CHANNEL, str(_chat_id(callback)))
    if user_id is None:
        await callback.answer(texts.PRESS_START, show_alert=True)
    return user_id


async def _exported(service: SeaWorkService, user_id: uuid.UUID) -> UserDataExport:
    data = await asyncio.to_thread(service.export, user_id)
    if data is None:
        raise LookupError(f"No profile for user {user_id}")
    return data


def _chat_id(callback: CallbackQuery) -> int:
    return cast(Message, callback.message).chat.id
