"""validate's rules, residue's cases and preflight's refusals (plan section 7)."""
import errno
import json
import os

import pytest

from conftest import finding, git, load, run

review = load("review")


# ---------------------------------------------------------------- validate

MANIFEST = {"files": [{"path": "a.py"}, {"path": "src/m.py"}],
            "reference": [{"path": "data.csv", "kind": "tabular"},
                          {"path": "model.pkl", "kind": "opaque"}]}


def doc(**over):
    d = {"reviewer": "code", "machine": {}, "denominators": {},
         "scope": {"files_read": ["a.py", "src/m.py"]},
         "findings": [finding("c1", "a.py", 2)]}
    d.update(over)
    return d


def with_finding(**over):
    f = finding("c1", "a.py", 2)
    for k, v in over.items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = v
    return doc(findings=[f])


def problems(repo, d, manifest=MANIFEST):
    return review.validate_findings(d, dict(manifest, repo=str(repo)) if manifest else None,
                                    str(repo))


def test_a_valid_document_passes(repo):
    assert problems(repo, doc()) == []


CMD = {"kind": "command", "command": "python3 -c 0", "expected": "0", "cost": "cheap",
       "ran": True}

RULES = {
    "missing top-level key": (lambda: {k: v for k, v in doc().items() if k != "machine"},
                              "missing 'machine'"),
    "files_read is a count": (lambda: doc(scope={"files_read": 2}), "must be a LIST"),
    "opaque file read": (lambda: doc(scope={"files_read": ["a.py", "src/m.py", "model.pkl"]}),
                         "OPAQUE"),
    "reference file read": (lambda: doc(scope={"files_read": ["a.py", "src/m.py",
                                                              "data.csv"]}),
                            "reference-tier"),
    "opaque file queried": (lambda: doc(scope={"files_read": ["a.py", "src/m.py"],
                                               "files_queried": ["model.pkl"]}),
                            "opaque file(s) listed in files_queried"),
    "unread file in prose only": (lambda: doc(scope={"files_read": ["a.py"]},
                                              not_reviewed=["I also looked at src/m.py"]),
                                  "SCOPE GAP"),
    "missing finding field": (lambda: with_finding(fix=None), "missing 'fix'"),
    "duplicate id": (lambda: doc(findings=[finding("c1", "a.py", 1),
                                           finding("c1", "a.py", 2)]), "duplicate id"),
    "bad severity": (lambda: with_finding(severity="High"), "severity 'High'"),
    "bad confidence": (lambda: with_finding(confidence="sure"), "confidence 'sure'"),
    "bad class": (lambda: with_finding(**{"class": "bug"}), "class 'bug'"),
    "bad fix_certainty": (lambda: with_finding(fix_certainty="easy"), "fix_certainty 'easy'"),
    "empty claim": (lambda: with_finding(claim="  "), "empty claim"),
    "bad evidence kind": (lambda: with_finding(evidence={"kind": "vibes"}), "evidence.kind"),
    "command without command": (lambda: with_finding(evidence=dict(CMD, command="")),
                                "but no command"),
    "command without expected": (lambda: with_finding(evidence=dict(CMD, expected="")),
                                 "no 'expected'"),
    "command without cost": (lambda: with_finding(evidence={k: v for k, v in CMD.items()
                                                             if k != "cost"}),
                             "cost=cheap|heavy"),
    "command without ran": (lambda: with_finding(evidence={k: v for k, v in CMD.items()
                                                            if k != "ran"}),
                            "ran=true|false"),
    "heavy command run": (lambda: with_finding(evidence=dict(CMD, cost="heavy",
                                                             cost_basis="hours")),
                          "HEAVY command was run"),
    "heavy without basis": (lambda: with_finding(evidence=dict(CMD, cost="heavy", ran=False)),
                            "needs cost_basis"),
    "quote without text": (lambda: with_finding(evidence={"kind": "quote", "quote": ""}),
                           "no 'quote' text"),
    "reasoning certain": (lambda: with_finding(evidence={"kind": "reasoning"},
                                               confidence="certain"),
                          "reasoning evidence cannot be 'certain'"),
    "unrun command certain": (lambda: with_finding(evidence=dict(CMD, ran=False),
                                                   confidence="certain"),
                              "unrun command cannot be 'certain'"),
    "file does not exist": (lambda: with_finding(file="nope.py"), "file does not exist"),
    "line out of range": (lambda: with_finding(line=99), "line 99 out of range"),
}


@pytest.mark.parametrize("rule", sorted(RULES))
def test_each_rule_rejects_its_violation(repo, rule):
    make, message = RULES[rule]
    v = problems(repo, make())
    assert any(message in x for x in v), v


def test_a_manifest_without_files_is_reported(repo):
    assert any("no `files` key" in x for x in problems(repo, doc(), manifest={"reference": []}))


@pytest.mark.parametrize("entry", [
    "src/m.py — out of time",
    {"path": "src/m.py", "reason": "out of time"},
    "`src/m.py` — rendered by the summary"])
def test_a_declared_unread_file_is_not_a_gap(repo, entry):
    assert problems(repo, doc(scope={"files_read": ["a.py"]}, not_reviewed=[entry])) == []


def test_a_path_with_spaces_can_be_declared(repo):
    manifest = {"files": [{"path": "a.py"}, {"path": "outputs - Copy/note.md"}],
                "reference": []}
    d = doc(scope={"files_read": ["a.py"]},
            not_reviewed=["outputs - Copy/note.md — a copy of outputs/"])
    assert problems(repo, d, manifest) == []


def test_validate_exits_1_and_lists_violations(tmp_path, repo):
    p = tmp_path / "f.json"
    p.write_text(json.dumps(with_finding(severity="High")))
    r = run("review.py", "validate", "--findings", p, "--repo", repo, cwd=repo)
    assert r.returncode == 1 and "CONTRACT VIOLATION" in r.stdout


# ---------------------------------------------------------------- residue

def residue(repo, run_dir, *args):
    return run("review.py", "residue", "--repo", repo, "--run-dir", run_dir, *args, cwd=repo)


@pytest.fixture
def snap(tmp_path, repo):
    (repo / ".gitignore").write_text("*.log\ncache/\n")
    (repo / "cache").mkdir()
    (repo / "cache" / "old").write_text("x\n")
    git(repo, "add", ".gitignore")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ignore")
    (repo / "dirty.py").write_text("before\n")      # untracked before the run
    d = tmp_path / "run"
    d.mkdir()
    assert residue(repo, d, "--save").returncode == 0
    return d


@pytest.mark.parametrize("change,expect", [
    (lambda r: (r / "new.txt").write_text("x\n"), "new.txt   untracked file left behind"),
    (lambda r: (r / "run.log").write_text("x\n"), "run.log   ignored file left behind"),
    (lambda r: (r / "a.py").write_text("edited\n"), "a.py   status 'M' (was clean)"),
    (lambda r: (r / "a.py").unlink(), "a.py   status 'D' (was clean)"),
    (lambda r: (r / "dirty.py").write_text("after\n"), "dirty.py   changed during the run"),
])
def test_residue_names_what_changed(repo, snap, change, expect):
    change(repo)
    r = residue(repo, snap)
    assert r.returncode == 1 and expect in r.stdout, r.stdout


def test_residue_survives_a_non_utf8_file_name(repo, snap):
    try:
        (repo / os.fsdecode(b"bad-\xff.txt")).write_text("x\n")
    except OSError as e:
        if e.errno != errno.EILSEQ:
            raise
        pytest.skip("this file system refuses non-UTF-8 names (macOS APFS), so the case cannot arise")
    r = residue(repo, snap)
    assert r.returncode == 1 and "Traceback" not in r.stderr


def test_residue_exempts_the_summary(installed, repo, snap):
    (repo / "REVIEW-SUMMARY.md").write_text("written by summarize\n")
    assert residue(repo, snap).returncode == 0


def test_residue_cannot_see_inside_an_already_ignored_directory(repo, snap):
    # Documented in DESIGN.md: ignored entries are not hashed, and a directory is one entry.
    (repo / "cache" / "new").write_text("left by a reviewer\n")
    assert residue(repo, snap).returncode == 0


# ---------------------------------------------------------------- preflight

def preflight(repo, cwd):
    return run("review.py", "preflight", "--repo", repo, cwd=cwd)


def test_preflight_passes_in_an_installed_repository(installed, repo):
    r = preflight(repo, repo)
    assert r.returncode == 0 and "preflight OK" in r.stdout


@pytest.mark.parametrize("where", ["src", "elsewhere"])
def test_preflight_refuses_a_session_outside_the_root(installed, repo, tmp_path, where):
    cwd = repo / "src" if where == "src" else tmp_path
    r = preflight(repo, cwd)
    assert r.returncode == 2 and "repository's root" in r.stderr


def test_preflight_refuses_without_an_install(repo):
    r = preflight(repo, repo)
    assert r.returncode == 2 and "no install record" in r.stderr


def test_preflight_refuses_when_the_recorded_kit_is_gone(installed, repo):
    cfg_path = review.config_path(repo)
    cfg = json.loads(cfg_path.read_text())
    cfg["kit"] = "/nonexistent/kit"
    cfg_path.write_text(json.dumps(cfg))
    r = preflight(repo, repo)
    assert r.returncode == 2 and "does not exist" in r.stderr


def test_preflight_refuses_without_a_store(installed, repo):
    import shutil
    shutil.rmtree(installed)
    r = preflight(repo, repo)
    assert r.returncode == 2 and "no review store" in r.stderr
