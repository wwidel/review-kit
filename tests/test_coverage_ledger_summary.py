"""coverage replay (bug 6), the reconciliation check (bugs 7 and 9), ledger and summarize."""
import json
import re

import pytest

from conftest import finding, fingerprints, item, make_run, run, write_reconciliation


def coverage(run_dir, out):
    r = run("review.py", "coverage", "--run-dir", run_dir, "--out", out)
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())["files"]


def test_a_leg_added_on_resume_is_counted(tmp_path, repo):
    d = make_run(tmp_path, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    out = tmp_path / "coverage.json"
    coverage(d, out)
    # The run is resumed and the docs leg finishes.
    (d / "findings-docs.json").write_text(json.dumps({
        "reviewer": "docs", "scope": {"files_read": ["README.md"]},
        "findings": [finding("d1", "README.md", 1)]}))
    files = coverage(d, out)
    assert files["README.md"]["reviewed_by"] == {"docs": "R1"}
    assert files["README.md"]["times_reviewed"] == 1
    assert files["README.md"]["findings_total"] == 1
    assert files["a.py"]["times_reviewed"] == 1 and files["a.py"]["findings_total"] == 1


def test_a_plain_replay_changes_no_counter(tmp_path, repo):
    d = make_run(tmp_path, "R1", repo, read={"code": ["a.py"]},
                 findings={"code": [finding("c1", "a.py", 2)]})
    out = tmp_path / "coverage.json"
    once = coverage(d, out)
    assert coverage(d, out) == once


def test_counts_accumulate_across_runs(tmp_path, repo):
    out = tmp_path / "coverage.json"
    coverage(make_run(tmp_path, "R1", repo, read={"code": ["a.py"]},
                      findings={"code": [finding("c1", "a.py", 2)]}), out)
    files = coverage(make_run(tmp_path, "R2", repo, read={"code": ["a.py"]},
                              findings={"code": [finding("c1", "a.py", 2)]}), out)
    assert files["a.py"]["times_reviewed"] == 2 and files["a.py"]["findings_total"] == 2


def test_a_record_from_before_per_run_counts(tmp_path, repo):
    out = tmp_path / "coverage.json"
    out.write_text(json.dumps({"runs": ["R0"], "files": {"a.py": {
        "tier": "review", "first_seen_run": "R0", "times_reviewed": 3, "findings_total": 5,
        "reviewed_by": {"code": "R0"}, "sha256_when_reviewed": {}, "last_reviewed_run": "R0"}}}))
    r0 = make_run(tmp_path, "R0", repo, read={"code": ["a.py"]},
                  findings={"code": [finding("c1", "a.py", 2)]})
    files = coverage(r0, out)
    assert files["a.py"]["times_reviewed"] == 3 and files["a.py"]["findings_total"] == 5
    files = coverage(make_run(tmp_path, "R1", repo, read={"code": ["a.py"]},
                              findings={"code": [finding("c1", "a.py", 2)]}), out)
    assert files["a.py"]["times_reviewed"] == 4 and files["a.py"]["findings_total"] == 6


# ---------------------------------------------------------------- the reconciliation check

@pytest.fixture
def reconciled(tmp_path, repo):
    d = make_run(tmp_path, "R1", repo, read={"code": ["a.py"], "docs": ["README.md"]},
                 findings={"code": [finding("c1", "a.py", 2), finding("c2", "a.py", 1)],
                           "docs": [finding("d1", "README.md", 1, severity="minor")]})
    c1, c2 = fingerprints(d, "code")
    (d1,) = fingerprints(d, "docs")
    return d, {"c1": c1, "c2": c2, "d1": d1}


STAGES = {
    "carryover": lambda d, tmp: ["carryover", "--run-dir", d, "--reconciliation",
                                 d / "reconciliation.json", "--out", tmp / "od.json"],
    "summarize": lambda d, tmp: ["summarize", "--reconciliation", d / "reconciliation.json",
                                 "--out", tmp / "SUMMARY.md", "--run-dir", d],
    "ledger": lambda d, tmp: ["ledger", "--reconciliation", d / "reconciliation.json",
                              "--out", tmp / "ledger.jsonl", "--run-dir", d],
}

BAD = {
    "severity": ({"severity": "High"}, "severity 'High'"),
    "disposition": ({"disposition": "confirmed"}, "disposition 'confirmed'"),
    "class": ({"class": "bug"}, "class 'bug'"),
    "reviewer": ({"corroborated_by": ["review-tests"]}, "not sources"),
    "corroborated_by type": ({"corroborated_by": "code"}, "must be a list"),
    "missing key": ({"defect": None}, "missing 'defect'"),
}


@pytest.mark.parametrize("stage", sorted(STAGES))
@pytest.mark.parametrize("bad", sorted(BAD))
def test_every_stage_refuses_an_invalid_item(tmp_path, reconciled, stage, bad):
    d, fp = reconciled
    change, message = BAD[bad]
    x = item(fp["c1"], "a.py:2")
    x.update(change)
    if x.get("defect") is None:
        del x["defect"]
    write_reconciliation(d, [x])
    r = run("review.py", *STAGES[stage](d, tmp_path))
    assert r.returncode == 2, r.stdout
    assert message in r.stderr
    assert not any((tmp_path / f).exists() for f in ("od.json", "SUMMARY.md", "ledger.jsonl"))


def test_all_problems_are_listed_at_once(tmp_path, reconciled):
    d, fp = reconciled
    write_reconciliation(d, [dict(item(fp["c1"], "a.py:2"), severity="High"),
                             dict(item(fp["c2"], "a.py:1"), disposition="confirmed")])
    r = run("review.py", *STAGES["ledger"](d, tmp_path))
    assert "2 problem(s)" in r.stderr


def ledger_rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]


def test_ledger_counts_per_reviewer_and_canonicalises_old_names(tmp_path, reconciled):
    d, fp = reconciled
    write_reconciliation(d, [
        item(fp["c1"], "a.py:2", corroborated_by=["review-code", "review-docs"]),
        item(fp["c2"], "a.py:1", disposition="dropped", corroborated_by=["code"]),
        item(fp["d1"], "README.md:1", severity="minor", corroborated_by=[])])
    r = run("review.py", *STAGES["ledger"](d, tmp_path))
    assert r.returncode == 0, r.stderr
    rows = {x["reviewer"]: x for x in ledger_rows(tmp_path)}
    assert set(rows) == {"code", "docs", "(unattributed)"}
    assert (rows["code"]["reported"], rows["code"]["confirmed"], rows["code"]["refuted"]) \
        == (2, 1, 1)
    assert rows["code"]["rate_verifiable"] == 0.5
    assert rows["docs"]["confirmed"] == 1


def test_ledger_replay_appends_nothing(tmp_path, reconciled):
    d, fp = reconciled
    write_reconciliation(d, [item(fp["c1"], "a.py:2")])
    run("review.py", *STAGES["ledger"](d, tmp_path))
    before = (tmp_path / "ledger.jsonl").read_text()
    r = run("review.py", *STAGES["ledger"](d, tmp_path))
    assert r.returncode == 0 and "replay" in r.stderr
    assert (tmp_path / "ledger.jsonl").read_text() == before


def test_summary_headers_match_their_items(tmp_path, reconciled):
    d, fp = reconciled
    write_reconciliation(d, [
        item(fp["c1"], "a.py:2", severity="critical"),
        item(fp["c2"], "a.py:1", disposition="dropped"),
        item(fp["d1"], "README.md:1", severity="minor")])
    r = run("review.py", *STAGES["summarize"](d, tmp_path))
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "SUMMARY.md").read_text()
    assert "**2 confirmed defects for you (1 critical).**" in text
    assert "1 reported finding was refuted and dropped" in text
    assert "## For you (2)" in text
    sections = dict(re.findall(r"### (\w+) — (\d+)", text))
    assert sections == {"Critical": "1", "Minor": "1"}
    assert "a.py:1" not in text.split("## For you")[1].split("##")[0]


# ---------------------------------------------------------------- the static source

def test_static_is_a_source_the_reconciliation_may_name(tmp_path, reconciled):
    d, fp = reconciled
    write_reconciliation(d, [item(fp["c1"], "a.py:2", severity="critical",
                                  corroborated_by=["static"]),
                             item(fp["c2"], "a.py:1", corroborated_by=["code", "static"])])
    r = run("review.py", *STAGES["summarize"](d, tmp_path))
    assert r.returncode == 0, r.stderr
    assert "**static check only**" in (tmp_path / "SUMMARY.md").read_text()
    r = run("review.py", *STAGES["ledger"](d, tmp_path))
    assert {x["reviewer"] for x in ledger_rows(tmp_path)} == {"code", "static"}


def test_coverage_does_not_credit_the_static_checks(tmp_path, repo):
    d = make_run(tmp_path, "R1", repo, read={"code": ["a.py"]})
    (d / "findings-static.json").write_text(json.dumps({
        "reviewer": "static", "scope": {"files_read": ["a.py", "src/m.py"]},
        "findings": [finding("s1", "src/m.py", 1)]}))
    files = coverage(d, tmp_path / "coverage.json")
    assert files["a.py"]["reviewed_by"] == {"code": "R1"}
    assert files["src/m.py"]["reviewed_by"] == {} and files["src/m.py"]["findings_total"] == 0


def test_runs_does_not_list_the_static_file_as_unexpected(tmp_path, installed, repo):
    from conftest import load
    store = load("review").store_path(repo)
    d = make_run(store / "runs", "R1", repo, read={"code": ["a.py"]})
    (d / "findings-static.json").write_text("{}")
    r = run("review.py", "runs", "--repo", repo, cwd=repo)
    assert "also present" not in r.stdout
