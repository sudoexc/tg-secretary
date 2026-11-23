"""Сквозной прогон без сети, собранный как в bot.main: настоящий цикл поллинга aiogram (getUpdates с offset
и allowed_updates), примесь SkipBadUpdates, RetryAfter и PollWatch на сессии, диспетчер и хендлеры из кода,
планировщик в фоне. Подменены только транспорт (фейковый Bot API ниже), ИИ и часы.

Сценарий: владелец 111 подключил бота → клиент написал → через задержку один ответ ассистента через
business_connection_id → эхо нашего ответа не считается ручным ответом → правка и удаление приходят
владельцу → чужое подключение 999 и его чаты не оставляют в базе ничего.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import TelegramType

from bot import panel, scheduler, texts
from bot import tg as tg_mod
from bot.app import App
from bot.main import (
    ALLOWED_UPDATES,
    HANDLERS_SHUTDOWN,
    WORKER_SHUTDOWN,
    build_dispatcher,
    default_properties,
    drop_webhook,
    setup_profile,
    stop_worker,
)
from bot.storage import Repo
from bot.tg import SkipBadUpdates
from tests.conftest import FakeLLM, make_settings
from tests.harness import BOT_USER, TOKEN, FakeClock, MockSession

OWNER_ID = 111
CLIENT_ID = 222
VICTIM_ID = 333
FOREIGN_ID = 999
CONN = "bc-e2e"
EVIL = "bc-evil"

FIRST = "Здравствуйте! Сколько стоит лендинг для кафе?"
EDITED = "Здравствуйте! Сколько стоит лендинг для кофейни, к 1 декабря?"
FOREIGN_TEXT = "секрет чужого аккаунта"

pytestmark = pytest.mark.filterwarnings("ignore:Detected unknown update type:RuntimeWarning")


def _user(uid: int, name: str, username: str | None = None) -> dict[str, Any]:
    u: dict[str, Any] = {"id": uid, "is_bot": False, "first_name": name, "language_code": "ru"}
    if username:
        u["username"] = username
    return u


def _chat(uid: int, name: str, username: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"id": uid, "type": "private", "first_name": name}
    if username:
        c["username"] = username
    return c


OWNER = _user(OWNER_ID, "Javohir", "javohir")
CLIENT = _user(CLIENT_ID, "Азиз", "aziz_cafe")
CLIENT_CHAT = _chat(CLIENT_ID, "Азиз", "aziz_cafe")
FOREIGN = _user(FOREIGN_ID, "Чужой")
VICTIM = _user(VICTIM_ID, "Жертва")
VICTIM_CHAT = _chat(VICTIM_ID, "Жертва")
# getMe в Bot API 10.5 отдаёт полный профиль бота (BotFullInfo): без этих полей aiogram не стартует поллинг.
ME = {**BOT_USER, "can_join_groups": False, "can_read_all_group_messages": False, "supports_guest_queries": False,
      "supports_inline_queries": False, "needs_inline_query_location": False, "can_connect_to_business": True,
      "has_main_web_app": False, "has_topics_enabled": False, "allows_users_to_create_topics": False,
      "has_topic_sidebar": False, "can_manage_bots": False, "supports_join_request_queries": False}


class FakeBotAPI(SkipBadUpdates, MockSession):
    """Bot API как сервер: апдейты лежат в очереди, getUpdates отдаёт те, что не подтверждены offset,
    и только типов из allowed_updates (остальные Telegram не прислал бы вовсе)."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.queue: list[dict[str, Any]] = []
        self.dropped: list[dict[str, Any]] = []
        self.confirmed = 0  # все update_id <= confirmed подтверждены offset'ом
        self.allowed_seen: list[list[str] | None] = []
        self._ids = itertools.count(5000)

    def push(self, *updates: dict[str, Any]) -> int:
        self.queue.extend(updates)
        return max(u["update_id"] for u in updates)

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> Any:
        if isinstance(method, GetUpdates):
            # Long polling: без паузы пустой ответ крутил бы цикл, не отдавая управление.
            await asyncio.sleep(0.005)
        return await super().make_request(bot, method, timeout)

    def _message(self, data: dict[str, Any], **extra: Any) -> dict[str, Any]:
        msg = super()._message(data, **extra)
        if data.get("business_connection_id"):
            msg["from"] = OWNER  # от имени аккаунта, к которому подключён бот
        return msg

    def _result(self, name: str, data: dict[str, Any], method: Any) -> Any:
        if name == "getMe":
            return ME
        if name != "getUpdates":
            return super()._result(name, data, method)
        offset = data.get("offset")
        if offset is not None:
            self.confirmed = max(self.confirmed, offset - 1)
            self.queue = [u for u in self.queue if u["update_id"] >= offset]
        allowed = data.get("allowed_updates")
        self.allowed_seen.append(allowed)
        if allowed is not None:
            keep = [u for u in self.queue if not (set(u) - {"update_id"}) or (set(u) - {"update_id"}) & set(allowed)]
            self.dropped += [u for u in self.queue if u not in keep]
            self.queue = keep
        return self.queue[: data.get("limit") or 100]

    # --- инспекция ---------------------------------------------------------------

    def to_owner(self) -> list[Any]:
        return [c for c in self.calls if c.method.startswith("send") and c.method != "sendChatAction"
                and c.chat_id == OWNER_ID and not c.get("business_connection_id")]

    def business_sends(self) -> list[Any]:
        return [c for c in self.calls if c.method == "sendMessage" and c.get("business_connection_id")]


async def until(cond: Callable[[], Any], what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        got = cond()
        if inspect.isawaitable(got):
            got = await got
        if got:
            return
        if loop.time() > deadline:
            raise AssertionError(f"не дождались: {what}")
        await asyncio.sleep(0.01)


@dataclass
class Live:
    app: App
    dp: Dispatcher
    bot: Bot
    api: FakeBotAPI
    clock: FakeClock
    llm: FakeLLM
    repo: Repo
    _ids: Any = None

    def uid(self) -> int:
        if self._ids is None:
            self._ids = itertools.count(1)
        return next(self._ids)

    def ts(self) -> int:
        return int(self.clock().timestamp())

    async def send(self, *updates: dict[str, Any]) -> None:
        """Положить апдейты на сервер и дождаться, пока поллинг их подтвердит и хендлеры доработают."""
        last = self.api.push(*updates)
        await until(lambda: self.api.confirmed >= last and not self.dp._handle_update_tasks,  # noqa: SLF001
                    f"обработка апдейтов до {last}")

    async def count(self, table: str) -> int:
        row = await self.repo._one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: SLF001
        return int(row["n"]) if row else 0

    async def ticks(self, n: int = 5) -> None:
        """Дать фоновому планировщику пройти несколько циклов."""
        await asyncio.sleep(scheduler.TICK_SECONDS * n + 0.02)

    # --- апдейты Telegram в сыром JSON ----------------------------------------------

    def connection(self, user: dict[str, Any], conn: str, *, can_reply: bool = True) -> dict[str, Any]:
        return {"update_id": self.uid(), "business_connection": {
            "id": conn, "user": user, "user_chat_id": user["id"], "date": self.ts(), "is_enabled": True,
            "rights": {"can_reply": can_reply, "can_read_messages": True}}}

    def bmsg(self, chat: dict[str, Any], sender: dict[str, Any], mid: int, text: str, *, conn: str = CONN,
             bot_sent: bool = False) -> dict[str, Any]:
        m: dict[str, Any] = {"message_id": mid, "from": sender, "chat": chat, "date": self.ts(),
                             "business_connection_id": conn, "text": text}
        if bot_sent:
            m["sender_business_bot"] = BOT_USER
        return {"update_id": self.uid(), "business_message": m}

    def bedit(self, chat: dict[str, Any], sender: dict[str, Any], mid: int, text: str, sent_at: int, *,
              conn: str = CONN) -> dict[str, Any]:
        return {"update_id": self.uid(), "edited_business_message": {
            "message_id": mid, "from": sender, "chat": chat, "date": sent_at, "edit_date": self.ts(),
            "business_connection_id": conn, "text": text}}

    def bdeleted(self, chat: dict[str, Any], ids: list[int], *, conn: str = CONN) -> dict[str, Any]:
        return {"update_id": self.uid(), "deleted_business_messages": {
            "business_connection_id": conn, "chat": chat, "message_ids": ids}}

    def dm(self, user: dict[str, Any], text: str) -> dict[str, Any]:
        m: dict[str, Any] = {"message_id": 7000 + self.uid(), "from": user, "date": self.ts(), "text": text,
                             "chat": _chat(user["id"], user["first_name"])}
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        return {"update_id": self.uid(), "message": m}

    def broken(self) -> dict[str, Any]:
        """Апдейт, который эта версия aiogram не разбирает (новый тип forward_origin)."""
        return {"update_id": self.uid(), "business_message": {
            "message_id": 4242, "date": self.ts(), "chat": CLIENT_CHAT, "from": CLIENT,
            "business_connection_id": CONN, "text": "текст из битого апдейта",
            "forward_origin": {"type": "brand_new_origin", "date": 1}}}


@pytest.fixture
async def live(tmp_path: Path, request: pytest.FixtureRequest,
               monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Live]:
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "STARTUP_GRACE", 0)
    clock = FakeClock("2026-11-23 14:00")  # понедельник, рабочее время Ташкента
    settings = make_settings(tmp_path, OWNER_ID=request.param)
    repo = await Repo.open(settings.db_path)
    api = FakeBotAPI(clock)
    bot = Bot(TOKEN, session=api, default=default_properties())
    tg_mod.setup_session(bot)
    poll = tg_mod.PollWatch()
    bot.session.middleware(poll)
    llm = FakeLLM()
    # Как в бою: настоящий asyncio.sleep, ошибки хендлеров ловятся и пишутся в лог, а не летят в тест.
    app = App(settings, repo, bot, llm=llm, clock=clock, typing_seconds=(0.01, 0.02))
    await app.load()
    await repo.requeue_running(app.now())
    dp = build_dispatcher(app)
    await setup_profile(bot)
    await panel.setup_commands(app)
    await scheduler.startup_notice(app)
    await drop_webhook(bot)
    stop = asyncio.Event()
    worker = asyncio.create_task(scheduler.run(app, stop=stop, ready=poll.caught_up), name="pending")
    polling = asyncio.create_task(dp.start_polling(
        bot, allowed_updates=ALLOWED_UPDATES, handle_signals=False, close_bot_session=False,
        tasks_shutdown_timeout=HANDLERS_SHUTDOWN))
    await until(lambda: poll.caught_up.is_set(), "первый getUpdates")
    try:
        yield Live(app, dp, bot, api, clock, llm, repo)
    finally:
        if not polling.done():
            await dp.stop_polling()
        await asyncio.wait_for(polling, 5)
        await stop_worker(worker, stop, WORKER_SHUTDOWN)
        await repo.close()
        await bot.session.close()


@pytest.mark.parametrize("live", ["", str(OWNER_ID)], ids=["owner-claimed", "owner-from-env"], indirect=True)
async def test_full_flow_through_polling(live: Live, caplog: pytest.LogCaptureFixture) -> None:
    w, api = live, live.api
    caplog.set_level(logging.INFO)
    assert api.of("deleteWebhook")[0].get("drop_pending_updates") is False

    # 1. Владелец 111 подключает бота в Telegram Business
    await w.send(w.connection(OWNER, CONN))
    assert w.app.owner_id == OWNER_ID
    if w.app.s.owner_id is None:
        assert await w.repo.kv_get("owner_id") == str(OWNER_ID)
    conn = await w.repo.get_connection(CONN)
    assert conn is not None and conn.user_id == OWNER_ID and conn.can_reply and conn.is_enabled
    assert any(texts.CONNECTED in c.text for c in api.to_owner())
    assert any(c.get("scope", {}).get("chat_id") == OWNER_ID for c in api.of("setMyCommands")
               if isinstance(c.get("scope"), dict))

    # 2. Клиент пишет; ответ ставится на 5 минут, раньше ничего не уходит
    t0 = w.ts()
    await w.send(w.bmsg(CLIENT_CHAT, CLIENT, 50, FIRST))
    stored = await w.repo.find_message(CLIENT_ID, 50)
    assert stored is not None and stored.text == FIRST and not stored.is_owner and not stored.is_bot_sent
    pending = await w.repo._all("SELECT status, trigger_message_id FROM pending")  # noqa: SLF001
    assert [(p["status"], p["trigger_message_id"]) for p in pending] == [("waiting", 50)]
    await w.ticks()
    w.clock.advance(minutes=4)
    await w.ticks()
    assert api.business_sends() == []

    # 3. Задержка прошла: ровно один ответ от имени владельца через business_connection_id
    w.clock.advance(minutes=2)
    await until(lambda: api.business_sends(), "ответ ассистента клиенту")
    await w.ticks()
    sends = api.business_sends()
    assert len(sends) == 1
    reply = sends[0]
    assert reply.chat_id == CLIENT_ID and reply.get("business_connection_id") == CONN
    assert reply.get("parse_mode") is None
    assert "ассистент Джавохира" in reply.text
    typing = [c for c in api.of("sendChatAction") if c.chat_id == CLIENT_ID]
    assert typing and typing[0].get("business_connection_id") == CONN and typing[0].get("action") == "typing"
    assert api.calls.index(typing[0]) < api.calls.index(reply)
    assert [c["schema_name"] for c in w.llm.calls] == ["chat_class", "secretary_reply"]
    assert FIRST in w.llm.of("secretary_reply")[0]["user"]
    owner_texts = [c.text for c in api.to_owner()]
    assert any("Новый клиент" in t for t in owner_texts)
    done = [c for c in api.to_owner() if c.text.startswith("🤖 Ответил")]
    assert len(done) == 1 and done[0].get("disable_notification") is True
    pending = await w.repo._all("SELECT status, reason FROM pending")  # noqa: SLF001
    assert [(p["status"], p["reason"]) for p in pending] == [("done", "replied")]

    # 4. Telegram присылает эхо нашего ответа (sender_business_bot): это не ручной ответ владельца
    echo_id = reply.result.message_id
    await w.send(w.bmsg(CLIENT_CHAT, OWNER, echo_id, reply.text, bot_sent=True))
    echo = await w.repo.find_message(CLIENT_ID, echo_id)
    assert echo is not None and echo.is_bot_sent and echo.is_owner
    chat = await w.repo.get_chat(CLIENT_ID)
    assert chat is not None and chat.last_owner_msg_at is None and chat.chat_class == "business"
    w.clock.advance(minutes=10)
    await w.ticks()
    assert len(api.business_sends()) == 1  # повторно не ответил

    # 5. Клиент правит сообщение; битый апдейт рядом пропускается и не держит очередь
    before = len(api.to_owner())
    await w.send(w.bedit(CLIENT_CHAT, CLIENT, 50, EDITED, t0), w.broken())
    edits = api.to_owner()[before:]
    assert len(edits) == 1
    note = edits[0].text
    assert note.startswith("✏️") and "Азиз" in note and "изменил(а)" in note
    assert "Было:" in note and FIRST in note and "Стало:" in note and EDITED in note
    assert edits[0].get("disable_notification") is None  # громко
    history = await w.repo.edits_of(CLIENT_ID, 50)
    assert len(history) == 1 and history[0]["old_text"] == FIRST
    now50 = await w.repo.find_message(CLIENT_ID, 50)
    assert now50 is not None and now50.text == EDITED and now50.version == 2
    assert await w.repo.find_message(CLIENT_ID, 4242) is None
    assert len(api.business_sends()) == 1  # правка не будит секретаря

    # 6. Клиент удаляет сообщение: владельцу последняя версия и время отправки
    before = len(api.to_owner())
    await w.send(w.bdeleted(CLIENT_CHAT, [50]))
    gone = api.to_owner()[before:]
    assert len(gone) == 1
    note = gone[0].text
    assert note.startswith("🗑") and "Азиз" in note and "удалил(а)" in note and "14:00" in note
    assert EDITED in note
    deleted = await w.repo.find_message(CLIENT_ID, 50)
    assert deleted is not None and deleted.deleted_at is not None

    # 7. Чужой аккаунт 999 подключает того же бота: ни подключения, ни его переписки в базе
    snapshot = {t: await w.count(t) for t in ("connections", "messages", "edits", "chats", "pending", "leads")}
    before = len(api.to_owner())
    await w.send(w.connection(FOREIGN, EVIL))
    await w.send(
        w.bmsg(VICTIM_CHAT, VICTIM, 60, FOREIGN_TEXT, conn=EVIL),
        w.bmsg(VICTIM_CHAT, FOREIGN, 61, FOREIGN_TEXT + " 2", conn=EVIL),
        w.bedit(VICTIM_CHAT, VICTIM, 60, FOREIGN_TEXT + " 3", w.ts(), conn=EVIL),
        w.bdeleted(VICTIM_CHAT, [60, 61], conn=EVIL),
        w.bmsg(CLIENT_CHAT, CLIENT, 62, FOREIGN_TEXT + " 4", conn=EVIL),
        w.dm(FOREIGN, "/start"),
    )
    w.clock.advance(minutes=10)
    await w.ticks()
    assert {t: await w.count(t) for t in snapshot} == snapshot
    assert await w.repo.get_connection(EVIL) is None
    assert await w.repo.find_message(VICTIM_ID, 60) is None and await w.repo.get_chat(VICTIM_ID) is None
    assert await w.repo.find_message(CLIENT_ID, 62) is None
    assert w.app.owner_id == OWNER_ID
    if w.app.s.owner_id is None:
        assert await w.repo.kv_get("owner_id") == str(OWNER_ID)
    assert api.to_owner()[before:] == []
    assert api.of("getBusinessConnection") == []  # чужое подключение помним в памяти
    to_foreign = [c for c in api.calls if c.method.startswith("send") and c.chat_id in (FOREIGN_ID, VICTIM_ID)]
    assert [c.text for c in to_foreign] == [texts.NOT_YOURS]
    assert all(c.get("business_connection_id") != EVIL for c in api.calls)
    assert len(api.business_sends()) == 1
    assert len(w.llm.calls) == 2  # чужие сообщения ИИ не видел

    # 8. Транспорт: каждый getUpdates с явным allowed_updates, ничего не отброшено, всё подтверждено
    assert api.allowed_seen and all(a == ALLOWED_UPDATES for a in api.allowed_seen)
    assert api.dropped == []
    await w.dp.stop_polling()
    await until(lambda: api.queue == [] or api.confirmed >= max(u["update_id"] for u in api.queue),
                "подтверждение последней пачки")

    # Логи: без ошибок и без текстов переписки
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (FIRST, EDITED, FOREIGN_TEXT, "лендинг", "битого апдейта", reply.text):
        assert secret not in log_text
