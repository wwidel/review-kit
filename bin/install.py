#!/usr/bin/env python3
"""Install the review kit into a repository.

Two things have to be true before a review works, and both are easy to get wrong by hand:
the skills must be where Claude Code looks for them, and the access-log hook must be in the
settings file of the directory the SESSION was started in — not the repository being
reviewed, if those differ.

Idempotent: safe to re-run. Merges into an existing settings.json rather than replacing it.
Stdlib only.
"""
from __future__ import annotations

import argparse, json, os, re, shutil, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
MATCHER = "Read|Bash|Grep|Glob|Edit|Write"


def install_skills(target: Path, link: bool):
    dest = target / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    done = []
    for skill in sorted((KIT / "skills").iterdir()):
        if not (skill / "SKILL.md").exists():
            continue
        out = dest / skill.name
        if out.is_symlink() or out.exists():
            if out.is_symlink():
                out.unlink()
            else:
                shutil.rmtree(out)
        if link:
            # A symlink keeps one copy of the truth: a `git pull` in the kit updates every
            # repo at once, where copies would be a set of forks drifting apart.
            out.symlink_to(skill)
        else:
            shutil.copytree(skill, out)
        done.append(skill.name)
    return dest, done


def store_path(repo: Path) -> Path:
    """The one place the external store path is computed. Every caller uses it."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", repo.name)
    return (Path.home() / ".review-kit" / slug / "review").resolve()


def install_store(repo: Path, in_repo: bool):
    """Put the review store outside the repository and symlink it in.

    `grep -r` and `rg` skip symlinked directories, so a reviewer sweeping the repo cannot pick
    up a previous run's conclusions, while a person opening a file under `.review/` by path reads
    it normally. --in-repo keeps a real directory for teams who want the whole store committed
    and accept that a grep can then reach it.
    """
    link = repo / ".review"
    if in_repo:
        if link.is_symlink():
            # Bring the external store's contents in rather than orphaning a defect history
            # the next run would silently start over from. The reverse direction already
            # migrates; both should.
            external = link.resolve()
            link.unlink()
            link.mkdir(parents=True, exist_ok=True)
            if external.is_dir():
                for item in external.iterdir():
                    target = link / item.name
                    if not target.exists():
                        shutil.move(str(item), str(target))
        link.mkdir(parents=True, exist_ok=True)
        return link, None
    store = store_path(repo)
    store.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == store:
            return link, store
        link.unlink()
    elif link.exists():
        # A real directory is already there from an earlier install; move its contents rather
        # than silently abandoning them.
        for item in link.iterdir():
            target = store / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(link)
    link.symlink_to(store, target_is_directory=True)
    return link, store


def install_rules(repo: Path):
    """Copy the house-rules template into the repo, where the repo then owns it.

    Copied, never referenced: the point is that a team edits or deletes it. A file read out
    of the kit would be the kit's rules wearing the repo's name.
    """
    src = KIT / "REVIEW-RULES.template.md"
    dest = repo / "REVIEW-RULES.md"
    if dest.exists():
        return dest, False
    shutil.copyfile(src, dest)
    return dest, True


def install_hook(settings_path: Path, access_root: Path | None):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text())
        except ValueError as e:
            print(f"error: {settings_path} is not valid JSON ({e}).", file=sys.stderr)
            print("A malformed settings file silently disables EVERY setting in it. "
                  "Fix it before re-running.", file=sys.stderr)
            sys.exit(2)

    cmd = f"python3 {KIT / 'bin' / 'log_file_access.py'}"
    if access_root:
        cmd = f"REVIEW_ACCESS_ROOT={access_root} " + cmd

    hooks = cfg.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    for entry in post:
        if entry.get("matcher") == MATCHER:
            for h in entry.get("hooks", []):
                if "log_file_access.py" in str(h.get("command", "")):
                    h["command"] = cmd
                    break
            else:
                entry.setdefault("hooks", []).append({"type": "command", "command": cmd})
            break
    else:
        post.append({"matcher": MATCHER,
                     "hooks": [{"type": "command", "command": cmd}]})

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n")
    return cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to review (default: cwd)")
    ap.add_argument("--session-root",
                    help="directory the Claude Code session was started in, if different "
                         "from --repo. The hook MUST go in this directory's settings file; "
                         "a settings.json in a subdirectory is never read.")
    ap.add_argument("--minimal", action="store_true",
                    help="touch the repository not at all: skills go to ~/.claude/skills so "
                         "they are available everywhere, the review store lives under "
                         "~/.review-kit/<repo>/, and no symlink, hook or rules file is "
                         "written. `review.py audit` is unavailable in this mode.")
    ap.add_argument("--copy", action="store_true",
                    help="copy the skills instead of symlinking them")
    ap.add_argument("--no-hook", action="store_true",
                    help="install skills only; `review.py audit` will then be unavailable")
    ap.add_argument("--in-repo", action="store_true",
                    help="keep the review store as a real directory inside the repo instead "
                         "of symlinking it to ~/.review-kit/<repo>/. Committable, but a "
                         "reviewer's grep can then reach a previous run's conclusions.")
    ap.add_argument("--with-rules", action="store_true",
                    help="copy the house-rules template into the repo as REVIEW-RULES.md, "
                         "for you to edit or delete. Never overwrites an existing one.")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    session_root = Path(a.session_root).resolve() if a.session_root else repo
    if not repo.is_dir():
        print(f"error: no such directory: {repo}", file=sys.stderr)
        sys.exit(2)

    if a.minimal:
        ignored = [f for f, on in [("--in-repo", a.in_repo), ("--with-rules", a.with_rules),
                                   ("--no-hook", a.no_hook),
                                   ("--session-root", bool(a.session_root))] if on]
        if ignored:
            # Silently ignoring a flag someone deliberately passed is how a person ends up
            # believing a file exists that was never written.
            print(f"error: --minimal writes nothing into the repository, so it cannot be "
                  f"combined with {', '.join(ignored)}.", file=sys.stderr)
            print("Drop --minimal, or drop the other flag.", file=sys.stderr)
            sys.exit(2)
        store = store_path(repo)
        store.mkdir(parents=True, exist_ok=True)
        dest, skills = install_skills(Path.home(), link=not a.copy)
        print(f"skills: {len(skills)} {'symlinked' if not a.copy else 'copied'} into {dest}")
        print(f"store:  {store}")
        print(f"repo:   {repo}  — UNTOUCHED, nothing was written inside it")
        print()
        print("What this gives you:")
        print("  · /repo-audit and the single-purpose skills, in every project on this machine")
        print("  · every artefact a review produces, under the store path above")
        print("  · reviewer independence by construction: no review output sits where a")
        print("    reviewer's `grep -r` over the repository could reach it")
        print()
        print("What it does NOT give you:")
        print("  · `review.py audit` — that needs a PostToolUse hook in the settings file of")
        print("    the directory your session starts in, which this mode does not write. Until")
        print("    it runs, a reviewer's claim about which files it read is taken on trust.")
        print("  · a `.review` symlink in the repo, so nothing points at the store from inside")
        print("    it — note the store path above, or re-run without --minimal to add one")
        print("  · REVIEW-SUMMARY.md in the repo; the summary is written into the store")
        print("    instead, so there is nothing to commit alongside the code")
        print("  · REVIEW-RULES.md — add it later with --with-rules when you want to record")
        print("    which practices this repository has adopted")
        print("  · skills scoped to this project: they are global, so /repo-audit appears in")
        print("    every repository you open, including ones you never intend to audit")
        print()
        print(f"review-kit: {KIT}")
        print("\nStart a review with /repo-audit — a person invokes it; nothing here "
              "schedules anything.")
        return

    link, store = install_store(repo, a.in_repo)
    if store:
        print(f"store: {link} -> {store}")
        print("  Outside the repo on purpose: grep -r and rg skip symlinked directories, so a")
        print("  reviewer sweeping the repository cannot pick up an earlier run's conclusions.")
    else:
        print(f"store: {link} (real directory, --in-repo)")
        print("  A reviewer's grep can reach previous conclusions here. Run `review.py shield`")
        print("  before the reviewer stage if that matters to you.")

    dest, skills = install_skills(session_root, link=not a.copy)
    print(f"skills: {len(skills)} {'symlinked' if not a.copy else 'copied'} into {dest}")
    for s in skills:
        print(f"  {s}")

    if a.with_rules:
        dest, created = install_rules(repo)
        if created:
            print(f"\nrules: template copied to {dest}")
            print("  It is yours — record what you have actually adopted, and delete the rest.")
            print("  Adopting nothing is a supported answer: findings then carry no rule id.")
        else:
            print(f"\nrules: {dest} already exists — left untouched")

    if a.no_hook:
        print("\nhook: skipped (--no-hook). `review.py audit` will refuse to run.")
    else:
        settings = session_root / ".claude" / "settings.json"
        access_root = repo if repo != session_root else None
        cmd = install_hook(settings, access_root)
        print(f"\nhook: PostToolUse on {MATCHER} → {settings}")
        print(f"  {cmd}")
        if access_root:
            print(f"  (REVIEW_ACCESS_ROOT set because the repo under review differs from "
                  f"the session root)")
        print("\nIf a review's `audit` step reports no access log, the settings watcher did "
              "not pick this up:\n  open /hooks once, or restart the session. Adding the "
              "hook mid-session usually works,\n  but it is not guaranteed for a settings "
              "file that did not exist when the session began.")

    print(f"\nThe summary stays a real in-repo file at {repo / 'REVIEW-SUMMARY.md'}, so it can")
    print("be committed. Because a real file IS reachable by grep, the orchestrator shields it")
    print("for the duration of the reviewer stage and restores it afterwards.")
    print(f"\nreview-kit: {KIT}")
    print(f"repo:  {repo}")
    print("\nNow start a review with /repo-audit — a person invokes it; nothing here "
          "schedules anything.")


if __name__ == "__main__":
    main()
