"""Scenario generator: builds the git side of one scenario run.

For a scenario file `scenarios/Snn.toml` and a Jira issue key, it
1. creates the base branch: a clean ref plus the scenario's seed (the known defect),
2. creates the twin branch and applies one attempt (edits plus acceptance tests),
3. optionally pushes both branches and opens the pull request into the base branch.

Commit and pull request text say only whether a change was `scripted` (written in advance) or
`live` (written during the run). Which model wrote a live change is kept in a run record under
the git directory (`<git dir>/scenario-runs/`), never in the work tree, so it cannot be
committed. The Jira side (ticket, claim) is done in Jira.

Safety: everything is validated before any branch is created. Branch names must start with
`test/` (base) or `fix/` or `feat/` (twin), the twin branch must name the issue key, and no
branch may be a protected name such as `main`. Issue keys must look like `BANK-3`. Every path a
step touches must stay inside the repository and outside `.git`, `.github/workflows` and
`.githooks`. Only the paths a step touched are committed. Any refusal or git failure exits
with code 2.

Isolation: every run works in a temporary linked worktree outside the repository; your
checkout is never changed. One run per branch (a lock under the git common dir). A finished run
(commit, and push if asked) is never undone. An unfinished run that fails or is interrupted
(Ctrl-C, SIGTERM, SIGHUP) removes its worktree and rolls the branch back only where it recorded
what it did, with compare-and-swap; a branch that moved meanwhile is left alone, with a message.
Signals are held while a switch or commit and its record are taken. Known limits: see
scenarios/README.md.

Usage:
  uv run python scripts/scenario.py base S01 [--from origin/main] [--push]
  uv run python scripts/scenario.py attempt S01 v1 --key BANK-3 [--push] [--open-pr]
      [--author live --model NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The temporary worktree of the current run; None outside a run (reads come from ROOT).
TREE: Path | None = None

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
BRANCH_RE = re.compile(r"^(test|fix|feat)/[a-z0-9][a-z0-9._-]*$")
REF_RE = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/~^-]+$")
PROTECTED = {"main", "master", "develop", "release", "ci-reports", "gh-pages"}
AUTHORS = {"scripted", "live"}
DENIED_DIRS = ((".git",), (".github", "workflows"), (".githooks",))

# Fields that end up in commit or pull request text, with the form each must have.
TEXT_FIELDS = {
    "id": re.compile(r"^S\d{2}$"),
    "twin": re.compile(r"^[A-Z][a-z]{1,19}$"),
    "title": re.compile(r"^[A-Za-z0-9][\w ,.:;()'\"/-]{2,99}$"),
    "pr_title": re.compile(r"^\{KEY\}: [\w ,.:;()'\"/-]{3,90}$"),
    "seed": re.compile(r"^k\d+_[a-z0-9_]+$"),
}
COMMIT_RE = re.compile(
    r"^(feat|fix|test|refactor|perf|docs|chore)(\([a-z0-9-]+\))?: [\w ,.:;()'\"/{}-]{3,90}$"
)
ATTEMPT_RE = re.compile(r"^v\d{1,2}$")
# A guard, not a guarantee: attribution phrases and a short list of unambiguous AI product
# names, checked in every scenario field and template that can reach a commit, branch or PR.
TOOL_NAMES_RE = re.compile(
    r"co-authored-by|generated (?:with|by) (?:an? )?(?:ai|claude|chatgpt|copilot|codex|gpt)"
    r"|written (?:by|with) (?:an? )?ai\b"
    r"|claude|chatgpt|openai|anthropic|copilot|\bgpt(?:[-_ ]?\d|\b)",
    re.IGNORECASE,
)
# The allowed step kinds and the exact keys each one takes.
STEP_KEYS = {
    "revert_seed": {"revert_seed"},
    "restore": {"restore"},
    "replace": {"replace", "old", "new"},
    "add_test": {"add_test", "dest"},
}
CLAIM_RE = re.compile(r"^[a-z][a-z ]{1,30}$")


class ScenarioError(Exception):
    """A scenario file, argument or git state that must not reach git."""


# Variables that would point git at another repository, index or object store (for example
# inside a hook). The script always works on ROOT and its own temporary worktree.
GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
)  # fmt: skip


def env() -> dict[str, str]:
    """Pathspec magic such as ":/" is never interpreted in a path the script passes to git, and
    git's location variables from the caller's environment are dropped."""
    clean = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARS}
    return {**clean, "GIT_LITERAL_PATHSPECS": "1"}


def here() -> Path:
    return TREE or ROOT


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """A git command that may fail; the caller checks the return code."""
    return subprocess.run(["git", *args], cwd=here(), text=True, capture_output=True, env=env())


# The command that is running now, so a repeated stop signal can end it (signals_deferred).
CHILD: subprocess.Popen | None = None


def spawn(args: list[str] | tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess:
    """Like subprocess.run with captured text output, but the child is known while it runs. It
    gets its own process group, so the script decides when it stops, and a stop reaches the hooks
    it started too (a hook that still holds the output pipes would otherwise keep us waiting)."""
    global CHILD
    proc = subprocess.Popen(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env(),
        process_group=0,
    )  # fmt: skip
    CHILD = proc
    try:
        out, err = proc.communicate()
    except BaseException:
        stop_group(proc, signal.SIGKILL)
        proc.wait()
        raise
    finally:
        CHILD = None
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def stop_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run(*args: str, capture: bool = False, cwd: Path | None = None) -> str:
    try:
        result = spawn(args, cwd or here())
    except FileNotFoundError:
        raise ScenarioError(f"{args[0]} is not installed") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
        raise ScenarioError(f"{args[0]} {args[1]} failed: {detail[0]}")
    return result.stdout.strip() if capture else ""


def check_text(spec: dict, field: str) -> str:
    value = spec.get(field)
    if not isinstance(value, str) or not TEXT_FIELDS[field].fullmatch(value):
        raise ScenarioError(f"scenario field {field!r} is missing or malformed: {value!r}")
    return value


def load(scenario: str) -> dict:
    if not re.fullmatch(r"S\d{2}", scenario):
        raise ScenarioError(f"scenario id must look like S01, not {scenario!r}")
    path = ROOT / "scenarios" / f"{scenario}.toml"
    try:
        spec = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScenarioError(f"no scenario file scenarios/{scenario}.toml") from None
    except tomllib.TOMLDecodeError as exc:
        raise ScenarioError(f"scenarios/{scenario}.toml is not valid TOML: {exc}") from None
    for field in ("base_branch", "twin_branch"):
        if not isinstance(spec.get(field), str):
            raise ScenarioError(f"scenarios/{scenario}.toml needs {field}")
    attempts = spec.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ScenarioError(f"scenarios/{scenario}.toml needs at least one [[attempts]]")
    for attempt in attempts:
        if not isinstance(attempt, dict) or not isinstance(attempt.get("steps"), list):
            raise ScenarioError(f"scenarios/{scenario}.toml: every attempt needs a steps list")
        for step in attempt["steps"]:
            check_step(step)
            for field in ("restore", "replace", "add_test"):
                if field in step:
                    safe_path(step[field])
    for field in TEXT_FIELDS:
        check_text(spec, field)
    check_no_tool_names(spec)
    if spec["id"] != scenario:
        raise ScenarioError(f"scenarios/{scenario}.toml has id {spec['id']!r}")
    for attempt in attempts:
        if not ATTEMPT_RE.fullmatch(str(attempt.get("name", ""))):
            raise ScenarioError(f"attempt name must look like v1: {attempt.get('name')!r}")
        if not COMMIT_RE.fullmatch(str(attempt.get("commit", ""))):
            raise ScenarioError(f"attempt {attempt['name']}: commit subject is malformed")
        if not CLAIM_RE.fullmatch(str(attempt.get("claim", ""))):
            raise ScenarioError(f"attempt {attempt['name']}: claim is malformed")
        if "twin" in attempt:
            check_text({"twin": attempt["twin"]}, "twin")
        # Two twins on one ticket (S32): an attempt may have its own branch and PR title.
        if "twin_branch" in attempt and not isinstance(attempt["twin_branch"], str):
            raise ScenarioError(f"attempt {attempt['name']}: twin_branch must be a string")
        if "pr_title" in attempt:
            check_text({"pr_title": attempt["pr_title"]}, "pr_title")
    return spec


def check_step(step: object) -> None:
    """A step is one known kind with exactly its keys, all strings (revert_seed: true)."""
    if not isinstance(step, dict):
        raise ScenarioError(f"a step must be a table: {step!r}")
    kinds = [kind for kind in STEP_KEYS if kind in step]
    if len(kinds) != 1 or set(step) != STEP_KEYS[kinds[0]]:
        raise ScenarioError(
            f"a step must be exactly one of {sorted(STEP_KEYS)} with its keys: {step!r}"
        )
    for key, value in step.items():
        ok = value is True if key == "revert_seed" else isinstance(value, str) and value != ""
        if not ok:
            raise ScenarioError(f"step field {key!r} has a bad value: {value!r}")


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def check_no_tool_names(*values: object) -> None:
    for text in (s for value in values for s in strings(value)):
        found = TOOL_NAMES_RE.search(text)
        if found:
            raise ScenarioError(
                f"scenario text may not name an AI tool or attribution: {found[0]!r}"
            )


def check_key(key: str) -> str:
    if not KEY_RE.fullmatch(key):
        raise ScenarioError(f"issue key must look like BANK-3, not {key!r}")
    return key


def check_branch(name: str, kind: str) -> str:
    """Refuse protected names and anything outside the allowed prefixes for this kind."""
    prefixes = ("test/",) if kind == "base" else ("fix/", "feat/")
    if name in PROTECTED or name.split("/")[-1] in PROTECTED:
        raise ScenarioError(f"{kind} branch may not be a protected name: {name!r}")
    if not BRANCH_RE.fullmatch(name) or not name.startswith(prefixes):
        raise ScenarioError(f"{kind} branch must match {'/'.join(prefixes)}<name>, not {name!r}")
    return name


def check_ref(ref: str, what: str) -> str:
    """A ref that names an existing commit, with no option or range syntax."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise ScenarioError(f"{what} is not a plain git ref: {ref!r}")
    found = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0 or not found.stdout.strip():
        raise ScenarioError(f"{what} {ref!r} does not name a commit in this repository")
    return ref


def safe_path(relative: str, write: bool = False) -> Path:
    """A normalised file path inside the current tree, outside .git, .github/workflows and
    .githooks. A write target may not be a symbolic link."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ScenarioError(f"path must be relative to the repository: {relative!r}")
    if relative.startswith(":") or relative.endswith("/") or os.path.normpath(relative) != relative:
        raise ScenarioError(f"path must be a plain normalised file path: {relative!r}")
    tree = here()
    path = (tree / relative).resolve()
    root = tree.resolve()
    if not path.is_relative_to(root):
        raise ScenarioError(f"path leaves the repository: {relative!r}")
    if path == root or path.is_dir():
        raise ScenarioError(f"path must name a file, not a folder: {relative!r}")
    if write and (tree / relative).is_symlink():
        raise ScenarioError(f"path is a symbolic link; not written: {relative!r}")
    # Case-folded, because macOS file systems treat .GIT and .git as the same folder.
    parts = tuple(p.casefold() for p in path.relative_to(root).parts)
    if any(parts[: len(denied)] == denied for denied in DENIED_DIRS):
        raise ScenarioError(f"path is in a protected folder: {relative!r}")
    return tree / relative


def tokens(key: str) -> dict[str, str]:
    return {
        "KEY": key,
        "TOKEN": key.replace("-", ""),
        "token": key.replace("-", "").lower(),
        "key_lower": key.lower(),
    }


def fill(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text


def step_paths(step: dict, values: dict[str, str]) -> list[str]:
    if "restore" in step:
        return [step["restore"]]
    if "replace" in step:
        return [step["replace"]]
    if "add_test" in step:
        return [fill(step["dest"], values)]
    return []


def validate_attempt(spec: dict, attempt: dict, values: dict[str, str]) -> str:
    """Check every branch and path of an attempt before git is touched. Returns the branch."""
    check_branch(spec["base_branch"], "base")
    branch = check_branch(fill(attempt.get("twin_branch", spec["twin_branch"]), values), "twin")
    if not re.search(rf"(^|[/-]){re.escape(values['key_lower'])}($|-)", branch):
        raise ScenarioError(f"twin branch {branch!r} must contain the issue key {values['KEY']}")
    for step in attempt["steps"]:
        if "add_test" in step:
            safe_path(step["add_test"])
        for path in step_paths(step, values):
            safe_path(path)
    return branch


def seed_subject(spec: dict) -> str:
    return f"test(seed): apply {spec['seed']} for scenario {spec['id']}"


def clean_ref(spec: dict) -> str:
    """The commit just before the seed on the base branch: the code without the defect."""
    if "clean_ref" in spec:
        return check_ref(spec["clean_ref"], "clean_ref")
    log = run("git", "log", "--format=%H%x09%s", f"refs/heads/{spec['base_branch']}", capture=True)
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if subject == seed_subject(spec):
            return f"{sha}~1"
    raise ScenarioError(f"no seed commit on {spec['base_branch']}")


def repo_slug() -> str:
    url = run("git", "remote", "get-url", "origin", capture=True)
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(\.git)?$", url)
    if not match:
        raise ScenarioError(f"origin is not a GitHub repository: {url}")
    return match.group(1)


def patch_paths(patch: Path) -> list[str]:
    """Every path a patch touches, including the old name of a renamed file.

    ``git apply --numstat`` lists only the new name of a rename, so the old names come from the
    patch's own ``rename from`` lines. A path git had to quote is refused.
    """
    out = git("apply", "--numstat", "-z", str(patch))
    if out.returncode != 0:
        raise ScenarioError(f"seed patch {patch.name} does not apply: {out.stderr.strip()}")
    paths = [rec.split("\t", 2)[2] for rec in out.stdout.split("\0") if rec.count("\t") >= 2]
    try:
        text = patch.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ScenarioError(f"seed patch {patch.name} is not UTF-8 text") from None
    for line in text.splitlines():
        if line.startswith(("rename from ", "copy from ")):
            paths.append(line.split(" ", 2)[2])
    if any(not p or p.startswith('"') for p in paths):
        raise ScenarioError(f"seed patch {patch.name} has a path git had to quote")
    return sorted(set(paths))


def commit_message(subject: str, twin: str, author: str) -> list[str]:
    """Commit text: subject and a body that names the twin and scripted or live, nothing else."""
    if author not in AUTHORS:
        raise ScenarioError(f"author must be one of {sorted(AUTHORS)}, not {author!r}")
    return [subject, f"Twin change: {twin}, {author}."]


def pr_body(spec: dict, key: str, twin: str, author: str) -> str:
    return (
        f"Twin change: {author} ({twin}). Scenario {spec['id']}: {spec['title']}.\n\n"
        f"Jira: {key}. Base branch `{spec['base_branch']}` holds the seeded defect.\n"
    )


STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


@contextmanager
def signals_deferred() -> Iterator[None]:
    """Hold Ctrl-C, SIGTERM and SIGHUP while a git step and the record of what it did are taken
    together. A signal that arrives meanwhile is only noted, and raised as KeyboardInterrupt right
    after, when the record exists. Done with Python handlers (they always run in the main
    thread), not a thread signal mask, which other threads could bypass.

    A git step can block (a hook that waits), and a signal sent only to this process does not
    reach the child. So a second stop signal ends the running child with SIGTERM, and a third with
    SIGKILL; the step then fails, its record is still taken, and the run rolls back."""
    pending: list[int] = []

    def hold(signum: int, _frame: object) -> None:
        pending.append(signum)
        child = CHILD
        if len(pending) >= 2 and child is not None:
            stop_group(child, signal.SIGTERM if len(pending) == 2 else signal.SIGKILL)

    previous = {s: signal.signal(s, hold) for s in STOP_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if pending:
            raise KeyboardInterrupt(f"signal {pending[0]}")


def commit(paths: list[str], message: list[str], state: RunState | None = None) -> str:
    """Format, stage and commit only the given paths; record the new commit in ``state``."""
    for path in paths:
        safe_path(path)
    py = [p for p in paths if p.endswith(".py") and (here() / p).exists()]
    if py:
        # ruff runs inside the temporary tree (its config, no cache), from the project's
        # environment in the main checkout, which is not written to.
        ruff = ["uv", "run", "--no-sync", "--project", str(ROOT), "ruff"]
        run(*ruff, "format", "-q", "--no-cache", "--", *py)
        run(*ruff, "check", "-q", "--fix", "--no-cache", "--", *py)
    # Exactly the paths the run wrote, even where an ignore rule matches them.
    run("git", "add", "-A", "-f", "--", *sorted(set(paths)))
    args = ["git", "commit", "-q"]
    for part in message:
        args += ["-m", part]
    before = run("git", "rev-parse", "HEAD", capture=True)
    with signals_deferred():
        try:
            run(*args)
        finally:
            # Taken even when the commit was stopped: a commit that exists must be undone.
            if state is not None:
                head = run("git", "rev-parse", "HEAD", capture=True)
                if head != before:
                    state.made = head
    left = run("git", "status", "--porcelain", capture=True)
    if left:
        raise ScenarioError(f"changes left out of the commit: {left.splitlines()[:5]}")
    return run("git", "rev-parse", "HEAD", capture=True)


def run_record(
    spec: dict, args: argparse.Namespace, twin: str, author: str, branch: str, sha: str,
    attempt: dict,
) -> dict:  # fmt: skip
    return {
        "scenario": spec["id"],
        "attempt": args.attempt,
        "key": args.key,
        "twin": twin,
        "author": author,
        "model": args.model if author == "live" else None,
        "branch": branch,
        "commit": sha,
        "claim": attempt["claim"],
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def runs_dir() -> Path:
    """Run records live in the git directory, outside the work tree, so they are never staged."""
    return common_dir() / "scenario-runs"


def write_run_record(record: dict) -> Path:
    folder = runs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{record['scenario']}-{record['attempt']}-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class RunState:
    """What this run did to its branch. Undo acts only on these recorded values."""

    branch: str
    created: bool = False  # this run's own `git switch -c` succeeded
    switched_tip: str | None = None  # the branch tip right after this run's own switch
    made: str | None = None  # the commit this run made
    finished: bool = False  # commit (and push, if asked) done: never rolled back


def undo(state: RunState) -> None:
    """Roll back a failed run, only where this run recorded what it did, with compare-and-swap.

    Nothing happens before this run's own switch succeeded or after the run finished. A branch
    this run created is deleted only if it still points where this run left it; an existing
    branch is set back only if it still points at this run's own commit. Otherwise it is left
    alone and the script says so.
    """
    if state.finished or state.switched_tip is None:
        return
    ref = f"refs/heads/{state.branch}"
    if state.created:
        expected = state.made or state.switched_tip
        result = git("update-ref", "-d", ref, expected)
        what = f"delete {state.branch}, which this run created, at {expected[:12]}"
    elif state.made is not None:
        result = git("update-ref", ref, state.switched_tip, state.made)
        what = f"set {state.branch} back from this run's commit {state.made[:12]}"
    else:
        return
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        print(
            f"rollback incomplete: could not {what}; the branch was left as it is: {detail[0]}",
            file=sys.stderr,
        )


def common_dir() -> Path:
    common = Path(run("git", "rev-parse", "--git-common-dir", capture=True, cwd=ROOT))
    return common if common.is_absolute() else ROOT / common


@contextmanager
def branch_lock(branch: str) -> Iterator[None]:
    """One run per branch at a time: a lock file under the git common dir, with the run's pid.
    A lock whose process is gone is taken over; a live one makes this run refuse."""
    folder = common_dir() / "scenario-locks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (branch.replace("/", "__") + ".lock")
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            try:
                pid = int(path.read_text(encoding="utf-8").split()[0])
                os.kill(pid, 0)
            except ProcessLookupError:
                path.unlink(missing_ok=True)  # the holder is gone
                continue
            except (ValueError, IndexError, PermissionError, OSError):
                pass
            raise ScenarioError(
                f"another run holds {branch} ({path}); wait for it, or remove the file if no run "
                "is active"
            ) from None
    else:
        raise ScenarioError(f"could not take the lock for {branch}: {path}")
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(UTC).isoformat(timespec='seconds')}\n".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def own_temp_tree(path: str) -> bool:
    """A worktree this script made: <system temp dir>/scenario-run-*/tree."""
    candidate = Path(path)
    return (
        candidate.name == "tree"
        and candidate.parent.name.startswith("scenario-run-")
        and candidate.parent.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    )


def check_leftovers(branch: str) -> None:
    """A leftover temporary worktree of this script (under a scenario-run-* temp folder): one
    whose folder is gone is removed by its path; one that still exists and holds the branch is
    named with the exact command. Other worktrees are never touched."""
    listing = run("git", "worktree", "list", "--porcelain", capture=True, cwd=ROOT)
    for block in listing.split("\n\n"):
        lines = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path = lines.get("worktree", "")
        if not own_temp_tree(path):
            continue
        if not Path(path).exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", path],
                cwd=ROOT, capture_output=True, env=env(),
            )  # fmt: skip
            continue
        if lines.get("branch") == f"refs/heads/{branch}":
            raise ScenarioError(
                f"a leftover temporary worktree holds {branch}; remove it with: "
                f"git worktree remove --force {path}"
            )


@contextmanager
def temp_tree(start: str) -> Iterator[Path]:
    """A temporary linked worktree, detached at ``start``, removed again whatever happens."""
    global TREE
    holder = Path(tempfile.mkdtemp(prefix="scenario-run-"))
    tree = holder / "tree"
    try:
        run("git", "worktree", "add", "-q", "--detach", str(tree), start, cwd=ROOT)
        TREE = tree
        yield tree
    finally:
        TREE = None
        # The clean-up is not stopped half way: a stop signal is held until it is done (a
        # repeated one still ends a blocked git step), and anything left is named.
        with signals_deferred():
            removed = spawn(["git", "worktree", "remove", "--force", str(tree)], ROOT)
            shutil.rmtree(holder, ignore_errors=True)
            if holder.exists() or (removed.returncode != 0 and tree.exists()):
                print(
                    f"rollback incomplete: temporary folder {holder} was not removed; "
                    f"run: git worktree remove --force {tree}; rm -rf {holder}",
                    file=sys.stderr,
                )


def branch_tip(branch: str) -> str | None:
    found = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 else None


def switch_into(state: RunState, start_new: bool) -> None:
    """Create or check out the branch in the temporary tree and record that this run did it.

    Signals are held while the switch runs and the record is taken (from the private tree's
    HEAD), so an interrupt at that point is delivered only once the record exists.
    """
    with signals_deferred():
        try:
            if start_new:
                run("git", "switch", "-q", "--no-track", "-c", state.branch)
            else:
                run("git", "switch", "-q", state.branch)  # refused if checked out elsewhere
        finally:
            head = git("symbolic-ref", "-q", "HEAD").stdout.strip()
            if head == f"refs/heads/{state.branch}":
                state.created = start_new
                state.switched_tip = git("rev-parse", "HEAD").stdout.strip()


def cmd_base(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    branch = check_branch(spec["base_branch"], "base")
    source = check_ref(args.source, "--from")
    state = RunState(branch)
    with branch_lock(branch):
        check_leftovers(branch)
        if branch_tip(branch) is not None:
            raise ScenarioError(f"base branch {branch} already exists")
        try:
            with temp_tree(source):
                switch_into(state, start_new=True)
                patch = safe_path(f"seeds/{spec['seed']}.patch")
                paths = patch_paths(patch)
                for path in paths:
                    if (here() / path).exists():
                        safe_path(path, write=True)
                    else:
                        safe_path_new(path)
                run("git", "apply", str(patch))
                commit(paths, [seed_subject(spec), "Seeded defect, scripted."], state)
                if args.push:
                    push(branch)
                state.finished = True
        except BaseException:
            if args.push and pushed_before_interrupt(branch, state):
                print(f"interrupted after the push of {branch} completed; the branch is kept")
            undo(state)
            raise
    print(
        f"base branch {branch} from {source} with seed {spec['seed']} (your checkout is unchanged)"
    )


def safe_path_new(relative: str) -> Path:
    """A path the run will create: it must not exist yet in the tree (not even ignored)."""
    path = safe_path_parent(relative)
    if path.exists() or path.is_symlink():
        raise ScenarioError(f"{relative} already exists; not overwritten")
    return path


def safe_path_parent(relative: str) -> Path:
    """Like safe_path for a file that does not exist yet: checks the path, not the file."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ScenarioError(f"path must be relative to the repository: {relative!r}")
    if relative.startswith(":") or relative.endswith("/") or os.path.normpath(relative) != relative:
        raise ScenarioError(f"path must be a plain normalised file path: {relative!r}")
    tree = here()
    parent = (tree / relative).parent.resolve()
    if not parent.is_relative_to(tree.resolve()):
        raise ScenarioError(f"path leaves the repository: {relative!r}")
    parts = tuple(p.casefold() for p in Path(relative).parts)
    if any(parts[: len(denied)] == denied for denied in DENIED_DIRS):
        raise ScenarioError(f"path is in a protected folder: {relative!r}")
    return tree / relative


def remote_has(branch: str, sha: str) -> bool:
    """Whether origin's branch points at ``sha`` (the push completed before an interrupt)."""
    found = git("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}")
    return found.returncode == 0 and found.stdout.split()[:1] == [sha]


def push(branch: str) -> None:
    run("git", "push", "-q", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}")


def pushed_before_interrupt(branch: str, state: RunState) -> bool:
    """An interrupt came after the remote accepted the push: the run is done. The upstream
    setting that `push -u` makes last may be missing, so it is set here."""
    if not (state.made and remote_has(branch, state.made)):
        return False
    state.finished = True
    git("config", f"branch.{branch}.remote", "origin")
    git("config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    return True


def apply_step(step: dict, values: dict[str, str], spec: dict, clean: str) -> list[str]:
    """Apply one step in the temporary tree. Returns the paths it wrote."""
    if "revert_seed" in step:
        patch = safe_path(f"seeds/{spec['seed']}.patch")
        paths = patch_paths(patch)
        run("git", "apply", "-R", str(patch))
        return paths
    if "restore" in step:
        safe_path_parent(step["restore"])
        if (here() / step["restore"]).is_symlink():
            raise ScenarioError(f"path is a symbolic link; not written: {step['restore']!r}")
        run("git", "checkout", clean, "--", step["restore"])
        return [step["restore"]]
    if "replace" in step:
        path = (
            safe_path(step["replace"], write=True) if (here() / step["replace"]).exists() else None
        )
        if path is None:
            raise ScenarioError(f"replace: {step['replace']} does not exist")
        text = path.read_text(encoding="utf-8")
        if step["old"] not in text:
            raise ScenarioError(f"replace: text not found in {step['replace']}")
        path.write_text(text.replace(step["old"], step["new"], 1), encoding="utf-8")
        return [step["replace"]]
    if "add_test" in step:
        rel = fill(step["dest"], values)
        text = fill(step["_template"], values)
        if (here() / rel).exists() or (here() / rel).is_symlink():
            # An existing tracked file is never overwritten. Re-adding the same test (as a later
            # attempt of the same scenario does) is allowed only if the content is identical.
            dest = safe_path(rel, write=True)
            if dest.read_text(encoding="utf-8") != text:
                raise ScenarioError(f"add_test: {rel} exists with other content; not overwritten")
            return []
        dest = safe_path_new(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return [rel]
    raise ScenarioError(f"unknown step {step}")


def cmd_attempt(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    check_no_tool_names(args.key)
    values = tokens(check_key(args.key))
    attempt = next((a for a in spec["attempts"] if a["name"] == args.attempt), None)
    if attempt is None:
        raise ScenarioError(f"no attempt {args.attempt} in {args.scenario}")
    author = args.author or spec.get("author", "scripted")
    if author not in AUTHORS:
        raise ScenarioError(f"author must be one of {sorted(AUTHORS)}, not {author!r}")
    if author == "live" and not args.model:
        raise ScenarioError("a live attempt needs --model (kept in the run record only)")
    branch = validate_attempt(spec, attempt, values)
    twin = attempt.get("twin", spec["twin"])
    base = check_ref(f"refs/heads/{spec['base_branch']}", "base branch")
    # Templates come from the scenario folder of your checkout, read before the run.
    for step in attempt["steps"]:
        if "add_test" in step:
            step["_template"] = safe_path(step["add_test"]).read_text(encoding="utf-8")
            check_no_tool_names(step["_template"])
    clean = clean_ref(spec)
    state = RunState(branch)
    with branch_lock(branch):
        check_leftovers(branch)
        before = branch_tip(branch)
        if (
            before is not None
            and git("merge-base", "--is-ancestor", base, f"refs/heads/{branch}").returncode != 0
        ):
            raise ScenarioError(f"existing branch {branch} does not descend from {base}")
        try:
            with temp_tree(base if before is None else f"refs/heads/{branch}"):
                switch_into(state, start_new=before is None)
                written: list[str] = []
                for step in attempt["steps"]:
                    written += apply_step(step, values, spec, clean)
                message = commit_message(fill(attempt["commit"], values), twin, author)
                sha = commit(written, message, state)
                if args.push:
                    push(branch)
                state.finished = True
        except BaseException:
            if args.push and pushed_before_interrupt(branch, state):
                # The run is done; its record is written before the interrupt goes on.
                done = run_record(spec, args, twin, author, branch, state.made, attempt)
                path = write_run_record(done)
                print(f"interrupted after the push of {branch} completed; run record {path}")
            undo(state)
            raise
    record = run_record(spec, args, twin, author, branch, sha, attempt)
    print(f"{branch}: attempt {args.attempt} committed (your checkout is unchanged); "
          f"run record {write_run_record(record)}")  # fmt: skip
    print(f"post in Jira: claim: {attempt['claim']}")
    if args.open_pr:
        run(
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug(),
            "--base",
            spec["base_branch"],
            "--head",
            branch,
            "--title",
            fill(attempt.get("pr_title", spec["pr_title"]), values),
            "--body",
            pr_body(spec, args.key, twin, author),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("base")
    base.add_argument("scenario")
    base.add_argument("--from", dest="source", default="origin/main")
    base.add_argument("--push", action="store_true")
    base.set_defaults(fn=cmd_base)
    attempt = sub.add_parser("attempt")
    attempt.add_argument("scenario")
    attempt.add_argument("attempt")
    attempt.add_argument("--key", required=True)
    attempt.add_argument("--author", choices=sorted(AUTHORS))
    attempt.add_argument("--model", help="model name for a live attempt; run record only")
    attempt.add_argument("--push", action="store_true")
    attempt.add_argument("--open-pr", action="store_true")
    attempt.set_defaults(fn=cmd_attempt)
    args = parser.parse_args(argv)

    def stop(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    # SIGTERM and SIGHUP roll back like Ctrl-C does.
    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        args.fn(args)
    except ScenarioError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
