"""The morning notice: one message to each person who has something new, nothing otherwise.

Contract: docs/modules/telegram-bot.md, 12.4 and 7. The notice records nothing about the
vacancies: a card counts as delivered when it is on the screen. What is recorded here is
only that today's notice went out (`digest_notified_at`), which is what makes a second run
of the day silent.
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from seawork.app import SeaWorkService
from seawork.bot import texts
from seawork.bot.handlers import CHANNEL
from seawork.bot.keyboards import Nav, NavCb

# Telegram allows about 30 messages a second to different chats; a person gets up to two.
RECIPIENT_INTERVAL_SECONDS = 0.1

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]


@dataclass
class DigestResult:
    notified: int = 0
    blocked: int = 0
    failures: int = 0


async def send_digest(
    bot: Bot, service: SeaWorkService, now: datetime, *, sleep: Sleep = asyncio.sleep
) -> DigestResult:
    result = DigestResult()
    recipients = await asyncio.to_thread(service.digest_recipients, CHANNEL)
    for external_id, user_id in recipients:
        try:
            try:
                notified = await _notify(bot, service, external_id, user_id, now)
            except TelegramRetryAfter as flood:
                await sleep(flood.retry_after)
                notified = await _notify(bot, service, external_id, user_id, now)
            if notified:
                result.notified += 1
                await sleep(RECIPIENT_INTERVAL_SECONDS)
        except TelegramForbiddenError:
            await asyncio.to_thread(service.mark_blocked, CHANNEL, external_id, now)
            result.blocked += 1
        # The border of a background task: one person's failure must not cost the others
        # their morning, and it is reported through the exit code.
        except Exception:
            logger.exception("Digest failed for %s", external_id)
            result.failures += 1
    return result


async def _notify(
    bot: Bot, service: SeaWorkService, external_id: str, user_id: uuid.UUID, now: datetime
) -> bool:
    """Send first, record after (3.4): a failure in between repeats the notice, never loses it."""
    if await asyncio.to_thread(service.digest_notified_today, user_id, now):
        return False
    count = await asyncio.to_thread(service.digest_count, user_id, now)
    if count == 0:
        return False
    await bot.send_message(
        int(external_id),
        texts.digest_notice(count),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.DIGEST_OPEN, callback_data=NavCb(to=Nav.FEED).pack()
                    )
                ]
            ]
        ),
    )
    await asyncio.to_thread(service.mark_digest_notified, user_id, now)
    return True
