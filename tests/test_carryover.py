"""carryover: its outcomes, the collision rule, and replay."""
import json

import pytest

from conftest import (finding, fingerprints, git, item, make_run, run, write_reconciliation)

LINE2 = "x = np.random.rand(3)\n"


@pytest.fixture
def runs(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def carry(run_dir, out, run_id=None):
    args = ["carryover", "--run-dir", run_dir, "--reconciliation",
            run_dir / "reconciliation.json", "--out", out]
    if run_id:
        args += ["--run-id", run_id]
    return run("review.py", *args)


def defects(out):
    return json.loads(out.read_text())["defects"]


@pytest.fixture
def first(tmp_path, runs, repo):
    """Run 1 confirms one defect at a.py:2. Returns (its fingerprint, the open-defects file)."""
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp, "a.py:2")])
    out = tmp_path / "open_defects.json"
    r = carry(d, out)
    assert r.returncode == 0, r.stderr
    assert defects(out)[fp]["status"] == "open"
    return fp, out


def second(runs, repo, rid="R2", items=(), refile=False, **kw):
    findings = {"code": [finding("c1", "a.py", 2)]} if refile else None
    d = make_run(runs, rid, repo, read={"code": ["a.py"]}, findings=findings, **kw)
    write_reconciliation(d, list(items))
    return d


def outcome(out, fp):
    d = defects(out)[fp]
    return d["status"], d["last_outcome"]


def test_re_reported(first, runs, repo):
    fp, out = first
    d = second(runs, repo, refile=True, items=[item(fp, "a.py:2")])
    assert carry(d, out).returncode == 0
    assert outcome(out, fp) == ("open", "re-reported")


def test_missed(first, runs, repo):
    fp, out = first
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("open", "missed")


def test_plausibly_fixed(first, runs, repo):
    fp, out = first
    (repo / "a.py").write_text("import numpy as np\nx = np.random.default_rng(0).random(3)\n")
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("plausibly-fixed", "plausibly-fixed")


def test_not_re_checked_when_the_file_is_outside_the_window(first, runs, repo):
    fp, out = first
    d = second(runs, repo, mode="since", files=["README.md"])
    assert carry(d, out).returncode == 0
    assert outcome(out, fp) == ("open", "not-re-checked")


def test_file_deleted_on_a_full_scope(first, runs, repo):
    fp, out = first
    git(repo, "rm", "-q", "a.py")
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("plausibly-fixed", "file-deleted")


def test_a_missing_file_under_since_is_out_of_window_not_deleted(first, runs, repo):
    fp, out = first
    git(repo, "rm", "-q", "a.py")
    assert carry(second(runs, repo, mode="since"), out).returncode == 0
    assert outcome(out, fp) == ("open", "not-re-checked")


def test_fixed_this_run(first, runs, repo):
    fp, out = first
    d = second(runs, repo, refile=True, items=[item(fp, "a.py:2", disposition="applied")])
    assert carry(d, out).returncode == 0
    assert outcome(out, fp) == ("fixed", "fixed-this-run")


def test_refuted_this_run(first, runs, repo):
    fp, out = first
    d = second(runs, repo, refile=True, items=[item(fp, "a.py:2", disposition="dropped")])
    assert carry(d, out).returncode == 0
    assert outcome(out, fp) == ("dropped", "refuted-this-run")


def test_reported_not_carried(first, runs, repo):
    fp, out = first
    assert carry(second(runs, repo, refile=True), out).returncode == 0
    assert outcome(out, fp) == ("open", "reported-not-carried")


def test_anchor_ambiguous(first, runs, repo):
    fp, out = first
    (repo / "a.py").write_text("import numpy as np\n" + LINE2 + LINE2)
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("open", "anchor-ambiguous")


def test_unanchored_not_checkable(tmp_path, runs, repo):
    f = finding("c1", "a.py", None, claim="the whole module lacks a seed")
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]}, findings={"code": [f]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp, "a.py")])
    out = tmp_path / "open_defects.json"
    assert carry(d, out).returncode == 0
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("open", "unanchored-not-checkable")


def regress_setup(first, runs, repo):
    fp, out = first
    (repo / "a.py").write_text("import numpy as np\nx = np.random.default_rng(0).random(3)\n")
    assert carry(second(runs, repo), out).returncode == 0
    (repo / "a.py").write_text("import numpy as np\n" + LINE2)
    return fp, out


def test_regressed(first, runs, repo):
    fp, out = regress_setup(first, runs, repo)
    assert carry(second(runs, repo, "R3"), out).returncode == 0
    assert outcome(out, fp) == ("open", "regressed")


def test_a_fixed_defect_is_not_reopened_by_leaving_the_window(first, runs, repo):
    fp, out = regress_setup(first, runs, repo)
    d = second(runs, repo, "R3", mode="since", files=["README.md"])
    assert carry(d, out).returncode == 0
    assert outcome(out, fp) == ("plausibly-fixed", "plausibly-fixed")


def test_replaying_the_last_run_gives_the_same_record(first, runs, repo):
    fp, out = regress_setup(first, runs, repo)
    d = second(runs, repo, "R3")
    assert carry(d, out).returncode == 0
    once = json.loads(out.read_text())
    assert carry(d, out).returncode == 0
    twice = json.loads(out.read_text())
    once.pop("updated"), twice.pop("updated")
    assert twice == once
    assert outcome(out, fp) == ("open", "regressed")


def test_replaying_an_older_run_is_refused(first, runs, repo):
    fp, out = first
    assert carry(second(runs, repo), out).returncode == 0
    r = carry(runs / "R1", out)
    assert r.returncode == 2 and "Only the most recent run can be replayed" in r.stderr


def test_a_record_from_before_replay_tracking_still_merges(first, runs, repo):
    fp, out = first
    rec = json.loads(out.read_text())
    for k in ("runs_merged", "last_run", "before_last_run"):
        rec.pop(k)
    out.write_text(json.dumps(rec))
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("open", "missed")


@pytest.mark.parametrize("order", [("dropped", "open"), ("open", "dropped")])
def test_a_real_defect_wins_a_shared_fingerprint(tmp_path, runs, repo, order):
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp, "a.py:2", disposition=x) for x in order])
    out = tmp_path / "open_defects.json"
    r = carry(d, out)
    assert r.returncode == 0 and "differing dispositions" in r.stdout
    assert defects(out)[fp]["status"] == "open"


def test_a_run_id_that_disagrees_with_the_directory_is_refused(tmp_path, runs, repo):
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp, "a.py:2")], run_id="SOMETHING-ELSE")
    r = carry(d, tmp_path / "open_defects.json")
    assert r.returncode == 2 and not (tmp_path / "open_defects.json").exists()


def test_an_abbreviated_fingerprint_is_repaired(tmp_path, runs, repo):
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp[:8], "a.py:2")])
    out = tmp_path / "open_defects.json"
    r = carry(d, out)
    assert r.returncode == 0 and "abbreviated" in r.stderr
    assert list(defects(out)) == [fp]


@pytest.mark.parametrize("given,why", [("abc", "ambiguous prefix"),
                                       ("zzz999", "matches no finding")])
def test_an_ambiguous_or_unknown_fingerprint_is_refused(tmp_path, runs, repo, given, why):
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]})
    doc = json.loads((d / "findings-code.json").read_text())
    doc["findings"] = [dict(finding("c1", "a.py", 1), fingerprint="abc111"),
                       dict(finding("c2", "a.py", 2), fingerprint="abc222")]
    (d / "findings-code.json").write_text(json.dumps(doc))
    write_reconciliation(d, [item(given, "a.py:2")])
    r = carry(d, tmp_path / "open_defects.json")
    assert r.returncode == 2 and why in r.stderr


# ---------------------------------------------------------------- the same defect, a new id

def three_lines(repo, third):
    (repo / "a.py").write_text(f"import numpy as np\nx = np.random.rand(3)\n{third}\n")


def test_a_refiled_defect_whose_next_line_changed_moves_to_its_new_id(tmp_path, runs, repo):
    three_lines(repo, "y = 1")
    d1 = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                  findings={"code": [finding("c1", "a.py", 2)]})
    (fp1,) = fingerprints(d1, "code")
    write_reconciliation(d1, [item(fp1, "a.py:2")])
    out = tmp_path / "open_defects.json"
    assert carry(d1, out).returncode == 0
    three_lines(repo, "y = 2")          # the line after the defect changes; the defect does not
    d2 = make_run(runs, "R2", repo, read={"code": ["a.py"]},
                  findings={"code": [finding("c1", "a.py", 2)]})
    (fp2,) = fingerprints(d2, "code")
    assert fp2 != fp1
    write_reconciliation(d2, [item(fp2, "a.py:2")])
    assert carry(d2, out).returncode == 0
    od = defects(out)
    assert (od[fp1]["status"], od[fp1]["superseded_by"]) == ("superseded", fp2)
    assert (od[fp2]["status"], od[fp2]["first_run"]) == ("open", "R1")
    s = tmp_path / "SUMMARY.md"
    r = run("review.py", "summarize", "--reconciliation", d2 / "reconciliation.json",
            "--out", s, "--open-defects", out, "--run-dir", d2)
    assert r.returncode == 0, r.stderr
    assert "Still open from earlier runs" not in s.read_text()
    # A third run that does not re-file it: the defect is missed once, under its new id.
    assert carry(second(runs, repo, rid="R3"), out).returncode == 0
    od = defects(out)
    assert od[fp1]["status"] == "superseded" and outcome(out, fp2) == ("open", "missed")


def test_a_reformatted_line_is_not_a_fix(first, runs, repo):
    fp, out = first
    (repo / "a.py").write_text("import numpy as np\nx=np.random.rand(3)\n")
    assert carry(second(runs, repo), out).returncode == 0
    assert outcome(out, fp) == ("open", "missed")


def test_an_unverifiable_defect_not_raised_again_is_not_a_miss(tmp_path, runs, repo):
    d = make_run(runs, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    (fp,) = fingerprints(d, "code")
    write_reconciliation(d, [item(fp, "a.py:2", disposition="unverifiable")])
    out = tmp_path / "open_defects.json"
    assert carry(d, out).returncode == 0
    d2 = second(runs, repo)
    assert carry(d2, out).returncode == 0
    assert outcome(out, fp) == ("open", "unconfirmed-not-re-raised")
    s = tmp_path / "SUMMARY.md"
    r = run("review.py", "summarize", "--reconciliation", d2 / "reconciliation.json",
            "--out", s, "--open-defects", out, "--run-dir", d2)
    assert r.returncode == 0, r.stderr
    text = s.read_text()
    assert "state undecided (1)" in text and "deferred or unverifiable when filed" in text
    assert "not re-reported" not in text
