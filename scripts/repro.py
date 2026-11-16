"""Run the pull request's test files on the base commit and write the repro block (DESIGN 15b).

    uv run python scripts/repro.py --base <sha> --head-report ci-report.json

1. Make a worktree at the base commit (the merge base with main).
2. Copy onto it only the test files that the pull request adds or changes under ``tests/``, from
   the head. ``conftest.py`` and every source file stay as on base.
3. Run those files on base with the report plugin, with the base ``src`` first on the path.
4. Pair each head test of those files with its base outcome and exception type, and write the
   block into the head report as ``repro``, with the full-suite result of the head run.

The script never decides anything: the verifier reads the block and applies the rules.
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


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def test_files(changed: list[str]) -> list[str]:
    """Test modules the pull request adds or changes; conftest.py and data files are left out."""
    return sorted(
        f
        for f in changed
        if f.startswith("tests/") and Path(f).name.startswith("test_") and f.endswith(".py")
    )


def build_block(base_sha: str, files: list[str], base: dict, head: dict) -> dict:
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
        "tests": rows,
        "full_suite": {
            "outcome": "failed" if failed else "passed",
            "passed": passed,
            "failed": failed,
        },
    }


def run_on_base(base_sha: str, head_sha: str, files: list[str]) -> dict:
    work = Path(tempfile.mkdtemp(prefix="repro-"))
    tree = work / "base"
    try:
        git("worktree", "add", "--detach", str(tree), base_sha)
        for f in files:
            target = tree / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                subprocess.run(
                    ["git", "show", f"{head_sha}:{f}"], cwd=ROOT, check=True, capture_output=True
                ).stdout
            )
        out = work / "base-report.json"
        env = dict(os.environ, PYTHONPATH=str(tree / "src"), CI_HEAD_SHA=base_sha)
        subprocess.run(
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
        return json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {"tests": []}
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, check=False)
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--head-report", required=True, type=Path)
    args = parser.parse_args()
    head_sha = git("rev-parse", args.head).strip()
    base_sha = git("merge-base", args.base, head_sha).strip()
    files = test_files(git("diff", "--name-only", f"{base_sha}...{head_sha}").split())
    head = json.loads(args.head_report.read_text(encoding="utf-8"))
    base = run_on_base(base_sha, head_sha, files) if files else {"tests": []}
    head["repro"] = build_block(base_sha, files, base, head)
    args.head_report.write_text(
        json.dumps(head, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        f"repro: {len(head['repro']['tests'])} test(s) from {len(files)} file(s)",
        f"on base {base_sha[:7]}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
