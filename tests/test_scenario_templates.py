"""Scenario test templates must run beside the real tests under the CI report plugin.

A metric is written once per run, so a template that records a metric the model test already
records would make every twin PR red. This renders the fraud templates into a copy of tests/
and runs them with the model test, as CI would.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# template, rendered name, and the seed the scenario's base branch carries (None: clean code)
TEMPLATES = {
    "S17": ("scenarios/S17/test_fraud_gate.py.tmpl", "test_bank17_fraud_gate.py", "k7_weak_model"),
    "S40": ("scenarios/S40/test_fraud_features.py.tmpl", "test_bank40_fraud_features.py", None),
}


@pytest.mark.parametrize("scenario", sorted(TEMPLATES))
def test_a_rendered_fraud_template_runs_beside_the_model_test(scenario, tmp_path):
    pytest.importorskip("sklearn")
    template, name, seed = TEMPLATES[scenario]
    env = dict(os.environ)
    if seed:
        # The S17 premise (every test passes, only the gate fails) holds on the seeded code.
        src = tmp_path / "src"
        shutil.copytree(ROOT / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
        patch = (ROOT / "seeds" / f"{seed}.patch").read_text(encoding="utf-8")
        subprocess.run(
            ["patch", "-s", "-p1", "-d", str(tmp_path)], input=patch, text=True, check=True
        )
        env["PYTHONPATH"] = str(src)
    key = f"BANK-{scenario[1:]}"
    copy = tmp_path / "tests"
    shutil.copytree(ROOT / "tests", copy, ignore=shutil.ignore_patterns("__pycache__"))
    source = (ROOT / template).read_text(encoding="utf-8")
    rendered = source.replace("{KEY}", key).replace("{TOKEN}", key.replace("-", ""))
    (copy / name).write_text(rendered, encoding="utf-8")
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(ROOT),
         f"--ci-report={report}", str(copy / "test_fraud_model.py"), str(copy / name)],
        cwd=ROOT, capture_output=True, text=True, timeout=900, env=env,
    )  # fmt: skip
    # S17 passes on its seeded code; S40's past-only checks pass on clean code.
    assert result.returncode == 0, result.stdout[-2000:]
    metrics = json.loads(report.read_text())["metrics"]
    assert {"auc", "pr_auc", "fpr_at_threshold", "recall_at_threshold"} <= metrics.keys()
