"""Typed settings for the YouTube uploads source and widget.

Every path here is an explicit user-selected Takeout file. The source never guesses
a Takeout directory and never reads a browser profile or cookie store.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ChannelSetting", "YouTubeSettings", "YouTubeWidgetSettings"]


class ChannelSetting(BaseModel):
    """One configured channel.

    ``id`` accepts a raw ``UC…`` channel ID or a URL containing ``/channel/UC…``
    or ``channel_id=UC…``. Handles and bare names are left unresolved on purpose.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str | None = None
    favorite: bool = False


class YouTubeSettings(BaseModel):
    """Source settings for ``youtube.uploads``."""

    model_config = ConfigDict(extra="forbid")

    channels: list[ChannelSetting] = Field(default_factory=list)
    favorite_channel_ids: list[str] = Field(default_factory=list)
    active_channel_limit: int = Field(default=12, ge=1, le=30)
    takeout_subscriptions_csv: str | None = None
    takeout_watch_history_json: str | None = None
    half_life_days: float = Field(default=30.0, gt=0.0)
    history_window_days: int = Field(default=90, ge=1, le=3650)
    imported_channel_ids: list[str] = Field(default_factory=list)
    active_seconds: float | None = None
    ambient_seconds: float | None = None
    background_seconds: float | None = None


class YouTubeWidgetSettings(BaseModel):
    """Presentation settings for the ``youtube.uploads`` widget."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=8, ge=1, le=100)
    show_channel: bool = True
    favorites_only: bool = False
    group_by_channel: bool = False
