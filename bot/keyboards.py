"""Инлайн-кнопки и их callback data. Формат проверяет CallbackData.unpack, существование объекта панель."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import texts
from .storage import Chat, Lead

DELAYS = (1, 3, 5, 10, 30)
MODES = ("off", "always", "away")
AI_MODES = ("auto", "on", "off")


class MenuCb(CallbackData, prefix="m"):
    act: str
    val: str = ""


class ChatCb(CallbackData, prefix="c"):
    # on/off/auto: режим чата; stop: стоп из уведомления; ok: убрать кнопки;
    # take: «беру сам»; let: «пусть ведёт дальше»
    act: str
    chat_id: int
    src: str = ""  # list: перерисовать список /chats


class LeadCb(CallbackData, prefix="l"):
    act: str
    lead_id: int


def btn(text: str, data: CallbackData | str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data if isinstance(data, str) else data.pack())


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def onoff(flag: bool) -> str:
    return "✅" if flag else "❌"


def menu(mode: str, delay: int, spy_del: bool, spy_edit: bool, spy_mine: bool) -> InlineKeyboardMarkup:
    mode_row = [btn(("• " if m == mode else "") + texts.MODE_NAMES[m].capitalize(), MenuCb(act="mode", val=m))
                for m in MODES]
    delay_row = [btn(("• " if d == delay else "") + f"{d} мин", MenuCb(act="delay", val=str(d))) for d in DELAYS]
    return kb([
        mode_row,
        delay_row,
        [btn(f"🗑 Удаления {onoff(spy_del)}", MenuCb(act="spy", val="deletes")),
         btn(f"✏️ Правки {onoff(spy_edit)}", MenuCb(act="spy", val="edits")),
         btn(f"👤 Мои {onoff(spy_mine)}", MenuCb(act="spy", val="mine"))],
        [btn("📋 Лиды", MenuCb(act="leads")), btn("💬 Чаты", MenuCb(act="chats"))],
        [btn("📚 База знаний", MenuCb(act="kb")), btn("🧪 Тест", MenuCb(act="test"))],
    ])


def back() -> InlineKeyboardMarkup:
    return kb([[btn("⬅️ Меню", MenuCb(act="home"))]])


def chats(items: list[Chat]) -> InlineKeyboardMarkup:
    rows = []
    for c in items:
        label = (c.name or (f"@{c.username}" if c.username else str(c.chat_id)))[:18]
        row = [btn(label, MenuCb(act="noop"))]
        for m in AI_MODES:
            mark = "• " if c.ai_mode == m else ""
            row.append(btn(mark + texts.AI_MODE_NAMES[m], ChatCb(act=m, chat_id=c.chat_id, src="list")))
        rows.append(row)
    rows.append([btn("⬅️ Меню", MenuCb(act="home"))])
    return kb(rows)


def leads(items: list[Lead], names: dict[int, str]) -> InlineKeyboardMarkup:
    rows = [[btn(f"✅ Закрыт: {names.get(lead.chat_id, lead.name or str(lead.chat_id))[:24]}",
                 LeadCb(act="close", lead_id=lead.id))] for lead in items]
    rows.append([btn("⬅️ Меню", MenuCb(act="home"))])
    return kb(rows)


def replied(chat_id: int) -> InlineKeyboardMarkup:
    return kb([[btn("⏸ Стоп в этом чате", ChatCb(act="stop", chat_id=chat_id)),
                btn("✅ Ок", ChatCb(act="ok", chat_id=chat_id))]])


def lead_card(chat_id: int, with_url: bool = True, username: str | None = None) -> InlineKeyboardMarkup:
    first: list[InlineKeyboardButton] = []
    if with_url:
        first = [url_btn("💬 Открыть чат", f"tg://user?id={chat_id}")]
    elif username:
        first = [url_btn("💬 Открыть чат", f"https://t.me/{username}")]
    return kb([
        first,
        [btn("🙋 Беру сам", ChatCb(act="take", chat_id=chat_id)),
         btn("🤖 Пусть ведёт дальше", ChatCb(act="let", chat_id=chat_id))],
    ])


def new_client(chat_id: int) -> InlineKeyboardMarkup:
    return kb([[btn("🤖 Отвечать", ChatCb(act="on", chat_id=chat_id)),
                btn("🙅 Не отвечать", ChatCb(act="off", chat_id=chat_id))]])
