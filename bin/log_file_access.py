#!/usr/bin/env python3
"""PostToolUse hook: append every file a tool touched to .review/access.jsonl.

Wire it into settings.json (see review-kit/README.md). Reviewers cannot see or write this
log, so `review.py audit` can compare it against what a reviewer *claimed* to read.
This is the only check in the kit that a lying agent cannot defeat from inside its
own context: the record is written by the harness, not by the agent.

Never fails the tool call — a broken hook must not break the session.
"""
import json, os, re, sys, datetime
from pathlib import Path

PATHY = re.compile(r"(?:^|[\s'\"=])((?:/|\./|\.\./)?[\w./\-]+\.[A-Za-z0-9]{1,6})")


def looks_like_path(s: str, root: str) -> bool:
    """`json.load` and `np.array` match the path regex; a dotted call is not a file.

    Accept only what carries a separator or actually exists on disk. An audit built on
    false positives would vouch for files nobody opened, which is the failure this log
    exists to catch.
    """
    if "/" in s:
        return True
    try:
        return (Path(root) / s).exists()
    except OSError:
        return False


def store_for(root):
    """The review store for this working directory, or None if the kit is not installed here.

    The slug comes from the kit's own `review.py` rather than a copy, so the hook and every
    other stage can never disagree about where a repository's store is. Importing it costs
    about 2ms against the ~8ms this process already spends starting Python.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "review_kit_review", Path(__file__).resolve().parent / "review.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        store = mod.store_path(Path(root))
    except Exception:
        return None
    return store if store.is_dir() else None


def paths_from(tool, inp):
    out = set()
    # `path` on a Grep or Glob is the directory searched, not a file whose contents came back.
    # Counting it would let one grep vouch for every file under it — the opposite of what
    # this log is for, and contrary to the note below.
    keys = ("file_path", "notebook_path") if tool in ("Grep", "Glob") \
        else ("file_path", "path", "notebook_path")
    for k in keys:
        if isinstance(inp.get(k), str):
            out.add(inp[k])
    if isinstance(inp.get("command"), str):
        # Only paths given to a command that actually returns file contents. Previously any
        # path mentioned anywhere in the command line counted, so a reviewer could "read" a
        # file by naming it in an `ls`, a `rm`, or a command that failed outright.
        cmd = inp["command"]
        # wc, md5sum and sha256sum were here and return no file CONTENT — only a count or a
        # digest. A reviewer that read nothing could pass the scope-honesty check with a
        # single `wc -l $(ls *.py)`, which defeats the one check it cannot otherwise defeat.
        if re.search(r"\b(cat|head|tail|sed|awk|less|more|grep|rg|python3?|node|jq|diff|"
                     r"nl|strings|xxd|od)\b", cmd):
            out |= set(PATHY.findall(cmd))
    # A Grep or Glob pattern is deliberately ignored: searching a tree is not reading any
    # particular file, and counting it as one would let a single grep "support" a claim to
    # have read everything it matched.
    return {p for p in out if p and not p.startswith("-")}


def filtered(paths, root):
    return sorted(p for p in paths if looks_like_path(p, root))


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    tool = ev.get("tool_name", "")
    if tool not in ("Read", "Bash", "NotebookEdit", "Edit", "Write", "Grep", "Glob"):
        return
    inp = ev.get("tool_input") or {}
    root = os.environ.get("REVIEW_ACCESS_ROOT") or os.getcwd()
    logdir = store_for(root)
    if logdir is None:
        # No store for this directory, so this is not a repository anyone installed the kit
        # for. Writing anyway is how the previous version littered a `.review/` into every
        # directory a session ever touched — and worse, that bare directory then read as an
        # in-repo install and put a whole run inside the repository being audited.
        return
    try:
        rec = {"t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "tool": tool, "agent": ev.get("agent_id") or ev.get("session_id"),
               "paths": filtered(paths_from(tool, inp), root)}
        if rec["paths"]:
            with (logdir / "access.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception as e:
        # Still never break the session — but leave a trace. A silently failed hook and an
        # honest reviewer that read nothing produce the same empty log, and `audit` then
        # accuses the reviewer.
        try:
            (logdir / "access.errors").open("a", encoding="utf-8").write(
                f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {type(e).__name__}: {e}\n")
        except Exception:
            pass


if __name__ == "__main__":
    main()
