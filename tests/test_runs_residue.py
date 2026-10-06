"""`runs` and `residue`: plan bugs 2 and 3."""
import json

from conftest import load, run


def test_residue_without_a_snapshot_says_so_instead_of_blaming_the_reviewers(tmp_path, repo):
    (repo / "untracked.txt").write_text("was here before the run\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    r = run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, cwd=repo)
    assert r.returncode == 2
    assert "no snapshot" in r.stderr
    assert "RESIDUE" not in r.stdout


def test_residue_on_a_clean_tree_with_an_empty_snapshot_is_clean(tmp_path, repo):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, "--save",
               cwd=repo).returncode == 0
    assert json.loads((run_dir / "tree-before.json").read_text()) == {}
    r = run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, cwd=repo)
    assert r.returncode == 0 and "as the reviewers found it" in r.stdout


def test_residue_names_what_a_run_left_behind(tmp_path, repo):
    (repo / "before.txt").write_text("already untracked\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, "--save", cwd=repo)
    (repo / "left.txt").write_text("a reviewer wrote this\n")
    r = run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, cwd=repo)
    assert r.returncode == 1
    assert "left.txt" in r.stdout and "before.txt" not in r.stdout


def make_run(store, run_id="2026-10-06T10-00-00Z"):
    d = store / "runs" / run_id
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"files": [], "generated": "x"}))
    return d


def test_runs_reads_the_store_even_with_a_leftover_in_repo_review_dir(installed, repo):
    make_run(installed)
    (repo / ".review" / "runs").mkdir(parents=True)
    r = run("review.py", "runs", "--repo", repo, cwd=repo)
    assert r.returncode == 0
    assert "no runs yet" not in r.stdout
    assert "RESUMABLE: 2026-10-06T10-00-00Z" in r.stdout
    assert "left over from an older version" in r.stderr


def test_runs_with_no_runs(installed, repo):
    r = run("review.py", "runs", "--repo", repo, cwd=repo)
    assert r.returncode == 0 and "no runs yet" in r.stdout
