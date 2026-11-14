"""OpenAI-compatible Chat Completions adapter for agent.Model (M3.6 B1).

The only provider-specific code. It translates between the agent's typed messages, tool
specs and model turns and the Chat Completions wire format, over httpx (no SDK). The agent
loop, its limits, the tools and the grounding validator are unchanged and stay authoritative:
this module only carries messages and never decides anything.

- Tools are sent as functions. The final answer is a call to one extra function,
  submit_answer, whose parameters are the AnswerDraft schema. Every request requires a
  function call (tool_choice "required"), and forces submit_answer when no agent tools are
  offered (a repair or the last turn). A plain-content reply from a server that ignores
  this is still read as an answer and validated as one.
- Tool-call ids from the provider are kept, so each tool result answers its own call. That
  includes the submit_answer call: a rejected draft is answered with a tool result giving
  the rejection codes, and the repair request offers and forces submit_answer only.
- One HTTP request per complete() call. Nothing is retried: a provider error, malformed
  response or timeout raises ProviderError, which run_agent turns into a model_error.
- A request never outlives the run: its timeout is the smaller of the configured request
  timeout and what is left of RUN_TIMEOUT_SECONDS since this adapter's first request. Use a
  new adapter for each run.
- Secrets: the API key is a SecretStr, sent only in the Authorization header, and removed
  from any provider text that is reported. Requests, headers and environment variables are
  never logged or printed.
"""

import json
import re
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from financial_analyst.agent import (
    ANSWER_TOOL,
    RUN_TIMEOUT_SECONDS,
    AnswerDraft,
    Message,
    ModelTurn,
    ToolCall,
    ToolSpec,
)

ANSWER_TOOL_DESCRIPTION = (
    "Submit the final answer draft. Call this alone, after the other tools, instead of "
    "replying in text. Numbers only as {placeholder}s mapped to value_ids."
)
_ERROR_TEXT_CHARS = 200


class ProviderSettings(BaseSettings):
    """LLM_API_KEY (or OpenAI's conventional OPENAI_API_KEY), LLM_MODEL and optional LLM_*
    settings, from the environment (or the git-ignored .env file through pydantic-settings;
    this code never reads it itself)."""

    model_config = SettingsConfigDict(
        env_prefix="LLM_", env_file=".env", extra="ignore", populate_by_name=True
    )

    # LLM_API_KEY wins when both are set.
    api_key: SecretStr = Field(
        min_length=1, validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY")
    )
    model: str = Field(min_length=1, max_length=200)
    base_url: str = "https://api.openai.com/v1"
    # Per request; never more than the whole run may take.
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=RUN_TIMEOUT_SECONDS)
    temperature: float | None = Field(default=None, ge=0, le=2)  # omitted when unset
    # USD per million tokens, for cost estimates only; no estimate when unset.
    input_cost_per_mtok: Decimal | None = Field(default=None, ge=0)
    output_cost_per_mtok: Decimal | None = Field(default=None, ge=0)

    @field_validator("base_url")
    @classmethod
    def _https_or_local(cls, url: str) -> str:
        url = url.rstrip("/")
        local = url.startswith(("http://localhost", "http://127.0.0.1"))
        if not (url.startswith("https://") or local):
            raise ValueError("must be https://, or http://localhost for a local server")
        return url


class ProviderConfigError(Exception):
    """Configuration is missing or invalid. The message names settings, never values."""


def load_provider_settings(**overrides: Any) -> ProviderSettings:
    try:
        return ProviderSettings(**overrides)
    except ValidationError as exc:
        # Only field names and error types: pydantic's own message can echo input values.
        problems = sorted(
            f"{_setting_name(e['loc'][0])} ({'missing' if e['type'] == 'missing' else 'invalid'})"
            for e in exc.errors()
        )
        raise ProviderConfigError("LLM provider configuration: " + ", ".join(problems)) from None


def _setting_name(field: object) -> str:
    if str(field).upper() in ("API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        return "LLM_API_KEY (or OPENAI_API_KEY)"
    return f"LLM_{str(field).upper()}"


ProviderErrorKind = Literal[
    "timeout",
    "http_error",
    "transport",
    "malformed_response",  # not a Chat Completions message, or an unusable tool call
    "empty_response",  # a well-formed message with no tool call and no text
    "refusal",  # the model refused (message.refusal), with no tool call or text
    "content_filter",  # finish_reason=content_filter
]


_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,40}")
_TEXT_PARTS = frozenset({"text", "output_text"})


class ResponseShape(BaseModel):
    """The structure of one provider response, never its content: types, counts, names of
    tools and keys (identifiers only), finish reason, refusal and usage presence."""

    model_config = ConfigDict(frozen=True)

    message_keys: list[str]
    content_type: Literal["absent", "null", "empty_string", "string", "parts", "other"]
    part_types: list[str]  # for structured content, each part's "type"
    tool_call_count: int
    tool_names: list[str]
    finish_reason: str
    refusal_present: bool
    usage_available: bool

    def summary(self) -> str:
        parts = f"{self.content_type}"
        if self.content_type == "parts":
            parts += f" [{', '.join(self.part_types) or 'none'}]"
        flags = {"refusal": self.refusal_present, "usage": self.usage_available}
        yes_no = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in flags.items())
        return (f"content={parts}, tool_calls={self.tool_call_count}, "
                f"finish_reason={self.finish_reason}, {yes_no}, "
                f"keys=[{', '.join(self.message_keys)}]")  # fmt: skip


def _safe(name: Any) -> str:
    return name if isinstance(name, str) and _SAFE_NAME.fullmatch(name) else "?"


def describe_response(body: Any) -> ResponseShape | None:
    """The shape of a response body, or None when it has no choices[0].message object."""
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict) or not isinstance(choice, dict):
        return None
    content = message.get("content", _ABSENT)
    if content is _ABSENT:
        content_type = "absent"
    elif content is None:
        content_type = "null"
    elif isinstance(content, str):
        content_type = "string" if content.strip() else "empty_string"
    elif isinstance(content, list):
        content_type = "parts"
    else:
        content_type = "other"
    parts = content if isinstance(content, list) else []
    calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    names = [_safe(c.get("function", {}).get("name")) if isinstance(c, dict)
             and isinstance(c.get("function"), dict) else "?" for c in calls]  # fmt: skip
    return ResponseShape(
        message_keys=sorted(_safe(k) for k in message),
        content_type=content_type,
        part_types=[_safe(p.get("type")) if isinstance(p, dict) else "?" for p in parts],
        tool_call_count=len(calls),
        tool_names=names,
        finish_reason=_safe(choice.get("finish_reason")),
        refusal_present=bool(message.get("refusal")),
        usage_available=isinstance(body.get("usage"), dict),
    )


_ABSENT = object()


def _text_of(content: Any) -> str | None:
    """Text content: a string, or the text parts of structured content joined in order."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p["text"] for p in content if isinstance(p, dict)
                 and p.get("type") in _TEXT_PARTS and isinstance(p.get("text"), str)]  # fmt: skip
        return "".join(texts)
    return None


class ProviderError(Exception):
    def __init__(self, kind: ProviderErrorKind, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class ProviderCall(BaseModel):
    """One request: timing, token usage when the provider reported it, and the response's
    shape (never its content), for cost, latency and failure diagnosis."""

    model_config = ConfigDict(frozen=True)

    latency_seconds: float
    outcome: Literal["ok", "error"]
    status_code: int | None
    error_kind: str | None = None
    prompt_tokens: int | None  # None: the response carried no usable usage
    completion_tokens: int | None
    shape: ResponseShape | None = None


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests: int
    requests_without_usage: int  # their tokens are unknown, not zero
    prompt_tokens: int  # over the requests with usage
    completion_tokens: int
    latency_seconds: float
    estimated_cost_usd: Decimal | None  # None without configured prices

    @property
    def usage_complete(self) -> bool:
        """False when some requests reported no usage: the totals are then lower bounds."""
        return self.requests_without_usage == 0


# --- Wire-format translation (pure functions) --------------------------------------------------


def to_provider_tools(tools: list[ToolSpec]) -> list[dict]:
    """The agent's tools plus submit_answer, as Chat Completions function tools."""
    if any(t.name == ANSWER_TOOL for t in tools):
        raise ValueError(f"{ANSWER_TOOL} is reserved for the answer draft")
    specs = [*tools, ToolSpec(name=ANSWER_TOOL, description=ANSWER_TOOL_DESCRIPTION,
                              parameters=AnswerDraft.model_json_schema())]  # fmt: skip
    return [
        {"type": "function",
         "function": {"name": s.name, "description": s.description, "parameters": s.parameters}}
        for s in specs
    ]  # fmt: skip


def to_provider_messages(messages: list[Message]) -> list[dict]:
    converted = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            converted.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": _arguments_json(c.arguments)}}
                    for c in message.tool_calls
                ],
            })  # fmt: skip
        elif message.role == "tool":
            converted.append({"role": "tool", "tool_call_id": message.tool_call_id,
                              "content": message.content})  # fmt: skip
        else:
            converted.append({"role": message.role, "content": message.content})
    return converted


def parse_response(body: Any) -> ModelTurn:
    """One Chat Completions response as a ModelTurn. Raises ProviderError for anything that
    is not a usable message. Tool arguments that are not valid JSON are kept as the raw
    string, so the tool layer reports invalid_arguments to the model."""
    try:
        choice = body["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("malformed_response", "no choices[0].message") from None
    if not isinstance(message, dict):
        raise ProviderError("malformed_response", "choices[0].message is not an object")
    if choice.get("finish_reason") == "length":
        raise ProviderError("malformed_response", "the response was cut off (finish_reason=length)")
    if choice.get("finish_reason") == "content_filter":
        raise ProviderError("content_filter", "the provider filtered the response")
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise ProviderError("malformed_response", "tool_calls is not a list")
    calls = []
    for raw in raw_calls:
        try:
            if raw.get("type", "function") != "function":  # some servers omit "type"
                raise ValueError(raw["type"])
            function = raw["function"]
            arguments = _parse_arguments(function["arguments"])
            calls.append(ToolCall(id=raw["id"], name=function["name"], arguments=arguments))
        except (KeyError, TypeError, ValueError, ValidationError):
            raise ProviderError("malformed_response", "a tool call is malformed") from None
    answers = [c for c in calls if c.name == ANSWER_TOOL]
    if answers and len(calls) > 1:
        raise ProviderError("malformed_response", f"{ANSWER_TOOL} must be alone in its turn")
    content = _text_of(message.get("content"))
    if not calls and (content is None or not content.strip()):
        shape = describe_response(body)
        kind = "refusal" if shape.refusal_present else "empty_response"
        raise ProviderError(kind, f"no tool call and no text ({shape.summary()})")
    try:
        if answers:
            return ModelTurn(answer=answers[0].arguments, answer_call_id=answers[0].id)
        if calls:
            return ModelTurn(tool_calls=calls)
        return ModelTurn(answer=_parse_arguments(content))
    except ValidationError:  # duplicate call ids, or an empty (null) answer
        raise ProviderError("malformed_response", "not a valid model turn") from None


def _parse_arguments(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _arguments_json(arguments: Any) -> str:
    return arguments if isinstance(arguments, str) else json.dumps(arguments, sort_keys=True)


# --- The adapter ------------------------------------------------------------------------------


class OpenAICompatibleModel:
    """agent.Model over an OpenAI-compatible /chat/completions endpoint. One per run."""

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._deadline: float | None = None
        # No retries: httpx sends each request once; redirects are not followed.
        self._client = httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            transport=transport,
            follow_redirects=False,
        )
        self.calls: list[ProviderCall] = []

    def __repr__(self) -> str:
        return f"OpenAICompatibleModel(model={self._settings.model!r})"

    def __enter__(self) -> "OpenAICompatibleModel":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelTurn:
        now = self._clock()
        if self._deadline is None:
            self._deadline = now + RUN_TIMEOUT_SECONDS
        remaining = self._deadline - now
        if remaining <= 0:
            raise ProviderError("timeout", "the run's time budget is spent; no request sent")
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": to_provider_messages(messages),
            "tools": to_provider_tools(tools),
            # Every acceptable turn is a function call, so one is required: an agent tool or
            # submit_answer. With no agent tools offered (a repair or the last turn),
            # submit_answer is forced.
            "tool_choice": "required" if tools else {"type": "function",
                                                      "function": {"name": ANSWER_TOOL}},
        }  # fmt: skip
        if self._settings.temperature is not None:
            payload["temperature"] = self._settings.temperature
        timeout = min(self._settings.request_timeout_seconds, remaining)
        started = self._clock()
        status, body = None, None
        try:
            response = self._client.post("/chat/completions", json=payload, timeout=timeout)
            status = response.status_code
            if status >= 400:
                raise ProviderError("http_error", f"HTTP {status}: {self._error_text(response)}")
            try:
                body = response.json()
            except ValueError:
                raise ProviderError("malformed_response", "the body is not JSON") from None
            turn = parse_response(body)
        except httpx.TimeoutException:
            self._record(started, status, None, "timeout")
            raise ProviderError("timeout", f"no response within {timeout:g} seconds") from None
        except httpx.HTTPError as exc:
            self._record(started, status, None, "transport")
            raise ProviderError("transport", type(exc).__name__) from None
        except ProviderError as exc:
            # Usage and shape are kept even when the message itself is unusable.
            self._record(started, status, body, exc.kind)
            raise
        self._record(started, status, body, None)
        return turn

    def usage(self) -> Usage:
        """Totals over the requests that reported usage; the rest are counted, not zeroed."""
        known = [c for c in self.calls if c.prompt_tokens is not None
                 and c.completion_tokens is not None]  # fmt: skip
        prompt = sum(c.prompt_tokens for c in known)
        completion = sum(c.completion_tokens for c in known)
        prices = (self._settings.input_cost_per_mtok, self._settings.output_cost_per_mtok)
        cost = None
        if None not in prices:
            cost = (prompt * prices[0] + completion * prices[1]) / Decimal(1_000_000)
        return Usage(requests=len(self.calls), requests_without_usage=len(self.calls) - len(known),
                     prompt_tokens=prompt, completion_tokens=completion,
                     latency_seconds=sum(c.latency_seconds for c in self.calls),
                     estimated_cost_usd=cost)  # fmt: skip

    def _record(self, started: float, status: int | None, body: Any, error: str | None) -> None:
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        tokens = [usage.get(k) if isinstance(usage.get(k), int) else None
                  for k in ("prompt_tokens", "completion_tokens")]  # fmt: skip
        self.calls.append(ProviderCall(
            latency_seconds=self._clock() - started, outcome="error" if error else "ok",
            status_code=status, error_kind=error, prompt_tokens=tokens[0],
            completion_tokens=tokens[1], shape=describe_response(body),
        ))  # fmt: skip

    def _error_text(self, response: httpx.Response) -> str:
        """The provider's error message, shortened, with the API key removed if echoed."""
        try:
            text = str(response.json()["error"]["message"])
        except (ValueError, KeyError, TypeError):
            text = response.reason_phrase or "error"
        return redact(text, self._settings)[:_ERROR_TEXT_CHARS]


def redact(text: str, settings: ProviderSettings) -> str:
    key = settings.api_key.get_secret_value()
    return text.replace(key, "[redacted]") if key else text
