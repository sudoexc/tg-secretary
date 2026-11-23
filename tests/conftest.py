"""Фикстуры: временная БД и DATA_DIR на тест, бот с мок-сессией, настоящий диспетчер, фейковый ИИ и часы."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update, User

from bot import config, scheduler
from bot import tg as tg_mod
from bot.app import App
from bot.llm import LLM, STT, LLMError, LLMResult, Part
from bot.main import build_dispatcher, default_properties
from bot.storage import Repo
from tests.harness import CLIENT, FRIEND, OWNER, STRANGER, TG, TOKEN, FakeClock, MockSession

DEFAULT_REPLY = {"reply": "Здравствуйте! Я ассистент Джавохира, он сейчас занят. Что нужно сделать?",
                 "handoff": False, "handoff_reason": None, "lead": None, "lead_ready": False, "language": "ru"}
DEFAULT_CLASS = {"chat_class": "business", "reason": "спрашивает про сайт"}


class FakeLLM(LLM):
    """Очереди ответов по имени схемы. Элемент: dict (JSON), str (сырой текст), Exception, "REFUSE"."""

    provider = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queues: dict[str, list[Any]] = {"secretary_reply": [], "chat_class": []}
        self.defaults: dict[str, Any] = {"secretary_reply": DEFAULT_REPLY, "chat_class": DEFAULT_CLASS}

    def push(self, schema_name: str, *items: Any) -> None:
        self.queues.setdefault(schema_name, []).extend(items)

    def reply(self, *items: Any) -> None:
        self.push("secretary_reply", *items)

    def klass(self, *items: Any) -> None:
        self.push("chat_class", *items)

    def of(self, schema_name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["schema_name"] == schema_name]

    async def complete(self, *, system: str, parts: list[Part], schema: dict[str, Any], schema_name: str,
                       max_tokens: int = 4096) -> LLMResult:
        user = "\n".join(p["text"] for p in parts if p["type"] == "text")
        self.calls.append({"system": system, "user": user, "parts": parts, "schema_name": schema_name,
                           "schema": schema})
        q = self.queues.get(schema_name) or []
        item = q.pop(0) if q else self.defaults.get(schema_name)
        if isinstance(item, Exception):
            raise item
        if item == "REFUSE":
            return LLMResult("", 10, 0, refused=True)
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return LLMResult(text, 100, 20)


class FakeSTT(STT):
    provider = "fake"
    model = "fake-stt"

    def __init__(self, text: str = "Нужен сайт для кафе, бюджет 500 долларов") -> None:
        self.text = text
        self.calls: list[tuple[int, str, str]] = []

    async def transcribe(self, data: bytes, filename: str, mime: str) -> str:
        self.calls.append((len(data), filename, mime))
        if self.text == "ERROR":
            raise LLMError("stt down")
        return self.text


KNOWLEDGE = "# Студия TIZIM\nЛендинг от 300$, срок от 5 дней.\n<!-- внутреннее: маржа 40% -->\nКарта 8600 1234 5678 9012\n"
STYLE = "# Стиль\nКоротко, на ты не переходить.\n"


def make_settings(tmp_path: Path, **over: str) -> config.Settings:
    default_kb = tmp_path / "kb_default"
    default_kb.mkdir(exist_ok=True)
    (default_kb / "knowledge.md").write_text(KNOWLEDGE, encoding="utf-8")
    (default_kb / "style.md").write_text(STYLE, encoding="utf-8")
    env = {
        "BOT_TOKEN": TOKEN,
        "OWNER_ID": str(OWNER),
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "test-key",
        "DATA_DIR": str(tmp_path / "data"),
        "KB_DEFAULT_DIR": str(default_kb),
        "TZ": "Asia/Tashkent",
    }
    env.update(over)
    return config.load({k: v for k, v in env.items() if v is not None})


@pytest.fixture
def clock() -> FakeClock:
    # Понедельник, 23 ноября 2026, 14:00 по Ташкенту: рабочее время
    return FakeClock("2026-11-23 14:00")


@pytest.fixture
def env_over() -> dict[str, str]:
    return {}


@pytest.fixture
def settings(tmp_path: Path, env_over: dict[str, str]) -> config.Settings:
    return make_settings(tmp_path, **env_over)


@pytest.fixture
def session(clock: FakeClock) -> MockSession:
    return MockSession(clock)


@pytest.fixture
async def bot(session: MockSession) -> AsyncIterator[Bot]:
    instance = Bot(TOKEN, session=session, default=default_properties())

    async def no_sleep(_: float) -> None:
        return None

    tg_mod.setup_session(instance, sleep=no_sleep)
    try:
        yield instance
    finally:
        await instance.session.close()


@pytest.fixture
async def repo(settings: config.Settings) -> AsyncIterator[Repo]:
    r = await Repo.open(settings.db_path)
    try:
        yield r
    finally:
        await r.close()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def stt() -> FakeSTT | None:
    return None


@pytest.fixture
def use_llm() -> bool:
    return True


@pytest.fixture
async def app(settings: config.Settings, repo: Repo, bot: Bot, clock: FakeClock, llm: FakeLLM,
              stt: FakeSTT | None, use_llm: bool) -> App:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    a = App(settings, repo, bot, llm=llm if use_llm else None, stt=stt, clock=clock, sleep=fake_sleep,
            raise_errors=True, typing_seconds=(2.0, 4.0))
    a.test_sleeps = sleeps  # type: ignore[attr-defined]
    await a.load()
    return a


@pytest.fixture
def dp(app: App) -> Dispatcher:
    return build_dispatcher(app, catch_errors=False)


@pytest.fixture
def tg(clock: FakeClock) -> TG:
    return TG(clock)


class World:
    """Сценарии словами пользователя: клиент написал, владелец ответил, прошло 5 минут."""

    def __init__(self, app: App, dp: Dispatcher, bot: Bot, session: MockSession, tg: TG, clock: FakeClock,
                 llm: FakeLLM) -> None:
        self.app, self.dp, self.bot, self.session, self.tg, self.clock, self.llm = app, dp, bot, session, tg, clock, llm
        self.owner = tg.user(OWNER, "Javohir", "javohir")
        self.client = tg.user(CLIENT, "Азиз", "aziz_dev")
        self.friend = tg.user(FRIEND, "Дилшод")
        self.stranger = tg.user(STRANGER, "Чужой")

    async def feed(self, update: Update) -> Any:
        return await self.dp.feed_update(self.bot, update)

    async def connect(self, user: User | None = None, conn: str = "bc1", **kw: Any) -> None:
        await self.feed(self.tg.connection(user or self.owner, conn, **kw))

    async def client_says(self, text: str | None = "Здравствуйте, сколько стоит сайт?", *,
                          peer: User | None = None, **kw: Any) -> int:
        peer = peer or self.client
        upd = self.tg.bmsg(peer, peer, text, **kw)
        await self.feed(upd)
        return upd.business_message.message_id  # type: ignore[union-attr]

    async def owner_says(self, text: str = "Сейчас отвечу", *, peer: User | None = None, **kw: Any) -> int:
        upd = self.tg.bmsg(peer or self.client, self.owner, text, **kw)
        await self.feed(upd)
        return upd.business_message.message_id  # type: ignore[union-attr]

    async def dm(self, text: str, user: User | None = None) -> None:
        await self.feed(self.tg.dm(user or self.owner, text))

    async def press(self, data: str, user: User | None = None, message_id: int = 1, text: str = "карточка") -> None:
        await self.feed(self.tg.press(user or self.owner, data, message_id=message_id, text=text))

    async def tick(self) -> int:
        return await scheduler.tick(self.app)

    async def wait(self, minutes: float) -> int:
        self.clock.advance(minutes=minutes)
        return await self.tick()


@pytest.fixture
def world(app: App, dp: Dispatcher, bot: Bot, session: MockSession, tg: TG, clock: FakeClock,
          llm: FakeLLM) -> World:
    return World(app, dp, bot, session, tg, clock, llm)


@pytest.fixture
async def ready(world: World) -> World:
    """Владелец подключил бота с правом отвечать, журнал вызовов чистый, чистка сегодня уже была."""
    await world.connect()
    await world.app.repo.kv_set("retention_day", world.app.now_local().date().isoformat())
    world.session.clear()
    return world
