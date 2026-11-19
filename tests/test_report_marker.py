"""The report hash marker (app design P0-3, 19 Nov 2026): binds a CI report to the run that made it.

The ``report`` job (pull requests) and the ``publish-main-report`` job (pushes to ``main``) run no
repository code. Each hashes the downloaded ``ci-report.json`` and uploads it again as an artifact
named ``ci-report-sha256-<hash>-attempt-<n>`` or ``main-report-sha256-<hash>-attempt-<n>``. Only
the run itself can upload an artifact to its own run. The app reads the run's artifact list (public,
no token) and compares the marker with the hash of the report comment or of ``main.json``.

Hash rule, the same in the app (``reportHash`` in src/verifier/report.js): sha256 of the UTF-8
text with trailing spaces, tabs, CR and LF removed. Both test suites check the same vector.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

# The workflow helpers of tests/test_ci_workflow.py, loaded by path: CI runs pytest without the
# repository root on sys.path, so "from tests..." does not import there.
_spec = importlib.util.spec_from_file_location(
    "ci_workflow_rules", Path(__file__).with_name("test_ci_workflow.py")
)
_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rules)
CI, jobs = _rules.CI, _rules.jobs

# The shared test vector (also in the app's test/report-provenance.test.js).
VECTOR_TEXT = '{"schema": "aat-ci-report/2", "head_sha": "abc"}\n\n \t\r\n'
VECTOR_HASH = "26557a85526d7658e174b7d4f8d8135894e0872a1633131809bf03d75d4b399d"

MARKERS = {"report": "ci-report-sha256-", "publish-main-report": "main-report-sha256-"}
REPO_CODE = re.compile(r"actions/checkout@|\buv (run|sync)|\bpytest\b|scripts/")


def steps(block: str) -> list[str]:
    return re.split(r"^      - ", block, flags=re.M)[1:]


def marker_errors(text: str) -> list[str]:
    errors = []
    all_jobs = jobs(text)
    for name, prefix in MARKERS.items():
        block = all_jobs.get(name, "")
        if REPO_CODE.search(block):
            errors.append(f"{name} runs repository code")
        hashing = [s for s in steps(block) if re.search(r"^        id: hash$", s, flags=re.M)]
        if len(hashing) != 1:
            errors.append(f"{name} has no single hash step (id: hash)")
        name_line = (
            f"          name: {prefix}${{{{ steps.hash.outputs.sha256 }}}}"
            f"-attempt-${{{{ github.run_attempt }}}}\n"
        )
        uploads = [
            s for s in steps(block) if "uses: actions/upload-artifact@" in s and name_line in s
        ]
        if len(uploads) != 1 or "          path: ci-report.json\n" not in uploads[0]:
            errors.append(f"{name} does not upload one {prefix}<hash> marker of ci-report.json")
    # No other job may upload a marker: the jobs that run repository code must not.
    for name, block in all_jobs.items():
        if name not in MARKERS and re.search(r"(ci|main)-report-sha256-", block):
            errors.append(f"{name} names a report marker")
    return errors


def hash_command(text: str, job: str) -> str:
    """The python3 command of the job's hash step (the shared hash rule)."""
    step = next(
        s for s in steps(jobs(text)[job]) if re.search(r"^        id: hash$", s, flags=re.M)
    )
    m = re.search(r"python3 -c '([^']*)'", step)
    assert m, "the hash step runs python3 -c '...'"
    return m.group(1)


def test_the_report_jobs_upload_one_marker_and_run_no_repository_code():
    assert marker_errors(CI) == []


@pytest.mark.parametrize("job", sorted(MARKERS))
def test_the_hash_step_follows_the_shared_rule(job, tmp_path):
    report = tmp_path / "ci-report.json"
    report.write_bytes(VECTOR_TEXT.encode("utf-8"))
    out = subprocess.run(  # noqa: S603 the running Python with a fixed argument list, no shell
        [sys.executable, "-c", hash_command(CI, job), str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == VECTOR_HASH
    # The comment adds a blank line after the file; the hash does not change.
    report.write_bytes((VECTOR_TEXT.rstrip(" \t\r\n") + "\n").encode("utf-8"))
    again = subprocess.run(  # noqa: S603 the running Python with a fixed argument list, no shell
        [sys.executable, "-c", hash_command(CI, job), str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert again.stdout.strip() == VECTOR_HASH


REMOVED = [
    ("report", "ci-report-sha256-", "ci-report-x-"),
    ("publish-main-report", "main-report-sha256-", "main-report-x-"),
    ("report", "        id: hash\n", ""),
]


@pytest.mark.parametrize(("job", "old", "new"), REMOVED)
def test_the_rule_fails_when_a_marker_control_is_removed(job, old, new):
    block = jobs(CI)[job]
    assert old in block
    assert marker_errors(CI.replace(block, block.replace(old, new, 1), 1)) != []


def test_the_rule_fails_when_a_report_job_runs_repository_code():
    block = jobs(CI)["report"]
    changed = block.replace(
        "    steps:\n", "    steps:\n      - uses: actions/checkout@" + "0" * 40 + "\n", 1
    )
    assert marker_errors(CI.replace(block, changed, 1)) != []


def test_the_rule_fails_when_the_test_job_names_a_marker():
    block = jobs(CI)["test"]
    changed = block + "      - run: echo ci-report-sha256-forged\n"
    assert marker_errors(CI.replace(block, changed, 1)) != []
