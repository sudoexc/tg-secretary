"""Шпион: архив личных чатов владельца, уведомления о правках и удалениях, локальные копии медиа."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, BusinessMessagesDeleted, FSInputFile, Message

from . import fmt
from .app import App
from .storage import Chat, Connection, StoredMessage

log = logging.getLogger(__name__)

DOWNLOAD_KINDS = frozenset({"photo", "voice", "video_note", "sticker", "document", "video"})
MAX_DOWNLOAD = 20 * 1024 * 1024  # лимит getFile у Bot API
BULK_THRESHOLD = 5
CAPTION_LIMIT = 1024
# Признаки одноразовых и самоуничтожающихся медиа, если Telegram их пришлёт (поля вне текущей схемы).
EPHEMERAL_KEYS = ("ttl_seconds", "ttl_period", "self_destruct", "self_destruct_in", "view_once", "is_view_once",
                  "has_timer", "has_ttl")
# Ключи extra, которые правка сообщения не стирает: автоответ Telegram и граница пачки нашего ответа.
STICKY_EXTRA = ("offline", "upto")
EXT = {"photo": ".jpg", "voice": ".ogg", "video_note": ".mp4", "video": ".mp4"}
MEDIA_LABEL = {
    "photo": "фото", "video": "видео", "voice": "голосовое", "video_note": "кружок", "audio": "аудио",
    "document": "файл", "sticker": "стикер", "animation": "гиф",
}


@dataclass
class Extracted:
    kind: str
    text: str | None = None
    file_id: str | None = None
    file_unique_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Incoming:
    conn: Connection
    chat_id: int
    message_id: int
    date: datetime
    stored: StoredMessage | None
    is_owner: bool
    bot_sent: bool
    from_offline: bool
    from_bot: bool
    ephemeral: bool


def _flag(obj: Any) -> bool:
    extra = getattr(obj, "model_extra", None) or {}
    return any(extra.get(k) for k in EPHEMERAL_KEYS)


def is_ephemeral(msg: Message) -> bool:
    """Защищённое от пересылки, одноразовое или с таймером: не храним и не пересылаем."""
    if msg.has_protected_content:
        return True
    if _flag(msg):
        return True
    media = [msg.photo[-1] if msg.photo else None, msg.video, msg.voice, msg.video_note, msg.document,
             msg.animation, msg.audio]
    return any(m is not None and _flag(m) for m in media)


def _entities_hash(msg: Message) -> str | None:
    ents = msg.entities or msg.caption_entities
    if not ents:
        return None
    raw = json.dumps([[e.type, e.offset, e.length, e.url or "", e.custom_emoji_id or ""] for e in ents])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def extract(msg: Message) -> Extracted:
    text = msg.text if msg.text is not None else msg.caption
    ex = Extracted(kind="other", text=text)
    if msg.text is not None:
        ex.kind = "text"
    elif msg.photo:
        p = msg.photo[-1]
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "photo", p.file_id, p.file_unique_id, p.file_size
        ex.mime = "image/jpeg"
    elif msg.video:
        v = msg.video
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "video", v.file_id, v.file_unique_id, v.file_size
        ex.file_name, ex.mime = v.file_name, v.mime_type
    elif msg.voice:
        v = msg.voice
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "voice", v.file_id, v.file_unique_id, v.file_size
        ex.mime = v.mime_type or "audio/ogg"
    elif msg.video_note:
        v = msg.video_note
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "video_note", v.file_id, v.file_unique_id, v.file_size
        ex.mime = "video/mp4"
    elif msg.audio:
        a = msg.audio
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "audio", a.file_id, a.file_unique_id, a.file_size
        ex.file_name, ex.mime = a.file_name, a.mime_type
    elif msg.animation:  # у гифок Bot API заполняет и document, поэтому раньше него
        a = msg.animation
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "animation", a.file_id, a.file_unique_id, a.file_size
        ex.file_name, ex.mime = a.file_name, a.mime_type
    elif msg.document:
        d = msg.document
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "document", d.file_id, d.file_unique_id, d.file_size
        ex.file_name, ex.mime = d.file_name, d.mime_type
    elif msg.sticker:
        s = msg.sticker
        ex.kind, ex.file_id, ex.file_unique_id, ex.file_size = "sticker", s.file_id, s.file_unique_id, s.file_size
        ex.text = s.emoji
        ex.extra["sticker"] = "tgs" if s.is_animated else ("webm" if s.is_video else "webp")
    elif msg.location or msg.venue:
        loc = msg.location or (msg.venue.location if msg.venue else None)
        ex.kind = "location"
        if loc is not None:
            ex.extra.update(lat=loc.latitude, lon=loc.longitude)
        if msg.venue:
            ex.extra["title"] = msg.venue.title
    elif msg.contact:
        c = msg.contact
        ex.kind = "contact"
        ex.extra.update(name=" ".join(x for x in (c.first_name, c.last_name) if x), phone=c.phone_number)
    elif msg.poll:
        ex.extra["poll"] = msg.poll.question
    ents = _entities_hash(msg)
    if ents:
        ex.extra["ents"] = ents
    return ex


def counterpart_name(msg_chat: Any) -> tuple[str | None, str | None]:
    name = " ".join(x for x in (getattr(msg_chat, "first_name", None), getattr(msg_chat, "last_name", None)) if x)
    return (name or None), getattr(msg_chat, "username", None)


def describe(m: StoredMessage, *, for_ai: bool = False) -> str:
    """Сообщение одной строкой: текст или [тип: подпись]."""
    if m.kind == "text":
        return m.text or ""
    if m.kind == "location":
        lat, lon = m.extra.get("lat"), m.extra.get("lon")
        title = m.extra.get("title")
        if for_ai:
            return f"[геопозиция{': ' + title if title else ''}]"
        where = f"{lat}, {lon}" if lat is not None else "?"
        return f"📍 {title + ', ' if title else ''}{where}"
    if m.kind == "contact":
        return f"[контакт: {m.extra.get('name') or ''} {m.extra.get('phone') or ''}]".replace("  ", " ")
    if m.kind == "other":
        if m.extra.get("poll"):
            return f"[опрос: {m.extra['poll']}]"
        return "[сообщение другого типа]" + (f" {m.text}" if m.text else "")
    label = MEDIA_LABEL.get(m.kind, m.kind)
    if m.kind == "document" and m.file_name:
        label += f" {m.file_name}"
    if m.kind in ("voice", "video_note") and for_ai:
        if m.transcript:
            return f"[{label}, расшифровка: {m.transcript}]"
        return f"[{label} без расшифровки]"
    body = m.text or ""
    return f"[{label}: {body}]" if body else f"[{label}]"


def _date(msg: Message) -> datetime:
    d = msg.date
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(int(d), tz=timezone.utc)


def _stored(conn: Connection, msg: Message, ex: Extracted, is_owner: bool, bot_sent: bool) -> StoredMessage:
    if msg.is_from_offline:
        # Приветствие или «нет на месте» из Telegram Business: не ответ владельца, пачку клиента не закрывает.
        ex.extra["offline"] = True
    return StoredMessage(
        connection_id=conn.id, chat_id=msg.chat.id, message_id=msg.message_id,
        from_id=msg.from_user.id if msg.from_user else None, is_owner=is_owner, is_bot_sent=bot_sent,
        date=_date(msg), kind=ex.kind, text=ex.text, file_id=ex.file_id, file_unique_id=ex.file_unique_id,
        file_size=ex.file_size, file_name=ex.file_name, mime=ex.mime, extra=ex.extra,
        reply_to=msg.reply_to_message.message_id if msg.reply_to_message else None,
    )


async def _resolve(app: App, msg: Message) -> tuple[Connection, bool, bool] | None:
    """Апдейт владельца из лички? (подключение, от владельца, отправлен ботом). Чужое: None."""
    conn = await app.owner_connection(msg.business_connection_id)
    if conn is None:
        return None
    if msg.chat.type != "private" or app.is_owner(msg.chat.id):
        return None
    from_id = msg.from_user.id if msg.from_user else None
    return conn, app.is_owner(from_id), msg.sender_business_bot is not None


# --- новые сообщения ----------------------------------------------------------


async def on_message(app: App, msg: Message) -> Incoming | None:
    resolved = await _resolve(app, msg)
    if resolved is None:
        return None
    conn, is_owner, bot_sent = resolved
    chat_id, now = msg.chat.id, app.now()
    name, username = counterpart_name(msg.chat)
    from_bot = bool(msg.from_user and msg.from_user.is_bot and not is_owner)
    await app.repo.touch_chat(chat_id, name, username, now, is_bot=None if is_owner else from_bot)
    base = dict(conn=conn, chat_id=chat_id, message_id=msg.message_id, date=_date(msg),
                is_owner=is_owner or bot_sent, bot_sent=bot_sent, from_offline=bool(msg.is_from_offline),
                from_bot=from_bot)
    if is_ephemeral(msg):
        log.info("сообщение %s/%s защищено или одноразовое: не храним", chat_id, msg.message_id)
        if not (is_owner or bot_sent):
            await _notify_ephemeral(app, msg, chat_id)
        return Incoming(stored=None, ephemeral=True, **base)
    ex = extract(msg)
    stored = _stored(conn, msg, ex, is_owner or bot_sent, bot_sent)
    await app.repo.save_message(stored)
    log.info("сообщение %s/%s: %s, %s", chat_id, msg.message_id, ex.kind,
             "бот" if bot_sent else ("владелец" if is_owner else "собеседник"))
    if not stored.is_owner or await app.get_bool("spy_mine"):
        await download(app, stored)
    return Incoming(stored=stored, ephemeral=False, **base)


EPHEMERAL_LABEL = (("photo", "фото"), ("video", "видео"), ("video_note", "кружок"), ("voice", "голосовое"),
                   ("animation", "гиф"), ("document", "файл"), ("audio", "аудио"))


async def _notify_ephemeral(app: App, msg: Message, chat_id: int) -> None:
    """Только факт и тип: само содержимое одноразового или защищённого сообщения не берём."""
    label = next((name for attr, name in EPHEMERAL_LABEL if getattr(msg, attr, None)), "сообщение")
    how = "с защитой от сохранения" if msg.has_protected_content else "на один просмотр"
    who = fmt.link(await app.repo.get_chat(chat_id), chat_id)
    at = fmt.when(_date(msg), app.s.tz, app.now())
    await app.notify(f"📸 {who} прислал(а) {label} {how} · {at}\nСодержимое не сохраняется.")


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", part)[:40] or "f"


def media_path(app: App, m: StoredMessage) -> Path:
    if m.kind == "sticker":
        ext = "." + str(m.extra.get("sticker") or "webp")
    elif m.kind == "document":
        suffix = Path(m.file_name or "").suffix
        ext = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix or "") else ".bin"
    else:
        ext = EXT.get(m.kind, ".bin")
    return Path("media") / str(m.chat_id) / f"{m.message_id}_{_safe(m.file_unique_id or 'x')}{ext}"


def abs_path(app: App, rel: str | None) -> Path | None:
    if not rel:
        return None
    p = app.s.data_dir / rel
    return p if p.is_file() else None


async def download(app: App, m: StoredMessage) -> str | None:
    """Копия медиа в DATA_DIR/media. Любая ошибка только в лог: архив и секретарь работают дальше."""
    if m.kind not in DOWNLOAD_KINDS or not m.file_id:
        return None
    if m.file_size and m.file_size > MAX_DOWNLOAD:
        return None
    rel = media_path(app, m)
    target = app.s.data_dir / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        await app.bot.download(m.file_id, destination=target, timeout=60)
    except Exception as e:  # noqa: BLE001  сеть, 400 «file is too big», диск
        log.warning("медиа %s/%s не скачалось: %s", m.chat_id, m.message_id, type(e).__name__)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    m.local_path = str(rel)
    await app.repo.set_local_path(m, str(rel), app.now())
    return str(rel)


# --- пересылка медиа владельцу -----------------------------------------------


async def _send_media_once(app: App, m: StoredMessage, src: Any, caption: str | None) -> bool:
    bot, chat = app.bot, app.owner_id
    k = m.kind
    if k == "photo":
        await bot.send_photo(chat_id=chat, photo=src, caption=caption)
    elif k == "video":
        await bot.send_video(chat_id=chat, video=src, caption=caption)
    elif k == "voice":
        await bot.send_voice(chat_id=chat, voice=src, caption=caption)
    elif k == "video_note":
        await bot.send_video_note(chat_id=chat, video_note=src)
    elif k == "audio":
        await bot.send_audio(chat_id=chat, audio=src, caption=caption)
    elif k == "document":
        await bot.send_document(chat_id=chat, document=src, caption=caption)
    elif k == "animation":
        await bot.send_animation(chat_id=chat, animation=src, caption=caption)
    elif k == "sticker":
        await bot.send_sticker(chat_id=chat, sticker=src)
    else:
        return False
    return True


async def send_media(app: App, m: StoredMessage, caption: str) -> bool:
    """Медиа владельцу: по file_id, при ошибке локальным файлом. False, если не вышло ни так ни так."""
    if app.owner_id is None or not m.file_id:
        return False
    no_caption = m.kind in ("sticker", "video_note")
    cap = None if no_caption else caption
    if no_caption:
        await app.notify(caption)
    try:
        return await _send_media_once(app, m, m.file_id, cap)
    except TelegramAPIError as e:
        log.info("медиа %s/%s по file_id не ушло: %s", m.chat_id, m.message_id, type(e).__name__)
    local = abs_path(app, m.local_path)
    if local is None:
        return False
    try:
        return await _send_media_once(app, m, FSInputFile(local), cap)
    except TelegramAPIError as e:
        log.warning("медиа %s/%s локальным файлом не ушло: %s", m.chat_id, m.message_id, type(e).__name__)
        return False


def _caption(header: str, body: str | None) -> str:
    if not body:
        return header
    room = CAPTION_LIMIT - len(header) - 40
    return f"{header}\n{fmt.quote(body, max(100, room))}"


# --- правки -------------------------------------------------------------------


async def on_edit(app: App, msg: Message) -> None:
    resolved = await _resolve(app, msg)
    if resolved is None:
        return
    conn, is_owner, bot_sent = resolved
    is_owner = is_owner or bot_sent
    chat_id, now = msg.chat.id, app.now()
    name, username = counterpart_name(msg.chat)
    await app.repo.touch_chat(chat_id, name, username, now)
    old = await app.repo.find_message(chat_id, msg.message_id)
    if is_ephemeral(msg):
        if old is not None:
            path = abs_path(app, old.local_path)
            if path:
                path.unlink(missing_ok=True)
            await app.repo.drop_message(chat_id, msg.message_id)
        log.info("правка %s/%s защищена: не храним", chat_id, msg.message_id)
        return
    ex = extract(msg)
    new = _stored(conn, msg, ex, is_owner, bot_sent)
    if old is not None:
        for key in STICKY_EXTRA:
            if key in old.extra:
                new.extra.setdefault(key, old.extra[key])
    edited_at = datetime.fromtimestamp(msg.edit_date, tz=timezone.utc) if msg.edit_date else now
    if old is None:
        await app.repo.save_message(new)
        log.info("правка %s/%s: старой версии нет", chat_id, msg.message_id)
        if not is_owner or await app.get_bool("spy_mine"):
            await download(app, new)
        change = "missing"
    else:
        same_text = (old.text or "") == (new.text or "")
        same_media = (old.file_unique_id or None) == (new.file_unique_id or None)
        same_ents = old.extra.get("ents") == new.extra.get("ents")
        if same_text and same_media and same_ents:
            return  # Telegram прислал ту же версию: ни записи, ни уведомления
        new.version = await app.repo.apply_edit(old, new, edited_at)
        new.connection_id = old.connection_id
        change = "text" if not same_text else ("media" if not same_media else "markup")
        log.info("правка %s/%s: %s, версия %s", chat_id, msg.message_id, change, new.version)
        if not same_media and (not is_owner or await app.get_bool("spy_mine")):
            await download(app, new)
    if not await app.get_bool("spy_edits"):
        return
    if is_owner and not await app.get_bool("spy_mine"):
        return
    chat = await app.repo.get_chat(chat_id)
    await _notify_edit(app, chat, chat_id, is_owner, old, new, change, edited_at)


async def _notify_edit(app: App, chat: Chat | None, chat_id: int, is_owner: bool, old: StoredMessage | None,
                       new: StoredMessage, change: str, edited_at: datetime) -> None:
    tz, now = app.s.tz, app.now()
    who = fmt.link(chat, chat_id)
    at = fmt.when(edited_at, tz, now)
    if is_owner:
        head = f"✏️ Ты изменил(а) сообщение в чате с {who} · {at}"
    else:
        head = f"✏️ {who} изменил(а) сообщение · {at}"
    new_text = describe(new)
    if change == "missing" or old is None:
        await app.notify(f"{head}\nСтарой версии нет в архиве.\nСтало:\n{fmt.quote(new_text)}")
        return
    old_text = describe(old)
    if change == "text":
        await app.notify(f"{head}\nБыло:\n{fmt.quote(old_text or '(без текста)')}\n"
                         f"Стало:\n{fmt.quote(new_text or '(без текста)')}")
    elif change == "media":
        body = f"{head}\nЗаменено медиа, текст тот же."
        if new.text:
            body += "\n" + fmt.quote(new.text)
        await app.notify(body)
        if old.file_id:
            await send_media(app, old, "Было:")
    else:
        body = f"{head}\nИзменилась только разметка, текст тот же."
        if new.text:
            body += "\n" + fmt.quote(new.text)
        await app.notify(body)


# --- удаления -----------------------------------------------------------------


async def on_deleted(app: App, ev: BusinessMessagesDeleted) -> None:
    conn = await app.owner_connection(ev.business_connection_id)
    if conn is None:
        return
    if ev.chat.type != "private" or app.is_owner(ev.chat.id):
        return
    chat_id, now = ev.chat.id, app.now()
    name, username = counterpart_name(ev.chat)
    await app.repo.touch_chat(chat_id, name, username, now)
    found: list[StoredMessage] = []
    missing = 0
    for mid in ev.message_ids:
        m = await app.repo.find_message(chat_id, mid)
        if m is None:
            missing += 1
        elif m.deleted_at is None:
            found.append(m)
    await app.repo.mark_deleted(chat_id, list(ev.message_ids), now)
    log.info("удалено в %s: %d в архиве, %d нет", chat_id, len(found), missing)
    if not await app.get_bool("spy_deletes"):
        return
    show_mine = await app.get_bool("spy_mine")
    visible = [m for m in found if show_mine or not m.is_owner]
    chat = await app.repo.get_chat(chat_id)
    if len(visible) > BULK_THRESHOLD:
        await _notify_bulk(app, chat, chat_id, visible, missing)
        return
    for m in visible:
        await _notify_one(app, chat, chat_id, m)
    if missing:
        who = fmt.link(chat, chat_id)
        await app.notify(f"🗑 В чате с {who} удалено {missing} {_plural(missing)}, которых нет в архиве")


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "сообщение"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "сообщения"
    return "сообщений"


def _deleted_header(app: App, chat: Chat | None, chat_id: int, m: StoredMessage) -> str:
    who = fmt.link(chat, chat_id)
    at = fmt.when(m.date, app.s.tz, app.now())
    if m.is_owner:
        return f"🗑 Удалено твоё сообщение в чате с {who} · отправлено {at}"
    return f"🗑 {who} удалил(а) · отправлено {at}"


async def _notify_one(app: App, chat: Chat | None, chat_id: int, m: StoredMessage) -> None:
    header = _deleted_header(app, chat, chat_id, m)
    if m.kind in DOWNLOAD_KINDS or m.kind in ("audio", "animation"):
        if await send_media(app, m, _caption(header, m.text)):
            return
        body = f"{header}\n{fmt.esc(describe(m))}\nМедиа не сохранилось."
        await app.notify(body)
        return
    await app.notify(f"{header}\n{fmt.quote(describe(m))}")


async def _notify_bulk(app: App, chat: Chat | None, chat_id: int, items: list[StoredMessage],
                       missing: int) -> None:
    tz = app.s.tz
    name = fmt.chat_name(chat, chat_id)
    lines = [f"Удалённые сообщения: чат с {name}" + (f" (@{chat.username})" if chat and chat.username else "")
             + f", id {chat_id}", f"Удалено: {fmt.full_dt(app.now(), tz)}", ""]
    for m in sorted(items, key=lambda x: (x.date, x.message_id)):
        author = "Ты" if m.is_owner else name
        lines.append(f"[{fmt.full_dt(m.date, tz)}] {author}: {describe(m)}")
    if missing:
        lines += ["", f"И ещё {missing} {_plural(missing)}, которых нет в архиве."]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    stamp = app.now_local().strftime("%Y%m%d-%H%M")
    caption = (f"🗑 В чате с {fmt.link(chat, chat_id)} удалено разом {len(items) + missing} "
               f"{_plural(len(items) + missing)}, похоже на очистку чата. Все удалённые в файле.")
    if app.owner_id is None:
        return
    try:
        await app.bot.send_document(chat_id=app.owner_id,
                                    document=BufferedInputFile(data, filename=f"deleted-{chat_id}-{stamp}.txt"),
                                    caption=caption)
    except TelegramAPIError as e:
        log.warning("сводка удалений не ушла: %s", type(e).__name__)
