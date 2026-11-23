"""SQLite через aiosqlite (WAL, автокоммит). Время в базе в UTC ISO с точностью до секунды."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS connections(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    is_enabled INTEGER NOT NULL,
    can_reply INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    from_id INTEGER,
    is_owner INTEGER NOT NULL,
    is_bot_sent INTEGER NOT NULL DEFAULT 0,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    file_id TEXT,
    file_unique_id TEXT,
    file_size INTEGER,
    file_name TEXT,
    mime TEXT,
    local_path TEXT,
    media_saved_at TEXT,
    transcript TEXT,
    extra TEXT,
    reply_to INTEGER,
    edited_at TEXT,
    deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (connection_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages(chat_id, message_id);
CREATE INDEX IF NOT EXISTS messages_by_date ON messages(date);
CREATE TABLE IF NOT EXISTS edits(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT,
    old_text TEXT,
    old_file_id TEXT,
    old_file_unique_id TEXT,
    edited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edits_by_msg ON edits(chat_id, message_id);
CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    ai_mode TEXT NOT NULL DEFAULT 'auto' CHECK (ai_mode IN ('auto', 'on', 'off')),
    chat_class TEXT NOT NULL DEFAULT 'unknown'
        CHECK (chat_class IN ('business', 'personal', 'spam', 'unknown')),
    class_reason TEXT,
    first_seen TEXT NOT NULL,
    last_owner_msg_at TEXT,
    last_client_msg_at TEXT,
    handoff_at TEXT,
    new_client_card_at TEXT,
    voice_notice_at TEXT
);
CREATE TABLE IF NOT EXISTS pending(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    connection_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    trigger_message_id INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_one_waiting ON pending(chat_id) WHERE status = 'waiting';
CREATE INDEX IF NOT EXISTS pending_due ON pending(status, due_at);
CREATE TABLE IF NOT EXISTS leads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT,
    summary TEXT,
    need TEXT,
    budget TEXT,
    deadline TEXT,
    contact TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    card_sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_by_chat ON leads(chat_id, status);
CREATE TABLE IF NOT EXISTS ai_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    chat_id INTEGER,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ai_log_at ON ai_log(at);
"""

LEAD_FIELDS = ("name", "summary", "need", "budget", "deadline", "contact", "notes")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Connection:
    id: str
    user_id: int
    is_enabled: bool
    can_reply: bool
    updated_at: datetime | None = None


@dataclass
class StoredMessage:
    connection_id: str
    chat_id: int
    message_id: int
    from_id: int | None
    is_owner: bool
    is_bot_sent: bool
    date: datetime
    kind: str
    text: str | None = None
    file_id: str | None = None
    file_unique_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime: str | None = None
    local_path: str | None = None
    transcript: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    reply_to: int | None = None
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    version: int = 1

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> StoredMessage:
        return cls(
            connection_id=r["connection_id"], chat_id=r["chat_id"], message_id=r["message_id"],
            from_id=r["from_id"], is_owner=bool(r["is_owner"]), is_bot_sent=bool(r["is_bot_sent"]),
            date=from_iso(r["date"]) or datetime.now(timezone.utc), kind=r["kind"], text=r["text"],
            file_id=r["file_id"], file_unique_id=r["file_unique_id"], file_size=r["file_size"],
            file_name=r["file_name"], mime=r["mime"], local_path=r["local_path"],
            transcript=r["transcript"], extra=json.loads(r["extra"]) if r["extra"] else {},
            reply_to=r["reply_to"], edited_at=from_iso(r["edited_at"]),
            deleted_at=from_iso(r["deleted_at"]), version=r["version"],
        )


@dataclass
class Chat:
    chat_id: int
    name: str | None
    username: str | None
    is_bot: bool
    ai_mode: str
    chat_class: str
    class_reason: str | None
    first_seen: datetime
    last_owner_msg_at: datetime | None
    last_client_msg_at: datetime | None
    handoff_at: datetime | None
    new_client_card_at: datetime | None
    voice_notice_at: datetime | None

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> Chat:
        return cls(
            chat_id=r["chat_id"], name=r["name"], username=r["username"], is_bot=bool(r["is_bot"]),
            ai_mode=r["ai_mode"], chat_class=r["chat_class"], class_reason=r["class_reason"],
            first_seen=from_iso(r["first_seen"]) or datetime.now(timezone.utc),
            last_owner_msg_at=from_iso(r["last_owner_msg_at"]),
            last_client_msg_at=from_iso(r["last_client_msg_at"]),
            handoff_at=from_iso(r["handoff_at"]), new_client_card_at=from_iso(r["new_client_card_at"]),
            voice_notice_at=from_iso(r["voice_notice_at"]),
        )


@dataclass
class Pending:
    id: int
    chat_id: int
    connection_id: str
    due_at: datetime
    trigger_message_id: int | None
    status: str
    reason: str | None

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> Pending:
        return cls(id=r["id"], chat_id=r["chat_id"], connection_id=r["connection_id"],
                   due_at=from_iso(r["due_at"]) or datetime.now(timezone.utc),
                   trigger_message_id=r["trigger_message_id"], status=r["status"], reason=r["reason"])


@dataclass
class Lead:
    id: int
    chat_id: int
    name: str | None
    summary: str | None
    need: str | None
    budget: str | None
    deadline: str | None
    contact: str | None
    notes: str | None
    status: str
    card_sent_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, r: aiosqlite.Row) -> Lead:
        return cls(id=r["id"], chat_id=r["chat_id"], name=r["name"], summary=r["summary"], need=r["need"],
                   budget=r["budget"], deadline=r["deadline"], contact=r["contact"], notes=r["notes"],
                   status=r["status"], card_sent_at=from_iso(r["card_sent_at"]),
                   created_at=from_iso(r["created_at"]), updated_at=from_iso(r["updated_at"]))


@dataclass
class Usage:
    calls: int
    replies: int
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Repo:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: str | Path) -> Repo:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        if not row or row[0] < SCHEMA_VERSION:
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def _one(self, sql: str, args: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    # --- kv -----------------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    async def kv_del(self, key: str) -> None:
        await self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # --- подключения --------------------------------------------------------

    async def upsert_connection(self, c: Connection, now: datetime) -> None:
        await self.conn.execute(
            """INSERT INTO connections(id, user_id, is_enabled, can_reply, updated_at) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET user_id = excluded.user_id, is_enabled = excluded.is_enabled,
               can_reply = excluded.can_reply, updated_at = excluded.updated_at""",
            (c.id, c.user_id, int(c.is_enabled), int(c.can_reply), iso(now)))

    async def get_connection(self, conn_id: str) -> Connection | None:
        r = await self._one("SELECT * FROM connections WHERE id = ?", (conn_id,))
        if not r:
            return None
        return Connection(r["id"], r["user_id"], bool(r["is_enabled"]), bool(r["can_reply"]),
                          from_iso(r["updated_at"]))

    async def latest_connection(self, user_id: int) -> Connection | None:
        r = await self._one("SELECT * FROM connections WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
                            (user_id,))
        if not r:
            return None
        return Connection(r["id"], r["user_id"], bool(r["is_enabled"]), bool(r["can_reply"]),
                          from_iso(r["updated_at"]))

    # --- сообщения ----------------------------------------------------------

    async def save_message(self, m: StoredMessage) -> None:
        """Новое сообщение. Повтор того же id (наш ответ уже записан при отправке) не затирает файл,
        а extra сливается: граница пачки (upto) у нашего ответа не теряется."""
        await self.conn.execute(
            """INSERT INTO messages(connection_id, chat_id, message_id, from_id, is_owner, is_bot_sent, date,
                   kind, text, file_id, file_unique_id, file_size, file_name, mime, extra, reply_to)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET
                   from_id = excluded.from_id, is_owner = excluded.is_owner,
                   is_bot_sent = MAX(messages.is_bot_sent, excluded.is_bot_sent),
                   kind = excluded.kind, text = excluded.text,
                   file_id = COALESCE(excluded.file_id, messages.file_id),
                   file_unique_id = COALESCE(excluded.file_unique_id, messages.file_unique_id),
                   file_size = COALESCE(excluded.file_size, messages.file_size),
                   file_name = COALESCE(excluded.file_name, messages.file_name),
                   mime = COALESCE(excluded.mime, messages.mime),
                   extra = CASE WHEN messages.extra IS NULL THEN excluded.extra
                                ELSE json_patch(messages.extra, COALESCE(excluded.extra, '{}')) END,
                   reply_to = excluded.reply_to""",
            (m.connection_id, m.chat_id, m.message_id, m.from_id, int(m.is_owner), int(m.is_bot_sent),
             iso(m.date), m.kind, m.text, m.file_id, m.file_unique_id, m.file_size, m.file_name, m.mime,
             json.dumps(m.extra, ensure_ascii=False) if m.extra else None, m.reply_to))

    async def find_message(self, chat_id: int, message_id: int) -> StoredMessage | None:
        """id сообщений в личках аккаунта сквозные, поэтому ищем без привязки к подключению."""
        r = await self._one(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ? ORDER BY date DESC LIMIT 1",
            (chat_id, message_id))
        return StoredMessage.from_row(r) if r else None

    async def apply_edit(self, old: StoredMessage, new: StoredMessage, edited_at: datetime) -> int:
        """Старая версия в edits, новая на месте. Возвращает номер новой версии."""
        await self.conn.execute(
            """INSERT INTO edits(connection_id, chat_id, message_id, version, kind, old_text, old_file_id,
                   old_file_unique_id, edited_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (old.connection_id, old.chat_id, old.message_id, old.version, old.kind, old.text, old.file_id,
             old.file_unique_id, iso(edited_at)))
        media_changed = (new.file_unique_id or None) != (old.file_unique_id or None)
        version = old.version + 1
        await self.conn.execute(
            """UPDATE messages SET kind = ?, text = ?, file_id = ?, file_unique_id = ?, file_size = ?,
                   file_name = ?, mime = ?, extra = ?, edited_at = ?, version = ?,
                   local_path = CASE WHEN ? THEN NULL ELSE local_path END,
                   media_saved_at = CASE WHEN ? THEN NULL ELSE media_saved_at END,
                   transcript = CASE WHEN ? THEN NULL ELSE transcript END
               WHERE connection_id = ? AND chat_id = ? AND message_id = ?""",
            (new.kind, new.text, new.file_id, new.file_unique_id, new.file_size, new.file_name, new.mime,
             json.dumps(new.extra, ensure_ascii=False) if new.extra else None, iso(edited_at), version,
             int(media_changed), int(media_changed), int(media_changed),
             old.connection_id, old.chat_id, old.message_id))
        return version

    async def edits_of(self, chat_id: int, message_id: int) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM edits WHERE chat_id = ? AND message_id = ? ORDER BY id",
                               (chat_id, message_id))

    async def set_local_path(self, m: StoredMessage, path: str | None, now: datetime) -> None:
        await self.conn.execute(
            "UPDATE messages SET local_path = ?, media_saved_at = ? "
            "WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (path, iso(now) if path else None, m.connection_id, m.chat_id, m.message_id))

    async def set_transcript(self, m: StoredMessage, text: str) -> None:
        await self.conn.execute(
            "UPDATE messages SET transcript = ? WHERE connection_id = ? AND chat_id = ? AND message_id = ?",
            (text, m.connection_id, m.chat_id, m.message_id))

    async def drop_message(self, chat_id: int, message_id: int) -> None:
        """Стало защищённым после правки: не храним вообще."""
        await self.conn.execute("DELETE FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        await self.conn.execute("DELETE FROM edits WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))

    async def mark_deleted(self, chat_id: int, message_ids: list[int], now: datetime) -> None:
        if not message_ids:
            return
        marks = ",".join("?" * len(message_ids))
        await self.conn.execute(
            f"UPDATE messages SET deleted_at = ? WHERE chat_id = ? AND message_id IN ({marks}) "
            "AND deleted_at IS NULL",
            (iso(now), chat_id, *message_ids))

    async def recent_messages(self, chat_id: int, limit: int = 30) -> list[StoredMessage]:
        rows = await self._all(
            # id сообщений в личках аккаунта растут монотонно: порядок по id надёжнее даты в секундах.
            "SELECT * FROM messages WHERE chat_id = ? AND deleted_at IS NULL "
            "ORDER BY message_id DESC LIMIT ?", (chat_id, limit))
        return [StoredMessage.from_row(r) for r in reversed(rows)]

    async def last_bot_sent_at(self, chat_id: int) -> datetime | None:
        r = await self._one("SELECT MAX(date) AS d FROM messages WHERE chat_id = ? AND is_bot_sent = 1",
                            (chat_id,))
        return from_iso(r["d"]) if r and r["d"] else None

    # --- чаты ---------------------------------------------------------------

    async def touch_chat(self, chat_id: int, name: str | None, username: str | None, now: datetime,
                         *, is_bot: bool | None = None) -> None:
        """Кэш имени обновляется из каждого апдейта; первое появление фиксируется один раз."""
        await self.conn.execute(
            """INSERT INTO chats(chat_id, name, username, is_bot, first_seen) VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET name = COALESCE(excluded.name, chats.name),
                   username = CASE WHEN excluded.name IS NULL AND excluded.username IS NULL
                                   THEN chats.username ELSE excluded.username END,
                   is_bot = CASE WHEN ? IS NULL THEN chats.is_bot ELSE excluded.is_bot END""",
            (chat_id, name, username, int(bool(is_bot)), iso(now), None if is_bot is None else int(is_bot)))

    async def get_chat(self, chat_id: int) -> Chat | None:
        r = await self._one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
        return Chat.from_row(r) if r else None

    async def update_chat(self, chat_id: int, **fields: Any) -> None:
        allowed = {"ai_mode", "chat_class", "class_reason", "last_owner_msg_at", "last_client_msg_at",
                   "handoff_at", "new_client_card_at", "voice_notice_at"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"нельзя обновить поля чата: {sorted(set(fields) - allowed)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        await self.conn.execute(f"UPDATE chats SET {cols} WHERE chat_id = ?", (*vals, chat_id))

    async def recent_chats(self, limit: int = 20) -> list[Chat]:
        rows = await self._all(
            """SELECT * FROM chats ORDER BY MAX(COALESCE(last_client_msg_at, ''), COALESCE(last_owner_msg_at, ''),
               first_seen) DESC LIMIT ?""", (limit,))
        return [Chat.from_row(r) for r in rows]

    # --- отложенные ответы --------------------------------------------------

    async def schedule(self, chat_id: int, connection_id: str, due_at: datetime, trigger_mid: int | None,
                       now: datetime) -> str:
        """Один ждущий ответ на чат: новый или со сдвинутым сроком. Возвращает 'new' или 'shifted'."""
        cur = await self.conn.execute(
            "UPDATE pending SET due_at = ?, trigger_message_id = ?, connection_id = ?, updated_at = ? "
            "WHERE chat_id = ? AND status = 'waiting'",
            (iso(due_at), trigger_mid, connection_id, iso(now), chat_id))
        if cur.rowcount:
            return "shifted"
        try:
            await self.conn.execute(
                "INSERT INTO pending(chat_id, connection_id, due_at, trigger_message_id, status, created_at, "
                "updated_at) VALUES(?, ?, ?, ?, 'waiting', ?, ?)",
                (chat_id, connection_id, iso(due_at), trigger_mid, iso(now), iso(now)))
            return "new"
        except sqlite3.IntegrityError:
            # Соседний апдейт того же чата успел вставить строку: просто сдвигаем её.
            await self.conn.execute(
                "UPDATE pending SET due_at = ?, trigger_message_id = ?, updated_at = ? "
                "WHERE chat_id = ? AND status = 'waiting'", (iso(due_at), trigger_mid, iso(now), chat_id))
            return "shifted"

    async def waiting_pending(self, chat_id: int) -> Pending | None:
        r = await self._one("SELECT * FROM pending WHERE chat_id = ? AND status = 'waiting'", (chat_id,))
        return Pending.from_row(r) if r else None

    async def cancel_pending(self, chat_id: int, now: datetime, reason: str = "owner") -> int:
        cur = await self.conn.execute(
            "UPDATE pending SET status = 'cancelled', reason = ?, updated_at = ? "
            "WHERE chat_id = ? AND status = 'waiting'", (reason, iso(now), chat_id))
        return cur.rowcount

    async def due_pending(self, now: datetime, limit: int = 20) -> list[Pending]:
        rows = await self._all(
            "SELECT * FROM pending WHERE status = 'waiting' AND due_at <= ? ORDER BY due_at LIMIT ?",
            (iso(now), limit))
        return [Pending.from_row(r) for r in rows]

    async def claim_pending(self, pid: int, now: datetime) -> bool:
        """waiting -> running. False, если строку уже взяли, отменили или срок сдвинулся вперёд."""
        cur = await self.conn.execute(
            "UPDATE pending SET status = 'running', updated_at = ? WHERE id = ? AND status = 'waiting' "
            "AND due_at <= ?", (iso(now), pid, iso(now)))
        return bool(cur.rowcount)

    async def finish_pending(self, pid: int, status: str, reason: str | None, now: datetime) -> None:
        await self.conn.execute("UPDATE pending SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                                (status, reason, iso(now), pid))

    async def requeue_running(self, now: datetime) -> int:
        """После рестарта: прерванные на полпути снова ждут (если для чата нет нового ожидания)."""
        cur = await self.conn.execute(
            """UPDATE pending SET status = 'waiting', updated_at = ? WHERE status = 'running'
               AND chat_id NOT IN (SELECT chat_id FROM pending WHERE status = 'waiting')""", (iso(now),))
        await self.conn.execute(
            "UPDATE pending SET status = 'cancelled', reason = 'restart', updated_at = ? WHERE status = 'running'",
            (iso(now),))
        return cur.rowcount

    async def pending_list(self, chat_id: int) -> list[Pending]:
        rows = await self._all("SELECT * FROM pending WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [Pending.from_row(r) for r in rows]

    # --- лиды ---------------------------------------------------------------

    async def open_lead(self, chat_id: int) -> Lead | None:
        r = await self._one("SELECT * FROM leads WHERE chat_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                            (chat_id,))
        return Lead.from_row(r) if r else None

    async def upsert_lead(self, chat_id: int, fields: dict[str, str | None], now: datetime) -> Lead:
        """Открытый лид чата дополняется новыми непустыми полями, иначе создаётся."""
        clean = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v}
        lead = await self.open_lead(chat_id)
        if lead is None:
            cols = ["chat_id", "status", "created_at", "updated_at", *clean]
            vals = [chat_id, "open", iso(now), iso(now), *clean.values()]
            cur = await self.conn.execute(
                f"INSERT INTO leads({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", vals)
            lead_id = cur.lastrowid
        else:
            lead_id = lead.id
            if clean:
                sets = ", ".join(f"{k} = ?" for k in clean)
                await self.conn.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                                        (*clean.values(), iso(now), lead_id))
        got = await self.get_lead(int(lead_id or 0))
        assert got is not None
        return got

    async def get_lead(self, lead_id: int) -> Lead | None:
        r = await self._one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        return Lead.from_row(r) if r else None

    async def open_leads(self, limit: int = 30) -> list[Lead]:
        rows = await self._all("SELECT * FROM leads WHERE status = 'open' ORDER BY updated_at DESC LIMIT ?",
                               (limit,))
        return [Lead.from_row(r) for r in rows]

    async def count_open_leads(self) -> int:
        r = await self._one("SELECT COUNT(*) AS n FROM leads WHERE status = 'open'")
        return int(r["n"]) if r else 0

    async def close_lead(self, lead_id: int, now: datetime) -> bool:
        cur = await self.conn.execute(
            "UPDATE leads SET status = 'closed', updated_at = ? WHERE id = ? AND status = 'open'",
            (iso(now), lead_id))
        return bool(cur.rowcount)

    async def mark_lead_card(self, lead_id: int, now: datetime) -> None:
        await self.conn.execute("UPDATE leads SET card_sent_at = ? WHERE id = ?", (iso(now), lead_id))

    # --- расход ИИ ----------------------------------------------------------

    async def log_ai(self, kind: str, chat_id: int | None, provider: str, model: str, input_tokens: int,
                     output_tokens: int, ok: bool, now: datetime) -> None:
        await self.conn.execute(
            "INSERT INTO ai_log(at, kind, chat_id, provider, model, input_tokens, output_tokens, ok) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (iso(now), kind, chat_id, provider, model, input_tokens, output_tokens, int(ok)))

    async def count_replies(self, since: datetime, chat_id: int | None = None) -> int:
        if chat_id is None:
            r = await self._one("SELECT COUNT(*) AS n FROM ai_log WHERE kind = 'reply' AND at >= ?", (iso(since),))
        else:
            r = await self._one("SELECT COUNT(*) AS n FROM ai_log WHERE kind = 'reply' AND at >= ? AND chat_id = ?",
                                (iso(since), chat_id))
        return int(r["n"]) if r else 0

    async def count_calls(self, since: datetime, chat_id: int | None = None) -> int:
        """Вызовы модели и расшифровки (в том числе без ответа клиенту): для лимита на стоимость."""
        sql = "SELECT COUNT(*) AS n FROM ai_log WHERE kind IN ('decide', 'classify', 'stt') AND at >= ?"
        args: tuple[Any, ...] = (iso(since),)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            args += (chat_id,)
        r = await self._one(sql, args)
        return int(r["n"]) if r else 0

    async def usage_since(self, since: datetime) -> Usage:
        r = await self._one(
            """SELECT COUNT(*) FILTER (WHERE kind != 'reply') AS calls,
                      COUNT(*) FILTER (WHERE kind = 'reply') AS replies,
                      COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o
               FROM ai_log WHERE at >= ?""", (iso(since),))
        if not r:
            return Usage(0, 0, 0, 0)
        return Usage(int(r["calls"]), int(r["replies"]), int(r["i"]), int(r["o"]))

    # --- ретенция -----------------------------------------------------------

    async def media_older_than(self, cutoff: datetime) -> list[tuple[int, int, str]]:
        rows = await self._all(
            "SELECT chat_id, message_id, local_path FROM messages WHERE local_path IS NOT NULL "
            "AND COALESCE(media_saved_at, date) < ?", (iso(cutoff),))
        return [(r["chat_id"], r["message_id"], r["local_path"]) for r in rows]

    async def forget_local_path(self, chat_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE messages SET local_path = NULL, media_saved_at = NULL WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id))

    async def local_paths_before(self, cutoff: datetime) -> list[str]:
        rows = await self._all("SELECT local_path FROM messages WHERE date < ? AND local_path IS NOT NULL",
                               (iso(cutoff),))
        return [r["local_path"] for r in rows]

    async def purge_messages_before(self, cutoff: datetime) -> int:
        c = iso(cutoff)
        await self.conn.execute(
            """DELETE FROM edits WHERE EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = edits.chat_id
               AND m.message_id = edits.message_id AND m.date < ?)""", (c,))
        cur = await self.conn.execute("DELETE FROM messages WHERE date < ?", (c,))
        return cur.rowcount

    async def purge_service_before(self, cutoff: datetime) -> None:
        c = iso(cutoff)
        await self.conn.execute("DELETE FROM pending WHERE status != 'waiting' AND updated_at < ?", (c,))
        await self.conn.execute("DELETE FROM ai_log WHERE at < ?", (c,))
