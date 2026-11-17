"""Write the coverage block of the CI report (DESIGN 15c).

    uv run python scripts/changed_coverage.py --base <sha> --coverage coverage.json \\
        --head-report ci-report.json [--base-report main.json]

The block holds the changed executable lines of ``src/``, how many of them the head run covered,
the changed lines that the coverage settings exclude (``# pragma: no cover`` and the like), the
head total and the base total. Without ``--base`` (a push to main) only the totals are written.
The script decides nothing: the verifier applies the floor and the allowed drop.

- The changed lines come from the diff between ``--base`` and the commit that is checked out, the
  same commit that the coverage run measured (for a pull request, GitHub's merge commit; its
  first parent is the base). ``--head`` must name that commit; any other commit is refused,
  because its line numbers can differ.
- The base total comes from ``main.json`` on the ``ci-reports`` branch: the report of the latest
  push to main, not of the pull request's base commit. A later step fetches it; this script gets
  no token.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args: str, cwd: Path = ROOT) -> str:
    # Paths are passed literally, never as patterns.
    return subprocess.run(
        ["git", "--literal-pathspecs", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def hunk_lines(diff: str) -> set[int]:
    """New-side line numbers of a ``-U0`` diff of one file. Only ``@@`` lines are read: a diff
    line inside a hunk starts with ``+``, ``-``, a space or a backslash, so an added line such
    as ``++ x`` (shown as ``+++ x``) cannot pass for a header."""
    result: set[int] = set()
    for line in diff.splitlines():
        m = HUNK.match(line)
        if m:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            result.update(range(start, start + count))
    return result


def changed_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    """New-side changed lines of each Python file under ``src/`` that exists on ``head``. The
    names come NUL-separated (``-z``), so a space or a non-ASCII letter is kept as it is."""
    names = git(
        "diff",
        "-z",
        "--name-only",
        "--no-renames",
        "--diff-filter=d",
        base,
        head,
        "--",
        "src",
        cwd=root,
    ).split("\0")
    result: dict[str, set[int]] = {}
    for name in names:
        if not (name.startswith("src/") and name.endswith(".py")):
            continue
        lines = hunk_lines(git("diff", "-U0", "--no-renames", base, head, "--", name, cwd=root))
        if lines:
            result[name] = lines
    return result


def build_block(changed: dict[str, set[int]], coverage: dict, base_report: dict | None) -> dict:
    executable = covered = excluded = 0
    for path, lines in changed.items():
        data = coverage.get("files", {}).get(path, {})
        executed = set(data.get("executed_lines", []))
        missing = set(data.get("missing_lines", []))
        executable += len(lines & (executed | missing))
        covered += len(lines & executed)
        # Lines the coverage settings exclude are not executable for coverage; count them, so a
        # "# pragma: no cover" on changed code is visible.
        excluded += len(lines & set(data.get("excluded_lines", [])))
    base_total = ((base_report or {}).get("coverage") or {}).get("head_total")
    return {
        "changed_executable": executable,
        "changed_covered": covered,
        "changed_excluded": excluded,
        "head_total": round(float(coverage["totals"]["percent_covered"]), 2),
        "base_total": base_total if isinstance(base_total, int | float) else None,
    }


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD", help="the checked-out commit (default HEAD)")
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--head-report", required=True, type=Path)
    parser.add_argument("--base-report", type=Path)
    args = parser.parse_args(argv)
    head = git("rev-parse", args.head, cwd=root).strip()
    checked_out = git("rev-parse", "HEAD", cwd=root).strip()
    if head != checked_out:
        print(
            f"coverage: --head {head[:7]} is not the checked-out commit {checked_out[:7]}, "
            "which the coverage run measured",
            file=sys.stderr,
        )
        return 1
    changed: dict[str, set[int]] = {}
    if args.base:
        changed = changed_lines(root, args.base, head)
    coverage = json.loads(args.coverage.read_text(encoding="utf-8"))
    base = None
    if args.base_report and args.base_report.is_file():
        try:
            base = json.loads(args.base_report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            base = None
    report = json.loads(args.head_report.read_text(encoding="utf-8"))
    report["coverage"] = build_block(changed, coverage, base)
    args.head_report.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("coverage:", json.dumps(report["coverage"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
