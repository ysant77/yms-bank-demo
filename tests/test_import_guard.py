"""scripts/import_guard.py: imports added by a pull request, compared with the known modules.

An added import is known when it is in the standard library, in this repository, or given by a
package in uv.lock. Any other added import is listed as unknown in the ``imports`` block of the
CI report. The script decides nothing: the verifier reads the block.
"""

import importlib.metadata
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("import_guard", ROOT / "scripts" / "import_guard.py")
ig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ig)

SOURCE = """\
import os
import json as j, xml.etree.ElementTree
from collections import abc
from . import sibling
from .pkg import thing


def late():
    import requests
    from yaml import safe_load
    importlib.import_module("evil.module")
    __import__(name)
"""

LOCK = """\
version = 1

[[package]]
name = "httpx"
version = "0.28.1"

[[package]]
name = "scikit-learn"
version = "1.9.1"
"""


def test_imports_are_read_on_the_given_lines_only():
    found = ig.imports_in(SOURCE, set(range(1, 30)))
    assert [(f["line"], f["module"]) for f in found] == [
        (1, "os"),
        (2, "json"),
        (2, "xml"),
        (3, "collections"),
        (4, None),
        (5, None),
        (9, "requests"),
        (10, "yaml"),
        (11, "evil"),
        (12, None),
    ]
    assert [f["how"] for f in found if f["line"] in (4, 11, 12)] == [
        "relative",
        "dynamic",
        "dynamic",
    ]
    assert [f["module"] for f in ig.imports_in(SOURCE, {1, 9})] == ["os", "requests"]
    assert ig.imports_in(SOURCE, set()) == []


def test_each_module_is_classified():
    local, locked = {"bank", "fraud"}, {"httpx", "sklearn"}
    assert ig.classify({"module": "os", "how": "import"}, local, locked) == "stdlib"
    assert ig.classify({"module": "__future__", "how": "import"}, local, locked) == "stdlib"
    assert ig.classify({"module": "bank", "how": "import"}, local, locked) == "local"
    assert ig.classify({"module": None, "how": "relative"}, local, locked) == "local"
    assert ig.classify({"module": "sklearn", "how": "import"}, local, locked) == "locked"
    assert ig.classify({"module": "requests", "how": "import"}, local, locked) == "unknown"
    assert ig.classify({"module": None, "how": "dynamic"}, local, locked) == "unknown"
    assert ig.classify({"module": "os", "how": "dynamic"}, local, locked) == "stdlib"


def test_locked_import_names_come_from_the_lock_through_the_installed_metadata():
    dists = {"sklearn": ["scikit-learn"], "yaml": ["PyYAML"], "httpx": ["httpx"]}
    assert ig.locked_import_names(LOCK, dists) == {"httpx", "sklearn", "scikit_learn"}


def test_every_import_on_this_branch_is_known():
    """No false alarm on the code as it is: each import of each tracked file classifies."""
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    locked = ig.locked_import_names(lock, importlib.metadata.packages_distributions())
    unknown = []
    files = subprocess.run(  # noqa: S603, S607 fixed git argument list, no shell
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        local = ig.local_names(ROOT, name)
        for found in ig.imports_in(text, set(range(1, text.count("\n") + 2))):
            if ig.classify(found, local, locked) == "unknown":
                unknown.append((name, found["line"], found["module"]))
    assert unknown == []


def _git(root, *args):
    return subprocess.run(  # noqa: S603, S607 fixed git argument list, no shell
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root, files, message):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    base = _commit(
        root,
        {
            "uv.lock": LOCK,
            "src/bank/__init__.py": "",
            "src/bank/api.py": "import socket\n\nx = 1\n",
            "tests/helper.py": "",
        },
        "base",
    )
    return root, base


def _run(root, base, head, tmp_path):
    report = tmp_path / "ci-report.json"
    report.write_text(json.dumps({"tests": []}), encoding="utf-8")
    args = ["--head", head, "--head-report", str(report)]
    if base:
        args += ["--base", base]
    assert ig.main(args, root=root, distributions={"httpx": ["httpx"]}) == 0
    return json.loads(report.read_text(encoding="utf-8"))["imports"]


def test_an_unknown_added_import_is_listed(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(
        root,
        {
            "src/bank/api.py": "import socket\nimport os\nimport evilpkg\nfrom bank import x\n"
            "import httpx\n\nx = 1\n",
            "tests/test_new.py": "import helper\nfrom pastebin_client import send\n",
        },
        "pr",
    )
    block = _run(root, base, head, tmp_path)
    assert block["result"] == "unknown_import"
    assert block["unknown"] == ["evilpkg", "pastebin_client"]
    assert block["lock_changed"] is False
    assert [(a["file"], a["line"], a["module"], a["kind"]) for a in block["added"]] == [
        ("src/bank/api.py", 2, "os", "stdlib"),
        ("src/bank/api.py", 3, "evilpkg", "unknown"),
        ("src/bank/api.py", 4, "bank", "local"),
        ("src/bank/api.py", 5, "httpx", "locked"),
        ("tests/test_new.py", 1, "helper", "local"),
        ("tests/test_new.py", 2, "pastebin_client", "unknown"),
    ]


def test_known_imports_pass_and_a_lock_change_is_stated(tmp_path):
    root, base = _repo(tmp_path)
    lock = LOCK + '\n[[package]]\nname = "requests"\nversion = "2.32.5"\n'
    head = _commit(root, {"uv.lock": lock, "src/bank/api.py": "import os\n\nx = 2\n"}, "pr")
    block = _run(root, base, head, tmp_path)
    assert block["result"] == "pass"
    assert block["unknown"] == []
    assert block["lock_changed"] is True


def test_a_file_that_does_not_parse_is_not_checked(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"src/bank/api.py": "import os\ndef broken(:\n"}, "pr")
    block = _run(root, base, head, tmp_path)
    assert block["result"] == "not_checked"
    assert block["unparsed"] == ["src/bank/api.py"]


def test_without_a_base_nothing_is_compared(tmp_path):
    root, base = _repo(tmp_path)
    block = _run(root, None, base, tmp_path)
    assert block == {
        "version": 1,
        "result": "not_checked",
        "reason": "no base commit",
        "added": [],
        "unknown": [],
        "unparsed": [],
        "lock_changed": False,
    }


def test_the_ci_workflow_writes_the_block_before_the_report_is_uploaded():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.index("scripts/import_guard.py")
    assert step < ci.index("- name: Upload CI report")
    assert step > ci.index("--ci-report=ci-report.json")


def test_the_script_runs_from_the_command_line(tmp_path):
    root, base = _repo(tmp_path)
    head = _commit(root, {"src/bank/api.py": "import os\n\nx = 3\n"}, "pr")
    report = tmp_path / "r.json"
    report.write_text("{}", encoding="utf-8")
    out = subprocess.run(  # noqa: S603 the running Python with a fixed argument list
        [sys.executable, str(ROOT / "scripts" / "import_guard.py"), "--base", base]
        + ["--head", head, "--head-report", str(report)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(report.read_text(encoding="utf-8"))["imports"]["result"] == "pass"
