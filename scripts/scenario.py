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

Usage:
  uv run python scripts/scenario.py base S01 [--from origin/main] [--push]
  uv run python scripts/scenario.py attempt S01 v1 --key BANK-3 [--push] [--open-pr]
      [--author live --model NAME]
"""

from __future__ import annotations

import argparse
import json
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
CLAIM_RE = re.compile(r"^[a-z][a-z ]{1,30}$")


class ScenarioError(Exception):
    """A scenario file, argument or git state that must not reach git."""


def run(*args: str, capture: bool = False) -> str:
    try:
        result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True)
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
    for field in TEXT_FIELDS:
        check_text(spec, field)
    if spec["id"] != scenario:
        raise ScenarioError(f"scenarios/{scenario}.toml has id {spec['id']!r}")
    for attempt in spec.get("attempts", []):
        if not ATTEMPT_RE.fullmatch(str(attempt.get("name", ""))):
            raise ScenarioError(f"attempt name must look like v1: {attempt.get('name')!r}")
        if not COMMIT_RE.fullmatch(str(attempt.get("commit", ""))):
            raise ScenarioError(f"attempt {attempt['name']}: commit subject is malformed")
        if not CLAIM_RE.fullmatch(str(attempt.get("claim", ""))):
            raise ScenarioError(f"attempt {attempt['name']}: claim is malformed")
        if "twin" in attempt:
            check_text({"twin": attempt["twin"]}, "twin")
    return spec


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
    if not run("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", capture=True):
        raise ScenarioError(f"{what} {ref!r} is not a commit")
    return ref


def safe_path(relative: str) -> Path:
    """A path inside the repository, outside .git, .github/workflows and .githooks."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ScenarioError(f"path must be relative to the repository: {relative!r}")
    path = (ROOT / relative).resolve()
    root = ROOT.resolve()
    if not path.is_relative_to(root):
        raise ScenarioError(f"path leaves the repository: {relative!r}")
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
    if values["key_lower"] not in branch:
        raise ScenarioError(f"twin branch {branch!r} must contain the issue key {values['KEY']}")
    for step in attempt["steps"]:
        if "add_test" in step:
            safe_path(step["add_test"])
        for path in step_paths(step, values):
            safe_path(path)
    return branch


def ensure_clean() -> None:
    if run("git", "status", "--porcelain", capture=True):
        raise ScenarioError("working tree is not clean; commit or stash first")


def seed_subject(spec: dict) -> str:
    return f"test(seed): apply {spec['seed']} for scenario {spec['id']}"


def clean_ref(spec: dict) -> str:
    """The commit just before the seed on the base branch: the code without the defect."""
    if "clean_ref" in spec:
        return check_ref(spec["clean_ref"], "clean_ref")
    log = run("git", "log", "--format=%H%x09%s", spec["base_branch"], capture=True)
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
    out = run("git", "apply", "--numstat", str(patch), capture=True)
    return [line.split("\t")[2] for line in out.splitlines() if line.count("\t") >= 2]


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


def cmd_base(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    branch = check_branch(spec["base_branch"], "base")
    source = check_ref(args.source, "--from")
    patch = safe_path(f"seeds/{spec['seed']}.patch")
    ensure_clean()
    run("git", "switch", "-q", "--no-track", "-c", branch, source)
    paths = patch_paths(patch)
    run("git", "apply", str(patch))
    commit(paths, [seed_subject(spec), "Seeded defect, scripted."])
    print(f"base branch {branch} from {source} with seed {spec['seed']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", branch)


def apply_step(step: dict, values: dict[str, str], spec: dict, clean: str) -> list[str]:
    """Apply one step. Returns the paths it touched."""
    if "revert_seed" in step:
        patch = safe_path(f"seeds/{spec['seed']}.patch")
        paths = patch_paths(patch)
        run("git", "apply", "-R", str(patch))
        return paths
    if "restore" in step:
        safe_path(step["restore"])
        run("git", "checkout", clean, "--", step["restore"])
    elif "replace" in step:
        path = safe_path(step["replace"])
        text = path.read_text(encoding="utf-8")
        if step["old"] not in text:
            raise ScenarioError(f"replace: text not found in {step['replace']}")
        path.write_text(text.replace(step["old"], step["new"], 1), encoding="utf-8")
    elif "add_test" in step:
        dest = safe_path(fill(step["dest"], values))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fill(step["_template"], values), encoding="utf-8")
    else:
        raise ScenarioError(f"unknown step {step}")
    return step_paths(step, values)


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
    base = check_ref(spec["base_branch"], "base branch")
    ensure_clean()
    # Read templates now: switching branches may remove the scenarios folder.
    for step in attempt["steps"]:
        if "add_test" in step:
            step["_template"] = safe_path(step["add_test"]).read_text(encoding="utf-8")
    clean = clean_ref(spec)
    if run("git", "branch", "--list", branch, capture=True):
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, branch], cwd=ROOT, check=False
        )
        if ancestor.returncode != 0:
            raise ScenarioError(f"existing branch {branch} does not descend from {base}")
        run("git", "switch", "-q", branch)
    else:
        run("git", "switch", "-q", "--no-track", "-c", branch, base)
    touched: list[str] = []
    for step in attempt["steps"]:
        touched += apply_step(step, values, spec, clean)
    sha = commit(touched, commit_message(fill(attempt["commit"], values), twin, author))
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
        run("git", "push", "-q", "-u", "origin", branch)
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
