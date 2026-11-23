"""Единственный источник «сейчас». Тесты подменяют часы через App(clock=...)."""
from datetime import datetime, timezone


def now() -> datetime:
    """Текущий момент в UTC, всегда с таймзоной."""
    return datetime.now(timezone.utc)
