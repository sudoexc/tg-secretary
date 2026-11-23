"""Запуск: python -m bot.main. На импорте в сеть не ходит."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from . import config, handlers, panel, scheduler, texts, tg
from .app import App
from .llm import build_llm, build_stt
from .logs import install_redaction, log_exc
from .storage import Repo

log = logging.getLogger("kotib")

ALLOWED_UPDATES = ["message", "callback_query", "business_connection", "business_message",
                   "edited_business_message", "deleted_business_messages"]
# Архив личных переписок: файлы только владельцу процесса (база, WAL, медиа, kb 0600, каталоги 0700).
UMASK = 0o077
# Остановка: апдейты в обработке ждём столько, потом отложенный ответ в работе. Вместе меньше
# stop_grace_period в compose (30 с), иначе Docker убьёт процесс раньше.
HANDLERS_SHUTDOWN = 10
WORKER_SHUTDOWN = 15


def default_properties() -> DefaultBotProperties:
    return DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)


async def on_error(event: ErrorEvent) -> bool:
    log_exc(log, "ошибка при обработке апдейта %s", event.exception, event.update.update_id)
    cq = event.update.callback_query
    if cq is not None:
        with contextlib.suppress(TelegramAPIError):
            await cq.answer("Что-то пошло не так, попробуй ещё раз")
    return True


def build_dispatcher(app: App, *, catch_errors: bool = True) -> Dispatcher:
    dp = Dispatcher()
    dp["app"] = app
    dp.include_router(handlers.build_router())
    dp.include_router(panel.build_router())
    if catch_errors:
        dp.errors.register(on_error)
    return dp


async def setup_profile(bot: Bot) -> None:
    try:
        await bot.set_my_description(description=texts.DESCRIPTION)
        await bot.set_my_short_description(short_description=texts.SHORT_DESCRIPTION)
    except TelegramAPIError as e:
        log.warning("описание не установилось: %s", type(e).__name__)


# Чужие логгеры, которые могут записать тексты: aiosqlite на DEBUG пишет SQL с параметрами,
# httpx/SDK на INFO пишут URL запросов (в URL Bot API токен). Им только WARNING и выше.
QUIET_LOGGERS = ("aiosqlite", "httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "openai", "aiogram.event")


def quiet_loggers() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def private_files() -> int:
    """Новые файлы и каталоги только для своего пользователя. Возвращает прежнюю маску."""
    return os.umask(UMASK)


async def drop_webhook(bot: Bot) -> None:
    """Если на токене когда-то стоял webhook, getUpdates отвечает 409 и поллинг молча стоит.
    Очередь не сбрасываем: правки и удаления за время простоя шпиону нужны."""
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except TelegramAPIError as e:
        log.warning("deleteWebhook: %s", type(e).__name__)


async def stop_worker(worker: asyncio.Task[None], stop: asyncio.Event, timeout: float) -> None:
    """Мягко: новые ответы не берём, текущий даём закончить. Не успел: отмена, строка вернётся после рестарта."""
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout)
    except TimeoutError:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


async def main() -> None:
    private_files()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_loggers()
    install_redaction()
    s = config.load()
    repo = await Repo.open(s.db_path)
    bot = Bot(s.bot_token, session=tg.SafeSession(), default=default_properties())
    tg.setup_session(bot)
    poll = tg.PollWatch()
    bot.session.middleware(poll)
    llm = build_llm(s)
    stt = build_stt(s)
    app = App(s, repo, bot, llm=llm, stt=stt)
    await app.load()
    await repo.requeue_running(app.now())
    dp = build_dispatcher(app)
    await setup_profile(bot)
    await panel.setup_commands(app)

    me = await bot.get_me()
    log.info("@%s запущен: владелец %s, ИИ %s/%s%s, STT %s", me.username, app.owner_id or "не задан",
             s.llm_provider, s.llm_model, "" if llm else " (нет ключа)", stt.model if stt else "нет")
    await scheduler.startup_notice(app)
    await drop_webhook(bot)
    stop = asyncio.Event()
    worker = asyncio.create_task(scheduler.run(app, stop=stop, ready=poll.caught_up), name="pending")
    try:
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=True,
                               close_bot_session=False, tasks_shutdown_timeout=HANDLERS_SHUTDOWN)
    finally:
        await stop_worker(worker, stop, WORKER_SHUTDOWN)
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
