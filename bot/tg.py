"""Best-effort вызовы Telegram и повтор при 429."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

log = logging.getLogger(__name__)


def not_modified(e: TelegramAPIError) -> bool:
    return isinstance(e, TelegramBadRequest) and "message is not modified" in e.message.lower()


async def answer(cq: CallbackQuery, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await cq.answer(text, show_alert=True if alert else None)
    except TelegramAPIError as e:
        log.info("ответ на кнопку не ушёл: %s", type(e).__name__)


async def edit_text(bot: Bot, chat_id: int, message_id: int, text: str,
                    markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        return True
    except TelegramAPIError as e:
        if not_modified(e):
            return True
        log.debug("правка %s/%s не вышла: %s", chat_id, message_id, type(e).__name__)
        return False


async def strip_markup(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError as e:
        log.debug("кнопки %s/%s не убрались: %s", chat_id, message_id, type(e).__name__)


class RetryAfter(BaseRequestMiddleware):
    """429: ждём, сколько просит Telegram, и повторяем. Слишком долгое ожидание не тянем."""

    def __init__(self, attempts: int = 2, max_wait: float = 30,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.attempts = attempts
        self.max_wait = max_wait
        self.sleep = sleep

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        attempt = 0
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt >= self.attempts or e.retry_after > self.max_wait:
                    raise
                attempt += 1
                log.warning("429 на %s, жду %s с", method.__api_method__, e.retry_after)
                await self.sleep(e.retry_after)


def setup_session(bot: Bot, **retry: Any) -> None:
    bot.session.middleware(RetryAfter(**retry))
