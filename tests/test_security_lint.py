"""The security rules of the lint step (ruff rule group S) stay on, and each exception is stated.

- The lint configuration selects the group S.
- The only file-level exception is S101 (``assert``) in tests/: pytest reports a failed test
  through ``assert``.
- Every other S exception is one ``# noqa: S...`` on one line, with the reason on the same line.

Each rule is a function over the text. Each test runs it on the real files and on a copy with the
control removed, so the test fails when the control is gone.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
NOQA_S = re.compile(r"#\s*noqa:\s*((?:S\d{3}\s*,?\s*)+)(.*)$")


def config_errors(text: str) -> list[str]:
    lint = tomllib.loads(text).get("tool", {}).get("ruff", {}).get("lint", {})
    errors = []
    if "S" not in lint.get("select", []):
        errors.append("the lint step does not select S")
    for name in ("ignore", "extend-ignore"):
        for code in lint.get(name, []):
            if code.startswith("S"):
                errors.append(f"{name} turns off {code} everywhere")
    for pattern, codes in lint.get("per-file-ignores", {}).items():
        for code in codes:
            if code == "S" or (code.startswith("S") and (code, pattern) != ("S101", "tests/*.py")):
                errors.append(f"per-file-ignores turns off {code} for {pattern}")
    return errors


def noqa_errors(path: str, text: str) -> list[str]:
    errors = []
    for number, line in enumerate(text.splitlines(), start=1):
        m = NOQA_S.search(line)
        if m and len(m.group(2).strip()) < 10:
            errors.append(f"{path}:{number}: an S exception with no reason")
    return errors


def python_files() -> dict[str, str]:
    files = {}
    for folder in ("src", "scripts", "tests"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            files[str(path.relative_to(ROOT))] = path.read_text(encoding="utf-8")
    return files


def test_the_lint_step_selects_the_security_rules():
    assert config_errors(PYPROJECT) == []


def test_removing_s_from_the_selection_is_found():
    without = PYPROJECT.replace('"UP", "S"]', '"UP"]')
    assert without != PYPROJECT
    assert config_errors(without) == ["the lint step does not select S"]


def test_a_wide_exception_is_found():
    wide = PYPROJECT.replace(
        "[tool.ruff.lint.per-file-ignores]",
        '[tool.ruff.lint.per-file-ignores]\n"src/**" = ["S608"]',
    )
    assert config_errors(wide) == ["per-file-ignores turns off S608 for src/**"]
    every = PYPROJECT.replace('select = ["E"', 'ignore = ["S603"]\nselect = ["E"')
    assert config_errors(every) == ["ignore turns off S603 everywhere"]


def test_every_line_exception_states_its_reason():
    errors = []
    for path, text in python_files().items():
        errors += noqa_errors(path, text)
    assert errors == []


def test_a_line_exception_with_no_reason_is_found():
    assert noqa_errors("a.py", "x = 1  # noqa: S311\n") == ["a.py:1: an S exception with no reason"]
    assert noqa_errors("a.py", "x = 1  # noqa: S603, S607\n") != []
    assert noqa_errors("a.py", "x = 1  # noqa: S311 seeded demo data, never a secret\n") == []
