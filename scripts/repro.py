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
changed pytest support and config files (``changed_support``) and the removed test files. The
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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = {"failed", "error"}
# Files that change how pytest collects or runs tests, besides the test modules themselves.
SUPPORT_FILES = {"pyproject.toml", "uv.lock", "pytest.ini", "setup.cfg", "tox.ini", ".coveragerc"}
# Variables of the CI step that runs this script; the head test run does not have them.
SCRIPT_VARS = {"BASE_SHA", "HEAD_SHA"}


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def is_test_module(f: str) -> bool:
    return f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py")


def is_support(f: str) -> bool:
    return (
        Path(f).name == "conftest.py"
        or f in SUPPORT_FILES
        or (f.startswith("tests/") and not is_test_module(f))
    )


def test_files(changed: list[str]) -> list[str]:
    """Test modules the pull request adds or changes; conftest.py and data files are left out."""
    return sorted(f for f in changed if is_test_module(f))


def changed_paths(root: Path, base_sha: str, head_sha: str) -> dict[str, list[str]]:
    """Test modules to copy, test modules removed, and changed support files."""
    out = git("diff", "--name-status", "-M", f"{base_sha}...{head_sha}", cwd=root)
    tests, removed, support = set(), set(), set()
    for line in out.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("D"):
            if is_test_module(paths[0]):
                removed.add(paths[0])
        elif is_test_module(paths[-1]):
            tests.add(paths[-1])
        if status.startswith("R") and is_test_module(paths[0]) and not is_test_module(paths[-1]):
            removed.add(paths[0])
        support.update(p for p in paths if is_support(p))
    return {"tests": sorted(tests), "removed_tests": sorted(removed), "support": sorted(support)}


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
        "tests": rows,
        "full_suite": {
            "outcome": "failed" if failed else "passed",
            "passed": passed,
            "failed": failed,
        },
    }


def run_on_base(root: Path, base_sha: str, head_sha: str, files: list[str]) -> tuple[dict, dict]:
    """The base report and the base run facts: pytest exit code, tests collected, report found."""
    work = Path(tempfile.mkdtemp())
    tree = work / root.name
    try:
        git("clone", "--quiet", "--shared", "--no-checkout", str(root), str(tree), cwd=work)
        git("checkout", "--quiet", "--detach", base_sha, cwd=tree)
        for f in files:
            target = tree / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                subprocess.run(
                    ["git", "show", f"{head_sha}:{f}"], cwd=root, check=True, capture_output=True
                ).stdout
            )
        out = work / "base-report.json"
        env = {k: v for k, v in os.environ.items() if k not in SCRIPT_VARS}
        env.update(PYTHONPATH="src", CI_HEAD_SHA=base_sha, CI_BASE_SHA=base_sha)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--continue-on-collection-errors",
                f"--ci-report={out}",
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
    head["repro"] = build_block(
        base_sha,
        files,
        base,
        head,
        head_sha=head_sha,
        base_run=run,
        support=changed["support"],
        removed=changed["removed_tests"],
    )
    args.head_report.write_text(
        json.dumps(head, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        f"repro: {len(head['repro']['tests'])} test(s) from {len(files)} file(s)",
        f"on base {base_sha[:7]}",
    )
    errors = []
    if files and not run["report"]:
        errors.append(f"the base run wrote no report (pytest exit code {run['rc']})")
    if head.get("head_sha") and head["head_sha"] != head_sha:
        errors.append(f"the head report is for {head['head_sha'][:7]}, not {head_sha[:7]}")
    for e in errors:
        print(f"repro: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
