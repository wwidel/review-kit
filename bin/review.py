#!/usr/bin/env python3
"""Deterministic machinery for the review kit.

Everything here is bookkeeping an agent should never be asked to do by hand: scoping,
schema validation, scope-honesty checking, stable fingerprints, clustering candidates,
recurrence matching, summary rendering and the ledger. Agents find defects, verify
defects and decide fixes; this script does the rest.

Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, shutil, subprocess, sys, datetime, warnings
from pathlib import Path

SEVERITIES = ["critical", "major", "minor", "trivial"]
CONFIDENCES = ["certain", "likely", "speculative"]
CLASSES = ["defect", "hygiene"]
EVIDENCE_KINDS = ["command", "quote", "reasoning"]
FIX_CERTAINTIES = ["mechanical", "judgment"]

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "__pycache__",
             ".venv", "venv", ".mypy_cache", ".pytest_cache", ".review", ".ipynb_checkpoints",
             ".claude", ".idea", ".vscode"}
SOURCE_EXT = {".py", ".pyx", ".sh", ".R", ".r", ".Rmd", ".jl", ".sql",
              ".md", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".json", ".txt",
              ".ipynb", ".csv", ".tsv"}

# Source code and prose: always review tier, whatever their length. Demoting a long script
# to "describe, do not read" hides exactly the file most worth reading.
CODE_EXT = {".py", ".pyx", ".sh", ".R", ".r", ".Rmd", ".jl", ".sql"}

# Tabular data: described, and interrogated with a script rather than read.
TABULAR_EXT = {".csv", ".tsv", ".parquet", ".jsonl"}

# Files no reviewer can read as text and no short script can usefully query: decks, documents,
# spreadsheets, images, archives and model artefacts. They are still listed, because a reviewer
# that does not know `results.pptx` exists cannot say the numbers in it went unchecked — and a
# directory full of committed .pkl files is itself worth seeing (C-6).
OPAQUE_EXT = {
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".odt", ".odp", ".ods", ".rtf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tiff", ".ico",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pkl", ".pickle", ".joblib", ".pth", ".pt", ".ckpt", ".h5", ".hdf5", ".onnx", ".npy",
    ".npz", ".bin", ".so", ".dll", ".dylib", ".model", ".safetensors",
    ".mp4", ".mov", ".wav", ".mp3", ".ttf", ".woff", ".woff2",
}
SOURCE_EXT = SOURCE_EXT | OPAQUE_EXT


def read_json(path, what):
    """Read a required JSON input, or fail with a sentence.

    A traceback tells an agent following a skill nothing it can act on, and these are the
    inputs most likely to be missing: a stage that did not run, or a path typed wrong.
    """
    p = Path(path)
    if not p.exists():
        die(f"no {what} at {p}. Has the stage that writes it run yet?")
    try:
        return json.loads(p.read_text())
    except ValueError as e:
        die(f"{p} is not valid JSON ({e}).")


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def count_lines(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------- scope

def iter_source(repo: Path):
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() in SOURCE_EXT:
                yield p


DATA_EXT = {".csv", ".tsv", ".json", ".jsonl", ".parquet"}


def describe_reference(p: Path, rel: str):
    """Enough for a reviewer to know what is in a file without reading it."""
    opaque = p.suffix.lower() in OPAQUE_EXT
    d = {"path": rel, "sha256": sha256_of(p), "bytes": p.stat().st_size,
         "kind": "opaque" if opaque else "tabular"}
    if opaque:
        d["note"] = ("cannot be read as text or queried by a short script. Do NOT attempt to "
                     "read it. Note its existence, and say so in not_reviewed if its content "
                     "bears on a finding — a deck or PDF is where uncited numbers hide.")
        return d
    d["lines"] = count_lines(p)
    if p.suffix.lower() in (".csv", ".tsv"):
        sep = "\t" if p.suffix.lower() == ".tsv" else ","
        try:
            with p.open(errors="replace") as f:
                head = f.readline().rstrip("\n")
                first = f.readline().rstrip("\n")
            d["columns"] = [c.strip() for c in head.split(sep)]
            d["header"] = head[:300]
            d["first_row"] = first[:300]
            d["rows"] = max(d["lines"] - 1, 0)
        except OSError:
            pass
    return d


def cmd_scope(a):
    repo = Path(a.repo).resolve()
    if not repo.is_dir():
        die(f"no such repo: {repo}")
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    mode, base, paths = "full", None, None
    if a.since:
        rc, o, e = sh(f"git rev-parse --verify {a.since}^{{commit}}", cwd=repo)
        if rc:
            die(f"cannot resolve --since {a.since}: {e}")
        base, mode = o, "since"
        # `base` not `base..HEAD`: the working tree is included deliberately. On a live repo
        # the uncommitted change is usually the thing most worth reviewing, and excluding it
        # would silently review a state nobody is currently in.
        rc, o, _ = sh(f"git diff --name-only {base}", cwd=repo)
        paths = [repo / x for x in o.splitlines() if x.strip()]
        paths = [p for p in paths if p.exists() and p.suffix.lower() in SOURCE_EXT]

    if paths is None:
        paths = sorted(iter_source(repo))

    rc, head, _ = sh("git rev-parse --short HEAD", cwd=repo)
    review, reference, oversized = [], [], []
    for p in sorted(paths):
        rel = str(p.relative_to(repo))
        n = 0 if p.suffix.lower() in OPAQUE_EXT else count_lines(p)
        # Tabular data is reference-tier at any size, and an opaque file can never be read
        # at all; .json is neither, because a 20-line config is something a reviewer must
        # actually read. Length decides for everything else.
        ext = p.suffix.lower()
        is_code = p.suffix in CODE_EXT or ext in {e.lower() for e in CODE_EXT}
        if is_code and n > a.reference_threshold:
            oversized.append((rel, n))
        if ext in TABULAR_EXT or ext in OPAQUE_EXT or (n > a.reference_threshold
                                                       and not is_code):
            reference.append(describe_reference(p, rel))
        else:
            review.append({"path": rel, "sha256": sha256_of(p), "lines": n})

    manifest = {
        "repo": str(repo), "mode": mode, "base": base, "head": head or None,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "reference_threshold": a.reference_threshold,
        "files": review,          # the review tier; kept under this key for compatibility
        "reference": reference,
        "total_files": len(review),
        "total_lines": sum(f["lines"] for f in review),
        "reference_files": len(reference),
        "reference_lines": sum(f.get("lines", 0) for f in reference),
        "opaque_files": sum(1 for f in reference if f.get("kind") == "opaque"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    n_opaque = manifest["opaque_files"]
    n_tab = len(reference) - n_opaque
    print(f"scope: {len(review)} files to READ ({manifest['total_lines']} lines), "
          f"{n_tab} to QUERY ({manifest['reference_lines']} lines), "
          f"{n_opaque} opaque (listed, unreadable), "
          f"mode={mode}" + (f", base={base[:8]}" if base else ""))
    print(f"manifest: {out / 'manifest.json'}")
    if a.list:
        print("  -- review tier (read in full) --")
        for f in review:
            print(f"  {f['path']}  ({f['lines']} lines)")
        tab = [f for f in reference if f.get("kind") != "opaque"]
        opq = [f for f in reference if f.get("kind") == "opaque"]
        if tab:
            print("  -- reference tier: query with a script, do not read --")
            for f in tab:
                cols = f.get("columns")
                print(f"  {f['path']}  ({f.get('lines', 0)} lines"
                      + (f", {len(cols)} cols: {','.join(cols[:6])}" if cols else "") + ")")
        if opq:
            print("  -- opaque: cannot be read or queried; note that they exist --")
            for f in opq:
                print(f"  {f['path']}  ({f['bytes']} bytes)")
    if oversized:
        print(f"  note: {len(oversized)} source file(s) exceed {a.reference_threshold} lines "
              "and are STILL in the review tier — read them, but consider splitting the run:",
              file=sys.stderr)
        for path, n in sorted(oversized, key=lambda x: -x[1]):
            print(f"    {n:>6} lines  {path}", file=sys.stderr)
    if not review:
        print("EMPTY SCOPE — do not review nothing.", file=sys.stderr)
        sys.exit(3)


# ---------------------------------------------------------------- machine

def cmd_machine(a):
    cores = os.cpu_count() or 1
    usable_gb = total_gb = None
    try:
        with open("/proc/meminfo") as f:
            mi = {l.split(":")[0]: l.split()[1] for l in f if ":" in l}
        usable_gb = round(int(mi.get("MemAvailable", 0)) / 1048576, 1)
        total_gb = round(int(mi.get("MemTotal", 0)) / 1048576, 1)
    except (OSError, ValueError, IndexError):
        pass
    st = os.statvfs(".")
    free_disk_gb = round(st.f_bavail * st.f_frsize / 1024**3, 1)
    rc, gpu, _ = sh("nvidia-smi -L")
    gpus = [l for l in gpu.splitlines() if l.strip()] if rc == 0 else []

    frameworks = {}
    for mod in ("torch", "numpy", "pandas", "scipy", "polars", "sklearn", "lightgbm", "yaml"):
        rc, _, _ = sh(f"{sys.executable} -c 'import {mod}'")
        frameworks[mod] = rc == 0

    m = {"cores": cores, "usable_gb": usable_gb, "total_gb": total_gb,
         "gpu": gpus or None, "free_disk_gb": free_disk_gb, "importable": frameworks}
    if a.out:
        Path(a.out).write_text(json.dumps(m, indent=2))
    print(json.dumps(m, indent=2))


# ---------------------------------------------------------------- characterize

def cmd_characterize(a):
    repo = Path(a.repo).resolve()
    def has(pat):
        rc, o, _ = sh(f"ls -d {pat} 2>/dev/null | head -1", cwd=repo)
        return bool(o)
    test_files = [p for p in iter_source(repo)
                  if p.name.startswith("test_") or p.name.endswith("_test.py")
                  or "tests" in p.relative_to(repo).parts]
    rc, last, _ = sh("git log -1 --format=%cI", cwd=repo)
    rc2, n, _ = sh("git rev-list --count HEAD", cwd=repo)
    age_days = None
    if last:
        try:
            d = datetime.datetime.fromisoformat(last)
            age_days = (datetime.datetime.now(datetime.timezone.utc) - d).days
        except ValueError:
            pass
    linters = [f for f in ("ruff.toml", ".ruff.toml", "setup.cfg", "pyproject.toml",
                           ".flake8", "mypy.ini", ".pre-commit-config.yaml") if (repo / f).exists()]
    ci = has(".github/workflows") or (repo / ".gitlab-ci.yml").exists()

    signals = {
        "test_files": len(test_files), "linter_config": linters, "ci": bool(ci),
        "commits": int(n) if n.isdigit() else None,
        "last_commit": last or None, "days_since_last_commit": age_days,
    }
    frozen_score = sum([len(test_files) == 0, not linters, not ci,
                        (signals["commits"] or 99) < 5,
                        (age_days or 0) > 365])
    signals["posture"] = "likely-frozen" if frozen_score >= 4 else (
        "likely-live" if frozen_score <= 1 else "ambiguous")
    signals["posture_basis"] = (f"{frozen_score}/5 frozen signals; this is a HINT, not a "
                                "finding — the human states the repo's posture, and a live "
                                "repo is the default assumption for this kit")
    if a.out:
        Path(a.out).write_text(json.dumps(signals, indent=2))
    print(json.dumps(signals, indent=2))


# ---------------------------------------------------------------- probe

# (id, rule, description, regex). Deliberately over-inclusive: a probe hit is a place to
# LOOK, never a finding. Precision is the reviewer's job; recall is this table's job.
REGEX_PROBES = [
    ("seeds", "T-7", "every seeding call anywhere in the repo — if this list is short, "
     "reproducibility is not established",
     r"\b(?:random\.seed|manual_seed|seed_everything|np\.random\.seed|random_state|"
     r"cudnn\.deterministic|worker_init_fn|torch\.use_deterministic)"),
    ("unsafe-deserialize", "C-6", "deserialisation that executes arbitrary code",
     r"\b(?:pickle\.loads?|marshal\.loads?|joblib\.load|torch\.load|yaml\.load)\s*\("),
    ("broad-except", "E-4", "exception handling wide enough to hide an unrelated error",
     r"except\s*:|except\s+(?:Exception|BaseException)\s*[:(]"),
    ("eval-exec", "C-6", "eval/exec on data that may come from config or input",
     r"(?<![\w.])(?:eval|exec)\s*\("),
    ("shell-true", "C-6", "subprocess with shell=True", r"shell\s*=\s*True"),
    ("tls-off", "C-6", "certificate verification disabled",
     r"verify\s*=\s*False|rejectUnauthorized|NODE_TLS_REJECT_UNAUTHORIZED"),
    ("credentials", "D-7", "a literal that looks like a secret",
     r"(?i)\b(?:api_?key|password|passwd|secret|token|access_key)\s*=\s*[\'\"][^\'\"]{6,}"),
    ("mutable-default", None, "mutable default argument",
     r"def\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\}|set\(\))"),
    ("dynamic-dispatch", "E-4", "name resolved from globals()/getattr — a config typo becomes "
     "a TypeError rather than a named error, and any module symbol becomes callable",
     r"globals\(\)\s*\.\s*get|getattr\s*\(\s*(?:sys\.modules|globals)"),
    ("fit-sites", "T-4", "every .fit / fit_transform — check each is inside the fold loop",
     r"\.(?:fit|fit_transform|partial_fit)\s*\("),
    ("eager-read", "D-2", "a full read where a scan or chunked read may be needed",
     r"\b(?:read_parquet|read_csv|read_json|readlines|\.read\(\))"),
    ("nonatomic-write", "C-7", "a write straight to its final path — no write-then-rename",
     r"\b(?:to_csv|to_parquet|torch\.save|np\.save|json\.dump|pickle\.dump)\s*\("),
    ("naive-datetime", "D-6", "a datetime constructed without a timezone",
     r"datetime\.(?:now|utcnow|fromtimestamp)\s*\(\s*\)|datetime\.datetime\s*\([^)]*\)"),
    ("index-escape", None, "escaping index alignment — pairs rows positionally",
     r"\.(?:values|to_numpy|iat|iloc)\b"),
    ("merge-join", None, "a merge/join — check the row count is asserted afterwards",
     r"\.(?:merge|join)\s*\(|pd\.merge\s*\("),
    ("assert-in-src", None, "assert outside tests — stripped under python -O",
     r"^\s*assert\s"),
    ("todo", None, "unfinished work markers", r"\b(?:TODO|FIXME|HACK|XXX)\b"),
    ("magic-number", "C-5", "a literal used inline rather than as a named constant: a 3+ digit "
     "integer, or a decimal carried to 2+ places — the signature of a tuned threshold",
     r"(?<![\w.])\d{3,}(?![\w.])|(?<![\w.])\d*\.\d{2,}(?![\w.])"),
    ("path-literal", "D-5", "a path literal outside a paths module",
     r"[\'\"](?:\.{0,2}/)?(?:[\w.-]+/){1,}[\w.-]+[\'\"]"),
    ("open-no-with", None, "open() not in a with-statement", r"(?<!with\s)\bopen\s*\("),
]

MAX_HITS = 25


def cmd_probe(a):
    import ast as _ast
    man = read_json(a.manifest, "manifest")
    # The manifest already records which repo it describes. Trusting it rather than the
    # caller's cwd means probe cannot be pointed at the wrong tree by accident.
    repo = Path(man.get("repo") or a.repo or ".").resolve()
    files = [repo / f["path"] for f in man["files"]]
    texts = {}
    for p in files:
        try:
            texts[p] = p.read_text(errors="replace").splitlines()
        except OSError:
            pass

    probes = []
    for pid, rule, desc, pat in REGEX_PROBES:
        rx = re.compile(pat)
        hits, matched = [], {}
        for p, lines in texts.items():
            if p.suffix not in (".py", ".sh", ".yml", ".yaml"):
                continue
            for i, line in enumerate(lines, 1):
                m = rx.search(line)
                if m:
                    hits.append({"file": str(p.relative_to(repo)), "line": i,
                                 "text": line.strip()[:160], "match": m.group(0)[:40]})
                    matched.setdefault(m.group(0)[:40], 0)
                    matched[m.group(0)[:40]] += 1
        # A probe with hundreds of hits is truncated, and an arbitrary first 25 is not much
        # use. The distinct matched substrings are: 106 occurrences of a magic number may be
        # 30 distinct constants, and it is the constants a reviewer needs to see.
        probes.append({"probe": pid, "rule_id": rule, "description": desc,
                       "total_hits": len(hits), "hits": hits[:MAX_HITS],
                       "truncated": len(hits) > MAX_HITS,
                       "distinct_matches": len(matched),
                       "matches": dict(sorted(matched.items(), key=lambda kv: -kv[1])[:60])})

    # --- AST probes: things a regex cannot see ---
    unparseable, dup_names, bodies = [], [], {}
    for p, lines in texts.items():
        if p.suffix != ".py":
            continue
        src = "\n".join(lines)
        try:
            # compile(), not ast.parse(): ast.parse accepts a misplaced `from __future__`
            # import that the real compiler rejects, so a file that cannot run would
            # otherwise be reported as fine.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(src, str(p), "exec")
        except SyntaxError as e:
            unparseable.append({"file": str(p.relative_to(repo)), "line": e.lineno,
                                "text": f"{type(e).__name__}: {e.msg}"})
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = _ast.parse(src)
        except SyntaxError:
            continue
        # Redefinition only counts within ONE scope. `__init__` in two classes is not a
        # redefinition; walking the whole tree flat would report every method of every class.
        def scopes(n):
            yield n
            for child in _ast.iter_child_nodes(n):
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                    yield from scopes(child)
        for scope in scopes(tree):
            seen = {}
            for node in getattr(scope, "body", []):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                    if node.name in seen:
                        where = getattr(scope, "name", "module level")
                        dup_names.append(
                            {"file": str(p.relative_to(repo)), "line": node.lineno,
                             "text": f"{node.name} redefined in {where} (first at line "
                                     f"{seen[node.name]}) — the earlier definition is dead"})
                    seen[node.name] = node.lineno
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                try:
                    body = _ast.dump(_ast.Module(body=node.body, type_ignores=[]))
                except TypeError:
                    continue
                if len(body) > 200:
                    bodies.setdefault(hashlib.sha1(body.encode()).hexdigest(), []).append(
                        f"{p.relative_to(repo)}:{node.lineno} {node.name}")
    twins = [{"file": v[0].split(":")[0], "line": int(v[0].split(":")[1].split()[0]),
              "text": "identical body: " + " | ".join(v)}
             for v in bodies.values() if len(v) > 1]

    for pid, rule, desc, hits in [
        ("unparseable", None, "files that do not parse — nothing else can be trusted about them",
         unparseable),
        ("redefined-name", "C-4", "a name defined twice in one module; the first is dead and a "
         "change to it silently has no effect", dup_names),
        ("identical-bodies", "C-4", "two functions with byte-identical bodies — one code path "
         "per concept, or a copy that will diverge", twins),
    ]:
        probes.append({"probe": pid, "rule_id": rule, "description": desc,
                       "total_hits": len(hits), "hits": hits[:MAX_HITS],
                       "truncated": len(hits) > MAX_HITS,
                       "distinct_matches": len(hits), "matches": {}})

    out = {"repo": str(repo), "files_probed": len(texts),
           "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "A probe hit is a place to LOOK, not a finding. These are deliberately "
                   "over-inclusive; most hits are fine. Never file a finding because a probe "
                   "matched — read the code and decide. Never file one WITHOUT reading it.",
           "max_hits_per_probe": MAX_HITS, "probes": probes}
    Path(a.out).write_text(json.dumps(out, indent=2))
    nz = [p for p in probes if p["total_hits"]]
    print(f"probe: {len(texts)} files, {len(probes)} probes, {len(nz)} with hits "
          f"({sum(p['total_hits'] for p in probes)} total) → {a.out}")
    for pr in sorted(nz, key=lambda x: -x["total_hits"]):
        extra = (f"  ({pr['distinct_matches']} distinct)"
                 if pr.get("truncated") and pr.get("matches") else "")
        print(f"  {pr['probe']:20} {pr['total_hits']:4}"
              + (f"  [{pr['rule_id']}]" if pr["rule_id"] else "") + extra)


# ---------------------------------------------------------------- validate

REQUIRED_TOP = ["reviewer", "scope", "machine", "denominators", "findings"]
REQUIRED_FINDING = ["id", "claim", "evidence", "severity", "confidence", "class",
                    "fix", "fix_certainty", "file"]


def validate_findings(doc, manifest=None, repo=None):
    v = []
    for k in REQUIRED_TOP:
        if k not in doc:
            v.append(f"top-level: missing '{k}'")
    scope = doc.get("scope", {})
    read = scope.get("files_read")
    if not isinstance(read, list):
        v.append("scope.files_read must be a LIST of paths, not a count "
                 "(a count cannot be checked against the manifest)")
        read = []
    if manifest:
        refset = {f["path"] for f in manifest.get("reference", [])}
        inscope = {f["path"] for f in manifest["files"]}
        unread = inscope - set(read)
        bogus = set(read) - inscope
        if bogus:
            # Informational, never a violation. A reviewer legitimately opens things outside
            # the review window — committed data files, a config it needs to interpret, the
            # kit's own contract. Penalising that would teach it to under-report what it read,
            # which is the opposite of what scope.files_read exists for.
            opaqueset = {f["path"] for f in manifest.get("reference", [])
                         if f.get("kind") == "opaque"}
            inopq = sorted(bogus & opaqueset)
            inref = sorted((bogus & refset) - opaqueset)
            other = sorted(bogus - refset)
            if inopq:
                v.append(f"{len(inopq)} OPAQUE file(s) listed in files_read: "
                         + ", ".join(inopq[:4]) + ("\u2026" if len(inopq) > 4 else "")
                         + ". Decks, PDFs, spreadsheets, images and model artefacts cannot be "
                           "read as text. Note them in not_reviewed instead \u2014 their content "
                           "is unchecked and saying so is the useful act.")
            if inref:
                v.append(f"{len(inref)} reference-tier file(s) listed in files_read: "
                         + ", ".join(inref[:4]) + ("…" if len(inref) > 4 else "")
                         + ". Reference files are described in the manifest and QUERIED with a "
                           "script, never read into context — that is what the tier is for. "
                           "Put them in scope.files_queried instead.")
            if other:
                print(f"  note: {len(other)} file(s) read outside the manifest "
                      f"(not a violation): " + ", ".join(other[:4])
                      + ("…" if len(other) > 4 else ""), file=sys.stderr)
        queried = set(scope.get("files_queried") or [])
        opq = {f["path"] for f in manifest.get("reference", []) if f.get("kind") == "opaque"}
        if queried & opq:
            v.append(f"{len(queried & opq)} opaque file(s) listed in files_queried: "
                     + ", ".join(sorted(queried & opq)[:4])
                     + ". An opaque file cannot be queried either; it goes in not_reviewed.")
        nr = " ".join(str(x) for x in doc.get("not_reviewed", []))
        undeclared = [p for p in sorted(unread) if p not in nr]
        if undeclared:
            v.append(f"SCOPE GAP: {len(unread)} of {len(inscope)} in-scope files not read, "
                     f"and {len(undeclared)} of those are absent from not_reviewed: "
                     + ", ".join(undeclared[:6]) + ("…" if len(undeclared) > 6 else ""))

    ids = set()
    for i, f in enumerate(doc.get("findings", [])):
        tag = f.get("id", f"#{i}")
        for k in REQUIRED_FINDING:
            if k not in f:
                v.append(f"{tag}: missing '{k}'")
        if f.get("id") in ids:
            v.append(f"{tag}: duplicate id")
        ids.add(f.get("id"))
        if f.get("severity") not in SEVERITIES:
            v.append(f"{tag}: severity {f.get('severity')!r} not in {SEVERITIES}")
        if f.get("confidence") not in CONFIDENCES:
            v.append(f"{tag}: confidence {f.get('confidence')!r} not in {CONFIDENCES}")
        if f.get("class") not in CLASSES:
            v.append(f"{tag}: class {f.get('class')!r} not in {CLASSES}")
        if f.get("fix_certainty") not in FIX_CERTAINTIES:
            v.append(f"{tag}: fix_certainty {f.get('fix_certainty')!r} not in {FIX_CERTAINTIES}")
        if not str(f.get("claim", "")).strip():
            v.append(f"{tag}: empty claim")
        e = f.get("evidence") or {}
        kind = e.get("kind")
        if kind not in EVIDENCE_KINDS:
            v.append(f"{tag}: evidence.kind {kind!r} not in {EVIDENCE_KINDS}")
        if kind == "command":
            if not str(e.get("command", "")).strip():
                v.append(f"{tag}: evidence.kind=command but no command")
            if not str(e.get("expected", "")).strip():
                v.append(f"{tag}: evidence.kind=command but no 'expected'")
            if e.get("cost") not in ("cheap", "heavy"):
                v.append(f"{tag}: command evidence needs cost=cheap|heavy")
            if "ran" not in e:
                v.append(f"{tag}: command evidence needs ran=true|false")
            if e.get("cost") == "heavy" and e.get("ran"):
                v.append(f"{tag}: HEAVY command was run — forbidden by M-4")
            if e.get("cost") == "heavy" and not str(e.get("cost_basis", "")).strip():
                v.append(f"{tag}: heavy command needs cost_basis")
        if kind == "quote" and not str(e.get("quote", "")).strip():
            v.append(f"{tag}: evidence.kind=quote but no 'quote' text")
        if kind == "reasoning" and f.get("confidence") == "certain":
            v.append(f"{tag}: reasoning evidence cannot be 'certain' (cap is 'likely')")
        if kind == "command" and e.get("ran") is False and f.get("confidence") == "certain":
            v.append(f"{tag}: unrun command cannot be 'certain' (cap is 'likely')")
        if repo and f.get("file") and f.get("line"):
            p = Path(repo) / f["file"]
            if not p.exists():
                v.append(f"{tag}: file does not exist: {f['file']}")
            else:
                n = count_lines(p)
                if isinstance(f["line"], int) and not (1 <= f["line"] <= max(n, 1)):
                    v.append(f"{tag}: line {f['line']} out of range (file has {n})")
    return v


def cmd_validate(a):
    doc = read_json(a.findings, "findings file")
    manifest = read_json(a.manifest, "manifest") if a.manifest else None
    repo = manifest["repo"] if manifest else a.repo
    v = validate_findings(doc, manifest, repo)
    name = doc.get("reviewer", Path(a.findings).stem)
    if not v:
        print(f"validate {name}: OK — {len(doc.get('findings', []))} findings, "
              f"{len(doc.get('scope', {}).get('files_read', []))} files read")
        return
    print(f"validate {name}: {len(v)} CONTRACT VIOLATION(S)")
    for x in v:
        print(f"  - {x}")
    sys.exit(1)


# ---------------------------------------------------------------- fingerprint

COMMENT = re.compile(r"(?<!['\"])#.*$")


def normalize_anchor(text: str, strip_comments: bool = True) -> str:
    """Collapse a source line to a comparable form.

    `strip_comments` is off for prose: '#' starts a comment in Python and a heading in
    Markdown, and stripping it there would erase the line rather than normalise it.
    """
    t = COMMENT.sub("", text) if strip_comments else text
    t = re.sub(r"\s+", " ", t).strip()
    return t


CODE_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini"}


def finding_fingerprint(f, repo: Path):
    """Stable across line moves and reruns; changes when the offending code changes.

    Deliberately does NOT include rule_id: two reviewers looking at the same defect
    routinely attribute it to different rules (or to none), and including the rule
    stops them colliding, which is the whole point. Over-clustering is the safe
    direction — the reconciler can split a cluster it disagrees with, but it cannot
    merge two findings it was never shown together.
    """
    path = f.get("file") or ""
    anchor, basis = "", "claim-slug"
    line = f.get("line")
    p = repo / path if path else None
    if p and p.exists() and isinstance(line, int):
        try:
            lines = p.read_text(errors="replace").splitlines()
            if 1 <= line <= len(lines):
                window = lines[max(0, line - 1): line + 1]
                strip = p.suffix.lower() in CODE_SUFFIXES
                anchor = " ".join(normalize_anchor(x, strip) for x in window).strip()
                basis = "source-anchor"
        except OSError:
            pass
    if not anchor:
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", f.get("claim", ""))
        anchor = " ".join(words[:8]).lower()
    key = f"{path}|{anchor}"
    return hashlib.sha1(key.encode()).hexdigest()[:12], basis, anchor


def cmd_fingerprint(a):
    repo = repo_from(manifest=a.manifest, override=a.repo)
    p = Path(a.findings)
    doc = read_json(p, "findings file")
    weak = 0
    for f in doc.get("findings", []):
        fp, basis, anchor = finding_fingerprint(f, repo)
        f["fingerprint"] = fp
        f["fingerprint_basis"] = basis
        # The anchor TEXT, not just its hash. A hash cannot be reversed, and without the text
        # a later run cannot ask the one question that matters: is this defect still here?
        f["fingerprint_anchor"] = anchor
        f["fingerprint_line"] = f.get("line")
        weak += basis != "source-anchor"
    p.write_text(json.dumps(doc, indent=2))
    print(f"fingerprint {doc.get('reviewer')}: {len(doc.get('findings', []))} stamped, "
          f"{weak} weak (claim-slug, no source anchor)  [repo: {repo}]")


# ---------------------------------------------------------------- cluster

def cmd_cluster(a):
    run = Path(a.run_dir).resolve()
    docs = []
    for p in sorted(run.glob("findings-*.json")):
        docs.append(read_json(p, "findings file"))
    if not docs:
        die(f"no findings-*.json in {run}. Clustering nothing would write an empty file that "
            "later stages would read as 'no reviewer found anything'.")
    by_fp, all_f = {}, []
    for d in docs:
        for f in d.get("findings", []):
            f["_reviewer"] = d.get("reviewer")
            all_f.append(f)
            by_fp.setdefault(f.get("fingerprint"), []).append(f)

    clusters, singles = [], []
    for fp, group in by_fp.items():
        (clusters if len(group) > 1 else singles).append((fp, group))

    # proximity candidates: same file, lines within N, different fingerprints
    prox = []
    byfile = {}
    for f in all_f:
        byfile.setdefault(f.get("file"), []).append(f)
    for path, group in byfile.items():
        g = [f for f in group if isinstance(f.get("line"), int)]
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g[i].get("fingerprint") == g[j].get("fingerprint"):
                    continue
                if g[i]["_reviewer"] == g[j]["_reviewer"]:
                    continue
                if abs(g[i]["line"] - g[j]["line"]) <= a.window:
                    prox.append((g[i], g[j]))

    cross = [(fp, g) for fp, g in clusters if len({f["_reviewer"] for f in g}) > 1]
    intra = [(fp, g) for fp, g in clusters if len({f["_reviewer"] for f in g}) == 1]
    out = {
        "intra_reviewer_duplicates": [
            {"fingerprint": fp, "reviewer": g[0]["_reviewer"], "ids": [f["id"] for f in g],
             "file": g[0].get("file"), "line": g[0].get("line")}
            for fp, g in intra],
        "exact_clusters": [
            {"fingerprint": fp,
             "reviewers": sorted({f["_reviewer"] for f in g}),
             "ids": [f["id"] for f in g],
             "file": g[0].get("file"), "line": g[0].get("line"),
             "claim": g[0].get("claim")}
            for fp, g in sorted(cross, key=lambda x: -len(x[1]))],
        "proximity_candidates": [
            {"a": {"id": x["id"], "reviewer": x["_reviewer"], "line": x["line"]},
             "b": {"id": y["id"], "reviewer": y["_reviewer"], "line": y["line"]},
             "file": x.get("file")}
            for x, y in prox],
        "total_findings": len(all_f),
        "distinct_fingerprints": len(by_fp),
    }
    (run / "clusters.json").write_text(json.dumps(out, indent=2))
    print(f"cluster: {len(all_f)} findings → {len(by_fp)} distinct fingerprints; "
          f"{len(cross)} corroborated across reviewers; "
          f"{len(intra)} same-reviewer duplicates; "
          f"{len(prox)} proximity candidates to judge by hand")
    print(f"→ {run / 'clusters.json'}")


# ---------------------------------------------------------------- match (recurrence)

def cmd_match(a):
    cur = Path(a.run_dir).resolve()
    prev = Path(a.prev_run).resolve() if a.prev_run else None
    curfp = {}
    for p in sorted(cur.glob("findings-*.json")):
        for f in json.loads(p.read_text()).get("findings", []):
            curfp[f.get("fingerprint")] = f
    prevfp = {}
    if prev and prev.exists():
        for p in sorted(prev.glob("findings-*.json")):
            for f in json.loads(p.read_text()).get("findings", []):
                prevfp[f.get("fingerprint")] = f
    recurring = sorted(set(curfp) & set(prevfp))
    new = sorted(set(curfp) - set(prevfp))
    gone = sorted(set(prevfp) - set(curfp))
    out = {"recurring": [{"fingerprint": k, "file": curfp[k].get("file"),
                          "claim": curfp[k].get("claim")} for k in recurring],
           "new": len(new), "resolved_or_absent": len(gone),
           "prev_run": str(prev) if prev else None}
    (cur / "recurrence.json").write_text(json.dumps(out, indent=2))
    print(f"match: {len(recurring)} recurring, {len(new)} new, {len(gone)} gone since "
          f"{prev.name if prev else 'no prior run'}")
    for k in recurring[:10]:
        print(f"  STILL OPEN: {curfp[k].get('file')} — {curfp[k].get('claim', '')[:80]}")


# ---------------------------------------------------------------- coverage + carryover

def repo_from(run_dir=None, manifest=None, override=None):
    """Resolve the repository without depending on the caller's cwd.

    Every artefact this script writes records the repo it describes, so no later command
    should need telling again — and one that does will sooner or later be pointed at the
    wrong tree from the wrong directory, silently.
    """
    if override:
        return Path(override).resolve()
    for cand in (manifest, (Path(run_dir) / "manifest.json") if run_dir else None):
        if cand and Path(cand).exists():
            try:
                r = json.loads(Path(cand).read_text()).get("repo")
                if r and Path(r).is_dir():
                    return Path(r).resolve()
            except ValueError:
                pass
    return Path(".").resolve()


def _load_json(p, default=None):
    p = Path(p)
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text())
    except ValueError as e:
        die(f"{p} is not valid JSON: {e}")


def anchor_present(repo: Path, rel: str, anchor: str) -> bool:
    """Is this normalised source anchor still somewhere in the file?

    Position-independent on purpose: the defect has not been fixed merely because the lines
    around it moved. Only a change to the offending text itself counts.
    """
    if not anchor:
        return False
    p = repo / rel
    if not p.exists():
        return False
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return False
    strip = p.suffix.lower() in CODE_SUFFIXES
    for i in range(len(lines)):
        window = " ".join(normalize_anchor(x, strip) for x in lines[i:i + 2]).strip()
        if window and window == anchor:
            return True
    return False


def resolve_fingerprints(rec, run_dir: Path):
    """Check every fingerprint in a reconciliation against the run's findings.

    Reconciliation items are written by an agent, and an agent will abbreviate a hash. On a
    real run every one of 58 fingerprints was written as an 8-character prefix of the
    reviewers' 12; nothing checked, `carryover` matched none of them, and it reported a 100%
    miss rate that was pure artefact. A value that must travel verbatim between two files is
    not something to leave to retyping.

    A unique prefix is repaired loudly. Anything ambiguous or absent is an error: guessing
    would reintroduce exactly the silence this exists to remove.
    """
    known = set()
    for fp_ in sorted(Path(run_dir).glob("findings-*.json")):
        try:
            for f in json.loads(fp_.read_text()).get("findings", []):
                if f.get("fingerprint"):
                    known.add(f["fingerprint"])
        except ValueError:
            pass
    if not known:
        return [], []
    repaired, bad = [], []
    for item in rec.get("items", []):
        fp_ = item.get("fingerprint")
        if not fp_ or fp_ in known:
            continue
        cands = sorted(k for k in known if k.startswith(fp_))
        if len(cands) == 1:
            repaired.append((fp_, cands[0], item.get("where")))
            item["fingerprint"] = cands[0]
        else:
            bad.append((fp_, item.get("where"),
                        "ambiguous prefix" if cands else "matches no finding"))
    return repaired, bad


def report_fingerprints(rec, run_dir, strict=True):
    repaired, bad = resolve_fingerprints(rec, run_dir)
    if repaired:
        print(f"  note: {len(repaired)} reconciliation fingerprint(s) were abbreviated and "
              "have been resolved to the reviewers' full values:", file=sys.stderr)
        for short, full, where in repaired[:6]:
            print(f"    {short} -> {full}   {where}", file=sys.stderr)
        print("  Write fingerprints verbatim — a prefix that later becomes ambiguous is an "
              "error, not a repair.", file=sys.stderr)
    if bad:
        print(f"error: {len(bad)} reconciliation fingerprint(s) do not correspond to any "
              "finding in this run:", file=sys.stderr)
        for fp_, where, why in bad[:10]:
            print(f"    {fp_}  ({why})  {where}", file=sys.stderr)
        print("Every item must carry a fingerprint copied verbatim from a findings file. "
              "Without that, cross-run tracking silently matches nothing.", file=sys.stderr)
        if strict:
            sys.exit(2)
    return repaired, bad


def cmd_coverage(a):
    """Merge one run into the persistent per-(file, reviewer) coverage record."""
    run = Path(a.run_dir).resolve()
    repo = repo_from(run_dir=run, override=a.repo)
    man = _load_json(run / "manifest.json")
    if not man:
        die(f"no manifest.json in {run}")
    cov_path = Path(a.out)
    cov = _load_json(cov_path, {"files": {}, "runs": []})
    cov.setdefault("files", {})
    cov.setdefault("runs", [])
    run_id = a.run_id or run.name

    review_tier = {f["path"]: f for f in man.get("files", [])}
    ref_tier = {f["path"]: f for f in man.get("reference", [])}

    # who read what, this run
    read_by, queried_by = {}, {}
    for fp_ in sorted(run.glob("findings-*.json")):
        d = _load_json(fp_)
        rev = d.get("reviewer") or fp_.stem
        for path in d.get("scope", {}).get("files_read") or []:
            read_by.setdefault(path, set()).add(rev)
        for path in d.get("scope", {}).get("files_queried") or []:
            queried_by.setdefault(path, set()).add(rev)

    # findings per file, this run
    per_file = {}
    for fp_ in sorted(run.glob("findings-*.json")):
        for f in _load_json(fp_).get("findings", []):
            if f.get("file"):
                per_file[f["file"]] = per_file.get(f["file"], 0) + 1

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    # Idempotent: merging the same run twice must not double-count it. Re-running a stage
    # after fixing something downstream is normal, and a counter that drifts on replay makes
    # every rate computed from it wrong.
    replay = run_id in cov["runs"]
    for path, meta in list(review_tier.items()) + list(ref_tier.items()):
        tier = "review" if path in review_tier else "reference"
        e = cov["files"].setdefault(path, {
            "tier": tier, "first_seen_run": run_id, "times_reviewed": 0,
            "reviewed_by": {}, "sha256_when_reviewed": {}, "findings_total": 0})
        e["tier"] = tier
        e["sha256"] = meta.get("sha256")
        e["lines"] = meta.get("lines")
        touched = read_by.get(path, set()) | queried_by.get(path, set())
        if touched:
            if not replay:
                e["times_reviewed"] += 1
            e["last_reviewed_run"] = run_id
            for rev in sorted(touched):
                # Per (file, reviewer). review-docs opening a .py file for its comments is NOT
                # review-code having reviewed it; collapsing them would let a file be skipped
                # on the strength of the wrong reviewer's glance.
                e["reviewed_by"][rev] = run_id
                e["sha256_when_reviewed"][rev] = meta.get("sha256")
        if not replay:
            e["findings_total"] += per_file.get(path, 0)

    # staleness, recomputed from the run list
    if not replay:
        cov["runs"] = (cov["runs"] + [run_id])[-50:]
    idx = {r: i for i, r in enumerate(cov["runs"])}
    for path, e in cov["files"].items():
        last = e.get("last_reviewed_run")
        e["runs_since_reviewed"] = (len(cov["runs"]) - 1 - idx[last]) if last in idx else None
    cov["protocol_version"] = a.protocol_version or cov.get("protocol_version")
    cov["updated"] = now
    cov_path.write_text(json.dumps(cov, indent=2))

    files = cov["files"]
    never = [p for p, e in files.items() if not e.get("last_reviewed_run")]
    stale = sorted((e.get("runs_since_reviewed") or 0, p) for p, e in files.items()
                   if e.get("runs_since_reviewed"))
    print(f"coverage: {len(files)} files tracked over {len(cov['runs'])} run(s)"
          + (" [replay — counters unchanged]" if replay else "") + f" → {cov_path}")
    print(f"  reviewed this run: {sum(1 for e in files.values() if e.get('last_reviewed_run') == run_id)}"
          f" · never reviewed: {len(never)}")
    if never:
        for p_ in sorted(never)[:8]:
            print(f"    NEVER REVIEWED  {p_}")
    for n, p_ in sorted(stale, reverse=True)[:5]:
        print(f"    stale ({n} run(s))  {p_}")


def cmd_carryover(a):
    """Merge a reconciliation into the persistent open-defect record, and classify each
    previously-open defect against the current source."""
    run = Path(a.run_dir).resolve()
    repo = repo_from(run_dir=run, override=a.repo)
    run_id = a.run_id or run.name
    rec = _load_json(a.reconciliation)
    if not rec:
        die(f"no reconciliation at {a.reconciliation}")
    report_fingerprints(rec, run)
    man = _load_json(run / "manifest.json")
    reviewed = {f["path"] for f in man.get("files", [])}

    known = _load_json(a.out, {"defects": {}})
    known.setdefault("defects", {})

    # anchors seen this run, so a carried defect can be matched to fresh evidence
    anchors = {}
    for fp_ in sorted(run.glob("findings-*.json")):
        for f in _load_json(fp_).get("findings", []):
            if f.get("fingerprint"):
                anchors[f["fingerprint"]] = {"anchor": f.get("fingerprint_anchor", ""),
                                             "file": f.get("file")}

    this_run, collisions = {}, {}
    for item in rec.get("items", []):
        fp_ = item.get("fingerprint")
        if not fp_:
            continue
        if fp_ in this_run:
            prev = this_run[fp_]
            if prev.get("disposition") != item.get("disposition"):
                collisions.setdefault(fp_, set()).update(
                    [prev.get("disposition"), item.get("disposition")])
            # Two findings on one source line share a fingerprint. A real defect must always
            # win the slot: recording it as "dropped" because a refuted finding happened to
            # sit on the same line would hide it from every later run.
            if prev.get("disposition") != "dropped":
                continue
        this_run[fp_] = item

    states = {"re-reported": [], "not-re-checked": [], "plausibly-fixed": [], "missed": []}

    # 1. everything previously open: classify against the current source.
    #    `anchors` holds every fingerprint the REVIEWERS filed this run; `this_run` holds
    #    what the reconciler carried through. A defect present in the first and absent from
    #    the second was not missed by the reviewers — it was dropped downstream. Recording
    #    only "missed" would blame the wrong stage, and the two need different fixes.
    for fp_, d in known["defects"].items():
        if d.get("status") in ("fixed", "dropped"):
            continue
        if fp_ in this_run:
            continue  # handled below
        file_ = d.get("file")
        anchor = d.get("anchor", "")
        if d.get("status") == "plausibly-fixed":
            # Already resolved once. Only the offending text COMING BACK reopens it — a file
            # dropping out of the review window must never resurrect a fixed defect.
            if anchor and file_ in reviewed and anchor_present(repo, file_, anchor):
                d["status"], d["last_outcome"] = "open", "regressed"
                states.setdefault("regressed", []).append(fp_)
            continue
        if not anchor:
            # No source anchor (a repo-wide or prose-level defect): presence cannot be
            # checked, so it is never auto-closed. Silence is not evidence of a fix.
            d["status"] = "open"
            d["last_outcome"] = "unanchored-not-checkable"
            states.setdefault("unanchored", []).append(fp_)
        elif file_ not in reviewed:
            d["status"] = "open"
            d["last_outcome"] = "not-re-checked"
            states["not-re-checked"].append(fp_)
        elif fp_ in anchors:
            # A reviewer did raise it; it did not survive reconciliation. Not a reviewer
            # false negative — a reconciler drop, or a cluster that swallowed it.
            d["status"] = "open"
            d["last_outcome"] = "reported-not-carried"
            d["reported_not_carried_in_runs"] = sorted(
                set(d.get("reported_not_carried_in_runs", []) + [run_id]))
            states.setdefault("reported-not-carried", []).append(fp_)
        elif anchor_present(repo, file_, anchor):
            # The file was reviewed, the defect was not re-reported, and the anchored text is
            # byte-identical. That is a CANDIDATE reviewer false negative, not a proven one:
            # a fix made adjacent to the anchor — a seed added on the line above, a guard
            # wrapped around the block — leaves the anchored line untouched and lands here.
            # The asymmetry is deliberate. A false "missed" costs one line in a report that a
            # human will check; a false "fixed" hides a real defect forever. Staying open is
            # the safe direction, so the anchor is kept narrow and the verdict is soft.
            d["status"] = "open"
            d["last_outcome"] = "missed"
            d["missed_in_runs"] = sorted(set(d.get("missed_in_runs", []) + [run_id]))
            states["missed"].append(fp_)
        else:
            d["status"] = "plausibly-fixed"
            d["last_outcome"] = "plausibly-fixed"
            d["resolved_run"] = run_id
            states["plausibly-fixed"].append(fp_)

    # 2. this run's items (collisions already resolved in favour of the non-dropped one)
    for fp_, item in this_run.items():
        disp = item.get("disposition")
        d = known["defects"].setdefault(fp_, {
            "first_run": run_id, "file": item.get("where", "").split(":")[0],
            "anchor": anchors.get(fp_, {}).get("anchor", "")})
        if anchors.get(fp_, {}).get("file"):
            d["file"] = anchors[fp_]["file"]
        if anchors.get(fp_, {}).get("anchor"):
            d["anchor"] = anchors[fp_]["anchor"]
        d.update({"where": item.get("where"), "severity": item.get("severity"),
                  "class": item.get("class"), "title": item.get("title"),
                  "last_seen_run": run_id, "last_outcome": "re-reported"})
        if disp == "applied":
            d["status"], d["resolved_run"] = "fixed", run_id
        elif disp == "dropped":
            d["status"] = "dropped"
        else:
            d["status"] = "open"
            states["re-reported"].append(fp_)

    known["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    Path(a.out).write_text(json.dumps(known, indent=2))

    still_open = {k: v for k, v in known["defects"].items() if v.get("status") == "open"}
    print(f"carryover: {len(known['defects'])} defect(s) tracked, {len(still_open)} open "
          f"→ {a.out}")
    if collisions:
        print(f"  note: {len(collisions)} fingerprint(s) carried findings with differing "
              "dispositions — two defects on one source line. The non-dropped one was kept; "
              "check they are genuinely the same defect:")
        for fp_, disps in list(collisions.items())[:6]:
            print(f"    {fp_}  {sorted(disps)}  {known['defects'][fp_].get('where')}")
    for st in ("re-reported", "reported-not-carried", "missed", "regressed", "unanchored",
               "not-re-checked", "plausibly-fixed"):
        if states.get(st):
            print(f"  {st:16} {len(states[st])}")
    if states.get("reported-not-carried"):
        print("  REPORTED BUT NOT CARRIED — a reviewer filed it; reconciliation did not keep")
        print("  it. A downstream drop, not a reviewer miss:")
        for fp_ in states["reported-not-carried"][:10]:
            d = known["defects"][fp_]
            print(f"    [{d.get('severity')}] {d.get('where')} — {d.get('title')}")
    if states["missed"]:
        print("  STILL PRESENT? — file reviewed, anchored source unchanged, and NO reviewer")
        print("  filed it. Verify each: a fix made next to the anchor also lands here.")
        for fp_ in states["missed"]:
            d = known["defects"][fp_]
            print(f"    [{d.get('severity')}] {d.get('where')} — {d.get('title')}")
    if states["not-re-checked"]:
        print("  NOT RE-CHECKED — file was out of this run's review tier:")
        for fp_ in states["not-re-checked"][:10]:
            d = known["defects"][fp_]
            print(f"    [{d.get('severity')}] {d.get('where')} — {d.get('title')}")


# ---------------------------------------------------------------- shield / unshield

SHIELDABLE = ["REVIEW-SUMMARY.md"]


def _hold_dir(repo: Path, hold: str | None) -> Path:
    """Where shielded files go. It MUST resolve outside the repository.

    The first version of this held them under the run directory, which lives inside the repo:
    `grep -r` found them exactly as before and the shield did nothing. Somewhere outside the
    tree is the whole requirement, so it is checked rather than assumed.
    """
    if hold:
        h = Path(hold).expanduser().resolve()
    else:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", repo.name)
        h = (Path.home() / ".review-kit" / slug / "shield").resolve()
    try:
        h.relative_to(repo)
    except ValueError:
        return h  # outside the repo, which is what we need
    die(f"--hold {h} resolves inside {repo}. Shielded files must sit outside the repository "
        "or a reviewer's `grep -r` finds them exactly as before.")


def cmd_shield(a):
    """Move conclusion-bearing artefacts out of the repository for the reviewer stage.

    `grep -r` and `rg` skip symlinked directories, which is why the review store lives outside
    the repo — but a REAL file at the repo root is found by both, and the summary is
    deliberately a real committed file. Two reviewers have already read a previous run's
    conclusions this way. Independence is the one property the fan-out exists to buy, so the
    summary steps aside while reviewers run and comes back afterwards.
    """
    repo = Path(a.repo).resolve()
    hold = _hold_dir(repo, a.hold)
    marker = hold / "SHIELD.json"
    if marker.exists():
        # An interrupted run left files out of the repo. One marker per repository, so this is
        # always recoverable no matter which run dir was in play when it stopped.
        back = _unshield(marker)
        print(f"  note: an unrestored shield was found and has been restored first "
              f"({len(back)} file(s))", file=sys.stderr)
    hold.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in SHIELDABLE:
        src = repo / name
        if src.exists() and not src.is_symlink():
            dst = hold / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                # shutil.move, not Path.replace: the hold is under $HOME and the repository
                # may be on another filesystem, where os.replace raises EXDEV. That is the
                # ordinary case, not an edge one.
                shutil.move(str(src), str(dst))
            except OSError as e:
                for done in moved:  # put back what already moved; never a half-shielded repo
                    try:
                        shutil.move(str(hold / done), str(repo / done))
                    except OSError:
                        pass
                die(f"could not shield {name}: {e}. Nothing was left out of the repository.")
            moved.append(name)
    marker.write_text(json.dumps(
        {"repo": str(repo), "moved": moved,
         "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
        indent=2))
    print(f"shield: {len(moved)} file(s) held outside the repo for the reviewer stage"
          + (f" — {', '.join(moved)}" if moved else " (nothing to shield)"))
    print(f"  held in: {hold}")
    print(f"  restore: review.py unshield --repo {repo}")


def _unshield(marker: Path):
    spec = json.loads(marker.read_text())
    repo, hold = Path(spec["repo"]), marker.parent
    back = []
    for name in spec.get("moved", []):
        src, dst = hold / name, repo / name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            back.append(name)
    marker.unlink()
    return back


def cmd_unshield(a):
    hold = _hold_dir(Path(a.repo).resolve(), a.hold)
    marker = hold / "SHIELD.json"
    if not marker.exists():
        print("unshield: nothing is shielded for this repository")
        return
    back = _unshield(marker)
    print(f"unshield: restored {len(back)} file(s)" + (f" — {', '.join(back)}" if back else ""))


# ---------------------------------------------------------------- audit (scope honesty)

def cmd_audit(a):
    """Compare declared files_read against the harness-written access log.

    The access log is produced by a PostToolUse hook, outside any agent's reach. A
    reviewer can overstate files_read in its own JSON; it cannot manufacture entries
    here without actually issuing the tool calls.
    """
    doc = read_json(a.findings, "findings file")
    declared = set(doc.get("scope", {}).get("files_read") or [])
    repo = Path(a.repo).resolve()
    touched = set()
    logp = Path(a.access_log)
    if not logp.exists():
        die(f"no access log at {logp} — the hook is not wired up; see review-kit/README.md")
    for line in logp.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if a.agent and rec.get("agent") and rec["agent"] != a.agent:
            continue
        for p in rec.get("paths", []):
            q = (repo / p) if not p.startswith("/") else Path(p)
            try:
                touched.add(str(q.resolve().relative_to(repo)))
            except (ValueError, OSError):
                pass
    unsupported = sorted(declared - touched)
    name = doc.get("reviewer", "?")
    print(f"audit {name}: declared {len(declared)} files read, access log shows "
          f"{len(touched & declared)} of them actually touched")
    if unsupported:
        print(f"  UNSUPPORTED CLAIM: {len(unsupported)} declared file(s) appear in no tool call:")
        for p in unsupported[:20]:
            print(f"    {p}")
        sys.exit(1)
    print("  every declared file appears in the access log")


# ---------------------------------------------------------------- summarize + ledger

def cmd_summarize(a):
    r = read_json(a.reconciliation, "reconciliation")
    run_dir = Path(a.run_dir) if getattr(a, "run_dir", None) else Path(a.reconciliation).parent
    report_fingerprints(r, run_dir)
    items = r["items"]
    fixed = [x for x in items if x["disposition"] == "applied"]
    openi = [x for x in items if x["disposition"] == "open"]
    deferred = [x for x in items if x["disposition"] == "deferred"]
    dropped = [x for x in items if x["disposition"] == "dropped"]
    unver = [x for x in items if x["disposition"] == "unverifiable"]
    order = {s: i for i, s in enumerate(SEVERITIES)}
    openi.sort(key=lambda x: (order.get(x.get("severity"), 9), x.get("where") or ""))

    L = []
    w = L.append
    m = r.get("machine", {})
    w(f"# Review summary — {r['date']} @ {r.get('head', '?')}\n")
    w(f"**{len(fixed) + len(openi) + len(deferred)} confirmed defects: {len(fixed)} fixed in "
      f"the working tree, {len(openi)} for you "
      f"({sum(1 for x in openi if x.get('severity') == 'critical')} critical), "
      f"{len(deferred)} deferred for compute.** "
      + (f"{len(dropped)} reported finding{'s were' if len(dropped) != 1 else ' was'} "
         "refuted and dropped" if dropped else "Nothing was refuted")
      + (f"; {len(unver)} could not be settled on any machine" if unver else "") + ".")
    w(f"Machine: {m.get('cores')} cores, {m.get('usable_gb')} GB usable, "
      f"{'GPU: ' + ', '.join(m['gpu']) if m.get('gpu') else 'no GPU'}.")
    w(f"Full detail: `{r['run_dir']}` · revert the fixes: `{r.get('revert_cmd', '—')}`\n")
    w("---\n")

    w(f"## Fixed ({len(fixed)})\n")
    if fixed:
        w("| # | where | defect | fix implemented |")
        w("|---|---|---|---|")
        for i, x in enumerate(fixed, 1):
            w(f"| {i} | `{x['where']}` | {x['defect']} | {x['fix']} |")
    else:
        w("Nothing was applied automatically.")
    if r.get("line_shift_note"):
        w(f"\n> {r['line_shift_note']}")
    w("")

    w(f"## For you ({len(openi)})\n")
    for sev in SEVERITIES:
        grp = [x for x in openi if x.get("severity") == sev]
        if not grp:
            continue
        w(f"### {sev.capitalize()} — {len(grp)}\n")
        if sev in ("critical", "major"):
            for i, x in enumerate(grp, 1):
                corr = x.get("corroborated_by") or []
                w(f"**{i}. {x['title']}** · `{x['where']}`"
                  + (f" · corroborated by {', '.join(corr)}" if len(corr) > 1 else
                     " · **single reviewer**" if corr else ""))
                w(f"- **What is wrong:** {x['defect']}")
                if x.get("verified_by"):
                    w(f"- **Verified by:** {x['verified_by']}")
                if x.get("blast_radius"):
                    w(f"- **Already affected:** {x['blast_radius']}")
                w(f"- **Recommended fix:** {x['fix']}")
                if x.get("fix_cost"):
                    w(f"- **Cost of the fix:** {x['fix_cost']}")
                w(f"- **Not applied because:** {x.get('not_applied_because', 'judgment call')}")
                if x.get("recurring_since"):
                    w(f"- **Note:** also reported in run {x['recurring_since']} and still open.")
                w("")
        else:
            w("| where | defect | recommended fix |")
            w("|---|---|---|")
            for x in grp:
                w(f"| `{x['where']}` | {x['defect']} | {x['fix']} |")
            w("")

    if deferred:
        w(f"## Deferred for compute ({len(deferred)})\n")
        for x in deferred:
            w(f"**`{x['where']}`** — {x['title']}\n")
            w(f"{x['defect']}\n")
            w(f"**Not run:** {x.get('why_not_run', 'beyond this machine')}\n")
            w(f"**To settle it:**\n\n```bash\n{x.get('command', '')}\n```\n")

    # carried-forward defects from earlier runs
    carried = {}
    if a.open_defects and Path(a.open_defects).exists():
        carried = json.loads(Path(a.open_defects).read_text()).get("defects", {})
    here = {x.get("fingerprint") for x in items}
    missed = [d for k, d in carried.items()
              if d.get("status") == "open" and d.get("last_outcome") == "missed" and k not in here]
    regressed = [d for k, d in carried.items()
                 if d.get("status") == "open" and d.get("last_outcome") == "regressed"
                 and k not in here]
    unchecked = [d for k, d in carried.items()
                 if d.get("status") == "open" and d.get("last_outcome") == "not-re-checked"
                 and k not in here]
    if missed or unchecked or regressed:
        w(f"## Still open from earlier runs ({len(missed) + len(unchecked) + len(regressed)})\n")
        if regressed:
            w(f"### Regressed — fixed once, and the offending code is back ({len(regressed)})\n")
            w("These were confirmed, then resolved in a later run, and the source text they "
              "were anchored to has now reappeared. No reviewer raised them this run.\n")
            w("| where | severity | defect | first seen |")
            w("|---|---|---|---|")
            for d in sorted(regressed, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{d.get('where')}` | {d.get('severity')} | {d.get('title')} "
                  f"| {d.get('first_run')} |")
            w("")
        if missed:
            w(f"### Reviewed again and not re-reported — but the code has not changed "
              f"({len(missed)})\n")
            w("A reviewer looked at these files this run and did not raise these defects, yet "
              "the offending source is byte-identical to when they were first confirmed. Treat "
              "them as open, and as evidence about the reviewers rather than about the code.\n")
            w("| where | severity | defect | first seen |")
            w("|---|---|---|---|")
            for d in sorted(missed, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{d.get('where')}` | {d.get('severity')} | {d.get('title')} "
                  f"| {d.get('first_run')} |")
            w("")
        if unchecked:
            w(f"### Not re-checked this run — file was out of scope ({len(unchecked)})\n")
            w("| where | severity | defect | first seen |")
            w("|---|---|---|---|")
            for d in sorted(unchecked, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{d.get('where')}` | {d.get('severity')} | {d.get('title')} "
                  f"| {d.get('first_run')} |")
            w("")

    if unver:
        w(f"## Open questions ({len(unver)}) — not settleable on any machine\n")
        for x in unver:
            w(f"- `{x['where']}` — {x['defect']}")
        w("")

    w("## Not reviewed\n")
    for x in r.get("not_reviewed", []):
        w(f"- {x}")
    w("")
    w("## Confidence\n")
    w(r.get("confidence_note", ""))

    Path(a.out).write_text("\n".join(L) + "\n")
    tot = len(fixed) + len(openi) + len(deferred)
    print(f"summary: {tot} confirmed ({len(fixed)} fixed, {len(openi)} open, "
          f"{len(deferred)} deferred), {len(dropped)} dropped → {a.out}")
    print("counts derived from reconciliation.json; headers cannot disagree with contents")


def rate_txt(v):
    return "—" if v is None else f"{v:.0%}"


def cmd_ledger(a):
    r = read_json(a.reconciliation, "reconciliation")
    items = r["items"]
    rows = []
    reviewers = sorted({rv for x in items for rv in (x.get("corroborated_by") or ["?"])})
    for rev in reviewers:
        mine = [x for x in items if rev in (x.get("corroborated_by") or [])]
        conf = sum(1 for x in mine if x["disposition"] in ("applied", "open"))
        ref = sum(1 for x in mine if x["disposition"] == "dropped")
        dfr = sum(1 for x in mine if x["disposition"] == "deferred")
        unv = sum(1 for x in mine if x["disposition"] == "unverifiable")
        rows.append({"run_id": r["run_id"], "date": r["date"], "reviewer": rev,
                     "reported": len(mine), "confirmed": conf, "refuted": ref,
                     "deferred": dfr, "unverifiable": unv,
                     "rate_verifiable": round(conf / (conf + ref), 3) if conf + ref else None,
                     "machine": r.get("machine")})
    with open(a.out, "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    for row in rows:
        print(f"  {row['reviewer']:18} {row['reported']:3} reported → {row['confirmed']} confirmed, "
              f"{row['refuted']} refuted, {row['deferred']} deferred  "
              f"({rate_txt(row['rate_verifiable'])})")
    print(f"ledger: {len(rows)} rows appended → {a.out}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="review.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scope", help="compute the review window + file manifest")
    p.add_argument("--repo", required=True); p.add_argument("--out", required=True)
    p.add_argument("--since")
    p.add_argument("--full", action="store_true",
                   help="whole repository (the default when --since is absent)")
    p.add_argument("--list", action="store_true")
    p.add_argument("--reference-threshold", type=int, default=500,
                   help="files longer than this, and all data files, go to the reference "
                        "tier: described in the manifest, never read (default 500)")
    p.set_defaults(fn=cmd_scope)

    p = sub.add_parser("machine", help="measure the compute budget")
    p.add_argument("--out"); p.set_defaults(fn=cmd_machine)

    p = sub.add_parser("characterize", help="signals about the repo's posture")
    p.add_argument("--repo", required=True); p.add_argument("--out")
    p.set_defaults(fn=cmd_characterize)

    p = sub.add_parser("probe", help="run the mechanical static checks once, for all reviewers")
    p.add_argument("--manifest", required=True)
    p.add_argument("--repo", help="overrides the repo recorded in the manifest; rarely needed")
    p.add_argument("--out", required=True); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("validate", help="enforce the findings contract")
    p.add_argument("--findings", required=True); p.add_argument("--manifest")
    p.add_argument("--repo"); p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("fingerprint", help="stamp stable ids onto findings, in place")
    p.add_argument("--findings", required=True)
    p.add_argument("--manifest", help="the run's manifest.json; its recorded repo is used")
    p.add_argument("--repo", help="overrides the repo recorded in the manifest")
    p.set_defaults(fn=cmd_fingerprint)

    p = sub.add_parser("cluster", help="corroboration + proximity candidates")
    p.add_argument("--run-dir", required=True); p.add_argument("--window", type=int, default=15)
    p.set_defaults(fn=cmd_cluster)

    p = sub.add_parser("match", help="recurrence against a previous run")
    p.add_argument("--run-dir", required=True); p.add_argument("--prev-run")
    p.set_defaults(fn=cmd_match)

    p = sub.add_parser("coverage", help="merge a run into the persistent coverage record")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", help="overrides the repo recorded in the run's manifest")
    p.add_argument("--out", required=True); p.add_argument("--run-id")
    p.add_argument("--protocol-version"); p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("carryover", help="track defects across runs; detect misses")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", help="overrides the repo recorded in the run's manifest")
    p.add_argument("--reconciliation", required=True); p.add_argument("--out", required=True)
    p.add_argument("--run-id"); p.set_defaults(fn=cmd_carryover)

    p = sub.add_parser("shield", help="move in-repo conclusions aside for the reviewer stage")
    p.add_argument("--repo", required=True)
    p.add_argument("--hold", help="where to hold them; must be outside the repo "
                                  "(default ~/.review-kit/<repo>/shield)")
    p.set_defaults(fn=cmd_shield)

    p = sub.add_parser("unshield", help="restore what shield moved")
    p.add_argument("--repo", required=True); p.add_argument("--hold")
    p.set_defaults(fn=cmd_unshield)

    p = sub.add_parser("audit", help="check files_read against the harness access log")
    p.add_argument("--findings", required=True); p.add_argument("--repo", required=True)
    p.add_argument("--access-log", required=True); p.add_argument("--agent")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("summarize", help="render REVIEW-SUMMARY.md from reconciliation.json")
    p.add_argument("--reconciliation", required=True); p.add_argument("--out", required=True)
    p.add_argument("--open-defects", help=".review/open_defects.json, to carry forward "
                                          "defects that earlier runs confirmed")
    p.add_argument("--run-dir", help="the run directory, for checking that every "
                                     "reconciliation fingerprint matches a real finding "
                                     "(defaults to the reconciliation's own directory)")
    p.set_defaults(fn=cmd_summarize)

    p = sub.add_parser("ledger", help="append ledger rows from reconciliation.json")
    p.add_argument("--reconciliation", required=True); p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_ledger)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
