#!/usr/bin/env python3
"""Deterministic machinery for the review kit.

Everything here is bookkeeping an agent should never be asked to do by hand: scoping,
schema validation, scope-honesty checking, stable fingerprints, clustering candidates,
recurrence matching, summary rendering and the ledger. Agents find defects, verify
defects and decide fixes; this script does the rest.

Stdlib only. Python 3.8+.
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, shlex, shutil, subprocess, sys, datetime, warnings
from pathlib import Path

SEVERITIES = ["critical", "major", "minor", "trivial"]
CONFIDENCES = ["certain", "likely", "speculative"]
CLASSES = ["defect", "hygiene"]
EVIDENCE_KINDS = ["command", "quote", "reasoning"]
FIX_CERTAINTIES = ["mechanical", "judgment"]

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "__pycache__",
             ".venv", "venv", ".mypy_cache", ".pytest_cache", ".review", ".ipynb_checkpoints",
             ".claude", ".idea", ".vscode"}
# Every suffix set is lower-cased at definition and compared against `suffix.lower()`.
# `.Rmd` sat here in mixed case while `classify` lower-cased, so no R Markdown file was ever
# classified at all — in a data repository that is the primary analysis artefact. `.jsonl`
# and `.parquet` were declared tabular but missing here, so they never survived classify to
# reach the tabular branch, which was dead for both.
SOURCE_EXT = {e.lower() for e in {
    ".py", ".pyx", ".sh", ".R", ".r", ".Rmd", ".jl", ".sql", ".patch", ".diff",
    ".md", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".json", ".txt",
    ".ipynb", ".csv", ".tsv", ".jsonl", ".parquet"}}

# Source code and prose: always review tier, whatever their length. Demoting a long script
# to "describe, do not read" hides exactly the file most worth reading.
# A .patch is code: in a competition repo it is usually the change made to a pinned
# dependency, which the first live run left in no tier at all.
CODE_EXT = {e.lower() for e in {".py", ".pyx", ".sh", ".R", ".r", ".Rmd", ".jl", ".sql",
                                ".patch", ".diff"}}

# Markdown is exempt from the length threshold: review-docs is the document-integrity leg and
# demoting a long README to "query with a script" blinds it to the longest document in the
# repo, which is where an uncited number hides. Deliberately .md alone — .txt is a grab-bag
# that has already included a 128,451-line word list, and .ipynb carries embedded output.
# Both stay subject to the threshold.
PROSE_EXT = {".md"}

# Tabular data: described, and interrogated with a script rather than read.
TABULAR_EXT = {".csv", ".tsv", ".parquet", ".jsonl"}

# Tabular but not text: counting "lines" in one means counting newline bytes in a binary
# container, which puts a meaningless number in the manifest.
BINARY_TABULAR_EXT = {".parquet"}

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


def write_atomic(path, text):
    """Write via a sibling temp file and rename. `C-7`, which this kit asks of others.

    It matters most where the target is somebody else's work: `fingerprint` rewrites a
    reviewer's findings in place, and a crash between truncate and write loses a file no
    stage can regenerate.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        # Explicit UTF-8: the summary carries em dashes and middots, and the locale encoding
        # either raised under LC_ALL=C or silently wrote cp1252 into a file meant to be
        # committed and re-read elsewhere.
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)   # a failed write used to leave a stale .tmp behind
        raise


def read_json(path, what):
    """Read a required JSON input, or fail with a sentence.

    A traceback tells an agent following a skill nothing it can act on, and these are the
    inputs most likely to be missing: a stage that did not run, or a path typed wrong.
    """
    p = Path(path)
    if not p.exists():
        die(f"no {what} at {p}. Has the stage that writes it run yet?")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
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


def repo_slug(repo: Path) -> str:
    """A per-repository directory name that two repositories cannot share.

    The name alone is not enough: `~/work/analysis` and `~/client/analysis` both reduce to
    "analysis", and they would then share one coverage record, one open-defect ledger and one
    carryover history — so a defect confirmed in the first reads as "missed" in the second.
    The path hash is what makes the name unique; the readable part is kept so a person can
    still tell the directories apart by eye.
    """
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", repo.name) or "repo"
    return f"{name}-{hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:8]}"


def store_path(repo: Path) -> Path:
    """The review store for a repo. Every caller in the kit uses this, install.py included."""
    return (Path.home() / ".review-kit" / repo_slug(repo) / "review").resolve()


def config_path(repo: Path) -> Path:
    """Per-repository install record, beside the store rather than inside the repository.

    Two facts have to survive between `install.py` and a run days later, and neither can be
    recomputed. The first is where the kit itself lives: every skill says
    `<review-kit>/bin/review.py`, and nothing else records what that resolves to. The skills
    are symlinks into the kit, but nothing should have to resolve one to find out.
    The second is which filename this
    repository's summary claimed, because the obvious one may already have been taken by a
    file that is not ours.
    """
    return (Path.home() / ".review-kit" / repo_slug(repo) / "config.json").resolve()


DEFAULT_SUMMARY = "REVIEW-SUMMARY.md"


def read_config(repo: Path) -> dict:
    p = config_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def write_config(repo: Path, **kw) -> dict:
    p = config_path(repo)
    cfg = read_config(repo)
    cfg.update({k: v for k, v in kw.items() if v is not None})
    cfg["repo"] = str(repo.resolve())
    p.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(p, json.dumps(cfg, indent=2) + "\n")
    return cfg


def claim_summary_name(repo: Path) -> str:
    """Pick a summary filename that is free, ONCE, and remember it.

    Claimed at install and then reused for the life of the install. Searching for a free name
    on every run would leave REVIEW-SUMMARY-1.md, -2.md, -3.md behind, so the search happens
    once and the answer is recorded.
    A repository that already has a REVIEW-SUMMARY.md of its own keeps it untouched.
    """
    cfg = read_config(repo)
    if cfg.get("summary"):
        return cfg["summary"]
    stem, ext = DEFAULT_SUMMARY.rsplit(".", 1)
    name = DEFAULT_SUMMARY
    n = 0
    while (repo / name).exists():
        n += 1
        name = f"{stem}-{n}.{ext}"
    return name


def summary_path(repo: Path) -> Path:
    """Where this repository's summary belongs. Always the repo root: it is the deliverable.

    The name comes from the install record, because the plain one may have been taken.
    """
    return repo / (read_config(repo).get("summary") or DEFAULT_SUMMARY)


def summary_name(repo: Path) -> str:
    """This repository's summary filename, as claimed at install.

    The summary stays in the working tree during a review and reviewers are instructed not to
    read it. That instruction is the whole protection. Moving the file out and back only ever
    hid the working-tree copy — `git show HEAD:<name>` served the previous run's conclusions
    regardless — so the machinery bought partial cover at the price of a marker file, a
    crash-recovery path, and a way to strand the deliverable outside the repository.
    """
    return read_config(repo).get("summary") or DEFAULT_SUMMARY


# ---------------------------------------------------------------- scope

# Extensionless files that are nonetheless code. Without these a Dockerfile or Makefile —
# part of the pipeline in most repos — is in no tier at all.
NAMED_CODE = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
              "Justfile", "justfile", "Rakefile", "Procfile", "Vagrantfile"}


def looks_binary(p: Path) -> bool:
    """A NUL byte in the first 8 KiB — git's own test. Unreadable counts as binary."""
    try:
        with p.open("rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def is_opaque(p: Path) -> bool:
    """Listed but never read: a known opaque suffix, or an unknown suffix holding binary."""
    ext = p.suffix.lower()
    if ext in OPAQUE_EXT:
        return True
    if p.name in NAMED_CODE or ext in SOURCE_EXT:
        return False
    return looks_binary(p)


def classify(p: Path):
    """'source' if the file belongs in a tier, else None — the caller records the rest.

    An unknown suffix is sniffed rather than rejected. Rejection put `l5kit.patch` and every
    `.gitignore` in no tier on the first live run, so nobody was asked to read them; a text
    file of unknown kind is placed by length like any other, and a binary one is opaque.
    Only a file that cannot be opened is left unclassified.
    """
    if p.name in NAMED_CODE or p.suffix.lower() in SOURCE_EXT:
        return "source"
    try:
        with p.open("rb"):
            return "source"
    except OSError:
        return None


def is_readable(p: Path) -> bool:
    """Can this path actually be opened? A dangling symlink cannot.

    `data/train.csv -> /mnt/nfs/...` on an unmounted volume is the most ordinary thing in a
    data repository, and `os.walk` lists it like any other file. Every later stat, hash and
    read then raises FileNotFoundError, which ended the whole audit at stage 0 with a
    traceback. Such a path is recorded, never silently dropped.
    """
    try:
        # Opening it is the only honest test. `stat().st_size >= 0` is true for anything whose
        # metadata is reachable — a mode-000 file passed, and then sha256_of raised and ended
        # the whole audit at stage 0 with a traceback, which is precisely what this prevents.
        with p.open("rb"):
            return True
    except OSError:
        return False


def iter_source(repo: Path, rejected=None, unreadable=None, skipped=None):
    for root, dirs, files in os.walk(repo):
        pruned = [d for d in dirs if d in SKIP_DIRS]
        if skipped is not None:
            # `unclassified`, `unreadable` and `deleted` all exist so the manifest never
            # reports a scope it did not achieve. Whole directories were the one exception:
            # a vendored patch or a committed generated module under vendor/ or build/ left
            # no trace anywhere.
            skipped.extend(Path(root) / d for d in pruned)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = Path(root) / fn
            if not is_readable(p):
                if unreadable is not None:
                    unreadable.append(p)
                continue
            if classify(p):
                yield p
            elif rejected is not None:
                rejected.append(p)


def describe_reference(p: Path, rel: str):
    """Enough for a reviewer to know what is in a file without reading it."""
    opaque = is_opaque(p)
    # A file can become unreadable between the walk and here — a race, a revoked permission,
    # a link whose target went away. Describing it as best we can beats ending the run.
    try:
        size, digest = p.stat().st_size, sha256_of(p)
    except OSError as e:
        size, digest = None, None
    d = {"path": rel, "sha256": digest, "bytes": size,
         "kind": "opaque" if opaque else "tabular"}
    # `kind` stays "tabular" for any queryable text, which is what the reviewers' skills key on;
    # `text` tells a long log or lockfile from a CSV.
    if not opaque and p.suffix.lower() not in TABULAR_EXT:
        d["text"] = True
    if digest is None:
        d["note"] = f"could not be read when the manifest was built ({p}); treat as unchecked"
        return d
    if opaque:
        d["note"] = ("cannot be read as text or queried by a short script. Do NOT attempt to "
                     "read it. Note its existence, and say so in not_reviewed if its content "
                     "bears on a finding — a deck or PDF is where uncited numbers hide.")
        return d
    if p.suffix.lower() not in BINARY_TABULAR_EXT:
        d["lines"] = count_lines(p)
    if p.suffix.lower() in (".csv", ".tsv"):
        sep = "\t" if p.suffix.lower() == ".tsv" else ","
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
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
    if a.since and a.full:
        die("--since and --full ask for different windows; pass one.")
    if a.since:
        rc, o, e = sh(f"git rev-parse --verify {a.since}^{{commit}}", cwd=repo)
        if rc:
            die(f"cannot resolve --since {a.since}: {e}")
        base, mode = o, "since"
        # `base` not `base..HEAD`: the working tree is included deliberately. On a live repo
        # the uncommitted change is usually the thing most worth reviewing, and excluding it
        # would silently review a state nobody is currently in.
        # --relative: `git diff` prints paths from the worktree ROOT, `ls-files --others`
        # prints them from cwd. With --repo on a subdirectory the two disagreed, every
        # modified file resolved to a non-existent path, and the review window reported it
        # as deleted — while announcing that "a removal can be the defect".
        rc, o, _ = sh(f"git diff --relative --name-only {base}", cwd=repo)
        names = [x for x in o.splitlines() if x.strip()]
        # `git diff` lists tracked modifications only. A file created and not yet added is
        # both the most likely thing to be under review and the most likely to be missed.
        rc2, untracked, _ = sh("git ls-files --others --exclude-standard", cwd=repo)
        names += [x for x in untracked.splitlines() if x.strip()]
        paths = [repo / x for x in dict.fromkeys(names)]

    rejected, unreadable, deleted, skipped = [], [], [], []
    if paths is None:
        paths = sorted(iter_source(repo, rejected, unreadable, skipped))
    else:
        # --since used to filter silently, so a new file with an unknown suffix — a .dvc
        # stage, a .lock — vanished from the manifest with nothing recorded. Full mode
        # records those under `unclassified`; the mode most runs actually use must too.
        kept = []
        for p in paths:
            # Full mode prunes SKIP_DIRS in iter_source; this path never did. A tracked,
            # changed file under vendor/, build/ or .venv/ therefore entered the review tier
            # and was read in full by every reviewer — and a target repo carrying its own
            # .review/ had a previous run's findings read by the legs that must not see them.
            rel_parts = set(p.relative_to(repo).parts[:-1])
            if rel_parts & SKIP_DIRS:
                hit = sorted(rel_parts & SKIP_DIRS)[0]
                d_ = repo / hit
                if d_ not in skipped:
                    skipped.append(d_)
                continue
            if not is_readable(p):
                # A path in the diff that is simply gone was DELETED in this window — an
                # ordinary event, and not the same thing as a file that exists and cannot be
                # opened. Calling a deletion "unreadable" would put an unmounted-volume
                # warning on most incremental runs. A dangling symlink still counts as
                # unreadable: it is present, and its target is not.
                (unreadable if p.is_symlink() else deleted).append(p)
                continue
            (kept if classify(p) else rejected).append(p)
        paths = kept

    rc, head, _ = sh("git rev-parse --short HEAD", cwd=repo)
    # The summary is this kit's own output, not the repository's prose, and reviewers are told
    # not to read it. Leaving it in the manifest would charge every one of them a SCOPE GAP
    # for obeying that instruction, and the only truthful not_reviewed reason they could give
    # names the file — which tells them what it is.
    summary = summary_name(repo)
    review, reference, oversized = [], [], []
    # An unknown suffix is placed only if git tracks it. A tracked one was put there on
    # purpose — a patch, an ignore file, a LICENSE. An untracked one is far more often an
    # ignored tree of extensionless output (mlruns/, wandb/, a DVC cache), and placing those
    # in any tier floods the manifest and the coverage record; they stay `unclassified`.
    # Bytes, decoded as the filesystem does: with -z git prints names unescaped, and a
    # tracked name that is not UTF-8 ended the whole audit at stage 0.
    r_t = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True)
    tracked = {os.fsdecode(x) for x in r_t.stdout.split(b"\0")} if r_t.returncode == 0 else set()
    for p in sorted(paths):
        if p.name == summary and p.parent == repo:
            continue
        rel = str(p.relative_to(repo))
        if (p.suffix.lower() not in SOURCE_EXT and p.name not in NAMED_CODE
                and rel not in tracked):
            rejected.append(p)
            continue
        if not is_readable(p):
            # It passed the walk and became unreadable since, or arrived through --since.
            unreadable.append(p)
            continue
        # Tabular data is reference-tier at any size, and an opaque file can never be read
        # at all; .json is neither, because a 20-line config is something a reviewer must
        # actually read. Length decides for everything else. One lower-cased suffix, compared
        # against sets that are themselves lower-cased at definition.
        ext = p.suffix.lower()
        opaque = is_opaque(p)
        n = 0 if opaque or ext in BINARY_TABULAR_EXT else count_lines(p)
        is_code = ext in CODE_EXT or ext in PROSE_EXT
        if is_code and n > a.reference_threshold:
            oversized.append((rel, n))
        if ext in TABULAR_EXT or opaque or (n > a.reference_threshold
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
        # Recorded rather than silently dropped: a manifest that lists neither the file nor
        # the fact that it was skipped reports a complete-looking scope it has not achieved.
        "unclassified": [{"path": str(q.relative_to(repo)),
                          "bytes": q.stat().st_size if is_readable(q) else None}
                         for q in sorted(rejected)],
        # Listed, not dropped: a reviewer told "these files exist and could not be opened"
        # can say so, where a silent omission reports a complete scope it did not achieve.
        "unreadable": [{"path": str(q.relative_to(repo)),
                        "symlink_to": os.readlink(q) if q.is_symlink() else None}
                       for q in sorted(unreadable)],
        # Only ever populated by --since. A removal can itself be the defect — a script the
        # docs still reference, a test that guarded the thing that just changed — so the
        # window names what left it rather than showing a scope with a silent hole.
        "deleted": [str(q.relative_to(repo)) for q in sorted(deleted)],
        "skipped_dirs": sorted(str(q.relative_to(repo)) for q in skipped),
        "reference_files": len(reference),
        "reference_lines": sum(f.get("lines", 0) for f in reference),
        "opaque_files": sum(1 for f in reference if f.get("kind") == "opaque"),
    }
    write_atomic((out / "manifest.json"), json.dumps(manifest, indent=2))
    n_opaque = manifest["opaque_files"]
    n_tab = len(reference) - n_opaque
    if skipped:
        print(f"  note: {len(skipped)} director(ies) were pruned by name and nothing in them "
              "was reviewed — listed under `skipped_dirs` in the manifest:", file=sys.stderr)
        for q in sorted(skipped)[:8]:
            print(f"    {q.relative_to(repo)}/", file=sys.stderr)
    if deleted:
        print(f"  note: {len(deleted)} file(s) were DELETED in this window and are listed "
              "under `deleted` in the manifest — a removal can be the defect:", file=sys.stderr)
        for q in sorted(deleted)[:8]:
            print(f"    {q.relative_to(repo)}", file=sys.stderr)
    if unreadable:
        print(f"  note: {len(unreadable)} path(s) could not be opened and are in no tier — "
              "recorded under `unreadable` in the manifest. A dangling symlink is the usual "
              "cause; the target may simply not be mounted here:", file=sys.stderr)
        for q in sorted(unreadable)[:8]:
            tgt = f" -> {os.readlink(q)}" if q.is_symlink() else ""
            print(f"    {q.relative_to(repo)}{tgt}", file=sys.stderr)
    if rejected:
        print(f"  note: {len(rejected)} file(s) have an unknown type and are not tracked by git, "
              "or could not be opened — recorded under `unclassified` in the manifest, not "
              "reviewed:", file=sys.stderr)
        for q in sorted(rejected)[:8]:
            print(f"    {q.relative_to(repo)}", file=sys.stderr)
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


# ---------------------------------------------------------------- runs

REVIEWER_LEGS = ("fresh-eyes", "code", "analysis", "docs")


def run_state(run_dir: Path, repo: Path) -> dict:
    """What a run directory contains, and whether it can still be resumed.

    Every stage of a review is a pure producer of one file, so "where did this get to" is
    answerable by looking rather than by remembering. Staleness is the other half: a run
    whose manifest no longer matches the working tree cannot be resumed, because the findings
    cite line numbers in files that have since moved.
    """
    man = _load_json(run_dir / "manifest.json")
    legs = {leg: (run_dir / f"findings-{leg}.json").exists() for leg in REVIEWER_LEGS}
    extra = sorted(q.name for q in run_dir.glob("findings-*.json")
                   if q.stem[len("findings-"):] not in REVIEWER_LEGS)
    st = {"run_id": run_dir.name, "path": str(run_dir),
          "has_manifest": bool(man),
          "legs_done": [k for k, v in legs.items() if v],
          "legs_missing": [k for k, v in legs.items() if not v],
          "other_findings": extra,
          "clustered": (run_dir / "clusters.json").exists(),
          "reconciled": (run_dir / "reconciliation.json").exists()}

    drifted, missing = [], []
    for f in man.get("files", []):
        q = repo / f["path"]
        if not q.exists():
            missing.append(f["path"])
        elif f.get("sha256") and sha256_of(q) != f["sha256"]:
            drifted.append(f["path"])
    st["drifted"] = drifted
    st["gone"] = missing
    st["stale"] = bool(drifted or missing)

    if not st["has_manifest"]:
        st["state"] = "unusable: no manifest"
    elif st["reconciled"]:
        st["state"] = "complete"
    elif st["stale"]:
        st["state"] = "stale: the working tree moved since this run was scoped"
    elif not st["legs_missing"]:
        st["state"] = "resumable: all reviewers done, needs the reconciler"
    elif st["legs_done"]:
        st["state"] = ("resumable: needs reviewer(s) "
                       + ", ".join(st["legs_missing"]) + ", then the reconciler")
    else:
        st["state"] = "resumable: no reviewer has run yet"
    return st


def cmd_runs(a):
    """List this repository's runs and say which, if any, can be resumed.

    Without this, a session that died mid-run could only be resumed by hand: Stage 0 mints a
    new run directory unconditionally, so a fresh `/repo-audit` re-ran four reviewers whose
    findings were already on disk and paid for.
    """
    repo = Path(a.repo).resolve()
    # Always the external store. This used to switch to an in-repository `.review/` whenever
    # one existed — a leftover from an older version, which nothing else reads — and then
    # reported "no runs yet" while the store held a run that could be resumed.
    runs_dir = store_path(repo) / "runs"
    if (repo / ".review").is_dir():
        print(f"note: {repo / '.review'} is left over from an older version of this kit and is "
              "not used; runs live in the external store.", file=sys.stderr)
    if not runs_dir.is_dir():
        print(f"no runs yet for {repo}")
        return
    states = [run_state(d, repo) for d in sorted(runs_dir.iterdir()) if d.is_dir()]
    if not states:
        print(f"no runs yet for {repo}")
        return
    for st in states[-a.limit:]:
        print(f"{st['run_id']}  {st['state']}")
        if st["has_manifest"]:
            print(f"    reviewers: {len(st['legs_done'])}/4 done"
                  + (f" ({', '.join(st['legs_done'])})" if st["legs_done"] else "")
                  + (f" · missing: {', '.join(st['legs_missing'])}" if st["legs_missing"] else "")
                  + (f" · clustered" if st["clustered"] else ""))
        if st["other_findings"]:
            print(f"    also present: {', '.join(st['other_findings'])}")
        if st["stale"]:
            n = len(st["drifted"]) + len(st["gone"])
            shown = (st["drifted"] + st["gone"])[:4]
            print(f"    {n} scoped file(s) changed or vanished since: {', '.join(shown)}"
                  + ("…" if n > 4 else ""))
            print("    Do NOT resume this one — its findings cite line numbers that have moved.")
    resumable = [st for st in states if st["state"].startswith("resumable")]
    print()
    if resumable:
        last = resumable[-1]
        print(f"RESUMABLE: {last['run_id']}")
        print(f"  D={last['path']}")
        print("  Reuse that run id and directory verbatim; do not mint a new one.")
        print("  Skip Stage 0 — manifest, machine, probes and profile are already there.")
        print("  Skip every reviewer whose findings file is present.")
    else:
        print("Nothing to resume; a new run starts clean.")


# ---------------------------------------------------------------- preflight

# Every settings file a hook can legitimately live in. Checking only the first two reported
# "not wired" for a hook that was wired, which reads as a missing capability.
HOOK_FILES = ("~/.claude/settings.json", "~/.claude/settings.local.json")


def hook_wired(repo: Path):
    """Which settings files mention the access-log hook. Informational, never fatal."""
    found = []
    for cand in ([Path(f).expanduser() for f in HOOK_FILES]
                 + [repo / ".claude/settings.json", repo / ".claude/settings.local.json"]):
        try:
            if cand.exists() and "log_file_access.py" in cand.read_text(encoding="utf-8"):
                found.append(cand)
        except OSError:
            pass
    return found


def cmd_preflight(a):
    """Check the conditions a review depends on, before anything is spawned.

    One of them cannot be checked any other way: the access-log hook logs to the store of the
    repository the session is CURRENTLY in (walking up from its directory), not the one under
    review. Auditing repo A from a shell sitting in repo B silently logs A's reads into B's
    store, and `audit` then reports every honest reviewer as having claimed a file it never
    opened. The kit's rule is that a review runs from the root of the repository it reviews;
    this is the only thing that enforces it. A subdirectory would log correctly, but every
    stage after this one also assumes the shell starts at the root.
    """
    repo = Path(a.repo).resolve()
    cwd = Path.cwd().resolve()
    problems, notes = [], []

    if cwd != repo:
        problems.append(
            f"the shell is in {cwd}\n    but the repository under review is {repo}.\n"
            "    Run the review from the repository's root: from anywhere outside it, the\n"
            "    access-log hook records reads against the wrong store, and the later stages\n"
            "    assume they start at the root.")

    cfg = read_config(repo)
    if not cfg:
        problems.append(f"no install record at {config_path(repo)}.\n"
                        f"    Run: python3 <review-kit>/bin/install.py --repo {repo}")
    else:
        kit = Path(cfg.get("kit", "")) if cfg.get("kit") else None
        here = Path(__file__).resolve().parent.parent
        if not kit or not kit.exists():
            problems.append(f"the recorded kit path {cfg.get('kit')!r} does not exist. "
                            "Re-run install.py.")
        elif kit.resolve() != here:
            notes.append(f"this is {here}, but the install recorded {kit}. The skills a "
                         "reviewer reads may not be the ones you just edited.")
        notes.append(f"summary: {summary_path(repo)}")

    store = store_path(repo)
    if not store.is_dir():
        problems.append(f"no review store at {store}. Run install.py --repo {repo}")

    hooks = hook_wired(repo)
    if hooks:
        notes.append("access hook: " + ", ".join(str(h) for h in hooks)
                     + " — UNVERIFIED until a run produces a log; `audit` will say so")
    else:
        notes.append("access hook: not wired, so `review.py audit` is unavailable and every "
                     "reviewer's account of what it read is taken on trust")

    for n in notes:
        print(f"  {n}")
    if problems:
        print(f"\npreflight: {len(problems)} problem(s)", file=sys.stderr)
        for x in problems:
            print(f"  - {x}", file=sys.stderr)
        sys.exit(2)
    print(f"preflight OK — {repo}")


# ---------------------------------------------------------------- estimate

# Measured once, from the development runs recorded in DESIGN.md: a reviewer costs roughly
# 145k tokens before it has read a single line of the repository — the contract, its skill,
# the probe table, its own reasoning and its output. The reconciler costs less source and
# more verification. These are the kit's own one-off measurements, not a general law, and
# --floor / --reconciler exist so a run that measures its own can say so.
REVIEWER_FLOOR = 145_000
RECONCILER_COST = 250_000
DEFAULT_REVIEWERS = 4

# Characters per token. English prose runs near 4; code is denser — more punctuation, more
# short identifiers — so 3.5 is the working figure and the range below is deliberately wide.
CHARS_PER_TOKEN = 3.5


def human(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def cmd_estimate(a):
    """Count what a full review would actually read, and price it, BEFORE anything is spawned.

    A fan-out of four Opus reviewers over a large repository can exhaust a weekly limit before
    the first one writes its findings file — full cost, no artefact. The two numbers that
    decide that are the file count and the line count, and both are already in the manifest.
    """
    man = read_json(a.manifest, "manifest")
    repo = Path(man.get("repo", "?"))
    review = man.get("files", [])
    ref = man.get("reference", [])
    files, lines = len(review), sum(f.get("lines", 0) for f in review)

    chars = 0
    missing = 0
    for f in review:
        q = repo / f["path"]
        try:
            chars += q.stat().st_size
        except OSError:
            missing += 1
    src = int(chars / a.chars_per_token)

    n = a.reviewers
    per_reviewer = a.floor + src
    reviewers_total = n * per_reviewer
    total = reviewers_total + a.reconciler

    print(f"estimate for {repo}")
    print(f"  review tier      {files:>6} files, {lines:>7} lines, {human(chars)} characters"
          + (f"  ({missing} unreadable, not counted)" if missing else ""))
    if ref:
        print(f"  reference tier   {len(ref):>6} files — described and queried, never read, "
              "so they cost almost nothing")
    print(f"  source in context ~{human(src)} tokens, for a reviewer that reads all of it")
    print()
    print(f"  {n} reviewer(s) x ({human(a.floor)} protocol floor + {human(src)} source)"
          f" = {human(reviewers_total)}")
    print(f"  reconciler                                          = {human(a.reconciler)}")
    print(f"  {'-' * 58}")
    print(f"  ESTIMATED TOTAL                                       {human(total)} tokens")
    print()
    print(f"  Basis: {a.chars_per_token} characters per token; a {human(a.floor)} floor per "
          f"reviewer and {human(a.reconciler)} for the reconciler,")
    print("  measured once from this kit's own development runs (see DESIGN.md). It is an "
          "ESTIMATE, not a")
    print("  measurement of this run, and a reviewer that reads only part of its scope costs "
          "less.")
    print()
    print("  STOP HERE if that is more than you want to spend. Smaller options, in order of "
          "how much they save:")
    print("    · review a window instead of everything:  scope --since <ref>")
    print("    · drop a reviewer leg (never fresh-eyes): estimate --reviewers 3")
    print("    · run one single-purpose skill on the files you actually care about")

    if a.budget is not None:
        if total > a.budget:
            print()
            die(f"estimated {human(total)} exceeds the stated budget of {human(a.budget)}. "
                "Narrow the scope or raise the budget deliberately.", code=3)
        print(f"\n  within the stated budget of {human(a.budget)}.")


# ---------------------------------------------------------------- machine

# What a reviewer would want to import to re-derive a number. Order matters only for display.
PROBE_MODULES = ("numpy", "pandas", "scipy", "sklearn", "polars", "lightgbm", "xgboost",
                 "torch", "matplotlib", "yaml")

# Where a Python environment hides, relative to a repository or to $HOME. Deliberately shallow:
# a full-tree search on a repo with a vendored node_modules costs more than it is worth.
VENV_DIRS = (".venv", "venv", "env", ".env", ".virtualenv")


def _interpreter_modules(python: str, modules=PROBE_MODULES) -> dict:
    """Which of `modules` this interpreter can import, in ONE subprocess rather than ten."""
    script = ("import importlib.util as u,json,sys;"
              "print(json.dumps({m: u.find_spec(m) is not None for m in " + repr(list(modules)) + "}))")
    rc, out, _ = sh(f"{shlex.quote(python)} -c {shlex.quote(script)}")
    if rc != 0:
        return {m: False for m in modules}
    try:
        return json.loads(out)
    except ValueError:
        return {m: False for m in modules}


def find_environments(repo: Path = None):
    """Every Python environment on this machine that might let a reviewer re-derive a number.

    The kit used to probe `sys.executable` alone — the interpreter running this script, which
    on a machine whose work happens under `uv run` or in a per-project venv is the one
    interpreter with nothing installed. It then told four reviewers that nothing was runnable,
    so every `command` evidence came back `ran: false` and `validate` capped those findings at
    `likely`. The review was weaker than the machine allowed, and nothing said so.

    Searching is shallow and read-only: the repo's own venv directories, `~/.venvs/*`,
    `~/.virtualenvs/*`, conda prefixes, and whatever is on PATH.
    """
    seen, found = set(), []
    cands = [("running this script", sys.executable)]
    if repo:
        for d in VENV_DIRS:
            cands.append((f"{repo.name}/{d}", str(repo / d / "bin" / "python")))
    for parent in (Path.home() / ".venvs", Path.home() / ".virtualenvs"):
        try:
            for d in sorted(parent.iterdir()):
                cands.append((f"~/{parent.name}/{d.name}", str(d / "bin" / "python")))
        except OSError:
            pass
    for base in (Path.home() / "miniconda3" / "envs", Path.home() / "anaconda3" / "envs"):
        try:
            for d in sorted(base.iterdir()):
                cands.append((f"conda:{d.name}", str(d / "bin" / "python")))
        except OSError:
            pass
    rc, out, _ = sh("command -v python3 python 2>/dev/null")
    for line in out.splitlines():
        cands.append(("on PATH", line.strip()))

    for label, py in cands:
        if not py:
            continue
        # Deduplicate on the path AS GIVEN, never on its resolved target. A virtualenv's
        # bin/python is a symlink to the base interpreter, so resolving first collapsed every
        # venv onto /usr/bin/python3 and the search returned exactly the interpreter it was
        # written to look past. Running the symlink is what selects the venv's site-packages.
        key = os.path.abspath(os.path.expanduser(py))
        if key in seen or not Path(py).exists() or not os.access(py, os.X_OK):
            continue
        seen.add(key)
        mods = _interpreter_modules(py)
        n = sum(1 for v in mods.values() if v)
        found.append({"label": label, "python": py, "n_modules": n, "importable": mods})
    # Most useful first: the interpreter that can import the most of what a reviewer needs.
    found.sort(key=lambda e: -e["n_modules"])
    return found


def cmd_machine(a):
    cores = os.cpu_count() or 1
    usable_gb = total_gb = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mi = {l.split(":")[0]: l.split()[1] for l in f if ":" in l}
        usable_gb = round(int(mi.get("MemAvailable", 0)) / 1048576, 1)
        total_gb = round(int(mi.get("MemTotal", 0)) / 1048576, 1)
    except (OSError, ValueError, IndexError):
        pass
    st = os.statvfs(".")
    free_disk_gb = round(st.f_bavail * st.f_frsize / 1024**3, 1)
    rc, gpu, _ = sh("nvidia-smi -L")
    gpus = [l for l in gpu.splitlines() if l.strip()] if rc == 0 else []

    repo = Path(a.repo).resolve() if a.repo else None
    envs = find_environments(repo)
    if a.python:
        # An explicit --python wins outright: the caller knows something the search cannot,
        # such as that the project runs under `uv run`. It is still probed, not trusted.
        if not Path(a.python).exists():
            die(f"--python {a.python} does not exist.")
        mods = _interpreter_modules(a.python)
        # abspath, not resolve: a venv's python symlinks to the base interpreter, so
        # resolving here would discard every venv as a "duplicate" of the given one.
        given = os.path.abspath(os.path.expanduser(a.python))
        envs = ([{"label": "given with --python", "python": a.python,
                  "n_modules": sum(1 for v in mods.values() if v), "importable": mods}]
                + [e for e in envs
                   if os.path.abspath(os.path.expanduser(e["python"])) != given])
    best = envs[0] if envs else None
    # `importable` keeps its meaning — what a reviewer can actually use — but now answers for
    # the BEST interpreter on the machine rather than whichever one happens to run this file.
    frameworks = best["importable"] if best else {m_: False for m_ in PROBE_MODULES}

    m = {"cores": cores, "usable_gb": usable_gb, "total_gb": total_gb,
         "gpu": gpus or None, "free_disk_gb": free_disk_gb,
         "python": best["python"] if best else sys.executable,
         "python_label": best["label"] if best else "running this script",
         "importable": frameworks,
         # Every candidate, so a reviewer that needs a module the best one lacks can see
         # whether another environment has it rather than concluding it cannot check.
         "environments": [{k: e[k] for k in ("label", "python", "n_modules")} for e in envs]}
    if a.out:
        write_atomic(Path(a.out), json.dumps(m, indent=2))
    print(json.dumps(m, indent=2))
    if best and best["n_modules"] == 0:
        print("  note: no interpreter on this machine can import any of "
              + ", ".join(PROBE_MODULES) + ".\n  Reviewers will verify by reading. If the "
              "project uses `uv run` or a venv this search missed,\n  pass --python so they "
              "can re-derive numbers instead.", file=sys.stderr)
    elif best:
        print(f"  reviewers should run commands with: {best['python']}  "
              f"({best['label']}, {best['n_modules']}/{len(PROBE_MODULES)} modules)",
              file=sys.stderr)


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
        write_atomic(Path(a.out), json.dumps(signals, indent=2))
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
    ("nonatomic-write", "C-7", "a write straight to its final path — no write-then-rename. "
     "Includes the stdlib idioms, without which a pure-stdlib project gets a clean report it "
     "has not earned",
     r"\b(?:to_csv|to_parquet|torch\.save|np\.save|json\.dump|pickle\.dump)\s*\(|"
     r"\.write_text\s*\(|\.write_bytes\s*\(|\bopen\s*\([^)]*['\"][wa]"),
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

# Suffixes the regex probes actually sweep. Reported coverage must match this set: probe
# once claimed 13 files probed when it had swept 3, which overstates the denominator every
# other number is read against.
PROBE_EXT = {".py", ".pyx", ".sh", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sql",
             ".r", ".rmd", ".jl"}

MAX_HITS = 25


def cmd_probe(a):
    import ast as _ast
    man = read_json(a.manifest, "manifest")
    # The manifest already records which repo it describes. Trusting it rather than the
    # caller's cwd means probe cannot be pointed at the wrong tree by accident.
    # --repo first: its help calls it an override, and the manifest always winning meant it
    # silently did nothing when a manifest was moved between machines.
    repo = Path(a.repo or man.get("repo") or ".").resolve()
    files = [repo / f["path"] for f in man["files"]]
    texts = {}
    for p in files:
        try:
            texts[p] = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass

    probes = []
    for pid, rule, desc, pat in REGEX_PROBES:
        rx = re.compile(pat)
        hits, matched = [], {}
        for p, lines in texts.items():
            if p.suffix.lower() not in PROBE_EXT:
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

    swept = [q for q in texts if q.suffix.lower() in PROBE_EXT]
    out = {"repo": str(repo), "files_probed": len(swept),
           "files_in_review_tier": len(texts),
           "files_not_swept_by_regex_probes": sorted(
               str(q.relative_to(repo)) for q in texts if q.suffix.lower() not in PROBE_EXT),
           "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "A probe hit is a place to LOOK, not a finding. These are deliberately "
                   "over-inclusive; most hits are fine. Never file a finding because a probe "
                   "matched — read the code and decide. Never file one WITHOUT reading it.",
           "max_hits_per_probe": MAX_HITS, "probes": probes}
    write_atomic(Path(a.out), json.dumps(out, indent=2))
    nz = [p for p in probes if p["total_hits"]]
    print(f"probe: {len(swept)} of {len(texts)} review-tier files swept, "
          f"{len(probes)} probes, {len(nz)} with hits "
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


def manifest_spelling(repo, p):
    """A declared path as the manifest spells it — relative to the repository, normalised —
    when it lies inside the repository; otherwise unchanged."""
    if not repo or not isinstance(p, str):
        return p
    root = Path(repo)
    q = Path(os.path.normpath(Path(p) if os.path.isabs(p) else root / p))
    try:
        return q.relative_to(root).as_posix()
    except ValueError:
        return p


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
    # One spelling per file, the manifest's: `./a.py` and `/abs/repo/a.py` are `a.py`. Raw
    # strings made a declared `./a.py` a file read outside the manifest AND an unread
    # in-scope file, a SCOPE GAP for a file that had been read. `audit` matches the same way.
    read = [manifest_spelling(repo, r) for r in read]
    if manifest:
        if "files" not in manifest:
            v.append("manifest has no `files` key — it is not a scope manifest, or `scope` "
                     "did not finish writing it")
            manifest = None
    if manifest:
        refset = {f["path"] for f in manifest.get("reference", [])}
        inscope = {f["path"] for f in manifest["files"]}
        unread = inscope - set(read)
        bogus = set(read) - inscope
        if bogus:
            # Only the `other` bucket below is informational. Opaque and reference-tier files
            # ARE violations: they cannot have been read as claimed. A reviewer legitimately
            # opens things outside
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
        queried = {manifest_spelling(repo, q) for q in scope.get("files_queried") or []}
        opq = {f["path"] for f in manifest.get("reference", []) if f.get("kind") == "opaque"}
        if queried & opq:
            v.append(f"{len(queried & opq)} opaque file(s) listed in files_queried: "
                     + ", ".join(sorted(queried & opq)[:4])
                     + ". An opaque file cannot be queried either; it goes in not_reviewed.")
        # A path must be DECLARED, not merely mentioned. The previous substring test let
        # prose vouch for coverage: a reviewer filing `files_read: []` with the note
        # "I looked at a.py and b.py, honest" passed, because the names appeared somewhere
        # in the string. An entry now declares a path only by leading with it.
        declared = set()
        for entry in doc.get("not_reviewed", []):
            if isinstance(entry, dict) and entry.get("path"):
                declared.add(str(entry["path"]).strip())
                continue
            # Match a real in-scope path first; only parse one out of the text if none
            # matches. Splitting on whitespace cannot declare `outputs - Copy/note.md` at
            # all, so a reviewer with such a file had an unclearable SCOPE GAP and no way to
            # diagnose it — the error told it to lead with the path, which it had done.
            raw = str(entry).strip().strip("`'\"")
            matched = None
            for cand in sorted(unread, key=len, reverse=True):
                # The delimiter set includes a backtick, because the kit's own summary
                # renders these as `path` — reason, and a closing backtick immediately after
                # the path would otherwise stop a spaced path from ever matching.
                if raw == cand or (raw.startswith(cand)
                                   and raw[len(cand):len(cand) + 1] in
                                   (" ", ",", ";", ":", "`", "'", '"', "")):
                    matched = cand
                    break
            if matched:
                declared.add(matched)
                continue
            # Still strict: prose that merely mentions a filename declares nothing, which is
            # what this check exists for.
            head = re.split(r"[\s,;:]|—|--", raw, maxsplit=1)[0].strip().rstrip(",")
            head = head.strip("`'\"")
            if head:
                declared.add(head)
        undeclared = [q for q in sorted(unread) if q not in declared]
        if undeclared:
            v.append(f"SCOPE GAP: {len(unread)} of {len(inscope)} in-scope files not read, "
                     f"and {len(undeclared)} of those are not declared in not_reviewed: "
                     + ", ".join(undeclared[:6]) + ("…" if len(undeclared) > 6 else "")
                     + ". Each unread file needs its own not_reviewed entry LEADING with the "
                       "path — \"path/to/file.py — why it was not read\" — or an object "
                       "{\"path\": ..., \"reason\": ...}. Prose that merely mentions a "
                       "filename does not count as declaring it.")

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


# Suffixes where `#` begins a comment, so an anchor should be compared without it. Kept
# separate from CODE_EXT, which decides tiering, but derived from it rather than retyped:
# .R and .jl were missing here while present there, so editing a comment in an R script moved
# the fingerprint and the defect read as fixed. `.sql` comments with `--`, and `.Rmd` is
# Markdown around its chunks, where `#` is a heading — stripping it there deletes the line.
# Lower-cased on both sides: CODE_EXT is now normalised at definition, so subtracting the
# mixed-case literals would have removed nothing and quietly let `.rmd` into the set — where
# stripping `#` deletes a Markdown heading rather than a comment.
HASH_COMMENT_EXT = ((CODE_EXT - {".sql", ".rmd"})
                    | {".yml", ".yaml", ".toml", ".cfg", ".ini"})
CODE_SUFFIXES = {e.lower() for e in HASH_COMMENT_EXT}


def finding_fingerprint(f, repo: Path):
    """Stable across line moves and reruns; changes when the offending code changes.

    Deliberately does NOT include rule_id: two reviewers looking at the same defect
    routinely attribute it to different rules (or to none), and including the rule
    stops them colliding, which is the whole point. Over-clustering is the safe
    direction — the reconciler can split a cluster it disagrees with, but it cannot
    merge two findings it was never shown together.
    """
    path = f.get("file") or ""
    anchor, anchor_line, basis = "", "", "claim-slug"
    line = f.get("line")
    p = repo / path if path else None
    if p and p.exists() and isinstance(line, int):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if 1 <= line <= len(lines):
                window = lines[max(0, line - 1): line + 1]
                strip = p.suffix.lower() in CODE_SUFFIXES
                anchor = " ".join(normalize_anchor(x, strip) for x in window).strip()
                # Two lines give the fingerprint its uniqueness; ONE line is what the defect
                # actually sits on. Testing presence against the pair meant that editing the
                # next, unrelated line made the anchor absent and carryover concluded
                # "plausibly-fixed" — closing a live critical defect, permanently, because
                # summarize renders only what is still open.
                anchor_line = normalize_anchor(lines[line - 1], strip)
                basis = "source-anchor"
        except OSError:
            pass
    if anchor:
        key = f"{path}|{anchor}"
    else:
        # No source anchor: still needs a stable id, so hash a slug of the claim — but the
        # anchor itself stays empty. `anchor_present` can only answer honestly about text
        # that is actually in the file, and a slug never is.
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", f.get("claim", ""))
        key = f"{path}|{' '.join(words[:8]).lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:12], basis, anchor, anchor_line


def cmd_fingerprint(a):
    repo = repo_from(manifest=a.manifest, override=a.repo)
    p = Path(a.findings)
    doc = read_json(p, "findings file")
    weak = 0
    for f in doc.get("findings", []):
        fp, basis, anchor, anchor_line = finding_fingerprint(f, repo)
        f["fingerprint"] = fp
        f["fingerprint_basis"] = basis
        # The anchor TEXT, not just its hash. A hash cannot be reversed, and without the text
        # a later run cannot ask the one question that matters: is this defect still here?
        f["fingerprint_anchor"] = anchor
        f["fingerprint_anchor_line"] = anchor_line
        f["fingerprint_line"] = f.get("line")
        weak += basis != "source-anchor"
    write_atomic(p, json.dumps(doc, indent=2))
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
    unstamped = [(d.get("reviewer"), f.get("id")) for d in docs
                 for f in d.get("findings", []) if not f.get("fingerprint")]
    if unstamped:
        die("these findings carry no fingerprint, so they would all group together and "
            "invent corroboration between unrelated defects — run `fingerprint` first:\n"
            + "\n".join(f"    {r}: {i}" for r, i in unstamped[:10]))
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
    write_atomic((run / "clusters.json"), json.dumps(out, indent=2))
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
        for f in read_json(p, "findings file").get("findings", []):
            if not f.get("fingerprint"):
                die(f"{p.name} finding {f.get('id')!r} has no fingerprint; run `fingerprint` "
                    "first or every unstamped finding matches every other.")
            curfp[f["fingerprint"]] = f
    prevfp = {}
    if prev and prev.exists():
        for p in sorted(prev.glob("findings-*.json")):
            for f in json.loads(p.read_text(encoding="utf-8")).get("findings", []):
                prevfp[f.get("fingerprint")] = f
    recurring = sorted(set(curfp) & set(prevfp))
    new = sorted(set(curfp) - set(prevfp))
    gone = sorted(set(prevfp) - set(curfp))
    out = {"recurring": [{"fingerprint": k, "file": curfp[k].get("file"),
                          "claim": curfp[k].get("claim")} for k in recurring],
           "new": len(new), "resolved_or_absent": len(gone),
           "prev_run": str(prev) if prev else None}
    write_atomic((cur / "recurrence.json"), json.dumps(out, indent=2))
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
                r = json.loads(Path(cand).read_text(encoding="utf-8")).get("repo")
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
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        die(f"{p} is not valid JSON: {e}")


def anchor_present(repo: Path, rel: str, anchor: str, anchor_line: str = ""):
    """Is this normalised source anchor still in the file? True, False, or None for "cannot tell".

    Position-independent on purpose: the defect has not been fixed merely because the lines
    around it moved. Only a change to the offending text itself counts.

    None is returned when the anchor occurs more than once, because an anchor that matches in
    several places locates nothing — and answering anyway would either bury a real defect or
    accuse a reviewer of missing one it had no way to find.
    """
    if not anchor and not anchor_line:
        return False
    p = repo / rel
    if not p.exists():
        return False
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    strip = p.suffix.lower() in CODE_SUFFIXES
    norm = [normalize_anchor(x, strip) for x in lines]

    if anchor_line:
        hits = sum(1 for x in norm if x and x == anchor_line)
        if hits > 1:
            # Occurs more than once, so it identifies nothing. Declining to answer is the
            # safe side: the caller treats "not present" as evidence of a fix.
            return None
        return hits == 1

    # A record stamped before the single-line anchor existed. The two-line window cannot
    # distinguish "the defect is gone" from "the line after it changed", so it never
    # answers False — an unverifiable defect stays open rather than closing wrongly.
    for i in range(len(norm)):
        if " ".join(norm[i:i + 2]).strip() == anchor:
            return True
    return None


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
            for f in json.loads(fp_.read_text(encoding="utf-8")).get("findings", []):
                if f.get("fingerprint"):
                    known.add(f["fingerprint"])
        except ValueError:
            pass
    if not known:
        if rec.get("items"):
            present = sorted(x.name for x in Path(run_dir).glob("findings-*.json"))
            what = (f"the {len(present)} findings file(s) in {run_dir} carry no fingerprints "
                    f"({', '.join(present)}) — has `fingerprint` run?"
                    if present else f"no findings-*.json in {run_dir}")
            die(f"{what}, so no reconciliation fingerprint can be "
                "checked against anything. This validation exists because an agent once "
                "abbreviated all 58 of them and nothing noticed; silently passing here would "
                "reinstate that.")
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
    man = _load_json(run / "manifest.json")
    if not man:
        die(f"no manifest.json in {run}")
    cov_path = Path(a.out)
    cov = _load_json(cov_path, {"files": {}, "runs": []})
    cov.setdefault("files", {})
    cov.setdefault("runs", [])
    # No reconciliation exists yet at this stage, so the directory name is the only source —
    # which is also the canonical one that resolve_run_id holds the other stages to.
    run_id = a.run_id or run.name

    review_tier = {f["path"]: f for f in man.get("files", [])}
    ref_tier = {f["path"]: f for f in man.get("reference", [])}

    # who read what, this run
    read_by, queried_by = {}, {}
    for fp_ in sorted(run.glob("findings-*.json")):
        d = _load_json(fp_)
        rev = d.get("reviewer") or fp_.stem
        # The manifest's spelling, as `validate` and `audit` use: a declared `./a.py` is a.py.
        for path in d.get("scope", {}).get("files_read") or []:
            read_by.setdefault(manifest_spelling(man.get("repo"), path), set()).add(rev)
        for path in d.get("scope", {}).get("files_queried") or []:
            queried_by.setdefault(manifest_spelling(man.get("repo"), path), set()).add(rev)

    # findings per file, this run
    per_file = {}
    for fp_ in sorted(run.glob("findings-*.json")):
        for f in _load_json(fp_).get("findings", []):
            if f.get("file"):
                k = manifest_spelling(man.get("repo"), f["file"])
                per_file[k] = per_file.get(k, 0) + 1

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
        # Model weights and images can be listed but never reviewed, so counting them as
        # "never reviewed" put a permanent, unactionable entry at the top of every report.
        e["opaque"] = meta.get("kind") == "opaque"
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
        if last in idx:
            e["runs_since_reviewed"] = len(cov["runs"]) - 1 - idx[last]
            e["runs_since_reviewed_exact"] = True
        elif last:
            # Reviewed once, longer ago than the retained run list. It used to get None here,
            # which put it in neither the "never reviewed" list nor the stale one — the
            # stalest files in the repository were the ones that disappeared from the report.
            e["runs_since_reviewed"] = len(cov["runs"])
            e["runs_since_reviewed_exact"] = False
        else:
            e["runs_since_reviewed"] = None
            e["runs_since_reviewed_exact"] = True
    cov["protocol_version"] = a.protocol_version or cov.get("protocol_version")
    cov["updated"] = now
    write_atomic(cov_path, json.dumps(cov, indent=2))

    files = cov["files"]
    never = [p for p, e in files.items()
             if not e.get("last_reviewed_run") and not e.get("opaque")]
    opaque = [p for p, e in files.items() if e.get("opaque")]
    stale = sorted((e.get("runs_since_reviewed") or 0, p) for p, e in files.items()
                   if e.get("runs_since_reviewed") and not e.get("opaque"))
    print(f"coverage: {len(files)} files tracked over {len(cov['runs'])} run(s)"
          + (" [replay — counters unchanged]" if replay else "") + f" → {cov_path}")
    print(f"  reviewed this run: {sum(1 for e in files.values() if e.get('last_reviewed_run') == run_id)}"
          f" · never reviewed: {len(never)}"
          + (f" · opaque, not reviewable: {len(opaque)}" if opaque else ""))
    if never:
        for p_ in sorted(never)[:8]:
            print(f"    NEVER REVIEWED  {p_}")
    for n, p_ in sorted(stale, reverse=True)[:5]:
        atleast = "" if files[p_].get("runs_since_reviewed_exact", True) else "at least "
        print(f"    stale ({atleast}{n} run(s))  {p_}")


def resolve_run_id(rec, run_dir=None, explicit=None):
    """Settle what this run is called, and refuse when two records disagree.

    Three persistent files key on a run id — coverage, open_defects and the ledger — and they
    reached it by two different routes: the run directory's basename for the first two, the
    reconciliation's own `run_id` for the third. When those differ, replay guards, `first_run`
    and `recurring_since` end up in incompatible namespaces and nothing notices.

    The directory name is canonical, because the orchestrator creates it and the reconciler
    merely copies the value into its file. A mismatch is therefore a transcription error, and
    this kit exists to catch exactly that kind of silence rather than paper over it.
    """
    from_dir = Path(run_dir).name if run_dir else None
    from_rec = rec.get("run_id") if rec else None
    rec_dir = Path(rec["run_dir"]).name if rec and rec.get("run_dir") else None

    for label, a_, b_ in (("the reconciliation's run_id and its own run_dir", from_rec, rec_dir),
                          ("the reconciliation's run_id and the run directory", from_rec, from_dir)):
        if a_ and b_ and a_ != b_ and not explicit:
            die(f"{label} disagree: {a_!r} vs {b_!r}. A run has one id — coverage, "
                "open_defects and the ledger all key on it, and two namespaces mean the "
                "replay guards and the carried-forward history silently stop matching. Fix "
                "the reconciliation, or pass --run-id deliberately.")
    if explicit:
        if from_dir and explicit != from_dir:
            print(f"  note: --run-id {explicit!r} overrides the run directory name "
                  f"{from_dir!r}.", file=sys.stderr)
        return explicit
    return from_rec or from_dir


def cmd_carryover(a):
    """Merge a reconciliation into the persistent open-defect record, and classify each
    previously-open defect against the current source."""
    run = Path(a.run_dir).resolve()
    repo = repo_from(run_dir=run, override=a.repo)
    rec = _load_json(a.reconciliation)
    if not rec:
        die(f"no reconciliation at {a.reconciliation}")
    run_id = resolve_run_id(rec, run_dir=run, explicit=a.run_id)
    report_fingerprints(rec, run)
    man = _load_json(run / "manifest.json")
    reviewed = {f["path"] for f in man.get("files", [])}
    full_scope = man.get("mode") == "full"

    known = _load_json(a.out, {"defects": {}})
    known.setdefault("defects", {})

    # anchors seen this run, so a carried defect can be matched to fresh evidence
    anchors = {}
    for fp_ in sorted(run.glob("findings-*.json")):
        for f in _load_json(fp_).get("findings", []):
            if f.get("fingerprint"):
                anchors[f["fingerprint"]] = {"anchor": f.get("fingerprint_anchor", ""),
                                             "anchor_line": f.get("fingerprint_anchor_line", ""),
                                             "file": f.get("file")}

    this_run, collisions = {}, {}
    for item in rec.get("items", []):
        fp_ = item.get("fingerprint")
        if not fp_:
            # Dropping it silently lost a confirmed critical from the carried history while
            # exiting 0, and `summarize` refuses the very same file. Two stages reading one
            # artefact must agree on what is acceptable in it.
            die(f"reconciliation item {item.get('where', 'no where')} "
                f"({item.get('title', 'no title')!r}) has no fingerprint, so it cannot be "
                "tracked across runs. Every item needs one, copied verbatim from a findings "
                "file.")
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
        anchor_line = d.get("anchor_line", "")
        if d.get("status") == "plausibly-fixed":
            # Already resolved once. Only the offending text COMING BACK reopens it — a file
            # dropping out of the review window must never resurrect a fixed defect.
            if ((anchor or anchor_line) and file_ in reviewed
                    and anchor_present(repo, file_, anchor, anchor_line) is True):
                d["status"], d["last_outcome"] = "open", "regressed"
                states.setdefault("regressed", []).append(fp_)
            continue
        if not anchor and not anchor_line:
            # No source anchor (a repo-wide or prose-level defect): presence cannot be
            # checked, so it is never auto-closed. Silence is not evidence of a fix.
            d["status"] = "open"
            d["last_outcome"] = "unanchored-not-checkable"
            states.setdefault("unanchored-not-checkable", []).append(fp_)
        elif full_scope and file_ and not (repo / file_).exists():
            # The file is gone and this run looked at the whole repository, so it was deleted
            # rather than merely left out of a window. Deletion removes the defect; carrying
            # it forever as "not re-checked" kept dead findings open indefinitely. Only ever
            # concluded on a full scope — under --since, absence means out-of-window.
            d["status"] = "plausibly-fixed"
            d["last_outcome"] = "file-deleted"
            d["resolved_run"] = run_id
            states.setdefault("file-deleted", []).append(fp_)
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
        elif anchor_present(repo, file_, anchor, anchor_line) is None:
            d["status"] = "open"
            d["last_outcome"] = "anchor-ambiguous"
            states.setdefault("anchor-ambiguous", []).append(fp_)
        elif anchor_present(repo, file_, anchor, anchor_line):
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
            "anchor": anchors.get(fp_, {}).get("anchor", ""),
            "anchor_line": anchors.get(fp_, {}).get("anchor_line", "")})
        if anchors.get(fp_, {}).get("file"):
            d["file"] = anchors[fp_]["file"]
        if anchors.get(fp_, {}).get("anchor"):
            d["anchor"] = anchors[fp_]["anchor"]
        if anchors.get(fp_, {}).get("anchor_line"):
            d["anchor_line"] = anchors[fp_]["anchor_line"]
        d.update({"where": item.get("where"), "severity": item.get("severity"),
                  "class": item.get("class"), "title": item.get("title"),
                  "last_seen_run": run_id})
        # The outcome follows the disposition. It used to be stamped "re-reported" for every
        # item including the fixed and the refuted ones, so the record said "found again,
        # still open" about a defect it had just marked fixed.
        if disp == "applied":
            # Retired. A reconciliation written before the kit stopped editing files may still
            # say this; its fix really was applied then, so the record stands. Nothing emits it
            # now — a defect the human fixes is closed by the NEXT run's anchor check, which
            # observes that the offending source is gone rather than asserting it.
            d["status"], d["resolved_run"] = "fixed", run_id
            d["last_outcome"] = "fixed-this-run"
        elif disp == "dropped":
            d["status"] = "dropped"
            d["last_outcome"] = "refuted-this-run"
        else:
            d["status"] = "open"
            d["last_outcome"] = "re-reported"
            states["re-reported"].append(fp_)

    known["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    write_atomic(Path(a.out), json.dumps(known, indent=2))

    still_open = {k: v for k, v in known["defects"].items() if v.get("status") == "open"}
    print(f"carryover: {len(known['defects'])} defect(s) tracked, {len(still_open)} open "
          f"→ {a.out}")
    if collisions:
        print(f"  note: {len(collisions)} fingerprint(s) carried findings with differing "
              "dispositions — two defects on one source line. The non-dropped one was kept; "
              "check they are genuinely the same defect:")
        for fp_, disps in list(collisions.items())[:6]:
            print(f"    {fp_}  {sorted(disps)}  {known['defects'][fp_].get('where')}")
    for st in ("re-reported", "reported-not-carried", "missed", "regressed",
               "unanchored-not-checkable", "anchor-ambiguous", "not-re-checked",
               "file-deleted", "plausibly-fixed"):
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


def cmd_store(a):
    """Print the paths a run needs, so no caller ever re-derives one.

    All three come from the install record rather than from convention: the store, the
    summary's claimed filename, and where the kit itself lives. The orchestrator used to
    hard-code `.review/`, and wrote a whole run inside the repository it was auditing.
    """
    repo = Path(a.repo).resolve()
    if a.summary:
        print(summary_path(repo))
    elif a.kit:
        kit = read_config(repo).get("kit")
        if not kit:
            die(f"no kit path recorded for {repo}. Run install.py --repo {repo} first.")
        print(kit)
    else:
        print(store_path(repo))


# ---------------------------------------------------------------- audit (scope honesty)

# Tools whose records show a file being modified, not read. The hook logs them; `audit` never
# lets them support a claim to have read the file.
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")

def git_toplevel(repo: Path) -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo,
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"{repo} is not inside a git repository, so `residue` cannot compare the working "
            "tree. Say in the handover that residue went unchecked this run.")
    return Path(r.stdout.strip())


def worktree_state(repo: Path):
    """Every path `git status` reports under `repo`, ignored files included, as {path: code}.

    Paths are relative to the repository's top level, which is how git prints them even when
    `repo` is a subdirectory; `-- .` limits the report to that subdirectory.
    """
    top = git_toplevel(repo)
    # Not `sh`: it strips the output, and the first entry's status code can begin with a space.
    # Bytes, not text: with -z git prints names unescaped, and one untracked file whose name
    # is not UTF-8 made this — the first command of a run — raise and abort the audit.
    # `--untracked-files=normal` reports an untracked or ignored directory as one entry; `all`
    # would list every file inside .venv/ and the data directories, on every run.
    r = subprocess.run(["git", "status", "--porcelain=v1", "--ignored", "-z",
                        "--untracked-files=normal", "--", "."], cwd=repo, capture_output=True)
    if r.returncode != 0:
        die(f"git status failed in {repo}: {os.fsdecode(r.stderr).strip()}")
    out = os.fsdecode(r.stdout)
    state, items = {}, out.split("\0")
    i = 0
    while i < len(items):
        e = items[i]
        i += 1
        if len(e) < 4:
            continue
        code, path = e[:2], e[3:]
        if code[0] in "RC":
            i += 1          # a rename carries its source path as the next entry
        # A file already modified before the run keeps the same code if a reviewer modifies
        # it again, so a modified or untracked file is identified by its content as well.
        # Ignored entries are not hashed: they are caches and data, often very large.
        f = top / path
        if code != "!!" and not path.endswith("/") and f.is_file():
            try:
                code = f"{code}:{sha256_of(f)[:16]}"
            except OSError:
                pass
        state[path] = code
    return state


def cmd_residue(a):
    """Snapshot the working tree before reviewers run; afterwards, name what they left behind.

    Every reviewer is told to edit nothing, and the kit says it edits nothing. The first live
    run still left a `.pytest_cache/` in the repository from a `pytest --collect-only`, and it
    was only noticed because the orchestrator happened to look. Ignored files are included:
    caches are exactly what a reviewer leaves, and `.gitignore` hides them from a plain status.
    """
    repo = Path(a.repo).resolve()
    snap = Path(a.run_dir) / "tree-before.json"
    if a.save:
        if snap.exists() and not a.force:
            print(f"residue: keeping the existing snapshot {snap} (this run is being resumed)")
            return
        write_atomic(snap, json.dumps(worktree_state(repo), indent=1))
        print(f"residue: snapshot of the working tree → {snap}")
        return
    # Existence, not content: an empty snapshot is a clean tree. `_load_json` returns {} for a
    # missing file, so the check used to test for None, never fire, and report every untracked
    # file in the repository as left behind by the reviewers.
    if not snap.exists():
        die(f"no snapshot at {snap}: run `residue --save` before the reviewers, or say in the "
            "handover that residue went unchecked this run")
    before = _load_json(snap)
    summary_rel = str((repo / summary_name(repo)).relative_to(git_toplevel(repo)))
    after = worktree_state(repo)
    new = {p: c for p, c in after.items()
           if before.get(p) != c and p.rstrip("/") != summary_rel}
    gone = sorted(p for p in before if p not in after)
    if not new and not gone:
        print("residue: the working tree is as the reviewers found it")
        return
    print("RESIDUE: the working tree changed while the review ran "
          "(paths relative to the repository's top level).")
    for p, c in sorted(new.items()):
        code, prev = c[:2], before.get(p)
        if prev is None:
            what = {"!!": "ignored file left behind", "??": "untracked file left behind"}.get(
                code, f"status {code.strip()!r} (was clean)")
        else:
            what = f"changed during the run (status {code.strip()!r}, was {prev[:2].strip()!r})"
        print(f"    {p}   {what}")
    for p in gone:
        print(f"    {p}   gone (was {before[p][:2].strip()!r})")
    print("  Name these in the handover. Do not delete them yourself: the person decides.")
    sys.exit(1)


def declared_path(repo: Path, d: str) -> dict:
    """A declared files_read entry as audit compares it: as written and resolved, and inside?"""
    q = Path(d) if os.path.isabs(d) else repo / d
    logical = Path(os.path.normpath(q))
    try:
        resolved = q.resolve()
    except (OSError, RuntimeError):
        resolved = logical

    def within(p):
        try:
            p.relative_to(repo)
            return True
        except ValueError:
            return False
    return {"logical": logical, "resolved": resolved,
            "inside": within(logical) or within(resolved)}


def cmd_audit(a):
    """Compare declared files_read against the harness-written access log.

    The access log is produced by a PostToolUse hook, outside any agent's reach. A
    reviewer can overstate files_read in its own JSON; it cannot manufacture entries
    here without actually issuing the tool calls.
    """
    repo = Path(a.repo).resolve()
    logp = Path(a.access_log)
    if not logp.exists():
        die(f"no access log at {logp} — the hook is not wired up; see review-kit/README.md")

    if not a.list_agents and not a.findings:
        die("audit needs --findings (or --list-agents to see which ids are in the log).")
    if a.list_agents:
        # Without this, the skill told an orchestrator to pass --agent and gave it no way to
        # learn the ids, so the documented loop could not be run at all. The ids are in the
        # log; what they cannot be is guessed.
        seen = {}
        for line in logp.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            e = seen.setdefault(rec.get("agent") or "(no agent id — ignored by audit)",
                                {"calls": 0, "paths": set()})
            e["calls"] += 1
            if rec.get("tool") not in WRITE_TOOLS:
                # Matching a reviewer by files it wrote would pair it on evidence audit
                # itself refuses to count.
                e["paths"].update(rec.get("paths", []))
        if not seen:
            die(f"{logp} holds no usable records.")
        print(f"agents in {logp}:")
        for agent, e in sorted(seen.items(), key=lambda kv: -kv[1]["calls"]):
            print(f"  {agent}   {e['calls']} call(s), {len(e['paths'])} distinct path(s) read")
            for q in sorted(e["paths"])[:6]:
                print(f"      {q}")
        print("\nMatch each id to a reviewer by the paths it touched, then audit each findings "
              "file with that id.\nIf you cannot establish the mapping, do not guess: skip "
              "`audit` and say in the handover that\nscope honesty was not checked this run.")
        return

    doc = read_json(a.findings, "findings file")
    declared = set(doc.get("scope", {}).get("files_read") or [])
    touched = set()
    agents_seen = set()
    commands, truncated = [], []

    # The log is appended to forever, across every run of this repository. Without a floor on
    # the timestamp, a record written three weeks ago vouched for a file this run never
    # opened — so the one check an agent cannot forge could still be satisfied by history,
    # and on a second audit of the same repo that covers most of the scope.
    since = a.since
    if not since and a.run_dir:
        man = _load_json(Path(a.run_dir) / "manifest.json")
        since = man.get("generated")
        if not since:
            die(f"{a.run_dir}/manifest.json records no `generated` time, so there is nothing "
                "to date the access records against. Pass --since explicitly.")
    if not since:
        die("audit needs a floor on the access log: pass --run-dir (the run's manifest.json "
            "carries its start time) or --since <ISO timestamp>. Without one, a record from "
            "any earlier run supports this run's claims.")
    try:
        floor = datetime.datetime.fromisoformat(since)
    except ValueError:
        die(f"--since {since!r} is not an ISO timestamp.")
    if floor.tzinfo is None:
        floor = floor.replace(tzinfo=datetime.timezone.utc)
    stale = unattributed = 0
    for line in logp.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # A record with no timestamp predates this check and cannot be placed in time.
        # Excluding it is the safe direction: it can no longer vouch for anything.
        try:
            t = datetime.datetime.fromisoformat(rec["t"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
        except (KeyError, TypeError, ValueError):
            stale += 1
            continue
        if t < floor:
            stale += 1
            continue
        if not rec.get("agent"):
            # A record with no agent id is unattributable, so it supports nobody's claim —
            # with or without --agent. Claude Code always sends an id; the only id-less
            # records seen so far were test payloads piped in by hand, and an earlier version
            # let them vouch for whichever reviewer was the only named one in the log.
            unattributed += 1
            continue
        agents_seen.add(rec["agent"])
        if a.agent and rec["agent"] != a.agent:
            continue
        if rec.get("tool") in WRITE_TOOLS:
            # Logged as evidence that something was modified, never as a read: writing a
            # file is not reading it. An honest reviewer that edits a file it did not create
            # will normally have Read it first, and that Read is what supports the claim.
            continue
        if rec.get("reads") and rec.get("cmd"):
            commands.append((rec["cmd"], rec.get("cwd") or str(repo)))
        if rec.get("truncated"):
            truncated.append(rec.get("cmd") or rec.get("tool"))
        for p in rec.get("paths", []):
            q = Path(p) if os.path.isabs(p) else repo / p
            try:
                touched.add(q.resolve())
            except (OSError, RuntimeError):
                pass
    if not a.agent and len(agents_seen) > 1:
        die(f"this access log holds {len(agents_seen)} distinct agents and no --agent was "
            "given, so every agent's reads would vouch for this one's claims. Pass --agent "
            "with the subagent id for this findings file. Agents in the log: "
            + ", ".join(sorted(agents_seen)[:6]))
    # Declared and logged paths are compared as resolved absolute paths, so every spelling of
    # one file matches: `a.py`, `./a.py`, `/abs/repo/a.py`, and `data/d.py` read through a
    # symlinked `data/`. Comparing repo-relative strings reported a declared `./a.py` or
    # absolute path as an UNSUPPORTED CLAIM although it had been read, and could never credit
    # a file under a symlinked directory at all.
    canon = {d: declared_path(repo, d) for d in declared}
    # A path the reviewer read from outside the repository — the kit's own FINDINGS.md, for
    # instance — cannot be checked against this repository's log. `validate` calls reading
    # outside the manifest "not a violation"; the two must agree. Inside means inside by path
    # components, as written or resolved: `../kit/x.md` is outside, and `/x/r2/z.md` is not
    # inside `/x/r` merely because the strings share a prefix.
    outside = sorted(d for d, c in canon.items() if not c["inside"])
    checkable = {d for d, c in canon.items() if c["inside"]}
    seen = {d for d in checkable if canon[d]["resolved"] in touched}
    unsupported = sorted(checkable - seen)
    # A file whose name appears in one of this agent's reading commands, though the hook could
    # not resolve the command to it — a `$(...)`, an alias, a loop deeper than it unrolls. It is
    # neither proven read nor proven unread, and reporting it as an UNSUPPORTED CLAIM is what
    # made the first live run accuse every reviewer of reads its transcript shows it made.
    # Only commands that ran a reader count, so a file named only in an `ls` or an `echo`
    # stays UNSUPPORTED. The command must name THIS file: its path as seen from the command's
    # cwd, or its name together with the directory it is in (`cd src/x && cat $(echo a.py)`).
    # A bare stem matched any search word, and a bare basename let `cat docs/README.md`
    # turn a fabricated claim to have read src/README.md into one that merely needed checking.
    def mentions(cmd, s):
        return re.search(r"(?<![\w.\-/])" + re.escape(s) + r"(?![\w.\-/])", cmd) is not None

    def named_in(path):
        full = canon[path]["logical"]
        for cmd, cwd in commands:
            here = os.path.relpath(full, cwd)
            if mentions(cmd, here) or mentions(cmd, str(full)) or mentions(cmd, path):
                return cmd
            parent = os.path.relpath(full.parent, cwd)
            if mentions(cmd, full.name) and parent != "." and (
                    mentions(cmd, parent) or mentions(cmd, str(full.parent))):
                return cmd
        return None
    unresolved = {p: named_in(p) for p in unsupported}
    unresolved = {p: c for p, c in unresolved.items() if c}
    unsupported = [p for p in unsupported if p not in unresolved]
    name = doc.get("reviewer", "?")
    print(f"audit {name}: {len(checkable)} of {len(declared)} declared file(s) are checkable "
          f"against this log; {len(seen)} of those appear in it")
    print(f"  records considered: those at or after {floor.isoformat()}"
          + (f"; {stale} earlier or undated record(s) ignored" if stale else "")
          + (f"; {unattributed} record(s) with no agent id ignored" if unattributed else ""))
    if outside:
        print(f"  note: {len(outside)} declared path(s) lie outside {repo} and cannot be "
              "checked against this log: " + ", ".join(outside[:4]), file=sys.stderr)
    if unresolved:
        print(f"  UNRESOLVED: {len(unresolved)} declared file(s) are named in a reading command "
              "the hook could not resolve to a path. Neither proven read nor unread — check "
              "the command before reporting either:")
        for p, c in sorted(unresolved.items())[:20]:
            one = " ".join(c.split())
            print(f"    {p}\n      in: {one[:160]}{'…' if len(one) > 160 else ''}")
    if truncated and (unsupported or unresolved):
        print(f"  note: {len(truncated)} of this agent's commands matched more files than the hook "
              "follows (500 per glob or loop, 2,000 per record, or its work limit), so a file "
              "below may have been read by one of them:")
        for c in truncated[:3]:
            one = " ".join(str(c).split())
            print(f"      {one[:160]}")
    if unsupported:
        print(f"  UNSUPPORTED CLAIM: {len(unsupported)} declared file(s) appear in no tool call:")
        for p in unsupported[:20]:
            print(f"    {p}")
        sys.exit(1)
    if unresolved:
        sys.exit(3)
    print("  every declared file appears in the access log")


# ---------------------------------------------------------------- summarize + ledger

def cmd_summarize(a):
    r = read_json(a.reconciliation, "reconciliation")
    run_dir = Path(a.run_dir) if getattr(a, "run_dir", None) else Path(a.reconciliation).parent
    report_fingerprints(r, run_dir)
    missing = [k for k in ("run_id", "date", "run_dir", "items") if k not in r]
    if missing:
        die(f"reconciliation is missing required top-level key(s): {', '.join(missing)}. "
            "The shape is {run_id, date, run_dir, head?, machine?, not_reviewed?, "
            "confidence_note?, items[]} — see Stage 5 of the "
            "reconcile-reviews skill.")
    items = r["items"]
    for i, x in enumerate(items):
        # Every key the renderer goes on to read without a .get(). It used to check three and
        # then index five, so a reconciliation missing `defect` died with a KeyError rather
        # than a sentence naming the item.
        for k in ("disposition", "fingerprint", "where", "severity", "title", "defect", "fix"):
            if k not in x:
                die(f"reconciliation item {i} ({x.get('where', 'no where')}) is missing "
                    f"'{k}'. Every item needs: disposition, fingerprint, where, severity, "
                    "title, defect, fix.")
    # `applied` is retired: the reconciler no longer edits the repository, so nothing is
    # fixed by the time this renders. Records written before that change still carry it, and
    # they are counted as open rather than dropped on the floor.
    legacy_applied = [x for x in items if x["disposition"] == "applied"]
    openi = [x for x in items if x["disposition"] in ("open", "applied")]
    deferred = [x for x in items if x["disposition"] == "deferred"]
    dropped = [x for x in items if x["disposition"] == "dropped"]
    unver = [x for x in items if x["disposition"] == "unverifiable"]
    order = {s: i for i, s in enumerate(SEVERITIES)}
    openi.sort(key=lambda x: (order.get(x.get("severity"), 9), x.get("where") or ""))

    L = []
    w = L.append
    m = r.get("machine", {})
    w(f"# Review summary — {r['date']} @ {r.get('head', '?')}\n")
    w(f"**{len(openi)} confirmed defects for you "
      f"({sum(1 for x in openi if x.get('severity') == 'critical')} critical).** "
      f"{len(deferred)} more are deferred for compute — checkable, but not on this machine. "
      + (f"{len(dropped)} reported finding{'s were' if len(dropped) != 1 else ' was'} "
         "refuted and dropped" if dropped else "Nothing was refuted")
      + (f"; {len(unver)} could not be settled on any machine" if unver else "") + ".")
    w(f"Machine: {m.get('cores')} cores, {m.get('usable_gb')} GB usable, "
      f"{'GPU: ' + ', '.join(m['gpu']) if m.get('gpu') else 'no GPU'}.")
    # The run directory is an absolute path under someone's home. This file is committed, so
    # it names the run instead and lets the reader resolve it: `review.py store --repo .`
    w(f"Full detail: run `{r.get('run_id', '?')}` under this repository's review store "
      f"(`review.py store --repo .`)")
    w("\nNothing in your working tree was changed. Every defect below is yours to fix or "
      "decline; the ones marked\n*mechanical* are unambiguous and local, and are the cheapest "
      "place to start.\n")
    if legacy_applied:
        w(f"> {len(legacy_applied)} item(s) in this reconciliation use the retired `applied` "
          "disposition, from a run made when the\n> reconciler still edited files. They are "
          "listed below as open; check your working tree before acting on them.\n")
    w("---\n")

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
                w(f"| `{cell(x['where'])}` | {cell(x['defect'])} | {cell(x['fix'])} |")
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
        carried = json.loads(Path(a.open_defects).read_text(encoding="utf-8")).get("defects", {})
    here = {x.get("fingerprint") for x in items}
    missed = [d for k, d in carried.items()
              if d.get("status") == "open" and d.get("last_outcome") == "missed" and k not in here]
    regressed = [d for k, d in carried.items()
                 if d.get("status") == "open" and d.get("last_outcome") == "regressed"
                 and k not in here]
    unchecked = [d for k, d in carried.items()
                 if d.get("status") == "open" and d.get("last_outcome") == "not-re-checked"
                 and k not in here]
    # Every open outcome must surface somewhere. Three of these states were added after this
    # renderer was written and were dropped in silence, which is how a carried defect
    # disappears without anyone deciding that it should.
    undecidable = [d for k, d in carried.items()
                   if d.get("status") == "open" and k not in here
                   and d.get("last_outcome") in ("reported-not-carried",
                                                 "unanchored-not-checkable",
                                                 "anchor-ambiguous")]
    if missed or unchecked or regressed or undecidable:
        w(f"## Still open from earlier runs "
          f"({len(missed) + len(unchecked) + len(regressed) + len(undecidable)})\n")
        if regressed:
            w(f"### Regressed — fixed once, and the offending code is back ({len(regressed)})\n")
            w("These were confirmed, then resolved in a later run, and the source text they "
              "were anchored to has now reappeared. No reviewer raised them this run.\n")
            w("| where | severity | defect | first seen |")
            w("|---|---|---|---|")
            for d in sorted(regressed, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{cell(d.get('where'))}` | {cell(d.get('severity'))} "
                  f"| {cell(d.get('title'))} | {cell(d.get('first_run'))} |")
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
                w(f"| `{cell(d.get('where'))}` | {cell(d.get('severity'))} "
                  f"| {cell(d.get('title'))} | {cell(d.get('first_run'))} |")
            w("")
        if unchecked:
            w(f"### Not re-checked this run — file was out of scope ({len(unchecked)})\n")
            w("| where | severity | defect | first seen |")
            w("|---|---|---|---|")
            for d in sorted(unchecked, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{cell(d.get('where'))}` | {cell(d.get('severity'))} "
                  f"| {cell(d.get('title'))} | {cell(d.get('first_run'))} |")
            w("")

        if undecidable:
            w(f"### Carried forward, state undecided ({len(undecidable)})\n")
            w("Neither confirmed fixed nor shown still present. Each says which, and why "
              "nothing here could settle it.\n")
            w("| where | severity | defect | why undecided |")
            w("|---|---|---|---|")
            reason = {"reported-not-carried": "a reviewer filed it; reconciliation did not carry it",
                      "unanchored-not-checkable": "no source anchor, so presence cannot be tested",
                      "anchor-ambiguous": "its anchor occurs more than once, so it locates nothing"}
            for d in sorted(undecidable, key=lambda x: order.get(x.get("severity"), 9)):
                w(f"| `{cell(d.get('where'))}` | {cell(d.get('severity'))} "
                  f"| {cell(d.get('title'))} "
                  f"| {cell(reason.get(d.get('last_outcome'), d.get('last_outcome')))} |")
            w("")

    if unver:
        w(f"## Open questions ({len(unver)}) — not settleable on any machine\n")
        for x in unver:
            w(f"- `{x['where']}` — {x['defect']}")
        w("")

    w("## Not reviewed\n")
    for x in r.get("not_reviewed", []):
        w(f"- {bullet(x)}")
    w("")
    w("## Confidence\n")
    w(r.get("confidence_note", ""))

    write_atomic(Path(a.out), "\n".join(L) + "\n")
    # Matches the file's own headline. Deferred items are confirmed as *findings* but not as
    # defects-with-a-verdict, and reporting one total here and a different one at the top of
    # the file made the two look like a disagreement.
    print(f"summary: {len(openi)} confirmed for the human, {len(deferred)} deferred for "
          f"compute, {len(dropped)} dropped → {a.out}")
    if legacy_applied:
        print(f"  note: {len(legacy_applied)} item(s) carry the retired `applied` disposition "
              "and are counted as open.", file=sys.stderr)
    print("counts derived from reconciliation.json; headers cannot disagree with contents")


def cell(v) -> str:
    """One Markdown table cell. A defect that mentions `a | b` used to add a column."""
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def bullet(entry) -> str:
    """Render a not_reviewed entry, which the contract allows as a string or an object.

    Only the string form was handled, so an object arrived in the committed deliverable as a
    Python dict repr — `{'path': 'data/big.csv', 'reason': ...}` — in the file a human reads.
    """
    if isinstance(entry, dict):
        path, reason = entry.get("path"), entry.get("reason")
        return f"`{path}` — {reason}" if reason else f"`{path}`"
    return str(entry)


def rate_txt(v):
    return "—" if v is None else f"{v:.0%}"


def cmd_ledger(a):
    r = read_json(a.reconciliation, "reconciliation")
    if "items" not in r:
        die(f"{a.reconciliation} has no `items` key, so there is nothing to tally.")
    run_id = resolve_run_id(r, run_dir=a.run_dir, explicit=a.run_id)
    items = r["items"]
    rows = []
    UNATTRIB = "(unattributed)"
    reviewers = sorted({rv for x in items for rv in (x.get("corroborated_by") or [UNATTRIB])})
    for rev in reviewers:
        mine = [x for x in items if rev in (x.get("corroborated_by") or [UNATTRIB])]
        # "applied" is retired but still counted, so a row from an older run stays
        # comparable with a new one: both mean "the reconciler confirmed this".
        conf = sum(1 for x in mine if x["disposition"] in ("applied", "open"))
        ref = sum(1 for x in mine if x["disposition"] == "dropped")
        dfr = sum(1 for x in mine if x["disposition"] == "deferred")
        unv = sum(1 for x in mine if x["disposition"] == "unverifiable")
        rows.append({"run_id": run_id, "date": r["date"], "reviewer": rev,
                     "reported": len(mine), "confirmed": conf, "refuted": ref,
                     "deferred": dfr, "unverifiable": unv,
                     "rate_verifiable": round(conf / (conf + ref), 3) if conf + ref else None,
                     "machine": r.get("machine")})
    # coverage guards against replay; ledger did not, so re-running the documented Stage 6
    # block duplicated rows and silently doubled the denominator of every rate.
    seen = set()
    outp = Path(a.out)
    if outp.exists():
        for line in outp.read_text(encoding="utf-8").splitlines():
            try:
                prev = json.loads(line)
            except ValueError:
                continue
            seen.add((prev.get("run_id"), prev.get("reviewer")))
    fresh = [r for r in rows if (r["run_id"], r["reviewer"]) not in seen]
    if len(fresh) != len(rows):
        print(f"  note: {len(rows) - len(fresh)} row(s) for this run are already in the "
              "ledger and were not appended again (replay).", file=sys.stderr)
    # One write, not one per row: a crash midway through appending used to leave a partial
    # line, and every later read of the ledger then skipped the truncated row in silence.
    if fresh:
        with open(a.out, "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(row) + "\n" for row in fresh))
    for row in rows:
        print(f"  {row['reviewer']:18} {row['reported']:3} reported → {row['confirmed']} confirmed, "
              f"{row['refuted']} refuted, {row['deferred']} deferred  "
              f"({rate_txt(row['rate_verifiable'])})")
    print(f"ledger: {len(fresh)} rows appended → {a.out}")


# ---------------------------------------------------------------- main

def main():
    # The console output carries arrows, em dashes and middots. Under a C/POSIX locale stdout
    # defaults to ascii and every one of them raises mid-print, after the work is done and
    # the file is written — so the command fails while having succeeded. Writing is what
    # matters; a replacement character in a terminal is not.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(prog="review.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scope", help="compute the review window + file manifest")
    p.add_argument("--repo", required=True); p.add_argument("--out", required=True)
    p.add_argument("--since")
    p.add_argument("--full", action="store_true",
                   help="whole repository. This is already the default; the flag exists so a "
                        "caller can say so explicitly, and it is an error to combine it with "
                        "--since")
    p.add_argument("--list", action="store_true")
    p.add_argument("--reference-threshold", type=int, default=500,
                   help="line count above which a file is demoted to the reference tier — "
                        "described in the manifest and queried, never read. Tabular and "
                        "opaque files are reference-tier at any size; source and Markdown "
                        "are never demoted however long (default 500)")
    p.set_defaults(fn=cmd_scope)

    p = sub.add_parser("runs", help="list this repo's runs and say which can be resumed")
    p.add_argument("--repo", required=True)
    p.add_argument("--limit", type=int, default=5, help="how many recent runs to show")
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("preflight", help="check the conditions a review depends on")
    p.add_argument("--repo", required=True); p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("estimate", help="price a full review BEFORE spawning anything")
    p.add_argument("--manifest", required=True)
    p.add_argument("--reviewers", type=int, default=DEFAULT_REVIEWERS,
                   help=f"how many reviewer legs (default {DEFAULT_REVIEWERS})")
    p.add_argument("--floor", type=int, default=REVIEWER_FLOOR,
                   help="tokens a reviewer costs before reading any source "
                        f"(default {REVIEWER_FLOOR})")
    p.add_argument("--reconciler", type=int, default=RECONCILER_COST,
                   help=f"tokens for the reconciler stage (default {RECONCILER_COST})")
    p.add_argument("--chars-per-token", type=float, default=CHARS_PER_TOKEN)
    p.add_argument("--budget", type=int,
                   help="exit 3 if the estimate exceeds this many tokens")
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("machine", help="measure the compute budget and find a usable Python")
    p.add_argument("--out")
    p.add_argument("--repo", help="also look for a virtualenv inside this repository")
    p.add_argument("--python", help="an interpreter to prefer over anything discovered — use "
                                    "it when the project runs under `uv run` or an env this "
                                    "search cannot see")
    p.set_defaults(fn=cmd_machine)

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
    p.add_argument("--out", required=True); p.add_argument("--run-id")
    p.add_argument("--protocol-version"); p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("carryover", help="track defects across runs; detect misses")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", help="overrides the repo recorded in the run's manifest")
    p.add_argument("--reconciliation", required=True); p.add_argument("--out", required=True)
    p.add_argument("--run-id"); p.set_defaults(fn=cmd_carryover)

    p = sub.add_parser("store", help="print this repo's review store path")
    p.add_argument("--repo", required=True)
    p.add_argument("--summary", action="store_true",
                   help="print this repository's summary path instead — the name was claimed "
                        "at install and may be suffixed if the plain one was taken")
    p.add_argument("--kit", action="store_true",
                   help="print where the review kit itself lives, as recorded at install")
    p.set_defaults(fn=cmd_store)

    p = sub.add_parser("residue", help="snapshot the working tree, then report what a run left")
    p.add_argument("--repo", required=True); p.add_argument("--run-dir", required=True)
    p.add_argument("--save", action="store_true", help="take the snapshot (before reviewers)")
    p.add_argument("--force", action="store_true", help="with --save, replace an existing one")
    p.set_defaults(fn=cmd_residue)

    p = sub.add_parser("audit", help="check files_read against the harness access log")
    p.add_argument("--findings"); p.add_argument("--repo", required=True)
    p.add_argument("--access-log", required=True)
    p.add_argument("--agent", help="the subagent id whose reads may support this file's "
                                   "claims; required when the log holds more than one")
    p.add_argument("--run-dir", help="the run whose manifest.json dates this review; access "
                                     "records older than its start time are ignored")
    p.add_argument("--since", help="ISO timestamp floor, instead of --run-dir")
    p.add_argument("--list-agents", action="store_true",
                   help="print the agent ids in the log with what each touched, so each can "
                        "be matched to a reviewer. --findings is not needed with this")
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
    p.add_argument("--run-dir", help="the run directory, to check its name against the "
                                     "reconciliation's run_id")
    p.add_argument("--run-id", help="override both")
    p.set_defaults(fn=cmd_ledger)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
