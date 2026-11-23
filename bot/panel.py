"""Личка бота: меню владельца, списки, песочница, база знаний. Всем остальным одна строка."""
from __future__ import annotations

import io
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from . import fmt, secretary, texts, tg
from . import keyboards as kb
from .app import App, parse_hm
from .kb import MAX_BYTES, KBError
from .logs import log_exc
from .keyboards import ChatCb, LeadCb, MenuCb

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="chats", description="Последние чаты и режим ИИ"),
    BotCommand(command="leads", description="Открытые лиды"),
    BotCommand(command="test", description="Песочница секретаря"),
    BotCommand(command="stop", description="Выйти из песочницы"),
    BotCommand(command="pause", description="Пауза секретаря: /pause 2h"),
    BotCommand(command="resume", description="Снять паузу"),
    BotCommand(command="hours", description="Рабочие часы для режима «вне работы»"),
]


async def setup_commands(app: App) -> None:
    if app.owner_id is None:
        return
    try:
        await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=app.owner_id))
    except TelegramAPIError as e:
        log.warning("команды не установились: %s", type(e).__name__)


class OwnerOnly(BaseMiddleware):
    """Команды и кнопки только владельцу, в личке. Остальным «Это личный бот», группы молча мимо."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        app: App = data["app"]
        if isinstance(event, Message):
            if event.chat.type != "private":
                return None
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        else:
            return await handler(event, data)
        if user is None or not app.is_owner(user.id):
            log.info("не владелец %s: отказ", user.id if user else None)
            try:
                if isinstance(event, CallbackQuery):
                    await tg.answer(event, texts.NOT_YOURS, alert=True)
                else:
                    await event.answer(texts.NOT_YOURS)
            except TelegramAPIError:
                pass
            return None
        try:
            return await handler(event, data)
        except Exception as e:
            log_exc(log, "ошибка в панели", e)
            if app.raise_errors:
                raise
            if isinstance(event, CallbackQuery):
                await tg.answer(event, "Что-то пошло не так, попробуй ещё раз")
            return None


# --- экраны ---------------------------------------------------------------------


def _start_of_day(app: App) -> datetime:
    local = app.now_local()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def menu_view(app: App) -> tuple[str, InlineKeyboardMarkup]:
    conn = await app.current_connection()
    if conn is None:
        state = "none"
    elif not conn.is_enabled:
        state = "disabled"
    else:
        state = "ok" if conn.can_reply else "no_reply"
    mode = await app.get("mode")
    delay = await app.get_int("delay")
    takeover = await app.get_int("takeover")
    spy_del, spy_edit, spy_mine = (await app.get_bool("spy_deletes"), await app.get_bool("spy_edits"),
                                   await app.get_bool("spy_mine"))
    lines = ["🤖 <b>Kotib</b>", f"Подключение: {texts.connection_line(state)}", ""]
    if app.llm is None:
        sec = "выключен, " + texts.AI_OFF_KEY.format(env=app.s.llm_key_env)
    else:
        sec = texts.MODE_NAMES.get(mode, mode)
        if mode == "away":
            start, end, days = await app.away_window()
            off = ", ".join(DAY_NAMES[d - 1] for d in sorted(days)) or "нет"
            sec += f" (с {start.strftime('%H:%M')} до {end.strftime('%H:%M')}, выходные: {off})"
        sec += f", задержка {delay} мин, тишина после тебя {takeover} ч"
        paused = await app.paused_until()
        if paused:
            sec += f"\n⏸ пауза до {fmt.when(paused, app.s.tz, app.now())}"
        if state == "no_reply":
            sec += "\n⚠️ нет права отвечать, включи в настройках Telegram Business"
    lines.append(f"<b>Секретарь:</b> {sec}")
    lines.append(f"<b>Шпион:</b> удаления {kb.onoff(spy_del)}, правки {kb.onoff(spy_edit)}, "
                 f"мои {kb.onoff(spy_mine)}")
    usage = await app.repo.usage_since(_start_of_day(app))
    model = app.llm.model if app.llm else app.s.llm_model
    ai = f"{app.s.llm_provider}, {model}" + ("" if app.llm else ", ключа нет")
    lines.append(f"<b>ИИ:</b> {fmt.esc(ai)}. Сегодня: ответов {usage.replies}, вызовов {usage.calls}, "
                 f"токенов {fmt.tokens(usage.tokens)}")
    stt = f"расшифровка {app.stt.model}" if app.stt else "без расшифровки"
    lines.append(f"<b>Голосовые:</b> {fmt.esc(stt)}")
    if app.sandbox is not None:
        lines.append("🧪 Открыта песочница, выход: /stop")
    return "\n".join(lines), kb.menu(mode, delay, spy_del, spy_edit, spy_mine)


async def chats_view(app: App) -> tuple[str, InlineKeyboardMarkup]:
    items = await app.repo.recent_chats(20)
    if not items:
        return "💬 Чатов пока нет. Они появятся, когда тебе напишут.", kb.back()
    rows = ["💬 <b>Последние чаты</b>: класс, режим ИИ (авто: по классу)"]
    for c in items:
        rows.append(f"{fmt.link(c, c.chat_id)}: {texts.CLASS_NAMES.get(c.chat_class, c.chat_class)}, "
                    f"{texts.AI_MODE_NAMES.get(c.ai_mode, c.ai_mode)}")
    return "\n".join(rows), kb.chats(items)


async def leads_view(app: App) -> tuple[str, InlineKeyboardMarkup]:
    items = await app.repo.open_leads()
    if not items:
        return "📋 Открытых лидов нет.", kb.back()
    rows = [f"📋 <b>Открытые лиды</b>: {len(items)}"]
    names: dict[int, str] = {}
    for i, lead in enumerate(items, 1):
        chat = await app.repo.get_chat(lead.chat_id)
        names[lead.chat_id] = fmt.chat_name(chat, lead.chat_id)
        parts = []
        for key, label in (("need", "нужно"), ("deadline", "срок"), ("budget", "бюджет"), ("contact", "контакт")):
            val = getattr(lead, key)
            if val:
                parts.append(f"{label} {fmt.esc(fmt.trunc(val, 120))}")
        if not parts and lead.summary:
            parts.append(fmt.esc(fmt.trunc(lead.summary, 160)))
        upd = fmt.when(lead.updated_at, app.s.tz, app.now())
        rows.append(f"{i}. {fmt.link(chat, lead.chat_id)}: {', '.join(parts) or 'без деталей'} ({upd})")
    return "\n".join(rows), kb.leads(items, names)


async def kb_view(app: App) -> tuple[str, InlineKeyboardMarkup]:
    rows = ["📚 <b>База знаний</b>"]
    for name in ("knowledge.md", "style.md"):
        head = app.kb.head(name)
        count = app.kb.line_count(name)
        if head:
            rows.append(f"<b>{name}</b>, строк: {count}")
            rows.append(fmt.quote("\n".join(head), 700))
        else:
            rows.append(f"<b>{name}</b>: файла нет")
    rows.append(texts.KB_HINT)
    return "\n".join(rows), kb.back()


async def _show(message: Message, view: tuple[str, InlineKeyboardMarkup]) -> None:
    await message.answer(view[0], reply_markup=view[1])


async def _rerender(cq: CallbackQuery, bot: Bot, view: tuple[str, InlineKeyboardMarkup]) -> None:
    msg = cq.message
    if isinstance(msg, Message):
        if not await tg.edit_text(bot, msg.chat.id, msg.message_id, view[0], view[1]):
            await bot.send_message(chat_id=msg.chat.id, text=view[0], reply_markup=view[1])


async def _append(cq: CallbackQuery, bot: Bot, line: str) -> None:
    """Дописать итог под карточкой и убрать кнопки."""
    msg = cq.message
    if not isinstance(msg, Message):
        return
    base = msg.html_text or ""
    if line not in base:
        base = f"{base}\n{line}" if base else line
    await tg.edit_text(bot, msg.chat.id, msg.message_id, base, None)


# --- команды ----------------------------------------------------------------------

DAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
DURATION = re.compile(r"^(\d{1,4})\s*([mhdмчд]?)\w*$", re.I)


def parse_duration(raw: str) -> timedelta | None:
    m = DURATION.match(raw.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), (m.group(2) or "m").lower()
    if n <= 0:
        return None
    if unit in ("h", "ч"):
        return timedelta(hours=n)
    if unit in ("d", "д"):
        return timedelta(days=n)
    return timedelta(minutes=n)


def build_router() -> Router:
    r = Router(name="panel")
    r.message.outer_middleware(OwnerOnly())
    r.callback_query.outer_middleware(OwnerOnly())

    @r.message(CommandStart())
    @r.message(Command("menu", "help"))
    async def on_start(message: Message, app: App) -> None:
        await _show(message, await menu_view(app))

    @r.message(Command("chats"))
    async def on_chats(message: Message, app: App) -> None:
        await _show(message, await chats_view(app))

    @r.message(Command("leads"))
    async def on_leads(message: Message, app: App) -> None:
        await _show(message, await leads_view(app))

    @r.message(Command("test"))
    async def on_test(message: Message, app: App) -> None:
        await _enter_sandbox(app, message.chat.id)

    @r.message(Command("stop"))
    async def on_stop(message: Message, app: App) -> None:
        if app.sandbox is None:
            await message.answer(texts.SANDBOX_NOT_ACTIVE)
            return
        app.sandbox = None
        await message.answer(texts.SANDBOX_OFF)

    @r.message(Command("pause"))
    async def on_pause(message: Message, command: CommandObject, app: App) -> None:
        dur = parse_duration(command.args or "")
        if dur is None:
            await message.answer(texts.PAUSE_USAGE)
            return
        until = app.now() + dur
        await app.set("paused_until", until.isoformat())
        await message.answer(f"⏸ Секретарь на паузе до {fmt.when(until, app.s.tz, app.now())}. Снять: /resume")

    @r.message(Command("resume"))
    async def on_resume(message: Message, app: App) -> None:
        await app.set("paused_until", "")
        await message.answer("▶️ Секретарь снова работает.")

    @r.message(Command("hours"))
    async def on_hours(message: Message, command: CommandObject, app: App) -> None:
        start, end, days = await app.away_window()
        off = ", ".join(DAY_NAMES[d - 1] for d in sorted(days)) or "нет"
        now_txt = f"нерабочее с {start.strftime('%H:%M')} до {end.strftime('%H:%M')}, выходные: {off}"
        args = (command.args or "").replace("–", "-").split()
        if not args:
            await message.answer(texts.HOURS_USAGE.format(now=now_txt))
            return
        span = args[0].split("-")
        a = parse_hm(span[0]) if len(span) == 2 else None
        b = parse_hm(span[1]) if len(span) == 2 else None
        if a is None or b is None or a == b:
            await message.answer(texts.HOURS_USAGE.format(now=now_txt))
            return
        await app.set("away_from", a.strftime("%H:%M"))
        await app.set("away_to", b.strftime("%H:%M"))
        if len(args) > 1:
            picked = sorted({int(x) for x in args[1].split(",") if x.isdigit() and 1 <= int(x) <= 7})
            await app.set("away_days", ",".join(map(str, picked)))
        await message.answer(f"✅ Нерабочее время: с {a.strftime('%H:%M')} до {b.strftime('%H:%M')}.")

    @r.message(F.document)
    async def on_document(message: Message, app: App, bot: Bot) -> None:
        doc = message.document
        assert doc is not None
        name = app.kb.normalize_name(doc.file_name)
        if name is None:
            await message.answer(texts.KB_WRONG_NAME)
            return
        if doc.file_size and doc.file_size > MAX_BYTES:
            await message.answer("Файл больше 512 КБ.")
            return
        buf = io.BytesIO()
        try:
            await bot.download(doc, destination=buf, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("kb: файл не скачался: %s", type(e).__name__)
            await message.answer(texts.KB_DOWNLOAD_FAILED)
            return
        try:
            lines, backup = app.kb.replace(name, buf.getvalue(), app.now_local())
        except KBError as e:
            await message.answer(f"Не заменил: {e}")
            return
        log.info("kb: %s заменён, строк %d", name, lines)
        tail = f" Прошлая версия: backup/{backup.name}" if backup else ""
        await message.answer(f"✅ {name} заменён, строк: {lines}.{tail}")

    @r.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message, app: App) -> None:
        if app.sandbox is None:
            await message.answer(texts.UNKNOWN_CMD)
            return
        await _sandbox_reply(app, message)

    @r.message()
    async def on_other(message: Message, app: App) -> None:
        await message.answer(texts.SANDBOX_TEXT_ONLY if app.sandbox is not None else texts.UNKNOWN_CMD)

    # --- кнопки ---------------------------------------------------------------

    @r.callback_query(MenuCb.filter())
    async def on_menu(cq: CallbackQuery, callback_data: MenuCb, app: App, bot: Bot) -> None:
        act, val = callback_data.act, callback_data.val
        if act == "mode" and val in kb.MODES:
            await app.set("mode", val)
        elif act == "delay" and val.isdigit() and int(val) in kb.DELAYS:
            await app.set("delay", val)
        elif act == "spy" and val in ("deletes", "edits", "mine"):
            key = f"spy_{val}"
            await app.set(key, "0" if await app.get_bool(key) else "1")
        elif act == "leads":
            await tg.answer(cq)
            await _rerender(cq, bot, await leads_view(app))
            return
        elif act == "chats":
            await tg.answer(cq)
            await _rerender(cq, bot, await chats_view(app))
            return
        elif act == "kb":
            await tg.answer(cq)
            await _rerender(cq, bot, await kb_view(app))
            return
        elif act == "test":
            await tg.answer(cq)
            if isinstance(cq.message, Message):
                await _enter_sandbox(app, cq.message.chat.id)
            return
        elif act == "noop":
            await tg.answer(cq)
            return
        elif act != "home":
            await tg.answer(cq, texts.STALE_BUTTON)
            return
        await tg.answer(cq)
        await _rerender(cq, bot, await menu_view(app))

    @r.callback_query(ChatCb.filter())
    async def on_chat(cq: CallbackQuery, callback_data: ChatCb, app: App, bot: Bot) -> None:
        cid, act = callback_data.chat_id, callback_data.act
        chat = await app.repo.get_chat(cid)
        if chat is None or act not in ("on", "off", "auto", "stop", "ok", "take", "let"):
            await tg.answer(cq, texts.STALE_BUTTON)
            return
        now = app.now()
        if act == "ok":
            await tg.answer(cq)
            if isinstance(cq.message, Message):
                await tg.strip_markup(bot, cq.message.chat.id, cq.message.message_id)
            return
        if act in ("on", "off", "auto"):
            fields: dict[str, Any] = {"ai_mode": act}
            if act == "on":
                fields["handoff_at"] = None
            await app.repo.update_chat(cid, **fields)
            if act == "off":
                await app.repo.cancel_pending(cid, now, "chat_off")
            await tg.answer(cq, texts.DONE)
            if callback_data.src == "list":
                await _rerender(cq, bot, await chats_view(app))
            else:
                if act == "on":
                    await secretary.resume_chat(app, cid)
                line = "🤖 Ассистент отвечает в этом чате" if act == "on" else (
                    "🙅 Ассистент в этом чате молчит" if act == "off" else "Режим чата: авто")
                await _append(cq, bot, line)
            return
        if act in ("stop", "take"):
            await app.repo.update_chat(cid, ai_mode="off", handoff_at=None)
            await app.repo.cancel_pending(cid, now, "chat_off")
            await tg.answer(cq, texts.DONE)
            await _append(cq, bot, "⏸ ИИ в этом чате выключен" if act == "stop"
                          else "🙋 Ведёшь сам, ИИ в этом чате выключен")
            return
        # let: пусть ассистент ведёт дальше, тишина после handoff и ручного ответа снимается
        fields = {"handoff_at": None, "last_owner_msg_at": None}
        if chat.ai_mode == "off" or chat.chat_class != "business":
            fields["ai_mode"] = "on"
        await app.repo.update_chat(cid, **fields)
        await secretary.resume_chat(app, cid)
        await tg.answer(cq, texts.DONE)
        await _append(cq, bot, "🤖 Ассистент ведёт дальше")

    @r.callback_query(LeadCb.filter())
    async def on_lead(cq: CallbackQuery, callback_data: LeadCb, app: App, bot: Bot) -> None:
        lead = await app.repo.get_lead(callback_data.lead_id)
        if lead is None or callback_data.act != "close" or lead.status != "open":
            await tg.answer(cq, texts.STALE_BUTTON)
            return
        await app.repo.close_lead(lead.id, app.now())
        await tg.answer(cq, "Лид закрыт")
        await _rerender(cq, bot, await leads_view(app))

    @r.callback_query()
    async def on_unknown(cq: CallbackQuery) -> None:
        await tg.answer(cq, texts.STALE_BUTTON)

    return r


async def _enter_sandbox(app: App, chat_id: int) -> None:
    app.sandbox = []
    text = texts.SANDBOX_ON if app.llm else texts.SANDBOX_ON + "\n" + texts.AI_OFF_KEY.format(env=app.s.llm_key_env)
    await app.bot.send_message(chat_id=chat_id, text=text)


async def _sandbox_reply(app: App, message: Message) -> None:
    if app.llm is None:
        await message.answer(texts.AI_OFF_KEY.format(env=app.s.llm_key_env))
        return
    text = message.text or ""
    try:
        await app.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except TelegramAPIError:
        pass
    outcome = await secretary.sandbox_turn(app, text)
    d = outcome.decision
    if d is None:
        raw = fmt.esc(fmt.trunc(outcome.raw or outcome.error or "", 1500))
        await message.answer(f"{texts.SANDBOX_FAILED}\n<blockquote expandable><code>{raw}</code></blockquote>")
        return
    head = fmt.esc(d.reply) if d.reply else f"<i>{texts.SANDBOX_NO_REPLY}</i>"
    js = fmt.esc(fmt.trunc(secretary.decision_json(d), 3000))
    await message.answer(f"{head}\n\n<blockquote expandable><code>{js}</code></blockquote>")
