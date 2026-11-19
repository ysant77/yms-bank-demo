"""The investigation branch for discovery from symptoms (Mode B) has no answer key.

scripts/build_discovery_branch.py turns a copy of this repository into a branch where two
defects are ordinary commits: the withdrawal race and the hidden Send button. An agent that
reads the branch must find them from a symptom. So the branch must not hold the seed patches,
the scenarios, the scenario generator, the tests that name the defects (the thread race test,
the two-process race test and the Send button CSS check) or any text that points to them.

This test builds the branch in a temporary copy of the current tree and checks that. The
builder and this test are removed from the branch too.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_discovery_branch.py"

# Text that names a defect, a seed or the scenario kit. None of it may stay on the branch.
LABELS = [
    "seeds/",
    "k1_race",
    "k1_hidden_button",
    "hidden_button",
    "known defect",
    "scenarios/",
    "scenario.py",
    "test_concurrent_withdrawals_never_overdraw",
    "test_send_button_not_hidden",
    "test_concurrent_withdrawals_from_separate_processes",
    "build_discovery_branch",
]
GONE = [
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


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout


def copy_of_tree(dest: Path, branch: str = "investigate/symptoms") -> Path:
    """A fresh repository with the tracked files of this checkout, on ``branch``."""
    files = git(ROOT, "ls-files", "-z").split("\0")
    for name in filter(None, files):
        src = ROOT / name
        if not src.is_file() or src.is_symlink():
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.name", "Test Builder")
    git(dest, "config", "user.email", "builder@example.invalid")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "base")
    git(dest, "switch", "-q", "-c", branch)
    return dest


def build(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(BUILDER), str(tree)], capture_output=True, text=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tree = copy_of_tree(tmp_path_factory.mktemp("discovery"))
    result = build(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    return tree


def test_no_answer_key_path_stays(built):
    tracked = git(built, "ls-files").splitlines()
    for gone in GONE:
        assert not any(t == gone or t.startswith(gone + "/") for t in tracked), gone
        assert not (built / gone).exists(), gone


def test_no_file_names_a_defect_seed_or_scenario(built):
    hits = []
    for name in git(built, "ls-files").splitlines():
        text = (built / name).read_text(encoding="utf-8", errors="replace").lower()
        hits += [f"{name}: {label}" for label in LABELS if label.lower() in text]
    assert hits == []


def test_both_defects_are_ordinary_commits(built):
    log = git(built, "log", "--format=%s", "main..HEAD").splitlines()
    assert len(log) == 3, log
    for subject in log:
        assert not any(label.lower() in subject.lower() for label in LABELS + ["defect", "seed", "bug"])
    ledger = (built / "src/bank/ledger.py").read_text(encoding="utf-8")
    assert "time.sleep(0.01)" in ledger
    css = (built / "src/bank/web/app.css").read_text(encoding="utf-8")
    assert "#send-button { display: none; }" in css
    # The authors are the repository's configured identity; the builder sets none of its own.
    authors = set(git(built, "log", "--format=%an <%ae>", "main..HEAD").splitlines())
    assert authors == {"Test Builder <builder@example.invalid>"}


def test_the_branch_passes_lint(built):
    for args in (["check", "."], ["format", "--check", "."]):
        result = subprocess.run([sys.executable, "-m", "ruff", *args], cwd=built, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_the_remaining_tests_still_collect(built):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=built,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]


def test_the_builder_refuses_a_wrong_start(tmp_path):
    # Not on an investigate/ branch.
    tree = copy_of_tree(tmp_path / "a", branch="feature/x")
    assert build(tree).returncode == 2
    # A dirty work tree.
    tree = copy_of_tree(tmp_path / "b")
    (tree / "README.md").write_text("changed\n", encoding="utf-8")
    assert build(tree).returncode == 2
    # Already built: the seeds are gone.
    tree = copy_of_tree(tmp_path / "c")
    assert build(tree).returncode == 0
    assert build(tree).returncode == 2
