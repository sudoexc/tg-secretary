"""Личка бота: меню на месте, /chats, /leads, база знаний, песочница /test."""
from __future__ import annotations

import json
import re

import pytest

from bot import fmt, texts
from bot.keyboards import ChatCb, LeadCb, MenuCb
from bot.llm import LLMError
from tests.conftest import World
from tests.harness import CLIENT, OWNER

DASHES = re.compile(r"[—–]")


async def test_menu_renders_status(ready: World) -> None:
    w = ready
    await w.dm("/start")
    menu = w.session.of("sendMessage", chat_id=OWNER)[-1]
    t = menu.text
    assert "Kotib" in t and "подключено, отвечать можно" in t
    assert "Секретарь:</b> всегда, задержка 5 мин, тишина после тебя 3 ч" in t
    assert "Шпион:</b> удаления ✅, правки ✅, мои ❌" in t
    assert "anthropic, fake-1" in t and "ответов 0" in t
    assert not DASHES.search(t)
    for label in ("Лиды", "Чаты", "База знаний", "Тест", "• Всегда", "• 5 мин", "Удаления ✅"):
        menu.button(label)


async def test_menu_buttons_rerender_in_place(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="mode", val="away").pack(), message_id=77)
    edit = w.session.of("editMessageText", chat_id=OWNER)[-1]
    assert edit.get("message_id") == 77
    assert "вне работы (с 20:00 до 09:00, выходные: вс)" in edit.text
    edit.button("• Вне работы")
    await w.press(MenuCb(act="spy", val="mine").pack(), message_id=77)
    edit = w.session.of("editMessageText", chat_id=OWNER)[-1]
    assert "мои ✅" in edit.text
    assert len(w.session.of("sendMessage", chat_id=OWNER)) == 0  # всё на месте, новых сообщений нет
    await w.press(MenuCb(act="chats").pack(), message_id=77)
    assert "Чатов пока нет" in w.session.of("editMessageText")[-1].text
    await w.press(MenuCb(act="home").pack(), message_id=77)
    assert "Kotib" in w.session.of("editMessageText")[-1].text


async def test_usage_shown_today(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    await w.wait(6)
    await w.dm("/start")
    t = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert "ответов 1" in t and "вызовов 2" in t  # классификация + решение


async def test_chats_list_and_toggle(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    await w.client_says("Как дела?", peer=w.friend)
    await w.dm("/chats")
    view = w.session.of("sendMessage", chat_id=OWNER)[-1]
    assert "Азиз" in view.text and "Дилшод" in view.text and "не разобран, авто" in view.text
    btn = view.button("• авто")
    assert btn["callback_data"].startswith("c:")
    await w.press(ChatCb(act="off", chat_id=CLIENT, src="list").pack(), message_id=5)
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "off"
    edit = w.session.of("editMessageText")[-1]
    assert edit.get("message_id") == 5 and "выкл" in edit.text
    # Выключение отменяет ждущий ответ
    assert await w.app.repo.waiting_pending(CLIENT) is None


async def test_new_client_card_buttons(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    await w.wait(6)
    await w.press(ChatCb(act="off", chat_id=CLIENT).pack(), text="🆕 Новый клиент: Азиз")
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.ai_mode == "off"
    edit = w.session.of("editMessageText")[-1]
    assert "молчит" in edit.text and edit.get("reply_markup") is None


async def test_leads_list_and_close(ready: World) -> None:
    w = ready
    lead = await w.app.repo.upsert_lead(CLIENT, {"need": "бот", "budget": "300$"}, w.app.now())
    await w.app.repo.touch_chat(CLIENT, "Азиз", "aziz_dev", w.app.now())
    await w.dm("/leads")
    view = w.session.of("sendMessage", chat_id=OWNER)[-1]
    assert "нужно бот" in view.text and "бюджет 300$" in view.text
    await w.press(LeadCb(act="close", lead_id=lead.id).pack(), message_id=9)
    assert await w.app.repo.open_leads() == []
    assert "Открытых лидов нет" in w.session.of("editMessageText")[-1].text
    # Повторное нажатие: кнопка устарела
    await w.press(LeadCb(act="close", lead_id=lead.id).pack(), message_id=9)
    assert w.session.of("answerCallbackQuery")[-1].get("text") == texts.STALE_BUTTON


async def test_kb_view_and_replace(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="kb").pack(), message_id=3)
    view = w.session.of("editMessageText")[-1].text
    assert "knowledge.md" in view and "Лендинг от 300$" in view and "style.md" in view
    # Замена документом с бэкапом
    w.session.files["newkb"] = "# Новая база\nСайт от 500$\n".encode()
    await w.feed(w.tg.dm_document(w.owner, "Knowledge.md", "newkb"))
    assert w.app.kb.read("knowledge.md") == "# Новая база\nСайт от 500$\n"
    backups = list(w.app.kb.backup_dir.glob("knowledge-*.md"))
    assert len(backups) == 1 and "Лендинг от 300$" in backups[0].read_text()
    reply = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert reply.startswith("✅ knowledge.md заменён, строк: 2") and "backup/" in reply
    # Новая база сразу в промпте
    await w.client_says("Сколько сайт?")
    await w.wait(6)
    assert "Сайт от 500$" in w.llm.of("secretary_reply")[-1]["system"]


async def test_kb_replace_rejects_bad_files(ready: World) -> None:
    w = ready
    before = w.app.kb.read("style.md")
    await w.feed(w.tg.dm_document(w.owner, "notes.txt", "x1"))
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.KB_WRONG_NAME
    w.session.files["bin"] = b"\xff\xfe\x00bad"
    await w.feed(w.tg.dm_document(w.owner, "style.md", "bin"))
    assert "UTF-8" in w.session.of("sendMessage", chat_id=OWNER)[-1].text
    w.session.files["empty"] = b"   \n"
    await w.feed(w.tg.dm_document(w.owner, "style.md", "empty"))
    assert "пустой" in w.session.of("sendMessage", chat_id=OWNER)[-1].text
    await w.feed(w.tg.dm_document(w.owner, "style.md", "huge", size=600 * 1024))
    assert "512" in w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert w.app.kb.read("style.md") == before


async def test_kb_missing_default_is_graceful(ready: World, tmp_path) -> None:  # type: ignore[no-untyped-def]
    w = ready
    w.app.kb.path("knowledge.md").unlink()
    w.app.kb.default_dir = tmp_path / "nowhere"
    assert w.app.kb.read("knowledge.md") == ""
    w.app.kb.ensure()
    await w.press(MenuCb(act="kb").pack())
    assert "knowledge.md</b>: файла нет" in w.session.of("editMessageText")[-1].text
    await w.client_says("Сколько сайт?")
    await w.wait(6)
    assert "база знаний пуста" in w.llm.of("secretary_reply")[-1]["system"]


async def test_sandbox_flow(ready: World) -> None:
    w = ready
    await w.dm("/test")
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.SANDBOX_ON
    await w.dm("Сколько стоит лендинг?")
    out = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert "ассистент Джавохира" in out
    assert "<blockquote expandable><code>" in out and "&quot;" not in out
    js = re.search(r"<code>(.*)</code>", out, re.S)
    assert js and json.loads(js.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
    call = w.llm.of("secretary_reply")[-1]
    assert "Сколько стоит лендинг?" in call["user"] and "Лендинг от 300$" in call["system"]
    assert "Первый ответ ассистента: да" in call["user"]
    # Второй ход помнит историю, без задержки, клиентам ничего
    await w.dm("А сроки?")
    call = w.llm.of("secretary_reply")[-1]
    assert "Сколько стоит лендинг?" in call["user"] and "Первый ответ ассистента: нет" in call["user"]
    assert w.session.to_client() == []
    assert await w.app.repo.count_replies(w.app.now().replace(year=2000)) == 0
    await w.dm("/stop")
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.SANDBOX_OFF
    n = len(w.llm.calls)
    await w.dm("Ещё вопрос")
    assert len(w.llm.calls) == n
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.UNKNOWN_CMD


async def test_sandbox_failure_and_text_only(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="test").pack())
    assert w.app.sandbox == []
    w.llm.reply("мусор", LLMError("boom"))
    await w.dm("Привет")
    assert texts.SANDBOX_FAILED in w.session.of("sendMessage", chat_id=OWNER)[-1].text
    await w.feed(w.tg.dm_document(w.owner, "brief.pdf", "pdf1"))  # документ всё равно обрабатывается как файл
    assert w.session.of("sendMessage", chat_id=OWNER)[-1].text == texts.KB_WRONG_NAME


@pytest.mark.parametrize("use_llm", [False])
async def test_sandbox_without_key(ready: World, use_llm: bool) -> None:
    w = ready
    await w.dm("/test")
    await w.dm("Привет")
    assert "нет ключа ANTHROPIC_API_KEY" in w.session.of("sendMessage", chat_id=OWNER)[-1].text


async def test_user_facing_texts_have_no_dashes() -> None:
    for name in dir(texts):
        if name.startswith("_"):
            continue
        val = getattr(texts, name)
        if isinstance(val, str):
            assert not DASHES.search(val), name
        if isinstance(val, dict):
            for v in val.values():
                assert not DASHES.search(v), name


async def test_leads_view_fits_telegram_limit(ready: World) -> None:
    """30 открытых лидов с длинными полями: одно сообщение до 4096, «и ещё N», кнопки только у показанных."""
    w = ready
    now = w.app.now()
    for i in range(30):
        cid = 700000 + i
        await w.app.repo.touch_chat(cid, f"Клиент номер {i} с длинным именем", f"client_{i}_name", now)
        await w.app.repo.upsert_lead(cid, {"need": "сайт для сети кафе с доставкой " * 6, "deadline": "к концу месяца",
                                           "budget": "около 500 долларов", "contact": "+998 90 123 45 67"}, now)
    await w.dm("/leads")
    view = w.session.of("sendMessage", chat_id=OWNER)[-1]
    assert fmt.visible_len(view.text) <= fmt.MESSAGE_LIMIT
    assert "Открытые лиды</b>: 30" in view.text
    shown = len(re.findall(r"^\d+\. ", view.text, re.M))
    assert 0 < shown < 30 and f"и ещё {30 - shown}" in view.text
    closes = [b for b in view.buttons if b["text"].startswith("✅ Закрыт")]
    assert len(closes) == shown
    # Закрыли показанный: список перерисован, лидов меньше
    await w.press(closes[0]["callback_data"], message_id=9)
    assert await w.app.repo.count_open_leads() == 29
    assert fmt.visible_len(w.session.of("editMessageText")[-1].text) <= fmt.MESSAGE_LIMIT


async def test_sandbox_long_reply_fits_limit(ready: World) -> None:
    w = ready
    reply = ("Пакет включает сайт, бот, CRM и поддержку. " * 40)[:1490]
    w.llm.reply({"reply": reply, "handoff": False, "handoff_reason": None,
                 "lead": {"name": "Тест", "need": "подробно про все услуги " * 40, "budget": None, "deadline": None,
                          "contact": None, "notes": "заметка " * 120},
                 "lead_ready": False, "language": "ru"})
    await w.dm("/test")
    await w.dm("Расскажи подробно про все услуги")
    out = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert fmt.visible_len(out) <= fmt.MESSAGE_LIMIT
    assert reply[:200] in out


async def test_sandbox_shows_what_client_would_get(ready: World) -> None:
    """Песочница показывает итог после той же постобработки, что в бою: без тире, реквизиты не уходят."""
    w = ready
    base = {"handoff": False, "handoff_reason": None, "lead": None, "lead_ready": False, "language": "ru"}
    w.llm.reply({"reply": "Лендинг — от 300$", **base}, {"reply": "Переведите на карту 8600 1234 5678 9012", **base})
    await w.dm("/test")
    await w.dm("Сколько стоит?")
    out = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert "Лендинг, от 300$" in out and not DASHES.search(out)
    await w.dm("Куда платить?")
    out = w.session.of("sendMessage", chat_id=OWNER)[-1].text
    assert "8600" not in out and texts.SANDBOX_NO_REPLY in out and "реквизиты" in out
    assert [ln.text for ln in w.app.sandbox or [] if ln.who == "assistant"] == ["Лендинг, от 300$"]
