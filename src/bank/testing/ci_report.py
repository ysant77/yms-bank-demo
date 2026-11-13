"""Pytest plugin that writes the ``aat-ci-report/1`` JSON report.

Enable it with ``-p bank.testing.ci_report`` (the project's pytest config does this) and write
the report with ``--ci-report=PATH``. Tests record numbers through the ``record_metric``
fixture. Model tests can record the sha256 of their training and live data through the
``record_data_hashes`` fixture, which adds an optional ``data_hashes`` block. Head and base
commits come from ``CI_HEAD_SHA`` and ``CI_BASE_SHA``, else from ``git rev-parse`` (``HEAD`` and
``CI_BASE_REF``, default ``main``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bank.migrate import parse as parse_migration

SCHEMA = "aat-ci-report/1"
COLLECTOR = pytest.StashKey["Collector"]()

# (pattern at the start of a statement, operation name), checked in order.
OPERATIONS = [
    (r"CREATE TABLE", "CREATE TABLE"),
    (r"CREATE (UNIQUE )?INDEX", "CREATE INDEX"),
    (r"CREATE VIEW", "CREATE VIEW"),
    (r"CREATE TRIGGER", "CREATE TRIGGER"),
    (r"ALTER TABLE \S+ ADD", "ADD COLUMN"),
    (r"ALTER TABLE \S+ DROP", "DROP"),
    (r"ALTER TABLE \S+ RENAME", "RENAME"),
    (r"DROP", "DROP"),
    (r"UPDATE", "UPDATE"),
    (r"INSERT", "INSERT"),
    (r"DELETE", "DELETE"),
]


def sql_operations(sql: str) -> list[str]:
    """The kinds of SQL statement in ``sql``, in order of first appearance."""
    sql = re.sub(r"--[^\n]*", "", sql)
    found: list[str] = []
    for statement in sql.split(";"):
        text = " ".join(statement.split()).upper()
        for pattern, name in OPERATIONS:
            if re.match(pattern + r"\b", text):
                if name not in found:
                    found.append(name)
                break
    return found


def migrations(root: Path) -> list[dict[str, object]]:
    result = []
    for path in sorted((root / "migrations").glob("*.sql")):
        migration = parse_migration(path)
        result.append(
            {
                "number": migration.number,
                "file": path.relative_to(root).as_posix(),
                "operations": sql_operations(migration.up),
            }
        )
    return result


def locked_deps(root: Path) -> dict[str, str]:
    """Package versions from uv.lock, without the project itself."""
    lock = root / "uv.lock"
    if not lock.is_file():
        return {}
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    deps = {}
    for package in data.get("package", []):
        source = package.get("source", {})
        if "editable" in source or "virtual" in source or "version" not in package:
            continue
        deps[package["name"]] = package["version"]
    return dict(sorted(deps.items()))


def workflow_hashes(root: Path) -> dict[str, str]:
    folder = root / ".github" / "workflows"
    if not folder.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def git_sha(root: Path, ref: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def audit_vulnerabilities(path: str | None) -> int | None:
    """Count known vulnerabilities in a pip-audit JSON report, if one was produced."""
    if not path or not Path(path).is_file():
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return sum(len(dep.get("vulns", [])) for dep in data.get("dependencies", []))


class Collector:
    def __init__(self) -> None:
        self.tests: dict[str, dict[str, object]] = {}
        self.metrics: dict[str, float | int | dict[str, float | int]] = {}
        self.data_hashes: dict[str, str] | None = None

    def record(self, name: str, value: float | int) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("metric name must be a non-empty string")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"metric {name} must be a number, not {type(value).__name__}")
        if not math.isfinite(value):
            raise ValueError(f"metric {name} must be finite")
        self.metrics[name] = value

    def record_samples(self, name: str, samples: list[float | int]) -> None:
        """A repeated measurement: the report holds its median, interquartile range and count, so
        a reader can tell a real difference from run-to-run noise."""
        if not isinstance(samples, list) or len(samples) < 4:
            raise ValueError(f"metric {name} needs at least 4 samples")
        for value in samples:
            self.record(name, value)  # the same checks as a single number
        low, mid, high = statistics.quantiles(samples, n=4, method="inclusive")
        self.metrics[name] = {
            "median": round(mid, 3),
            "iqr_low": round(low, 3),
            "iqr_high": round(high, 3),
            "runs": len(samples),
        }

    def record_data_hashes(self, train: str, live: str) -> None:
        for name, value in (("train", train), ("live", live)):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} data hash must be a lowercase sha256 hex digest")
        self.data_hashes = {"train": train, "live": live}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.tests.setdefault(
            report.nodeid, {"id": report.nodeid, "outcome": None, "duration_s": 0.0}
        )
        entry["duration_s"] = round(float(entry["duration_s"]) + report.duration, 6)
        if report.when == "call":
            entry["outcome"] = report.outcome  # passed, failed or skipped (xfail counts here)
        elif report.failed and entry["outcome"] != "failed":
            entry["outcome"] = "error"  # setup or teardown broke
        elif report.when == "setup" and report.skipped:
            entry["outcome"] = "skipped"

    def build(self, root: Path) -> dict[str, object]:
        base_ref = os.environ.get("CI_BASE_REF", "main")
        tests = [dict(t, outcome=t["outcome"] or "error") for t in self.tests.values()]
        vulns = audit_vulnerabilities(os.environ.get("CI_AUDIT_JSON"))
        if vulns is not None:
            self.metrics["audit_vulns"] = vulns
        report: dict[str, object] = {
            "schema": SCHEMA,
            "repository": os.environ.get("GITHUB_REPOSITORY") or f"local/{root.name}",
            "head_sha": os.environ.get("CI_HEAD_SHA") or git_sha(root, "HEAD"),
            "base_sha": os.environ.get("CI_BASE_SHA") or git_sha(root, base_ref),
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "workflow_files": workflow_hashes(root),
            "tests": tests,
            "metrics": dict(sorted(self.metrics.items())),
            "deps": locked_deps(root),
            "migrations": migrations(root),
        }
        if self.data_hashes is not None:
            report["data_hashes"] = dict(self.data_hashes)
        return report


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("ci-report").addoption(
        "--ci-report",
        metavar="PATH",
        default=None,
        help=f"write an {SCHEMA} JSON report of this run to PATH",
    )


def pytest_configure(config: pytest.Config) -> None:
    collector = Collector()
    config.stash[COLLECTOR] = collector
    config.pluginmanager.register(collector, "ci-report-collector")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    path = session.config.getoption("ci_report")
    if not path:
        return
    report = session.config.stash[COLLECTOR].build(session.config.rootpath)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def record_metric(request: pytest.FixtureRequest) -> Callable[[str, float | int], None]:
    """Record a number for the report: ``record_metric("transfers_per_s", 412.0)``."""
    return request.config.stash[COLLECTOR].record


@pytest.fixture
def record_samples(request: pytest.FixtureRequest) -> Callable[[str, list[float | int]], None]:
    """Record a repeated measurement: ``record_samples("transfers_per_s", [410.2, 398.7, ...])``."""
    return request.config.stash[COLLECTOR].record_samples


@pytest.fixture
def record_data_hashes(request: pytest.FixtureRequest) -> Callable[[str, str], None]:
    """Record the sha256 of the training and live data: ``record_data_hashes(train, live)``."""
    return request.config.stash[COLLECTOR].record_data_hashes
