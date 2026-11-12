import pytest

pytest.importorskip("sklearn", reason="needs the fraud extra: uv sync --extra fraud")

import numpy as np

from fraud.evaluate import threshold_at_fpr
from fraud.model import TARGET_FPR, TEST_DAYS, TRAIN_DAYS, VALID_DAYS, holdout_metrics


def test_split_is_time_based(fraud_run):
    txns = fraud_run.data.transactions
    parts = (fraud_run.train_index, fraud_run.valid_index, fraud_run.test_index)
    assert (TRAIN_DAYS, VALID_DAYS, TEST_DAYS) == ((1, 45), (46, 60), (61, 90))
    for index, (first, last) in zip(parts, (TRAIN_DAYS, VALID_DAYS, TEST_DAYS), strict=True):
        assert txns["day"][index].min() == first and txns["day"][index].max() == last
        # No shuffling: each part keeps time order.
        assert np.all(np.diff(index) > 0)
    train, valid, test = parts
    assert txns["ts"][train].max() < txns["ts"][valid].min()
    assert txns["ts"][valid].max() < txns["ts"][test].min()
    # Together the parts cover every row once.
    assert np.array_equal(np.concatenate(parts), np.arange(len(txns["txn_id"])))
    assert np.array_equal(fraud_run.valid.ts, txns["ts"][valid])
    assert np.array_equal(fraud_run.test.ts, txns["ts"][test])


def test_threshold_is_chosen_on_validation_not_holdout(fraud_run):
    y_test = fraud_run.test_rows["is_fraud"]
    flagged = fraud_run.scores >= fraud_run.threshold
    assert np.isfinite(fraud_run.threshold)
    assert fraud_run.metrics["fpr_at_threshold"] == flagged[y_test == 0].mean()
    assert fraud_run.metrics["recall_at_threshold"] == flagged[y_test == 1].mean()
    assert fraud_run.fairness["threshold"] == fraud_run.threshold
    # The holdout labels would give another threshold; the run must not use them.
    assert threshold_at_fpr(y_test, fraud_run.scores, TARGET_FPR) != fraud_run.threshold


def test_holdout_fpr_can_exceed_the_target_when_the_data_drifts():
    rng = np.random.default_rng(3)
    y = np.array([0] * 5_000 + [1] * 50)
    valid_scores = np.concatenate([rng.random(5_000), rng.uniform(0.9, 1.0, 50)])
    # On the holdout, genuine scores shift up: more of them pass the validation threshold.
    test_scores = valid_scores.copy()
    test_scores[:5_000] = np.minimum(test_scores[:5_000] + 0.05, 1.0)
    regions = np.array(["A", "B"] * 2_525)
    threshold, metrics, _ = holdout_metrics(y, valid_scores, y, test_scores, regions)
    valid_fpr = (valid_scores[y == 0] >= threshold).mean()
    assert valid_fpr <= TARGET_FPR
    assert metrics["fpr_at_threshold"] > TARGET_FPR
    # Without drift the same check passes.
    _, same, _ = holdout_metrics(y, valid_scores, y, valid_scores, regions)
    assert same["fpr_at_threshold"] <= TARGET_FPR


def test_model_metrics_are_recorded(fraud_run, record_metric):
    metrics = fraud_run.metrics
    for name in ("auc", "pr_auc", "recall_at_threshold", "fpr_at_threshold", "fpr_ratio_max_min"):
        record_metric(name, metrics[name])
    assert 0.75 < metrics["auc"] <= 1.0
    assert 0.0 < metrics["pr_auc"] <= 1.0
    assert 0.0 < metrics["recall_at_threshold"] <= 1.0
    assert 0.0 <= metrics["fpr_at_threshold"] <= 1.0


def test_fairness_report_covers_every_region(fraud_run):
    report = fraud_run.fairness
    assert sorted(report["fpr_by_group"]) == ["A", "B", "C", "D", "E"]
    rates = report["fpr_by_group"].values()
    assert report["fpr_ratio_max_min"] == max(rates) / min(rates)


def test_data_hashes_are_recorded(fraud_run, record_data_hashes):
    hashes = fraud_run.data_hashes
    assert hashes["train"] != hashes["live"]
    record_data_hashes(hashes["train"], hashes["live"])
