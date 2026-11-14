"""The threshold a session's store is read with: the effective threshold.

A Thorough run may lower the threshold for its session when the audit finds
flight emails below it (see ``engine.thorough``). The lowered value is stored
with the session, never written to the config file. Everything that selects
messages from a store by gate probability (extract, audit, review, export,
sources and the summaries) uses ``effective_threshold``: the lower of the
configured threshold and the stored one. So setting ``[classify] threshold``
below the stored value takes effect, while raising it above the stored value
does not raise the session's threshold.
"""

from __future__ import annotations

from .config import Config
from .store import Store

EFFECTIVE_THRESHOLD = "effective_threshold"


def effective_threshold(config: Config, store: Store) -> float:
    """The configured threshold, or the session's stored one if lower."""
    stored = store.session_value(EFFECTIVE_THRESHOLD)
    return config.threshold if stored is None else min(config.threshold, float(stored))


def record_effective_threshold(store: Store, value: float) -> None:
    """Store ``value`` as the session's effective threshold."""
    store.set_session_value(EFFECTIVE_THRESHOLD, repr(float(value)))
