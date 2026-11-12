"""Wiring: settings to service, router to dispatcher, long polling."""

import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, LinkPreviewOptions
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from seawork.app import Names, SeaWorkService
from seawork.bot import texts
from seawork.bot.delivery import DigestResult, send_digest
from seawork.bot.handlers import Clock, build_router
from seawork.config import Settings
from seawork.storage.profile_vocabulary import ProfileVocabulary


def _utcnow() -> datetime:
    return datetime.now(UTC)


COMMANDS = [
    BotCommand(command="feed", description=texts.COMMAND_FEED),
    BotCommand(command="profile", description=texts.COMMAND_PROFILE),
    BotCommand(command="pause", description=texts.COMMAND_PAUSE),
    BotCommand(command="resume", description=texts.COMMAND_RESUME),
    BotCommand(command="export", description=texts.COMMAND_EXPORT),
    BotCommand(command="delete", description=texts.COMMAND_DELETE),
    BotCommand(command="help", description=texts.COMMAND_HELP),
]


def build_dispatcher(service: SeaWorkService, clock: Clock = _utcnow) -> Dispatcher:
    dispatcher = Dispatcher(service=service, clock=clock)
    dispatcher.include_router(build_router())
    return dispatcher


def _bot(token: str) -> Bot:
    return Bot(
        token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )


def _service(settings: Settings, engine: Engine) -> SeaWorkService:
    return SeaWorkService(
        sessionmaker(bind=engine, class_=Session),
        ProfileVocabulary.from_directory(settings.reference_dir),
        Names.load(settings.reference_dir),
    )


async def run(settings: Settings, token: str) -> None:
    logging.basicConfig(level=logging.INFO)
    engine = create_engine(settings.database_url)
    try:
        dispatcher = build_dispatcher(_service(settings, engine))
        async with _bot(token) as bot:
            await bot.set_my_commands(COMMANDS)
            # aiogram types its UNSET sentinel as Any, which strict mode reads as Unknown.
            await dispatcher.start_polling(bot)  # pyright: ignore[reportUnknownMemberType]
    finally:
        engine.dispose()


async def run_digest(settings: Settings, token: str) -> DigestResult:
    engine = create_engine(settings.database_url)
    try:
        async with _bot(token) as bot:
            return await send_digest(bot, _service(settings, engine), _utcnow())
    finally:
        engine.dispose()
