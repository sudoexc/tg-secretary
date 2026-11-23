"""Ретенция, весть о запуске, логи без текстов, устойчивость хендлеров."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import ClientDecodeError
from aiogram.methods import GetUpdates

from bot import scheduler, texts
from bot import tg as tg_mod
from bot.logs import AiogramRedact
from bot.main import ALLOWED_UPDATES, build_dispatcher, drop_webhook, private_files, quiet_loggers, stop_worker
from bot.tg import SkipBadUpdates
from tests.conftest import World
from tests.harness import CLIENT, OWNER, FakeClock, MockSession


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


# --- транспорт, логи, старт и остановка ---------------------------------------------

BAD_UPDATE = {"update_id": 11, "business_message": {
    "message_id": 5, "date": 1, "chat": {"id": 1, "type": "private", "first_name": "A"},
    "business_connection_id": "bc1", "text": "секретный текст клиента",
    "forward_origin": {"type": "brand_new_origin", "date": 1}}}
GOOD_UPDATE = {"update_id": 10, "message": {"message_id": 1, "date": 1,
                                            "chat": {"id": 1, "type": "private", "first_name": "A"}, "text": "hi"}}


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


async def test_bad_update_is_skipped_without_text_in_logs(bot: Bot, clock: FakeClock,
                                                          caplog: pytest.LogCaptureFixture) -> None:
    class Safe(SkipBadUpdates, MockSession):
        pass

    content = json.dumps({"ok": True, "result": [GOOD_UPDATE, BAD_UPDATE]}, ensure_ascii=False)
    with caplog.at_level(logging.DEBUG):
        resp = Safe(clock).check_response(bot, GetUpdates(), 200, content)
    updates = resp.result
    assert [u.update_id for u in updates] == [10, 11]  # offset пойдёт дальше битого
    assert updates[0].message is not None and updates[1].business_message is None
    assert "секретный" not in caplog.text and "апдейт 11 пропущен" in caplog.text
    # Без примеси вся пачка падает, а сырой JSON сидит в тексте исключения
    with pytest.raises(ClientDecodeError) as err:
        MockSession(clock).check_response(bot, GetUpdates(), 200, content)
    assert "секретный" in str(err.value)
    # Пустой Update aiogram пропускает без ошибки
    with pytest.warns(RuntimeWarning):
        await Dispatcher().feed_update(bot, updates[1])


def test_aiogram_logs_get_no_exception_text() -> None:
    err = ClientDecodeError("Failed to deserialize object", ValueError("x"), '{"text": "секретный текст"}')
    out = _Lines()
    out.addFilter(AiogramRedact())
    parent = logging.getLogger("aiogram")
    parent.addHandler(out)
    try:
        logging.getLogger("aiogram.dispatcher").error("Failed to fetch updates - %s: %s", type(err).__name__, err)
        try:
            raise err
        except ClientDecodeError as e:
            logging.getLogger("aiogram.event").exception("Cause exception while process update id=%d\n%s: %s",
                                                         7, type(e).__name__, e)
        logging.getLogger("aiogram.dispatcher").warning("Sleep for %f seconds", 1.5)
    finally:
        parent.removeHandler(out)
    assert len(out.lines) == 3
    assert not any("секретный" in ln for ln in out.lines)
    assert out.lines[0] == "Failed to fetch updates - ClientDecodeError: ClientDecodeError"
    assert "aiogram.exceptions.ClientDecodeError at" in out.lines[1]
    assert out.lines[2] == "Sleep for 1.500000 seconds"


def test_files_are_private(tmp_path: Path) -> None:
    old = private_files()
    try:
        f = tmp_path / "kotib.db"
        f.write_text("x")
        d = tmp_path / "media"
        d.mkdir()
        assert f.stat().st_mode & 0o777 == 0o600
        assert d.stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(old)
    root = Path(__file__).resolve().parent.parent
    assert "chmod 700 data" in (root / "deploy.sh").read_text()
    assert "stop_grace_period: 30s" in (root / "docker-compose.yml").read_text()


async def test_drop_webhook_keeps_queue(bot: Bot, session: MockSession) -> None:
    await drop_webhook(bot)
    assert session.of("deleteWebhook")[-1].get("drop_pending_updates") is False
    session.fail("deleteWebhook", "Unauthorized", status=401)
    await drop_webhook(bot)  # ошибка только в лог


async def test_poll_watch_marks_caught_up(bot: Bot, session: MockSession) -> None:
    watch = tg_mod.PollWatch()
    bot.session.middleware(watch)
    session.updates = [[{"update_id": i} for i in range(100)], []]
    await bot(GetUpdates(limit=100))
    assert not watch.caught_up.is_set()  # полная пачка: бэклог ещё не разобран
    await bot(GetUpdates(limit=100))
    assert watch.caught_up.is_set()


async def test_scheduler_waits_for_polling_backlog(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w = ready
    monkeypatch.setattr(scheduler, "STARTUP_GRACE", 0)
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
    await w.client_says("Нужен сайт")
    w.clock.advance(minutes=6)
    caught_up, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(scheduler.run(w.app, stop=stop, ready=caught_up))
    await asyncio.sleep(0.05)
    assert w.session.to_client() == []  # поллинг ещё не догнал очередь
    caught_up.set()
    for _ in range(50):
        if w.session.to_client():
            break
        await asyncio.sleep(0.01)
    assert len(w.session.to_client()) == 1
    stop.set()
    await asyncio.wait_for(task, 1)


async def _hanging_reply(w: World) -> tuple[asyncio.Event, asyncio.Event]:
    await w.client_says("Нужен сайт")
    w.clock.advance(minutes=6)
    original = w.llm.complete
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow(**kw):  # type: ignore[no-untyped-def]
        if kw["schema_name"] == "secretary_reply":
            entered.set()
            await gate.wait()
        return await original(**kw)

    w.llm.complete = slow  # type: ignore[method-assign]
    return entered, gate


async def test_stop_worker_lets_current_reply_finish(ready: World) -> None:
    w = ready
    entered, gate = await _hanging_reply(w)
    stop = asyncio.Event()
    worker = asyncio.create_task(scheduler.run(w.app, stop=stop))
    await asyncio.wait_for(entered.wait(), 5)
    asyncio.get_running_loop().call_later(0.05, gate.set)
    await stop_worker(worker, stop, 5)
    assert worker.done() and not worker.cancelled()
    assert len(w.session.to_client()) == 1


async def test_stop_worker_timeout_leaves_row_for_restart(ready: World) -> None:
    w = ready
    entered, _ = await _hanging_reply(w)
    stop = asyncio.Event()
    worker = asyncio.create_task(scheduler.run(w.app, stop=stop))
    await asyncio.wait_for(entered.wait(), 5)
    await stop_worker(worker, stop, 0.05)
    assert worker.cancelled()
    assert [p.status for p in await w.app.repo.pending_list(CLIENT)] == ["running"]
    assert await w.app.repo.requeue_running(w.app.now()) == 1


def test_style_has_no_invented_facts() -> None:
    style = (Path(__file__).resolve().parent.parent / "kb" / "style.md").read_text(encoding="utf-8")
    for bad in ("на встрече", "бугун ози йозади", "по скидке решает"):
        assert bad not in style.lower()
