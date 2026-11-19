"""Write the imports block of the CI report: the imports that a pull request adds.

    uv run python scripts/import_guard.py --base <sha> --head-report ci-report.json

An added import is **known** when its top-level module is in the standard library, in this
repository (a package under ``src/``, or a module beside the importing file), or given by a
package in ``uv.lock``. Any other added import is **unknown**. A dynamic import whose name is not
a constant (``__import__(name)``) is unknown too.

The block lists each added import with its kind, the unknown modules, the files that do not
parse, and whether ``uv.lock`` or ``pyproject.toml`` changed (a new package in the lock makes its
import "known", so a lock change is a fact the reader must see). The script decides nothing:
``result`` is ``pass``, ``unknown_import`` or ``not_checked``, and the verifier reads it.

- The added lines come from the diff between ``--base`` and ``--head``, read at ``--head``.
- The import names of a locked package come from the installed package metadata (CI runs
  ``uv sync --locked`` first), and from the package name itself.
- Without ``--base`` (a push to main) nothing is compared.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
TRUST_FILES = ("uv.lock", "pyproject.toml")
DYNAMIC = {("importlib", "import_module"), (None, "__import__"), (None, "import_module")}


def git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 fixed git argument list, no shell; git from the runner PATH
        ["git", "--literal-pathspecs", *args],  # noqa: S607 fixed git argument list, no shell
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def added_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    """New-side added lines of each Python file that exists on ``head``."""
    names = git(root, "diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", base, head)
    result: dict[str, set[int]] = {}
    for name in names.split("\0"):
        if not name.endswith(".py"):
            continue
        lines: set[int] = set()
        for line in git(root, "diff", "-U0", "--no-renames", base, head, "--", name).splitlines():
            m = HUNK.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                lines.update(range(start, start + count))
        if lines:
            result[name] = lines
    return result


def _call_name(func: ast.expr) -> tuple[str | None, str] | None:
    if isinstance(func, ast.Name):
        return (None, func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    return None


def imports_in(source: str, lines: set[int]) -> list[dict]:
    """Imports that start on one of ``lines``: top-level module and how it is imported.

    Raises SyntaxError when the source does not parse."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if getattr(node, "lineno", None) not in lines:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, node.col_offset, alias.name.split(".")[0], "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append((node.lineno, node.col_offset, None, "relative"))
            else:
                found.append((node.lineno, node.col_offset, node.module.split(".")[0], "import"))
        elif isinstance(node, ast.Call) and _call_name(node.func) in DYNAMIC:
            arg = node.args[0] if node.args else None
            name = (
                arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
            )
            module = name.split(".")[0] if name and not name.startswith(".") else None
            found.append((node.lineno, node.col_offset, module, "dynamic"))
    found.sort(key=lambda f: (f[0], f[1]))
    return [{"line": line, "module": module, "how": how} for line, _, module, how in found]


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def locked_import_names(lock_text: str, distributions: dict[str, list[str]]) -> set[str]:
    """Top-level import names of the packages in ``uv.lock``."""
    locked = {normalize(p["name"]) for p in tomllib.loads(lock_text).get("package", [])}
    names = {name for name in locked}
    for module, dists in distributions.items():
        if any(normalize(d) in locked for d in dists):
            names.add(module)
    return names


def local_names(root: Path, path: str, tracked: list[str] | None = None) -> set[str]:
    """Modules of this repository that ``path`` can import by a plain name."""
    if tracked is None:
        tracked = git(root, "ls-files", "-z").split("\0")
    folder = PurePosixPath(path).parent
    names = set()
    for name in tracked:
        p = PurePosixPath(name)
        if len(p.parts) >= 2 and p.parts[0] == "src":
            names.add(p.parts[1].removesuffix(".py"))
        if p.parent == folder and p.suffix == ".py":
            names.add(p.stem)
    return names


def classify(found: dict, local: set[str], locked: set[str]) -> str:
    module = found["module"]
    if found["how"] == "relative":
        return "local"
    if module is None:
        return "unknown"
    if module in sys.stdlib_module_names or module == "__future__":
        return "stdlib"
    if module in local:
        return "local"
    if module in locked or normalize(module) in locked:
        return "locked"
    return "unknown"


def build_block(
    root: Path, base: str | None, head: str, distributions: dict[str, list[str]]
) -> dict:
    block: dict = {"version": 1, "result": "not_checked"}
    added: list[dict] = []
    unparsed: list[str] = []
    lock_changed = False
    if base is None:
        block["reason"] = "no base commit"
    else:
        changed = git(root, "diff", "--name-only", base, head, "--", *TRUST_FILES).split()
        lock_changed = bool(changed)
        lock = git(root, "show", f"{head}:uv.lock")
        locked = locked_import_names(lock, distributions)
        tracked = git(root, "ls-tree", "-r", "-z", "--name-only", head).split("\0")
        for name, lines in sorted(added_lines(root, base, head).items()):
            try:
                found = imports_in(git(root, "show", f"{head}:{name}"), lines)
            except SyntaxError:
                unparsed.append(name)
                continue
            local = local_names(root, name, tracked)
            for f in found:
                kind = classify(f, local, locked)
                added.append({"file": name, **f, "kind": kind})
    unknown = sorted({a["module"] or "(dynamic)" for a in added if a["kind"] == "unknown"})
    if base is not None:
        block["result"] = "unknown_import" if unknown else "not_checked" if unparsed else "pass"
    for a in added:
        a.pop("how")
    block.update(added=added, unknown=unknown, unparsed=unparsed, lock_changed=lock_changed)
    return block


def main(
    argv: list[str] | None = None,
    root: Path | None = None,
    distributions: dict[str, list[str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base branch commit of the pull request")
    parser.add_argument("--head", default="HEAD", help="the head commit (default HEAD)")
    parser.add_argument("--head-report", required=True, type=Path)
    args = parser.parse_args(argv)
    root = root or Path.cwd()
    if distributions is None:
        distributions = importlib.metadata.packages_distributions()
    head = git(root, "rev-parse", args.head).strip()
    report = json.loads(args.head_report.read_text(encoding="utf-8"))
    report["imports"] = build_block(root, args.base, head, distributions)
    args.head_report.write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("imports:", json.dumps(report["imports"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
