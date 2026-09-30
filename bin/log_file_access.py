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


def paths_from(tool, inp):
    out = set()
    for k in ("file_path", "path", "notebook_path"):
        if isinstance(inp.get(k), str):
            out.add(inp[k])
    if isinstance(inp.get("command"), str):
        out |= set(PATHY.findall(inp["command"]))
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
    logdir = Path(root) / ".review"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        rec = {"t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "tool": tool, "agent": ev.get("agent_id") or ev.get("session_id"),
               "paths": filtered(paths_from(tool, inp), root)}
        if rec["paths"]:
            with (logdir / "access.jsonl").open("a") as f:
                f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
