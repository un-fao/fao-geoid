"""Vocabulary aliasing — surface terminology is config-driven, storage is neutral.

Physical table names are label-agnostic (``workspace`` / ``collection`` / ``place``).
The API surfaces a vocabulary chosen at deploy time (``GEOID_VOCAB``); Remi's final
terminology ruling — **workspace / collection / item** — ships as the ``fao``
preset and is the default. Switching presets is a config flip, not a migration.

OGC API Features path segments (``/collections``, ``/items``) are fixed by the
standard regardless of vocabulary; the alias only affects human-facing ``type``
labels and the management surface.
"""

from __future__ import annotations

from dataclasses import dataclass

# concept -> {vocab_name: singular label}
# "fao" is the team's final terminology ruling (workspace/collection/item) and
# the shipped default.
_LABELS: dict[str, dict[str, str]] = {
    "workspace": {"fao": "workspace", "stac": "catalog", "neutral": "workspace"},
    "collection": {"fao": "collection", "stac": "collection", "neutral": "collection"},
    "place": {"fao": "item", "stac": "item", "neutral": "place"},
}

_PLURALS = {
    "catalog": "catalogs",
    "workspace": "workspaces",
    "collection": "collections",
    "item": "items",
    "place": "places",
}

DEFAULT_VOCAB = "fao"
SUPPORTED_VOCABS = ("fao", "stac", "neutral")


@dataclass(frozen=True)
class Vocab:
    """Resolved surface vocabulary. Immutable; derived once from settings."""

    name: str

    def label(self, concept: str) -> str:
        """Singular surface label for a neutral concept (``workspace``/``collection``/``place``)."""
        mapping = _LABELS.get(concept)
        if mapping is None:
            return concept
        return mapping.get(self.name, mapping[DEFAULT_VOCAB])

    def plural(self, concept: str) -> str:
        """Plural surface label for a neutral concept."""
        singular = self.label(concept)
        return _PLURALS.get(singular, f"{singular}s")

    @property
    def item_type(self) -> str:
        """The value placed in a feature's ``type``-adjacent metadata label."""
        return self.label("place")


def get_vocab(name: str | None) -> Vocab:
    """Resolve a :class:`Vocab` from a configured name, defaulting safely."""
    chosen = (name or DEFAULT_VOCAB).lower()
    if chosen not in SUPPORTED_VOCABS:
        chosen = DEFAULT_VOCAB
    return Vocab(name=chosen)
