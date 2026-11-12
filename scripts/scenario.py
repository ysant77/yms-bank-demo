"""Scenario generator: builds the git side of one scenario run.

For a scenario file `scenarios/Snn.toml` and a Jira issue key, it
1. creates the base branch: a clean ref plus the scenario's seed (the known defect),
2. creates the twin branch and applies one attempt (edits plus acceptance tests),
3. optionally pushes both branches and opens the pull request into the base branch.

Commit and pull request text say only whether a change was `scripted` (written in advance) or
`live` (written during the run). Which model wrote a live change is kept in a run record under
`.runs/` (untracked), which the monitor reads. The Jira side (ticket, claim) is done in Jira.

Safety: everything is validated before any branch is created. Branch names must start with
`test/` (base) or `fix/` or `feat/` (twin) and may never be a protected name such as `main`;
issue keys must look like `BANK-3`; every path a step touches must stay inside the repository.

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
RUNS = ROOT / ".runs"

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
BRANCH_RE = re.compile(r"^(test|fix|feat)/[a-z0-9][a-z0-9._-]*$")
PROTECTED = {"main", "master", "develop", "release", "ci-reports", "gh-pages"}
AUTHORS = {"scripted", "live"}


class ScenarioError(Exception):
    """A scenario file or argument that must not reach git."""


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def load(scenario: str) -> dict:
    if not re.fullmatch(r"S\d{2}", scenario):
        raise ScenarioError(f"scenario id must look like S01, not {scenario!r}")
    return tomllib.loads((ROOT / "scenarios" / f"{scenario}.toml").read_text(encoding="utf-8"))


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


def safe_path(relative: str) -> Path:
    """A path inside the repository, outside .git."""
    if Path(relative).is_absolute():
        raise ScenarioError(f"path must be relative to the repository: {relative!r}")
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path.is_relative_to(ROOT / ".git"):
        raise ScenarioError(f"path leaves the repository: {relative!r}")
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


def validate_attempt(spec: dict, attempt: dict, values: dict[str, str]) -> str:
    """Check every branch and path of an attempt before git is touched. Returns the branch."""
    check_branch(spec["base_branch"], "base")
    branch = check_branch(fill(spec["twin_branch"], values), "twin")
    for step in attempt["steps"]:
        for field in ("restore", "replace"):
            if field in step:
                safe_path(step[field])
        if "add_test" in step:
            safe_path(step["add_test"])
            safe_path(fill(step["dest"], values))
    return branch


def ensure_clean() -> None:
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("working tree is not clean; commit or stash first")


def seed_subject(spec: dict) -> str:
    return f"test(seed): apply {spec['seed']} for scenario {spec['id']}"


def clean_ref(spec: dict) -> str:
    """The commit just before the seed on the base branch: the code without the defect."""
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


def commit(message: str, label: str) -> str:
    changed = run("git", "diff", "--name-only", "HEAD", capture=True).split()
    new = run("git", "ls-files", "--others", "--exclude-standard", capture=True).split()
    py = [f for f in changed + new if f.endswith(".py") and (ROOT / f).exists()]
    if py:
        run("uv", "run", "ruff", "format", "-q", *py)
        run("uv", "run", "ruff", "check", "-q", "--fix", *py)
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", message, "-m", f"Twin change: {label}.")
    return run("git", "rev-parse", "HEAD", capture=True)


def write_run_record(record: dict) -> Path:
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS / f"{record['scenario']}-{record['attempt']}-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_base(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    branch = check_branch(spec["base_branch"], "base")
    ensure_clean()
    run("git", "switch", "-q", "-c", branch, args.source)
    run("git", "apply", str(safe_path(f"seeds/{spec['seed']}.patch")))
    commit(seed_subject(spec), "seeded defect, scripted")
    print(f"base branch {branch} from {args.source} with seed {spec['seed']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", branch)


def apply_step(step: dict, values: dict[str, str], spec: dict, clean: str) -> None:
    if "revert_seed" in step:
        run("git", "apply", "-R", str(safe_path(f"seeds/{spec['seed']}.patch")))
    elif "restore" in step:
        safe_path(step["restore"])
        run("git", "checkout", clean, "--", step["restore"])
    elif "replace" in step:
        path = safe_path(step["replace"])
        text = path.read_text(encoding="utf-8")
        if step["old"] not in text:
            sys.exit(f"replace: text not found in {step['replace']}")
        path.write_text(text.replace(step["old"], step["new"], 1), encoding="utf-8")
    elif "add_test" in step:
        safe_path(fill(step["dest"], values)).write_text(
            fill(step["_template"], values), encoding="utf-8"
        )
    else:
        sys.exit(f"unknown step {step}")


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
    ensure_clean()
    # Read templates now: switching branches may remove the scenarios folder.
    for step in attempt["steps"]:
        if "add_test" in step:
            step["_template"] = safe_path(step["add_test"]).read_text(encoding="utf-8")
    clean = spec.get("clean_ref") or clean_ref(spec)
    if run("git", "branch", "--list", branch, capture=True):
        run("git", "switch", "-q", branch)
    else:
        run("git", "switch", "-q", "-c", branch, spec["base_branch"])
    for step in attempt["steps"]:
        apply_step(step, values, spec, clean)
    sha = commit(fill(attempt["commit"], values), f"{twin}, {author}")
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
        body = (
            f"Twin change: {author} ({twin}). Scenario {spec['id']}: {spec['title']}.\n\n"
            f"Jira: {args.key}. Base branch `{spec['base_branch']}` holds the seeded defect.\n"
        )
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
            body,
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
