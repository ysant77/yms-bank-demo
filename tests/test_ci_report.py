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

    assert report["schema"] == "aat-ci-report/2"
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
    assert report["schema"] == "aat-ci-report/2"


@pytest.mark.parametrize("value", ["", "ABC", "a" * 63, None])
def test_record_data_hashes_rejects_non_digests(value):
    with pytest.raises(ValueError):
        Collector().record_data_hashes(value, "a" * 64)


def test_record_samples_keeps_median_iqr_min_max_and_runs():
    c = Collector()
    c.record_samples("transfers_per_s", [440.0, 400.0, 420.0, 430.0, 410.0])
    assert c.metrics["transfers_per_s"] == {
        "median": 420.0,
        "iqr_low": 410.0,
        "iqr_high": 430.0,
        "min": 400.0,
        "max": 440.0,
        "runs": 5,
    }


def test_record_samples_with_an_even_count_interpolates():
    c = Collector()
    c.record_samples("t", [1.0, 2.0, 3.0, 4.0])
    assert c.metrics["t"] == {
        "median": 2.5,
        "iqr_low": 1.75,
        "iqr_high": 3.25,
        "min": 1.0,
        "max": 4.0,
        "runs": 4,
    }


def test_small_values_are_not_rounded_away():
    c = Collector()
    c.record_samples("latency_s", [0.0004, 0.0005, 0.0006, 0.0007])
    c.record("p95_s", 0.00042)
    assert 0.0005 < c.metrics["latency_s"]["median"] < 0.0006
    assert c.metrics["p95_s"] == 0.00042


@pytest.mark.parametrize(
    ("samples", "error"),
    [
        ([1.0, 2.0, 3.0], ValueError),
        ([1.0, 2.0, float("nan"), 4.0], ValueError),
        ([1e308] * 4, ValueError),
        ([10**400] * 4, ValueError),
        ("1234", TypeError),
        ((1.0, 2.0, 3.0, 4.0), TypeError),
        ([1.0, True, 3.0, 4.0], TypeError),
    ],
)
def test_record_samples_rejects_too_few_or_bad_values_and_writes_nothing(samples, error):
    c = Collector()
    c.record("kept", 1.5)
    with pytest.raises(error):
        c.record_samples("x", samples)
    assert c.metrics == {"kept": 1.5}


def test_a_metric_is_written_once_per_run():
    c = Collector()
    c.record_samples("transfers_per_s", [400.0, 410.0, 420.0, 430.0])
    before = dict(c.metrics)
    with pytest.raises(ValueError, match="already recorded"):
        c.record_samples("transfers_per_s", [99999.0] * 20)
    with pytest.raises(ValueError, match="already recorded"):
        c.record("transfers_per_s", 99999.0)
    assert c.metrics == before


def test_a_name_is_checked_before_the_samples():
    with pytest.raises(ValueError, match="non-empty"):
        Collector().record_samples("", "not samples")


def test_a_rewrite_by_another_test_fails_the_run(project):
    project.makepyfile(
        test_cheat="""
def test_real(record_samples):
    record_samples("transfers_per_s", [400.0, 410.0, 420.0, 430.0])

def test_cheat(record_samples):
    record_samples("transfers_per_s", [99999.0] * 20)
"""
    )
    result = project.runpytest(
        "-p", "bank.testing.ci_report", "--ci-report=out.json", "test_cheat.py"
    )
    result.assert_outcomes(passed=1, failed=1)
    report = json.loads((project.path / "out.json").read_text())
    assert report["metrics"]["transfers_per_s"]["median"] == 415.0


def test_record_samples_fixture_is_available(record_samples):
    assert callable(record_samples)


def test_audit_vulns_is_reserved_for_the_plugin():
    c = Collector()
    with pytest.raises(ValueError, match="report plugin"):
        c.record("audit_vulns", 0)
    with pytest.raises(ValueError, match="report plugin"):
        c.record_samples("audit_vulns", [0, 0, 0, 0])
    assert c.metrics == {}


def test_large_integer_samples_are_written_as_floats():
    c = Collector()
    c.record_samples("rows", [10**20, 10**20, 10**20, 10**20])
    assert all(isinstance(v, float) for k, v in c.metrics["rows"].items() if k != "runs")
