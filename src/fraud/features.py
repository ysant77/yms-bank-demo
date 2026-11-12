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

from fraud.data import CATEGORIES, DAY_S, Table, chargeback_window, take

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


def later_data_changes(
    txns: Table,
    chargebacks: Table,
    matrix: FeatureMatrix,
    index: np.ndarray,
    feature_set: str,
    rows: int = 150,
    seed: int = 0,
) -> list[str]:
    """Rows whose features change when the chargebacks filed at or after them are removed.

    A behavioural past-only check that does not trust the recorded source times. ``matrix`` row
    ``k`` must belong to transaction ``index[k]``. Only rows whose account has a later
    chargeback can leak, so it checks the ``rows`` of them with the smallest gap to that next
    chargeback (a short look-ahead shows there first) plus ``rows // 3`` others drawn at random.
    Each row is rebuilt alone from the chargebacks filed strictly before it and compared. An
    empty result means no change.

    Limits: it withholds only the chargeback table and rebuilds through the same ``build``
    function. A feature that memoises by ``txn_id``, reads ``is_fraud`` or keeps state between
    calls escapes it; the AUC cap (``auc`` at most 0.95) is the only check behind it.
    """
    if len(chargebacks["ts"]) == 0:
        return []  # no later data exists to leak
    shift = np.int64(1) << 32
    keys = np.sort(chargebacks["account_id"].astype(np.int64) * shift + chargebacks["ts"])
    base = txns["account_id"][index].astype(np.int64) * shift
    ts = txns["ts"][index].astype(np.int64)
    nxt = np.searchsorted(keys, base + ts, side="right")
    found = nxt < len(keys)
    gap = np.full(len(index), np.iinfo(np.int64).max)
    same = found & (keys[np.minimum(nxt, len(keys) - 1)] < base + shift)
    gap[same] = keys[nxt[same]] - base[same] - ts[same]
    exposed = np.flatnonzero(same)
    closest = exposed[np.argsort(gap[exposed], kind="stable")[:rows]]
    rest = np.setdiff1d(exposed, closest)
    rng = np.random.default_rng(seed)
    extra = rng.choice(rest, min(rows // 3, len(rest)), replace=False)
    changes = []
    for k in np.sort(np.concatenate([closest, extra])):
        i = index[k]
        past = take(chargebacks, chargebacks["ts"] < txns["ts"][i])
        alone = build(take(txns, np.array([i])), past, feature_set)
        for j, name in enumerate(matrix.names):
            if alone.X[0, j] != matrix.X[k, j]:
                txn = int(txns["txn_id"][i])
                changes.append(f"txn {txn} {name}: {matrix.X[k, j]} with, {alone.X[0, j]} without")
    return changes
