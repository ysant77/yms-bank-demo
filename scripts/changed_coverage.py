"""Write the coverage block of the CI report (DESIGN 15c).

    uv run python scripts/changed_coverage.py --base <sha> --coverage coverage.json \\
        --head-report ci-report.json [--base-report main.json]

The block holds the changed executable lines of ``src/`` (from the diff against the merge base
with main), how many of them the head run covered, the head total and the base total (from the
main report, ``ci-reports/main.json``). Without ``--base`` (a push to main) only the totals are
written. The script decides nothing: the verifier applies the floor and the allowed drop.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def changed_lines(diff: str) -> dict[str, set[int]]:
    """New-side line numbers of each changed Python file under src/, from a ``-U0`` diff."""
    result: dict[str, set[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            current = path if path.startswith("src/") and path.endswith(".py") else None
            continue
        m = HUNK.match(line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            if count:
                result.setdefault(current, set()).update(range(start, start + count))
    return result


def build_block(changed: dict[str, set[int]], coverage: dict, base_report: dict | None) -> dict:
    executable = covered = 0
    for path, lines in changed.items():
        data = coverage.get("files", {}).get(path, {})
        executed = set(data.get("executed_lines", []))
        missing = set(data.get("missing_lines", []))
        executable += len(lines & (executed | missing))
        covered += len(lines & executed)
    base_total = ((base_report or {}).get("coverage") or {}).get("head_total")
    return {
        "changed_executable": executable,
        "changed_covered": covered,
        "head_total": round(float(coverage["totals"]["percent_covered"]), 2),
        "base_total": base_total if isinstance(base_total, int | float) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--head-report", required=True, type=Path)
    parser.add_argument("--base-report", type=Path)
    args = parser.parse_args()
    changed: dict[str, set[int]] = {}
    if args.base:
        merge_base = git("merge-base", args.base, args.head).strip()
        changed = changed_lines(git("diff", "-U0", f"{merge_base}...{args.head}", "--", "src"))
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
