"""Лог исключений без текста ошибки: в сообщении pydantic или SDK может оказаться текст переписки."""
from __future__ import annotations

import logging
import traceback


def frames(exc: BaseException, limit: int = 8) -> str:
    """Тип исключения и места в коде (файл:строка функция), без str(exc) и без локальных переменных."""
    tb = traceback.extract_tb(exc.__traceback__)[-limit:]
    where = " <- ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in reversed(tb))
    return f"{type(exc).__module__}.{type(exc).__qualname__} at {where or '?'}"


def log_exc(logger: logging.Logger, msg: str, exc: BaseException, *args: object) -> None:
    logger.error(msg + ": %s", *args, frames(exc))
