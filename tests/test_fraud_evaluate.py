import pytest

pytest.importorskip("sklearn", reason="needs the fraud extra: uv sync --extra fraud")

import hashlib
import math

import numpy as np

from fraud.evaluate import (
    auc,
    canonical_csv,
    data_hash,
    fairness_report,
    pr_auc,
    psi,
    recall_at_fpr,
    threshold_at_fpr,
)


def test_psi_of_a_dataset_with_itself_is_zero(fraud_run):
    amount = fraud_run.data.transactions["amount_minor"]
    assert psi(amount, amount) == 0.0
    assert psi(amount, amount.copy(), bins=20) == 0.0


def test_psi_grows_with_drift():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 1, 20_000)
    assert psi(base, rng.normal(0, 1, 20_000)) < 0.01
    assert psi(base, rng.normal(0.5, 1, 20_000)) > 0.1
    assert psi(base, base * 100) > 1.0


def test_ranking_metrics_on_a_known_case():
    y = np.array([0] * 98 + [1, 1])
    perfect = np.arange(100, dtype=float)
    assert auc(y, perfect) == 1.0 and pr_auc(y, perfect) == 1.0
    assert recall_at_fpr(y, perfect, 0.01) == 1.0
    # One positive ranks below two negatives: half the positives are found at 1% FPR.
    scores = perfect.copy()
    scores[98] = 95.5
    assert recall_at_fpr(y, scores, 0.01) == 0.5
    assert auc(y, scores) == pytest.approx(1 - 2 / (2 * 98))
    assert threshold_at_fpr(y, scores, 0.01) == 99.0


def test_fairness_ratio():
    y = np.zeros(8, dtype=int)
    groups = np.array(["A"] * 4 + ["B"] * 4)
    scores = np.array([1, 1, 0, 0, 1, 0, 0, 0], dtype=float)
    report = fairness_report(y, scores, groups, threshold=0.5)
    assert report["fpr_by_group"] == {"A": 0.5, "B": 0.25}
    assert report["fpr_ratio_max_min"] == 2.0
    none_flagged = fairness_report(y, scores * 0, groups, threshold=0.5)
    assert math.isinf(none_flagged["fpr_ratio_max_min"])


def test_data_hash_is_sha256_of_canonical_csv():
    table = {"txn_id": np.array([1, 2]), "region": np.array(["A", "B"], dtype=object)}
    assert canonical_csv(table) == b"txn_id,region\n1,A\n2,B\n"
    assert data_hash(table) == hashlib.sha256(b"txn_id,region\n1,A\n2,B\n").hexdigest()
    assert data_hash(table) != data_hash({"txn_id": np.array([2, 1]), "region": table["region"]})
