"""Шпион: архив, правки, удаления (одиночное, медиа, пачка, нет в архиве), одноразовые медиа, настройки."""
from __future__ import annotations

from datetime import timedelta

from bot.keyboards import MenuCb
from tests.conftest import World
from tests.harness import CLIENT, OWNER


async def test_message_saved_both_directions(ready: World) -> None:
    w = ready
    mid = await w.client_says("Привет")
    oid = await w.owner_says("Привет, слушаю")
    m = await w.app.repo.find_message(CLIENT, mid)
    o = await w.app.repo.find_message(CLIENT, oid)
    assert m and not m.is_owner and m.text == "Привет" and m.kind == "text"
    assert o and o.is_owner and not o.is_bot_sent
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.name == "Азиз" and chat.username == "aziz_dev"


async def test_name_cache_updates(ready: World) -> None:
    w = ready
    await w.client_says("Привет")
    w.client = w.tg.user(CLIENT, "Азиз Каримов", "aziz_new")
    await w.client_says("Это я")
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.name == "Азиз Каримов" and chat.username == "aziz_new"


async def test_edit_text_saved_and_notified(ready: World) -> None:
    w = ready
    mid = await w.client_says("Бюджет 300$")
    w.clock.advance(minutes=3)
    await w.feed(w.tg.bedit(w.client, w.client, mid, "Бюджет 200$"))
    m = await w.app.repo.find_message(CLIENT, mid)
    assert m and m.text == "Бюджет 200$" and m.version == 2
    edits = await w.app.repo.edits_of(CLIENT, mid)
    assert len(edits) == 1 and edits[0]["old_text"] == "Бюджет 300$"
    note = w.session.owner_texts()[-1]
    assert note.startswith("✏️") and "изменил(а) сообщение · 14:03" in note
    assert "Было:" in note and "Бюджет 300$" in note and "Стало:" in note and "Бюджет 200$" in note
    assert f"tg://user?id={CLIENT}" in note and "@aziz_dev" in note


async def test_edit_markup_only_and_media_replaced(ready: World) -> None:
    w = ready
    mid = await w.client_says("Смотри сайт")
    ents = [{"type": "bold", "offset": 0, "length": 6}]
    await w.feed(w.tg.bedit(w.client, w.client, mid, "Смотри сайт", entities=ents))
    assert "только разметка" in w.session.owner_texts()[-1]
    # Та же версия ещё раз: ни записи, ни уведомления
    n = len(w.session.owner_texts())
    await w.feed(w.tg.bedit(w.client, w.client, mid, "Смотри сайт", entities=ents))
    assert len(w.session.owner_texts()) == n

    pid = await w.client_says(None, kind="photo", caption="макет", file_id="ph1")
    w.session.clear()
    await w.feed(w.tg.bedit(w.client, w.client, pid, kind="photo", caption="макет", file_id="ph2"))
    texts_ = w.session.owner_texts()
    assert any("Заменено медиа" in t for t in texts_)
    old = w.session.to_owner("sendPhoto")
    assert old and old[-1].get("photo") == "ph1" and "Было" in old[-1].text


async def test_edit_unknown_message(ready: World) -> None:
    w = ready
    await w.feed(w.tg.bedit(w.client, w.client, 55555, "новая версия"))
    note = w.session.owner_texts()[-1]
    assert "Старой версии нет в архиве" in note and "новая версия" in note
    assert await w.app.repo.find_message(CLIENT, 55555)


async def test_owner_edits_hidden_by_default(ready: World) -> None:
    w = ready
    oid = await w.owner_says("Цена 100")
    await w.feed(w.tg.bedit(w.client, w.owner, oid, "Цена 150"))
    assert not any(t.startswith("✏️") for t in w.session.owner_texts())
    m = await w.app.repo.find_message(CLIENT, oid)
    assert m and m.text == "Цена 150"  # сохранено, просто не показано
    await w.press(MenuCb(act="spy", val="mine").pack())
    await w.feed(w.tg.bedit(w.client, w.owner, oid, "Цена 200"))
    assert any(t.startswith("✏️ Ты изменил(а)") for t in w.session.owner_texts())


async def test_edits_toggle_off(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="spy", val="edits").pack())
    assert await w.app.get("spy_edits") == "0"
    mid = await w.client_says("раз")
    await w.feed(w.tg.bedit(w.client, w.client, mid, "два"))
    assert not any(t.startswith("✏️") for t in w.session.owner_texts())


async def test_delete_single_text(ready: World) -> None:
    w = ready
    mid = await w.client_says("Секретный план")
    w.clock.advance(minutes=10)
    await w.feed(w.tg.bdeleted(w.client, [mid]))
    note = w.session.owner_texts()[-1]
    assert note.startswith("🗑") and "удалил(а) · отправлено 14:00" in note and "Секретный план" in note
    m = await w.app.repo.find_message(CLIENT, mid)
    assert m and m.deleted_at is not None


async def test_delete_media_forwarded_by_file_id(ready: World) -> None:
    w = ready
    pid = await w.client_says(None, kind="photo", caption="паспорт? нет, макет", file_id="photoX")
    m = await w.app.repo.find_message(CLIENT, pid)
    assert m and m.local_path and (w.app.s.data_dir / m.local_path).is_file()
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, [pid]))
    sent = w.session.to_owner("sendPhoto")
    assert len(sent) == 1 and sent[0].get("photo") == "photoX"
    assert "удалил(а)" in sent[0].text and "макет" in sent[0].text


async def test_delete_media_falls_back_to_local_file(ready: World) -> None:
    w = ready
    vid = await w.client_says(None, kind="voice", file_id="voiceX")
    w.session.clear()
    w.session.fail("sendVoice", "Bad Request: wrong file identifier", when=lambda d: d.get("voice") == "voiceX")
    await w.feed(w.tg.bdeleted(w.client, [vid]))
    sent = w.session.to_owner("sendVoice")
    assert len(sent) == 2
    assert sent[0].get("voice") == "voiceX"
    assert sent[1].get("voice") != "voiceX"  # второй раз файлом с диска


async def test_delete_sticker_and_video_note_get_header_first(ready: World) -> None:
    w = ready
    sid = await w.client_says(None, kind="sticker", file_id="stk")
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, [sid]))
    calls = w.session.to_owner()
    assert calls[0].method == "sendMessage" and "удалил(а)" in calls[0].text
    assert calls[1].method == "sendSticker"


async def test_delete_bulk_sends_txt(ready: World) -> None:
    w = ready
    ids = []
    for i in range(7):
        ids.append(await w.client_says(f"сообщение {i}"))
        w.clock.advance(minutes=1)
    ids.append(await w.owner_says("мой ответ"))
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, ids + [1, 2]))
    docs = w.session.to_owner("sendDocument")
    assert len(docs) == 1
    assert "удалено разом" in docs[0].text
    content = docs[0].obj.document.data.decode()
    assert content.count("сообщение ") == 7
    assert "мой ответ" not in content  # «мои» выключены
    assert "нет в архиве" in content
    assert w.session.to_owner("sendMessage") == []


async def test_delete_missing_messages(ready: World) -> None:
    w = ready
    await w.feed(w.tg.bdeleted(w.client, [11, 12, 13]))
    note = w.session.owner_texts()[-1]
    assert "удалено 3 сообщения, которых нет в архиве" in note


async def test_owner_deletes_hidden_by_default(ready: World) -> None:
    w = ready
    oid = await w.owner_says("удалю это")
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, [oid]))
    assert w.session.to_owner() == []


async def test_deletes_toggle_off(ready: World) -> None:
    w = ready
    await w.press(MenuCb(act="spy", val="deletes").pack())
    mid = await w.client_says("раз")
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, [mid]))
    assert w.session.to_owner() == []
    m = await w.app.repo.find_message(CLIENT, mid)
    assert m and m.deleted_at is not None


async def test_view_once_and_protected_not_stored(ready: World) -> None:
    w = ready
    p1 = await w.client_says(None, kind="photo", file_id="once1", protected=True)
    p2 = await w.client_says(None, kind="photo", file_id="once2", media_extra={"ttl_seconds": 10})
    p3 = await w.client_says(None, kind="video", file_id="once3", msg_extra={"view_once": True})
    for mid in (p1, p2, p3):
        assert await w.app.repo.find_message(CLIENT, mid) is None
    assert not any(f.startswith("once") for f in w.session.downloads)
    assert not list((w.app.s.media_dir).rglob("*once*"))
    # Удаление таких сообщений: только «нет в архиве», без пересылки
    w.session.clear()
    await w.feed(w.tg.bdeleted(w.client, [p1, p2, p3]))
    assert w.session.to_owner("sendPhoto", "sendVideo") == []
    assert "которых нет в архиве" in w.session.owner_texts()[-1]
    # И на них секретарь не отвечает (содержимого нет)
    assert await w.app.repo.waiting_pending(CLIENT) is None


async def test_view_once_notifies_owner_without_content(ready: World) -> None:
    w = ready
    w.session.clear()
    await w.client_says("секрет", kind="photo", file_id="once9", media_extra={"ttl_seconds": 10})
    texts = w.session.owner_texts()
    assert any("прислал(а) фото на один просмотр" in t and "Содержимое не сохраняется" in t for t in texts)
    assert not any("секрет" in t for t in texts)
    assert w.session.to_owner("sendPhoto", "sendVideo") == []
    w.session.clear()
    await w.client_says(None, kind="video", file_id="prot9", protected=True)
    assert any("видео с защитой от сохранения" in t for t in w.session.owner_texts())


async def test_edit_to_protected_drops_stored_copy(ready: World) -> None:
    w = ready
    mid = await w.client_says(None, kind="photo", file_id="pp", caption="x")
    m = await w.app.repo.find_message(CLIENT, mid)
    assert m and m.local_path
    path = w.app.s.data_dir / m.local_path
    assert path.is_file()
    await w.feed(w.tg.bedit(w.client, w.client, mid, kind="photo", file_id="pp", caption="x", protected=True))
    assert await w.app.repo.find_message(CLIENT, mid) is None
    assert not path.exists()


async def test_download_skips_big_files_and_survives_errors(ready: World) -> None:
    w = ready
    big = await w.client_says(None, kind="video", file_id="bigvid", size=25 * 1024 * 1024)
    m = await w.app.repo.find_message(CLIENT, big)
    assert m and m.file_id == "bigvid" and m.local_path is None
    assert "bigvid" not in w.session.downloads
    w.session.fail("getFile", "Bad Request: file is too big")
    doc = await w.client_says(None, kind="document", file_id="docx", caption="ТЗ")
    m2 = await w.app.repo.find_message(CLIENT, doc)
    assert m2 and m2.file_id == "docx" and m2.local_path is None
    # Секретарь всё равно запланировал ответ
    assert await w.app.repo.waiting_pending(CLIENT) is not None


async def test_owner_media_not_downloaded_by_default(ready: World) -> None:
    w = ready
    await w.owner_says(None, kind="photo", file_id="mine1")
    assert "mine1" not in w.session.downloads


async def test_group_like_and_self_chats_ignored(ready: World) -> None:
    w = ready
    # Сообщение, где чат = сам владелец (на всякий случай), не храним
    upd = w.tg.bmsg(w.owner, w.owner, "заметка")
    await w.feed(upd)
    assert await w.app.repo.get_chat(OWNER) is None


async def test_stale_media_file_id_text_fallback(ready: World) -> None:
    w = ready
    pid = await w.client_says(None, kind="photo", file_id="gone", caption="фото")
    m = await w.app.repo.find_message(CLIENT, pid)
    assert m and m.local_path
    (w.app.s.data_dir / m.local_path).unlink()
    w.session.clear()
    w.session.fail("sendPhoto", "Bad Request: wrong file identifier")
    await w.feed(w.tg.bdeleted(w.client, [pid]))
    assert "Медиа не сохранилось" in w.session.owner_texts()[-1]


async def test_edit_time_is_edit_date(ready: World) -> None:
    w = ready
    mid = await w.client_says("v1")
    w.clock.advance(timedelta(minutes=17).total_seconds() / 60)
    await w.feed(w.tg.bedit(w.client, w.client, mid, "v2"))
    assert "· 14:17" in w.session.owner_texts()[-1]


async def test_minimal_chat_object_keeps_username(ready: World) -> None:
    w = ready
    mid = await w.client_says("привет")
    from aiogram.types import BusinessMessagesDeleted, Chat, Update

    ev = BusinessMessagesDeleted(business_connection_id="bc1", chat=Chat(id=CLIENT, type="private"),
                                 message_ids=[mid])
    await w.feed(Update(update_id=999, deleted_business_messages=ev))
    chat = await w.app.repo.get_chat(CLIENT)
    assert chat and chat.name == "Азиз" and chat.username == "aziz_dev"
