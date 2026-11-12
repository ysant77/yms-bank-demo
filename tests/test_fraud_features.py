import pytest

pytest.importorskip("sklearn", reason="needs the fraud extra: uv sync --extra fraud")

import dataclasses

import numpy as np

from fraud.data import DAY_S, chargeback_window, take
from fraud.features import (
    HISTORY,
    NO_SOURCE,
    REQUEST,
    FeatureMatrix,
    build,
    later_data_changes,
    past_only_violations,
)


def test_features_use_past_only(fraud_run):
    for matrix in (fraud_run.train, fraud_run.valid, fraud_run.test):
        assert past_only_violations(matrix) == {}


def test_history_features_have_source_times_before_the_transaction(fraud_run):
    matrix = fraud_run.test
    for j, kind in enumerate(matrix.kinds):
        source = matrix.source_ts[:, j]
        used = source != NO_SOURCE
        if kind == HISTORY:
            assert np.all(source[used] < matrix.ts[used])
        else:
            assert kind == REQUEST
            assert np.array_equal(source, matrix.ts)


def test_chargeback_window_counts_only_earlier_chargebacks():
    rng = np.random.default_rng(0)
    account = rng.integers(1, 6, size=300)
    ts = rng.integers(0, 90 * DAY_S, size=300)
    cb_account = rng.integers(1, 6, size=60)
    cb_ts = rng.integers(0, 120 * DAY_S, size=60)
    counts, latest = chargeback_window(account, ts, cb_account, cb_ts)
    for i in range(len(ts)):
        hit = (cb_account == account[i]) & (cb_ts >= ts[i] - 30 * DAY_S) & (cb_ts < ts[i])
        assert counts[i] == hit.sum()
        assert latest[i] == (cb_ts[hit].max() if hit.any() else NO_SOURCE)


def test_past_only_check_catches_a_future_source():
    ts = np.array([1_000, 2_000, 3_000, 4_000])
    request = ts.copy()
    history = np.array([900, NO_SOURCE, 3_000, 4_500])  # row 2 is at ts, row 3 after it
    matrix = FeatureMatrix(
        names=("amount_log", "chargebacks_30d_before"),
        kinds=(REQUEST, HISTORY),
        categorical=(False, False),
        X=np.zeros((4, 2)),
        source_ts=np.column_stack([request, history]),
        ts=ts,
    )
    assert past_only_violations(matrix) == {"chargebacks_30d_before": 2}
    late_request = np.column_stack([request + 1, np.full(4, NO_SOURCE)])
    late = FeatureMatrix(**{**matrix.__dict__, "source_ts": late_request})
    assert past_only_violations(late) == {"amount_log": 4}


def test_chargeback_feature_matches_the_data_column(fraud_run):
    matrix = fraud_run.test
    if "chargebacks_30d_before" in matrix.names:
        j = matrix.names.index("chargebacks_30d_before")
        column = fraud_run.test_rows["chargebacks_30d_before"]
        assert np.array_equal(matrix.X[:, j], column)


def test_features_do_not_change_when_later_chargebacks_are_removed(fraud_run):
    txns, chargebacks = fraud_run.data.transactions, fraud_run.data.chargebacks
    parts = (
        (fraud_run.train, fraud_run.train_index),
        (fraud_run.valid, fraud_run.valid_index),
        (fraud_run.test, fraud_run.test_index),
    )
    for matrix, index in parts:
        changes = later_data_changes(txns, chargebacks, matrix, index, fraud_run.feature_set)
        assert changes == [], changes[:5]


def test_the_behavioural_check_catches_a_look_ahead_that_hides_its_source_time(fraud_run):
    """A 10-day look-ahead that reports a source time just before the transaction still fails."""
    txns, chargebacks = fraud_run.data.transactions, fraud_run.data.chargebacks
    index = fraud_run.test_index
    rows = take(txns, index)
    ahead = take(chargebacks, np.ones(len(chargebacks["ts"]), dtype=bool))
    ahead["ts"] = ahead["ts"] - 10 * DAY_S  # every chargeback appears 10 days early
    leaky = build(rows, ahead, fraud_run.feature_set)
    j = leaky.names.index("chargebacks_30d_before")
    source = leaky.source_ts.copy()
    source[:, j] = np.where(source[:, j] != NO_SOURCE, leaky.ts - 1, NO_SOURCE)
    disguised = dataclasses.replace(leaky, source_ts=source)
    assert past_only_violations(disguised) == {}
    assert later_data_changes(txns, chargebacks, disguised, index, fraud_run.feature_set) != []


def test_chargeback_window_without_any_chargebacks():
    empty = np.array([], dtype=np.int64)
    counts, latest = chargeback_window(np.array([4]), np.array([10 * DAY_S]), empty, empty)
    assert counts.tolist() == [0] and latest.tolist() == [-1]
