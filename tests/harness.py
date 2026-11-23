"""Оффлайн-мок транспорта aiogram и сборщик апдейтов Telegram Business. В сеть ничего не ходит.

Ответы собираются в JSON и проходят через настоящий BaseSession.check_response,
поэтому типы и ошибки Telegram такие же, как в бою.
"""
from __future__ import annotations

import itertools
import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    Audio,
    BusinessBotRights,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    Chat,
    Document,
    Message,
    PhotoSize,
    Sticker,
    Update,
    User,
    Video,
    VideoNote,
    Voice,
)

if TYPE_CHECKING:
    from aiogram import Bot

BOT_ID = 880000
TOKEN = f"{BOT_ID}:TEST-ONLY-TOKEN-NOT-REAL"
OWNER = 5762026796
CLIENT = 900001
FRIEND = 900002
STRANGER = 900003
TZ = ZoneInfo("Asia/Tashkent")
BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "Kotib", "username": "kotib_test_bot"}

FORBIDDEN_24H = "Bad Request: BUSINESS_PEER_USAGE_MISSING"
# Один счётчик id на входящие и наши ответы: в аккаунте id сообщений растут монотонно.
MESSAGE_IDS = itertools.count(1000)


class FakeClock:
    """Часы задаются в ташкентском времени, отдают UTC."""

    def __init__(self, local: str = "2026-11-23 14:00") -> None:
        self._now = datetime.now(timezone.utc)
        self.set(local)

    def set(self, local: str) -> None:
        fmt = "%Y-%m-%d %H:%M:%S" if local.count(":") == 2 else "%Y-%m-%d %H:%M"
        self._now = datetime.strptime(local, fmt).replace(tzinfo=TZ).astimezone(timezone.utc)

    def advance(self, minutes: float = 0, seconds: float = 0, hours: float = 0, days: float = 0) -> None:
        self._now += timedelta(minutes=minutes, seconds=seconds, hours=hours, days=days)

    def __call__(self) -> datetime:
        return self._now


@dataclass
class ApiCall:
    method: str
    data: dict[str, Any]
    obj: Any
    result: Any = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def text(self) -> str:
        return str(self.data.get("text") or self.data.get("caption") or "")

    @property
    def chat_id(self) -> int | None:
        raw = self.data.get("chat_id")
        return int(raw) if raw is not None else None

    @property
    def buttons(self) -> list[dict[str, Any]]:
        markup = self.data.get("reply_markup")
        if not isinstance(markup, dict):
            return []
        return [b for row in markup.get("inline_keyboard") or [] for b in row]

    def button(self, needle: str) -> dict[str, Any]:
        for b in self.buttons:
            if needle in b["text"]:
                return b
        raise AssertionError(f"кнопки {needle!r} нет: {[b['text'] for b in self.buttons]}")


@dataclass
class ErrorRule:
    method: str
    description: str
    status: int = 400
    times: int = 1  # -1 = всегда
    when: Callable[[dict[str, Any]], bool] | None = None


class MockSession(BaseSession):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.calls: list[ApiCall] = []
        self.rules: list[ErrorRule] = []
        self.files: dict[str, bytes] = {}  # file_id -> содержимое для download
        self.connections: dict[str, dict[str, Any]] = {}  # для getBusinessConnection
        self._ids = MESSAGE_IDS
        self.downloads: list[str] = []
        self.updates: list[list[dict[str, Any]]] = []  # пачки для getUpdates

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True,
                             ) -> AsyncGenerator[bytes, None]:
        file_id = url.rsplit("/", 1)[-1]
        self.downloads.append(file_id)
        yield self.files.get(file_id, b"MEDIA-" + file_id.encode())

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> Any:
        name = method.__api_method__
        data = method.model_dump(warnings=False)
        call = ApiCall(name, data, method)
        self.calls.append(call)
        for rule in self.rules:
            if rule.method == name and rule.times != 0 and (rule.when is None or rule.when(data)):
                rule.times -= 1
                payload = {"ok": False, "error_code": rule.status, "description": rule.description}
                self.check_response(bot, method, rule.status, json.dumps(payload))
        if name == "getBusinessConnection" and data["business_connection_id"] not in self.connections:
            payload = {"ok": False, "error_code": 400, "description": "Bad Request: BUSINESS_CONNECTION_INVALID"}
            self.check_response(bot, method, 400, json.dumps(payload))
        payload = {"ok": True, "result": self._result(name, data, method)}
        response = self.check_response(bot, method, 200, json.dumps(payload, default=str))
        call.result = response.result
        return response.result

    def fail(self, method: str, description: str, *, status: int = 400, times: int = 1,
             when: Callable[[dict[str, Any]], bool] | None = None) -> None:
        self.rules.append(ErrorRule(method, description, status, times, when))

    def _message(self, data: dict[str, Any], **extra: Any) -> dict[str, Any]:
        cid = int(data.get("chat_id") or 0)
        msg: dict[str, Any] = {
            "message_id": next(self._ids),
            "date": int(self.clock().timestamp()),
            "chat": {"id": cid, "type": "private", "first_name": "U"},
            "from": BOT_USER,
        }
        if data.get("business_connection_id"):
            msg["business_connection_id"] = data["business_connection_id"]
            msg["from"] = {"id": OWNER, "is_bot": False, "first_name": "Javohir"}
            msg["sender_business_bot"] = BOT_USER
        msg.update({k: v for k, v in extra.items() if v is not None})
        return msg

    def _result(self, name: str, data: dict[str, Any], method: Any) -> Any:
        if name == "getMe":
            return BOT_USER
        if name == "getFile":
            fid = data["file_id"]
            return {"file_id": fid, "file_unique_id": f"u-{fid}", "file_size": 10, "file_path": f"files/{fid}"}
        if name == "getBusinessConnection":
            return self.connections[data["business_connection_id"]]
        if name == "getUpdates":
            return self.updates.pop(0) if self.updates else []
        if name in ("sendMessage", "editMessageText"):
            return self._message(data, text=data.get("text"))
        if name in ("sendPhoto",):
            return self._message(data, photo=[{"file_id": "sent", "file_unique_id": "sent", "width": 1,
                                               "height": 1}], caption=data.get("caption"))
        if name == "sendDocument":
            fn = getattr(getattr(method, "document", None), "filename", None) or "file"
            return self._message(data, caption=data.get("caption"),
                                 document={"file_id": "doc", "file_unique_id": "doc", "file_name": fn})
        if name in ("sendVoice", "sendVideo", "sendVideoNote", "sendSticker", "sendAudio", "sendAnimation"):
            return self._message(data, text=".")
        if name == "editMessageReplyMarkup":
            return self._message(data, text=".")
        return True

    # --- инспекция ------------------------------------------------------------

    def of(self, *methods: str, chat_id: int | None = None) -> list[ApiCall]:
        return [c for c in self.calls if c.method in methods and (chat_id is None or c.chat_id == chat_id)]

    def to_owner(self, *methods: str) -> list[ApiCall]:
        ms = methods or ("sendMessage", "sendPhoto", "sendDocument", "sendVoice", "sendVideo", "sendVideoNote",
                         "sendSticker", "sendAudio", "sendAnimation")
        return [c for c in self.of(*ms, chat_id=OWNER) if not c.get("business_connection_id")]

    def owner_texts(self) -> list[str]:
        return [c.text for c in self.to_owner()]

    def to_client(self, chat_id: int = CLIENT) -> list[ApiCall]:
        return [c for c in self.of("sendMessage", chat_id=chat_id) if c.get("business_connection_id")]

    def clear(self) -> None:
        self.calls.clear()


class TG:
    """Сборщик входящих апдейтов: подключение, сообщения Business, правки, удаления, личка и кнопки."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self._update_ids = itertools.count(1)
        self._msg_ids = MESSAGE_IDS
        self._cb_ids = itertools.count(1)

    @staticmethod
    def user(uid: int, name: str = "User", username: str | None = None, is_bot: bool = False) -> User:
        return User(id=uid, is_bot=is_bot, first_name=name, username=username, language_code="ru")

    @staticmethod
    def chat_of(user: User) -> Chat:
        return Chat(id=user.id, type="private", first_name=user.first_name, username=user.username)

    def _uid(self) -> int:
        return next(self._update_ids)

    def connection(self, user: User, conn_id: str = "bc1", *, can_reply: bool = True, enabled: bool = True,
                   legacy: bool = False) -> Update:
        kwargs: dict[str, Any] = {}
        if legacy:
            kwargs["can_reply"] = can_reply
        else:
            kwargs["rights"] = BusinessBotRights(can_reply=can_reply, can_read_messages=True)
        bc = BusinessConnection(id=conn_id, user=user, user_chat_id=user.id, date=self.clock(), is_enabled=enabled,
                                **kwargs)
        return Update(update_id=self._uid(), business_connection=bc)

    @staticmethod
    def connection_json(user: User, conn_id: str, can_reply: bool = True) -> dict[str, Any]:
        return {"id": conn_id, "user": {"id": user.id, "is_bot": False, "first_name": user.first_name},
                "user_chat_id": user.id, "date": 1, "is_enabled": True, "rights": {"can_reply": can_reply}}

    def _content(self, kind: str, text: str | None, caption: str | None, file_id: str | None,
                 size: int | None, media_extra: dict[str, Any] | None) -> dict[str, Any]:
        fid = file_id or f"f{next(self._msg_ids)}"
        mx = media_extra or {}
        if kind == "text":
            return {"text": text or ""}
        if kind == "photo":
            return {"photo": [PhotoSize(file_id=fid + "s", file_unique_id=fid + "us", width=90, height=90),
                              PhotoSize(file_id=fid, file_unique_id="u" + fid, width=1280, height=960,
                                        file_size=size, **mx)], "caption": caption}
        if kind == "voice":
            return {"voice": Voice(file_id=fid, file_unique_id="u" + fid, duration=3, file_size=size,
                                   mime_type="audio/ogg", **mx)}
        if kind == "video_note":
            return {"video_note": VideoNote(file_id=fid, file_unique_id="u" + fid, length=240, duration=3,
                                            file_size=size, **mx)}
        if kind == "video":
            return {"video": Video(file_id=fid, file_unique_id="u" + fid, width=1, height=1, duration=3,
                                   file_size=size, **mx), "caption": caption}
        if kind == "document":
            return {"document": Document(file_id=fid, file_unique_id="u" + fid, file_name="brief.pdf",
                                         file_size=size, **mx), "caption": caption}
        if kind == "audio":
            return {"audio": Audio(file_id=fid, file_unique_id="u" + fid, duration=3, file_size=size)}
        if kind == "sticker":
            return {"sticker": Sticker(file_id=fid, file_unique_id="u" + fid, type="regular", width=1, height=1,
                                       is_animated=False, is_video=False, emoji="😀")}
        raise ValueError(kind)

    def bmsg(self, peer: User, sender: User, text: str | None = None, *, conn: str = "bc1", kind: str = "text",
             caption: str | None = None, file_id: str | None = None, size: int | None = None,
             protected: bool = False, msg_extra: dict[str, Any] | None = None,
             media_extra: dict[str, Any] | None = None, bot_sent: bool = False, offline: bool = False,
             mid: int | None = None) -> Update:
        """business_message в чате с peer; sender = peer (входящее) или владелец (исходящее)."""
        content = self._content(kind, text, caption, file_id, size, media_extra)
        msg = Message(
            message_id=mid or next(self._msg_ids), date=self.clock(), chat=self.chat_of(peer), from_user=sender,
            business_connection_id=conn, has_protected_content=protected or None,
            sender_business_bot=User(**{k: v for k, v in BOT_USER.items()}) if bot_sent else None,
            is_from_offline=offline or None, **{k: v for k, v in content.items() if v is not None},
            **(msg_extra or {}))
        return Update(update_id=self._uid(), business_message=msg)

    def bedit(self, peer: User, sender: User, mid: int, text: str | None = None, *, conn: str = "bc1",
              kind: str = "text", caption: str | None = None, file_id: str | None = None,
              protected: bool = False, entities: list[dict[str, Any]] | None = None,
              sent_at: datetime | None = None) -> Update:
        content = self._content(kind, text, caption, file_id, None, None)
        if entities is not None:
            content["entities" if kind == "text" else "caption_entities"] = entities
        msg = Message(
            message_id=mid, date=sent_at or self.clock(), chat=self.chat_of(peer), from_user=sender,
            business_connection_id=conn, edit_date=int(self.clock().timestamp()),
            has_protected_content=protected or None, **{k: v for k, v in content.items() if v is not None})
        return Update(update_id=self._uid(), edited_business_message=msg)

    def bdeleted(self, peer: User, ids: list[int], *, conn: str = "bc1") -> Update:
        ev = BusinessMessagesDeleted(business_connection_id=conn, chat=self.chat_of(peer), message_ids=ids)
        return Update(update_id=self._uid(), deleted_business_messages=ev)

    def dm(self, user: User, text: str) -> Update:
        """Сообщение в личке бота (не Business)."""
        msg = Message(message_id=next(self._msg_ids), date=self.clock(), chat=self.chat_of(user), from_user=user,
                      text=text, entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                      if text.startswith("/") else None)
        return Update(update_id=self._uid(), message=msg)

    def dm_document(self, user: User, file_name: str, file_id: str, size: int = 100) -> Update:
        msg = Message(message_id=next(self._msg_ids), date=self.clock(), chat=self.chat_of(user), from_user=user,
                      document=Document(file_id=file_id, file_unique_id="u" + file_id, file_name=file_name,
                                        file_size=size))
        return Update(update_id=self._uid(), message=msg)

    def press(self, user: User, data: str, *, message_id: int = 1, text: str = "карточка") -> Update:
        msg = Message(message_id=message_id, date=self.clock(), chat=self.chat_of(user), from_user=User(**BOT_USER),
                      text=text)
        cq = CallbackQuery(id=str(next(self._cb_ids)), from_user=user, chat_instance="ci", data=data, message=msg)
        return Update(update_id=self._uid(), callback_query=cq)
