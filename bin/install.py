#!/usr/bin/env python3
"""Install the review kit for a repository.

The kit is external to the repository it reviews, and `install.py` writes **nothing** inside
it. Skills go to `~/.claude/skills/`, the access-log hook to `~/.claude/settings.json`, and
every artefact a review produces to `~/.review-kit/<repo>-<hash>/`. The only file that ever
lands in the repository is the summary, written at the end of a run, which is the deliverable.

That is possible because of one rule the kit depends on: **a review runs from a session
started in the repository being reviewed.** The hook then needs no configuration at all — its
working directory is the repository — and nothing per-repository is baked into any settings
file, so installing for a second repository cannot disturb the first. `review.py preflight`
enforces the rule rather than assuming it.

Idempotent: safe to re-run, for this repository or any other. Stdlib only.
"""
from __future__ import annotations

import argparse, datetime, importlib.util, json, os, shutil, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
# Must cover every tool log_file_access.py knows how to read a path out of; NotebookEdit
# was handled there and absent here, so notebook reads reached the log from no tool at all.
MATCHER = "Read|Bash|Grep|Glob|Edit|Write|NotebookEdit"
SKILLS_HOME = Path.home() / ".claude" / "skills"
SETTINGS = Path.home() / ".claude" / "settings.json"
# Which skill directories this kit owns, and where each came from. Without it a copied skill
# is indistinguishable from one the user wrote, and a reinstall would have to choose between
# clobbering their work and never updating its own.
OWNERSHIP = Path.home() / ".review-kit" / "installed-skills.json"


def review_py():
    """The kit's own module, so the store path has exactly one definition in the codebase."""
    spec = importlib.util.spec_from_file_location("review_kit_review", KIT / "bin" / "review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RV = review_py()


def load_ownership() -> dict:
    if not OWNERSHIP.exists():
        return {}
    try:
        return json.loads(OWNERSHIP.read_text()).get("skills", {})
    except ValueError:
        return {}


def save_ownership(skills: dict):
    OWNERSHIP.parent.mkdir(parents=True, exist_ok=True)
    tmp = OWNERSHIP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"skills": skills}, indent=2) + "\n")
    tmp.replace(OWNERSHIP)


def ours(path: Path, name: str, owned: dict) -> bool:
    """Is the thing at this path the kit's, or somebody else's skill of the same name?

    `review-code`, `review-docs` and `review-tests` are exactly what a person would call a
    skill of their own, and `~/.claude/skills/` is shared by every project on the machine, so
    a collision is a question of time rather than of luck.

    A symlink is ours only if it actually points into this kit. The previous version unlinked
    whatever it found and silently repointed a user's own skill at ours — exit 0, nothing
    said. A real directory can never be told apart by inspection, which is what the ownership
    record is for. With no record everything reads as somebody else's and the install refuses,
    which is the safe direction.
    """
    if path.is_symlink():
        try:
            return path.resolve() == (KIT / "skills" / name).resolve()
        except OSError:
            return name in owned   # dangling: ours if the record claims it, so it can be repaired
    return name in owned


def plan_skills():
    """Decide everything before writing anything, so a refusal leaves nothing behind.

    The check used to run per skill inside the install loop: six of seven skills were written
    and the run then exited 2 saying they "would be overwritten".
    """
    owned = load_ownership()
    todo, clashes = [], []
    for skill in sorted((KIT / "skills").iterdir()):
        if not (skill / "SKILL.md").exists():
            continue
        out = SKILLS_HOME / skill.name
        if (out.exists() or out.is_symlink()) and not ours(out, skill.name, owned):
            clashes.append(out)
        else:
            todo.append(skill)
    return todo, clashes, owned


def install_skills(link: bool):
    todo, clashes, owned = plan_skills()
    if clashes:
        print(f"error: {len(clashes)} skill name(s) in {SKILLS_HOME} are not this kit's:",
              file=sys.stderr)
        for c in clashes:
            print(f"    {c}", file=sys.stderr)
        print("\nThey are yours, and replacing them would be irrecoverable. NOTHING HAS BEEN "
              "WRITTEN.\nRename or move them and re-run — or run --uninstall first if they "
              "are in fact leftovers\nfrom an older install of this kit.", file=sys.stderr)
        sys.exit(2)

    SKILLS_HOME.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    done = []
    for skill in todo:
        out = SKILLS_HOME / skill.name
        if out.is_symlink():
            out.unlink()
        elif out.exists():
            # Ours, from an earlier install: replaced rather than merged, so an updated kit
            # actually reaches the skills a reviewer reads.
            shutil.rmtree(out)
        if link:
            out.symlink_to(skill)
        else:
            shutil.copytree(skill, out)
        owned[skill.name] = {"source": str(skill), "mode": "link" if link else "copy",
                             "installed": now}
        done.append(skill.name)
    save_ownership(owned)
    return SKILLS_HOME, done


def uninstall_skills():
    """Remove only what the record says this kit owns, and nothing else."""
    owned = load_ownership()
    removed, skipped = [], []
    for name in sorted(owned):
        out = SKILLS_HOME / name
        if not (out.exists() or out.is_symlink()):
            removed.append(name)
            continue
        if not ours(out, name, owned):
            skipped.append(name)
            continue
        if out.is_symlink():
            out.unlink()
        else:
            shutil.rmtree(out)
        removed.append(name)
    save_ownership({k: v for k, v in owned.items() if k not in removed})
    return removed, skipped


def install_rules(repo: Path):
    """Copy the house-rules template in, where the repo then owns it. Never overwrites."""
    dest = repo / "REVIEW-RULES.md"
    if dest.exists():
        return dest, False
    shutil.copyfile(KIT / "REVIEW-RULES.template.md", dest)
    return dest, True


def install_hook():
    """Wire the access-log hook once, globally, with no per-repository configuration.

    It carries no `REVIEW_ACCESS_ROOT`: the hook derives the store from its own working
    directory, which under this kit's one rule is the repository under review. Baking a path
    in here is what let a second install silently stop the first repository being logged —
    one hook line, pointing at whichever repo was installed last.
    """
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if SETTINGS.exists():
        try:
            cfg = json.loads(SETTINGS.read_text())
        except ValueError as e:
            print(f"error: {SETTINGS} is not valid JSON ({e}).", file=sys.stderr)
            print("A malformed settings file silently disables EVERY setting in it. "
                  "Fix it before re-running.", file=sys.stderr)
            sys.exit(2)

    cmd = f"python3 {KIT / 'bin' / 'log_file_access.py'}"
    post = cfg.setdefault("hooks", {}).setdefault("PostToolUse", [])
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
        post.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": cmd}]})

    # Via a temp file and rename: these are the user's settings, and a crash between truncate
    # and write leaves them with the malformed file this function warns about above.
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, SETTINGS)
    return cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to review (default: cwd)")
    ap.add_argument("--copy", action="store_true",
                    help="copy the skills instead of symlinking them. A symlink keeps one "
                         "copy of the truth, so a git pull in the kit updates every repo")
    ap.add_argument("--no-hook", action="store_true",
                    help="skip the access-log hook; `review.py audit` is then unavailable")
    ap.add_argument("--with-rules", action="store_true",
                    help="copy the house-rules template into the repo as REVIEW-RULES.md, "
                         "for you to edit or delete. Never overwrites an existing one. This "
                         "is the one flag that writes into the repository")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the skills this kit installed, and nothing else. Stores, "
                         "summaries and the hook line are left alone")
    a = ap.parse_args()

    if a.uninstall:
        removed, skipped = uninstall_skills()
        print(f"uninstalled {len(removed)} skill(s) from {SKILLS_HOME}"
              + (": " + ", ".join(removed) if removed else ""))
        for name in skipped:
            print(f"  kept {name}: it is no longer this kit's", file=sys.stderr)
        print(f"\nThe hook line in {SETTINGS} was left in place; remove it by hand if you "
              "want it gone.")
        print("Stores under ~/.review-kit/ are untouched — they hold your review history.")
        return

    repo = Path(a.repo).resolve()
    if not repo.is_dir():
        print(f"error: no such directory: {repo}", file=sys.stderr)
        sys.exit(2)

    # Skills first: it is the step that can refuse, and refusing after the store and config
    # were written would leave a half-installed repository behind.
    dest, skills = install_skills(link=not a.copy)

    store = RV.store_path(repo)
    store.mkdir(parents=True, exist_ok=True)
    summary = RV.claim_summary_name(repo)
    RV.write_config(repo, kit=str(KIT), summary=summary)

    print(f"repo:   {repo}")
    print(f"store:  {store}")
    print(f"skills: {len(skills)} {'symlinked' if not a.copy else 'copied'} into {dest}")
    print(f"config: {RV.config_path(repo)}")

    if a.with_rules:
        dest_r, created = install_rules(repo)
        print(f"\nrules:  {dest_r}" + ("" if created else " — already existed, left untouched"))
        if created:
            print("  It is yours — record what you have actually adopted, and delete the rest.")
            print("  Adopting nothing is a supported answer: findings then carry no rule id.")

    if a.no_hook:
        print("\nhook:   skipped (--no-hook). `review.py audit` will refuse to run, so every "
              "reviewer's\n        account of which files it read is taken on trust.")
    else:
        cmd = install_hook()
        print(f"\nhook:   PostToolUse on {MATCHER} → {SETTINGS}")
        print(f"        {cmd}")
        print("        It takes no configuration: it logs to the store of whichever "
              "repository the session\n        is in, and writes nothing at all for a "
              "directory that has no store.")
        print("\n        UNVERIFIED until your first run. A hook that is not firing is "
              "invisible until\n        `audit` reports no access log — if that happens, open "
              "/hooks once or restart the\n        session, because the settings watcher may "
              "not pick up a file it did not see at start.")

    print(f"\nNothing was written inside {repo}."
          + (" (--with-rules added REVIEW-RULES.md.)" if a.with_rules else ""))
    print(f"The summary will be written to {repo / summary} at the end of a run — a real, "
          "committable file.")
    if summary != RV.DEFAULT_SUMMARY:
        print(f"  Note: {RV.DEFAULT_SUMMARY} already exists and is not ours, so this install "
              f"claimed {summary} instead.")

    print(f"\nNow START A NEW SESSION IN {repo} and type /repo-audit.")
    print("That is the only supported way to run this: the access hook follows the session's")
    print("working directory, and `review.py preflight` refuses a review launched from "
          "anywhere else.")


if __name__ == "__main__":
    main()
