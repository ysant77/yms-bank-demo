"""Synthetic card transactions for the YMS Bank fraud score demo.

Everything here is made up from a fixed seed: no real people, cards, merchants or institutions.
The same seed always gives the same data.

A transaction is fraud with a probability that depends on a few of its features (merchant
category, hour, amount, account age) and on a hidden per-account risk. The dependence is
learnable but noisy, so a reasonable model does well but not perfectly.

Every fraud transaction gets a chargeback some days after it; a few genuine transactions get a
dispute chargeback too. ``chargebacks_30d_before`` counts the chargebacks filed on the same
account in the 30 days strictly before the transaction time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SEED = 7
N_ROWS = 50_000
N_DAYS = 90
N_ACCOUNTS = 6_000
START_TS = 1_767_225_600  # 2026-01-01T00:00:00Z, the start of day 1
DAY_S = 86_400
CHARGEBACK_WINDOW_DAYS = 30

COLUMNS = (
    "txn_id",
    "day",
    "ts",
    "account_id",
    "amount_minor",
    "merchant_category",
    "hour",
    "account_age_days",
    "region",
    "age_band",
    "is_fraud",
    "chargebacks_30d_before",
)

REGIONS = ("A", "B", "C", "D", "E")
REGION_P = (0.30, 0.25, 0.20, 0.15, 0.10)
AGE_BANDS = ("18-24", "25-34", "35-49", "50-64", "65+")
AGE_BAND_P = (0.14, 0.26, 0.28, 0.20, 0.12)

# Merchant category: share of transactions, median amount in minor units, fraud log-odds weight.
CATEGORIES = {
    "grocery": (0.24, 3_500, 0.0),
    "fuel": (0.10, 5_500, 0.3),
    "restaurant": (0.16, 4_000, 0.0),
    "transport": (0.10, 1_800, -0.4),
    "utilities": (0.06, 9_000, -1.0),
    "online_retail": (0.15, 6_000, 1.3),
    "electronics": (0.06, 25_000, 1.8),
    "travel": (0.05, 40_000, 0.9),
    "gift_cards": (0.03, 10_000, 2.3),
    "cash_withdrawal": (0.05, 15_000, 1.0),
}

# Share of transactions per hour of day (0 to 23), before the per-region night shift.
HOUR_WEIGHTS = np.array(
    [2, 1, 1, 1, 1, 2, 4, 6, 8, 9, 9, 10, 11, 10, 9, 9, 9, 10, 11, 10, 8, 6, 4, 3], dtype=float
)

Table = dict[str, np.ndarray]


@dataclass(frozen=True)
class Dataset:
    """Transactions (one row per card payment) and the chargebacks filed on them."""

    transactions: Table
    chargebacks: Table  # txn_id, account_id, ts (filing time), is_fraud


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def chargeback_window(
    account_id: np.ndarray,
    ts: np.ndarray,
    cb_account_id: np.ndarray,
    cb_ts: np.ndarray,
    days: int = CHARGEBACK_WINDOW_DAYS,
) -> tuple[np.ndarray, np.ndarray]:
    """Count chargebacks on the same account filed in ``[ts - days, ts)``.

    Returns the counts and, per transaction, the filing time of the latest chargeback counted
    (``-1`` when none was counted). The second array is the feature's source time.
    """
    shift = np.int64(1) << 32
    keys = np.sort(cb_account_id.astype(np.int64) * shift + cb_ts.astype(np.int64))
    base = account_id.astype(np.int64) * shift
    start = ts.astype(np.int64) - days * DAY_S
    end = ts.astype(np.int64)
    lo = np.searchsorted(keys, base + start, side="left")
    hi = np.searchsorted(keys, base + end, side="left")
    counts = hi - lo
    latest = np.where(counts > 0, keys[np.maximum(hi - 1, 0)] - base, -1)
    return counts.astype(np.int64), latest.astype(np.int64)


def generate(seed: int = SEED, n_rows: int = N_ROWS, n_days: int = N_DAYS) -> Dataset:
    """Build the synthetic transactions and chargebacks for ``seed``."""
    rng = np.random.default_rng(seed)

    # Accounts: region, age band, opening time, activity and a hidden risk flag.
    n_acc = N_ACCOUNTS
    acc_region = rng.choice(len(REGIONS), size=n_acc, p=REGION_P)
    acc_age_band = rng.choice(len(AGE_BANDS), size=n_acc, p=AGE_BAND_P)
    new = rng.random(n_acc) < 0.12
    age_at_start = np.where(
        new,
        rng.integers(1, 46, size=n_acc),
        np.clip(np.exp(rng.normal(6.4, 0.8, size=n_acc)), 46, 3_650).astype(np.int64),
    )
    acc_open_ts = START_TS - age_at_start * DAY_S - rng.integers(0, DAY_S, size=n_acc)
    activity = rng.gamma(2.0, 1.0, size=n_acc)
    risky = rng.random(n_acc) < 0.03

    # Transactions: account, time of day (region E shifts towards the night), category, amount.
    account = rng.choice(n_acc, size=n_rows, p=activity / activity.sum())
    region = acc_region[account]
    hour_p = np.vstack([HOUR_WEIGHTS] * len(REGIONS))
    hour_p[4, [22, 23, 0, 1, 2, 3, 4]] *= 2.0
    hour_p[3, [0, 1, 2, 3, 4]] *= 1.4
    hour_p /= hour_p.sum(axis=1, keepdims=True)
    cum = hour_p.cumsum(axis=1)[region]
    hour = (rng.random(n_rows)[:, None] > cum).sum(axis=1).clip(0, 23)
    day0 = rng.integers(0, n_days, size=n_rows)
    ts = START_TS + day0 * DAY_S + hour * 3_600 + rng.integers(0, 3_600, size=n_rows)

    names = list(CATEGORIES)
    cat_p = np.array([CATEGORIES[c][0] for c in names])
    category = rng.choice(len(names), size=n_rows, p=cat_p / cat_p.sum())
    median = np.array([CATEGORIES[c][1] for c in names], dtype=float)[category]
    amount = np.maximum(100, np.round(median * np.exp(rng.normal(0.0, 0.7, size=n_rows))))
    amount = amount.astype(np.int64)

    age_days = (ts - acc_open_ts[account]) // DAY_S

    # Pattern fraud rises steeply with a few features (plus a hidden account risk), so most of it
    # ranks high. Background fraud is a small, almost flat rate that no feature explains well.
    weight = np.array([CATEGORIES[c][2] for c in names])[category]
    night = (hour <= 4).astype(float)
    risk = (
        1.15 * weight
        + 1.9 * night
        + 1.05 * np.log(amount / median)
        + 1.0 * np.log(amount / 3_000.0)
        + 2.2 * (age_days < 30)
        + 1.0 * (age_days < 120)
        + 2.0 * risky[account]
    )
    pattern = _sigmoid(-20.2 + 2.6 * risk + rng.normal(0.0, 0.6, size=n_rows))
    background = 0.00075 * np.exp(0.5 * night + 0.3 * weight)
    p_fraud = pattern + background
    is_fraud = rng.random(n_rows) < p_fraud

    order = np.argsort(ts, kind="stable")
    account, ts, hour, category, amount = (a[order] for a in (account, ts, hour, category, amount))
    age_days, is_fraud, region = age_days[order], is_fraud[order], region[order]

    # Chargebacks: every fraud, and a few genuine disputes, filed some days later.
    dispute = ~is_fraud & (rng.random(n_rows) < 0.0015)
    filed = is_fraud | dispute
    delay = np.where(
        is_fraud,
        rng.integers(2 * DAY_S, 21 * DAY_S, n_rows),
        rng.integers(3 * DAY_S, 31 * DAY_S, n_rows),
    )
    txn_id = np.arange(1, n_rows + 1, dtype=np.int64)
    chargebacks: Table = {
        "txn_id": txn_id[filed],
        "account_id": account[filed] + 1,
        "ts": (ts + delay)[filed],
        "is_fraud": is_fraud[filed].astype(np.int64),
    }

    account_id = account.astype(np.int64) + 1
    counts, _ = chargeback_window(account_id, ts, chargebacks["account_id"], chargebacks["ts"])
    transactions: Table = {
        "txn_id": txn_id,
        "day": (ts - START_TS) // DAY_S + 1,
        "ts": ts.astype(np.int64),
        "account_id": account_id,
        "amount_minor": amount,
        "merchant_category": np.array(names, dtype=object)[category],
        "hour": hour.astype(np.int64),
        "account_age_days": age_days.astype(np.int64),
        "region": np.array(REGIONS, dtype=object)[region],
        "age_band": np.array(AGE_BANDS, dtype=object)[acc_age_band[account]],
        "is_fraud": is_fraud.astype(np.int64),
        "chargebacks_30d_before": counts,
    }
    return Dataset(transactions=transactions, chargebacks=chargebacks)


def take(table: Table, index: np.ndarray) -> Table:
    """The rows ``index`` (a boolean mask or integer positions) of every column."""
    return {name: column[index] for name, column in table.items()}
