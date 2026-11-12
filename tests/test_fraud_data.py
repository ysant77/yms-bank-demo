import numpy as np

from fraud.data import COLUMNS, N_DAYS, N_ROWS, REGIONS, generate
from fraud.evaluate import data_hash


def test_dataset_shape_and_columns(fraud_run):
    txns = fraud_run.data.transactions
    assert tuple(txns) == COLUMNS
    assert all(len(column) == N_ROWS for column in txns.values())
    assert txns["day"].min() == 1 and txns["day"].max() == N_DAYS
    assert set(txns["region"]) == set(REGIONS)
    assert np.all(np.diff(txns["ts"]) >= 0)
    assert np.array_equal(txns["txn_id"], np.arange(1, N_ROWS + 1))
    assert txns["account_age_days"].min() >= 1
    assert set(np.unique(txns["hour"])) <= set(range(24))


def test_fraud_rate_is_about_half_a_percent(fraud_run):
    assert 0.004 <= fraud_run.data.transactions["is_fraud"].mean() <= 0.006


def test_every_fraud_has_a_later_chargeback(fraud_run):
    txns, chargebacks = fraud_run.data.transactions, fraud_run.data.chargebacks
    fraud_ids = txns["txn_id"][txns["is_fraud"] == 1]
    assert set(fraud_ids) <= set(chargebacks["txn_id"])
    filed_after = chargebacks["ts"] > txns["ts"][chargebacks["txn_id"] - 1]
    assert filed_after.all()


def test_same_seed_gives_the_same_data(fraud_run):
    assert data_hash(generate().transactions) == data_hash(fraud_run.data.transactions)
    assert data_hash(generate(seed=8).transactions) != data_hash(fraud_run.data.transactions)
