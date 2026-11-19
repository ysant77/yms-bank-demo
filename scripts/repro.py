"""Run the pull request's test files on the base commit and write the repro block (DESIGN 15b).

    uv run python scripts/repro.py --base <sha> --head <sha> --head-report ci-report.json

1. Clone the repository (shared objects, ordinary ``.git`` folder) at the merge base with main.
2. Copy onto it only the test modules that the pull request adds, changes or renames under
   ``tests/``, from the head. Deleted test files are recorded, not copied. Every other file,
   ``conftest.py`` included, stays as on base.
3. Run those files on base with the report plugin and the base ``src`` first on the path
   (``PYTHONPATH=src``, the same value as the head report step).
4. Pair each head test of those files with its base outcome and exception type, and write the
   block into the head report as ``repro``, with the full-suite result of the head run.

The block also records ``head_sha``, the base run (``rc``, ``collected``, ``report``), the
changed pytest support and config files (``changed_support``), the removed test files, and
the test ids that the base commit has in every test module the PR changes, renames or removes
(``base_test_ids``, with the collection exit code and the modules that gave no id in
``base_collect``), so a dropped test is visible even when git reads the change as a rename. The
step fails (exit 1) when there are test files but no base report, or when the head report is for
another commit. The script never decides anything: the verifier reads the block.

Limits (the block proves separation, not correctness):
- A test can detect the base run (for example from the commit or the paths) and fail on purpose.
  The base run looks like an ordinary checkout, but it cannot be hidden.
- An ``assert`` in production code, or a bare ``raise AssertionError``, also records
  ``AssertionError`` on base.
- Each test runs once on base. A flaky test can fail on base by chance.
- When ``changed_support`` is not empty, the base run used base support files with head tests,
  so the verifier treats the result as unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pinned import build as build_pinned  # noqa: E402

FAILED = {"failed", "error"}
# Files that change how pytest collects or runs tests, besides the test modules themselves.
SUPPORT_FILES = {"pyproject.toml", "uv.lock", "pytest.ini", "setup.cfg", "tox.ini", ".coveragerc"}
# Variables of the CI step that runs this script; the head test run does not have them.
SCRIPT_VARS = {"BASE_SHA", "HEAD_SHA"}
# A test module path that may reach pytest: under tests/, plain names, no part starts with . or -
TEST_PATH = re.compile(r"tests/(?:[\w][\w.-]*/)*test_[\w.-]*\.py")


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(  # noqa: S603 fixed git argument list, no shell; git from the runner PATH
        ["git", *args],  # noqa: S607 fixed git argument list, no shell; git from the runner PATH
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def is_test_module(f: str) -> bool:
    return f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py")


def is_support(f: str) -> bool:
    return (
        Path(f).name == "conftest.py"
        or f in SUPPORT_FILES
        or (f.startswith("tests/") and not is_test_module(f))
        # The workflow and the CI scripts decide what runs and what the report says: a PR can add
        # "if: false" to a required job or change this script.
        or f.startswith((".github/", "scripts/"))
    )


def test_files(changed: list[str]) -> list[str]:
    """Test modules the pull request adds or changes; conftest.py and data files are left out."""
    return sorted(f for f in changed if is_test_module(f))


def checked_test_paths(tree: Path, paths: list[str]) -> tuple[list[str], list[dict]]:
    """Split the test paths from the PR diff into paths that may reach pytest and refused paths.

    A path is refused when it starts with "-" (pytest could read it as an option), is absolute,
    has a ".." part, does not match ``TEST_PATH``, or resolves (symlinks too) outside ``tree``.
    The paths come from the PR, so they are untrusted input."""
    top = tree.resolve()
    ok, refused = [], []
    for p in paths:
        if p.startswith("-"):
            reason = "starts with -"
        elif Path(p).is_absolute():
            reason = "is absolute"
        elif ".." in Path(p).parts:
            reason = "has a parent reference"
        elif not TEST_PATH.fullmatch(p):
            reason = "is not a test module path"
        elif not (tree / p).resolve().is_relative_to(top):
            reason = "resolves outside the repository"
        else:
            ok.append(p)
            continue
        refused.append({"path": p, "reason": reason})
    return ok, refused


def changed_paths(root: Path, base_sha: str, head_sha: str) -> dict[str, list[str]]:
    """Test modules to copy, test modules removed, changed support files, and the base-side paths
    of every test module the PR changes, renames or removes."""
    # -z: paths are not quoted, so a non-ASCII or unusual name is read as it is.
    out = git("diff", "--name-status", "-z", "-M", f"{base_sha}...{head_sha}", cwd=root)
    fields = out.split("\0")
    entries = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        n = 2 if status[0] in "RC" else 1
        entries.append((status, fields[i + 1 : i + 1 + n]))
        i += 1 + n
    tests, removed, support, base_side = set(), set(), set(), set()
    for status, paths in entries:
        if not status.startswith("A") and is_test_module(paths[0]):
            base_side.add(paths[0])
        if status.startswith("D"):
            if is_test_module(paths[0]):
                removed.add(paths[0])
        elif is_test_module(paths[-1]):
            tests.add(paths[-1])
        if status.startswith("R") and is_test_module(paths[0]) and not is_test_module(paths[-1]):
            removed.add(paths[0])
        support.update(p for p in paths if is_support(p))
    return {
        "tests": sorted(tests),
        "removed_tests": sorted(removed),
        "support": sorted(support),
        "base_side": sorted(base_side),
    }


def build_block(
    base_sha: str,
    files: list[str],
    base: dict,
    head: dict,
    *,
    head_sha: str | None = None,
    base_run: dict | None = None,
    support: list[str] | None = None,
    removed: list[str] | None = None,
    base_test_ids: list[str] | None = None,
    base_collect: dict | None = None,
) -> dict:
    base_tests = {t["id"]: t for t in base.get("tests", [])}
    broken = {e["id"]: e.get("exc_type") for e in base.get("collect_errors", [])}
    rows = []
    for t in head.get("tests", []):
        file = t["id"].split("::")[0]
        if file not in files:
            continue
        if t["id"] in base_tests:
            b = base_tests[t["id"]]
            base_result = {"outcome": b["outcome"], "exc_type": b.get("exc_type")}
            if b.get("exc_message"):
                base_result["exc_message"] = b["exc_message"]
        else:
            # Not collected on base: the file did not import, or the test did not exist there.
            base_result = {"outcome": "error", "exc_type": broken.get(file)}
        rows.append({"id": t["id"], "base": base_result, "head": {"outcome": t["outcome"]}})
    failed = sum(1 for t in head.get("tests", []) if t["outcome"] in FAILED)
    passed = sum(1 for t in head.get("tests", []) if t["outcome"] == "passed")
    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "base_run": base_run,
        "changed_support": support or [],
        "removed_test_files": removed or [],
        "base_test_ids": base_test_ids or [],
        "base_collect": base_collect,
        "tests": rows,
        "full_suite": {
            "outcome": "failed" if failed else "passed",
            "passed": passed,
            "failed": failed,
        },
    }


def collect_base_ids(root: Path, base_sha: str, paths: list[str]) -> tuple[list[str], dict]:
    """Test ids that the base commit has in the given test modules (pytest --collect-only), and
    the collection facts: the exit code and the modules that gave no id. A module without ids
    (an import error on base, for example) is missing evidence, not "no test dropped"."""
    if not paths:
        return [], {"rc": None, "modules_without_ids": []}
    work = Path(tempfile.mkdtemp())
    tree = work / root.name
    try:
        git("clone", "--quiet", "--shared", "--no-checkout", str(root), str(tree), cwd=work)
        git("checkout", "--quiet", "--detach", base_sha, cwd=tree)
        paths, refused = checked_test_paths(tree, paths)
        if not paths:
            return [], {"rc": None, "modules_without_ids": [], "refused": refused}
        env = {k: v for k, v in os.environ.items() if k not in SCRIPT_VARS}
        env.update(PYTHONPATH="src")
        proc = subprocess.run(  # noqa: S603 runs the PR's tests by design, in the read-only test job; no shell
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "--continue-on-collection-errors",
                "--",
                *paths,
            ],
            cwd=tree,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        ids = sorted({line.strip() for line in proc.stdout.splitlines() if "::" in line})
        found = {i.split("::")[0] for i in ids}
        facts = {
            "rc": proc.returncode,
            "modules_without_ids": sorted(p for p in paths if p not in found),
        }
        if refused:
            facts["refused"] = refused
        return ids, facts
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_on_base(root: Path, base_sha: str, head_sha: str, files: list[str]) -> tuple[dict, dict]:
    """The base report and the base run facts: pytest exit code, tests collected, report found."""
    work = Path(tempfile.mkdtemp())
    tree = work / root.name
    try:
        git("clone", "--quiet", "--shared", "--no-checkout", str(root), str(tree), cwd=work)
        git("checkout", "--quiet", "--detach", base_sha, cwd=tree)
        files, refused = checked_test_paths(tree, files)
        if not files:
            # With no path after "--", pytest would run the whole base suite.
            return {"tests": []}, {
                "rc": None,
                "collected": None,
                "report": False,
                "refused": refused,
            }
        for f in files:
            target = tree / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                subprocess.run(  # noqa: S603 fixed git argument list, no shell; git from the runner PATH
                    ["git", "show", f"{head_sha}:{f}"],  # noqa: S607 fixed git argument list, no shell; git from the runner PATH
                    cwd=root,
                    check=True,
                    capture_output=True,
                ).stdout
            )
        out = work / "base-report.json"
        env = {k: v for k, v in os.environ.items() if k not in SCRIPT_VARS}
        env.update(PYTHONPATH="src", CI_HEAD_SHA=base_sha, CI_BASE_SHA=base_sha)
        proc = subprocess.run(  # noqa: S603 runs the PR's tests by design, in the read-only test job; no shell
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--continue-on-collection-errors",
                f"--ci-report={out}",
                "--",
                *files,
            ],
            cwd=tree,
            env=env,
            check=False,
        )
        report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
        run = {
            "rc": proc.returncode,
            "collected": len(report.get("tests", [])) if report else None,
            "report": report is not None,
        }
        if refused:
            run["refused"] = refused
        return report or {"tests": []}, run
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--head-report", required=True, type=Path)
    args = parser.parse_args(argv)
    head_sha = git("rev-parse", args.head, cwd=root).strip()
    base_sha = git("merge-base", args.base, head_sha, cwd=root).strip()
    changed = changed_paths(root, base_sha, head_sha)
    files = changed["tests"]
    head = json.loads(args.head_report.read_text(encoding="utf-8"))
    if files:
        base, run = run_on_base(root, base_sha, head_sha, files)
    else:
        base, run = {"tests": []}, None
    base_ids, base_collect = collect_base_ids(root, base_sha, changed["base_side"])
    # Protected regression set: the base copies of the changed test modules on the head code.
    head["pinned"] = build_pinned(
        root,
        base_sha,
        head_sha,
        checked_test_paths(root, changed["base_side"])[0],
        checked_test_paths(root, changed["tests"])[0],
    )
    head["repro"] = build_block(
        base_sha,
        files,
        base,
        head,
        head_sha=head_sha,
        base_run=run,
        support=changed["support"],
        removed=changed["removed_tests"],
        base_test_ids=base_ids,
        base_collect=base_collect,
    )
    args.head_report.write_text(
        json.dumps(head, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        f"repro: {len(head['repro']['tests'])} test(s) from {len(files)} file(s)",
        f"on base {base_sha[:7]}",
    )
    errors = []
    for r in (run or {}).get("refused", []) + base_collect.get("refused", []):
        errors.append(f"refused test path {r['path']!r}: {r['reason']}")
    if files and not run["report"]:
        errors.append(f"the base run wrote no report (pytest exit code {run['rc']})")
    if head.get("head_sha") and head["head_sha"] != head_sha:
        errors.append(f"the head report is for {head['head_sha'][:7]}, not {head_sha[:7]}")
    for e in errors:
        print(f"repro: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
