"""Апдейты Telegram Business: подключение, сообщения, правки, удаления. Каждый в своём try/except."""
from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Router
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from . import secretary, spy
from .app import App
from .logs import log_exc
from .panel import setup_commands

log = logging.getLogger(__name__)


def safe(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Одно битое обновление не роняет поллинг: в лог id и тип ошибки, дальше работаем."""

    @functools.wraps(fn)
    async def wrapper(event: Any, app: App, **kw: Any) -> Any:
        try:
            return await fn(event, app)
        except Exception as e:
            log_exc(log, "ошибка в %s", e, fn.__name__)
            if app.raise_errors:
                raise
            return None

    return wrapper


@safe
async def on_connection(bc: BusinessConnection, app: App) -> None:
    had_owner = app.owner_id is not None
    conn = await app.accept_connection(bc)
    if conn is not None and not had_owner:
        await setup_commands(app)


@safe
async def on_business_message(message: Message, app: App) -> None:
    had_owner = app.owner_id is not None
    # Замок берётся до первого await: апдейты чата обрабатываются в порядке пачки getUpdates.
    async with app.archive_lock(message.chat.id):
        inc = await spy.on_message(app, message)
        if inc is None:
            return
        if inc.is_owner:
            # Наш же ответ, автоответ Telegram или отложенное сообщение не считаются ручным ответом.
            if not inc.bot_sent and not inc.from_offline:
                await secretary.on_owner_message(app, inc)
        else:
            await secretary.on_client_message(app, inc)
    if not had_owner:
        await setup_commands(app)


@safe
async def on_edited(message: Message, app: App) -> None:
    async with app.archive_lock(message.chat.id):
        await spy.on_edit(app, message)


@safe
async def on_deleted(event: BusinessMessagesDeleted, app: App) -> None:
    async with app.archive_lock(event.chat.id):
        await spy.on_deleted(app, event)


def build_router() -> Router:
    r = Router(name="business")
    r.business_connection.register(on_connection)
    r.business_message.register(on_business_message)
    r.edited_business_message.register(on_edited)
    r.deleted_business_messages.register(on_deleted)
    return r
