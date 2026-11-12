"""Train and evaluate the demo fraud score with a time-based split.

The model is fitted on days 1 to 45. The decision threshold is chosen on days 46 to 60
(validation), at a false positive rate of 1% there. Every reported metric comes from days 61 to
90 (holdout), which neither step sees; rows are never shuffled. So the holdout false positive
rate at the threshold is a real check and can exceed 1% when the data drifts.

The model is a small gradient-boosted tree ensemble with a fixed random state, so a run is
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
TRAIN_DAYS = (1, 45)
VALID_DAYS = (46, 60)
TEST_DAYS = (61, 90)
TARGET_FPR = 0.01
RANDOM_STATE = 0


@dataclass(frozen=True)
class Result:
    feature_set: str
    data: Dataset
    train_index: np.ndarray
    valid_index: np.ndarray
    test_index: np.ndarray
    train: FeatureMatrix
    valid: FeatureMatrix
    test: FeatureMatrix
    scores: np.ndarray  # holdout fraud scores
    threshold: float  # chosen on validation; scores at or above it are flagged
    metrics: dict[str, float]
    fairness: dict[str, object]
    data_hashes: dict[str, str]

    @property
    def train_rows(self) -> Table:
        return take(self.data.transactions, self.train_index)

    @property
    def valid_rows(self) -> Table:
        return take(self.data.transactions, self.valid_index)

    @property
    def test_rows(self) -> Table:
        return take(self.data.transactions, self.test_index)


def time_split(txns: Table) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Row positions for training, validation and holdout, in time order."""
    day = txns["day"]
    return tuple(
        np.flatnonzero((day >= first) & (day <= last))
        for first, last in (TRAIN_DAYS, VALID_DAYS, TEST_DAYS)
    )


def holdout_metrics(
    y_valid: np.ndarray,
    valid_scores: np.ndarray,
    y_test: np.ndarray,
    test_scores: np.ndarray,
    test_groups: np.ndarray,
    target_fpr: float = TARGET_FPR,
) -> tuple[float, dict[str, float], dict[str, object]]:
    """Choose the threshold on validation, then measure everything on the holdout.

    Returns the threshold, the holdout metrics and the holdout fairness report per group.
    """
    threshold = evaluate.threshold_at_fpr(y_valid, valid_scores, target_fpr)
    flagged = test_scores >= threshold
    fairness = evaluate.fairness_report(y_test, test_scores, test_groups, threshold)
    metrics = {
        "auc": evaluate.auc(y_test, test_scores),
        "pr_auc": evaluate.pr_auc(y_test, test_scores),
        "recall_at_threshold": float(flagged[y_test == 1].mean()),
        "fpr_at_threshold": float(flagged[y_test == 0].mean()),
        "fpr_ratio_max_min": float(fairness["fpr_ratio_max_min"]),
    }
    return threshold, metrics, fairness


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
    """Generate the data, fit, choose the threshold and evaluate (cached per process)."""
    data = generate(seed)
    txns = data.transactions
    train_index, valid_index, test_index = time_split(txns)
    train_rows, valid_rows, test_rows = (
        take(txns, index) for index in (train_index, valid_index, test_index)
    )
    train, valid, test = (
        build(rows, data.chargebacks, feature_set) for rows in (train_rows, valid_rows, test_rows)
    )

    model = make_model(train).fit(train.X, train_rows["is_fraud"])
    valid_scores = model.predict_proba(valid.X)[:, 1]
    scores = model.predict_proba(test.X)[:, 1]
    threshold, metrics, fairness = holdout_metrics(
        valid_rows["is_fraud"], valid_scores, test_rows["is_fraud"], scores, test_rows["region"]
    )
    # The training data hash covers every row the model was built from: fit and threshold.
    development = take(txns, np.concatenate([train_index, valid_index]))
    return Result(
        feature_set=feature_set,
        data=data,
        train_index=train_index,
        valid_index=valid_index,
        test_index=test_index,
        train=train,
        valid=valid,
        test=test,
        scores=scores,
        threshold=threshold,
        metrics=metrics,
        fairness=fairness,
        data_hashes={
            "train": evaluate.data_hash(development),
            "live": evaluate.data_hash(test_rows),
        },
    )


def main() -> None:
    result = run()
    print(f"feature set {result.feature_set}: {len(result.train_index)} train rows, "
          f"{len(result.valid_index)} validation rows, "
          f"{len(result.test_index)} holdout rows")  # fmt: skip
    print(f"{'threshold':>20} {result.threshold:.4f}")
    for name, value in result.metrics.items():
        print(f"{name:>20} {value:.4f}")
    for region, fpr in result.fairness["fpr_by_group"].items():
        print(f"{'fpr region ' + region:>20} {fpr:.4f}")


if __name__ == "__main__":
    main()
