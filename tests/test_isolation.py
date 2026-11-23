"""Владелец один: чужие подключения, сообщения, правки и удаления не хранятся; кнопки и команды только ему."""
from __future__ import annotations

import logging

import pytest

from bot import texts
from bot.app import App
from bot.keyboards import ChatCb, LeadCb, MenuCb
from tests.conftest import World
from tests.harness import CLIENT, OWNER, STRANGER


async def _count(app: App, table: str) -> int:
    row = await app.repo._one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: SLF001
    return int(row["n"]) if row else 0


async def test_foreign_connection_ignored(ready: World) -> None:
    w = ready
    await w.connect(w.stranger, conn="bc_x")
    assert await w.app.repo.get_connection("bc_x") is None
    assert w.session.to_owner() == []
    assert w.session.of("sendMessage", chat_id=STRANGER) == []
    # Сообщения, правки и удаления через чужое подключение
    victim = w.tg.user(123123, "Жертва")
    await w.feed(w.tg.bmsg(victim, victim, "секрет", conn="bc_x"))
    await w.feed(w.tg.bedit(victim, victim, 5, "секрет 2", conn="bc_x"))
    await w.feed(w.tg.bdeleted(victim, [5], conn="bc_x"))
    assert await _count(w.app, "messages") == 0
    assert await _count(w.app, "chats") == 0
    assert await _count(w.app, "pending") == 0
    assert w.session.to_owner() == []


async def test_unknown_connection_resolved_via_api(ready: World) -> None:
    w = ready
    # Чужое подключение, которого нет в базе: спросили Telegram, оно чужое, игнор
    w.session.connections["bc_y"] = w.tg.connection_json(w.stranger, "bc_y")
    await w.feed(w.tg.bmsg(w.client, w.client, "привет", conn="bc_y"))
    assert w.session.of("getBusinessConnection")
    assert await _count(w.app, "messages") == 0
    assert await w.app.repo.get_connection("bc_y") is None
    # Подключение владельца, созданное до запуска бота: принимаем
    w.session.connections["bc_old"] = w.tg.connection_json(w.owner, "bc_old")
    mid = await w.client_says("привет", conn="bc_old")
    assert await w.app.repo.find_message(CLIENT, mid)
    # Telegram не знает подключение: игнор
    await w.feed(w.tg.bmsg(w.client, w.client, "x", conn="bc_bad"))
    assert await _count(w.app, "messages") == 1


@pytest.mark.parametrize("env_over", [{"OWNER_ID": ""}])
async def test_first_connection_claims_owner(world: World, env_over: dict[str, str]) -> None:
    w = world
    assert w.app.owner_id is None
    await w.dm("/start")
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.NOT_YOURS
    await w.connect(w.owner)
    assert w.app.owner_id == OWNER
    assert await w.app.repo.kv_get("owner_id") == str(OWNER)
    assert w.session.of("setMyCommands")
    assert any(texts.CONNECTED in t for t in w.session.owner_texts())
    # Второй, кто подключит бота, владельцем не станет
    await w.connect(w.stranger, conn="bc_s")
    assert w.app.owner_id == OWNER
    assert await w.app.repo.get_connection("bc_s") is None
    # После рестарта владелец берётся из базы
    again = App(w.app.s, w.app.repo, w.bot, llm=w.llm, clock=w.clock)
    await again.load()
    assert again.owner_id == OWNER


async def test_env_owner_wins(world: World) -> None:
    w = world
    await w.connect(w.stranger, conn="bc_s")
    assert w.app.owner_id == OWNER
    assert await w.app.repo.get_connection("bc_s") is None


async def test_commands_from_stranger(ready: World) -> None:
    w = ready
    for cmd in ("/start", "/chats", "/leads", "/test", "/pause 2h", "привет"):
        await w.dm(cmd, user=w.stranger)
    answers = w.session.of("sendMessage", chat_id=STRANGER)
    assert [a.text for a in answers] == [texts.NOT_YOURS] * 6
    assert w.app.sandbox is None
    assert await w.app.get("paused_until") == ""


async def test_callback_from_stranger(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    for data in (MenuCb(act="mode", val="off").pack(), ChatCb(act="off", chat_id=CLIENT).pack(),
                 LeadCb(act="close", lead_id=1).pack(), "garbage"):
        await w.press(data, user=w.stranger)
    answers = w.session.of("answerCallbackQuery")
    assert len(answers) == 4
    assert all(a.get("text") == texts.NOT_YOURS and a.get("show_alert") for a in answers)
    assert await w.app.get("mode") == "always"
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "auto"
    assert w.session.of("editMessageText") == []


async def test_stranger_cannot_replace_kb(ready: World) -> None:
    w = ready
    before = w.app.kb.read("knowledge.md")
    w.session.files["kbx"] = b"# fake"
    await w.feed(w.tg.dm_document(w.stranger, "knowledge.md", "kbx"))
    assert w.app.kb.read("knowledge.md") == before
    assert w.session.of("sendMessage", chat_id=STRANGER)[-1].text == texts.NOT_YOURS
    assert "kbx" not in w.session.downloads


async def test_owner_callbacks_validated(ready: World) -> None:
    w = ready
    await w.press(ChatCb(act="off", chat_id=999999).pack())  # нет такого чата
    await w.press(MenuCb(act="delay", val="7").pack())       # нет такой задержки
    await w.press(MenuCb(act="mode", val="evil").pack())
    await w.press(ChatCb(act="drop", chat_id=CLIENT).pack())
    await w.press(LeadCb(act="close", lead_id=42).pack())
    await w.press("m:mode")  # битый формат CallbackData тоже не роняет
    answers = [a.get("text") for a in w.session.of("answerCallbackQuery")]
    assert answers.count(texts.STALE_BUTTON) == 6
    assert await w.app.get("delay") == "5"
    assert await w.app.get("mode") == "always"


async def test_foreign_connection_asked_once(ready: World, caplog: pytest.LogCaptureFixture) -> None:
    """Чужое подключение запоминается в памяти: флуд из его чатов не гонит getBusinessConnection на каждый апдейт."""
    w = ready
    w.session.connections["bc_y"] = w.tg.connection_json(w.stranger, "bc_y")
    victim = w.tg.user(123123, "Жертва")
    with caplog.at_level(logging.WARNING, logger="bot.app"):
        for i in range(10):
            await w.feed(w.tg.bmsg(victim, victim, f"флуд {i}", conn="bc_y"))
        await w.feed(w.tg.bedit(victim, victim, 5, "x", conn="bc_y"))
        await w.feed(w.tg.bdeleted(victim, [5], conn="bc_y"))
        await w.connect(w.stranger, conn="bc_y")
    assert len(w.session.of("getBusinessConnection")) == 1
    assert sum("чужое подключение" in r.getMessage() for r in caplog.records) == 1
    assert await _count(w.app, "messages") == 0 and await _count(w.app, "connections") == 1
