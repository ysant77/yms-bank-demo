"""Safety rules of scripts/scenario.py: nothing unsafe may reach git."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scenario.py"
spec = importlib.util.spec_from_file_location("scenario_script", SCRIPT)
scenario = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scenario  # dataclasses look their module up here
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


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
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


# Neutral ids: the parameter values must not appear in CI output or the report comment.
SPEC_CASES = [
    ("no-base", 'base_branch = "test/s98-seed-demo"\n', ""),
    ("no-twin-branch", 'twin_branch = "fix/{key_lower}-demo"\n', ""),
    (
        "bad-step-key",
        'steps = [ { restore = "notes.txt" } ]',
        'steps = [ { restore = "notes.txt", x = "y" } ]',
    ),
    (
        "replace-without-old",
        '{ replace = "notes.txt", old = "broken", new = "half fixed" }',
        '{ replace = "notes.txt", new = "half fixed" }',
    ),
    ("step-folder-magic", '{ restore = "notes.txt" }', '{ restore = ":/" }'),
    ("product-in-title", 'title = "Demo defect"', 'title = "Demo defect, Claude3 draft"'),
    (
        "version-in-pr-title",
        'pr_title = "{KEY}: fix the demo defect"',
        'pr_title = "{KEY}: fix from GPT4"',
    ),
    (
        "tool-in-commit",
        'commit = "fix(demo): revert the demo defect"',
        'commit = "fix(demo): revert via copilot"',
    ),
    (
        "trailer-in-commit",
        'commit = "fix(demo): revert the demo defect"',
        'commit = "fix(demo): co-authored-by someone"',
    ),
    ("product-as-twin", 'twin = "Nora"', 'twin = "Claude"'),
    (
        "product-in-branch",
        'twin_branch = "fix/{key_lower}-demo"',
        'twin_branch = "fix/{key_lower}-claude_code"',
    ),
    ("product-in-seed", 'seed = "k9_demo"', 'seed = "k9_chatgpt"'),
]


@pytest.mark.parametrize("old, new", [c[1:] for c in SPEC_CASES], ids=[c[0] for c in SPEC_CASES])
def test_incomplete_specs_and_tool_names_are_refused(demo_repo, old, new):
    assert old in SPEC
    (demo_repo / "scenarios" / "S98.toml").write_text(SPEC.replace(old, new))
    with pytest.raises(scenario.ScenarioError):
        scenario.load("S98")


ORDINARY = ["Gemini", "Mistral", "Phi", "Muse", "generated with the wrong date"]


@pytest.mark.parametrize("text", ORDINARY, ids=[f"word-{n}" for n in range(len(ORDINARY))])
def test_ordinary_words_are_not_refused(text):
    scenario.check_no_tool_names({"title": text})


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


def test_a_failed_step_keeps_an_existing_twin_branch_at_its_tip(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    tip = git(demo_repo, "rev-parse", "fix/bank-9-demo").strip()
    git(demo_repo, "switch", "-q", "main")
    spec = (demo_repo / "scenarios" / "S98.toml").read_text()
    (demo_repo / "scenarios" / "S98.toml").write_text(
        spec.replace('old = "broken"', 'old = "absent"')
    )
    git(demo_repo, "commit", "-q", "-am", "chore: break the step")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    assert git(demo_repo, "rev-parse", "fix/bank-9-demo").strip() == tip
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert git(demo_repo, "status", "--porcelain") == ""


def break_step(repo, old='old = "broken"', new='old = "absent"'):
    spec = (repo / "scenarios" / "S98.toml").read_text()
    (repo / "scenarios" / "S98.toml").write_text(spec.replace(old, new))
    git(repo, "commit", "-q", "-am", "chore: break the step")


def test_an_ignored_file_at_a_test_destination_is_not_overwritten(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    git(demo_repo, "switch", "-q", "main")
    (demo_repo / ".gitignore").write_text("checks/\n")
    git(demo_repo, "add", ".gitignore")
    git(demo_repo, "commit", "-q", "-m", "chore: ignore checks")
    git(demo_repo, "switch", "-q", "test/s98-seed-demo")
    git(demo_repo, "merge", "-q", "--no-edit", "main")
    git(demo_repo, "switch", "-q", "main")
    (demo_repo / "checks").mkdir()
    (demo_repo / "checks" / "bank9.txt").write_text("ignored user file\n")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 0
    assert (demo_repo / "checks" / "bank9.txt").read_text() == "ignored user file\n"
    assert git(demo_repo, "show", "fix/bank-9-demo:checks/bank9.txt") == "check for BANK-9\n"
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "main"


def test_a_step_path_that_is_a_folder_is_refused(demo_repo):
    (demo_repo / "folder").mkdir()
    (demo_repo / "folder" / "keep.txt").write_text("x\n")
    git(demo_repo, "add", "folder")
    git(demo_repo, "commit", "-q", "-m", "chore: add a folder")
    with pytest.raises(scenario.ScenarioError):
        scenario.safe_path("folder")


@pytest.mark.parametrize(
    "path",
    [":/", ":(glob)*", ".", "./", "notes/"],
    ids=["top", "glob", "dot", "dot-slash", "slash"],
)
def test_pathspec_magic_and_the_root_are_refused(demo_repo, path):
    with pytest.raises(scenario.ScenarioError):
        scenario.safe_path(path)


def test_an_interrupt_during_a_step_still_rolls_back(demo_repo, monkeypatch):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    git(demo_repo, "switch", "-q", "main")
    real = scenario.apply_step

    def interrupted(step, *args):
        real(step, *args)
        raise KeyboardInterrupt

    monkeypatch.setattr(scenario, "apply_step", interrupted)
    with pytest.raises(KeyboardInterrupt):
        scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"])
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert git(demo_repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(demo_repo, "branch", "--list", "fix/bank-9-demo") == ""


def test_a_seed_that_is_not_utf8_is_refused_and_rolled_back(demo_repo):
    (demo_repo / "seeds" / "k9_demo.patch").write_bytes(b"\xff\xfe not a patch")
    git(demo_repo, "commit", "-q", "-am", "chore: bad seed")
    assert scenario.main(["base", "S98", "--from", "main"]) == 2
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert git(demo_repo, "branch", "--list", "test/s98-seed-demo") == ""


def test_a_failed_step_from_a_detached_head_returns_there(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    git(demo_repo, "switch", "-q", "main")
    break_step(demo_repo)
    git(demo_repo, "switch", "-q", "--detach", "main")
    start = git(demo_repo, "rev-parse", "HEAD").strip()
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    assert git(demo_repo, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert git(demo_repo, "rev-parse", "HEAD").strip() == start


def test_a_failed_branch_undo_says_so(demo_repo, monkeypatch, capsys):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    break_step(demo_repo)
    real = scenario.git

    def failing_delete(*args):
        if args[:2] == ("update-ref", "-d"):
            return subprocess.CompletedProcess(args, 1, "", "fatal: simulated failure")
        return real(*args)

    monkeypatch.setattr(scenario, "git", failing_delete)
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    err = capsys.readouterr().err
    assert "rollback incomplete: could not delete fix/bank-9-demo, which this run created" in err


def test_reads_use_the_branch_not_a_tag_with_the_same_name(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    seeded = git(demo_repo, "rev-parse", "refs/heads/test/s98-seed-demo").strip()
    git(demo_repo, "tag", "test/s98-seed-demo", "main")
    git(demo_repo, "switch", "-q", "main")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 0
    assert git(demo_repo, "rev-parse", "fix/bank-9-demo~1").strip() == seeded


def checkout_state(repo):
    """Everything about the user's checkout that a run must not change."""
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "symbolic-ref", "-q", "HEAD", check=False),
        git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored"),
        git(repo, "diff"),
        git(repo, "diff", "--cached"),
    )


def ignore(repo, pattern):
    (repo / ".gitignore").write_text(pattern + "\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "chore: ignore a path")


def test_runs_never_touch_the_users_checkout(demo_repo):
    (demo_repo / "notes.txt").write_text("my local edit\n")
    git(demo_repo, "update-index", "--assume-unchanged", "notes.txt")
    git(demo_repo, "config", "status.showUntrackedFiles", "no")
    (demo_repo / "mine.txt").write_text("untracked user file\n")
    before = checkout_state(demo_repo)
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 0
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    assert checkout_state(demo_repo) == before
    assert (demo_repo / "notes.txt").read_text() == "my local edit\n"
    assert (demo_repo / "mine.txt").read_text() == "untracked user file\n"
    assert git(demo_repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def seed_adding(repo, path):
    """A seed patch that creates ``path``."""
    (repo / path).write_text("seeded file\n")
    git(repo, "add", "-f", "-N", path)
    patch = git(repo, "diff", "--", path)
    git(repo, "rm", "-q", "--cached", path)
    (repo / path).unlink()
    (repo / "seeds" / "k9_demo.patch").write_text(patch)
    git(repo, "commit", "-q", "-am", "chore: a seed that adds a file")


def test_base_keeps_an_ignored_user_file_where_the_seed_adds_one(demo_repo):
    ignore(demo_repo, "extra.txt")
    seed_adding(demo_repo, "extra.txt")
    (demo_repo / "extra.txt").write_text("ignored user file\n")
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert (demo_repo / "extra.txt").read_text() == "ignored user file\n"
    files = git(demo_repo, "show", "--name-only", "--format=", "test/s98-seed-demo").split()
    assert files == ["extra.txt"]


def test_revert_seed_keeps_an_ignored_user_file(demo_repo):
    ignore(demo_repo, "extra.txt")
    seed_adding(demo_repo, "extra.txt")
    spec = (demo_repo / "scenarios" / "S98.toml").read_text()
    (demo_repo / "scenarios" / "S98.toml").write_text(
        spec.replace('steps = [ { restore = "notes.txt" } ]', "steps = [ { revert_seed = true } ]")
    )
    git(demo_repo, "commit", "-q", "-am", "chore: revert the seed in v2")
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    (demo_repo / "extra.txt").write_text("ignored user file\n")
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    assert (demo_repo / "extra.txt").read_text() == "ignored user file\n"
    assert "extra.txt" not in git(demo_repo, "ls-tree", "-r", "--name-only", "fix/bank-9-demo")


def test_replace_never_edits_an_ignored_user_file(demo_repo, capsys):
    ignore(demo_repo, "local.txt")
    spec = (demo_repo / "scenarios" / "S98.toml").read_text()
    (demo_repo / "scenarios" / "S98.toml").write_text(
        spec.replace(
            '{ replace = "notes.txt", old = "broken"', '{ replace = "local.txt", old = "broken"'
        )
    )
    git(demo_repo, "commit", "-q", "-am", "chore: replace an untracked file")
    (demo_repo / "local.txt").write_text("broken but mine\n")
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    assert "replace: local.txt does not exist" in capsys.readouterr().err
    assert (demo_repo / "local.txt").read_text() == "broken but mine\n"
    assert git(demo_repo, "branch", "--list", "fix/bank-9-demo") == ""


def test_restore_never_writes_over_an_ignored_user_file(demo_repo):
    ignore(demo_repo, "notes.txt.orig")
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    (demo_repo / "notes.txt").write_text("user edit of a tracked file\n")
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    assert (demo_repo / "notes.txt").read_text() == "user edit of a tracked file\n"
    assert git(demo_repo, "show", "fix/bank-9-demo:notes.txt") == "working\n"


def test_a_non_normalised_path_and_a_tracked_symlink_are_refused(demo_repo):
    with pytest.raises(scenario.ScenarioError):
        scenario.safe_path("seeds/../notes.txt")
    (demo_repo / "link.txt").symlink_to("notes.txt")
    git(demo_repo, "add", "link.txt")
    git(demo_repo, "commit", "-q", "-m", "chore: a tracked symlink")
    with pytest.raises(scenario.ScenarioError):
        scenario.safe_path("link.txt", write=True)


def test_a_tool_name_in_the_key_is_refused(demo_repo, capsys):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v1", "--key", "CLAUDE-3"]) == 2
    assert "refused" in capsys.readouterr().err
    assert git(demo_repo, "branch", "--list", "fix/*") == ""


def test_an_interrupt_right_after_the_switch_leaves_nothing(demo_repo, monkeypatch):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    real = scenario.run

    def interrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        if args[:2] == ("git", "switch"):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(scenario, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"])
    assert git(demo_repo, "branch", "--list", "fix/bank-9-demo") == ""
    assert git(demo_repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def outside_commit(repo, branch, text):
    """A commit made on ``branch`` by someone else (one file added), without any worktree."""
    tip = git(repo, "rev-parse", f"refs/heads/{branch}").strip()
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=text, text=True,
        capture_output=True, check=True,
    ).stdout.strip()  # fmt: skip
    index = {**os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "outside-index")}
    entry = f"100644,{blob},outside.txt"
    for args in (["read-tree", tip], ["update-index", "--add", "--cacheinfo", entry]):
        subprocess.run(["git", *args], cwd=repo, env=index, check=True, capture_output=True)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=repo, env=index, text=True, capture_output=True, check=True
    ).stdout.strip()
    (repo / ".git" / "outside-index").unlink()
    new = git(repo, "commit-tree", tree, "-p", tip, "-m", "other run").strip()
    git(repo, "update-ref", f"refs/heads/{branch}", new, tip)
    return new


def test_a_failed_run_never_rewinds_a_commit_it_did_not_make(demo_repo, monkeypatch):
    """Another run finishes on the twin between this run's start and its switch; this run then
    fails. The other run's commit must stay on the branch."""
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    real = scenario.switch_into
    other = {}

    def late_switch(state, start_new):
        other["sha"] = outside_commit(demo_repo, state.branch, "other run's change\n")
        real(state, start_new)

    monkeypatch.setattr(scenario, "switch_into", late_switch)
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 2  # nothing to commit
    assert git(demo_repo, "rev-parse", "fix/bank-9-demo").strip() == other["sha"]


def test_a_branch_created_by_someone_else_is_never_deleted(demo_repo, monkeypatch):
    real = scenario.switch_into
    other = {}

    def raced(state, start_new):
        git(demo_repo, "branch", state.branch, "main")
        other["sha"] = git(demo_repo, "rev-parse", state.branch).strip()
        real(state, start_new)

    monkeypatch.setattr(scenario, "switch_into", raced)
    assert scenario.main(["base", "S98", "--from", "main"]) == 2  # switch -c: already exists
    assert git(demo_repo, "rev-parse", "test/s98-seed-demo").strip() == other["sha"]


def test_a_twin_checked_out_by_the_user_is_never_rewound(demo_repo):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    git(demo_repo, "switch", "-q", "fix/bank-9-demo")
    (demo_repo / "mine.txt").write_text("user work\n")
    git(demo_repo, "add", "mine.txt")
    git(demo_repo, "commit", "-q", "-m", "chore: user work")
    tip = git(demo_repo, "rev-parse", "HEAD").strip()
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2  # checked out here
    assert git(demo_repo, "rev-parse", "fix/bank-9-demo").strip() == tip
    assert git(demo_repo, "symbolic-ref", "--short", "HEAD").strip() == "fix/bank-9-demo"


def test_a_live_lock_makes_a_second_run_refuse(demo_repo, capsys):
    folder = demo_repo / ".git" / "scenario-locks"
    folder.mkdir()
    (folder / "test__s98-seed-demo.lock").write_text(f"{os.getpid()} now\n")
    assert scenario.main(["base", "S98", "--from", "main"]) == 2
    assert "another run holds test/s98-seed-demo" in capsys.readouterr().err
    (folder / "test__s98-seed-demo.lock").write_text("999999 gone\n")  # a dead holder
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert list(folder.iterdir()) == []


def test_git_location_variables_from_the_caller_are_ignored(demo_repo, monkeypatch):
    index = demo_repo / ".git" / "index"
    before = index.read_bytes()
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    monkeypatch.setenv("GIT_DIR", str(demo_repo / ".git"))
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 0
    assert index.read_bytes() == before


def test_sigterm_rolls_back_like_ctrl_c(demo_repo, monkeypatch):
    import signal

    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    real = scenario.apply_step

    def terminated(*args):
        result = real(*args)
        os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(scenario, "apply_step", terminated)
    with pytest.raises(KeyboardInterrupt):
        scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"])
    assert git(demo_repo, "branch", "--list", "fix/bank-9-demo") == ""
    assert git(demo_repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_a_leftover_temporary_worktree_names_the_command(demo_repo, tmp_path, capsys):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    left = tmp_path / "scenario-run-old" / "tree"
    git(demo_repo, "worktree", "add", "-q", str(left), "fix/bank-9-demo")
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 2
    assert f"git worktree remove --force {left}" in capsys.readouterr().err


def test_an_interrupt_after_a_finished_run_keeps_its_commit(demo_repo, monkeypatch):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0

    def interrupted_cleanup(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(scenario.shutil, "rmtree", interrupted_cleanup)
    with pytest.raises(KeyboardInterrupt):
        scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"])
    monkeypatch.undo()
    assert "half fixed" in git(demo_repo, "show", "fix/bank-9-demo:notes.txt")


def test_add_test_never_overwrites_a_tracked_file_with_other_content(demo_repo, capsys):
    assert scenario.main(["base", "S98", "--from", "main"]) == 0
    assert scenario.main(["attempt", "S98", "v1", "--key", "BANK-9"]) == 0
    # Same content again is allowed (a later attempt re-adding its test).
    spec = (demo_repo / "scenarios" / "S98.toml").read_text()
    readd = '{ add_test = "scenarios/S98/check.txt.tmpl", dest = "checks/{token}.txt" }'
    v2 = 'steps = [ { restore = "notes.txt" } ]'
    again = spec.replace(v2, f'steps = [ {{ restore = "notes.txt" }}, {readd} ]')
    (demo_repo / "scenarios" / "S98.toml").write_text(again)
    git(demo_repo, "commit", "-q", "-am", "chore: v2 re-adds the test")
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 0
    # Other content is refused.
    (demo_repo / "scenarios" / "S98" / "check.txt.tmpl").write_text("different {KEY}\n")
    git(demo_repo, "commit", "-q", "-am", "chore: change the template")
    assert scenario.main(["attempt", "S98", "v2", "--key", "BANK-9"]) == 2
    assert "exists with other content" in capsys.readouterr().err
