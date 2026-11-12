"""Train and evaluate the demo fraud score with a time-based split.

Training uses days 1 to 60 and the holdout uses days 61 to 90; rows are never shuffled. The
model is a small gradient-boosted tree ensemble with a fixed random state, so a run is
deterministic. This is a demo on synthetic data, not a real fraud system.

    uv run python -m fraud.model
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from fraud import evaluate
from fraud.data import SEED, Dataset, Table, generate, take
from fraud.features import FeatureMatrix, build

FEATURE_SET = "v2"
TRAIN_DAYS = (1, 60)
TEST_DAYS = (61, 90)
TARGET_FPR = 0.01
RANDOM_STATE = 0


@dataclass(frozen=True)
class Result:
    feature_set: str
    data: Dataset
    train_index: np.ndarray
    test_index: np.ndarray
    train: FeatureMatrix
    test: FeatureMatrix
    scores: np.ndarray  # holdout fraud scores
    metrics: dict[str, float]
    fairness: dict[str, object]
    data_hashes: dict[str, str]

    @property
    def train_rows(self) -> Table:
        return take(self.data.transactions, self.train_index)

    @property
    def test_rows(self) -> Table:
        return take(self.data.transactions, self.test_index)


def time_split(txns: Table) -> tuple[np.ndarray, np.ndarray]:
    """Row positions for training (days 1-60) and holdout (days 61-90), in time order."""
    day = txns["day"]
    train = np.flatnonzero((day >= TRAIN_DAYS[0]) & (day <= TRAIN_DAYS[1]))
    test = np.flatnonzero((day >= TEST_DAYS[0]) & (day <= TEST_DAYS[1]))
    return train, test


def make_model(matrix: FeatureMatrix) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=8,
        min_samples_leaf=80,
        l2_regularization=1.0,
        categorical_features=np.array(matrix.categorical),
        early_stopping=False,
        random_state=RANDOM_STATE,
    )


@functools.cache
def run(feature_set: str = FEATURE_SET, seed: int = SEED) -> Result:
    """Generate the data, train on days 1-60 and evaluate on days 61-90 (cached per process)."""
    data = generate(seed)
    txns = data.transactions
    train_index, test_index = time_split(txns)
    train_rows, test_rows = take(txns, train_index), take(txns, test_index)
    train = build(train_rows, data.chargebacks, feature_set)
    test = build(test_rows, data.chargebacks, feature_set)

    model = make_model(train).fit(train.X, train_rows["is_fraud"])
    scores = model.predict_proba(test.X)[:, 1]
    y = test_rows["is_fraud"]
    threshold = evaluate.threshold_at_fpr(y, scores, TARGET_FPR)
    fairness = evaluate.fairness_report(y, scores, test_rows["region"], threshold)
    flagged = scores >= threshold
    metrics = {
        "auc": evaluate.auc(y, scores),
        "pr_auc": evaluate.pr_auc(y, scores),
        "recall_at_1pct_fpr": evaluate.recall_at_fpr(y, scores, TARGET_FPR),
        "fpr_at_threshold": float(flagged[y == 0].mean()),
        "fpr_ratio_max_min": float(fairness["fpr_ratio_max_min"]),
    }
    return Result(
        feature_set=feature_set,
        data=data,
        train_index=train_index,
        test_index=test_index,
        train=train,
        test=test,
        scores=scores,
        metrics=metrics,
        fairness=fairness,
        data_hashes={
            "train": evaluate.data_hash(train_rows),
            "live": evaluate.data_hash(test_rows),
        },
    )


def main() -> None:
    result = run()
    print(f"feature set {result.feature_set}: {len(result.train_index)} train rows, "
          f"{len(result.test_index)} holdout rows")  # fmt: skip
    for name, value in result.metrics.items():
        print(f"{name:>20} {value:.4f}")
    for region, fpr in result.fairness["fpr_by_group"].items():
        print(f"{'fpr region ' + region:>20} {fpr:.4f}")


if __name__ == "__main__":
    main()
