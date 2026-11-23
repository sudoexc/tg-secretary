"""Форматирование для сообщений владельцу (HTML): экранирование, имена, ссылки, время."""
from __future__ import annotations

from datetime import datetime, tzinfo
from html import escape

from .storage import Chat


def esc(text: str | None) -> str:
    return escape(text or "", quote=False)


def trunc(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def chat_name(chat: Chat | None, fallback_id: int | None = None) -> str:
    if chat and chat.name:
        return chat.name
    if chat and chat.username:
        return f"@{chat.username}"
    cid = chat.chat_id if chat else fallback_id
    return f"id {cid}" if cid else "Без имени"


def link(chat: Chat | None, chat_id: int) -> str:
    """Имя ссылкой tg://user?id= и @username, если он есть."""
    name = esc(chat_name(chat, chat_id))
    out = f'<a href="tg://user?id={chat_id}">{name}</a>'
    if chat and chat.username:
        out += f" @{esc(chat.username)}"
    return out


def hm(dt: datetime | None, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M") if dt else "?"


def when(dt: datetime | None, tz: tzinfo, now: datetime) -> str:
    """14:03 сегодня, иначе 23.11 14:03."""
    if dt is None:
        return "?"
    local = dt.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def full_dt(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def quote(text: str, limit: int = 1500) -> str:
    return f"<blockquote>{esc(trunc(text, limit))}</blockquote>"


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
