import pytest

pytest.importorskip("sklearn", reason="needs the fraud extra: uv sync --extra fraud")

import numpy as np

from fraud.model import TEST_DAYS, TRAIN_DAYS


def test_split_is_time_based(fraud_run):
    txns = fraud_run.data.transactions
    train, test = fraud_run.train_index, fraud_run.test_index
    assert TRAIN_DAYS == (1, 60) and TEST_DAYS == (61, 90)
    assert txns["day"][train].min() == 1 and txns["day"][train].max() == 60
    assert txns["day"][test].min() == 61 and txns["day"][test].max() == 90
    assert txns["ts"][train].max() < txns["ts"][test].min()
    # No shuffling: both parts keep time order and together cover every row once.
    assert np.all(np.diff(train) > 0) and np.all(np.diff(test) > 0)
    assert len(train) + len(test) == len(txns["txn_id"])
    assert np.array_equal(fraud_run.test.ts, txns["ts"][test])


def test_model_metrics_are_recorded(fraud_run, record_metric):
    metrics = fraud_run.metrics
    for name in ("auc", "pr_auc", "recall_at_1pct_fpr", "fpr_ratio_max_min"):
        record_metric(name, metrics[name])
    assert 0.75 < metrics["auc"] <= 1.0
    assert 0.0 < metrics["pr_auc"] <= 1.0
    assert metrics["fpr_at_threshold"] <= 0.01


def test_fairness_report_covers_every_region(fraud_run):
    report = fraud_run.fairness
    assert sorted(report["fpr_by_group"]) == ["A", "B", "C", "D", "E"]
    rates = report["fpr_by_group"].values()
    assert report["fpr_ratio_max_min"] == max(rates) / min(rates)


def test_data_hashes_are_recorded(fraud_run, record_data_hashes):
    hashes = fraud_run.data_hashes
    assert hashes["train"] != hashes["live"]
    record_data_hashes(hashes["train"], hashes["live"])
