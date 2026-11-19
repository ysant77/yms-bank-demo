"""Build the investigation branch for discovery from symptoms (Mode B).

    python scripts/build_discovery_branch.py <work tree>

The work tree is a clean checkout of this repository (for example a clone of the demo
repository) on a branch whose name starts with ``investigate/``. The script makes three commits
there, with the repository's own git identity:

1. It removes the answer key: the seed patches, the scenarios, the scenario generator and its
   tests, this script and its test, and the tests that name the two defects (the thread race
   test, the two-process race test and the Send button CSS check). It removes the text that
   points to them from the README and two comments.
2. It applies the withdrawal race as an ordinary change of ``src/bank/ledger.py``.
3. It applies the hidden Send button as an ordinary change of ``src/bank/web/app.css``.

The patches are read from the work tree's own ``HEAD`` before step 1. Nothing is pushed.
Exit code 2 and a line starting with ``refused:`` on a wrong start.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

BRANCH_PREFIX = "investigate/"
PATCHES = [
    ("seeds/k1_race.patch", "refactor(ledger): check the balance before the write transaction"),
    ("seeds/k1_hidden_button.patch", "style(web): adjust the layout below 430 px"),
]
REMOVE = [
    "seeds",
    "scenarios",
    "scripts/scenario.py",
    "scripts/build_discovery_branch.py",
    "tests/test_scenario_script.py",
    "tests/test_scenario_templates.py",
    "tests/test_concurrency.py",
    "tests/test_concurrency_processes.py",
    "tests/test_discovery_branch.py",
]
# Top-level names removed from a file that otherwise stays (the Send button CSS check).
REMOVE_DEFS = {
    "tests/test_web.py": [
        "SMALL_SCREEN_PX",
        "strip_comments",
        "css_rules",
        "to_px",
        "applies_at_or_below",
        "send_button_tokens",
        "targets",
        "hiding_rules",
        "test_send_button_not_hidden_at_or_below_430px",
        "test_css_check_finds_hiding_rules",
    ],
}
# Exact text edits: (file, old, new). Each old text must be present once.
EDITS = [
    ("README.md", "| `seeds/` | Patches that add known defects for verifier scenarios. |\n", ""),
    (
        "README.md",
        "The default run includes a 20-thread withdrawal race test, an inclusive statement "
        "end-date\ntest, a test that walks every `/api/accounts/...` route to check the owner "
        "rule, and a CSS\ncheck that the Send button stays visible at 430 px and below.\n",
        "The default run includes an inclusive statement end-date test and a test that walks "
        "every\n`/api/accounts/...` route to check the owner rule.\n",
    ),
    (
        "README.md",
        "## Seeds\n\nKnown defects live as patches in `seeds/`, never on `main`. "
        "See [seeds/README.md](seeds/README.md)\n"
        "for what each seed does and which scenarios use it.\n\n",
        "",
    ),
    (
        "src/fraud/data.py",
        "  # about 290 holdout frauds: see seeds/README.md for the margins",
        "  # about 290 holdout frauds",
    ),
    ("tests/test_fraud_model.py", " See seeds/README.md for the measurements.", ""),
]


class Refused(Exception):
    pass


def git(tree: Path, *args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=tree, input=stdin, capture_output=True, text=True)
    if out.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])}: {out.stderr.strip()}")
    return out.stdout


def remove_defs(path: Path, names: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    spans = []
    found = set()
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
        if name in names:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            spans.append((start, node.end_lineno))
            found.add(name)
    missing = set(names) - found
    if missing:
        raise Refused(f"{path.name} has no {', '.join(sorted(missing))}")
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1 : end]
    path.write_text("".join(lines), encoding="utf-8")


def tidy(tree: Path, files: list[str]) -> None:
    """Drop imports that the removal left unused, and format the changed files."""
    for args in (["check", "--fix", "--select", "F401", *files], ["format", *files]):
        command = [sys.executable, "-m", "ruff", *args]
        out = subprocess.run(command, cwd=tree, capture_output=True, text=True)
        if out.returncode != 0:
            raise Refused(f"ruff {args[0]}: {out.stdout.strip()} {out.stderr.strip()}")


def build(tree: Path) -> list[str]:
    if not (tree / ".git").exists():
        raise Refused(f"{tree} is not the root of a git work tree")
    branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not branch.startswith(BRANCH_PREFIX):
        raise Refused(f"the branch must start with {BRANCH_PREFIX}, not {branch}")
    if git(tree, "status", "--porcelain"):
        raise Refused("the work tree has changes; start from a clean checkout")
    patches = []
    for path, subject in PATCHES:
        try:
            patches.append((git(tree, "show", f"HEAD:{path}"), subject))
        except Refused:
            raise Refused(f"HEAD has no {path}; the branch may be built already") from None

    git(tree, "rm", "-r", "-q", "--ignore-unmatch", "--", *REMOVE)
    for name, defs in REMOVE_DEFS.items():
        remove_defs(tree / name, defs)
    for name, old, new in EDITS:
        path = tree / name
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise Refused(f"{name}: the text to edit is not there once")
        path.write_text(text.replace(old, new), encoding="utf-8")
    tidy(tree, list(REMOVE_DEFS))
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "chore: remove the development kits from this branch")
    commits = [git(tree, "rev-parse", "HEAD").strip()]

    for patch, subject in patches:
        git(tree, "apply", "--index", "-", stdin=patch)
        git(tree, "commit", "-q", "-m", subject)
        commits.append(git(tree, "rev-parse", "HEAD").strip())
    return commits


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("refused: usage: build_discovery_branch.py <work tree>")
        return 2
    try:
        commits = build(Path(argv[0]).resolve())
    except Refused as error:
        print(f"refused: {error}")
        return 2
    for sha in commits:
        print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
