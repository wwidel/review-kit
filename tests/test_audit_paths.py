"""How audit matches a declared path to the log, including reads through symlinks."""
import pytest

from conftest import audit_case, hook, records


def read(repo, *paths, agent="x"):
    return {"tool": "Read", "agent": agent, "paths": [str(p) for p in paths]}


@pytest.mark.parametrize("spelling", ["a.py", "./a.py", "src/../a.py", "{repo}/a.py"])
def test_every_spelling_of_a_read_file_is_supported(tmp_path, repo, spelling):
    r = audit_case(tmp_path, repo, [spelling.format(repo=repo)], [read(repo, repo / "a.py")])
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("spelling", ["a.py", "./a.py", "{repo}/a.py"])
def test_every_spelling_of_an_unread_file_is_unsupported(tmp_path, repo, spelling):
    r = audit_case(tmp_path, repo, [spelling.format(repo=repo)],
                   [read(repo, repo / "README.md")])
    assert r.returncode == 1 and "UNSUPPORTED CLAIM" in r.stdout


def test_a_relative_path_outside_the_repository_is_outside(tmp_path, repo):
    r = audit_case(tmp_path, repo, ["../kit/FINDINGS.md"], [read(repo, repo / "README.md")])
    assert r.returncode == 0
    assert "1 declared path(s) lie outside" in r.stderr


def test_a_sibling_sharing_the_repositorys_prefix_is_outside(tmp_path, repo):
    sibling = repo.parent / (repo.name + "2") / "z.md"
    r = audit_case(tmp_path, repo, [str(sibling)], [read(repo, repo / "README.md")])
    assert r.returncode == 0
    assert "1 declared path(s) lie outside" in r.stderr


def test_a_read_through_a_symlinked_directory_is_supported(tmp_path, repo):
    data = tmp_path / "mnt" / "data"
    data.mkdir(parents=True)
    (data / "d.py").write_text("d = 1\n")
    (repo / "data").symlink_to(data)
    # The hook stores the resolved path; the reviewer declares the one it used.
    r = audit_case(tmp_path, repo, ["data/d.py"], [read(repo, data / "d.py")])
    assert r.returncode == 0, r.stdout + r.stderr


def test_symlinked_directory_end_to_end_through_the_hook(tmp_path, installed, repo):
    data = tmp_path / "mnt" / "data"
    data.mkdir(parents=True)
    (data / "d.py").write_text("d = 1\n")
    (repo / "data").symlink_to(data)
    hook({"tool_name": "Bash", "tool_input": {"command": "cat d.py"},
          "cwd": str(repo / "data"), "agent_id": "x"}, cwd=repo / "data")
    r = audit_case(tmp_path, repo, ["data/d.py"], records(installed))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_fabricated_claim_is_not_rescued_by_a_same_named_file_elsewhere(tmp_path, repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "README.md").write_text("x\n")
    (repo / "src" / "README.md").write_text("y\n")
    rec = {"tool": "Bash", "agent": "x", "paths": [str(repo / "docs" / "README.md")],
           "cmd": "cat docs/README.md", "cwd": str(repo), "reads": True}
    r = audit_case(tmp_path, repo, ["src/README.md"], [rec])
    assert r.returncode == 1 and "UNSUPPORTED CLAIM" in r.stdout


def test_a_file_named_in_an_unresolved_reading_command_is_unresolved(tmp_path, repo):
    rec = {"tool": "Bash", "agent": "x", "paths": [], "cmd": "cat $(echo src/m.py)",
           "cwd": str(repo), "reads": True}
    r = audit_case(tmp_path, repo, ["src/m.py"], [rec])
    assert r.returncode == 3 and "UNRESOLVED" in r.stdout


@pytest.mark.parametrize("spelling", ["a.py", "./a.py", "src/../a.py", "{repo}/a.py"])
def test_validate_accepts_every_spelling_of_an_in_scope_file(repo, spelling):
    from conftest import load
    review = load("review")
    manifest = {"repo": str(repo), "files": [{"path": "a.py"}], "reference": []}
    doc = {"reviewer": "code", "machine": {}, "denominators": {}, "findings": [],
           "scope": {"files_read": [spelling.format(repo=repo)]}}
    assert review.validate_findings(doc, manifest, str(repo)) == []


def test_coverage_credits_every_spelling_of_a_read_file(tmp_path, repo):
    import json
    from conftest import run
    d = tmp_path / "2026-10-06T10-00-00Z"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"repo": str(repo), "files": [
        {"path": "a.py", "sha256": "s", "lines": 2}], "reference": []}))
    (d / "findings-code.json").write_text(json.dumps({
        "reviewer": "code", "scope": {"files_read": ["./a.py"]},
        "findings": [{"file": f"{repo}/a.py"}]}))
    out = tmp_path / "coverage.json"
    r = run("review.py", "coverage", "--run-dir", d, "--out", out, cwd=repo)
    assert r.returncode == 0, r.stderr
    e = json.loads(out.read_text())["files"]["a.py"]
    assert e["reviewed_by"] == {"code": d.name}
    assert e["times_reviewed"] == 1 and e["findings_total"] == 1
