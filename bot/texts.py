"""Тексты для людей. Коротко, по-русски, без длинных тире."""

NOT_YOURS = "Это личный бот"
STARTED = "Kotib запущен"
STALE_BUTTON = "Кнопка устарела"
DONE = "Готово"
UNKNOWN_CMD = "Не понял. Меню: /start"

DESCRIPTION = "Личный секретарь и архив переписки владельца. Работает только у владельца."
SHORT_DESCRIPTION = "Личный бот владельца"

CONNECTED = "✅ Kotib подключён к аккаунту."
CONNECTED_NO_REPLY = ("⚠️ Kotib подключён, но права отвечать нет. Включи его: Настройки → Telegram Business → "
                      "Чат-боты, право отвечать на сообщения.")
CAN_REPLY_NOW = "✅ Право отвечать включено."
DISCONNECTED = "⏹ Kotib отключён от аккаунта в Telegram Business. Шпион и секретарь стоят."
MENU_HINT = "Меню: /start"

# Секретарь клиенту
VOICE_RECEIVED = {
    "ru": "Голосовое получил, передам Джавохиру.",
    "uz": "Ovozli xabarni oldim, Javohirga yetkazaman.",
    "en": "Got your voice message, I will pass it to Javohir.",
}
# То же, когда это первое сообщение ассистента в разговоре: сначала представиться.
VOICE_INTRO = {
    "ru": "Здравствуйте, я ассистент Джавохира, он сейчас занят. Голосовое получил, передам ему.",
    "uz": "Assalomu alaykum, men Javohirning assistentiman, u hozir band. Ovozli xabarni oldim, unga yetkazaman.",
    "en": "Hello, I am Javohir's assistant, he is busy right now. Got your voice message, I will pass it to him.",
}

# Секретарь владельцу
UNKNOWN_VOICE = ("🎤 Голосовое от {who}. Чат ещё не разобран, расшифровки нет, клиенту ничего не ушло.\n"
                 "Это клиент? 🤖 Отвечать: ассистент ответит ему.")
CLASSIFY_FAILED = "ИИ не смог разобрать новый чат, клиенту ничего не ушло. Ответь сам или нажми «Пусть ведёт дальше»."
LIMIT_CHAT = "Лимит ИИ: {n} ответов в этом чате за сутки. Клиенту больше не отвечаю."
LIMIT_ALL = "Лимит ИИ: {n} ответов за сутки на все чаты. Клиенту не ответил."
LIMIT_CHAT_CALLS = "Лимит ИИ: {n} вызовов модели в этом чате за сутки. Клиенту больше не отвечаю."
LIMIT_ALL_CALLS = "Лимит ИИ: {n} вызовов модели за сутки на все чаты. Клиенту не ответил."

# Песочница
SANDBOX_ON = ("🧪 Песочница. Пиши как клиент, отвечу тем же движком, что и в чатах, без задержки. "
              "Решение модели покажу под ответом. Выход: /stop")
SANDBOX_OFF = "Песочница закрыта."
SANDBOX_NOT_ACTIVE = "Песочница и так закрыта. Войти: /test"
SANDBOX_TEXT_ONLY = "В песочнице только текст."
SANDBOX_NO_REPLY = "Ассистент решил промолчать."
SANDBOX_FAILED = "ИИ не ответил по схеме дважды. В бою это был бы handoff тебе."

# База знаний
KB_HINT = "Пришли файл knowledge.md или style.md документом, и я заменю его (старая версия уйдёт в бэкап)."
KB_WRONG_NAME = "Нужен файл с именем knowledge.md или style.md."
KB_DOWNLOAD_FAILED = "Не смог скачать файл, пришли ещё раз."

PAUSE_USAGE = "Пример: /pause 2h, /pause 30m, /pause 1d"
HOURS_USAGE = ("Нерабочие часы и выходные для режима «вне работы»: /hours 20:00-09:00 вс "
               "(дни: пн вт ср чт пт сб вс через запятую, «нет» без выходных). Сейчас: {now}")
HOURS_BAD_DAYS = "Не понял дни: {raw}. Пиши пн, вт, ср, чт, пт, сб, вс через запятую или «нет». Ничего не менял."

AI_OFF_KEY = "ИИ выключен: нет ключа {env}"


def connection_line(state: str) -> str:
    return {
        "ok": "✅ подключено, отвечать можно",
        "no_reply": "⚠️ нет права отвечать, включи в настройках Telegram Business",
        "disabled": "⏹ подключение выключено",
        "none": "❌ не подключён: Настройки → Telegram Business → Чат-боты",
    }[state]


MODE_NAMES = {"off": "выкл", "always": "всегда", "away": "вне работы"}
CLASS_NAMES = {"business": "клиент", "personal": "личное", "spam": "спам", "unknown": "не разобран"}
AI_MODE_NAMES = {"auto": "авто", "on": "вкл", "off": "выкл"}
