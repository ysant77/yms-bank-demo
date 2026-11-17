"""scripts/changed_coverage.py: coverage of the changed lines (DESIGN 15c)."""

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "changed_coverage", ROOT / "scripts" / "changed_coverage.py"
)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

DIFF = """diff --git a/src/bank/ledger.py b/src/bank/ledger.py
--- a/src/bank/ledger.py
+++ b/src/bank/ledger.py
@@ -10,0 +11,3 @@ def post():
+    a = 1
+    b = 2
+    # a comment
@@ -40 +43 @@ def other():
-    x = 0
+    x = 1
diff --git a/tests/test_ledger.py b/tests/test_ledger.py
--- a/tests/test_ledger.py
+++ b/tests/test_ledger.py
@@ -1,0 +2 @@
+def test_x(): pass
"""


def test_hunk_lines_are_read_from_a_one_file_diff():
    one_file = DIFF.split("diff --git a/tests")[0]
    assert cc.hunk_lines(one_file) == {11, 12, 13, 43}


def test_only_executable_changed_lines_count():
    coverage = {
        "files": {"src/bank/ledger.py": {"executed_lines": [11, 43], "missing_lines": [12]}},
        "totals": {"percent_covered": 91.2345},
    }
    block = cc.build_block(
        {"src/bank/ledger.py": {11, 12, 13, 43}}, coverage, {"coverage": {"head_total": 90.8}}
    )
    # Line 13 is a comment: not executable, so it is not counted.
    assert block == {
        "changed_executable": 3,
        "changed_covered": 2,
        "changed_excluded": 0,
        "head_total": 91.23,
        "base_total": 90.8,
        "unmeasured_files": [],
    }


def test_a_missing_base_total_is_null_not_zero():
    coverage = {"files": {}, "totals": {"percent_covered": 50}}
    assert cc.build_block({}, coverage, None)["base_total"] is None
    assert cc.build_block({}, coverage, {"tests": []})["base_total"] is None


def test_a_changed_file_missing_from_the_coverage_data_is_named():
    # A fault of the coverage step can leave out a file. Its changed lines then count as neither
    # executable nor covered, so the counts alone look complete.
    coverage = {
        "files": {"src/bank/api.py": {"executed_lines": [5], "missing_lines": []}},
        "totals": {"percent_covered": 90},
    }
    block = cc.build_block({"src/bank/api.py": {5}, "src/bank/ledger.py": {7, 8}}, coverage, None)
    assert block["changed_executable"] == 1
    assert block["unmeasured_files"] == ["src/bank/ledger.py"]


def test_an_added_line_that_starts_with_two_plus_signs_does_not_hide_later_hunks():
    # Review finding 2: the source line "++ x" shows as "+++ x" in the diff.
    diff = """diff --git a/src/bank/a.py b/src/bank/a.py
--- a/src/bank/a.py
+++ b/src/bank/a.py
@@ -1,0 +2 @@
+++ x
@@ -9,0 +11,2 @@
+    y = 1
+    z = 2
"""
    assert cc.hunk_lines(diff) == {2, 11, 12}


def test_changed_lines_excluded_from_coverage_are_reported():
    # Review finding 3: "# pragma: no cover" makes lines non-executable for coverage.
    coverage = {
        "files": {
            "src/bank/a.py": {"executed_lines": [1], "missing_lines": [], "excluded_lines": [2, 3]}
        },
        "totals": {"percent_covered": 100.0},
    }
    block = cc.build_block({"src/bank/a.py": {1, 2, 3, 4}}, coverage, None)
    assert block["changed_excluded"] == 2
    assert block["changed_executable"] == 1


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root, files, message):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path):
    """main moves after the PR branched: the merge commit numbers the PR's lines differently."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    body = "".join(f"x{i} = {i}\n" for i in range(10))
    _commit(
        root,
        {"src/pkg/m.py": body, "src/pkg/a b.py": "a = 1\n", "src/pkg/café.py": "c = 1\n"},
        "base",
    )
    _git(root, "checkout", "-q", "-b", "pr")
    pr_head = _commit(
        root,
        {
            "src/pkg/m.py": body + "added = 1\n",
            "src/pkg/a b.py": "a = 2\n",
            "src/pkg/café.py": "c = 2\n",
        },
        "pr",
    )
    _git(root, "checkout", "-q", "main")
    base = _commit(root, {"src/pkg/m.py": "top = 0\n" + body}, "main moves")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    return root, base, pr_head


def _files(tmp_path):
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps({"files": {}, "totals": {"percent_covered": 50.0}}), encoding="utf-8"
    )
    report = tmp_path / "ci-report.json"
    report.write_text(json.dumps({"tests": []}), encoding="utf-8")
    return coverage, report


def test_main_reads_changed_lines_of_the_checked_out_commit(tmp_path):
    # Review findings 1, 4 and 5: main() over a real repository. The added line is line 12 in
    # the merge commit (main added a line on top), not line 11 as in the PR head. Names with a
    # space or a non-ASCII letter are kept.
    root, base, _ = _repo(tmp_path)
    coverage, report = _files(tmp_path)
    argv = ["--base", base, "--coverage", str(coverage), "--head-report", str(report)]
    assert cc.main(argv, root=root) == 0
    assert cc.changed_lines(root, base, "HEAD") == {
        "src/pkg/m.py": {12},
        "src/pkg/a b.py": {1},
        "src/pkg/café.py": {1},
    }
    block = json.loads(report.read_text(encoding="utf-8"))["coverage"]
    assert block["changed_executable"] == 0


def test_main_refuses_a_head_that_is_not_the_checked_out_commit(tmp_path):
    # Review finding 1: coverage ran on the checked-out commit, so the diff must end there.
    root, base, pr_head = _repo(tmp_path)
    coverage, report = _files(tmp_path)
    argv = [
        "--base",
        base,
        "--head",
        pr_head,
        "--coverage",
        str(coverage),
        "--head-report",
        str(report),
    ]
    assert cc.main(argv, root=root) == 1
