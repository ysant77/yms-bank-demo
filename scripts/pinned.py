"""Protected regression set: pin the base tests of the changed test modules and run their base
copies against the head code (called by scripts/repro.py; agile-team docs/PROTECTED-TESTS.md).

1. For each test module that the pull request changes, renames or removes, read the base
   version and hash the body of each test function (the AST with the function name blanked, so
   a rename keeps the hash and spacing or comments do not change it; decorators count).
2. Clone the repository at the head commit, write the base version of those modules over the
   head tree, and run them with pytest and the report plugin. The head's code is used. The
   head's ``addopts`` are ignored (``-o addopts=``), so a head plugin or deselect does not apply.
3. For each base test, record its head name and hash (the same name, or another name with the
   same body in a changed head module; none when it is gone) and the outcome of its base copy.

The script decides nothing: the verifier's check pinned_tests_hold reads the block.
Limit: test modules that the pull request does not change are not in the block; the head run
already runs them as they are, and the verifier sees a test missing from the head report.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PIN_OUTCOMES = {"passed", "failed", "error"}
SCRIPT_VARS = {"BASE_SHA", "HEAD_SHA"}


def body_hashes(source: str) -> dict[str, str] | None:
    """Test name (``test_x`` or ``TestClass::test_x``) to the hash of its body; None when the
    module does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    out: dict[str, str] = {}

    def add(node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            blank = copy.deepcopy(node)
            blank.name = "_"
            out[f"{prefix}{node.name}"] = hashlib.sha256(
                ast.dump(blank, include_attributes=False).encode()
            ).hexdigest()

    for node in tree.body:
        add(node, "")
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                add(item, f"{node.name}::")
    return out


def outcome_of(outcomes: dict[str, str], test_id: str) -> str:
    """The outcome of a base copy on head. A parametrized test (report ids ``test_id[...]``)
    takes the worst outcome of its parameters. Skipped, deselected or absent is not_run."""
    found = [o for i, o in outcomes.items() if i == test_id or i.startswith(f"{test_id}[")]
    if not found:
        return "not_run"
    for worst in ("error", "failed"):
        if worst in found:
            return worst
    return "passed" if all(o == "passed" for o in found) else "not_run"


def entries(
    base: dict[str, dict[str, str]], head: dict[str, dict[str, str]], outcomes: dict[str, str]
) -> list[dict]:
    """One row per base test of the changed modules. ``base`` and ``head`` map a module path to
    its {test name: body hash}; a module missing from ``head`` is gone on head."""
    head_ids = {f"{m}::{n}": h for m, tests in head.items() for n, h in tests.items()}
    base_ids = {f"{m}::{n}" for m, tests in base.items() for n in tests}
    rows = []
    for module, tests in sorted(base.items()):
        for name, h in sorted(tests.items()):
            test_id = f"{module}::{name}"
            head_id = test_id if test_id in head_ids else None
            if head_id is None:
                # A rename: a new head test (not a base test) with the same body.
                same = sorted(i for i, hh in head_ids.items() if hh == h and i not in base_ids)
                head_id = same[0] if same else None
            rows.append(
                {
                    "id": test_id,
                    "base_hash": h,
                    "head_id": head_id,
                    "head_hash": head_ids[head_id] if head_id else None,
                    "outcome": outcome_of(outcomes, test_id),
                }
            )
    return rows


def _show(root: Path, sha: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=root, check=False, capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def run_base_copies(root: Path, base_sha: str, head_sha: str, modules: list[str]) -> dict:
    """Outcome of each base test id when the base modules run on the head tree."""
    if not modules:
        return {}
    work = Path(tempfile.mkdtemp())
    tree = work / root.name
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(root), str(tree)],
            cwd=work,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", head_sha],
            cwd=tree,
            check=True,
            capture_output=True,
        )
        top = tree.resolve()
        safe = []
        for m in modules:
            target = tree / m
            # The head tree comes from the pull request: a symlink at this path, or a parent that
            # leads outside the clone, is not followed.
            if target.is_symlink():
                target.unlink()
            if not target.parent.resolve().is_relative_to(top):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_show(root, base_sha, m) or "", encoding="utf-8")
            safe.append(m)
        modules = safe
        if not modules:
            return {}
        out = work / "pinned-report.json"
        env = {k: v for k, v in os.environ.items() if k not in SCRIPT_VARS}
        env.update(PYTHONPATH="src", CI_HEAD_SHA=head_sha, CI_BASE_SHA=base_sha)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--continue-on-collection-errors",
                # The head's addopts are ignored: a head -p plugin or --deselect must not apply
                # to the base copies. The report plugin and the marker selection of the CI
                # report step are set here.
                "-o",
                "addopts=",
                "-p",
                "bank.testing.ci_report",
                "-m",
                "bench or not bench",
                f"--ci-report={out}",
                "--",
                *modules,
            ],
            cwd=tree,
            env=env,
            check=False,
            capture_output=True,
        )
        report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
        return {t["id"]: t["outcome"] for t in report.get("tests", [])}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build(
    root: Path, base_sha: str, head_sha: str, base_side: list[str], head_side: list[str]
) -> dict:
    """The pinned block. ``base_side``: base paths of the changed, renamed or removed test
    modules (already path-checked); ``head_side``: head paths of the changed test modules."""
    base: dict[str, dict[str, str]] = {}
    for m in base_side:
        hashes = body_hashes(_show(root, base_sha, m) or "")
        if hashes:
            base[m] = hashes
    head: dict[str, dict[str, str]] = {}
    for m in head_side:
        hashes = body_hashes(_show(root, head_sha, m) or "")
        if hashes:
            head[m] = hashes
    outcomes = run_base_copies(root, base_sha, head_sha, sorted(base))
    return {
        "version": 1,
        "base_sha": base_sha,
        "modules": sorted(base),
        "tests": entries(base, head, outcomes),
    }
