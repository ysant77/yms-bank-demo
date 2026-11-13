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
  case-folded), not normalised (such as `seeds/../notes.txt`), naming a folder or the repository
  root, or starting with `:` (pathspec magic; git also runs with `GIT_LITERAL_PATHSPECS=1`), and
  a symbolic link as a write target;
- an issue key or any scenario field that names an AI tool (see below);
- attribution phrases (such as a co-author trailer or "generated with" a named tool) and a short
  list of unambiguous AI product names in any scenario field or test template. This is a guard,
  not a guarantee: it cannot recognise every name, and ordinary words are allowed on purpose.

Every run works in a temporary linked worktree (`git worktree add --detach`) outside the
repository; your checkout (working tree, index, HEAD, ignored and untracked files) is never
changed. Git's location variables (`GIT_DIR`, `GIT_INDEX_FILE` and similar) from the caller's
environment are ignored. Only one run per branch at a time: a lock file under the git common
directory (with the run's process id) makes a second run refuse; a lock whose process is gone is
taken over. A leftover temporary worktree from an interrupted run is named with the exact
`git worktree remove --force <path>` command.

A run is *finished* when its commit (and its push, if asked) succeeded; a finished run is never
undone, even if it is interrupted while cleaning up. If an unfinished run fails or is interrupted
(Ctrl-C, SIGTERM or SIGHUP), the temporary worktree is removed and the branch is rolled back only
where this run recorded what it did, with compare-and-swap (`git update-ref`): a branch that this
run's own `git switch -c` created is deleted only if it still points where this run left it; an
existing branch is set back to the tip it had right after this run's switch only if it still
points at this run's own commit. If the branch has moved in the meantime (another run or the user
committed), it is left alone and the script prints `rollback incomplete: ...`.
Every refusal, a missing scenario file or a failed git command exits with code 2 and a line that
starts with `refused:`.
