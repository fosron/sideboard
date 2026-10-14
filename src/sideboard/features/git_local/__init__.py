"""Local git checkout status through the existing ``git`` CLI, read-only.

``status`` and ``log`` only. Nothing is fetched, pulled, staged, committed, checked out
or cleaned, and the index is not refreshed.
"""

from .feature import FEATURE

__all__ = ["FEATURE"]
