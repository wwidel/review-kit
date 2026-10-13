"""Every scripted stage of a review, in the order the skills call them, with canned agent
output in place of the reviewers and the reconciler.

Each stage reads files earlier stages wrote, so this is what catches one stage changing a
format another depends on. The order is the skills': repo-audit Stages 0-4, reconcile-reviews
Stage 5, then repo-audit's final `residue` once the summary is in the working tree.
"""
import json

import pytest

from conftest import finding, git, now_iso, run

LEGS = ("fresh-eyes", "code", "analysis", "docs")


class Review:
    def __init__(self, repo):
        self.repo = repo

    def __call__(self, *args, ok=True):
        r = run("review.py", *args, cwd=self.repo)
        if ok:
            assert r.returncode == 0, f"review.py {args[0]}: {r.stdout}{r.stderr}"
        return r

    def store(self):
        from pathlib import Path
        return Path(self("store", "--repo", ".").stdout.strip())

    def summary(self):
        return self.repo / self("store", "--repo", ".", "--summary").stdout.strip()


@pytest.fixture
def rv(installed, repo):
    return Review(repo)


def stage0(rv, run_id):
    """preflight, store, runs, residue --save, scope, machine, characterize, probe, estimate."""
    rv("preflight", "--repo", ".")
    store = rv.store()
    rv.summary()
    rv("runs", "--repo", ".")
    d = store / "runs" / run_id
    d.mkdir(parents=True)
    rv("residue", "--repo", ".", "--run-dir", d, "--save")
    rv("scope", "--repo", ".", "--out", d, "--list")
    rv("machine", "--repo", ".", "--out", d / "machine.json")
    rv("characterize", "--repo", ".", "--out", d / "repo_profile.json")
    rv("probe", "--manifest", d / "manifest.json", "--out", d / "probes.json")
    rv("estimate", "--manifest", d / "manifest.json")
    return store, d


def reviewer(rv, d, leg, findings):
    """What a reviewer leg leaves behind, then validate and fingerprint, as Stage 2 does."""
    manifest = json.loads((d / "manifest.json").read_text())
    p = d / f"findings-{leg}.json"
    p.write_text(json.dumps({
        "reviewer": leg, "machine": {}, "denominators": {"files": len(manifest["files"])},
        "scope": {"files_read": [f["path"] for f in manifest["files"]]},
        "findings": findings}))
    rv("validate", "--findings", p, "--manifest", d / "manifest.json")
    rv("fingerprint", "--findings", p, "--manifest", d / "manifest.json")
    return json.loads(p.read_text())


def static(rv, d):
    """The static checks, after the reviewers, then validate and fingerprint like theirs."""
    r = run("detect.py", "--manifest", d / "manifest.json", "--findings",
            d / "findings-static.json", cwd=rv.repo)
    assert r.returncode == 0, r.stderr
    rv("validate", "--findings", d / "findings-static.json", "--manifest", d / "manifest.json")
    rv("fingerprint", "--findings", d / "findings-static.json", "--manifest", d / "manifest.json")
    return json.loads((d / "findings-static.json").read_text())


def access_log(store, d, legs):
    """The hook's records for each leg's reads, one agent id per leg."""
    manifest = json.loads((d / "manifest.json").read_text())
    with (store / "access.jsonl").open("a") as f:
        for leg in legs:
            for m in manifest["files"]:
                f.write(json.dumps({"t": now_iso(), "tool": "Read", "agent": f"agent-{leg}",
                                    "paths": [str(manifest_repo(d) / m["path"])]}) + "\n")


def manifest_repo(d):
    from pathlib import Path
    return Path(json.loads((d / "manifest.json").read_text())["repo"])


def stage3(rv, store, d, legs, prev=None):
    """audit each leg, cluster, match, coverage."""
    listing = rv("audit", "--repo", ".", "--access-log", store / "access.jsonl",
                 "--list-agents").stdout
    for leg in legs:
        assert f"agent-{leg}" in listing
        rv("audit", "--findings", d / f"findings-{leg}.json", "--repo", ".", "--access-log",
           store / "access.jsonl", "--agent", f"agent-{leg}", "--run-dir", d)
    rv("cluster", "--run-dir", d)
    rv("match", "--run-dir", d, *(["--prev-run", prev] if prev else []))
    rv("coverage", "--run-dir", d, "--out", store / "coverage.json", "--run-id", d.name)


def stage5(rv, store, d, items):
    (d / "reconciliation.json").write_text(json.dumps({
        "run_id": d.name, "date": "2026-10-07", "run_dir": str(d), "head": "abc",
        "machine": {"cores": 1, "usable_gb": 1, "gpu": None}, "items": items}))
    rv("carryover", "--run-dir", d, "--reconciliation", d / "reconciliation.json",
       "--out", store / "open_defects.json", "--run-id", d.name)
    rv("summarize", "--reconciliation", d / "reconciliation.json", "--out", rv.summary(),
       "--open-defects", store / "open_defects.json", "--run-dir", d)
    rv("ledger", "--reconciliation", d / "reconciliation.json", "--out",
       store / "ledger.jsonl", "--run-dir", d)


def rec_item(f, disposition, legs):
    return {"fingerprint": f["fingerprint"], "disposition": disposition,
            "severity": f["severity"], "class": "defect", "title": f["claim"],
            "where": f"{f['file']}:{f['line']}", "defect": f["claim"], "fix": f["fix"],
            "corroborated_by": list(legs)}


def full_run(rv, run_id, legs=LEGS, prev=None):
    store, d = stage0(rv, run_id)
    docs = {leg: reviewer(rv, d, leg, []) for leg in legs}
    docs["code"] = reviewer(rv, d, "code", [
        finding("c1", "a.py", 2, "nothing seeds np.random", "critical"),
        finding("c2", "src/m.py", 2, "returns a constant", "minor")])
    if "analysis" in legs:
        docs["analysis"] = reviewer(rv, d, "analysis", [
            finding("a1", "a.py", 2, "the draw is unseeded", "critical")])
    static(rv, d)
    access_log(store, d, legs)
    stage3(rv, store, d, legs, prev)
    c1, c2 = docs["code"]["findings"]
    items = [rec_item(c1, "open", ["code", "analysis"] if "analysis" in legs else ["code"]),
             rec_item(c2, "dropped", ["code"])]
    stage5(rv, store, d, items)
    return store, d, c1, c2


def test_a_full_run(rv, repo):
    store, d, c1, c2 = full_run(rv, "R1")
    doc = json.loads((d / "findings-static.json").read_text())
    # a.py draws from numpy's global RNG with no seed: a held-back kind, counted, not sent.
    assert doc["reviewer"] == "static" and doc["denominators"]["held_back"] == 1
    text = rv.summary().read_text()
    assert "**1 confirmed defects for you (1 critical).**" in text
    assert "1 reported finding was refuted and dropped" in text
    assert "### Critical — 1" in text and "a.py:2" in text
    rows = {r["reviewer"]: r for r in map(json.loads,
                                          (store / "ledger.jsonl").read_text().splitlines())}
    assert set(rows) == {"code", "analysis"}
    assert (rows["code"]["confirmed"], rows["code"]["refuted"]) == (1, 1)
    od = json.loads((store / "open_defects.json").read_text())["defects"]
    assert od[c1["fingerprint"]]["status"] == "open"
    assert od[c2["fingerprint"]]["status"] == "dropped"
    cov = json.loads((store / "coverage.json").read_text())["files"]
    assert cov["a.py"]["times_reviewed"] == 1 and set(cov["a.py"]["reviewed_by"]) == set(LEGS)
    # The summary is now in the working tree, and residue still calls the run clean.
    assert rv.summary().exists()
    r = rv("residue", "--repo", ".", "--run-dir", d)
    assert "as the reviewers found it" in r.stdout
    assert "complete" in rv("runs", "--repo", ".").stdout


def test_resuming_a_run_with_a_leg_added(rv, repo):
    store, d, c1, c2 = full_run(rv, "R1", legs=("fresh-eyes", "code", "analysis"))
    ledger_before = (store / "ledger.jsonl").read_text()
    od_before = json.loads((store / "open_defects.json").read_text())
    # The docs leg finishes on resume; everything after Stage 2 is re-run.
    r = rv("residue", "--repo", ".", "--run-dir", d, "--save")
    assert "keeping the existing snapshot" in r.stdout
    reviewer(rv, d, "docs", [finding("d1", "README.md", 1, "the README is one line", "minor")])
    access_log(store, d, ["docs"])
    stage3(rv, store, d, ["docs"])
    stage5(rv, store, d, json.loads((d / "reconciliation.json").read_text())["items"])
    assert (store / "ledger.jsonl").read_text() == ledger_before
    od_after = json.loads((store / "open_defects.json").read_text())
    od_before.pop("updated"), od_after.pop("updated")
    assert od_after == od_before
    cov = json.loads((store / "coverage.json").read_text())["files"]
    assert cov["README.md"]["reviewed_by"]["docs"] == "R1"
    assert cov["README.md"]["findings_total"] == 1
    assert cov["a.py"]["times_reviewed"] == 1


def test_a_second_run_after_a_fix(rv, repo):
    store, d1, c1, c2 = full_run(rv, "R1")
    # The critical is fixed and committed; nothing else changes.
    (repo / "a.py").write_text("import numpy as np\nx = np.random.default_rng(0).random(3)\n")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "seed")
    store, d2 = stage0(rv, "R2")
    for leg in LEGS:
        reviewer(rv, d2, leg, [])
    access_log(store, d2, LEGS)
    stage3(rv, store, d2, LEGS, prev=d1)
    stage5(rv, store, d2, [])
    od = json.loads((store / "open_defects.json").read_text())["defects"]
    assert od[c1["fingerprint"]]["last_outcome"] == "plausibly-fixed"
    assert "**0 confirmed defects for you" in rv.summary().read_text()
    cov = json.loads((store / "coverage.json").read_text())["files"]
    assert cov["a.py"]["times_reviewed"] == 2


def test_static_findings_flow_through_cluster_carryover_and_summary(rv, repo):
    """A static finding that duplicates a reviewer's, and one only the static checks found."""
    (repo / "b.py").write_text("from sklearn.cluster import KMeans\nk = KMeans(3)\n")
    git(repo, "add", "b.py")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
    store, d = stage0(rv, "R1")
    for leg in ("fresh-eyes", "analysis", "docs"):
        reviewer(rv, d, leg, [])
    code = reviewer(rv, d, "code", [finding("c1", "b.py", 2, "KMeans has no random_state",
                                            "minor")])
    doc = static(rv, d)
    (s,) = [f for f in doc["findings"] if f["file"] == "b.py"]
    assert s["fingerprint"] == code["findings"][0]["fingerprint"]   # same site, same id
    access_log(store, d, LEGS)
    stage3(rv, store, d, LEGS)
    clusters = json.loads((d / "clusters.json").read_text())
    assert {"code", "static"} in [set(c["reviewers"]) for c in clusters["exact_clusters"]]
    stage5(rv, store, d, [rec_item(code["findings"][0], "open", ["code", "static"])])
    text = rv.summary().read_text()
    assert "b.py:2" in text
    rows = {r["reviewer"] for r in map(json.loads, (store / "ledger.jsonl").read_text().splitlines())}
    assert rows == {"code", "static"}
