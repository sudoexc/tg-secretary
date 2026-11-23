"""Best-effort вызовы Telegram и повтор при 429."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Update
from pydantic import ValidationError

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


def _clean_updates(bot: Bot, content: str) -> str:
    """Каждый апдейт из getUpdates проверяется отдельно. Тот, что эта версия aiogram не разбирает
    (Telegram добавил новый тип), заменяется пустым Update с тем же update_id: поллинг сдвигает offset
    и идёт дальше, а не повторяет всю пачку с сырым JSON в логе каждые 5 секунд."""
    try:
        data = json.loads(content)
    except ValueError:
        return content
    items = data.get("result") if isinstance(data, dict) and data.get("ok") else None
    if not isinstance(items, list):
        return content
    out: list[Any] = []
    changed = False
    for item in items:
        try:
            Update.model_validate(item, context={"bot": bot})
            out.append(item)
        except ValidationError as e:
            changed = True
            uid = item.get("update_id") if isinstance(item, dict) else None
            first = e.errors(include_url=False, include_context=False, include_input=False)[0]
            log.warning("апдейт %s пропущен: не разобрался (%s at %s)", uid, first["type"], first["loc"])
            if type(uid) is int:
                out.append({"update_id": uid})
    if not changed:
        return content
    data["result"] = out
    return json.dumps(data, ensure_ascii=False)


class SkipBadUpdates:
    """Примесь к сессии aiogram: битый апдейт не останавливает поллинг и не попадает в лог."""

    def check_response(self, bot: Bot, method: TelegramMethod[TelegramType], status_code: int,
                       content: str) -> Response[TelegramType]:
        if isinstance(method, GetUpdates):
            content = _clean_updates(bot, content)
        return super().check_response(bot, method, status_code, content)  # type: ignore[misc]


class SafeSession(SkipBadUpdates, AiohttpSession):
    """Боевая сессия."""


class PollWatch(BaseRequestMiddleware):
    """Отмечает, что поллинг догнал очередь: getUpdates вернул неполную пачку (бэклог разобран)."""

    def __init__(self) -> None:
        self.caught_up = asyncio.Event()

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        result = await make_request(bot, method)  # сессия отдаёт уже result, не обёртку Response
        if isinstance(method, GetUpdates) and not self.caught_up.is_set():
            got = result if isinstance(result, list) else []
            if len(got) < (method.limit or 100):
                self.caught_up.set()
        return result
