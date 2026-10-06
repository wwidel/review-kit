"""Where the hook writes, and what a record holds.

Claude Code runs the hook in the session's current directory. Before the walk-up, a `cd src`
switched logging off for every read that followed.
"""
import datetime
import io
import json

from conftest import git, hook, load, records, run


def read_payload(path, cwd, agent="agent-1"):
    return {"tool_name": "Read", "tool_input": {"file_path": str(path)}, "cwd": str(cwd),
            "agent_id": agent, "session_id": "session-1"}


def test_logs_from_the_repository_root(installed, repo):
    r = hook(read_payload(repo / "a.py", repo), cwd=repo)
    assert r.returncode == 0
    assert [x["paths"] for x in records(installed)] == [[str(repo / "a.py")]]


def test_keeps_logging_after_a_cd_into_a_subdirectory(installed, repo):
    hook(read_payload(repo / "src" / "m.py", repo / "src"), cwd=repo / "src")
    assert [x["paths"] for x in records(installed)] == [[str(repo / "src" / "m.py")]]


def test_the_payload_cwd_decides_not_the_hook_process_directory(installed, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hook(read_payload(repo / "a.py", repo), cwd=elsewhere)
    assert len(records(installed)) == 1


def test_writes_nothing_outside_an_installed_repository(installed, repo, tmp_path, home):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("x = 1\n")
    before = sorted(p for p in home.rglob("*"))
    hook(read_payload(other / "x.py", other), cwd=other)
    assert records(installed) == []
    assert sorted(p for p in home.rglob("*")) == before
    assert list(other.iterdir()) == [other / "x.py"]


def test_creates_nothing_inside_the_repository(installed, repo):
    hook(read_payload(repo / "a.py", repo), cwd=repo)
    hook({"tool_name": "Bash", "tool_input": {"command": "cd src && cat m.py"},
          "cwd": str(repo), "agent_id": "a"}, cwd=repo)
    status = git(repo, "status", "--porcelain", "--ignored").stdout
    assert status == ""


def test_relative_paths_resolve_against_the_payload_cwd(installed, repo, tmp_path):
    # The hook process runs somewhere else, so only the payload can say where `m.py` is.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hook(read_payload("m.py", repo / "src"), cwd=elsewhere)
    assert [x["paths"] for x in records(installed)] == [[str(repo / "src" / "m.py")]]


def test_bash_after_cd_resolves_names_against_the_payload_cwd(installed, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hook({"tool_name": "Bash", "tool_input": {"command": "cat -n m.py"},
          "cwd": str(repo / "src"), "agent_id": "a"}, cwd=elsewhere)
    (rec,) = records(installed)
    assert rec["paths"] == [str(repo / "src" / "m.py")]
    assert rec["cwd"] == str(repo / "src")
    assert rec["cmd"] == "cat -n m.py"
    assert rec["reads"] is True


def test_without_a_payload_cwd_the_hook_process_directory_is_used(installed, repo):
    p = read_payload("m.py", repo)
    del p["cwd"]
    hook(p, cwd=repo / "src")
    assert [x["paths"] for x in records(installed)] == [[str(repo / "src" / "m.py")]]


def test_a_relative_path_that_is_not_a_file_is_still_made_absolute(installed, repo):
    hook(read_payload("missing/x.py", repo / "src"), cwd=repo)
    assert [x["paths"] for x in records(installed)] == [[str(repo / "src" / "missing" / "x.py")]]


def test_payload_outside_while_the_hook_runs_inside_writes_nothing(installed, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("x = 1\n")
    hook(read_payload(other / "x.py", other), cwd=repo)
    assert records(installed) == []


def test_the_nearest_store_wins(installed, repo):
    inner = repo / "vendor" / "inner"
    inner.mkdir(parents=True)
    (inner / "s.py").write_text("s = 1\n")
    git(inner, "init", "-q")
    r = run("install.py", "--repo", inner, "--no-hook", cwd=inner)
    assert r.returncode == 0, r.stderr
    inner_store = load("review").store_path(inner)
    hook(read_payload(inner / "s.py", inner), cwd=inner)
    assert records(installed) == []
    assert [x["paths"] for x in records(inner_store)] == [[str(inner / "s.py")]]


def test_an_uninstalled_checkout_inside_an_installed_one_logs_to_the_outer_store(
        installed, repo):
    # Documented in store_for: anything below an installed repository is inside the tree
    # under review, a nested checkout included.
    inner = repo / "vendor" / "other"
    inner.mkdir(parents=True)
    (inner / "s.py").write_text("s = 1\n")
    git(inner, "init", "-q")
    hook(read_payload(inner / "s.py", inner), cwd=inner)
    assert [x["paths"] for x in records(installed)] == [[str(inner / "s.py")]]


def test_a_symlinked_subdirectory_still_finds_the_repository(installed, repo, tmp_path):
    data = tmp_path / "mnt" / "data"
    data.mkdir(parents=True)
    (data / "d.py").write_text("d = 1\n")
    (repo / "data").symlink_to(data)
    hook({"tool_name": "Bash", "tool_input": {"command": "cat d.py"},
          "cwd": str(repo / "data"), "agent_id": "a"}, cwd=repo / "data")
    assert len(records(installed)) == 1


def test_write_and_edit_are_logged(installed, repo):
    for tool in ("Write", "Edit"):
        hook({"tool_name": tool, "tool_input": {"file_path": str(repo / "a.py")},
              "cwd": str(repo), "agent_id": "a"}, cwd=repo)
    assert [x["tool"] for x in records(installed)] == ["Write", "Edit"]


def test_review_access_root_overrides_where_the_walk_starts(installed, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    hook(read_payload(repo / "a.py", other), cwd=other,
         env={"REVIEW_ACCESS_ROOT": str(repo / "src")})
    assert len(records(installed)) == 1


def test_record_format(installed, repo):
    hook(read_payload(repo / "a.py", repo), cwd=repo)
    (rec,) = records(installed)
    assert set(rec) == {"t", "tool", "agent", "paths"}
    assert rec["tool"] == "Read" and rec["agent"] == "agent-1"
    t = datetime.datetime.fromisoformat(rec["t"])
    assert t.utcoffset() == datetime.timedelta(0)
    assert abs((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()) < 60


def test_agent_falls_back_to_session_id(installed, repo):
    p = read_payload(repo / "a.py", repo)
    del p["agent_id"]
    hook(p, cwd=repo)
    assert records(installed)[0]["agent"] == "session-1"


def test_non_reading_bash_is_not_logged(installed, repo):
    hook({"tool_name": "Bash", "tool_input": {"command": "ls a.py"}, "cwd": str(repo),
          "agent_id": "a"}, cwd=repo)
    assert records(installed) == []


def test_garbage_on_stdin_is_ignored_and_never_fails(installed, repo):
    for junk in ("", "not json", "[1, 2]", '{"tool_name": 5}'):
        r = run("log_file_access.py", cwd=repo, stdin=junk)
        assert r.returncode == 0
    assert records(installed) == []


def _run_main(mod, monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    mod.main()


def test_an_exception_is_written_to_access_errors(installed, repo, monkeypatch):
    mod = load("log_file_access")

    def boom(*a, **k):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(mod, "paths_from", boom)
    _run_main(mod, monkeypatch, read_payload(repo / "a.py", repo))
    assert records(installed) == []
    assert "RuntimeError: parser exploded" in (installed / "access.errors").read_text()


def test_more_paths_than_max_are_cut_and_marked(installed, repo, monkeypatch):
    for i in range(5):
        (repo / f"f{i}.py").write_text("x\n")
    mod = load("log_file_access")
    monkeypatch.setattr(mod, "MAX_PATHS", 2)
    _run_main(mod, monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "cat f*.py"},
                                  "cwd": str(repo), "agent_id": "a"})
    (rec,) = records(installed)
    assert len(rec["paths"]) == 2 and rec["truncated"] is True
