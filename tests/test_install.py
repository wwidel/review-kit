"""install.py and --uninstall, against a temporary HOME."""
import json
import os
import shutil
import subprocess
import sys

import pytest

from conftest import KIT, git, hook, load, records, run

SKILLS = sorted(p.name for p in (KIT / "skills").iterdir() if (p / "SKILL.md").exists())


def install(kit, *args, cwd=None):
    return subprocess.run([sys.executable, str(kit / "bin" / "install.py"), *map(str, args)],
                          cwd=cwd, capture_output=True, text=True)


def copy_kit(dest):
    shutil.copytree(KIT, dest, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__",
                                                             ".review", ".pytest_cache"))
    return dest


def settings(home):
    return home / ".claude" / "settings.json"


def snapshot(root):
    """Every path under root, with file contents and link targets: what a refusal must not change."""
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            out[str(p)] = "-> " + os.readlink(p)
        elif p.is_file():
            out[str(p)] = p.read_bytes()
        else:
            out[str(p)] = "dir"
    return out


def test_a_fresh_install(home, repo):
    r = install(KIT, "--repo", repo, cwd=repo)
    assert r.returncode == 0, r.stderr
    links = home / ".claude" / "skills"
    assert sorted(p.name for p in links.iterdir()) == SKILLS
    for name in SKILLS:
        assert (links / name).resolve() == (KIT / "skills" / name).resolve()
    post = json.loads(settings(home).read_text())["hooks"]["PostToolUse"]
    assert len(post) == 1 and "log_file_access.py" in post[0]["hooks"][0]["command"]
    cfg = json.loads(load("review").config_path(repo).read_text())
    assert cfg["kit"] == str(KIT) and cfg["summary"] == "REVIEW-SUMMARY.md"
    assert git(repo, "status", "--porcelain", "--ignored").stdout == ""


def test_a_rerun_leaves_settings_byte_identical_even_with_hooks_after_ours(home, repo):
    settings(home).parent.mkdir(parents=True)
    settings(home).write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo before"}]}]}}))
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    cfg = json.loads(settings(home).read_text())
    cfg["hooks"]["PostToolUse"].append(
        {"matcher": "Read", "hooks": [{"type": "command", "command": "echo after"}]})
    settings(home).write_text(json.dumps(cfg, indent=2) + "\n")
    before = settings(home).read_bytes()
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    assert settings(home).read_bytes() == before


def write_settings(home, post):
    settings(home).parent.mkdir(parents=True, exist_ok=True)
    settings(home).write_text(json.dumps({"hooks": {"PostToolUse": post}}, indent=2) + "\n")


def post_entries(home):
    return json.loads(settings(home).read_text())["hooks"]["PostToolUse"]


def ours_count(home):
    return sum("log_file_access.py" in h["command"] for e in post_entries(home) for h in e["hooks"])


def test_duplicate_entries_from_older_installs_collapse_to_one(home, repo):
    old = {"type": "command", "command": "python3 /old/kit/bin/log_file_access.py"}
    write_settings(home, [{"matcher": "Read", "hooks": [old]}, {"matcher": "Bash", "hooks": [old]}])
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    assert len(post_entries(home)) == 1 and ours_count(home) == 1


def test_an_entry_mixing_ours_with_the_users_keeps_theirs(home, repo):
    old = {"type": "command", "command": "python3 /old/kit/bin/log_file_access.py"}
    theirs = {"type": "command", "command": "echo mine"}
    write_settings(home, [{"matcher": "Read", "hooks": [theirs, old]}])
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    entries = post_entries(home)
    assert entries[0] == {"matcher": "Read", "hooks": [theirs]}
    assert ours_count(home) == 1


def test_the_users_empty_entry_and_extra_keys_on_our_hook_survive(home, repo):
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    entries = post_entries(home)
    entries[0]["hooks"][0]["timeout"] = 30
    entries.append({"matcher": "Write", "hooks": []})
    write_settings(home, entries)
    before = settings(home).read_bytes()
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    assert settings(home).read_bytes() == before


def test_a_symlinked_settings_file_stays_a_link_with_its_mode(home, repo, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"theme": "dark"}\n')
    real.chmod(0o600)
    settings(home).parent.mkdir(parents=True)
    settings(home).symlink_to(real)
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    assert settings(home).is_symlink() and os.readlink(settings(home)) == str(real)
    cfg = json.loads(real.read_text())
    assert cfg["theme"] == "dark" and cfg["hooks"]["PostToolUse"]
    assert real.stat().st_mode & 0o777 == 0o600


def test_existing_settings_are_merged_and_kept(home, repo):
    settings(home).parent.mkdir(parents=True)
    settings(home).write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]},
                                          "hooks": {"PostToolUse": [
                                              {"matcher": "Bash", "hooks": [
                                                  {"type": "command", "command": "echo x"}]}]}}))
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    cfg = json.loads(settings(home).read_text())
    assert cfg["model"] == "opus" and cfg["permissions"] == {"allow": ["Bash(ls)"]}
    cmds = [h["command"] for e in cfg["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert cmds[0] == "echo x" and sum("log_file_access.py" in c for c in cmds) == 1


@pytest.mark.parametrize("content", [
    "{not json", "[]", '"a string"', "null", '{"hooks": []}', '{"hooks": null}',
    '{"hooks": {"PostToolUse": {}}}', '{"hooks": {"PostToolUse": ["x"]}}',
    '{"hooks": {"PostToolUse": [{"hooks": "x"}]}}', '{"hooks": {"PostToolUse": [{"hooks": [1]}]}}'])
def test_a_malformed_settings_file_is_refused_and_nothing_is_written(home, repo, content):
    settings(home).parent.mkdir(parents=True)
    settings(home).write_text(content)
    before = snapshot(home)
    r = install(KIT, "--repo", repo, cwd=repo)
    assert r.returncode == 2
    assert "NOTHING HAS BEEN WRITTEN" in r.stderr
    assert snapshot(home) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_an_unreadable_settings_file_is_refused_cleanly(home, repo):
    settings(home).parent.mkdir(parents=True)
    settings(home).write_text("{}")
    settings(home).chmod(0)
    try:
        r = install(KIT, "--repo", repo, cwd=repo)
    finally:
        settings(home).chmod(0o600)
    assert r.returncode == 2 and "cannot be read" in r.stderr and "Traceback" not in r.stderr
    assert not (home / ".review-kit").exists()


def test_no_hook_ignores_a_malformed_settings_file(home, repo):
    settings(home).parent.mkdir(parents=True)
    settings(home).write_text("{not json")
    assert install(KIT, "--repo", repo, "--no-hook", cwd=repo).returncode == 0
    assert settings(home).read_text() == "{not json"


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_a_users_own_skill_of_the_same_name_is_refused_and_kept(home, repo, tmp_path, kind):
    links = home / ".claude" / "skills"
    links.mkdir(parents=True)
    mine = links / "review-code"
    if kind == "directory":
        mine.mkdir()
        (mine / "SKILL.md").write_text("mine\n")
    else:
        (tmp_path / "my-skill").mkdir()
        mine.symlink_to(tmp_path / "my-skill")
    before = snapshot(home)
    r = install(KIT, "--repo", repo, cwd=repo)
    assert r.returncode == 2 and "NOTHING HAS BEEN WRITTEN" in r.stderr
    assert snapshot(home) == before
    assert install(KIT, "--uninstall").returncode == 0
    assert snapshot(home) == before


@pytest.mark.parametrize("dangling", [False, True])
def test_a_kit_link_the_user_repointed_is_theirs(home, repo, tmp_path, dangling):
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    link = home / ".claude" / "skills" / "review-code"
    target = tmp_path / "mine" / "review-code"
    if not dangling:
        target.mkdir(parents=True)
    link.unlink()
    link.symlink_to(target)
    r = install(KIT, "--repo", repo, cwd=repo)
    assert r.returncode == 2 and str(link) in r.stderr
    assert os.readlink(link) == str(target)
    u = install(KIT, "--uninstall")
    assert "kept review-code" in u.stderr and os.readlink(link) == str(target)


def test_a_symlink_loop_is_refused_without_a_traceback(home, repo):
    links = home / ".claude" / "skills"
    links.mkdir(parents=True)
    (links / "review-code").symlink_to(links / "review-code")
    r = install(KIT, "--repo", repo, cwd=repo)
    assert r.returncode == 2 and "Traceback" not in r.stderr


def test_a_kit_skill_replaced_by_the_user_is_kept_and_reported_by_uninstall(home, repo):
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    link = home / ".claude" / "skills" / "review-code"
    link.unlink()
    link.mkdir()
    (link / "SKILL.md").write_text("mine now\n")
    u = install(KIT, "--uninstall")
    assert u.returncode == 0 and "kept review-code" in u.stderr
    assert (link / "SKILL.md").read_text() == "mine now\n"


def test_an_existing_summary_is_left_alone_and_a_free_name_claimed(home, repo):
    (repo / "REVIEW-SUMMARY.md").write_text("someone else's\n")
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    review = load("review")
    assert review.summary_name(repo) == "REVIEW-SUMMARY-1.md"
    assert (repo / "REVIEW-SUMMARY.md").read_text() == "someone else's\n"
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    assert review.summary_name(repo) == "REVIEW-SUMMARY-1.md"


def test_a_second_repository_leaves_the_first_untouched(home, repo, tmp_path):
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    first = snapshot(home / ".review-kit" / load("review").repo_slug(repo))
    second = tmp_path / "second"
    second.mkdir()
    assert install(KIT, "--repo", second, cwd=second).returncode == 0
    assert snapshot(home / ".review-kit" / load("review").repo_slug(repo)) == first
    post = json.loads(settings(home).read_text())["hooks"]["PostToolUse"]
    assert len(post) == 1


def test_a_moved_kit_reinstalls_and_uninstalls(home, repo, tmp_path):
    a = copy_kit(tmp_path / "kitA")
    assert install(a, "--repo", repo, cwd=repo).returncode == 0
    b = tmp_path / "kitB"
    a.rename(b)
    r = install(b, "--repo", repo, cwd=repo)
    assert r.returncode == 0, r.stderr
    links = home / ".claude" / "skills"
    for name in SKILLS:
        assert (links / name).resolve() == (b / "skills" / name).resolve()
    a2 = tmp_path / "kitA"
    b.rename(a2)  # dangling again
    u = install(a2, "--uninstall")
    assert u.returncode == 0 and "kept" not in u.stderr
    assert list(links.iterdir()) == []


def test_a_copied_kit_takes_over_links_to_the_recorded_old_copy(home, repo, tmp_path):
    a = copy_kit(tmp_path / "kitA")
    assert install(a, "--repo", repo, cwd=repo).returncode == 0
    b = copy_kit(tmp_path / "kitB")
    r = install(b, "--repo", repo, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert f"another checkout of this kit, {a}" in r.stdout
    for name in SKILLS:
        assert (home / ".claude" / "skills" / name).resolve() == (b / "skills" / name).resolve()


def test_a_kit_path_with_a_space_gives_a_working_hook(home, repo, tmp_path):
    kit = copy_kit(tmp_path / "my tools" / "kit")
    assert install(kit, "--repo", repo, cwd=repo).returncode == 0
    cmd = json.loads(settings(home).read_text())["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(repo / "a.py")},
                          "cwd": str(repo), "agent_id": "x"})
    # The command names `python3`; make that the interpreter running the tests.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run(cmd, shell=True, input=payload, cwd=repo, capture_output=True, text=True,
                       env=env)
    assert r.returncode == 0, r.stderr
    assert len(records(load("review").store_path(repo))) == 1


def test_uninstall_removes_only_the_kits_skills_and_keeps_hook_and_stores(home, repo):
    assert install(KIT, "--repo", repo, cwd=repo).returncode == 0
    links = home / ".claude" / "skills"
    (links / "my-own").mkdir()
    store = load("review").store_path(repo)
    (store / "ledger.jsonl").write_text("{}\n")
    hook_before = settings(home).read_bytes()
    u = install(KIT, "--uninstall")
    assert u.returncode == 0
    assert [p.name for p in links.iterdir()] == ["my-own"]
    assert settings(home).read_bytes() == hook_before
    assert (store / "ledger.jsonl").read_text() == "{}\n"
    owned = json.loads((home / ".review-kit" / "installed-skills.json").read_text())
    assert owned == {"skills": {}}
