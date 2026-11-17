"""scripts/repro.py: the repro block from a base run and the head report (DESIGN 15b)."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

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


# ---- PR 14 review: regression tests on a real git repository ----


PYPROJECT = '[tool.pytest.ini_options]\naddopts = ["-p", "bank.testing.ci_report"]\n'


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root, files):
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _repo(tmp_path, base_files, head_files):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, base_files)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, head_files)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return root, base, _git(root, "rev-parse", "HEAD")


BASE = {
    "pyproject.toml": PYPROJECT,
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "def f():\n    return 1\n",
    "tests/test_old.py": "def test_old():\n    assert True\n",
    "tests/test_keep.py": "def test_keep():\n    assert True\n",
}


def test_deleted_and_renamed_test_files_do_not_crash_and_are_recorded(tmp_path):
    root, base, head = _repo(
        tmp_path,
        BASE,
        {
            "tests/test_old.py": None,
            "tests/test_keep.py": None,
            "tests/test_kept.py": "def test_keep():\n    assert True\n",
        },
    )
    changed = repro.changed_paths(root, base, head)
    assert changed["tests"] == ["tests/test_kept.py"]
    assert changed["removed_tests"] == ["tests/test_old.py"]
    report, run = repro.run_on_base(root, base, head, changed["tests"])
    assert run["report"] is True


def test_changed_pytest_support_and_config_are_listed(tmp_path):
    root, base, head = _repo(
        tmp_path,
        BASE,
        {
            "tests/conftest.py": "",
            "src/pkg/conftest.py": "",
            "tests/helpers.py": "X = 1\n",
            "tests/data/case.json": "{}\n",
            "pyproject.toml": PYPROJECT + "# changed\n",
            "uv.lock": "",
            "pytest.ini": "",
            ".coveragerc": "",
            "src/pkg/mod.py": "def f():\n    return 2\n",
        },
    )
    changed = repro.changed_paths(root, base, head)
    assert changed["support"] == [
        ".coveragerc",
        "pyproject.toml",
        "pytest.ini",
        "src/pkg/conftest.py",
        "tests/conftest.py",
        "tests/data/case.json",
        "tests/helpers.py",
        "uv.lock",
    ]
    assert changed["tests"] == []


def test_base_assertion_failure_and_collection_failure_are_told_apart(tmp_path):
    root, base, head = _repo(
        tmp_path,
        BASE,
        {
            "src/pkg/mod.py": "def f():\n    return 2\n",
            "src/pkg/newmod.py": "X = 1\n",
            "tests/test_fix.py": "from pkg.mod import f\n\n\ndef test_fix():\n"
            "    assert f() == 2\n",
            "tests/test_new.py": "from pkg.newmod import X\n\n\ndef test_new():\n"
            "    assert X == 1\n",
        },
    )
    files = repro.changed_paths(root, base, head)["tests"]
    report, run = repro.run_on_base(root, base, head, files)
    assert run == {"rc": 1, "collected": 1, "report": True}
    head_report = {
        "head_sha": head,
        "tests": [
            {"id": "tests/test_fix.py::test_fix", "outcome": "passed"},
            {"id": "tests/test_new.py::test_new", "outcome": "passed"},
        ],
    }
    block = repro.build_block(base, files, report, head_report, head_sha=head, base_run=run)
    rows = {r["id"]: r["base"] for r in block["tests"]}
    assert rows["tests/test_fix.py::test_fix"] == {
        "outcome": "failed",
        "exc_type": "AssertionError",
    }
    assert rows["tests/test_new.py::test_new"] == {
        "outcome": "error",
        "exc_type": "ModuleNotFoundError",
    }
    assert block["head_sha"] == head
    assert block["base_run"] == run


def test_the_base_run_looks_like_an_ordinary_checkout(tmp_path, monkeypatch):
    probe = (
        "import os\n\n\ndef test_probe():\n"
        "    assert 'repro' not in os.getcwd()\n"
        "    assert os.path.isdir('.git')\n"
        "    assert 'BASE_SHA' not in os.environ and 'HEAD_SHA' not in os.environ\n"
        "    assert os.environ.get('PYTHONPATH') == 'src'\n"
    )
    root, base, head = _repo(tmp_path, BASE, {"tests/test_probe.py": probe})
    monkeypatch.setenv("BASE_SHA", base)
    monkeypatch.setenv("HEAD_SHA", head)
    report, run = repro.run_on_base(root, base, head, ["tests/test_probe.py"])
    assert [t["outcome"] for t in report["tests"]] == ["passed"]


def test_a_missing_base_report_fails_the_step(tmp_path):
    # No report plugin in this repository: the base run writes no report.
    files = dict(BASE, **{"pyproject.toml": "[tool.pytest.ini_options]\n"})
    root, base, head = _repo(
        tmp_path, files, {"tests/test_fix.py": "def test_fix():\n    assert True\n"}
    )
    out = tmp_path / "ci-report.json"
    out.write_text(json.dumps({"head_sha": head, "tests": []}), encoding="utf-8")
    rc = repro.main(["--base", base, "--head", head, "--head-report", str(out)], root=root)
    assert rc == 1
    block = json.loads(out.read_text(encoding="utf-8"))["repro"]
    assert block["base_run"]["report"] is False
    assert block["tests"] == []


def test_a_head_report_for_another_commit_fails_the_step(tmp_path):
    root, base, head = _repo(
        tmp_path, BASE, {"tests/test_fix.py": "def test_fix():\n    assert True\n"}
    )
    out = tmp_path / "ci-report.json"
    out.write_text(json.dumps({"head_sha": "c" * 40, "tests": []}), encoding="utf-8")
    rc = repro.main(["--base", base, "--head", head, "--head-report", str(out)], root=root)
    assert rc == 1
    block = json.loads(out.read_text(encoding="utf-8"))["repro"]
    assert block["head_sha"] == head


@pytest.mark.parametrize("name", ["test_a.py", "helpers.py"])
def test_only_test_modules_are_copied(name):
    assert repro.test_files([f"tests/{name}"]) == (
        ["tests/test_a.py"] if name == "test_a.py" else []
    )


def test_base_test_ids_of_renamed_and_removed_test_files_are_recorded(tmp_path):
    # A PR that renames a test file and drops one of its tests: the base side keeps the old ids,
    # so the verifier can see the dropped test (benchmark case I10).
    keep = "def test_keep():\n    assert True\n\n\ndef test_guard():\n    assert True\n"
    root, base, head = _repo(
        tmp_path,
        dict(BASE, **{"tests/test_keep.py": keep}),
        {
            "tests/test_keep.py": None,
            "tests/test_kept.py": "def test_keep():\n    assert True\n",
            "tests/test_old.py": None,
        },
    )
    changed = repro.changed_paths(root, base, head)
    assert changed["base_side"] == ["tests/test_keep.py", "tests/test_old.py"]
    out = tmp_path / "ci-report.json"
    out.write_text(
        json.dumps(
            {
                "head_sha": head,
                "tests": [{"id": "tests/test_kept.py::test_keep", "outcome": "passed"}],
            }
        ),
        encoding="utf-8",
    )
    assert repro.main(["--base", base, "--head", head, "--head-report", str(out)], root=root) == 0
    block = json.loads(out.read_text(encoding="utf-8"))["repro"]
    assert block["base_test_ids"] == [
        "tests/test_keep.py::test_guard",
        "tests/test_keep.py::test_keep",
        "tests/test_old.py::test_old",
    ]


def test_a_base_module_that_does_not_collect_is_recorded(tmp_path):
    # Review finding 7: an import error on base gives no ids. The block must say so, so the
    # verifier reads it as missing evidence, never as "no test dropped".
    broken = "import missing_module_xyz\n\n\ndef test_keep():\n    assert True\n"
    root, base, head = _repo(
        tmp_path,
        dict(BASE, **{"tests/test_keep.py": broken}),
        {"tests/test_keep.py": "def test_keep():\n    assert True\n"},
    )
    ids, collect = repro.collect_base_ids(root, base, ["tests/test_keep.py", "tests/test_old.py"])
    assert ids == ["tests/test_old.py::test_old"]
    assert collect["rc"] != 0
    assert collect["modules_without_ids"] == ["tests/test_keep.py"]
    out = tmp_path / "ci-report.json"
    out.write_text(
        json.dumps(
            {
                "head_sha": head,
                "tests": [{"id": "tests/test_keep.py::test_keep", "outcome": "passed"}],
            }
        ),
        encoding="utf-8",
    )
    repro.main(["--base", base, "--head", head, "--head-report", str(out)], root=root)
    block = json.loads(out.read_text(encoding="utf-8"))["repro"]
    assert block["base_collect"]["modules_without_ids"] == ["tests/test_keep.py"]
    assert block["base_collect"]["rc"] != 0


def test_a_deleted_test_file_with_a_non_ascii_name_is_recorded(tmp_path):
    # Review finding 8: git quotes such a path unless -z is used.
    root, base, head = _repo(
        tmp_path,
        dict(BASE, **{"tests/test_café.py": "def test_c():\n    assert True\n"}),
        {"tests/test_café.py": None},
    )
    changed = repro.changed_paths(root, base, head)
    assert changed["removed_tests"] == ["tests/test_café.py"]
    assert "tests/test_café.py" in changed["base_side"]


@pytest.mark.parametrize(
    "name", [".github/workflows/ci.yml", "scripts/repro.py", "scripts/changed_coverage.py"]
)
def test_ci_files_count_as_changed_support(tmp_path, name):
    # Review finding 9: a PR can add "if: false" to a required job or change a CI script.
    root, base, head = _repo(tmp_path, dict(BASE, **{name: "x = 1\n"}), {name: "x = 2\n"})
    assert name in repro.changed_paths(root, base, head)["support"]


def test_the_base_row_carries_the_exception_message():
    base = {
        "tests": [
            {
                "id": T1,
                "outcome": "failed",
                "exc_type": "AttributeError",
                "exc_message": "AttributeError: module 'bank.ledger' has no attribute 'totals'",
            }
        ]
    }
    head = {"tests": [{"id": T1, "outcome": "passed"}]}
    block = repro.build_block("b" * 40, ["tests/test_transfers.py"], base, head)
    assert block["tests"][0]["base"]["exc_message"] == (
        "AttributeError: module 'bank.ledger' has no attribute 'totals'"
    )
