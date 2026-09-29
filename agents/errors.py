"""Shared runtime helpers and safe errors for review agents.

The model provider is asynchronous, but a few of the repository analyzers
are synchronous libraries.  This module keeps the provider boundary in one
place and makes the distinction between a clean model report and a broken
model response explicit.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Iterable, TypeVar

import anthropic

from agents.base import Finding
from config import settings

T = TypeVar("T")

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES = 5
MAX_REQUEST_TIMEOUT_SECONDS = 300.0

_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})
_MISSING = object()


class AgentRuntimeError(RuntimeError):
    """Safe, machine-readable failure from an agent runtime.

    ``message`` deliberately contains no provider response text.  Callers can
    log ``reason_code`` and ``retryable`` without leaking request data or
    provider internals into a review report.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        retryable: bool = False,
        agent_name: str | None = None,
    ) -> None:
        self.reason_code = reason_code
        # ``error_code`` mirrors the durable execution record field used by
        # orchestration while ``reason_code`` remains the public runtime name.
        self.error_code = reason_code
        self.retryable = retryable
        self.agent_name = agent_name
        super().__init__(message)


class AgentResponseError(AgentRuntimeError):
    """The provider returned a response that violates the agent contract."""


class AgentToolOutputError(AgentResponseError):
    """The expected tool output is missing, wrong, or malformed."""


class AgentProviderError(AgentRuntimeError):
    """The provider request failed or exceeded its runtime bound."""


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Bounded provider request settings used by one agent instance."""

    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_environment(
        cls,
        *,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> "AgentRuntimeConfig":
        """Resolve explicit arguments or safe environment defaults."""
        timeout = _positive_float(
            request_timeout,
            os.environ.get(
                "ANTHROPIC_REQUEST_TIMEOUT_SECONDS",
                settings.ANTHROPIC_REQUEST_TIMEOUT_SECONDS,
            ),
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        timeout = min(timeout, MAX_REQUEST_TIMEOUT_SECONDS)
        retries = _non_negative_int(
            max_retries,
            os.environ.get(
                "ANTHROPIC_MAX_RETRIES",
                settings.ANTHROPIC_MAX_RETRIES,
            ),
            DEFAULT_MAX_RETRIES,
        )
        retries = min(retries, MAX_RETRIES)
        return cls(
            request_timeout_seconds=timeout,
            max_retries=retries,
        )


def _positive_float(
    explicit: float | None,
    raw: str | float | None,
    default: float,
) -> float:
    value: Any = explicit if explicit is not None else raw
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return parsed


def _non_negative_int(
    explicit: int | None,
    raw: str | int | None,
    default: int,
) -> int:
    value: Any = explicit if explicit is not None else raw
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


class AgentRuntime:
    """Own one asynchronous Anthropic client and enforce request bounds."""

    def __init__(
        self,
        *,
        agent_name: str,
        api_key: str | None = None,
        client: Any | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.config = AgentRuntimeConfig.from_environment(
            request_timeout=request_timeout,
            max_retries=max_retries,
        )
        self._client = (
            client
            if client is not None
            else anthropic.AsyncAnthropic(
                api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
                timeout=self.config.request_timeout_seconds,
                max_retries=self.config.max_retries,
            )
        )

    @property
    def client(self) -> Any:
        """Return the underlying client for compatibility and test injection."""
        return self._client

    async def create_message(self, **kwargs: Any) -> Any:
        """Create one model message with timeout and cancellation propagation.

        ``AsyncAnthropic`` returns an awaitable.  The synchronous fallback is
        intentionally limited to test doubles and older callers that patched
        ``messages.create`` directly; production clients always take the
        awaitable branch.
        """
        try:
            pending = self._client.messages.create(**kwargs)
            if not inspect.isawaitable(pending):
                return pending
            # The SDK timeout covers its HTTP transport.  This outer bound also
            # protects custom transports and keeps the agent contract explicit.
            return await asyncio.wait_for(
                pending,
                timeout=self.config.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            # Cancellation is control flow.  It must reach the orchestrator so
            # a timed-out/cancelled review cannot be reported as a clean one.
            raise
        except asyncio.TimeoutError as exc:
            raise AgentProviderError(
                "provider_request_timeout",
                "model request exceeded its timeout",
                retryable=True,
                agent_name=self.agent_name,
            ) from exc
        except AgentRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider types vary by SDK version
            raise AgentProviderError(
                "provider_request_failed",
                "model request failed",
                retryable=True,
                agent_name=self.agent_name,
            ) from exc

    async def close(self) -> None:
        """Close the async HTTP client when the owning worker shuts down."""
        close = getattr(self._client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - close should remain safe
            raise AgentProviderError(
                "provider_close_failed",
                "model client could not be closed",
                agent_name=self.agent_name,
            ) from exc


async def maybe_await(value: T | Awaitable[T]) -> T:
    """Await an async result while retaining compatibility with sync mocks."""
    if inspect.isawaitable(value):
        return await value
    return value


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        result = value.get(name, _MISSING)
    else:
        result = getattr(value, name, _MISSING)
    if result is _MISSING:
        if default is not _MISSING:
            return default
        raise AgentToolOutputError(
            "malformed_tool_output",
            f"tool output is missing `{name}`",
        )
    return result


def _malformed(message: str, *, agent_name: str | None = None) -> AgentToolOutputError:
    return AgentToolOutputError(
        "malformed_tool_output",
        message,
        agent_name=agent_name,
    )


def parse_tool_findings(
    response: Any,
    *,
    expected_tool_name: str,
    filename: str,
    max_line: int,
    agent_name: str,
    allowed_categories: Iterable[str] | None = None,
    extra_required_keys: Iterable[str] = (),
) -> list[Finding]:
    """Validate and parse a complete tool-use report.

    A response containing ``findings=[]`` is a valid clean report.  Missing or
    malformed tool output raises a typed error so the orchestrator can record a
    partial review instead of silently claiming that the file was clean.
    """
    content = _field(response, "content", default=_MISSING)
    if not isinstance(content, (list, tuple)):
        raise _malformed("tool response content is not a list", agent_name=agent_name)

    tool_blocks: list[Any] = []
    unexpected_tool = False
    for block in content:
        block_type = _field(block, "type", default=None)
        if block_type != "tool_use":
            continue
        block_name = _field(block, "name", default=None)
        if block_name == expected_tool_name:
            tool_blocks.append(block)
        else:
            unexpected_tool = True

    if not tool_blocks:
        reason = "unexpected_tool_output" if unexpected_tool else "missing_tool_output"
        message = (
            "model returned an unexpected tool"
            if unexpected_tool
            else "model did not return the required findings tool"
        )
        raise AgentToolOutputError(reason, message, agent_name=agent_name)
    if len(tool_blocks) != 1:
        raise _malformed(
            "model returned multiple findings tool calls",
            agent_name=agent_name,
        )

    payload = _field(tool_blocks[0], "input", default=_MISSING)
    if not isinstance(payload, Mapping) or "findings" not in payload:
        raise _malformed(
            "findings tool input must contain a findings array",
            agent_name=agent_name,
        )
    raw_findings = payload["findings"]
    if not isinstance(raw_findings, list):
        raise _malformed("findings must be a list", agent_name=agent_name)

    category_set = set(allowed_categories or ())
    extra_keys = tuple(extra_required_keys)
    required = {
        "line_start",
        "line_end",
        "severity",
        "category",
        "description",
        "suggestion",
        "confidence",
        *extra_keys,
    }
    parsed: list[Finding] = []
    for raw in raw_findings:
        if not isinstance(raw, Mapping):
            raise _malformed(
                "each finding must be an object",
                agent_name=agent_name,
            )
        missing = required.difference(raw)
        if missing:
            raise _malformed(
                "finding is missing required fields",
                agent_name=agent_name,
            )

        line_start = raw["line_start"]
        line_end = raw["line_end"]
        if (
            isinstance(line_start, bool)
            or isinstance(line_end, bool)
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or line_start < 1
            or line_end < line_start
            or line_end > max_line
        ):
            raise AgentToolOutputError(
                "invalid_finding_coordinates",
                "finding coordinates are outside the reviewed source",
                agent_name=agent_name,
            )

        severity = raw["severity"]
        category = raw["category"]
        description = raw["description"]
        suggestion = raw["suggestion"]
        confidence = raw["confidence"]
        if (
            not isinstance(severity, str)
            or severity not in _SEVERITIES
            or not isinstance(category, str)
            or (category_set and category not in category_set)
            or not isinstance(description, str)
            or not description.strip()
            or not isinstance(suggestion, str)
            or not suggestion.strip()
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise AgentToolOutputError(
                "invalid_finding_shape",
                "finding contains invalid field values",
                agent_name=agent_name,
            )

        for key in extra_keys:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise AgentToolOutputError(
                    "invalid_finding_shape",
                    "finding contains invalid extension fields",
                    agent_name=agent_name,
                )

        try:
            parsed.append(
                Finding(
                    file=filename,
                    line_start=line_start,
                    line_end=line_end,
                    severity=severity,
                    category=category,
                    description=description,
                    suggestion=suggestion,
                    confidence=float(confidence),
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize model validation
            raise AgentToolOutputError(
                "invalid_finding_shape",
                "finding could not be validated",
                agent_name=agent_name,
            ) from exc
    return parsed


def max_source_line(file_diff: Any) -> int:
    """Return a safe upper line bound for one ``FileDiff``."""
    if file_diff.full_source is not None:
        return max(len(file_diff.full_source.splitlines()), 1)
    return max((line for line, _ in file_diff.added_lines), default=1)


def token_count(response: Any) -> int:
    """Extract usage totals without making usage metadata a clean-result gate."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    try:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(input_tokens, 0) + max(output_tokens, 0)


async def run_blocking(call: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run a sync analyzer off-loop.

    Cancelling the awaiting coroutine does not terminate the underlying native
    thread.  Callers must treat this as event-loop isolation, not hard thread
    cancellation; the analyzer is required to be side-effect free.
    """
    return await asyncio.to_thread(call, *args, **kwargs)
