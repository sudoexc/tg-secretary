"""Ретенция, весть о запуске, логи без текстов, устойчивость хендлеров."""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

import pytest

from bot import scheduler, texts
from bot.main import ALLOWED_UPDATES, build_dispatcher, quiet_loggers
from tests.conftest import World
from tests.harness import CLIENT, OWNER


async def test_retention_messages_60_days_media_30(ready: World) -> None:
    w = ready
    old = await w.client_says("старое", kind="text")
    pid = await w.client_says(None, kind="photo", file_id="p-old", caption="старое фото")
    m = await w.app.repo.find_message(CLIENT, pid)
    assert m and m.local_path
    photo_path = w.app.s.data_dir / m.local_path
    await w.feed(w.tg.bedit(w.client, w.client, old, "старое v2"))
    # 31 день спустя: медиа удалено, сообщения живы
    w.clock.advance(days=31)
    fresh = await w.client_says("свежее")
    rep = await scheduler.retention(w.app)
    assert rep.media >= 1 and not photo_path.exists()
    m = await w.app.repo.find_message(CLIENT, pid)
    assert m and m.local_path is None and m.file_id == "p-old"
    assert await w.app.repo.find_message(CLIENT, old)
    # 61 день: старые сообщения и их правки удалены, свежие остались
    w.clock.advance(days=30)
    rep = await scheduler.retention(w.app)
    assert rep.messages == 2
    assert await w.app.repo.find_message(CLIENT, old) is None
    assert await w.app.repo.edits_of(CLIENT, old) == []
    assert await w.app.repo.find_message(CLIENT, fresh)


async def test_retention_removes_orphan_files(ready: World) -> None:
    w = ready
    orphan = w.app.s.media_dir / "1" / "orphan.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    past = time.time() - 40 * 86400
    os.utime(orphan, (past, past))
    await scheduler.retention(w.app)
    assert not orphan.exists() and not orphan.parent.exists()


async def test_retention_runs_daily_at_four(world: World) -> None:
    w = world
    await w.connect()
    w.clock.set("2026-11-24 03:59")
    assert await scheduler.maybe_retention(w.app) is None
    w.clock.set("2026-11-24 04:00")
    assert await scheduler.maybe_retention(w.app) is not None
    w.clock.set("2026-11-24 12:00")
    assert await scheduler.maybe_retention(w.app) is None
    w.clock.set("2026-11-25 04:05")
    assert await scheduler.maybe_retention(w.app) is not None


async def test_startup_notice_throttled(ready: World) -> None:
    w = ready
    assert await scheduler.startup_notice(w.app)
    call = w.session.to_owner()[-1]
    assert call.text == texts.STARTED and call.get("disable_notification") is True
    w.clock.advance(minutes=5)
    assert not await scheduler.startup_notice(w.app)
    w.clock.advance(minutes=6)
    assert await scheduler.startup_notice(w.app)


def test_allowed_updates_explicit() -> None:
    assert ALLOWED_UPDATES == ["message", "callback_query", "business_connection", "business_message",
                               "edited_business_message", "deleted_business_messages"]


async def test_logs_have_no_message_texts(ready: World, caplog: pytest.LogCaptureFixture) -> None:
    w = ready
    secret = "МойСекретныйТекст"
    caplog.set_level(logging.DEBUG)
    quiet_loggers()  # как в бою: свои логгеры на DEBUG, чужие болтливые только WARNING
    mid = await w.client_says(f"{secret} один")
    await w.feed(w.tg.bedit(w.client, w.client, mid, f"{secret} два"))
    await w.client_says(None, kind="photo", caption=f"{secret} фото", file_id="ph")
    await w.owner_says(f"{secret} владелец")
    w.clock.advance(hours=4)
    await w.client_says(f"{secret} снова")
    await w.wait(6)
    await w.feed(w.tg.bdeleted(w.client, [mid]))
    await w.dm("/test")
    await w.dm(f"{secret} песочница")
    w.session.files["k"] = f"# {secret}".encode()
    await w.feed(w.tg.dm_document(w.owner, "knowledge.md", "k"))
    assert w.session.to_client()  # поток реально прошёл
    for rec in caplog.records:
        assert secret not in rec.getMessage(), rec.getMessage()
        assert secret not in str(rec.exc_text or "")
    reply = w.llm.of("secretary_reply")[0]
    assert secret in reply["user"]  # в ИИ текст уходит, в логи нет


async def test_broken_update_does_not_crash(ready: World, caplog: pytest.LogCaptureFixture) -> None:
    """В бою хендлер ловит исключение, пишет в лог и идёт дальше."""
    w = ready
    w.app.raise_errors = False
    dp = build_dispatcher(w.app)

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("input_value='текст клиента'")  # как в ValidationError: текст в сообщении ошибки

    w.app.repo.save_message = boom  # type: ignore[method-assign]
    await dp.feed_update(w.bot, w.tg.bmsg(w.client, w.client, "привет"))
    errors = [r for r in caplog.records if "ошибка в on_business_message" in r.getMessage()]
    assert errors and "RuntimeError" in errors[0].getMessage() and "spy.py" in errors[0].getMessage()
    assert all("текст клиента" not in r.getMessage() and not r.exc_info for r in caplog.records)
    # Следующий апдейт обрабатывается
    await dp.feed_update(w.bot, w.tg.dm(w.owner, "/start"))
    assert "Kotib" in w.session.of("sendMessage", chat_id=OWNER)[-1].text


async def test_pending_error_is_logged_and_marked(ready: World) -> None:
    w = ready
    w.app.raise_errors = False
    await w.client_says("Нужен сайт")

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("x")

    w.app.repo.recent_messages = boom  # type: ignore[method-assign]
    await w.wait(6)
    pend = await w.app.repo.pending_list(CLIENT)
    assert pend[-1].status == "error"
    assert w.session.to_client() == []


async def test_tick_noop_without_due(ready: World) -> None:
    w = ready
    assert await w.tick() == 0
    w.clock.advance(timedelta(days=1).total_seconds() / 60)
