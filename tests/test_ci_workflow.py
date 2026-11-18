"""Rules for .github/workflows/ci.yml that keep CI a trustworthy gate.

Each rule is a function over the workflow text. Each test runs the rule on the real file and on a
copy with the control removed, so a test fails when its control is gone. No YAML library is
needed: the file uses a plain two-space layout.
"""

import re
from pathlib import Path

import pytest

CI = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(
    encoding="utf-8"
)


def jobs(text: str) -> dict[str, str]:
    """Job name -> its block of lines, from the two-space keys under ``jobs:``."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def needs_of(block: str) -> list[str]:
    m = re.search(r"^    needs: \[(.*)\]$", block, flags=re.M)
    if m:
        return [n.strip() for n in m.group(1).split(",")]
    m = re.search(r"^    needs: (\S+)$", block, flags=re.M)
    return [m.group(1)] if m else []


def gate_errors(text: str) -> list[str]:
    all_jobs = jobs(text)
    gate = all_jobs.get("gate")
    if gate is None:
        return ["no gate job"]
    errors = []
    if not re.search(r"^    if: \$\{\{ always\(\) \}\}$", gate, flags=re.M):
        errors.append("the gate does not run always()")
    missing = sorted(set(all_jobs) - {"gate"} - set(needs_of(gate)))
    if missing:
        errors.append(f"the gate does not need: {', '.join(missing)}")
    return errors


def permission_errors(text: str) -> list[str]:
    errors = []
    top = text.split("\njobs:\n", 1)[0]
    if not re.search(r"^permissions:\n  contents: read\n", top, flags=re.M):
        errors.append("top-level permissions are not exactly contents: read")
    for name, block in jobs(text).items():
        if "uv run" in block or "uv sync" in block:
            # A job that runs repository code gets no extra permission and no token.
            if re.search(r"^    permissions:", block, flags=re.M):
                errors.append(f"{name} runs repository code and widens permissions")
        # A step that sees a token runs no repository code (uv, python, pytest, scripts/).
        for step in re.split(r"^      - ", block, flags=re.M):
            if re.search(r"GH_TOKEN|GITHUB_TOKEN|secrets\.", step) and re.search(
                r"\buv (run|sync)|\bpython\b|\bpytest\b|scripts/", step
            ):
                errors.append(f"a step of {name} runs repository code and sees a token")
    return errors


def checkout_errors(text: str) -> list[str]:
    errors = []
    steps = re.split(r"^      - ", text, flags=re.M)
    for step in steps:
        if step.startswith("uses: actions/checkout@") and "persist-credentials: false" not in step:
            errors.append("a checkout keeps its credentials")
    return errors


def timeout_errors(text: str) -> list[str]:
    errors = []
    for name, block in jobs(text).items():
        m = re.search(r"^    timeout-minutes: (\d+)$", block, flags=re.M)
        if not m:
            errors.append(f"{name} has no timeout-minutes")
        elif int(m.group(1)) > 30:
            errors.append(f"{name} may run {m.group(1)} minutes")
    return errors


def pin_errors(text: str) -> list[str]:
    return [
        f"not pinned to a commit: {u}"
        for u in re.findall(r"uses: (\S+)", text)
        if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u)
    ]


RULES = [gate_errors, permission_errors, checkout_errors, timeout_errors, pin_errors]


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.__name__)
def test_the_ci_workflow_keeps_its_controls(rule):
    assert rule(CI) == []


REMOVED = [
    (
        gate_errors,
        "    needs: [test, report, publish-main-report, secrets]",
        "    needs: [test, report]",
    ),
    (gate_errors, "    if: ${{ always() }}", "    if: ${{ success() }}"),
    (permission_errors, "permissions:\n  contents: read\n", "permissions:\n  contents: write\n"),
    (
        permission_errors,
        "  test:\n    runs-on: ubuntu-latest\n",
        "  test:\n    runs-on: ubuntu-latest\n    permissions:\n      pull-requests: write\n",
    ),
    (
        permission_errors,
        "          CI_AUDIT_JSON: audit.json\n",
        "          CI_AUDIT_JSON: audit.json\n          GH_TOKEN: ${{ github.token }}\n",
    ),
    (checkout_errors, "          persist-credentials: false\n", ""),
    (timeout_errors, "    timeout-minutes: 10\n", ""),
    (
        pin_errors,
        "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
        "astral-sh/setup-uv@v7",
    ),
]


@pytest.mark.parametrize(
    ("rule", "old", "new"), REMOVED, ids=lambda x: getattr(x, "__name__", None)
)
def test_each_rule_fails_when_its_control_is_removed(rule, old, new):
    assert old in CI
    assert rule(CI.replace(old, new, 1)) != []
