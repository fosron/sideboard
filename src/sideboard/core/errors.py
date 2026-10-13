"""Typed error taxonomy.

Providers raise these; the host converts them into contained error states and
``SourceErrorInfo`` records. Nothing here ever carries a raw private payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "BlockedError",
    "CapabilityError",
    "ConfigError",
    "ErrorCategory",
    "ParseError",
    "ProviderError",
    "RateLimitedError",
    "SideboardError",
    "SourceErrorInfo",
    "StorageError",
    "TransientNetworkError",
    "UnauthorizedError",
    "UnavailableError",
]


class ErrorCategory(StrEnum):
    """Redacted error category used in logs and diagnostics."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    BLOCKED = "blocked"
    PARSE = "parse"
    CAPABILITY = "capability"
    UNAVAILABLE = "unavailable"
    STORAGE = "storage"
    CONFIG = "config"
    INTERNAL = "internal"


@dataclass(slots=True)
class SourceErrorInfo:
    """Bounded, redacted description of a failed acquisition."""

    category: ErrorCategory
    message: str
    retry_after_seconds: float | None = None
    http_status: int | None = None
    at: str | None = None
    detail: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {"category": str(self.category), "message": self.message}
        if self.retry_after_seconds is not None:
            out["retry_after_seconds"] = self.retry_after_seconds
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.at is not None:
            out["at"] = self.at
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


class SideboardError(Exception):
    """Base class for typed application failures."""

    category: ErrorCategory = ErrorCategory.INTERNAL

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, object] = dict(detail)

    def to_info(self) -> SourceErrorInfo:
        return SourceErrorInfo(
            category=self.category, message=self.message, detail=dict(self.detail)
        )


class ProviderError(SideboardError):
    """A provider could not produce a payload."""

    category = ErrorCategory.INTERNAL


class TransientNetworkError(ProviderError):
    """Connect/read failure, DNS failure or 5xx. Retried once, then backed off."""

    category = ErrorCategory.NETWORK


class RateLimitedError(ProviderError):
    """429 or an explicit quota response. Honors Retry-After."""

    category = ErrorCategory.RATE_LIMITED

    def __init__(
        self,
        message: str,
        retry_after_seconds: float | None = None,
        *,
        http_status: int | None = None,
        **detail: object,
    ) -> None:
        super().__init__(message, **detail)
        self.retry_after_seconds = retry_after_seconds
        self.http_status = http_status

    def to_info(self) -> SourceErrorInfo:
        info = super().to_info()
        info.retry_after_seconds = self.retry_after_seconds
        return info


class UnauthorizedError(ProviderError):
    """401/403. Never a reason for access evasion, and never retried in a loop."""

    category = ErrorCategory.UNAUTHORIZED

    def __init__(self, message: str, *, http_status: int | None = None, **detail: object) -> None:
        super().__init__(message, **detail)
        self.http_status = http_status

    def to_info(self) -> SourceErrorInfo:
        info = super().to_info()
        info.http_status = self.http_status
        return info


class BlockedError(ProviderError):
    """The provider refuses automated access, or the user has not enabled it."""

    category = ErrorCategory.BLOCKED


class ParseError(ProviderError):
    """Malformed or oversized response that cannot be safely interpreted."""

    category = ErrorCategory.PARSE


class UnavailableError(ProviderError):
    """A required local resource (CLI, socket, file, sensor) is not available."""

    category = ErrorCategory.UNAVAILABLE


class CapabilityError(SideboardError):
    """A provider requested a capability it did not declare or was denied."""

    category = ErrorCategory.CAPABILITY


class ConfigError(SideboardError):
    """Configuration validation failure with field-level detail."""

    category = ErrorCategory.CONFIG

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])

    def to_info(self) -> SourceErrorInfo:
        return SourceErrorInfo(
            category=self.category,
            message=self.message,
            detail={f"problem_{i}": p for i, p in enumerate(self.problems[:20])},
        )


class StorageError(SideboardError):
    """Persistence failure."""

    category = ErrorCategory.STORAGE
