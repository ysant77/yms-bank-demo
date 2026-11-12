# Seed kit K1

Each seed is a `git diff` patch that adds one known defect to the clean code. The defects never
live on `main` or on a feature branch. A scenario applies its seed to a scratch branch, opens a
pull request from it and checks that CI catches the defect.

```bash
git switch -c scratch/k1-race main
git apply seeds/k1_race.patch
uv run pytest tests/test_concurrency.py   # fails
```

| Seed | File changed | Defect | Test that catches it | Scenarios |
| --- | --- | --- | --- | --- |
| `k1_race` | `src/bank/ledger.py` | `withdraw` reads the balance with a SELECT, checks it, then writes `balance - amount` in a separate step with a deferred `BEGIN`, so it does not hold the write lock from read to write. Concurrent withdrawals all pass the check and overwrite each other (lost update). | `tests/test_concurrency.py::test_concurrent_withdrawals_never_overdraw` | S04, S26, S32, S37 |
| `k1_hidden_button` | `src/bank/web/app.css` | A `@media (max-width: 430px)` block sets `display: none` on the transfer form's Send button, so phone users cannot send a transfer. | `tests/test_web.py::test_send_button_not_hidden_at_or_below_430px` | S01 |

## Verification

Each patch was applied with `git apply` in a scratch worktree of the final code, and the test
above was run 5 times. The worktree was then removed.

| Seed | Failures | Notes |
| --- | --- | --- |
| `k1_race` | 5 of 5 | All 20 threads report success, so the test fails on `successes == 1` (exactly one withdrawal may succeed). A further 20 runs also failed 20 of 20, so the seed needs **no** artificial delay. If a slower runner ever stops reproducing it, add `time.sleep(0.01)` between the read and the `BEGIN` in the seeded code only. |
| `k1_hidden_button` | 5 of 5 | The CSS check reports `('(max-width: 430px)', '.form-actions #send-button')`. |

With either seed applied, every other test in the default run still passes.

## Regenerating a patch

Make the change on a scratch branch, then `git diff > seeds/<name>.patch`, and run
`git apply --check seeds/<name>.patch` on a clean checkout of `main`.
