"""Evaluation for the fraud score: ranking metrics, drift, fairness and data hashes."""

from __future__ import annotations

import hashlib
import math

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve

from fraud.data import COLUMNS, Table


def auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Area under the ROC curve."""
    return float(roc_auc_score(y_true, scores))


def pr_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Area under the precision-recall curve (average precision)."""
    return float(average_precision_score(y_true, scores))


def threshold_at_fpr(y_true: np.ndarray, scores: np.ndarray, fpr: float = 0.01) -> float:
    """The lowest score threshold whose false positive rate is at most ``fpr``."""
    fprs, _, thresholds = roc_curve(y_true, scores)
    return float(thresholds[np.flatnonzero(fprs <= fpr).max()])


def recall_at_fpr(y_true: np.ndarray, scores: np.ndarray, fpr: float = 0.01) -> float:
    """The highest recall (true positive rate) with a false positive rate of at most ``fpr``."""
    fprs, tprs, _ = roc_curve(y_true, scores)
    return float(tprs[fprs <= fpr].max())


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population stability index of ``actual`` against ``expected``.

    Bins are the quantiles of ``expected``. Empty bins get a small share so the log is finite.
    """
    expected = np.asarray(expected, dtype=float)
    actual = np.asarray(actual, dtype=float)
    edges = np.unique(np.quantile(expected, np.linspace(0.0, 1.0, bins + 1)[1:-1]))
    e = np.bincount(np.searchsorted(edges, expected, side="right"), minlength=len(edges) + 1)
    a = np.bincount(np.searchsorted(edges, actual, side="right"), minlength=len(edges) + 1)
    e_share = np.clip(e / e.sum(), 1e-6, None)
    a_share = np.clip(a / a.sum(), 1e-6, None)
    return float(np.sum((a_share - e_share) * np.log(a_share / e_share)))


def fairness_report(
    y_true: np.ndarray, scores: np.ndarray, groups: np.ndarray, threshold: float
) -> dict[str, object]:
    """False positive rate per group at ``threshold``, and the ratio of the highest to the lowest.

    The ratio is infinite when a group has no false positives.
    """
    y_true = np.asarray(y_true)
    flagged = np.asarray(scores) >= threshold
    per_group = {}
    for group in sorted(set(groups.tolist())):
        genuine = (groups == group) & (y_true == 0)
        per_group[group] = float(flagged[genuine].mean()) if genuine.any() else 0.0
    low, high = min(per_group.values()), max(per_group.values())
    return {
        "threshold": threshold,
        "fpr_by_group": per_group,
        "fpr_ratio_max_min": high / low if low > 0 else math.inf,
    }


def canonical_csv(table: Table) -> bytes:
    """The table as CSV with a fixed column order, ``\\n`` line ends and no index."""
    columns = [name for name in COLUMNS if name in table]
    columns += sorted(set(table) - set(columns))
    text = np.column_stack([np.asarray(table[name]).astype(str) for name in columns])
    lines = [",".join(columns)] + [",".join(row) for row in text.tolist()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def data_hash(table: Table) -> str:
    """sha256 of the table's canonical CSV bytes."""
    return hashlib.sha256(canonical_csv(table)).hexdigest()
