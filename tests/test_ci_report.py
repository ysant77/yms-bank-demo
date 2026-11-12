import hashlib
import json
import subprocess

import pytest

from bank.testing.ci_report import Collector, sql_operations

pytest_plugins = ["pytester"]

LOCK = """version = 1
requires-python = ">=3.12"

[[package]]
name = "demo"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "fastapi"
version = "0.115.0"
source = { registry = "https://pypi.org/simple" }
"""

MIGRATION = """-- migrate:up
CREATE TABLE t (id INTEGER PRIMARY KEY, x INTEGER REFERENCES u (id) ON UPDATE CASCADE);
ALTER TABLE t ADD COLUMN y TEXT;
UPDATE t SET y = 'a';
CREATE UNIQUE INDEX t_y ON t (y);
-- migrate:down
DROP TABLE t;
"""

TESTS = """
import pytest

def test_ok(record_metric):
    record_metric("transfers_per_s", 412.0)
    record_metric("rows", 3)

def test_bad():
    assert False

@pytest.mark.skip(reason="demo")
def test_skipped():
    pass

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

def test_setup_error(broken):
    pass

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown fails")

def test_teardown_error(broken_teardown):
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    assert n == 1

@pytest.mark.xfail(reason="known")
def test_known_bug():
    assert False
"""


@pytest.fixture
def project(pytester):
    pytester.makeini("[pytest]\n")
    pytester.path.joinpath("uv.lock").write_text(LOCK)
    workflows = pytester.path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflows.joinpath("ci.yml").write_text("name: CI\n")
    migrations = pytester.path / "migrations"
    migrations.mkdir()
    migrations.joinpath("0001_start.sql").write_text(MIGRATION)
    pytester.makepyfile(test_sample=TESTS)
    return pytester


def run(project, *args):
    path = project.path / "out" / "report.json"
    project.runpytest("-p", "bank.testing.ci_report", f"--ci-report={path}", *args)
    return json.loads(path.read_text())


def test_report_follows_the_format(project, monkeypatch):
    monkeypatch.setenv("CI_HEAD_SHA", "a" * 40)
    monkeypatch.setenv("CI_BASE_SHA", "b" * 40)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/ledger-demo")
    report = run(project)

    assert report["schema"] == "aat-ci-report/1"
    assert report["repository"] == "owner/ledger-demo"
    assert (report["head_sha"], report["base_sha"]) == ("a" * 40, "b" * 40)
    assert report["generated_at"].endswith("Z")
    digest = hashlib.sha256(b"name: CI\n").hexdigest()
    assert report["workflow_files"] == {".github/workflows/ci.yml": digest}
    assert report["deps"] == {"fastapi": "0.115.0"}
    assert report["metrics"] == {"rows": 3, "transfers_per_s": 412.0}
    assert report["migrations"] == [
        {
            "number": "0001",
            "file": "migrations/0001_start.sql",
            "operations": ["CREATE TABLE", "ADD COLUMN", "UPDATE", "CREATE INDEX"],
        }
    ]
    outcomes = {t["id"].split("::")[-1]: t["outcome"] for t in report["tests"]}
    assert outcomes == {
        "test_ok": "passed",
        "test_bad": "failed",
        "test_skipped": "skipped",
        "test_setup_error": "error",
        "test_teardown_error": "error",
        "test_param[1]": "passed",
        "test_param[2]": "failed",
        "test_known_bug": "skipped",
    }
    assert all(t["id"].startswith("test_sample.py::") for t in report["tests"])
    assert all(isinstance(t["duration_s"], float) for t in report["tests"])


def test_shas_fall_back_to_git(project, monkeypatch):
    for name in ("CI_HEAD_SHA", "CI_BASE_SHA", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=project.path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "--allow-empty", "-m", "start")  # fmt: skip
    head = git("rev-parse", "HEAD")
    report = run(project, "-k", "test_ok")
    assert report["head_sha"] == head and report["base_sha"] == head
    assert report["repository"] == f"local/{project.path.name}"
    assert [t["outcome"] for t in report["tests"]] == ["passed"]


def test_no_report_without_option(project):
    result = project.runpytest("-p", "bank.testing.ci_report", "-k", "test_ok")
    result.assert_outcomes(passed=1)
    assert not (project.path / "out").exists()


@pytest.mark.parametrize("value", ["fast", True, float("nan"), None])
def test_record_metric_rejects_non_numbers(value):
    with pytest.raises((TypeError, ValueError)):
        Collector().record("speed", value)


def test_sql_operations_ignore_comments_and_clauses():
    sql = "-- UPDATE here\nALTER TABLE a RENAME TO b; DELETE FROM b; INSERT INTO b VALUES (1);"
    assert sql_operations(sql) == ["RENAME", "DELETE", "INSERT"]


def test_record_metric_fixture_is_available(record_metric):
    assert callable(record_metric)


def test_audit_vulnerabilities_counts_pip_audit_json(tmp_path):
    from bank.testing.ci_report import audit_vulnerabilities

    report = tmp_path / "audit.json"
    report.write_text(
        '{"dependencies": [{"name": "a", "version": "1", "vulns": [{"id": "X-1"}, {"id": "X-2"}]},'
        ' {"name": "b", "version": "2", "vulns": []}]}',
        encoding="utf-8",
    )
    assert audit_vulnerabilities(str(report)) == 2
    assert audit_vulnerabilities(str(tmp_path / "missing.json")) is None
    assert audit_vulnerabilities(None) is None


def test_data_hashes_block_is_optional(project):
    report = run(project, "-k", "test_ok")
    assert "data_hashes" not in report

    train, live = hashlib.sha256(b"train").hexdigest(), hashlib.sha256(b"live").hexdigest()
    project.makepyfile(
        test_model=f"""
def test_model(record_data_hashes):
    record_data_hashes("{train}", "{live}")
"""
    )
    report = run(project, "test_model.py")
    assert report["data_hashes"] == {"train": train, "live": live}
    assert report["schema"] == "aat-ci-report/1"


@pytest.mark.parametrize("value", ["", "ABC", "a" * 63, None])
def test_record_data_hashes_rejects_non_digests(value):
    with pytest.raises(ValueError):
        Collector().record_data_hashes(value, "a" * 64)
