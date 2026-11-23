"""Секретарь: когда отвечать, отложенный ответ, решение модели, отправка, лиды и handoff владельцу."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from . import fmt, prompts, spy, texts
from . import keyboards as kb
from .app import App
from .llm import LLMError, Part
from .logs import log_exc
from .prompts import Context, Decision, Line
from .storage import Chat, Connection, Lead, Pending, StoredMessage

log = logging.getLogger(__name__)

MAX_TOKENS = 8192  # ответ короткий, запас под рассуждение модели
CLASSIFY_TOKENS = 1024
FIRST_REPLY_GAP = timedelta(hours=12)
VOICE_NOTICE_GAP = timedelta(hours=24)
LIMIT_NOTICE_GAP = timedelta(hours=24)
CLASSIFY_FAIL_GAP = timedelta(hours=6)
DAY = timedelta(hours=24)
VOICE_KINDS = ("voice", "video_note")
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SERVICE_IDS = frozenset({777000})  # служебные уведомления Telegram


@dataclass
class Outcome:
    decision: Decision | None
    raw: str
    error: str | None


# --- кто чей ------------------------------------------------------------------


def is_auto(m: StoredMessage) -> bool:
    """Автоответ Telegram Business от имени владельца (приветствие, «нет на месте»): не его ответ."""
    return bool(m.extra.get("offline"))


def who_of(m: StoredMessage) -> str:
    if m.is_bot_sent:
        return "assistant"
    if m.is_owner:
        return "auto" if is_auto(m) else "owner"
    return "client"


def to_lines(app: App, history: list[StoredMessage]) -> list[Line]:
    return [Line(who_of(m), m.date.astimezone(app.s.tz), spy.describe(m, for_ai=True)) for m in history]


def answered_upto(history: list[StoredMessage]) -> int:
    """До какого id сообщения собеседника уже отвечено. Ручной ответ владельца закрывает всё до себя,
    наш ответ только свою пачку (extra.upto), автоответ Telegram ничего не закрывает."""
    upto = 0
    for m in history:
        if not m.is_owner or is_auto(m):
            continue
        mark = m.extra.get("upto") if m.is_bot_sent else None
        upto = max(upto, mark if isinstance(mark, int) else m.message_id)
    return upto


def client_batch(history: list[StoredMessage]) -> list[StoredMessage]:
    """Сообщения собеседника без ответа: на них и отвечаем одним ответом. Что клиент дописал, пока ИИ
    думал над прошлой пачкой, остаётся здесь, хотя наш ответ в чате уже ниже."""
    upto = answered_upto(history)
    return [m for m in history if not m.is_owner and m.message_id > upto]


def _meaningful(m: StoredMessage) -> bool:
    """Есть ли по сообщению что классифицировать: текст, подпись, расшифровка, файл."""
    if m.is_bot_sent or is_auto(m):
        return False
    if m.transcript:
        return True
    if m.kind == "sticker":
        return False
    if m.kind == "document":
        return True
    return bool((m.text or "").strip())


# --- можно ли отвечать ---------------------------------------------------------


async def blocker(app: App, chat: Chat | None, conn: Connection | None, at: datetime) -> str | None:
    """Причина молчать в момент at или None, если отвечать можно (класс unknown решится позже)."""
    if app.llm is None:
        return "llm_off"
    if chat is None:
        return "no_chat"
    mode = await app.get("mode")
    if mode == "off":
        return "mode_off"
    paused = await app.paused_until()
    if paused is not None and paused > at:
        return "paused"
    if mode == "away" and not await app.is_away_time(at):
        return "work_hours"
    if conn is None or not conn.is_enabled:
        return "no_connection"
    if not conn.can_reply:
        return "no_rights"
    if chat.is_bot or chat.chat_id in SERVICE_IDS:
        return "bot"
    if chat.ai_mode == "off":
        return "chat_off"
    if chat.ai_mode == "auto" and chat.chat_class in ("personal", "spam"):
        return chat.chat_class
    hours = await app.get_int("takeover")
    quiet = [t for t in (chat.last_owner_msg_at, chat.handoff_at) if t is not None]
    if quiet and at < max(quiet) + timedelta(hours=hours):
        return "takeover"
    return None


# --- входящие ------------------------------------------------------------------


async def on_owner_message(app: App, inc: spy.Incoming) -> None:
    """Владелец ответил сам: отложенный ответ отменяется, секретарь молчит takeover часов.
    Время берём из самого сообщения: после простоя бота апдейт приходит позже, чем владелец написал."""
    now = app.now()
    at = min(inc.date, now)
    chat = await app.repo.get_chat(inc.chat_id)
    if chat is None or chat.last_owner_msg_at is None or chat.last_owner_msg_at < at:
        await app.repo.update_chat(inc.chat_id, last_owner_msg_at=at)
    n = await app.repo.cancel_pending(inc.chat_id, now, "owner")
    if n:
        log.info("чат %s: владелец ответил, отложенный ответ отменён", inc.chat_id)


async def on_client_message(app: App, inc: spy.Incoming) -> None:
    now = app.now()
    await app.repo.update_chat(inc.chat_id, last_client_msg_at=now)
    if inc.stored is None or inc.from_bot:
        return
    chat = await app.repo.get_chat(inc.chat_id)
    delay = await app.get_int("delay")
    due = now + timedelta(minutes=delay)
    reason = await blocker(app, chat, inc.conn, due)
    if reason:
        log.info("чат %s: секретарь молчит (%s)", inc.chat_id, reason)
        return
    res = await app.repo.schedule(inc.chat_id, inc.conn.id, due, inc.message_id, now)
    log.info("чат %s: ответ %s на %s", inc.chat_id, "запланирован" if res == "new" else "сдвинут",
             due.astimezone(app.s.tz).strftime("%H:%M:%S"))


async def resume_chat(app: App, chat_id: int) -> bool:
    """Владелец отпустил чат: если последние сообщения собеседника без ответа, ставим ответ с задержкой."""
    conn = await app.current_connection()
    chat = await app.repo.get_chat(chat_id)
    if conn is None or chat is None:
        return False
    history = await app.repo.recent_messages(chat_id, prompts.CONTEXT_MESSAGES)
    batch = client_batch(history)
    now = app.now()
    if not batch or now - batch[-1].date > DAY:
        return False
    due = now + timedelta(minutes=await app.get_int("delay"))
    if await blocker(app, chat, conn, due):
        return False
    await app.repo.schedule(chat_id, conn.id, due, batch[-1].message_id, now)
    log.info("чат %s: владелец отпустил, ответ запланирован", chat_id)
    return True


# --- обработка отложенного ответа ----------------------------------------------


async def process_pending(app: App, p: Pending) -> str:
    async with app.chat_lock(p.chat_id):
        if not await app.repo.claim_pending(p.id, app.now()):
            return "taken"
        status, reason = "error", "exception"
        cancelled = False
        try:
            status, reason = await _process(app, p)
        except asyncio.CancelledError:
            # Остановка контейнера посреди ответа: строка остаётся running, и при старте requeue_running
            # вернёт её в очередь. Если ответ уже ушёл, повтор увидит пустую пачку и ничего не пошлёт.
            cancelled = True
            log.info("чат %s: остановка посреди ответа, повторю после рестарта", p.chat_id)
            raise
        except Exception as e:
            log_exc(log, "чат %s: ответ секретаря упал", e, p.chat_id)
            if app.raise_errors:
                raise
        finally:
            if not cancelled:
                await app.repo.finish_pending(p.id, status, reason, app.now())
        log.info("чат %s: отложенный ответ %s (%s)", p.chat_id, status, reason)
        return status


async def _process(app: App, p: Pending) -> tuple[str, str]:
    started = app.now()
    repo = app.repo
    chat = await repo.get_chat(p.chat_id)
    conn = await app.owner_connection(p.connection_id)
    if conn is None or not conn.is_enabled:
        # Владелец переподключил бота: отвечаем через актуальное подключение.
        conn = await app.current_connection() or conn
    reason = await blocker(app, chat, conn, started)
    if reason:
        return "skipped", reason
    assert chat is not None and conn is not None
    history = await repo.recent_messages(chat.chat_id, prompts.CONTEXT_MESSAGES)
    batch = client_batch(history)
    if not batch:
        return "skipped", "no_batch"

    limit = await _limit_reason(app, chat.chat_id, started)
    if limit:
        await _limit_handoff(app, chat, limit)
        return "skipped", "limit"

    # Сначала расшифровка: текст голосового нужен и классификации нового чата, и ответу.
    if app.stt is not None:
        for m in batch:
            if m.kind in VOICE_KINDS and not m.transcript:
                await transcribe(app, m)
    unheard = await _unheard_voices(app, chat.chat_id, batch)

    if chat.ai_mode == "auto" and chat.chat_class == "unknown":
        if not any(_meaningful(m) for m in history[-prompts.CLASSIFY_MESSAGES:]):
            # Разбирать нечего (голосовое без расшифровки, стикер, фото без подписи): класс не фиксируем.
            if unheard:
                await _unknown_voice_notice(app, chat)
                return "skipped", "voice_unknown"
            return "skipped", "unclear"
        cls = await classify(app, chat, history)
        if cls is None:
            await _classify_failed(app, chat)
            return "error", "classify_failed"
        if cls == "unclear" and unheard:
            await _unknown_voice_notice(app, chat)
            return "skipped", "voice_unknown"
        if cls != "business":
            return "skipped", cls
        chat = await repo.get_chat(chat.chat_id) or chat

    if unheard:
        taken = await _taken_over(app, chat.chat_id, started, batch)
        if taken:
            return "cancelled", taken
        await _voice_handoff(app, chat, conn, history, batch, unheard)
        return "done", "voice"

    first = await _first_reply(app, chat.chat_id, started)
    ctx = Context(name=fmt.chat_name(chat), username=chat.username, chat_class=chat.chat_class,
                  now_local=started.astimezone(app.s.tz), lines=to_lines(app, history), first_reply=first)
    images = _images(app, batch)
    outcome = await decide(app, ctx, images, chat_id=chat.chat_id)
    if outcome.decision is None:
        await system_handoff(app, chat, "ИИ не смог ответить по схеме, клиенту ничего не ушло")
        await repo.update_chat(chat.chat_id, handoff_at=app.now())
        return "error", "llm_failed"
    d = prompts.finalize(outcome.decision)
    reply, handoff, why = d.reply, d.handoff, d.handoff_reason

    if reply:
        await show_typing(app, conn, chat.chat_id)
    taken = await _taken_over(app, chat.chat_id, started, batch)
    if taken:
        return "cancelled", taken

    sent: Message | None = None
    if reply:
        sent = await send_reply(app, conn, chat.chat_id, reply, typing=False, upto=batch[-1].message_id)
        if sent is None:
            await system_handoff(app, chat, "Не смог отправить ответ: чат неактивен больше 24 ч или нет прав")
            await repo.update_chat(chat.chat_id, handoff_at=app.now())
            return "error", "send_failed"
        await repo.log_ai("reply", chat.chat_id, app.llm.provider if app.llm else "", "", 0, 0, True, app.now())
        await app.notify(f"🤖 Ответил {fmt.link(chat, chat.chat_id)}: «{fmt.esc(fmt.trunc(reply, 700))}»",
                         kb.replied(chat.chat_id), silent=True)

    lead: Lead | None = None
    if d.lead and not d.lead.is_empty():
        lead = await repo.upsert_lead(chat.chat_id, d.lead.fields(), app.now())
    if handoff or d.lead_ready:
        if lead is None:
            lead = await repo.upsert_lead(chat.chat_id, {"summary": why or "Нужен Джавохир"}, app.now())
        if handoff or lead.card_sent_at is None:
            await send_lead_card(app, chat, lead, why if handoff else None)
    if handoff:
        await repo.update_chat(chat.chat_id, handoff_at=app.now())
    if sent:
        return "done", "replied"
    return "done", "handoff" if handoff else "silent"


async def _taken_over(app: App, chat_id: int, started: datetime, batch: list[StoredMessage]) -> str | None:
    """Пока шли расшифровка, модель и «печатает», владелец мог ответить сам или выключить ИИ в чате.
    Ответ владельца ищем и по времени, и по порядку сообщений: после простоя дата и время обработки расходятся."""
    fresh = await app.repo.get_chat(chat_id)
    if fresh and fresh.last_owner_msg_at and fresh.last_owner_msg_at >= started.replace(microsecond=0):
        return "owner"
    if fresh and fresh.ai_mode == "off":
        return "chat_off"
    last = batch[-1].message_id
    recent = await app.repo.recent_messages(chat_id, prompts.CONTEXT_MESSAGES)
    if any(m.is_owner and not m.is_bot_sent and not is_auto(m) and m.message_id > last for m in recent):
        return "owner"
    return None


async def _once_per(app: App, key: str, gap: timedelta) -> bool:
    """True и отметка, если с прошлого раза по ключу прошло больше gap."""
    raw = await app.repo.kv_get(key)
    now = app.now()
    if raw:
        try:
            if now - datetime.fromisoformat(raw) < gap:
                return False
        except ValueError:
            pass
    await app.repo.kv_set(key, now.isoformat())
    return True


async def _first_reply(app: App, chat_id: int, now: datetime) -> bool:
    last = await app.repo.last_bot_sent_at(chat_id)
    return last is None or now - last > FIRST_REPLY_GAP


def _images(app: App, batch: list[StoredMessage]) -> list[Part]:
    parts: list[Part] = []
    for m in batch:
        if m.kind != "photo" or len(parts) >= MAX_IMAGES:
            continue
        path = spy.abs_path(app, m.local_path)
        if path is None:
            continue
        try:
            if path.stat().st_size >= MAX_IMAGE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        parts.append({"type": "image", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()})
    return parts


# --- модель ---------------------------------------------------------------------


async def decide(app: App, ctx: Context, images: list[Part], *, chat_id: int | None, kind: str = "decide",
                 ) -> Outcome:
    """Решение модели по схеме. Поломка (ошибка API, отказ, не JSON, не по схеме): один повтор."""
    llm = app.llm
    if llm is None:
        return Outcome(None, "", "llm_off")
    system = prompts.secretary_system(app.kb.read("knowledge.md"), app.kb.read("style.md"))
    parts: list[Part] = [*images, {"type": "text", "text": prompts.secretary_user(ctx)}]
    raw, error = "", None
    for attempt in (1, 2):
        try:
            res = await llm.complete(system=system, parts=parts, schema=prompts.DECISION_SCHEMA,
                                     schema_name=prompts.DECISION_NAME, max_tokens=MAX_TOKENS)
        except LLMError as e:
            error = str(e)
            log.warning("ИИ, попытка %d: %s", attempt, e)
            await app.repo.log_ai(kind, chat_id, llm.provider, llm.model, 0, 0, False, app.now())
            continue
        await app.repo.log_ai(kind, chat_id, llm.provider, llm.model, res.input_tokens, res.output_tokens,
                              not res.refused, app.now())
        raw = res.text
        if res.refused:
            error = "refusal"
            log.warning("ИИ, попытка %d: отказ модели", attempt)
            continue
        try:
            return Outcome(prompts.parse_decision(res.text), raw, None)
        except prompts.DecisionError as e:
            error = str(e)
            log.warning("ИИ, попытка %d: %s", attempt, e)
    return Outcome(None, raw, error)


async def classify(app: App, chat: Chat, history: list[StoredMessage]) -> str | None:
    """Класс нового чата. Сомнение решает модель в пользу personal. Сбой: None, чат остаётся unknown."""
    llm = app.llm
    if llm is None:
        return None
    lines = to_lines(app, history[-prompts.CLASSIFY_MESSAGES:])
    text = prompts.classify_user(fmt.chat_name(chat), lines)
    for attempt in (1, 2):
        try:
            res = await llm.complete(system=prompts.CLASSIFY_RULES, parts=[{"type": "text", "text": text}],
                                     schema=prompts.CLASS_SCHEMA, schema_name=prompts.CLASS_NAME,
                                     max_tokens=CLASSIFY_TOKENS)
        except LLMError as e:
            log.warning("классификация %s, попытка %d: %s", chat.chat_id, attempt, e)
            await app.repo.log_ai("classify", chat.chat_id, llm.provider, llm.model, 0, 0, False, app.now())
            continue
        await app.repo.log_ai("classify", chat.chat_id, llm.provider, llm.model, res.input_tokens,
                              res.output_tokens, not res.refused, app.now())
        if res.refused:
            continue
        try:
            parsed = prompts.parse_class(res.text)
        except prompts.DecisionError as e:
            log.warning("классификация %s, попытка %d: %s", chat.chat_id, attempt, e)
            continue
        cls = parsed.chat_class
        if cls == "unclear":
            # Запроса ещё нет (одно приветствие): класс не фиксируем, следующую пачку разберём заново.
            log.info("чат %s: класс пока не ясен", chat.chat_id)
            return cls
        await app.repo.update_chat(chat.chat_id, chat_class=cls, class_reason=fmt.trunc(parsed.reason, 300))
        log.info("чат %s: класс %s", chat.chat_id, cls)
        if cls == "business" and chat.new_client_card_at is None:
            await app.repo.update_chat(chat.chat_id, new_client_card_at=app.now())
            body = f"🆕 Новый клиент: {fmt.link(chat, chat.chat_id)}"
            if parsed.reason:
                body += f"\n{fmt.esc(fmt.trunc(parsed.reason, 300))}"
            body += "\nАссистент ответит сам. Не нужно: 🙅"
            await app.notify(body, kb.new_client(chat.chat_id))
        return cls
    return None


async def transcribe(app: App, m: StoredMessage) -> str | None:
    stt = app.stt
    if stt is None:
        return None
    data: bytes | None = None
    path = spy.abs_path(app, m.local_path)
    try:
        if path is not None:
            data = path.read_bytes()
        elif m.file_id and (not m.file_size or m.file_size <= spy.MAX_DOWNLOAD):
            buf = io.BytesIO()
            await app.bot.download(m.file_id, destination=buf, timeout=60)
            data = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        log.warning("голосовое %s/%s не прочиталось: %s", m.chat_id, m.message_id, type(e).__name__)
        return None
    if not data:
        return None
    name, mime = ("voice.ogg", m.mime or "audio/ogg") if m.kind == "voice" else ("note.mp4", "video/mp4")
    try:
        text = await stt.transcribe(data, name, mime)
    except LLMError as e:
        log.warning("расшифровка %s/%s: %s", m.chat_id, m.message_id, e)
        await app.repo.log_ai("stt", m.chat_id, stt.provider, stt.model, 0, 0, False, app.now())
        return None
    await app.repo.log_ai("stt", m.chat_id, stt.provider, stt.model, 0, 0, bool(text), app.now())
    if not text:
        return None
    m.transcript = text
    await app.repo.set_transcript(m, text)
    return text


# --- отправка клиенту ------------------------------------------------------------


async def show_typing(app: App, conn: Connection, chat_id: int) -> None:
    """«Печатает…» от имени владельца на 2-4 с перед ответом."""
    try:
        await app.bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=conn.id)
    except TelegramAPIError as e:
        log.info("typing в %s не ушёл: %s", chat_id, type(e).__name__)
    lo, hi = app.typing_seconds
    await app.sleep(random.uniform(lo, hi) if hi > 0 else 0)


async def send_reply(app: App, conn: Connection, chat_id: int, text: str, *, typing: bool = True,
                     upto: int | None = None) -> Message | None:
    """От имени владельца через business_connection_id, обычным текстом без разметки.
    upto: id последнего сообщения собеседника, на которое этот ответ отвечает (граница пачки)."""
    if typing:
        await show_typing(app, conn, chat_id)
    try:
        msg = await app.bot.send_message(chat_id=chat_id, text=text, business_connection_id=conn.id,
                                         parse_mode=None)
    except TelegramAPIError as e:
        log.warning("ответ в %s не ушёл: %s", chat_id, type(e).__name__)
        return None
    stored = StoredMessage(
        connection_id=conn.id, chat_id=chat_id, message_id=msg.message_id, from_id=app.owner_id, is_owner=True,
        is_bot_sent=True, date=app.now(), kind="text", text=text, extra={"upto": upto} if upto else {})
    await app.repo.save_message(stored)
    return msg


# --- владельцу ---------------------------------------------------------------------


async def notify_card(app: App, text: str, chat: Chat, *, silent: bool = False) -> Message | None:
    """Карточка с кнопкой tg://user?id=. Telegram может отвергнуть её из-за приватности: тогда без неё."""
    if app.owner_id is None:
        return None
    markup: InlineKeyboardMarkup = kb.lead_card(chat.chat_id)
    try:
        return await app.bot.send_message(chat_id=app.owner_id, text=text, reply_markup=markup,
                                          disable_notification=silent or None)
    except TelegramBadRequest as e:
        log.info("карточка с ссылкой на чат не ушла: %s", type(e).__name__)
    except TelegramAPIError as e:
        log.warning("карточка не ушла: %s", type(e).__name__)
        return None
    return await app.notify(text, kb.lead_card(chat.chat_id, with_url=False, username=chat.username),
                            silent=silent)


def lead_text(chat: Chat, lead: Lead, reason: str | None) -> str:
    rows = [f"🔥 Лид: {fmt.link(chat, chat.chat_id)}"]
    labels = (("need", "Нужно"), ("deadline", "Срок"), ("budget", "Бюджет"), ("contact", "Контакт"),
              ("name", "Имя"), ("notes", "Заметки"))
    for key, label in labels:
        val = getattr(lead, key)
        if val:
            rows.append(f"{label}: {fmt.esc(fmt.trunc(val, 300))}")
    if reason:
        rows.append(f"Почему тебе: {fmt.esc(fmt.trunc(reason, 300))}")
    return "\n".join(rows)


async def send_lead_card(app: App, chat: Chat, lead: Lead, reason: str | None) -> None:
    await notify_card(app, lead_text(chat, lead, reason), chat)
    await app.repo.mark_lead_card(lead.id, app.now())


async def system_handoff(app: App, chat: Chat, reason: str) -> None:
    await notify_card(app, f"🙋 Нужен ты: {fmt.link(chat, chat.chat_id)}\n{fmt.esc(reason)}", chat)


async def _limit_reason(app: App, chat_id: int, now: datetime) -> str | None:
    """Лимиты на ответы клиенту и отдельно на вызовы модели: решения без ответа тоже стоят денег."""
    since, s, repo = now - DAY, app.s, app.repo
    if await repo.count_replies(since, chat_id) >= s.chat_daily_limit:
        return "chat"
    if await repo.count_replies(since) >= s.total_daily_limit:
        return "all"
    if await repo.count_calls(since, chat_id) >= s.chat_call_limit:
        return "chat_calls"
    if await repo.count_calls(since) >= s.total_call_limit:
        return "all_calls"
    return None


async def _limit_handoff(app: App, chat: Chat, which: str) -> None:
    """Лимит: клиенту ничего, владельцу одно уведомление на чат в сутки."""
    if not await _once_per(app, f"limit_note:{chat.chat_id}", LIMIT_NOTICE_GAP):
        return
    s = app.s
    reason = {
        "chat": texts.LIMIT_CHAT.format(n=s.chat_daily_limit),
        "all": texts.LIMIT_ALL.format(n=s.total_daily_limit),
        "chat_calls": texts.LIMIT_CHAT_CALLS.format(n=s.chat_call_limit),
        "all_calls": texts.LIMIT_ALL_CALLS.format(n=s.total_call_limit),
    }[which]
    log.info("чат %s: лимит ИИ (%s)", chat.chat_id, which)
    await system_handoff(app, chat, reason)


async def _classify_failed(app: App, chat: Chat) -> None:
    """ИИ не разобрал новый чат: владельцу сказать, но не чаще раза в 6 часов на чат. Класс остаётся unknown."""
    if await _once_per(app, f"classify_fail:{chat.chat_id}", CLASSIFY_FAIL_GAP):
        await system_handoff(app, chat, texts.CLASSIFY_FAILED)


_UZ_LAT = re.compile(r"\b(salom|assalomu|rahmat|qancha|narx|kerak|qilib|bormi|yaxshi|iltimos|men|siz)\b|o'|g'|oʻ|gʻ",
                     re.I)
_UZ_CYR = re.compile(r"[ўқғҳ]", re.I)
_CYR = re.compile(r"[а-яё]", re.I)


def guess_language(texts_: list[str]) -> str:
    joined = " ".join(texts_)
    if _UZ_CYR.search(joined):
        return "uz_cyr"
    if _UZ_LAT.search(joined):
        return "uz"
    if _CYR.search(joined):
        return "ru"
    if re.search(r"[a-z]", joined, re.I):
        return "en"
    return "ru"


VOICE_UZ_CYR = "Овозли хабарни олдим, Жавоҳирга етказаман."
VOICE_INTRO_UZ_CYR = ("Ассалому алайкум, мен Жавоҳирнинг ассистентиман, у ҳозир банд. "
                      "Овозли хабарни олдим, унга етказаман.")


def voice_text(lang: str, intro: bool) -> str:
    if lang == "uz_cyr":
        return VOICE_INTRO_UZ_CYR if intro else VOICE_UZ_CYR
    table = texts.VOICE_INTRO if intro else texts.VOICE_RECEIVED
    return table.get(lang, table["ru"])


async def _unheard_voices(app: App, chat_id: int, batch: list[StoredMessage]) -> list[StoredMessage]:
    """Голосовые пачки без расшифровки, о которых владельцу ещё не сообщали."""
    raw = await app.repo.kv_get(f"voice_seen:{chat_id}")
    seen = int(raw) if raw and raw.isdigit() else 0
    return [m for m in batch if m.kind in VOICE_KINDS and not m.transcript and m.message_id > seen]


async def _voice_handoff(app: App, chat: Chat, conn: Connection, history: list[StoredMessage],
                         batch: list[StoredMessage], voices: list[StoredMessage]) -> None:
    """Расшифровки нет: клиенту один раз «голосовое получил» (первым сообщением ещё и представиться),
    владельцу уведомление. Эти голосовые больше не повод для handoff: следующий текст клиента ответит ИИ."""
    now = app.now()
    await app.repo.kv_set(f"voice_seen:{chat.chat_id}", str(max(m.message_id for m in voices)))
    if chat.voice_notice_at is None or now - chat.voice_notice_at > VOICE_NOTICE_GAP:
        lang = guess_language([m.text for m in history if not m.is_owner and m.kind == "text" and m.text])
        text = voice_text(lang, await _first_reply(app, chat.chat_id, now))
        if await send_reply(app, conn, chat.chat_id, text, upto=batch[-1].message_id):
            await app.repo.update_chat(chat.chat_id, voice_notice_at=now)
    await system_handoff(app, chat, "Голосовое сообщение, расшифровки нет. Послушай сам.")


async def _unknown_voice_notice(app: App, chat: Chat) -> None:
    """Голосовое из неразобранного чата: клиенту ничего (вдруг это друг), владельцу вопрос с кнопками."""
    if await _once_per(app, f"unknown_voice:{chat.chat_id}", VOICE_NOTICE_GAP):
        await app.notify(texts.UNKNOWN_VOICE.format(who=fmt.link(chat, chat.chat_id)), kb.new_client(chat.chat_id))


# --- песочница ----------------------------------------------------------------------


async def sandbox_turn(app: App, text: str) -> Outcome:
    """Тот же движок, без задержки и отправки клиенту. История только в памяти."""
    if app.sandbox is None:
        app.sandbox = []
    now = app.now()
    app.sandbox.append(Line("client", now.astimezone(app.s.tz), text))
    lines = app.sandbox[-prompts.CONTEXT_MESSAGES:]
    first = not any(ln.who == "assistant" for ln in lines)
    ctx = Context(name="Тестовый клиент", username=None, chat_class="business",
                  now_local=now.astimezone(app.s.tz), lines=lines, first_reply=first)
    outcome = await decide(app, ctx, [], chat_id=None, kind="test")
    if outcome.decision is not None:
        # Показываем то, что ушло бы клиенту: та же постобработка, что в бою.
        outcome.decision = prompts.finalize(outcome.decision)
        if outcome.decision.reply:
            app.sandbox.append(Line("assistant", app.now().astimezone(app.s.tz), outcome.decision.reply))
    return outcome


def decision_json(d: Decision) -> str:
    data: dict[str, Any] = d.model_dump()
    return json.dumps(data, ensure_ascii=False, indent=1)
