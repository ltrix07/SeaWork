"""Thin handlers: a tap becomes a call of `SeaWorkService`, its answer becomes a message.

Nothing here decides what the person should see. The only branching is on what the
person just pressed or what a use case returned.
"""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

from seawork.app import SeaWorkService
from seawork.bot import keyboards, texts
from seawork.bot.feed import CHANNEL, send_batch
from seawork.bot.keyboards import (
    ChangeCb,
    CountryCb,
    DeleteCb,
    DirectionCb,
    PickCb,
    ReactCb,
    SimilarCb,
    StageCb,
)
from seawork.bot.render import render_profile, render_similar
from seawork.domain.profile import UserDataExport

type Clock = Callable[[], datetime]


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


async def start(message: Message, bot: Bot, service: SeaWorkService, clock: Clock) -> None:
    result = await asyncio.to_thread(service.start, CHANNEL, str(message.chat.id))
    await message.answer(texts.WELCOME)
    if result.stage_known:
        await _feed(message, bot, service, result.user_id, clock())
    else:
        await message.answer(
            texts.STAGE_QUESTION, reply_markup=keyboards.stage_keyboard(onboarding=True)
        )


async def feed(message: Message, bot: Bot, service: SeaWorkService, clock: Clock) -> None:
    user_id = await _person(message, service)
    if user_id is not None:
        await _feed(message, bot, service, user_id, clock())


async def profile(message: Message, service: SeaWorkService) -> None:
    user_id = await _person(message, service)
    if user_id is None:
        return
    data = await _exported(service, user_id)
    await message.answer(
        render_profile(data.profile, service.names()), reply_markup=keyboards.profile_keyboard()
    )


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


async def export(message: Message, service: SeaWorkService) -> None:
    user_id = await _person(message, service)
    if user_id is None:
        return
    data = await _exported(service, user_id)
    document = BufferedInputFile(data.model_dump_json(indent=2).encode(), texts.EXPORT_FILENAME)
    await message.answer_document(document)


async def delete(message: Message) -> None:
    await message.answer(texts.DELETE_CONFIRM, reply_markup=keyboards.delete_keyboard())


async def help_(message: Message) -> None:
    await message.answer(texts.HELP)


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
    message = _message(callback)
    await message.edit_text(texts.PICKED_STAGE.format(stage=texts.STAGE_NAMES[callback_data.stage]))
    if answered.show_batch:
        await _feed(message, bot, service, user_id, clock())
    if answered.ask_directions:
        await _ask_directions(message, service)


async def pick_direction(
    callback: CallbackQuery, callback_data: DirectionCb, service: SeaWorkService
) -> None:
    await _pick(callback, callback_data, service, DirectionCb, service.set_directions)


async def pick_country(
    callback: CallbackQuery, callback_data: CountryCb, service: SeaWorkService
) -> None:
    await _pick(callback, callback_data, service, CountryCb, service.set_hiring_countries)


async def react(
    callback: CallbackQuery, callback_data: ReactCb, service: SeaWorkService, clock: Clock
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
    message = _message(callback)
    await message.edit_reply_markup(
        reply_markup=keyboards.reacted(
            _markup(message), callback_data.opportunity_id, callback_data.reaction
        )
    )


async def similar(
    callback: CallbackQuery, callback_data: SimilarCb, service: SeaWorkService
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
    await _message(callback).answer(render_similar(cards))


async def change(callback: CallbackQuery, callback_data: ChangeCb, service: SeaWorkService) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await callback.answer()
    message = _message(callback)
    match callback_data.what:
        case keyboards.Change.STAGE:
            await message.answer(
                texts.STAGE_QUESTION, reply_markup=keyboards.stage_keyboard(onboarding=False)
            )
        case keyboards.Change.DIRECTIONS:
            await _ask_directions(message, service)
        case keyboards.Change.COUNTRY:
            await _ask_country(message, service)


async def confirm_delete(
    callback: CallbackQuery, callback_data: DeleteCb, service: SeaWorkService
) -> None:
    message = _message(callback)
    if not callback_data.confirmed:
        await callback.answer()
        await message.edit_text(texts.DELETE_CANCELLED)
        return
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    await asyncio.to_thread(service.delete, user_id)
    await callback.answer()
    await message.edit_text(texts.DELETED)


async def note(callback: CallbackQuery) -> None:
    await callback.answer()


async def stale(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_BUTTON)


_REACTION_ANSWERS = {
    keyboards.Reaction.SAVE: texts.REACTION_SAVED,
    keyboards.Reaction.DISMISS: texts.REACTION_DISMISSED,
    keyboards.Reaction.REOPEN: texts.REACTION_REOPENED,
}


async def _feed(
    message: Message, bot: Bot, service: SeaWorkService, user_id: uuid.UUID, now: datetime
) -> None:
    if await asyncio.to_thread(service.should_ask_hiring_country, user_id, now):
        await _ask_country(message, service)
        await asyncio.to_thread(service.mark_hiring_country_asked, user_id, now)
    sent = await send_batch(bot, message.chat.id, service, service.names(), user_id, now)
    if sent == 0:
        await message.answer(texts.EMPTY_FEED)


async def _ask_directions(message: Message, service: SeaWorkService) -> None:
    await message.answer(
        texts.DIRECTIONS_QUESTION,
        reply_markup=keyboards.picker_keyboard(
            DirectionCb, service.names().directions, frozenset(), texts.SKIP
        ),
    )


async def _ask_country(message: Message, service: SeaWorkService) -> None:
    countries = service.names().countries
    codes = await asyncio.to_thread(service.hiring_country_options)
    options = {code: countries[code] for code in codes}
    await message.answer(
        texts.COUNTRY_QUESTION,
        reply_markup=keyboards.picker_keyboard(CountryCb, options, frozenset(), texts.SKIP_COUNTRY),
    )


async def _pick(
    callback: CallbackQuery,
    choice: PickCb,
    service: SeaWorkService,
    kind: type[PickCb],
    store: Callable[[uuid.UUID, Sequence[str]], None],
) -> None:
    user_id = await _caller(callback, service)
    if user_id is None:
        return
    message = _message(callback)
    if choice.key == keyboards.SKIP:
        await callback.answer()
        await message.delete()
        return
    markup = _markup(message)
    if choice.key == keyboards.DONE:
        selected = keyboards.picked(markup, kind)
        if not selected:
            await callback.answer(texts.PICK_SOMETHING, show_alert=True)
            return
        await asyncio.to_thread(store, user_id, sorted(selected))
        await callback.answer()
        await message.edit_text(texts.SAVED_PROFILE)
        return
    await callback.answer()
    await message.edit_reply_markup(reply_markup=keyboards.with_toggled(markup, kind, choice.key))


async def _person(message: Message, service: SeaWorkService) -> uuid.UUID | None:
    user_id = await asyncio.to_thread(service.touch, CHANNEL, str(message.chat.id))
    if user_id is None:
        await message.answer(texts.PRESS_START)
    return user_id


async def _caller(callback: CallbackQuery, service: SeaWorkService) -> uuid.UUID | None:
    chat_id = cast(Message, callback.message).chat.id
    user_id = await asyncio.to_thread(service.touch, CHANNEL, str(chat_id))
    if user_id is None:
        await callback.answer(texts.PRESS_START, show_alert=True)
    return user_id


async def _exported(service: SeaWorkService, user_id: uuid.UUID) -> UserDataExport:
    data = await asyncio.to_thread(service.export, user_id)
    if data is None:
        raise LookupError(f"No profile for user {user_id}")
    return data


def _message(callback: CallbackQuery) -> Message:
    if not isinstance(callback.message, Message):
        raise LookupError("The message of this button is no longer accessible")
    return callback.message


def _markup(message: Message) -> InlineKeyboardMarkup:
    return cast(InlineKeyboardMarkup, message.reply_markup)
