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


class AiogramRedact(logging.Filter):
    """aiogram пишет в лог str(исключения). У ClientDecodeError это сырой JSON апдейтов: тексты, имена,
    телефоны. Для записей aiogram исключение в аргументах заменяется именем типа, трейсбек кадрами."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("aiogram"):
            return True
        if isinstance(record.args, tuple) and any(isinstance(a, BaseException) for a in record.args):
            record.args = tuple(type(a).__name__ if isinstance(a, BaseException) else a for a in record.args)
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None:
            record.msg = f"{record.getMessage()} [{frames(exc)}]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def install_redaction() -> None:
    """На все обработчики корневого логгера: фильтр видит и записи, всплывшие от дочерних логгеров."""
    redact = AiogramRedact()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
