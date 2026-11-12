"""Safety rules of scripts/scenario.py: nothing unsafe may reach git."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scenario.py"
spec = importlib.util.spec_from_file_location("scenario_script", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)


@pytest.mark.parametrize("key", ["BANK-3", "BANK-123", "QA2-7"])
def test_valid_keys(key):
    assert scenario.check_key(key) == key


@pytest.mark.parametrize("key", ["BANK.3", "bank-3", "BANK-0", "BANK-3; rm -rf /", "-3", ""])
def test_invalid_keys_are_refused(key):
    with pytest.raises(scenario.ScenarioError):
        scenario.check_key(key)


@pytest.mark.parametrize(
    "name, kind",
    [("main", "twin"), ("master", "base"), ("test/main", "base"), ("fix/../main", "twin"),
     ("feature/x", "twin"), ("fix/x", "base"), ("test/x", "twin"), ("ci-reports", "base")],
)  # fmt: skip
def test_protected_or_badly_named_branches_are_refused(name, kind):
    with pytest.raises(scenario.ScenarioError):
        scenario.check_branch(name, kind)


def test_allowed_branches():
    assert scenario.check_branch("test/s01-seed-hidden-button", "base")
    assert scenario.check_branch("fix/bank-3-send-button", "twin")
    assert scenario.check_branch("feat/bank-9-export", "twin")


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", ".git/config", "tests/../../x"])
def test_paths_must_stay_inside_the_repository(path):
    with pytest.raises(scenario.ScenarioError):
        scenario.safe_path(path)


def test_a_scenario_that_targets_main_is_refused_before_git():
    spec = {"base_branch": "test/s99-seed", "twin_branch": "main", "attempts": []}
    with pytest.raises(scenario.ScenarioError):
        scenario.validate_attempt(spec, {"steps": []}, scenario.tokens("BANK-3"))


def test_a_step_that_writes_outside_the_repository_is_refused():
    spec = {"base_branch": "test/s99-seed", "twin_branch": "fix/{key_lower}-x"}
    attempt = {"steps": [{"add_test": "scenarios/README.md", "dest": "../{token}.py"}]}
    with pytest.raises(scenario.ScenarioError):
        scenario.validate_attempt(spec, attempt, scenario.tokens("BANK-3"))


@pytest.mark.parametrize(
    "name", sorted(p.stem for p in (SCRIPT.parent.parent / "scenarios").glob("S*.toml"))
)
def test_every_scenario_file_validates(name):
    spec_ = scenario.load(name)
    for attempt in spec_["attempts"]:
        scenario.validate_attempt(spec_, attempt, scenario.tokens("BANK-3"))


def test_bad_arguments_return_2_without_touching_git(capsys):
    before = subprocess.run(
        ["git", "branch", "--list"], cwd=scenario.ROOT, capture_output=True, text=True
    ).stdout
    assert scenario.main(["attempt", "S01", "v1", "--key", "BANK.3"]) == 2
    assert scenario.main(["attempt", "S01", "v1", "--key", "BANK-3", "--author", "live"]) == 2
    after = subprocess.run(
        ["git", "branch", "--list"], cwd=scenario.ROOT, capture_output=True, text=True
    ).stdout
    assert before == after
    assert "refused" in capsys.readouterr().err


def test_clean_ref_is_the_parent_of_the_seed_commit(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip

    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("clean\n")
    git("add", "-A")
    git("commit", "-q", "-m", "clean")
    clean_sha = git("rev-parse", "HEAD")
    git("switch", "-q", "-c", "test/s99-seed")
    (tmp_path / "a.txt").write_text("seeded\n")
    git("commit", "-q", "-am", "test(seed): apply k9 for scenario S99")
    (tmp_path / "a.txt").write_text("later\n")
    git("commit", "-q", "-am", "unrelated later commit")
    monkeypatch.setattr(scenario, "ROOT", tmp_path)
    ref = scenario.clean_ref({"base_branch": "test/s99-seed", "seed": "k9", "id": "S99"})
    assert git("rev-parse", ref) == clean_sha
