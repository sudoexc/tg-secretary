"""Фон: отложенные ответы каждые 10 с (переживают рестарт), ежедневная чистка в 04:00, весть о запуске."""
from __future__ import annotations

import asyncio
import logging
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from . import secretary, texts
from .app import App
from .logs import log_exc

log = logging.getLogger(__name__)

TICK_SECONDS = 10
RETENTION_AT = time(4, 0)
MESSAGES_DAYS = 60
MEDIA_DAYS = 30
SERVICE_DAYS = 90
START_NOTICE_GAP = timedelta(minutes=10)


@dataclass
class RetentionReport:
    messages: int
    media: int


async def tick(app: App) -> int:
    """Один проход: все наступившие ответы и, если пора, чистка. Возвращает число обработанных ответов."""
    done = 0
    for p in await app.repo.due_pending(app.now()):
        await secretary.process_pending(app, p)
        done += 1
    await maybe_retention(app)
    return done


async def run(app: App) -> None:
    while True:
        try:
            await tick(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_exc(log, "планировщик", e)
        await asyncio.sleep(TICK_SECONDS)


async def maybe_retention(app: App) -> RetentionReport | None:
    local = app.now_local()
    if local.time() < RETENTION_AT:
        return None
    day = local.date().isoformat()
    if await app.repo.kv_get("retention_day") == day:
        return None
    await app.repo.kv_set("retention_day", day)
    return await retention(app)


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


async def retention(app: App) -> RetentionReport:
    """Сообщения 60 дней, локальные медиа 30 дней. Служебные записи 90 дней."""
    now = app.now()
    data = app.s.data_dir
    media_n = 0
    for chat_id, mid, rel in await app.repo.media_older_than(now - timedelta(days=MEDIA_DAYS)):
        if _unlink(data / rel):
            media_n += 1
        await app.repo.forget_local_path(chat_id, mid)
    for rel in await app.repo.local_paths_before(now - timedelta(days=MESSAGES_DAYS)):
        _unlink(data / rel)
    n = await app.repo.purge_messages_before(now - timedelta(days=MESSAGES_DAYS))
    await app.repo.purge_service_before(now - timedelta(days=SERVICE_DAYS))
    # Осиротевшие файлы (старые версии медиа после правок) по времени файла.
    cutoff = time_mod.time() - MEDIA_DAYS * 86400
    media_root = app.s.media_dir
    if media_root.is_dir():
        for f in media_root.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff and _unlink(f):
                    media_n += 1
            except OSError:
                continue
        for d in sorted((p for p in media_root.rglob("*") if p.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    log.info("чистка: сообщений %d, медиа %d", n, media_n)
    return RetentionReport(n, media_n)


async def startup_notice(app: App) -> bool:
    """«Kotib запущен» тихо и не чаще раза в 10 минут: рестарт-петля не заспамит."""
    if app.owner_id is None:
        return False
    now = app.now()
    raw = await app.repo.kv_get("start_notice_at")
    if raw:
        try:
            if now - datetime.fromisoformat(raw) < START_NOTICE_GAP:
                return False
        except ValueError:
            pass
    await app.repo.kv_set("start_notice_at", now.isoformat())
    return await app.notify(texts.STARTED, silent=True) is not None
