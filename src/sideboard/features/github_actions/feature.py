"""GitHub Actions feature registration."""

from __future__ import annotations

from ...core.contracts import FeatureSpec, InputBinding, WidgetSpec
from .config import GithubActionsWidgetSettings
from .presenter import present_actions
from .provider import make_spec

__all__ = ["FEATURE"]

ACTIONS_WIDGET = WidgetSpec(
    type_id="github.actions",
    settings_model=GithubActionsWidgetSettings,
    inputs=(InputBinding(name="primary", accepts=frozenset({"github.actions"})),),
    present=present_actions,
    description="Workflow runs: what is running now and what failed recently.",
    default_category="dev",
    detail="table of recent runs with branch, event and age",
    min_width=32,
    min_height=5,
)

FEATURE = FeatureSpec(
    id="github_actions",
    sources=(make_spec(),),
    widgets=(ACTIONS_WIDGET,),
    default_sources={
        "github": {
            "type": "github.actions",
            "enabled": False,
            "settings": {"repos": [], "run_limit": 12, "failure_hours": 24.0},
        }
    },
    default_widgets={
        "github_actions": {"type": "github.actions", "sources": {"primary": "github"}},
    },
    description="Read-only GitHub Actions status through the authenticated gh CLI.",
)
