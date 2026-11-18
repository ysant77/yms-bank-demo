"""Product code reads no new environment variable and opens no network or process call without
a review. A new entry in the lists below is a decision that a reviewer sees in the diff.

Limit: this is a static check of names. A read through another helper, or a call through a
library that already is imported, is not seen.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"

# (module path under src/, variable name) pairs that product code may read.
ALLOWED_ENV = {
    ("bank/api.py", "BANK_DB"),
    ("bank/migrate.py", "BANK_MIGRATIONS_DIR"),
    ("bank/testing/ci_report.py", "CI_BASE_REF"),
    ("bank/testing/ci_report.py", "CI_AUDIT_JSON"),
    ("bank/testing/ci_report.py", "GITHUB_REPOSITORY"),
    ("bank/testing/ci_report.py", "CI_HEAD_SHA"),
    ("bank/testing/ci_report.py", "CI_BASE_SHA"),
}
NETWORK_OR_PROCESS = {
    "socket",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "smtplib",
    "ftplib",
    "subprocess",
}
ALLOWED_IMPORTS = {("bank/testing/ci_report.py", "subprocess")}


def findings(source: str, rel: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                top = name.split(".")[0]
                if top in NETWORK_OR_PROCESS and (rel, top) not in ALLOWED_IMPORTS:
                    out.append(f"{rel}: imports {name}")
        if isinstance(node, ast.Call):
            f = node.func
            text = ast.unparse(f)
            if text in {"os.environ.get", "os.getenv", "getenv"}:
                arg = node.args[0] if node.args else None
                var = (
                    arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg) if arg else "?"
                )
                if (rel, var) not in ALLOWED_ENV:
                    out.append(f"{rel}: reads environment variable {var}")
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            var = (
                node.slice.value
                if isinstance(node.slice, ast.Constant)
                else ast.unparse(node.slice)
            )
            if (rel, var) not in ALLOWED_ENV:
                out.append(f"{rel}: reads environment variable {var}")
    return out


def test_product_code_has_no_unreviewed_side_effect():
    problems = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        problems += findings(path.read_text(encoding="utf-8"), rel)
    assert problems == []


def test_a_new_environment_read_or_network_import_is_found():
    code = (
        "import os\nimport socket\nfrom urllib import request\n"
        'TOKEN = os.environ.get("BANK_SUPPORT_TOKEN", "support")\n'
        'KEY = os.environ["API_KEY"]\nX = os.getenv("BANK_DB")\n'
    )
    assert findings(code, "bank/api.py") == [
        "bank/api.py: imports socket",
        "bank/api.py: imports urllib",
        "bank/api.py: reads environment variable BANK_SUPPORT_TOKEN",
        "bank/api.py: reads environment variable API_KEY",
    ]
