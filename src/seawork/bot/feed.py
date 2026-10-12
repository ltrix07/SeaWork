"""Sending a prepared batch and recording what was actually sent (contract 3.4, 5.1)."""

import asyncio
import logging
import uuid
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from seawork.app import Names, SeaWorkService
from seawork.bot.keyboards import card_keyboard
from seawork.bot.render import render_card
from seawork.domain.recommendation import Recommendation

CHANNEL = "telegram"

logger = logging.getLogger(__name__)


async def send_batch(
    bot: Bot,
    chat_id: int,
    service: SeaWorkService,
    names: Names,
    user_id: uuid.UUID,
    now: datetime,
) -> int:
    """Returns how many cards the person received.

    A card Telegram rejects is logged and recorded as delivered: left unrecorded it would
    head every later batch and block the ones behind it for good. Network failures and
    `Forbidden` propagate, and only what was sent before them is recorded.
    """
    prepared = await asyncio.to_thread(service.prepare_batch, user_id, now)
    handled: list[Recommendation] = []
    received = 0
    try:
        for item in prepared:
            try:
                await bot.send_message(
                    chat_id, render_card(item.card, names), reply_markup=card_keyboard(item.card)
                )
                received += 1
            except TelegramBadRequest:
                logger.exception("Telegram rejected card %s", item.card.opportunity_id)
            handled.append(item.recommendation)
    finally:
        if handled:
            await asyncio.to_thread(service.confirm_delivered, user_id, handled, CHANNEL, now)
    return received
