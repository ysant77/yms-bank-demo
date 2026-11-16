"""scripts/changed_coverage.py: coverage of the changed lines (DESIGN 15c)."""

import importlib.util
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


def test_changed_lines_are_read_from_the_diff_for_source_files_only():
    assert cc.changed_lines(DIFF) == {"src/bank/ledger.py": {11, 12, 13, 43}}


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
        "head_total": 91.23,
        "base_total": 90.8,
    }


def test_a_missing_base_total_is_null_not_zero():
    coverage = {"files": {}, "totals": {"percent_covered": 50}}
    assert cc.build_block({}, coverage, None)["base_total"] is None
    assert cc.build_block({}, coverage, {"tests": []})["base_total"] is None
