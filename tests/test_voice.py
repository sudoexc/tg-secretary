"""Голосовые клиента: без STT вежливый handoff один раз, с STT расшифровка идёт в контекст."""
from __future__ import annotations

import pytest

from bot import texts
from tests.conftest import FakeSTT, World
from tests.harness import CLIENT


async def test_voice_without_stt_handoff_once(ready: World) -> None:
    w = ready
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    sent = w.session.to_client()
    assert [c.text for c in sent] == [texts.VOICE_RECEIVED["ru"]]
    assert w.llm.of("secretary_reply") == []
    cards = [c for c in w.session.to_owner() if "Нужен ты" in c.text and "Голосовое" in c.text]
    assert len(cards) == 1
    # Ещё голосовое в тот же день: клиенту второй раз не пишем, владельцу сообщаем
    w.clock.advance(minutes=30)
    await w.client_says(None, kind="voice", file_id="v2")
    await w.wait(6)
    assert len(w.session.to_client()) == 1
    assert len([c for c in w.session.to_owner() if "Голосовое" in c.text]) == 2


async def test_voice_notice_in_uzbek(ready: World) -> None:
    w = ready
    await w.client_says("Assalomu alaykum, sayt kerak edi")
    await w.client_says(None, kind="voice", file_id="v1")
    await w.wait(6)
    assert w.session.to_client()[-1].text == texts.VOICE_RECEIVED["uz"]


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
    await w.wait(6)
    assert [c.text for c in w.session.to_client()] == [texts.VOICE_RECEIVED["ru"]]
