"""Feature matrix for the fraud score.

Each feature records, per row, its source time: the time of the latest data it was built from.
Request features come from the payment request itself, so their source time is the
transaction time. History features come from earlier events (account opening, chargebacks), so
their source time must be strictly before the transaction time. ``past_only_violations`` checks
this on the values actually built, after every join.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from fraud.data import CATEGORIES, DAY_S, Table, chargeback_window

REQUEST = "request"
HISTORY = "history"
NO_SOURCE = -1  # the feature used no event for this row (for example, no chargeback counted)

Built = tuple[np.ndarray, np.ndarray]  # values, source time per row


@dataclass(frozen=True)
class Feature:
    name: str
    kind: str  # REQUEST or HISTORY
    categorical: bool
    build: Callable[[Table, Table], Built]


@dataclass(frozen=True)
class FeatureMatrix:
    names: tuple[str, ...]
    kinds: tuple[str, ...]
    categorical: tuple[bool, ...]
    X: np.ndarray  # rows x features, float
    source_ts: np.ndarray  # rows x features, int64 epoch seconds or NO_SOURCE
    ts: np.ndarray  # transaction time per row


def _amount(txns: Table, _: Table) -> Built:
    return np.log(txns["amount_minor"].astype(float)), txns["ts"]


def _category(txns: Table, _: Table) -> Built:
    codes = {name: i for i, name in enumerate(CATEGORIES)}
    values = np.array([codes[c] for c in txns["merchant_category"]], dtype=float)
    return values, txns["ts"]


def _hour(txns: Table, _: Table) -> Built:
    return txns["hour"].astype(float), txns["ts"]


def _account_age(txns: Table, _: Table) -> Built:
    # The account was opened at or before ts - age * 1 day; age is at least 1 day.
    age = txns["account_age_days"]
    return age.astype(float), txns["ts"] - age * DAY_S


def _chargebacks(txns: Table, chargebacks: Table) -> Built:
    counts, latest = chargeback_window(
        txns["account_id"], txns["ts"], chargebacks["account_id"], chargebacks["ts"]
    )
    return counts.astype(float), latest


FEATURES = {
    f.name: f
    for f in (
        Feature("amount_log", REQUEST, False, _amount),
        Feature("merchant_category", REQUEST, True, _category),
        Feature("hour", REQUEST, False, _hour),
        Feature("account_age_days", HISTORY, False, _account_age),
        Feature("chargebacks_30d_before", HISTORY, False, _chargebacks),
    )
}

FEATURE_SETS = {
    "v2": (
        "amount_log",
        "merchant_category",
        "hour",
        "account_age_days",
        "chargebacks_30d_before",
    ),
}


def build(txns: Table, chargebacks: Table, feature_set: str) -> FeatureMatrix:
    """Build the features of ``feature_set`` for the transactions ``txns``."""
    features = [FEATURES[name] for name in FEATURE_SETS[feature_set]]
    values, sources = zip(*(f.build(txns, chargebacks) for f in features), strict=True)
    return FeatureMatrix(
        names=tuple(f.name for f in features),
        kinds=tuple(f.kind for f in features),
        categorical=tuple(f.categorical for f in features),
        X=np.column_stack(values).astype(float),
        source_ts=np.column_stack(sources).astype(np.int64),
        ts=txns["ts"].astype(np.int64),
    )


def past_only_violations(matrix: FeatureMatrix) -> dict[str, int]:
    """Rows per feature whose source time breaks the past-only rule.

    Request features may use the transaction time itself; history features must use only data
    strictly before it. An empty result means every feature is past-only.
    """
    found = {}
    for j, (name, kind) in enumerate(zip(matrix.names, matrix.kinds, strict=True)):
        source = matrix.source_ts[:, j]
        used = source != NO_SOURCE
        if kind == REQUEST:
            bad = used & (source > matrix.ts)
        else:
            bad = used & (source >= matrix.ts)
        if bad.any():
            found[name] = int(bad.sum())
    return found
