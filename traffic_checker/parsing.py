"""Extraction of traffic figures from DataLoom API payloads."""

from __future__ import annotations

import math
from collections.abc import Mapping

# Candidate field names in order of preference. Matching is case-insensitive
# because the provider is not consistent about casing across endpoints.
_VISIT_KEYS = ("visits", "monthly_visits", "total_visits", "traffic")

# Objects the metric is known to be nested under. "engagments" is SimilarWeb's
# own long-standing misspelling.
_CONTAINER_KEYS = ("engagements", "engagments", "data", "result")


def extract_visits(payload: object) -> int | None:
    """Return the monthly visit count found in a ``/traffic`` response.

    The lookup is deliberately explicit: top-level fields win over nested
    ones and only known containers are inspected. Returns ``None`` when the
    payload carries no usable figure.
    """
    if not isinstance(payload, Mapping):
        return None

    root = _fold_keys(payload)
    scopes = [root]
    scopes.extend(
        _fold_keys(nested)
        for key in _CONTAINER_KEYS
        if isinstance(nested := root.get(key), Mapping)
    )

    for scope in scopes:
        for key in _VISIT_KEYS:
            visits = _coerce_visits(scope.get(key))
            if visits is not None:
                return visits
    return None


def _fold_keys(mapping: Mapping) -> dict[str, object]:
    return {str(key).lower(): value for key, value in mapping.items()}


def _coerce_visits(value: object) -> int | None:
    # bool is a subclass of int and must not be mistaken for a count.
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)
