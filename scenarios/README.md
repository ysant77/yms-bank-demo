# Scenarios

Each `Snn.toml` describes the git side of one scenario run: the seed (known defect) on a base
branch, the twin branch, and one or more attempts. `scripts/scenario.py` builds them. Test
templates use `{KEY}` (for example `BANK-3`) and `{TOKEN}` (`BANK3`), so test names carry the
Jira issue key and the verifier can match them to acceptance criteria.

Every attempt is labelled in its commit body and pull request body as `scripted` (a change
written in advance) or `live` (written during the run). Commit and pull request text never
name a tool or model; for a live attempt the model is recorded only in the untracked run record
under `.runs/`, which the monitor reads.

The generator refuses, before touching git: a protected branch name such as `main`, a base
branch that does not start with `test/`, a twin branch that does not start with `fix/` or
`feat/`, an issue key that does not look like `BANK-3`, and any path outside the repository.
