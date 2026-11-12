"""Scenario generator: builds the git side of one scenario run.

For a scenario file `scenarios/Snn.toml` and a Jira issue key, it
1. creates the base branch: a clean ref plus the scenario's seed (the known defect),
2. creates the twin branch and applies one attempt (edits plus acceptance tests),
3. optionally pushes both branches and opens the pull request into the base branch.

Every attempt is labelled `scripted` or `live-model (<name>)` in its commit body and in the
first line of the pull request body. The Jira side (ticket, claim comment) is done in Jira.

Usage:
  uv run python scripts/scenario.py base S01 --key BANK-3 [--from origin/main] [--push]
  uv run python scripts/scenario.py attempt S01 v1 --key BANK-3 [--push] [--open-pr]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def load(scenario: str) -> dict:
    return tomllib.loads((ROOT / "scenarios" / f"{scenario}.toml").read_text(encoding="utf-8"))


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


def ensure_clean() -> None:
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("working tree is not clean; commit or stash first")


def commit(message: str, label: str) -> None:
    changed = run("git", "diff", "--name-only", "HEAD", capture=True).split()
    new = run("git", "ls-files", "--others", "--exclude-standard", capture=True).split()
    py = [f for f in changed + new if f.endswith(".py") and (ROOT / f).exists()]
    if py:
        run("uv", "run", "ruff", "format", "-q", *py)
        run("uv", "run", "ruff", "check", "-q", "--fix", *py)
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", message, "-m", f"Twin change: {label}.")


def cmd_base(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    ensure_clean()
    run("git", "switch", "-q", "-c", spec["base_branch"], args.source)
    run("git", "apply", f"seeds/{spec['seed']}.patch")
    commit(f"test(seed): apply {spec['seed']} for scenario {spec['id']}", "seeded defect, scripted")
    print(f"base branch {spec['base_branch']} from {args.source} with seed {spec['seed']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", spec["base_branch"])


def apply_step(step: dict, values: dict[str, str], spec: dict, clean_ref: str) -> None:
    if "revert_seed" in step:
        run("git", "apply", "-R", f"seeds/{spec['seed']}.patch")
    elif "restore" in step:
        run("git", "checkout", clean_ref, "--", step["restore"])
    elif "replace" in step:
        path = ROOT / step["replace"]
        text = path.read_text(encoding="utf-8")
        if step["old"] not in text:
            sys.exit(f"replace: text not found in {step['replace']}")
        path.write_text(text.replace(step["old"], step["new"], 1), encoding="utf-8")
    elif "add_test" in step:
        template = step["_template"]
        dest = ROOT / fill(step["dest"], values)
        dest.write_text(fill(template, values), encoding="utf-8")
    else:
        sys.exit(f"unknown step {step}")


def cmd_attempt(args: argparse.Namespace) -> None:
    spec = load(args.scenario)
    values = tokens(args.key)
    attempt = next((a for a in spec["attempts"] if a["name"] == args.attempt), None)
    if attempt is None:
        sys.exit(f"no attempt {args.attempt} in {args.scenario}")
    ensure_clean()
    # Read templates now: switching branches may remove the scenarios folder.
    for step in attempt["steps"]:
        if "add_test" in step:
            step["_template"] = (ROOT / step["add_test"]).read_text(encoding="utf-8")
    branch = fill(spec["twin_branch"], values)
    exists = run("git", "branch", "--list", branch, capture=True)
    if exists:
        run("git", "switch", "-q", branch)
    else:
        run("git", "switch", "-q", "-c", branch, spec["base_branch"])
    for step in attempt["steps"]:
        # The clean code is the base branch's parent: the commit before the seed.
        apply_step(step, values, spec, spec.get("clean_ref", f"{spec['base_branch']}~1"))
    label = spec.get("author", "scripted")
    twin = attempt.get("twin", spec["twin"])  # an attempt may name its own twin
    commit(fill(attempt["commit"], values), f"{twin}, {label}")
    print(f"{branch}: attempt {args.attempt} committed; post in Jira: claim: {attempt['claim']}")
    if args.push:
        run("git", "push", "-q", "-u", "origin", branch)
    if args.open_pr:
        title = fill(spec["pr_title"], values)
        body = (
            f"Twin change: {label} ({twin}). Scenario {spec['id']}: {spec['title']}.\n\n"
            f"Jira: {args.key}. Base branch `{spec['base_branch']}` holds the seeded defect.\n"
        )
        run(
            "gh",
            "pr",
            "create",
            "--base",
            spec["base_branch"],
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        )


def main() -> None:
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
    attempt.add_argument("--push", action="store_true")
    attempt.add_argument("--open-pr", action="store_true")
    attempt.set_defaults(fn=cmd_attempt)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
