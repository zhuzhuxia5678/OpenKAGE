"""LLM client for any OpenAI-compatible Chat Completions endpoint.

Configuration (environment or ``.env`` at the repository root, see ``.env.example``):
    OPENAI_API_KEY    API key (required)
    OPENAI_BASE_URL   endpoint base URL; default https://api.openai.com/v1
                      (e.g. https://api.deepseek.com for DeepSeek)

Usage:
    from openkage.llm import get_llm
    llm = get_llm("model-name", role="task")   # BaseLLM; role in task/reflect/consolidate
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List

from dotenv import load_dotenv

from openkage.agent_core.llm.base import (
    BaseLLM,
    LLMResponse,
    StreamChunkEvent,
    StreamEvent,
    StreamFinishEvent,
    ToolCallDeltaEvent,
    ToolCallRequest,
)
from openkage.config import LLMConfig

try:
    from json_repair import loads as _json_repair_loads
except ImportError:  # optional: repairs malformed tool-call arguments
    _json_repair_loads = None

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent.parent
_ENV = _ROOT_DIR / ".env"
if _ENV.exists():
    load_dotenv(_ENV, override=False)

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_clients: dict[tuple, Any] = {}


def _client(*, async_: bool, timeout: float):
    # an AsyncOpenAI client is bound to the event loop it first ran in, so cache per loop
    loop_id = id(asyncio.get_running_loop()) if async_ else None
    key = (async_, timeout, loop_id)
    if key not in _clients:
        import openai

        cls = openai.AsyncOpenAI if async_ else openai.OpenAI
        _clients[key] = cls(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
            timeout=timeout,
            max_retries=0,  # retries are handled in OpenAICompatLLM
        )
    return _clients[key]


def _parse_arguments(raw: str) -> dict:
    raw = raw or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = _json_repair_loads(raw) if _json_repair_loads else {"_raw": raw}
    return args if isinstance(args, dict) else {}


def _tool_calls(raw_calls: Any) -> List[ToolCallRequest]:
    return [
        ToolCallRequest(id=tc.id, name=tc.function.name,
                        arguments=_parse_arguments(tc.function.arguments), index=i)
        for i, tc in enumerate(raw_calls or [])
    ]


def _usage(u: Any) -> Dict[str, int]:
    if not u:
        return {}
    return {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
            "total_tokens": u.total_tokens}


def _reasoning(obj: Any) -> str:
    # the field name differs between providers: reasoning_content / reasoning
    return getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None) or ""


def _merge_system_messages(messages):
    """Some providers accept at most one system message, and only first."""
    if not messages:
        return messages
    system_parts, rest = [], []
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            system_parts.append(str(content))
        else:
            rest.append(m)
    if len(system_parts) <= 1:
        return messages
    return [{"role": "system", "content": "\n\n".join(p for p in system_parts if p)}, *rest]


def _ensure_max_tokens(config, role):
    """Fill the per-role completion budget unless the caller set one."""
    if config and "max_tokens" in config:
        return config
    merged = dict(config) if config else {}
    c = LLMConfig.load()
    merged["max_tokens"] = c.max_tokens_answer if role == "task" else c.max_tokens_reflect_step
    return merged


def _ensure_reasoning_effort(config, role):
    """Fill the per-role ``reasoning_effort``. ``none`` in the yml means: do not send the
    parameter (for models or providers that do not accept it)."""
    if config and ("reasoning_effort" in config or "extra_body" in config):
        return config
    effort = LLMConfig.load().reasoning_effort_for(role)
    merged = dict(config) if config else {}
    if effort != "none":
        merged["reasoning_effort"] = effort
    return merged


def _ensure_temperature(config, role):
    if config and "temperature" in config:
        return config
    merged = dict(config) if config else {}
    merged["temperature"] = LLMConfig.load().temperature_for(role)
    return merged


class OpenAICompatLLM(BaseLLM):
    """Chat Completions client with function calling and three nested retry layers:
    (1) transport/HTTP errors (``llm.sdk`` in the yml), (2) malformed or empty responses
    (``llm.api``), (3) the rollout layer in ``openkage/rollout.py``."""

    def __init__(self, model_name: str, *, timeout: float, max_retries: int,
                 retry_base_delay: float, retry_max_delay: float,
                 error_retries: int, error_base_delay: float, role: str):
        super().__init__(model_name)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        self.retry_max_delay = max(self.retry_base_delay, float(retry_max_delay))
        self._error_retries = error_retries
        self._error_base_delay = error_base_delay
        # one instance serves one role: timeout and reasoning effort are per role
        self._role = role

    def _delay(self, attempt: int) -> float:
        d = min(self.retry_max_delay, self.retry_base_delay * (2 ** max(attempt - 1, 0)))
        return d * random.uniform(0.8, 1.2)

    def _is_retryable(self, exc: Exception) -> bool:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(exc, (APITimeoutError, APIConnectionError)):
            return True
        if isinstance(exc, APIStatusError):
            return exc.status_code in _RETRYABLE_STATUS
        msg = str(exc).lower()
        return any(k in msg for k in ("timeout", "rate limit", "temporarily unavailable"))

    def _kwargs(self, messages, tools, tool_choice, config) -> dict:
        cfg = _ensure_temperature(
            _ensure_reasoning_effort(_ensure_max_tokens(config, self._role), self._role),
            self._role)
        kw = {"model": self.model_name, "messages": _merge_system_messages(messages),
              "timeout": self.timeout, **self.default_config, **cfg}
        if tools:
            kw["tools"] = tools
        if tool_choice is not None:
            kw["tool_choice"] = tool_choice
        return kw

    @staticmethod
    def _to_response(completion: Any, model: str, attempt: int) -> LLMResponse:
        choice = completion.choices[0]  # empty choices -> IndexError -> error response
        msg = choice.message
        return LLMResponse(
            content=msg.content or "",
            tool_calls=_tool_calls(getattr(msg, "tool_calls", None)),
            finish_reason=choice.finish_reason or "stop",
            usage=_usage(completion.usage),
            metadata={"model": model, "attempts": attempt},
            reasoning=_reasoning(msg),
        )

    @staticmethod
    def _error(exc: Exception, model: str, attempt: int) -> LLMResponse:
        return LLMResponse(content=f"Error: {exc}", finish_reason="error",
                           metadata={"error": True, "error_message": str(exc),
                                     "model": model, "attempts": attempt})

    def _think_once(self, kw: dict) -> LLMResponse:
        client = _client(async_=False, timeout=self.timeout)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._to_response(client.chat.completions.create(**kw),
                                         self.model_name, attempt)
            except Exception as exc:  # noqa: BLE001
                if attempt > self.max_retries or not self._is_retryable(exc):
                    return self._error(exc, self.model_name, attempt)
                delay = self._delay(attempt)
                logger.info("LLM call failed, retry in %.1fs (%d/%d): %s",
                            delay, attempt, self.max_retries + 1, exc)
                time.sleep(delay)

    async def _async_think_once(self, kw: dict) -> LLMResponse:
        client = _client(async_=True, timeout=self.timeout)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._to_response(await client.chat.completions.create(**kw),
                                         self.model_name, attempt)
            except Exception as exc:  # noqa: BLE001
                if attempt > self.max_retries or not self._is_retryable(exc):
                    return self._error(exc, self.model_name, attempt)
                delay = self._delay(attempt)
                logger.info("LLM call failed, retry in %.1fs (%d/%d): %s",
                            delay, attempt, self.max_retries + 1, exc)
                await asyncio.sleep(delay)

    def think(self, messages, tools=None, tool_choice=None, config=None) -> LLMResponse:
        kw = self._kwargs(messages, tools, tool_choice, config)
        resp = None
        for attempt in range(1, self._error_retries + 1):
            resp = self._think_once(kw)
            if resp.finish_reason != "error":
                return resp
            if attempt < self._error_retries:
                time.sleep(self._error_base_delay * (2 ** (attempt - 1)))
        return resp

    async def async_think(self, messages, tools=None, tool_choice=None,
                          config=None) -> LLMResponse:
        kw = self._kwargs(messages, tools, tool_choice, config)
        resp = None
        for attempt in range(1, self._error_retries + 1):
            resp = await self._async_think_once(kw)
            if resp.finish_reason != "error":
                return resp
            if attempt < self._error_retries:
                await asyncio.sleep(self._error_base_delay * (2 ** (attempt - 1)))
        return resp

    async def stream_think(self, messages, tools=None, tool_choice=None,
                           config=None) -> AsyncGenerator[StreamEvent, None]:
        kw = self._kwargs(messages, tools, tool_choice, config)
        kw["stream"] = True
        kw["stream_options"] = {"include_usage": True}
        client = _client(async_=True, timeout=self.timeout)
        content, reasoning, finish, usage = "", "", "stop", {}
        buf: Dict[int, Dict[str, str]] = {}
        try:
            stream = await client.chat.completions.create(**kw)
            async for chunk in stream:
                if chunk.usage:
                    usage = _usage(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish = choice.finish_reason
                delta = choice.delta
                if not delta:
                    continue
                if delta.content:
                    content += delta.content
                    yield StreamChunkEvent(delta=delta.content, content=content)
                reasoning += _reasoning(delta)
                for i, tc in enumerate(delta.tool_calls or []):
                    idx = tc.index if getattr(tc, "index", None) is not None else i
                    b = buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        b["id"] = tc.id
                    piece = ""
                    if tc.function:
                        if tc.function.name:
                            b["name"] = tc.function.name
                        if tc.function.arguments:
                            piece = tc.function.arguments
                            b["arguments"] += piece
                    yield ToolCallDeltaEvent(index=idx, id=b["id"], name=b["name"],
                                             arguments_delta=piece,
                                             arguments_so_far=b["arguments"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM stream failed: %s", exc)
            err = self._error(exc, self.model_name, 1)
            err.content = content
            yield StreamFinishEvent(response=err)
            return
        calls = [ToolCallRequest(id=b["id"], name=b["name"],
                                 arguments=_parse_arguments(b["arguments"]), index=i)
                 for i, b in sorted(buf.items())]
        yield StreamFinishEvent(response=LLMResponse(
            content=content, tool_calls=calls, finish_reason=finish, usage=usage,
            metadata={"model": self.model_name}, reasoning=reasoning))


# cache key is (model, role): timeout and reasoning effort are per role, and two roles may
# use the same model
_cached_llms: dict[tuple[str | None, str], OpenAICompatLLM] = {}


def get_llm(model_name: str, *, role: str) -> OpenAICompatLLM:
    """Return a cached client for ``(model_name, role)``; ``role`` is one of
    task/reflect/consolidate. Timeouts and retries come from the ``llm:`` section of the yml."""
    key = (model_name, role)
    if key not in _cached_llms:
        c = LLMConfig.load()
        _cached_llms[key] = OpenAICompatLLM(
            model_name, timeout=c.sdk_timeout_for(role),
            max_retries=c.sdk_max_retries,
            retry_base_delay=c.sdk_retry_base_delay,
            retry_max_delay=c.sdk_retry_max_delay,
            error_retries=c.api_error_retries,
            error_base_delay=c.api_error_base_delay,
            role=role,
        )
    return _cached_llms[key]
