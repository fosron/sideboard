"""Local Docker statistics through the existing CLI, read-only.

``docker_local`` never starts, stops, restarts, execs, pulls, prunes, deletes or
changes a context. Every daemon call uses a fixed argument array and an explicitly
pinned, approved **local** context. Remote contexts and inherited ``DOCKER_HOST``
redirects are rejected rather than followed.
"""

from .feature import FEATURE

__all__ = ["FEATURE"]
