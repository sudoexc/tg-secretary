"""Голосовые клиента: без STT вежливый handoff один раз, с STT расшифровка идёт в контекст."""
from __future__ import annotations

import pytest

from bot import texts
from bot.keyboards import ChatCb
from tests.conftest import FakeSTT, World
from tests.harness import CLIENT


async def _business(w: World) -> None:
    await w.app.repo.update_chat(CLIENT, chat_class="business")


async def test_voice_without_stt_handoff_once(ready: World) -> None:
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await _business(w)
    await w.wait(6)
    sent = w.session.to_client()
    # Первое сообщение ассистента в разговоре: представиться, а не только «голосовое получил»
    assert [c.text for c in sent] == [texts.VOICE_INTRO["ru"]]
    assert w.llm.of("secretary_reply") == []
    cards = [c for c in w.session.to_owner() if "Нужен ты" in c.text and "Голосовое" in c.text]
    assert len(cards) == 1
    # Ещё голосовое в тот же день: клиенту второй раз не пишем, владельцу сообщаем
    w.clock.advance(minutes=30)
    await w.client_says(None, kind="voice", file_id="v2")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    assert len([c for c in w.session.to_owner() if "Голосовое" in c.text]) == 2
    # Потом текст: про те голосовые владелец уже знает, отвечает ИИ, повторно не представляется
    await w.client_says("Алло, вы тут?")
    await w.wait(6)
    assert len(w.session.to_client()) == 2
    assert len([c for c in w.session.to_owner() if "Голосовое" in c.text]) == 2
    assert "Первый ответ ассистента: нет" in w.llm.of("secretary_reply")[-1]["user"]


async def test_voice_notice_in_uzbek(ready: World) -> None:
    w = ready
    await w.client_says("Assalomu alaykum, sayt kerak edi")
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert w.session.to_client()[-1].text == texts.VOICE_INTRO["uz"]


async def test_voice_notice_without_intro_after_assistant_spoke(ready: World) -> None:
    w = ready
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    w.clock.advance(minutes=10)
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert w.session.to_client()[-1].text == texts.VOICE_RECEIVED["ru"]


@pytest.mark.parametrize("stt", [FakeSTT()])
async def test_voice_with_stt_goes_to_context(ready: World, stt: FakeSTT) -> None:
    w = ready
    vid = await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert stt.calls and stt.calls[0][1:] == ("voice.ogg", "audio/ogg")
    m = await w.app.repo.find_message(CLIENT, vid)
    assert m and m.transcript == stt.text
    user = w.llm.of("secretary_reply")[-1]["user"]
    assert f"[голосовое, расшифровка: {stt.text}]" in user
    assert len(w.session.to_client()) == 1
    assert w.session.to_client()[0].text != texts.VOICE_RECEIVED["ru"]


@pytest.mark.parametrize("stt", [FakeSTT("ERROR")])
async def test_voice_stt_failure_falls_back_to_handoff(ready: World, stt: FakeSTT) -> None:
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await _business(w)
    await w.wait(6)
    assert [c.text for c in w.session.to_client()] == [texts.VOICE_INTRO["ru"]]


@pytest.mark.parametrize("stt", [FakeSTT()])
async def test_new_chat_voice_first_is_transcribed_before_classification(ready: World, stt: FakeSTT) -> None:
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert stt.calls
    assert stt.text in w.llm.of("chat_class")[0]["user"]
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.chat_class == "business"
    assert len(w.session.to_client()) == 1
    assert w.session.to_client()[0].text not in texts.VOICE_INTRO.values()


async def test_new_chat_voice_first_without_stt_asks_owner(ready: World) -> None:
    """Голосовое из неразобранного чата: разбирать нечего, класс не фиксируем, клиенту ничего, владельцу вопрос."""
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert w.llm.of("chat_class") == []
    assert w.session.to_client() == []
    notes = [c for c in w.session.to_owner() if c.text.startswith("🎤 Голосовое")]
    assert len(notes) == 1
    notes[0].button("Отвечать")
    notes[0].button("Не отвечать")
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.chat_class == "unknown"
    # Дальше текстом: классификация по тексту и вежливый handoff по голосовому
    await w.client_says("Нужен сайт")
    await w.wait(6)
    assert len(w.llm.of("chat_class")) == 1
    assert [c.text for c in w.session.to_client()] == [texts.VOICE_INTRO["ru"]]


async def test_unknown_voice_owner_presses_answer(ready: World) -> None:
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    await w.press(ChatCb(act="on", chat_id=CLIENT).pack())
    assert await w.app.repo.waiting_pending(CLIENT) is not None
    await w.wait(6)
    assert [c.text for c in w.session.to_client()] == [texts.VOICE_INTRO["ru"]]


async def test_owner_answers_during_transcription(ready: World) -> None:
    w = ready

    class SlowSTT(FakeSTT):
        async def transcribe(self, data: bytes, filename: str, mime: str) -> str:
            w.clock.advance(seconds=5)
            await w.owner_says("Послушал, сейчас наберу")
            return ""

    w.app.stt = SlowSTT()
    await w.client_says(None, kind="voice", file_id="v1")
    await _business(w)
    await w.wait(6)
    assert w.session.to_client() == []
    assert [(p.status, p.reason) for p in await w.app.repo.pending_list(CLIENT)] == [("cancelled", "owner")]
