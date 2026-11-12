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


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/etc/passwd", ".git/config", "tests/../../x", ".GIT/config", ".Git/hooks/x",
     ".github/workflows/ci.yml", ".GitHub/Workflows/x.yml", ".githooks/pre-commit", ""],
)  # fmt: skip
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


def test_the_twin_branch_must_name_the_issue_key():
    spec = {"base_branch": "test/s99-seed", "twin_branch": "fix/some-other-work"}
    attempt = {"steps": []}
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


def test_unsafe_refs_are_refused():
    for ref in ["--upload-pack=x", "main..other", "a b", "-x"]:
        with pytest.raises(scenario.ScenarioError):
            scenario.check_ref(ref, "--from")


def test_commit_and_pr_text_say_only_scripted_or_live():
    spec = {"id": "S04", "title": "Double spend", "base_branch": "test/s04-seed"}
    assert scenario.commit_message("fix(ledger): x y z", "Raj", "live") == [
        "fix(ledger): x y z",
        "Twin change: Raj, live.",
    ]
    body = scenario.pr_body(spec, "BANK-4", "Raj", "scripted")
    assert body.startswith("Twin change: scripted (Raj). Scenario S04: Double spend.")
    with pytest.raises(scenario.ScenarioError):
        scenario.commit_message("fix: x", "Raj", "some-model")


# A small throw-away repository with one seed and one scenario.
SPEC = """
id = "S98"
title = "Demo defect"
seed = "k9_demo"
base_branch = "test/s98-seed-demo"
twin_branch = "fix/{key_lower}-demo"
pr_title = "{KEY}: fix the demo defect"
twin = "Raj"
author = "scripted"

[[attempts]]
name = "v1"
commit = "fix(demo): first try at the demo defect"
claim = "fixed"
steps = [
  { replace = "notes.txt", old = "broken", new = "half fixed" },
  { add_test = "scenarios/S98/check.txt.tmpl", dest = "checks/{token}.txt" },
]

[[attempts]]
name = "v2"
twin = "Nora"
commit = "fix(demo): revert the demo defect"
claim = "fixed"
steps = [ { restore = "notes.txt" } ]
"""


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def demo_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "notes.txt").write_text("working\n")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "chore: start")
    (repo / "notes.txt").write_text("broken\n")
    patch = git(repo, "diff")
    git(repo, "checkout", "-q", "--", "notes.txt")
    (repo / "seeds").mkdir()
    (repo / "seeds" / "k9_demo.patch").write_text(patch)
    (repo / "scenarios" / "S98").mkdir(parents=True)
    (repo / "scenarios" / "S98.toml").write_text(SPEC)
    (repo / "scenarios" / "S98" / "check.txt.tmpl").write_text("check for {KEY}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: add the demo scenario")
    monkeypatch.setattr(scenario, "ROOT", repo)
    return repo


def test_a_live_model_name_never_reaches_git(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert (
        scenario.main(
            ["attempt", "S98", "v1", "--key", "BANK-9", "--author", "live", "--model", "SECRET-X"]
        )
        == 0
    )
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    history = git(demo_repo, "log", "--all", "-p", "--format=%H%n%B")
    assert "SECRET-X" not in history
    assert git(demo_repo, "status", "--porcelain", "--ignored") == ""
    records = list((demo_repo / ".git" / "scenario-runs").glob("S98-v1-*.json"))
    assert len(records) == 1 and "SECRET-X" in records[0].read_text()
    bodies = git(demo_repo, "log", "--format=%B", "fix/bank-9-demo")
    assert "Twin change: Nora, scripted." in bodies and "Twin change: Raj, live." in bodies
    # The v1 commit holds only the touched paths.
    files = git(demo_repo, "show", "--name-only", "--format=", "fix/bank-9-demo~1").split()
    assert sorted(files) == ["checks/bank9.txt", "notes.txt"]
    assert (demo_repo / "notes.txt").read_text() == "working\n"


def test_an_existing_twin_must_descend_from_the_base(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    git(demo_repo, "switch", "-q", "-c", "fix/bank-9-demo", "main")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2


def test_a_malformed_scenario_field_is_refused(demo_repo):
    path = demo_repo / "scenarios" / "S98.toml"
    bad = [
        ('twin = "Raj"', 'twin = "Raj\\nCo-Authored-By: x"'),
        ('id = "S98"', 'id = "S97"'),
        ('seed = "k9_demo"', 'seed = "../k9"'),
        ('commit = "fix(demo): first try at the demo defect"', 'commit = "oops"'),
    ]
    for old, new in bad:
        path.write_text(SPEC.replace(old, new))
        with pytest.raises(scenario.ScenarioError):
            scenario.load("S98")


def test_a_missing_scenario_or_a_git_failure_returns_2(demo_repo, capsys):
    assert scenario.main(["base", "S97"]) == 2
    assert scenario.main(["base", "S98", "--from", "no-such-ref"]) == 2
    assert "refused" in capsys.readouterr().err


def test_a_renaming_seed_is_committed_completely(demo_repo):
    """A rename in a seed patch stages both the deletion and the new file."""
    git(demo_repo, "mv", "notes.txt", "renamed.txt")
    patch = git(demo_repo, "diff", "--cached", "-M")
    git(demo_repo, "reset", "-q", "--hard")
    (demo_repo / "seeds" / "k9_demo.patch").write_text(patch)
    git(demo_repo, "commit", "-q", "-am", "chore: renaming seed")
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert git(demo_repo, "status", "--porcelain") == ""
    files = git(demo_repo, "show", "--name-status", "--format=", "test/s98-seed-demo").split()
    assert files[0].startswith("R") and files[1:] == ["notes.txt", "renamed.txt"]


@pytest.mark.parametrize(
    "old, new",
    [
        ('base_branch = "test/s98-seed-demo"\n', ""),
        ('twin_branch = "fix/{key_lower}-demo"\n', ""),
        ('steps = [ { restore = "notes.txt" } ]', "commit_x = 1"),
        ('title = "Demo defect"', 'title = "Demo defect written with Claude"'),
        ('pr_title = "{KEY}: fix the demo defect"', 'pr_title = "{KEY}: fix from GPT-4o"'),
        (
            'commit = "fix(demo): revert the demo defect"',
            'commit = "fix(demo): revert via copilot"',
        ),
        ('twin = "Nora"', 'twin = "Gemini"'),
    ],
)
def test_incomplete_specs_and_tool_names_are_refused(demo_repo, old, new):
    (demo_repo / "scenarios" / "S98.toml").write_text(SPEC.replace(old, new))
    with pytest.raises(scenario.ScenarioError):
        scenario.load("S98")


def test_the_key_must_match_on_a_token_boundary():
    spec = {"base_branch": "test/s99-seed", "twin_branch": "fix/bank-31-x"}
    with pytest.raises(scenario.ScenarioError):
        scenario.validate_attempt(spec, {"steps": []}, scenario.tokens("BANK-3"))
    spec["twin_branch"] = "fix/bank-3-x"
    assert scenario.validate_attempt(spec, {"steps": []}, scenario.tokens("BANK-3"))


def test_a_bad_from_says_why(demo_repo, capsys):
    assert scenario.main(["base", "S98", "--from", "no-such-ref"]) == 2
    assert "'no-such-ref' does not name a commit" in capsys.readouterr().err


def test_a_failed_step_restores_the_starting_branch(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    git(demo_repo, "switch", "-q", "main")
    spec = (demo_repo / "scenarios" / "S98.toml").read_text()
    (demo_repo / "scenarios" / "S98.toml").write_text(
        spec.replace('old = "broken"', 'old = "absent"')
    )
    git(demo_repo, "commit", "-q", "-am", "chore: break the step")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert git(demo_repo, "status", "--porcelain") == ""
    assert git(demo_repo, "branch", "--list", "fix/bank-9-demo") == ""


def test_push_uses_an_explicit_refspec(demo_repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(demo_repo, "remote", "add", "origin", str(remote))
    # A tag with the branch's name must not capture the push.
    git(demo_repo, "tag", "test/s98-seed-demo")
    assert scenario.main(["base", "S98", "--from", "main", "--push"]) == 0
    heads = git(remote, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert "refs/heads/test/s98-seed-demo" in heads.split()
