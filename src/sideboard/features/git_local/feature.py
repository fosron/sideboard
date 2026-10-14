"""Local git feature registration."""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import GitStatusWidgetSettings
from .presenter import present_status
from .provider import make_spec

__all__ = ["FEATURE"]

STATUS_WIDGET = WidgetSpec(
    type_id="git.status",
    settings_model=GitStatusWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"git.repositories"})),),
    present=present_status,
    description="Branch state and uncommitted work for local checkouts.",
    default_category="dev",
    detail="table of checkouts with branch, changes, ahead/behind and last commit",
    min_width=32,
    min_height=5,
)

FEATURE = FeatureSpec(
    id="git_local",
    sources=(make_spec(),),
    widgets=(STATUS_WIDGET,),
    default_sources={
        "git": {
            "type": "git.repositories",
            "enabled": False,
            "settings": {"repos": []},
        }
    },
    default_widgets={
        "git_status": {"type": "git.status", "sources": {"primary": "git"}},
    },
    description="Read-only local git checkout status through the git CLI.",
)
