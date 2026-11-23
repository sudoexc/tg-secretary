"""Секретарь: задержка, отмена, сдвиг дедлайна, takeover, лимиты, режимы, away, отправка и handoff."""
from __future__ import annotations

import pytest

from bot import texts
from bot.keyboards import ChatCb, MenuCb
from tests.conftest import World
from tests.harness import CLIENT, FORBIDDEN_24H, OWNER


async def test_reply_after_delay_with_typing_and_quiet_notice(ready: World) -> None:
    w = ready
    await w.client_says("Здравствуйте, сколько стоит лендинг?")
    p = await w.app.repo.waiting_pending(CLIENT)
    assert p is not None
    assert (p.due_at - w.app.now()).total_seconds() == pytest.approx(300, abs=1)

    assert await w.wait(4) == 0
    assert w.session.to_client() == []

    await w.wait(1.1)
    sent = w.session.to_client()
    assert len(sent) == 1
    assert sent[0].get("business_connection_id") == "bc1"
    assert sent[0].get("parse_mode") is None
    typing = w.session.of("sendChatAction", chat_id=CLIENT)
    assert typing and typing[0].get("business_connection_id") == "bc1" and typing[0].get("action") == "typing"
    assert w.session.calls.index(typing[0]) < w.session.calls.index(sent[0])
    assert 2.0 <= w.app.test_sleeps[-1] <= 4.0  # type: ignore[attr-defined]

    notice = [c for c in w.session.to_owner() if c.text.startswith("🤖 Ответил")]
    assert len(notice) == 1
    assert notice[0].get("disable_notification") is True
    notice[0].button("Стоп в этом чате")
    notice[0].button("Ок")
    # Наш ответ записан как отправленный ботом и идёт в контекст следующего раза
    hist = await w.app.repo.recent_messages(CLIENT)
    assert hist[-1].is_bot_sent and hist[-1].is_owner


async def test_classification_new_client_card_once(ready: World) -> None:
    w = ready
    await w.client_says("Салом, нужен бот для доставки")
    await w.wait(6)
    assert len(w.llm.of("chat_class")) == 1
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.chat_class == "business"
    cards = [c for c in w.session.to_owner() if "Новый клиент" in c.text]
    assert len(cards) == 1
    cards[0].button("Отвечать")
    cards[0].button("Не отвечать")
    # Второй раз не классифицирует и карточку не шлёт
    w.clock.advance(hours=4)
    await w.client_says("Ещё вопрос")
    await w.wait(6)
    assert len(w.llm.of("chat_class")) == 1
    assert len([c for c in w.session.to_owner() if "Новый клиент" in c.text]) == 1
    assert len(w.session.to_client()) == 2


async def test_personal_chat_gets_no_reply(ready: World) -> None:
    w = ready
    w.llm.klass({"chat_class": "personal", "reason": "друг зовёт на футбол"})
    await w.client_says("Бро, футбол в 8?", peer=w.friend)
    await w.wait(6)
    assert w.session.to_client(w.friend.id) == []
    assert w.llm.of("secretary_reply") == []
    chat = await w.app.repo.get_chat(w.friend.id)
    assert chat and chat.chat_class == "personal"
    # Дальше даже не планирует
    await w.client_says("Ну что?", peer=w.friend)
    assert await w.app.repo.waiting_pending(w.friend.id) is None


async def test_spam_and_chat_modes(ready: World) -> None:
    w = ready
    w.llm.klass({"chat_class": "spam", "reason": "реклама"})
    await w.client_says("Купите крипту")
    await w.wait(6)
    assert w.session.to_client() == []
    # Режим on перебивает класс
    await w.app.repo.update_chat(CLIENT, ai_mode="on")
    await w.client_says("Всё же сайт нужен")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    # Режим off: никогда
    await w.app.repo.update_chat(CLIENT, ai_mode="off")
    w.clock.advance(hours=1)
    await w.client_says("Алло")
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_unknown_classification_failure_keeps_unknown(ready: World) -> None:
    w = ready
    w.llm.klass("не json", "тоже нет")
    await w.client_says("Привет")
    await w.wait(6)
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.chat_class == "unknown"
    assert w.session.to_client() == []
    assert len(w.llm.of("chat_class")) == 2


async def test_owner_reply_cancels_pending_and_takeover(ready: World) -> None:
    w = ready
    await w.client_says("Сколько стоит?")
    w.clock.advance(minutes=2)
    await w.owner_says("Сейчас посчитаю")
    p = await w.app.repo.waiting_pending(CLIENT)
    assert p is None
    await w.wait(10)
    assert w.session.to_client() == []
    # Через час клиент пишет снова: takeover 3 ч, молчим
    w.clock.advance(hours=1)
    await w.client_says("Ну что там?")
    assert await w.app.repo.waiting_pending(CLIENT) is None
    await w.wait(10)
    assert w.session.to_client() == []
    # После 3 часов от ответа владельца секретарь снова в деле
    w.clock.advance(hours=2)
    await w.client_says("Алло?")
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_bot_sent_and_offline_messages_are_not_takeover(ready: World) -> None:
    w = ready
    await w.client_says("Привет, нужен сайт")
    await w.owner_says("Автоответ: я в отпуске", offline=True)
    assert await w.app.repo.waiting_pending(CLIENT) is not None
    await w.owner_says("Ответ бота", bot_sent=True)
    assert await w.app.repo.waiting_pending(CLIENT) is not None
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.last_owner_msg_at is None


async def test_deadline_shifts_one_reply_per_batch(ready: World) -> None:
    w = ready
    await w.client_says("Здравствуйте")
    w.clock.advance(minutes=3)
    await w.client_says("Нужен сайт для кафе")
    w.clock.advance(minutes=1)
    await w.client_says("Срок две недели")
    assert len(await w.app.repo.pending_list(CLIENT)) == 1
    assert await w.wait(4.5) == 0  # 8.5 мин от первого, 4.5 от последнего
    assert w.session.to_client() == []
    await w.wait(1)
    assert len(w.session.to_client()) == 1
    ctx = w.llm.of("secretary_reply")[0]["user"]
    assert "Нужен сайт для кафе" in ctx and "Срок две недели" in ctx
    await w.wait(10)
    assert len(w.session.to_client()) == 1


async def test_delay_setting_from_menu(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="delay", val="1").pack())
    assert await w.app.get("delay") == "1"
    await w.client_says("Есть вопрос")
    await w.wait(1.1)
    assert len(w.session.to_client()) == 1


async def test_owner_writes_while_model_thinks(ready: World) -> None:
    w = ready
    await w.client_says("Сколько стоит бот?")
    w.clock.advance(minutes=6)
    original = w.llm.complete

    async def slow(**kw):  # type: ignore[no-untyped-def]
        res = await original(**kw)
        if kw["schema_name"] == "secretary_reply":
            w.clock.advance(seconds=5)
            await w.owner_says("Сам отвечу")
        return res

    w.llm.complete = slow  # type: ignore[method-assign]
    await w.tick()
    assert w.session.to_client() == []
    pend = await w.app.repo.pending_list(CLIENT)
    assert pend[-1].status == "cancelled"


async def test_limits_per_chat_handoff(ready: World, settings) -> None:  # type: ignore[no-untyped-def]
    w = ready
    object.__setattr__(w.app.s, "chat_daily_limit", 2)
    for i in range(3):
        await w.client_says(f"вопрос {i}")
        await w.wait(6)
    assert len(w.session.to_client()) == 2
    handoffs = [c for c in w.session.to_owner() if "Лимит ИИ" in c.text]
    assert len(handoffs) == 1
    # Ещё сообщение: клиенту ничего, повторного уведомления нет
    await w.client_says("вопрос 4")
    await w.wait(6)
    assert len(w.session.to_client()) == 2
    assert len([c for c in w.session.to_owner() if "Лимит ИИ" in c.text]) == 1
    # Через сутки снова можно
    w.clock.advance(hours=24)
    await w.client_says("новый день")
    await w.wait(6)
    assert len(w.session.to_client()) == 3


async def test_limits_total(ready: World) -> None:
    w = ready
    object.__setattr__(w.app.s, "total_daily_limit", 1)
    await w.client_says("вопрос")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    await w.client_says("Есть заказ", peer=w.friend)
    await w.wait(6)
    assert w.session.to_client(w.friend.id) == []
    assert any("на все чаты" in t for t in w.session.owner_texts())


@pytest.mark.parametrize(("local", "answers"), [
    ("2026-11-23 14:00", False),   # понедельник днём: работа
    ("2026-11-23 21:00", True),    # вечер
    ("2026-11-24 07:30", True),    # раннее утро
    ("2026-11-29 14:00", True),    # воскресенье днём
])
async def test_away_mode_by_tashkent_hours(ready: World, local: str, answers: bool) -> None:
    w = ready
    await w.app.set("mode", "away")
    w.clock.set(local)
    await w.app.repo.kv_set("retention_day", w.app.now_local().date().isoformat())
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert bool(w.session.to_client()) is answers


async def test_away_custom_hours(ready: World) -> None:
    w = ready
    await w.app.set("mode", "away")
    await w.dm("/hours 13:00-14:00")
    assert await w.app.get("away_from") == "13:00"
    w.clock.set("2026-11-23 13:10")
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_global_mode_off_and_pause(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="mode", val="off").pack())
    await w.client_says("Есть вопрос")
    await w.wait(6)
    assert w.session.to_client() == []
    await w.press(MenuCb(act="mode", val="always").pack())
    await w.dm("/pause 2h")
    assert "на паузе до" in w.session.owner_texts()[-1]
    await w.client_says("Ещё вопрос")
    await w.wait(6)
    assert w.session.to_client() == []
    await w.dm("/resume")
    await w.client_says("Алло")
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_pause_usage(ready: World) -> None:
    w = ready
    await w.dm("/pause")
    assert w.session.owner_texts()[-1] == texts.PAUSE_USAGE


async def test_no_rights_status_and_silence(world: World) -> None:
    w = world
    await w.connect(can_reply=False)
    assert any("права отвечать нет" in t for t in w.session.owner_texts())
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert w.session.to_client() == []
    await w.dm("/start")
    assert "нет права отвечать, включи в настройках Telegram Business" in w.session.owner_texts()[-1]
    # Право включили: уведомление и ответы пошли
    await w.connect(can_reply=True)
    assert texts.CAN_REPLY_NOW in w.session.owner_texts()[-1]
    await w.client_says("Алло")
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_legacy_can_reply_flag(world: World) -> None:
    w = world
    await w.connect(legacy=True, can_reply=True)
    conn = await w.app.repo.get_connection("bc1")
    assert conn and conn.can_reply


async def test_send_error_hands_off(ready: World) -> None:
    w = ready
    w.session.fail("sendMessage", FORBIDDEN_24H, when=lambda d: bool(d.get("business_connection_id")))
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert w.session.to_client()  # попытка была
    cards = [c for c in w.session.to_owner() if "Нужен ты" in c.text]
    assert len(cards) == 1 and "Не смог отправить" in cards[0].text
    assert cards[0].button("Открыть чат")["url"] == f"tg://user?id={CLIENT}"
    assert not [c for c in w.session.to_owner() if c.text.startswith("🤖 Ответил")]


async def test_lead_card_on_ready_and_handoff(ready: World) -> None:
    w = ready
    w.llm.reply({"reply": "Понял, сайт для кафе за 2 недели. Передам Джавохиру.", "handoff": False,
                 "handoff_reason": None, "lead": {"name": "Азиз", "need": "сайт для кафе", "budget": "500$",
                                                   "deadline": "2 недели", "contact": None, "notes": None},
                 "lead_ready": True, "language": "ru"})
    await w.client_says("Нужен сайт для кафе, 500$, 2 недели")
    await w.wait(6)
    cards = [c for c in w.session.to_owner() if c.text.startswith("🔥 Лид")]
    assert len(cards) == 1
    text = cards[0].text
    assert "сайт для кафе" in text and "500$" in text and "2 недели" in text
    assert "—" not in text and "–" not in text
    assert cards[0].get("disable_notification") is None  # громко
    cards[0].button("Беру сам")
    cards[0].button("Пусть ведёт дальше")
    leads = await w.app.repo.open_leads()
    assert len(leads) == 1 and leads[0].need == "сайт для кафе"
    # Handoff: тишина в чате до решения владельца
    w.llm.reply({"reply": None, "handoff": True, "handoff_reason": "личный вопрос", "lead": None,
                 "lead_ready": False, "language": "ru"})
    w.clock.advance(hours=1)
    await w.client_says("А у тебя есть девушка?")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    assert any("личный вопрос" in c.text for c in w.session.to_owner() if c.text.startswith("🔥 Лид"))
    await w.client_says("Ау")
    assert await w.app.repo.waiting_pending(CLIENT) is None
    # «Пусть ведёт дальше» снимает тишину
    await w.press(ChatCb(act="let", chat_id=CLIENT).pack())
    await w.client_says("Так что с сайтом?")
    await w.wait(6)
    assert len(w.session.to_client()) == 2


async def test_take_and_stop_buttons(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    await w.wait(6)
    await w.press(ChatCb(act="stop", chat_id=CLIENT).pack())
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "off"
    edits = w.session.of("editMessageText", chat_id=OWNER)
    assert edits and "выключен" in edits[-1].text
    await w.press(ChatCb(act="on", chat_id=CLIENT).pack())
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "on"
    await w.press(ChatCb(act="take", chat_id=CLIENT).pack())
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "off"


async def test_secret_like_reply_is_blocked(ready: World) -> None:
    w = ready
    w.llm.reply({"reply": "Переведите на карту 8600 1234 5678 9012", "handoff": False, "handoff_reason": None,
                 "lead": None, "lead_ready": False, "language": "ru"})
    await w.client_says("Куда платить?")
    await w.wait(6)
    assert w.session.to_client() == []
    assert any("реквизиты" in c.text for c in w.session.to_owner())


async def test_messages_from_bots_are_ignored(ready: World) -> None:
    w = ready
    some_bot = w.tg.user(424242, "SomeBot", "some_bot", is_bot=True)
    await w.client_says("Ваш заказ доставлен", peer=some_bot)
    assert await w.app.repo.waiting_pending(some_bot.id) is None


async def test_no_llm_key_spy_works_secretary_off(world: World, tmp_path) -> None:  # type: ignore[no-untyped-def]
    w = world
    w.app.llm = None
    await w.connect()
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert w.session.to_client() == []
    assert await w.app.repo.find_message(CLIENT, (await w.app.repo.recent_messages(CLIENT))[-1].message_id)
    await w.dm("/start")
    assert "нет ключа ANTHROPIC_API_KEY" in w.session.owner_texts()[-1]


async def test_pending_survives_restart(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    # «Рестарт»: прерванная обработка снова ждёт и исполняется
    p = await w.app.repo.waiting_pending(CLIENT)
    assert p is not None
    w.clock.advance(minutes=6)
    assert await w.app.repo.claim_pending(p.id, w.app.now())  # взяли в работу и «упали»
    assert await w.app.repo.requeue_running(w.app.now()) == 1
    await w.tick()
    assert len(w.session.to_client()) == 1


async def test_let_button_answers_unanswered_batch(ready: World) -> None:
    w = ready
    w.llm.reply({"reply": None, "handoff": True, "handoff_reason": "просит созвон", "lead": None,
                 "lead_ready": False, "language": "ru"})
    await w.client_says("Давайте созвонимся")
    await w.wait(6)
    assert w.session.to_client() == []
    await w.client_says("Алло?")
    assert await w.app.repo.waiting_pending(CLIENT) is None  # тишина после handoff
    await w.press(ChatCb(act="let", chat_id=CLIENT).pack())
    assert await w.app.repo.waiting_pending(CLIENT) is not None
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_shifted_deadline_not_taken_early(ready: World) -> None:
    w = ready
    await w.client_says("раз")
    w.clock.advance(minutes=6)
    due = await w.app.repo.due_pending(w.app.now())
    assert len(due) == 1
    # Пока планировщик занят другим чатом, клиент дописал: срок сдвинулся вперёд
    await w.client_says("два")
    from bot import secretary

    assert await secretary.process_pending(w.app, due[0]) == "taken"
    assert w.session.to_client() == []
    await w.wait(6)
    assert len(w.session.to_client()) == 1


async def test_owner_writes_while_typing(ready: World) -> None:
    w = ready
    await w.client_says("Сколько стоит бот?")
    w.clock.advance(minutes=6)

    async def typing_sleep(_: float) -> None:
        w.clock.advance(seconds=3)
        await w.owner_says("Сам отвечу")

    w.app.sleep = typing_sleep
    await w.tick()
    assert w.session.of("sendChatAction", chat_id=CLIENT)
    assert w.session.to_client() == []
    assert (await w.app.repo.pending_list(CLIENT))[-1].status == "cancelled"
