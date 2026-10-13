"""News: curated public RSS/Atom feeds with a deterministic diversity digest.

Publisher terms control access; this module only performs conditional HTTP requests
against explicitly configured feeds, stores a bounded excerpt, and never fetches the
article body, enclosure files or remote images.

The Overview digest is built by a **deterministic, non-model** rule: one article per
configured bucket in bucket order, at most ``max_ai_items_per_digest`` heuristic
AI-tagged items, and an unfilled bucket stays visibly unfilled rather than being
padded with stale news.
"""

from .feature import FEATURE

__all__ = ["FEATURE"]
