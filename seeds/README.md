# Seed kits

Each seed is a `git diff` patch that adds one known defect to the clean code. The defects never
live on `main` or on a feature branch. A scenario applies its seed to a scratch branch, opens a
pull request from it and checks that CI catches the defect.

```bash
git switch -c scratch/k1-race main
git apply seeds/k1_race.patch
uv run pytest tests/test_concurrency.py   # fails
```

## K1: ledger and web

| Seed | File changed | Defect | Test that catches it | Scenarios |
| --- | --- | --- | --- | --- |
| `k1_race` | `src/bank/ledger.py` | `withdraw` reads the balance with a SELECT, checks it, then writes `balance - amount` in a separate step with a deferred `BEGIN`, so it does not hold the write lock from read to write. Concurrent withdrawals all pass the check and overwrite each other (lost update). | `tests/test_concurrency.py::test_concurrent_withdrawals_never_overdraw` | S04, S26, S32, S37 |
| `k1_hidden_button` | `src/bank/web/app.css` | A `@media (max-width: 430px)` block sets `display: none` on the transfer form's Send button, so phone users cannot send a transfer. | `tests/test_web.py::test_send_button_not_hidden_at_or_below_430px` | S01 |

### Verification

Each patch was applied with `git apply` in a scratch worktree of the final code, and the test
above was run 5 times. The worktree was then removed.

| Seed | Failures | Notes |
| --- | --- | --- |
| `k1_race` | 5 of 5 | All 20 threads report success, so the test fails on `successes == 1` (exactly one withdrawal may succeed). A further 20 runs also failed 20 of 20, so the seed needs **no** artificial delay. If a slower runner ever stops reproducing it, add `time.sleep(0.01)` between the read and the `BEGIN` in the seeded code only. |
| `k1_hidden_button` | 5 of 5 | The CSS check reports `('(max-width: 430px)', '.form-actions #send-button')`. |

With either seed applied, every other test in the default run still passes.

## K7: fraud score

The fraud score kit (`src/fraud/`) trains on synthetic transactions only. Both seeds change the
model's data or features, not its tests.

| Seed | File changed | Defect | Test that catches it | Scenarios |
| --- | --- | --- | --- | --- |
| `k7_feature_leak` | `src/fraud/data.py` | `chargeback_window` ends 30 days after the transaction instead of at it, so `chargebacks_30d_before` counts the transaction's own later chargeback. Holdout AUC jumps to 0.98. | `tests/test_fraud_features.py::test_features_use_past_only`; the AUC band in `tests/test_fraud_model.py` | S40 |
| `k7_weak_model` | `src/fraud/features.py`, `src/fraud/model.py` | Adds feature set `v3`, which drops `amount_log`, and makes it the default. Holdout PR-AUC falls from 0.39 to 0.08. | None: every test passes. The CI report gate `metric_at_least(pr_auc, 0.25)` catches it. | S17 |

### Verification

Each patch was applied with `git apply` in a scratch worktree of the final code, then the
default test run and `python -m fraud.model` were run. The worktree was then removed. The model
is deterministic, so the metrics are the same on every run.

| Code | AUC | PR-AUC | Recall at threshold | Holdout FPR | FPR gap max-min (region) | Default test run |
| --- | --- | --- | --- | --- | --- | --- |
| Clean | 0.888 | 0.393 | 0.625 | 0.0097 | 0.0036 | all pass |
| `k7_feature_leak` | 0.983 | 0.628 | 0.784 | 0.0080 | 0.0047 | 5 fail: `test_features_use_past_only`, `test_history_features_have_source_times_before_the_transaction`, `test_chargeback_window_counts_only_earlier_chargebacks`, `test_features_do_not_change_when_later_chargebacks_are_removed`, `test_model_metrics_are_recorded` (AUC band) |
| `k7_weak_model` | 0.849 | 0.079 | 0.268 | 0.0092 | 0.0025 | all pass |
| both seeds | 0.977 | 0.278 | 0.491 | 0.0072 | - | the same 5 fail; at data seed 7 they pass S17 AC1-AC3 and fail AC4 (`auc` at most 0.95); other data seeds were not measured for this row |

The rendered acceptance tests were also run: S40 v1 (leak) fails `test_{TOKEN}_features_use_past_only`;
S40 v2 (clean) passes with `pr_auc` 0.393; S17 v1 (weak) passes and records `pr_auc` 0.079.

### Margins

The gates must sit well outside sampling noise, not only outside the deterministic value of one
run. The noise was measured with 300 bootstrap resamples of the holdout (frauds and genuine rows
resampled apart), on the clean model and the weak model.

| Rows | Holdout frauds | Clean AUC (SE) | Weak AUC (SE) | Clean PR-AUC (SE) | Weak PR-AUC (SE) |
| --- | --- | --- | --- | --- | --- |
| 50,000 | 77 | 0.906 (0.025) | 0.844 (0.025) | 0.317 (n/m) | 0.047 (n/m) |
| 200,000 | 291 | 0.888 (0.015) | 0.849 (0.015) | 0.393 (0.029) | 0.079 (0.013) |
| 400,000 | 549 | 0.903 (0.009) | 0.877 (0.009) | 0.419 (0.023) | 0.073 (0.008) |

(n/m: not measured.)

- With 50,000 rows the clean AUC was only about 1 SE above the old gate `auc >= 0.88`, and the
  clean value moved by more than 1 SE between data sizes. AUC does not separate the two models
  by enough at any size tried: the gap stays at 1.5 to 2 SE on each side of any gate.
- Dropping the amount mostly hurts the top of the ranking, which PR-AUC measures. The gate is
  therefore `pr_auc >= 0.25` at 200,000 rows: the clean model is 5 SE above it and the weak model
  13 SE below it.
- The holdout false positive rate stays within 0.0004 of the 1% validation target (one SE is
  about 0.0004), so the S17 limit is 1.5%, not 1%.
- S40 also has `metric_at_most(auc, 0.95)`: the clean model's AUC plus 2 SE is about 0.92, and
  the leak's AUC minus 2 SE is about 0.98. The verifier sends an AUC above 0.95 to a human.

## Regenerating a patch

Make the change on a scratch branch, then `git diff > seeds/<name>.patch`, and run
`git apply --check seeds/<name>.patch` on a clean checkout of `main`.
