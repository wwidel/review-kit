"""Shared fixtures. Every test gets its own git repository and its own home directory.

Nothing here may touch the real ~/.review-kit or ~/.claude: `home` points HOME at a temporary
directory, both for this process (review.py and the hook work out their paths on each call)
and for every subprocess (install.py works them out when it loads).
"""
import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
BIN = KIT / "bin"


def load(name):
    """Import a script from bin/ as a module, fresh each time."""
    spec = importlib.util.spec_from_file_location(f"kit_{name}", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # dataclasses look their module up while defining
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_path(tmp_path):
    """pytest's tmp_path with symlinks resolved. On macOS it lives under /var, a link to
    /private/var, and the kit reports resolved paths; comparing the two spellings would fail
    there and nowhere else."""
    return tmp_path.resolve()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test, whether it asks for this or not: a temporary HOME, and no git config from
    the machine running the tests (a global `commit.gpgsign` once broke every fixture)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("REVIEW_ACCESS_ROOT", raising=False)
    return h


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A small committed repository: a.py at the root, src/m.py, README.md."""
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    (r / "a.py").write_text("import numpy as np\nx = np.random.rand(3)\n")
    (r / "src" / "m.py").write_text("def f():\n    return 1\n")
    (r / "README.md").write_text("# proj\n")
    git(r, "init", "-q")
    git(r, "add", "-A")
    git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return r


def run(script, *args, cwd=None, stdin=None, env=None):
    """Run a bin/ script under the interpreter running the tests, with HOME as already set."""
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, str(BIN / script), *map(str, args)], cwd=cwd,
                          input=stdin, capture_output=True, text=True, env=e)


@pytest.fixture
def installed(home, repo):
    """`repo` installed with the hook, against the temporary home. Returns its store."""
    r = run("install.py", "--repo", repo, cwd=repo)
    assert r.returncode == 0, r.stderr
    review = load("review")
    return review.store_path(repo)


def now_iso(offset_s=0):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_s)
    return t.isoformat(timespec="seconds")


def hook(payload, cwd, env=None):
    """Feed one PostToolUse payload to the hook script, run from `cwd` as Claude Code would."""
    return run("log_file_access.py", cwd=cwd, stdin=json.dumps(payload), env=env)


def records(store):
    p = store / "access.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def audit_case(tmp_path, repo, declared, recs, agent=None, extra=()):
    """Run `review.py audit` on a findings file declaring `declared` and a log of `recs`.

    The run started a minute ago; each record is stamped now unless it carries its own `t`.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps({"generated": now_iso(-60),
                                                       "repo": str(repo), "files": []}))
    findings = run_dir / "findings-code.json"
    findings.write_text(json.dumps({"reviewer": "code",
                                    "scope": {"files_read": list(declared)}, "findings": []}))
    log = tmp_path / "access.jsonl"
    log.write_text("".join(json.dumps({"t": now_iso(), **r}) + "\n" for r in recs))
    args = ["audit", "--repo", repo, "--access-log", log, "--findings", findings,
            "--run-dir", run_dir, *extra]
    if agent:
        args += ["--agent", agent]
    return run("review.py", *args, cwd=repo)


# ---------------------------------------------------------------- bookkeeping fixtures

def finding(fid, file, line, claim="something is wrong here", severity="major"):
    """A finding that satisfies the contract in FINDINGS.md."""
    return {"id": fid, "claim": claim, "file": file, "line": line,
            "evidence": {"kind": "quote", "quote": "x"}, "severity": severity,
            "confidence": "likely", "class": "defect", "fix": "change it",
            "fix_certainty": "judgment"}


def make_run(root, run_id, repo, read=None, findings=None, mode="full", files=None):
    """A run directory with a manifest over every tracked file and fingerprinted findings.

    `read` maps a reviewer leg to the files it declares read; `findings` maps a leg to its
    findings. Fingerprints are stamped by the real `review.py fingerprint`.
    """
    review = load("review")
    d = root / run_id
    d.mkdir(parents=True)
    tracked = files if files is not None else git(repo, "ls-files").stdout.split()
    manifest = {"repo": str(repo), "generated": now_iso(-60), "mode": mode, "reference": [],
                "files": [{"path": p, "sha256": review.sha256_of(repo / p),
                           "lines": len((repo / p).read_text().splitlines())} for p in tracked]}
    (d / "manifest.json").write_text(json.dumps(manifest))
    for leg in sorted(set(read or {}) | set(findings or {})):
        doc = {"reviewer": leg, "machine": {}, "denominators": {},
               "scope": {"files_read": (read or {}).get(leg, [])},
               "findings": (findings or {}).get(leg, [])}
        p = d / f"findings-{leg}.json"
        p.write_text(json.dumps(doc))
        r = run("review.py", "fingerprint", "--findings", p, "--manifest", d / "manifest.json")
        assert r.returncode == 0, r.stderr
    return d


def fingerprints(run_dir, leg):
    return [f["fingerprint"] for f in
            json.loads((run_dir / f"findings-{leg}.json").read_text())["findings"]]


def item(fp, where, disposition="open", severity="major", corroborated_by=("code",)):
    return {"fingerprint": fp, "disposition": disposition, "severity": severity,
            "class": "defect", "title": f"defect at {where}", "where": where,
            "defect": "it is wrong", "fix": "change it",
            "corroborated_by": list(corroborated_by)}


def write_reconciliation(run_dir, items, run_id=None):
    p = run_dir / "reconciliation.json"
    p.write_text(json.dumps({"run_id": run_id or run_dir.name, "date": "2026-10-06",
                             "run_dir": str(run_dir), "machine": {}, "items": items}))
    return p
