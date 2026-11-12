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

Rollback: if anything fails between switching to the target branch and the commit (including
Ctrl-C), the script restores only the paths it touched, moves the branch back to its earlier
tip, returns to where the run started, and deletes the target branch only if this run created
it. If a rollback step fails, it stops and says where you are.

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
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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


def env() -> dict[str, str]:
    """Pathspec magic such as ":/" is never interpreted in a path the script passes to git."""
    return {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """A git command that may fail; the caller checks the return code."""
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, env=env())


def run(*args: str, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            args, cwd=ROOT, check=True, text=True, capture_output=True, env=env()
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or [""]
        raise ScenarioError(f"{args[0]} {args[1]} failed: {detail[0]}") from None
    except FileNotFoundError:
        raise ScenarioError(f"{args[0]} is not installed") from None
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


def safe_path(relative: str) -> Path:
    """A path inside the repository, outside .git, .github/workflows and .githooks."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ScenarioError(f"path must be relative to the repository: {relative!r}")
    if relative.startswith(":") or relative.endswith("/"):
        raise ScenarioError(f"path may not use pathspec magic or name a folder: {relative!r}")
    path = (ROOT / relative).resolve()
    root = ROOT.resolve()
    if not path.is_relative_to(root):
        raise ScenarioError(f"path leaves the repository: {relative!r}")
    if path == root or path.is_dir():
        raise ScenarioError(f"path must name a file, not a folder: {relative!r}")
    # Case-folded, because macOS file systems treat .GIT and .git as the same folder.
    parts = tuple(p.casefold() for p in path.relative_to(root).parts)
    if any(parts[: len(denied)] == denied for denied in DENIED_DIRS):
        raise ScenarioError(f"path is in a protected folder: {relative!r}")
    return path


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
    branch = check_branch(fill(spec["twin_branch"], values), "twin")
    if not re.search(rf"(^|[/-]){re.escape(values['key_lower'])}($|-)", branch):
        raise ScenarioError(f"twin branch {branch!r} must contain the issue key {values['KEY']}")
    for step in attempt["steps"]:
        if "add_test" in step:
            safe_path(step["add_test"])
        for path in step_paths(step, values):
            safe_path(path)
    return branch


def ensure_clean() -> None:
    """A clean tree, including untracked files that status settings may hide, and no file that
    git was told to ignore changes in (assume-unchanged or skip-worktree)."""
    if run("git", "status", "--porcelain", "--untracked-files=all", capture=True):
        raise ScenarioError("working tree is not clean; commit or stash first")
    flagged = [
        line[2:]
        for line in run("git", "ls-files", "-v", capture=True).splitlines()
        if line and (line[0].islower() or line[0] == "S")
    ]
    if flagged:
        raise ScenarioError(f"files marked assume-unchanged or skip-worktree: {flagged[:5]}")


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


def commit(paths: list[str], message: list[str]) -> str:
    """Format, stage and commit only the given paths."""
    for path in paths:
        safe_path(path)
    py = [p for p in paths if p.endswith(".py") and (ROOT / p).exists()]
    if py:
        run("uv", "run", "ruff", "format", "-q", "--", *py)
        run("uv", "run", "ruff", "check", "-q", "--fix", "--", *py)
    run("git", "add", "-A", "--", *sorted(set(paths)))
    args = ["git", "commit", "-q"]
    for part in message:
        args += ["-m", part]
    run(*args)
    left = run("git", "status", "--porcelain", capture=True)
    if left:
        raise ScenarioError(f"changes left out of the commit: {left.splitlines()[:5]}")
    return run("git", "rev-parse", "HEAD", capture=True)


def runs_dir() -> Path:
    """Run records live in the git directory, outside the work tree, so they are never staged."""
    common = Path(run("git", "rev-parse", "--git-common-dir", capture=True))
    return (common if common.is_absolute() else ROOT / common) / "scenario-runs"


def write_run_record(record: dict) -> Path:
    folder = runs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{record['scenario']}-{record['attempt']}-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def current_branch() -> tuple[str, bool]:
    """Where the run starts: (branch name, False) or (commit id, True) for a detached HEAD."""
    name = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if name:
        return name, False
    return run("git", "rev-parse", "HEAD", capture=True), True


def where() -> str:
    name, detached = current_branch()
    return f"detached HEAD at {name[:12]}" if detached else f"branch {name}"


def rollback(
    start: tuple[str, bool], branch: str, created: bool, before: str, touched: list[str]
) -> None:
    """Undo a failed run: only the script's own changes, then go back to where it began.

    1. Move the branch back to ``before`` (its tip right after the switch), keeping files.
    2. Restore every touched path that exists in ``before``; remove the touched paths that did
       not exist there (the script created them: it refuses to write over a file it did not
       create). Nothing else in the tree is touched.
    3. Switch back to the starting branch, or detach at the starting commit.
    4. Delete ``branch`` only if this run created it.
    Stops at the first failed step and says where you are.
    """
    in_before = [p for p in touched if git("cat-file", "-e", f"{before}:{p}").returncode == 0]
    created_paths = [p for p in touched if p not in in_before]
    steps: list[tuple[str, list[str]]] = [("reset --soft", ["reset", "-q", "--soft", before])]
    if in_before:
        restore = ["restore", "-q", f"--source={before}", "--staged", "--worktree", "--"]
        steps.append(("restore", [*restore, *in_before]))
    if created_paths:
        steps.append(
            ("unstage", ["rm", "-q", "--cached", "--ignore-unmatch", "--", *created_paths])
        )
    name, detached = start
    switch = ["switch", "-q", "--detach", name] if detached else ["switch", "-q", name]
    for label, args in steps:
        if not rollback_step(label, args):
            return
    for path in created_paths:
        try:
            (ROOT / path).unlink(missing_ok=True)
        except OSError as exc:
            print(
                f"rollback incomplete: you are on {where()}; removing {path} failed: {exc}",
                file=sys.stderr,
            )
            return
    if not rollback_step("switch back", switch):
        return
    if created:
        rollback_step("delete created branch", ["branch", "-q", "-D", branch])


def rollback_step(label: str, args: list[str]) -> bool:
    result = git(*args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        print(
            f"rollback incomplete: you are on {where()}; {label} failed: {detail[0]}",
            file=sys.stderr,
        )
        return False
    return True


def cmd_base(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    branch = check_branch(spec["base_branch"], "base")
    source = check_ref(args.source, "--from")
    patch = safe_path(f"seeds/{spec['seed']}.patch")
    ensure_clean()
    start = current_branch()
    run("git", "switch", "-q", "--no-track", "-c", branch, source)
    before = run("git", "rev-parse", "HEAD", capture=True)
    paths: list[str] = []
    try:
        paths = patch_paths(patch)
        run("git", "apply", str(patch))
        commit(paths, [seed_subject(spec), "Seeded defect, scripted."])
    except BaseException:
        rollback(start, branch, True, before, paths)
        raise
    print(f"base branch {branch} from {source} with seed {spec['seed']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}")


def apply_step(
    step: dict, values: dict[str, str], spec: dict, clean: str, touched: list[str]
) -> None:
    """Apply one step. Each path is added to ``touched`` after the step's checks and right
    before it is written, so a rollback never treats a file the step did not write as its own."""
    if "revert_seed" in step:
        patch = safe_path(f"seeds/{spec['seed']}.patch")
        touched += patch_paths(patch)
        run("git", "apply", "-R", str(patch))
    elif "restore" in step:
        safe_path(step["restore"])
        touched.append(step["restore"])
        run("git", "checkout", clean, "--", step["restore"])
    elif "replace" in step:
        path = safe_path(step["replace"])
        text = path.read_text(encoding="utf-8")
        if step["old"] not in text:
            raise ScenarioError(f"replace: text not found in {step['replace']}")
        touched.append(step["replace"])
        path.write_text(text.replace(step["old"], step["new"], 1), encoding="utf-8")
    elif "add_test" in step:
        rel = fill(step["dest"], values)
        dest = safe_path(rel)
        tracked = git("cat-file", "-e", f"HEAD:{rel}").returncode == 0
        if dest.exists() and not tracked:
            raise ScenarioError(
                f"add_test: {rel} already exists and is not tracked; not overwritten"
            )
        touched.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fill(step["_template"], values), encoding="utf-8")
    else:
        raise ScenarioError(f"unknown step {step}")


def cmd_attempt(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
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
    ensure_clean()
    # Read templates now: switching branches may remove the scenarios folder.
    for step in attempt["steps"]:
        if "add_test" in step:
            step["_template"] = safe_path(step["add_test"]).read_text(encoding="utf-8")
            check_no_tool_names(step["_template"])
    clean = clean_ref(spec)
    start = current_branch()
    created = not run("git", "branch", "--list", branch, capture=True)
    if not created:
        if git("merge-base", "--is-ancestor", base, f"refs/heads/{branch}").returncode != 0:
            raise ScenarioError(f"existing branch {branch} does not descend from {base}")
        run("git", "switch", "-q", branch)
    else:
        run("git", "switch", "-q", "--no-track", "-c", branch, base)
    before = run("git", "rev-parse", "HEAD", capture=True)
    touched: list[str] = []
    try:
        for step in attempt["steps"]:
            apply_step(step, values, spec, clean, touched)
        sha = commit(touched, commit_message(fill(attempt["commit"], values), twin, author))
    except BaseException:
        rollback(start, branch, created, before, sorted(set(touched)))
        raise
    record = {
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
    print(f"{branch}: attempt {args.attempt} committed; run record {write_run_record(record)}")
    print(f"post in Jira: claim: {attempt['claim']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
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
            fill(spec["pr_title"], values),
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
    try:
        args.fn(args)
    except ScenarioError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
