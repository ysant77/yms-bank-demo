"""scripts/pinned.py: the protected regression set. Every base test of a test module that the pull
request changes, renames or removes is pinned by the hash of its body, and its base copy runs
against the head code with the head's pytest configuration. The block goes into the CI report as
``pinned``; the verifier reads it (agile-team docs/PROTECTED-TESTS.md)."""

import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pinned", ROOT / "scripts" / "pinned.py")
pinned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinned)
rspec = importlib.util.spec_from_file_location("repro", ROOT / "scripts" / "repro.py")
repro = importlib.util.module_from_spec(rspec)
rspec.loader.exec_module(repro)

OLD = "def test_one():\n    # the value of f\n    assert f() == 1\n"


def test_a_rename_keeps_the_body_hash():
    a = pinned.body_hashes(OLD)
    b = pinned.body_hashes(OLD.replace("test_one", "test_first"))
    assert a["test_one"] == b["test_first"]


def test_spacing_and_comments_do_not_change_the_hash():
    other = "def test_one():\n\n    assert f()  ==  1   # same\n"
    assert pinned.body_hashes(OLD)["test_one"] == pinned.body_hashes(other)["test_one"]


def test_an_edited_value_or_an_added_skip_changes_the_hash():
    h = pinned.body_hashes(OLD)["test_one"]
    assert pinned.body_hashes(OLD.replace("== 1", "== 2"))["test_one"] != h
    skipped = "import pytest\n\n\n@pytest.mark.skip\n" + OLD
    assert pinned.body_hashes(skipped)["test_one"] != h


def test_only_test_functions_are_hashed_and_a_bad_module_gives_none():
    src = (
        "def helper():\n    pass\n\n\nclass TestX:\n    def test_m(self):\n        assert 1\n\n\n"
        + OLD
    )
    assert sorted(pinned.body_hashes(src)) == ["TestX::test_m", "test_one"]
    assert pinned.body_hashes("def (:\n") is None


def test_entries_name_unchanged_edited_renamed_and_deleted_tests():
    base = {
        "tests/test_a.py": {
            "test_keep": "h1",
            "test_edit": "h2",
            "test_old_name": "h3",
            "test_gone": "h4",
        }
    }
    head = {"tests/test_a.py": {"test_keep": "h1", "test_edit": "h9", "test_new_name": "h3"}}
    outcomes = {
        "tests/test_a.py::test_keep": "passed",
        "tests/test_a.py::test_edit": "failed",
        "tests/test_a.py::test_old_name": "passed",
        "tests/test_a.py::test_gone": "passed",
    }
    rows = {r["id"].split("::")[1]: r for r in pinned.entries(base, head, outcomes)}
    assert rows["test_keep"]["head_id"] == "tests/test_a.py::test_keep"
    assert rows["test_edit"]["head_hash"] == "h9"
    assert rows["test_edit"]["outcome"] == "failed"
    assert rows["test_old_name"]["head_id"] == "tests/test_a.py::test_new_name"
    assert rows["test_old_name"]["head_hash"] == "h3"
    assert rows["test_gone"]["head_id"] is None
    assert rows["test_gone"]["head_hash"] is None


def test_a_test_that_did_not_run_or_was_skipped_is_not_run():
    assert pinned.outcome_of({"x": "skipped"}, "x") == "not_run"
    assert pinned.outcome_of({}, "x") == "not_run"
    assert pinned.outcome_of({"x": "error"}, "x") == "error"


# ---- on a real git repository (the same helpers as tests/test_repro_script.py) ----

PYPROJECT = '[tool.pytest.ini_options]\naddopts = ["-p", "bank.testing.ci_report"]\n'
BASE = {
    "pyproject.toml": PYPROJECT,
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "def status():\n    return 409\n",
    "tests/test_api.py": (
        "from pkg.mod import status\n\n\n"
        "def test_status_is_409():\n    assert status() == 409\n\n\n"
        "def test_other():\n    assert True\n\n\n"
        "def test_dates():\n    assert 1 + 1 == 2\n"
    ),
    "tests/test_keep.py": "def test_keep():\n    assert True\n",
}


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path, head_files):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for files in (BASE, head_files):
        for name, text in files.items():
            path = root / name
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "c")
    return root, _git(root, "rev-parse", "HEAD~1"), _git(root, "rev-parse", "HEAD")


def _run(tmp_path, root, base, head):
    out = tmp_path / "ci-report.json"
    out.write_text(json.dumps({"head_sha": head, "tests": []}), encoding="utf-8")
    assert repro.main(["--base", base, "--head", head, "--head-report", str(out)], root=root) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    return {r["id"].split("::")[1]: r for r in report["pinned"]["tests"]}, report["pinned"]


def test_an_edited_expected_value_fails_as_a_base_copy_on_head(tmp_path):
    # Benchmark U09: the code now gives 400 and the old test is edited to expect 400.
    head_api = BASE["tests/test_api.py"].replace("== 409", "== 400")
    root, base, head = _repo(
        tmp_path,
        {"src/pkg/mod.py": "def status():\n    return 400\n", "tests/test_api.py": head_api},
    )
    rows, block = _run(tmp_path, root, base, head)
    assert block["version"] == 1
    assert block["base_sha"] == base
    assert block["modules"] == ["tests/test_api.py"]
    assert rows["test_status_is_409"]["outcome"] == "failed"
    assert rows["test_status_is_409"]["head_hash"] != rows["test_status_is_409"]["base_hash"]
    assert rows["test_other"]["outcome"] == "passed"
    # An unchanged test module is not in the block: the head run already runs it as it is.
    assert not any(r.startswith("test_keep") for r in rows)


def test_a_rename_with_the_same_body_is_found(tmp_path):
    # Benchmark U10: two tests renamed, bodies unchanged.
    head_api = BASE["tests/test_api.py"].replace("test_dates", "test_window_dates")
    root, base, head = _repo(tmp_path, {"tests/test_api.py": head_api})
    rows, _ = _run(tmp_path, root, base, head)
    assert rows["test_dates"]["head_id"] == "tests/test_api.py::test_window_dates"
    assert rows["test_dates"]["head_hash"] == rows["test_dates"]["base_hash"]
    assert rows["test_dates"]["outcome"] == "passed"


def test_a_deleted_test_has_no_head_side(tmp_path):
    head_api = BASE["tests/test_api.py"].split("\n\n\ndef test_dates")[0] + "\n"
    root, base, head = _repo(tmp_path, {"tests/test_api.py": head_api})
    rows, _ = _run(tmp_path, root, base, head)
    assert rows["test_dates"]["head_id"] is None
    assert rows["test_dates"]["head_hash"] is None


def test_a_test_deselected_by_the_head_configuration_is_not_run(tmp_path):
    # Benchmark U17 shape: the head adds a --deselect for an old test and changes its module.
    deselect = PYPROJECT.replace(
        '"bank.testing.ci_report"]',
        '"bank.testing.ci_report", "--deselect", "tests/test_api.py::test_other"]',
    )
    head_api = BASE["tests/test_api.py"] + "\n\ndef test_new():\n    assert True\n"
    root, base, head = _repo(tmp_path, {"pyproject.toml": deselect, "tests/test_api.py": head_api})
    rows, _ = _run(tmp_path, root, base, head)
    assert rows["test_other"]["outcome"] == "not_run"
    assert rows["test_status_is_409"]["outcome"] == "passed"


def test_no_changed_test_module_gives_an_empty_block(tmp_path):
    root, base, head = _repo(tmp_path, {"src/pkg/mod.py": "def status():\n    return 409  # x\n"})
    rows, block = _run(tmp_path, root, base, head)
    assert rows == {}
    assert block["tests"] == []
    assert block["modules"] == []


def test_a_head_symlink_at_a_pinned_path_is_not_followed(tmp_path):
    # The head tree comes from the pull request. A symlink at the module path must not let the
    # base copy write outside the clone.
    outside = tmp_path / "outside.py"
    outside.write_text("KEEP = 1\n", encoding="utf-8")
    root, base, head = _repo(tmp_path, {"tests/test_api.py": None})
    (root / "tests" / "test_api.py").symlink_to(outside)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "link")
    head = _git(root, "rev-parse", "HEAD")
    pinned.run_base_copies(root, base, head, ["tests/test_api.py"])
    assert outside.read_text(encoding="utf-8") == "KEEP = 1\n"


def test_a_parametrized_test_takes_the_worst_outcome_of_its_parameters():
    # Found on the set 4 case branches: the report ids of a parametrized test end in "[...]".
    outcomes = {
        "t.py::test_p[a]": "passed",
        "t.py::test_p[b]": "failed",
        "t.py::test_q[x]": "passed",
    }
    assert pinned.outcome_of(outcomes, "t.py::test_p") == "failed"
    assert pinned.outcome_of(outcomes, "t.py::test_q") == "passed"
    assert pinned.outcome_of({"t.py::test_p[a]": "skipped"}, "t.py::test_p") == "not_run"
    assert (
        pinned.outcome_of(
            {"t.py::test_p[a]": "passed", "t.py::test_p[b]": "skipped"}, "t.py::test_p"
        )
        == "not_run"
    )
