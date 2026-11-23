"""Настройки из окружения. Локально читается .env, на сервере env_file из compose."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROVIDERS = ("anthropic", "openai")
STT_PROVIDERS = ("none", "openai")
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-5.4-mini"}
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
# Потолок вызовов модели (классификация, решения без ответа, расшифровка) на один разрешённый ответ.
CALLS_PER_REPLY = 3
# kb/ рядом с пакетом: /app/kb в образе, корень репозитория локально.
PACKAGE_KB = Path(__file__).resolve().parent.parent / "kb"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int | None
    llm_provider: str
    llm_model: str
    llm_effort: str | None
    anthropic_api_key: str | None
    openai_api_key: str | None
    stt_provider: str
    stt_model: str
    data_dir: Path
    tz_name: str
    kb_default_dir: Path
    chat_daily_limit: int = 12
    total_daily_limit: int = 150

    @property
    def chat_call_limit(self) -> int:
        return self.chat_daily_limit * CALLS_PER_REPLY

    @property
    def total_call_limit(self) -> int:
        return self.total_daily_limit * CALLS_PER_REPLY

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kotib.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def kb_dir(self) -> Path:
        return self.data_dir / "kb"

    @property
    def llm_key(self) -> str | None:
        return self.anthropic_api_key if self.llm_provider == "anthropic" else self.openai_api_key

    @property
    def llm_key_env(self) -> str:
        return KEY_ENV[self.llm_provider]

    @property
    def bot_id(self) -> int | None:
        head = self.bot_token.split(":", 1)[0]
        return int(head) if head.isdigit() else None


def _int(name: str, raw: str, *, minimum: int | None = None) -> int:
    try:
        val = int(raw.strip())
    except ValueError:
        raise SystemExit(f"{name}: нужно целое число, а не «{raw}»") from None
    if minimum is not None and val < minimum:
        raise SystemExit(f"{name}: должно быть не меньше {minimum}")
    return val


def load(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        from dotenv import load_dotenv

        load_dotenv()
        env = os.environ

    def get(name: str, default: str = "") -> str:
        val = env.get(name)
        return default if val is None or not val.strip() else val.strip()

    token = get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN в .env")

    owner_raw = get("OWNER_ID")
    provider = get("LLM_PROVIDER", "anthropic").lower()
    if provider not in PROVIDERS:
        raise SystemExit(f"LLM_PROVIDER: anthropic или openai, а не «{provider}»")
    stt = get("STT_PROVIDER", "none").lower()
    if stt not in STT_PROVIDERS:
        raise SystemExit(f"STT_PROVIDER: none или openai, а не «{stt}»")
    effort_raw = get("LLM_EFFORT", "low").lower()
    if effort_raw in ("none", "off", "-"):
        effort: str | None = None
    elif effort_raw in EFFORTS:
        effort = effort_raw
    else:
        raise SystemExit(f"LLM_EFFORT: одно из {', '.join(EFFORTS)} или none")

    tz_name = get("TZ", "Asia/Tashkent")
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise SystemExit(f"TZ: неизвестный часовой пояс «{tz_name}»") from None

    return Settings(
        bot_token=token,
        owner_id=_int("OWNER_ID", owner_raw) if owner_raw else None,
        llm_provider=provider,
        llm_model=get("LLM_MODEL", DEFAULT_MODELS[provider]),
        llm_effort=effort,
        anthropic_api_key=get("ANTHROPIC_API_KEY") or None,
        openai_api_key=get("OPENAI_API_KEY") or None,
        stt_provider=stt,
        stt_model=get("STT_MODEL", DEFAULT_STT_MODEL),
        data_dir=Path(get("DATA_DIR", "data")),
        tz_name=tz_name,
        kb_default_dir=Path(get("KB_DEFAULT_DIR", str(PACKAGE_KB))),
        chat_daily_limit=_int("AI_CHAT_DAILY_LIMIT", get("AI_CHAT_DAILY_LIMIT", "12"), minimum=1),
        total_daily_limit=_int("AI_DAILY_LIMIT", get("AI_DAILY_LIMIT", "150"), minimum=1),
    )
