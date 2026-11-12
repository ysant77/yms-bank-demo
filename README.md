# ledger-demo

A small demo bank service used to exercise an agile-team verifier: accounts, transfers, a
double-entry ledger, statements, interest, reconciliation and a small fraud score.

**All data in this repository is synthetic.** It holds no customer data, no real account
numbers and no credentials. Seeded defects live on `seed/*` branches and are documented in each
branch's first commit.

## Develop

```bash
uv sync
uv run ruff check .
uv run pytest
```

Enable the local hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The pre-commit hook runs `gitleaks` on staged changes. Before any repository goes public, run
`scripts/scan_history.sh` on the full history.
