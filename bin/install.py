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

import argparse, json, os, shutil, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
# Must cover every tool log_file_access.py knows how to read a path out of; NotebookEdit
# was handled there and absent here, so notebook reads reached the log from no tool at all.
MATCHER = "Read|Bash|Grep|Glob|Edit|Write|NotebookEdit"


def install_skills(target: Path, link: bool):
    dest = target / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    done, clashes = [], []
    for skill in sorted((KIT / "skills").iterdir()):
        if not (skill / "SKILL.md").exists():
            continue
        out = dest / skill.name
        if out.is_symlink():
            out.unlink()
        elif out.exists():
            # A real directory here is somebody's own skill that happens to share one of our
            # names — `review-code` is exactly what a person who reviews code would call
            # theirs. Deleting it is irrecoverable and outside the repo under review, so
            # refuse and name the collision instead.
            clashes.append(out)
            continue
        if link:
            # A symlink keeps one copy of the truth: a `git pull` in the kit updates every
            # repo at once, where copies would be a set of forks drifting apart.
            out.symlink_to(skill)
        else:
            shutil.copytree(skill, out)
        done.append(skill.name)
    if clashes:
        print(f"error: {len(clashes)} existing skill directory/ies would be overwritten:",
              file=sys.stderr)
        for c in clashes:
            print(f"    {c}", file=sys.stderr)
        print("These are not the kit's — they are yours, and replacing them would be "
              "irrecoverable.\nRename or move them, then re-run.", file=sys.stderr)
        sys.exit(2)
    return dest, done


def _review_py():
    """Load bin/review.py as a module, so the store path has exactly one definition.

    It was copied here and in two places in review.py. Three copies of a path derivation is
    three chances for an install to point somewhere a review does not look.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_kit_review", KIT / "bin" / "review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def store_path(repo: Path) -> Path:
    return _review_py().store_path(repo)


def install_store(repo: Path):
    """Put the review store outside the repository and symlink it in.

    `grep -r` and `rg` skip symlinked directories, so a reviewer sweeping the repo cannot pick
    up a previous run's conclusions, while a person opening a file under `.review/` by path reads
    it normally. The store is always external: a real directory inside the repo would put an
    earlier run's conclusions where the next run's reviewers can read them.
    """
    link = repo / ".review"
    store = store_path(repo)
    store.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == store:
            return link, store
        link.unlink()
    elif link.exists():
        # A real directory is already there from an earlier install. Move its contents into
        # the store — but a name that already exists in the store is a genuine conflict
        # between two histories, and the previous version resolved it by keeping the store's
        # copy and then rmtree-ing the repo's. That destroyed 40 runs of coverage with no
        # backup and no message. Refuse instead, and name both sides.
        clashes = [item.name for item in sorted(link.iterdir()) if (store / item.name).exists()]
        if clashes:
            print(f"error: {len(clashes)} file(s) exist BOTH in {link} and in {store}:",
                  file=sys.stderr)
            for c in clashes:
                print(f"    {c}", file=sys.stderr)
            print("Each is a review history, and this cannot tell which one you want.\n"
                  "Merge or rename them by hand, then re-run — nothing has been moved or "
                  "deleted.", file=sys.stderr)
            sys.exit(2)
        for item in sorted(link.iterdir()):
            shutil.move(str(item), str(store / item.name))
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

    # Via a temp file and rename: this is the user's settings, and a crash between truncate
    # and write leaves them with the malformed file this function warns about four lines up.
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, settings_path)
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
                         "~/.review-kit/<repo>-<hash>/, and no symlink, hook or rules file is "
                         "written. `review.py audit` is unavailable in this mode.")
    ap.add_argument("--copy", action="store_true",
                    help="copy the skills instead of symlinking them")
    ap.add_argument("--no-hook", action="store_true",
                    help="install skills only; `review.py audit` will then be unavailable")
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
        ignored = [f for f, on in [("--with-rules", a.with_rules),
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

    link, store = install_store(repo)
    print(f"store: {link} -> {store}")
    print("  Outside the repo on purpose: grep -r and rg skip symlinked directories, so a")
    print("  reviewer sweeping the repository cannot pick up an earlier run's conclusions.")

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
