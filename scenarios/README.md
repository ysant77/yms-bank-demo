# Scenarios

Each `Snn.toml` describes the git side of one scenario run: the seed (known defect) on a base
branch, the twin branch, and one or more attempts. `scripts/scenario.py` builds them. Test
templates use `{KEY}` (for example `BANK-3`) and `{TOKEN}` (`BANK3`), so test names carry the
Jira issue key and the verifier can match them to acceptance criteria.

Every attempt is labelled in its commit body and pull request body as `scripted` (a change
written in advance) or `live` (written during the run). Commit and pull request text never
name a tool or model; for a live attempt the model is recorded only in the untracked run record
under the git directory (`.git/scenario-runs/`), outside the work tree, so it is never
committed. The monitor reads it from there.

The generator refuses, before touching git:
- a protected branch name such as `main`, a base branch that does not start with `test/`, a twin
  branch that does not start with `fix/` or `feat/` or does not name the issue key on a token
  boundary, and an existing twin branch that does not descend from the base branch;
- an issue key that does not look like `BANK-3`, and a `--from` or `clean_ref` that is not a
  plain existing ref (branches are always read as `refs/heads/<name>`, so a tag cannot stand in);
- a scenario file without `base_branch`, `twin_branch`, attempts or well-formed steps, and fields
  (`id`, `twin`, `title`, `pr_title`, `seed`, commit subject, claim) without their expected form;
- any path outside the repository, inside `.git`, `.github/workflows` or `.githooks` (compared
  case-folded), naming a folder or the repository root, or starting with `:` (pathspec magic;
  git also runs with `GIT_LITERAL_PATHSPECS=1`);
- a work tree with any change, including untracked files that status settings hide, and files
  marked assume-unchanged or skip-worktree;
- attribution phrases (such as a co-author trailer or "generated with" a named tool) and a short
  list of unambiguous AI product names in any scenario field or test template. This is a guard,
  not a guarantee: it cannot recognise every name, and ordinary words are allowed on purpose.

It writes a test file only where no untracked or ignored file exists, and it commits only the
paths that the seed or the attempt's steps touched. Every refusal, a missing scenario file or a
failed git command exits with code 2 and a line that starts with `refused:`.

If anything fails after the switch to the target branch and before the commit (also Ctrl-C), the
run is rolled back: the branch moves back to its earlier tip, only the touched paths are restored
or removed, the run returns to the branch or detached commit it started from, and the target
branch is deleted only if this same run created it. If a rollback step fails, it stops and
prints `rollback incomplete: you are on ...`.
