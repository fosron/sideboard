"""Typed settings for the news feature."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DigestRules",
    "FeedSetting",
    "NewsSettings",
    "NewsWidgetSettings",
    "SavedWidgetSettings",
]

BUCKETS = (
    "local",
    "lithuania",
    "europe",
    "world",
    "business",
    "personal_finance",
    "software",
    "ai",
    "gadgets",
    "infrastructure",
)


class FeedSetting(BaseModel):
    """One publisher feed. Disabled entries perform no I/O at all."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    category: str = "world"
    language: str = "en"
    label: str | None = None
    enabled: bool = True
    weight: float = 1.0
    max_items: int = 25
    max_age_hours: int = 72

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("feed url must start with http:// or https://")
        return value

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in BUCKETS:
            raise ValueError(f"unknown news category {value!r}; known: {', '.join(BUCKETS)}")
        return value


class DigestRules(BaseModel):
    """Deterministic diversity rules for the six-item Overview digest."""

    model_config = ConfigDict(extra="forbid")

    digest_size: int = 6
    bucket_order: list[str] = Field(
        default_factory=lambda: [
            "local",
            "lithuania",
            "europe",
            "world",
            "business",
            "personal_finance",
            "software",
            "ai",
            "gadgets",
            "infrastructure",
        ]
    )
    max_ai_items_per_digest: int = 2
    max_per_source: int = 2
    max_age_hours: int = 48


class NewsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feeds: list[FeedSetting] = Field(default_factory=list)
    refresh_seconds: float | None = None
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None
    concurrency: int = 4
    max_items_per_feed: int = 25
    digest: DigestRules = Field(default_factory=DigestRules)
    # Conservative keyword tagging. This is a heuristic label, not a model.
    ai_keywords: list[str] = Field(
        default_factory=lambda: [
            "llm",
            "gpt",
            "claude",
            "gemini",
            "openai",
            "anthropic",
            "machine learning",
            "neural",
            "ai model",
            "artificial intelligence",
        ]
    )


class NewsWidgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = 8
    show_source: bool = True
    show_age: bool = True
    buckets: list[str] = Field(default_factory=list)
    digest: bool = True
    max_ai_items_per_digest: int = 2
    max_age_hours: int = 48


class SavedWidgetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = 12
