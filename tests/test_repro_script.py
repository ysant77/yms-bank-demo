"""scripts/repro.py: the repro block from a base run and the head report (DESIGN 15b)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repro", ROOT / "scripts" / "repro.py")
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)

T1 = "tests/test_transfers.py::test_BANK4_concurrent"
T2 = "tests/test_new.py::test_BANK4_new_module"


def test_block_pairs_base_and_head_outcomes():
    base = {
        "tests": [{"id": T1, "outcome": "failed", "exc_type": "AssertionError"}],
        "collect_errors": [{"id": "tests/test_new.py", "exc_type": "ModuleNotFoundError"}],
    }
    head = {
        "tests": [
            {"id": T1, "outcome": "passed"},
            {"id": T2, "outcome": "passed"},
            {"id": "tests/test_other.py::test_x", "outcome": "passed"},
        ]
    }
    block = repro.build_block(
        "b" * 40, ["tests/test_transfers.py", "tests/test_new.py"], base, head
    )
    assert block["base_sha"] == "b" * 40
    assert block["tests"] == [
        {
            "id": T1,
            "base": {"outcome": "failed", "exc_type": "AssertionError"},
            "head": {"outcome": "passed"},
        },
        {
            "id": T2,
            "base": {"outcome": "error", "exc_type": "ModuleNotFoundError"},
            "head": {"outcome": "passed"},
        },
    ]
    assert block["full_suite"] == {"outcome": "passed", "passed": 3, "failed": 0}


def test_a_failing_head_suite_is_reported():
    head = {"tests": [{"id": T1, "outcome": "passed"}, {"id": "tests/a.py::t", "outcome": "error"}]}
    block = repro.build_block("b" * 40, [], {"tests": []}, head)
    assert block["tests"] == []
    assert block["full_suite"] == {"outcome": "failed", "passed": 1, "failed": 1}


def test_only_test_files_under_tests_are_copied():
    changed = ["src/bank/ledger.py", "tests/test_a.py", "tests/conftest.py", "tests/data/x.json"]
    assert repro.test_files(changed) == ["tests/test_a.py"]
