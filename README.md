# YMS Bank demo (`ledger-demo`)

YMS Bank is a fictional, small demo bank: customers, accounts, a double-entry ledger, transfers and
statements, with a plain web front end. It is a test target for a verifier that checks
developers' claims in an issue tracker against CI results.

**All data in this repository is synthetic and the money is fake.** Customer names come from a
fixed, seeded generator, account numbers use the made-up `YMS-0000-0001` format, and there are
no real customers, account numbers, credentials or secrets. YMS Bank is not a real bank.

## Layout

| Path | What it holds |
| --- | --- |
| `src/bank/money.py` | Money in integer minor units; floats are refused. |
| `src/bank/ledger.py` | Accounts, journal, `deposit`, `withdraw`, `transfer`, `statement`, invariant check. |
| `src/bank/migrate.py`, `migrations/` | Numbered SQL migrations with up and down sections. |
| `src/bank/api.py` | FastAPI routes and the demo session cookie. |
| `src/bank/web/` | HTML, CSS and JavaScript, served as is. |
| `src/bank/seed_data.py` | Synthetic demo database builder. |
| `src/bank/testing/ci_report.py` | Pytest plugin that writes the CI report. |
| `src/fraud/` | Synthetic card fraud score kit: data, features, model, evaluation. |
| `seeds/` | Patches that add known defects for verifier scenarios. |

## Ledger rules

- Every journal entry has lines that sum to zero per currency. Cash entering or leaving the
  bank goes through one clearing account per currency (`YMS-CLR-EUR` and so on).
- Each account stores `balance_minor`, which always equals the sum of its journal lines.
- `deposit`, `withdraw` and `transfer` run in one `BEGIN IMMEDIATE` transaction. A debit is a
  conditional `UPDATE ... WHERE balance_minor >= ?` with a row-count check, so concurrent
  withdrawals cannot overdraw an account. A `CHECK` constraint also refuses negative customer
  balances.
- Migrations: `migrations/NNNN_name.sql` with a `-- migrate:up` and a `-- migrate:down`
  section. `migrate_up(conn)` applies pending ones; `migrate_down(conn, target)` reverts down
  to version `target`. Applied versions are kept in `schema_migrations`.

## Run the app

```bash
uv sync
uv run python -m bank.seed_data --db demo.db   # optional: the app seeds an empty database itself
uv run uvicorn bank.api:app
```

Open <http://127.0.0.1:8000>, choose a demo customer, and use the accounts, transfer and
statement pages. `BANK_DB` selects another database file (default `demo.db`).

There are no passwords. `POST /api/session` with `{"customer_id": N}` sets the `yms_customer`
cookie. The API:

| Route | Purpose |
| --- | --- |
| `GET /api/customers` | Demo customers for the chooser page. |
| `POST`, `GET`, `DELETE /api/session` | Start, read and end the demo session. |
| `GET /api/accounts` | The session customer's accounts. |
| `GET /api/accounts/{id}` | One account. |
| `GET /api/accounts/{id}/statement?from=YYYY-MM-DD&to=YYYY-MM-DD` | Lines with a running balance; both dates inclusive; default is the last 30 days. |
| `POST /api/transfers` | `{"from_account_id", "to_account_number", "amount", "reference"}`; amount is a decimal string. |

Account routes answer 404 for any account the session customer does not own.

## Tests

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest                          # default run, benchmarks excluded
uv run pytest -m bench                 # 1,000-transfer throughput benchmark only
uv run pytest -m "bench or not bench"  # everything
```

The default run includes a 20-thread withdrawal race test, an inclusive statement end-date
test, a test that walks every `/api/accounts/...` route to check the owner rule, and a CSS
check that the Send button stays visible at 430 px and below.

## CI report

```bash
uv run pytest -m "bench or not bench" --ci-report=ci-report.json
```

The report follows the `aat-ci-report/1` format: test ids with outcomes and durations, metrics
recorded with the `record_metric(name, value)` fixture (for example `transfers_per_s`), an
optional `data_hashes` block from the `record_data_hashes(train, live)` fixture, locked
dependency versions from `uv.lock`, a sha256 of every file under `.github/workflows/`, head
and base commits from `CI_HEAD_SHA`/`CI_BASE_SHA` (or `git rev-parse` of `HEAD` and
`CI_BASE_REF`, default `main`), and the SQL operations in each migration. CI runs ruff, the
default tests, then this report run, and uploads `ci-report.json` as an artifact.

## Fraud score kit

`src/fraud/` is a small card fraud score for the ML scenarios. **The data is synthetic and the
model is a demo, not a real fraud system.** No real people, cards, merchants or institutions
are used.

- `data.py` generates 50,000 card transactions over 90 days from a fixed seed, with about 0.5%
  fraud that depends on merchant category, hour, amount and account age. Every fraud gets a
  later chargeback; `chargebacks_30d_before` counts the account's chargebacks in the 30 days
  strictly before the transaction.
- `features.py` builds the feature matrix. Each feature records its source time per row, and
  `past_only_violations` checks that no feature uses data from the transaction time or later.
- `model.py` fits a gradient-boosted model on days 1 to 45, chooses the decision threshold at a
  1% false positive rate on days 46 to 60 (validation), and reports every metric on days 61 to
  90 (holdout). The split is by time, with no shuffling and a fixed random state.
- `evaluate.py` has AUC, PR-AUC, recall at a false positive rate, PSI, false positive rate per
  region with the max/min ratio, and `data_hash` (sha256 of the canonical CSV).

The kit needs numpy and scikit-learn, which are in the optional `fraud` extra so the bank
service does not install them. Without the extra, the fraud tests are skipped. CI installs it.

```bash
uv sync --extra fraud
uv run python -m fraud.model   # train once and print the holdout metrics
```

The tests train once per session and record `auc`, `pr_auc`, `recall_at_threshold`,
`fpr_at_threshold` and `fpr_ratio_max_min` (all on the holdout) with `record_metric`, and the
training (days 1 to 60) and live (days 61 to 90) data hashes with `record_data_hashes`, which
adds a `data_hashes` block to the CI report. On the clean code the holdout AUC is about 0.91.

## Seeds

Known defects live as patches in `seeds/`, never on `main`. See [seeds/README.md](seeds/README.md)
for what each seed does and which scenarios use it.

## Leak checks

Enable the local hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook runs `gitleaks` on staged changes. Before any repository goes public, run
`scripts/scan_history.sh` on the full history.

## Licence

MIT. See [LICENSE](LICENSE).
