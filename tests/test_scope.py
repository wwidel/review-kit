"""scope: every kind of file lands in the right bucket, and nothing is silently dropped."""
import json

import pytest

from conftest import git, run


def commit(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


def scope(repo, out, *args, ok=True):
    r = run("review.py", "scope", "--repo", repo, "--out", out, *args, cwd=repo)
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
        return json.loads((out / "manifest.json").read_text())
    return r


def buckets(m):
    return {"review": sorted(f["path"] for f in m["files"]),
            "tabular": sorted(f["path"] for f in m["reference"] if f["kind"] == "tabular"),
            "opaque": sorted(f["path"] for f in m["reference"] if f["kind"] == "opaque"),
            "unclassified": sorted(f["path"] for f in m["unclassified"]),
            "unreadable": sorted(f["path"] for f in m["unreadable"]),
            "skipped_dirs": m["skipped_dirs"], "deleted": m["deleted"]}


@pytest.fixture
def mixed(installed, repo):
    (repo / "data.csv").write_text("a,b\n1,2\n")
    (repo / "words.txt").write_text("w\n" * 600)
    (repo / "long.py").write_text("x = 1\n" * 600)
    (repo / "long.md").write_text("line\n" * 600)
    (repo / "model.pkl").write_bytes(b"\x80\x04K\x01.")
    (repo / "LICENSE").write_text("MIT\n")
    (repo / "blob.weird").write_bytes(b"abc\0def")
    (repo / "notes.weird").write_text("plain text\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "x.js").write_text("x\n")
    (repo / "REVIEW-SUMMARY.md").write_text("the kit's own output\n")
    commit(repo)
    (repo / "out.weird").write_text("untracked output\n")
    (repo / "raw.csv").symlink_to("/nonexistent/raw.csv")
    return repo


def test_every_kind_of_file_lands_in_its_bucket(tmp_path, mixed):
    m = scope(mixed, tmp_path / "run")
    assert buckets(m) == {
        "review": ["LICENSE", "README.md", "a.py", "long.md", "long.py", "notes.weird",
                   "src/m.py"],
        "tabular": ["data.csv", "words.txt"],
        "opaque": ["blob.weird", "model.pkl"],
        "unclassified": ["out.weird"],
        "unreadable": ["raw.csv"],
        "skipped_dirs": [".git", "node_modules"],
        "deleted": []}
    assert m["mode"] == "full"


def test_long_source_and_markdown_are_named_not_demoted(tmp_path, mixed):
    r = run("review.py", "scope", "--repo", mixed, "--out", tmp_path / "run", cwd=mixed)
    assert "long.py" in r.stdout + r.stderr and "long.md" in r.stdout + r.stderr


def test_the_summary_is_left_out(tmp_path, mixed):
    m = scope(mixed, tmp_path / "run")
    every = json.dumps(m)
    assert "REVIEW-SUMMARY.md" not in every


def test_an_empty_scope_exits_non_zero(tmp_path, home):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    r = scope(empty, tmp_path / "run", ok=False)
    assert r.returncode != 0


def test_since_lists_changes_deletions_and_new_untracked_files(tmp_path, installed, repo):
    (repo / "gone.py").write_text("g = 1\n")
    commit(repo, "add gone")
    git(repo, "rm", "-q", "gone.py")
    (repo / "a.py").write_text("import numpy as np\nx = np.random.rand(4)\n")
    commit(repo, "change")
    (repo / "new.py").write_text("n = 1\n")
    m = scope(repo, tmp_path / "run", "--since", "HEAD~1")
    assert m["mode"] == "since"
    assert buckets(m)["review"] == ["a.py", "new.py"]
    assert m["deleted"] == ["gone.py"]


def test_since_honours_skipped_directories(tmp_path, installed, repo):
    (repo / "build").mkdir()
    (repo / "build" / "gen.py").write_text("x\n")
    commit(repo, "build")
    (repo / "build" / "gen.py").write_text("y\n")
    (repo / "a.py").write_text("changed\n")
    commit(repo, "edit")
    m = scope(repo, tmp_path / "run", "--since", "HEAD~1")
    assert buckets(m)["review"] == ["a.py"]
    assert m["skipped_dirs"] == ["build"]


def test_since_on_a_subdirectory(tmp_path, installed, repo):
    (repo / "src" / "m.py").write_text("def f():\n    return 2\n")
    commit(repo, "edit")
    m = scope(repo / "src", tmp_path / "run", "--since", "HEAD~1")
    assert buckets(m)["review"] == ["m.py"] and m["deleted"] == []


def test_since_and_full_together_are_refused(tmp_path, repo):
    r = scope(repo, tmp_path / "run", "--since", "HEAD", "--full", ok=False)
    assert r.returncode == 2


def test_since_keeps_a_changed_non_ascii_file(tmp_path, installed, repo):
    (repo / "café.py").write_text("x = 1\n")
    commit(repo, "add")
    (repo / "café.py").write_text("x = 2\n")
    commit(repo, "edit")
    m = scope(repo, tmp_path / "run", "--since", "HEAD~1")
    assert buckets(m)["review"] == ["café.py"] and m["deleted"] == []


def test_since_runs_no_shell(tmp_path, installed, repo):
    ref = "v1;touch${IFS}PWNED"
    git(repo, "branch", ref)
    (repo / "a.py").write_text("changed\n")
    m = scope(repo, tmp_path / "run", "--since", ref)
    assert not (repo / "PWNED").exists() and not list(repo.glob("PWNED*"))
    assert m["mode"] == "since" and buckets(m)["review"] == ["a.py"]


def test_every_summary_at_the_root_is_left_out(tmp_path, installed, repo):
    (repo / "REVIEW-SUMMARY.md").write_text("an older summary\n")
    (repo / "REVIEW-SUMMARY-1.md").write_text("another\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "REVIEW-SUMMARY.md").write_text("not at the root\n")
    m = scope(repo, tmp_path / "run")
    assert "docs/REVIEW-SUMMARY.md" in buckets(m)["review"]
    assert not {"REVIEW-SUMMARY.md", "REVIEW-SUMMARY-1.md"} & set(buckets(m)["review"])
