"""GitHub Actions status through the existing ``gh`` CLI, read-only.

No new credential: ``gh`` keeps its token in the system keyring. Nothing here cancels,
retries or dispatches a workflow.
"""

from .feature import FEATURE

__all__ = ["FEATURE"]
