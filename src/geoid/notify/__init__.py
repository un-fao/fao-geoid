"""Notifier factory — selects a :class:`Notifier` from configuration.

``notify_backend=none`` (the default) returns the no-op :class:`NullNotifier` and
never imports the Graph transport — same lazy-import discipline as the storage
backends. ``graph`` lazily imports :class:`GraphNotifier` (and ``httpx``).
"""

from __future__ import annotations

from functools import lru_cache

from geoid.config import Settings, get_settings
from geoid.notify.base import Notifier, NullNotifier

__all__ = ["Notifier", "NullNotifier", "get_notifier"]


def _build(settings: Settings) -> Notifier:
    if settings.notify_backend == "graph":
        from geoid.notify.graph import GraphNotifier  # lazy: avoids importing httpx unless used

        return GraphNotifier(settings)
    return NullNotifier()


@lru_cache
def get_notifier() -> Notifier:
    """Process-wide notifier (cached so the worker reuses one Graph token per drain)."""
    return _build(get_settings())
