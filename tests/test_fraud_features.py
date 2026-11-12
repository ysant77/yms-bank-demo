import numpy as np

from fraud.data import DAY_S, chargeback_window
from fraud.features import HISTORY, NO_SOURCE, REQUEST, FeatureMatrix, past_only_violations


def test_features_use_past_only(fraud_run):
    for matrix in (fraud_run.train, fraud_run.test):
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
