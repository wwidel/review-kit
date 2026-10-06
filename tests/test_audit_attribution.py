"""Which access records may support a claim: decisions 3a and 3b of the test plan."""
from conftest import audit_case


def test_a_matching_read_supports_the_claim(tmp_path, repo):
    r = audit_case(tmp_path, repo, ["a.py"],
                   [{"tool": "Read", "agent": "x", "paths": [str(repo / "a.py")]}])
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_record_with_no_agent_id_supports_nothing(tmp_path, repo):
    # One named agent in the log and no --agent: the id-less record used to vouch for it.
    r = audit_case(tmp_path, repo, ["a.py"],
                   [{"tool": "Read", "agent": "x", "paths": [str(repo / "README.md")]},
                    {"tool": "Read", "agent": None, "paths": [str(repo / "a.py")]}])
    assert r.returncode == 1
    assert "UNSUPPORTED CLAIM" in r.stdout
    assert "1 record(s) with no agent id ignored" in r.stdout


def test_a_record_with_no_agent_id_supports_nothing_under_agent(tmp_path, repo):
    r = audit_case(tmp_path, repo, ["a.py"],
                   [{"tool": "Read", "paths": [str(repo / "a.py")]}], agent="x")
    assert r.returncode == 1


def test_only_the_named_agents_records_count(tmp_path, repo):
    recs = [{"tool": "Read", "agent": "x", "paths": [str(repo / "README.md")]},
            {"tool": "Read", "agent": "y", "paths": [str(repo / "a.py")]}]
    assert audit_case(tmp_path, repo, ["a.py"], recs, agent="x").returncode == 1
    assert audit_case(tmp_path, repo, ["a.py"], recs, agent="y").returncode == 0


def test_several_agents_without_agent_flag_is_refused(tmp_path, repo):
    recs = [{"tool": "Read", "agent": "x", "paths": [str(repo / "a.py")]},
            {"tool": "Read", "agent": "y", "paths": [str(repo / "a.py")]}]
    r = audit_case(tmp_path, repo, ["a.py"], recs)
    assert r.returncode != 0 and "distinct agents" in r.stderr


def test_write_edit_and_notebookedit_records_are_not_reads(tmp_path, repo):
    for tool in ("Write", "Edit", "NotebookEdit"):
        r = audit_case(tmp_path, repo, ["a.py"],
                       [{"tool": tool, "agent": "x", "paths": [str(repo / "a.py")]}])
        assert r.returncode == 1, tool


def test_a_record_from_before_the_run_supports_nothing(tmp_path, repo):
    r = audit_case(tmp_path, repo, ["a.py"],
                   [{"tool": "Read", "agent": "x", "paths": [str(repo / "a.py")],
                     "t": "2020-01-01T00:00:00+00:00"}])
    assert r.returncode == 1
    assert "1 earlier or undated record(s) ignored" in r.stdout


def test_an_undated_record_supports_nothing(tmp_path, repo):
    r = audit_case(tmp_path, repo, ["a.py"],
                   [{"tool": "Read", "agent": "x", "paths": [str(repo / "a.py")], "t": None}])
    assert r.returncode == 1
    assert "1 earlier or undated record(s) ignored" in r.stdout


def test_hook_output_feeds_audit_end_to_end(tmp_path, installed, repo):
    """A real payload through the real hook, then audit against the log it wrote."""
    from conftest import hook, records
    hook({"tool_name": "Bash", "tool_input": {"command": "cat -n m.py"},
          "cwd": str(repo / "src"), "agent_id": "x"}, cwd=repo / "src")
    recs = records(installed)
    assert recs
    r = audit_case(tmp_path, repo, ["src/m.py"], recs)
    assert r.returncode == 0, r.stdout + r.stderr


def test_list_agents_shows_read_paths_only_and_flags_unattributed(tmp_path, repo):
    from conftest import run
    log = tmp_path / "access.jsonl"
    import json as _json
    log.write_text("".join(_json.dumps(r) + "\n" for r in [
        {"t": "2026-01-01T00:00:00+00:00", "tool": "Read", "agent": "x",
         "paths": [str(repo / "a.py")]},
        {"t": "2026-01-01T00:00:00+00:00", "tool": "Write", "agent": "x",
         "paths": [str(repo / "src" / "m.py")]},
        {"t": "2026-01-01T00:00:00+00:00", "tool": "Read", "paths": [str(repo / "README.md")]}]))
    r = run("review.py", "audit", "--repo", repo, "--access-log", log, "--list-agents", cwd=repo)
    assert r.returncode == 0
    assert "x   2 call(s), 1 distinct path(s) read" in r.stdout
    assert "m.py" not in r.stdout
    assert "(no agent id — ignored by audit)" in r.stdout
