"""Слой ИИ: anthropic и openai за одним интерфейсом. Модель отдаёт только JSON по схеме, инструментов нет."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

# Отказ модели по политике: сервер сам повторит запрос на запасной модели (fallbacks="default").
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT = 60.0

Part = dict[str, Any]  # {"type": "text", "text": ...} | {"type": "image", "media_type": ..., "data": base64}


class LLMError(Exception):
    """Вызов не удался: сеть, ключ, лимит провайдера, обрезанный ответ. Без текстов переписки."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False


class LLM:
    provider = "none"
    model = ""

    async def complete(self, *, system: str, parts: list[Part], schema: dict[str, Any], schema_name: str,
                       max_tokens: int = 4096) -> LLMResult:
        raise NotImplementedError


class STT:
    provider = "none"
    model = ""

    async def transcribe(self, data: bytes, filename: str, mime: str) -> str:
        raise NotImplementedError


class AnthropicLLM(LLM):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str | None, client: Any = None) -> None:
        import anthropic

        self._sdk = anthropic
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=2)

    async def complete(self, *, system: str, parts: list[Part], schema: dict[str, Any], schema_name: str,
                       max_tokens: int = 4096) -> LLMResult:
        sdk = self._sdk
        content: list[dict[str, Any]] = []
        for p in parts:
            if p["type"] == "image":
                content.append({"type": "image", "source": {"type": "base64", "media_type": p["media_type"],
                                                             "data": p["data"]}})
            else:
                content.append({"type": "text", "text": p["text"]})
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # Правила и база знаний стабильны: кэшируем префикс, изменчивое идёт в сообщении.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config=output_config,
                betas=[ANTHROPIC_FALLBACK_BETA],
                fallbacks="default",
            )
        except sdk.APIStatusError as e:
            raise LLMError(f"anthropic HTTP {e.status_code}, request {getattr(e, 'request_id', None)}") from e
        except sdk.APIConnectionError as e:
            raise LLMError("anthropic: нет связи или таймаут") from e
        except sdk.APIError as e:
            raise LLMError(f"anthropic: {type(e).__name__}") from e
        usage = resp.usage
        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        in_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        in_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if resp.stop_reason == "refusal":
            return LLMResult("", in_tokens, out_tokens, refused=True)
        if resp.stop_reason == "max_tokens":
            raise LLMError("anthropic: ответ обрезан по max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        return LLMResult(text, in_tokens, out_tokens)


class OpenAILLM(LLM):
    provider = "openai"

    def __init__(self, api_key: str, model: str, effort: str | None, client: Any = None) -> None:
        import openai

        self._sdk = openai
        self.model = model
        # У OpenAI нет «max», ближайшее xhigh.
        self.effort = "xhigh" if effort == "max" else effort
        self.client = client or openai.AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=2)

    async def complete(self, *, system: str, parts: list[Part], schema: dict[str, Any], schema_name: str,
                       max_tokens: int = 4096) -> LLMResult:
        sdk = self._sdk
        content: list[dict[str, Any]] = []
        for p in parts:
            if p["type"] == "image":
                content.append({"type": "input_image", "image_url": f"data:{p['media_type']};base64,{p['data']}"})
            else:
                content.append({"type": "input_text", "text": p["text"]})
        extra: dict[str, Any] = {}
        if self.effort:
            extra["reasoning"] = {"effort": self.effort}
        try:
            resp = await self.client.responses.create(
                model=self.model,
                instructions=system,
                input=[{"role": "user", "content": content}],
                text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
                max_output_tokens=max_tokens,
                store=False,
                **extra,
            )
        except sdk.APIStatusError as e:
            raise LLMError(f"openai HTTP {e.status_code}") from e
        except sdk.APIConnectionError as e:
            raise LLMError("openai: нет связи или таймаут") from e
        except sdk.APIError as e:
            raise LLMError(f"openai: {type(e).__name__}") from e
        usage = getattr(resp, "usage", None)
        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    return LLMResult("", in_tokens, out_tokens, refused=True)
        if getattr(resp, "status", None) == "incomplete":
            raise LLMError("openai: ответ не завершён")
        return LLMResult(resp.output_text or "", in_tokens, out_tokens)


class OpenAISTT(STT):
    provider = "openai"

    def __init__(self, api_key: str, model: str, client: Any = None) -> None:
        import openai

        self._sdk = openai
        self.model = model
        self.client = client or openai.AsyncOpenAI(api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=2)

    async def transcribe(self, data: bytes, filename: str, mime: str) -> str:
        sdk = self._sdk
        try:
            resp = await self.client.audio.transcriptions.create(model=self.model, file=(filename, data, mime))
        except sdk.APIStatusError as e:
            raise LLMError(f"openai stt HTTP {e.status_code}") from e
        except sdk.APIConnectionError as e:
            raise LLMError("openai stt: нет связи или таймаут") from e
        except sdk.APIError as e:
            raise LLMError(f"openai stt: {type(e).__name__}") from e
        return (getattr(resp, "text", "") or "").strip()


def build_llm(s: Settings) -> LLM | None:
    key = s.llm_key
    if not key:
        return None
    if s.llm_provider == "anthropic":
        return AnthropicLLM(key, s.llm_model, s.llm_effort)
    return OpenAILLM(key, s.llm_model, s.llm_effort)


def build_stt(s: Settings) -> STT | None:
    if s.stt_provider != "openai" or not s.openai_api_key:
        return None
    return OpenAISTT(s.openai_api_key, s.stt_model)
