"""Общее состояние: настройки, база, Telegram, ИИ, часы, владелец, подключение, рабочие настройки."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, InlineKeyboardMarkup, Message

from . import clock as clock_mod
from . import texts
from .config import Settings
from .kb import KB
from .llm import LLM, STT
from .prompts import Line
from .storage import Connection, Repo

log = logging.getLogger(__name__)

# Рабочие настройки владельца (kv), меняются кнопками и командами.
DEFAULTS: dict[str, str] = {
    "mode": "always",       # off | always | away
    "delay": "5",           # минут до ответа
    "takeover": "3",        # часов тишины после ручного ответа владельца
    "spy_deletes": "1",
    "spy_edits": "1",
    "spy_mine": "0",        # показывать и сообщения владельца
    "away_from": "20:00",   # с этого времени нерабочее
    "away_to": "09:00",     # до этого времени нерабочее
    "away_days": "7",       # выходные дни ISO, через запятую
    "paused_until": "",
}


def parse_hm(raw: str) -> time | None:
    try:
        h, m = raw.strip().split(":")
        hh, mm = int(h), int(m)
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


class App:
    def __init__(self, settings: Settings, repo: Repo, bot: Bot, *, llm: LLM | None = None,
                 stt: STT | None = None, clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], Awaitable[Any]] | None = None, raise_errors: bool = False,
                 typing_seconds: tuple[float, float] = (2.0, 4.0)) -> None:
        self.s = settings
        self.repo = repo
        self.bot = bot
        self.llm = llm
        self.stt = stt
        self._clock = clock
        self.sleep = sleep or asyncio.sleep
        self.raise_errors = raise_errors
        self.typing_seconds = typing_seconds
        self.owner_id: int | None = settings.owner_id
        self.kb = KB(settings.kb_dir, settings.kb_default_dir)
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._connections: dict[str, Connection] = {}
        self._claim_lock = asyncio.Lock()
        # Песочница /test: история в памяти, None = закрыта.
        self.sandbox: list[Line] | None = None

    async def load(self) -> None:
        if self.owner_id is None:
            raw = await self.repo.kv_get("owner_id")
            self.owner_id = int(raw) if raw else None
        try:
            self.kb.ensure()
        except OSError as e:
            log.error("kb: каталог недоступен: %s", type(e).__name__)
        self.s.media_dir.mkdir(parents=True, exist_ok=True)

    # --- время --------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock() if self._clock else clock_mod.now()

    def now_local(self) -> datetime:
        return self.now().astimezone(self.s.tz)

    def chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = self._chat_locks[chat_id] = asyncio.Lock()
        return lock

    # --- настройки ----------------------------------------------------------

    async def get(self, name: str) -> str:
        raw = await self.repo.kv_get(f"set:{name}")
        return DEFAULTS[name] if raw is None else raw

    async def set(self, name: str, value: str) -> None:
        if name not in DEFAULTS:
            raise KeyError(name)
        await self.repo.kv_set(f"set:{name}", value)

    async def get_int(self, name: str) -> int:
        try:
            return int(await self.get(name))
        except ValueError:
            return int(DEFAULTS[name])

    async def get_bool(self, name: str) -> bool:
        return (await self.get(name)) == "1"

    async def paused_until(self) -> datetime | None:
        raw = await self.get("paused_until")
        if not raw:
            return None
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return until if until > self.now() else None

    async def away_window(self) -> tuple[time, time, frozenset[int]]:
        start = parse_hm(await self.get("away_from")) or time(20, 0)
        end = parse_hm(await self.get("away_to")) or time(9, 0)
        raw_days = await self.get("away_days")
        days = frozenset(int(x) for x in raw_days.split(",") if x.strip().isdigit() and 1 <= int(x) <= 7)
        return start, end, days

    async def is_away_time(self, at: datetime) -> bool:
        """Нерабочее время по Ташкенту: выходной день или вне окна away_to..away_from."""
        start, end, days = await self.away_window()
        local = at.astimezone(self.s.tz)
        if local.isoweekday() in days:
            return True
        t = local.time()
        if start > end:
            return t >= start or t < end
        return start <= t < end

    # --- владелец и подключение ---------------------------------------------

    def is_owner(self, user_id: int | None) -> bool:
        return self.owner_id is not None and user_id == self.owner_id

    async def accept_connection(self, bc: BusinessConnection) -> Connection | None:
        """Подключение владельца: сохранить и сообщить ему об изменениях. Чужое: None, ничего не храним."""
        uid = bc.user.id
        async with self._claim_lock:
            if self.owner_id is None:
                # OWNER_ID пуст: владельцем становится первый, кто подключил бота.
                self.owner_id = uid
                await self.repo.kv_set("owner_id", str(uid))
                log.info("владелец зафиксирован: %s", uid)
        if uid != self.owner_id:
            log.warning("чужое подключение %s от user %s: игнор", bc.id, uid)
            return None
        rights = bc.rights
        can_reply = bool(rights.can_reply) if rights is not None else bool(bc.can_reply)
        conn = Connection(bc.id, uid, bool(bc.is_enabled), can_reply)
        before = self._connections.get(bc.id) or await self.repo.get_connection(bc.id)
        await self.repo.upsert_connection(conn, self.now())
        self._connections[bc.id] = conn
        log.info("подключение %s: enabled=%s can_reply=%s", bc.id, conn.is_enabled, conn.can_reply)
        if before is None or before.is_enabled != conn.is_enabled or before.can_reply != conn.can_reply:
            if not conn.is_enabled:
                await self.notify(texts.DISCONNECTED)
            elif not conn.can_reply:
                await self.notify(texts.CONNECTED_NO_REPLY + "\n" + texts.MENU_HINT)
            elif before is None or not before.is_enabled:
                await self.notify(texts.CONNECTED + "\n" + texts.MENU_HINT)
            else:
                await self.notify(texts.CAN_REPLY_NOW)
        return conn

    async def owner_connection(self, conn_id: str | None) -> Connection | None:
        """Подключение, через которое пришёл апдейт, если оно владельца. Иначе None (апдейт игнорируем)."""
        if not conn_id:
            return None
        conn = self._connections.get(conn_id) or await self.repo.get_connection(conn_id)
        if conn is None:
            # Подключение было до запуска бота: спросим Telegram, чьё оно.
            try:
                bc = await self.bot.get_business_connection(business_connection_id=conn_id)
            except TelegramAPIError as e:
                log.warning("getBusinessConnection %s: %s", conn_id, type(e).__name__)
                return None
            conn = await self.accept_connection(bc)
            if conn is None:
                return None
        self._connections[conn_id] = conn
        if not self.is_owner(conn.user_id):
            return None
        return conn

    async def current_connection(self) -> Connection | None:
        if self.owner_id is None:
            return None
        return await self.repo.latest_connection(self.owner_id)

    # --- сообщения владельцу ------------------------------------------------

    async def notify(self, text: str, markup: InlineKeyboardMarkup | None = None, *,
                     silent: bool = False) -> Message | None:
        if self.owner_id is None:
            return None
        try:
            return await self.bot.send_message(chat_id=self.owner_id, text=text, reply_markup=markup,
                                               disable_notification=silent or None)
        except TelegramAPIError as e:
            log.warning("владельцу не ушло: %s", type(e).__name__)
            return None
