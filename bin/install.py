#!/usr/bin/env python3
"""Install the review kit for a repository.

The kit is external to the repository it reviews. Skills go to `~/.claude/skills/`, the
access-log hook to `~/.claude/settings.json`, and every artefact a review produces to
`~/.review-kit/<repo>-<hash>/`. A plain install writes nothing inside the repository; only
`--with-rules` does, placing REVIEW-RULES.md there and never over an existing file. The
summary is written later, by a run, and is the deliverable.

That is possible because of one rule the kit depends on: **a review runs from a session started
in the repository being reviewed.** The hook then needs no configuration at all — the directory
each tool runs in is inside the repository — and nothing per-repository is baked into any
settings file, so installing for a second repository cannot disturb the first. `review.py
preflight` enforces the rule rather than assuming it.

Idempotent: safe to re-run, for this repository or any other. Stdlib only.
"""
from __future__ import annotations

import argparse, datetime, importlib.util, json, os, shlex, shutil, sys
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
        return json.loads(OWNERSHIP.read_text(encoding="utf-8")).get("skills", {})
    except ValueError:
        return {}


def save_ownership(skills: dict):
    OWNERSHIP.parent.mkdir(parents=True, exist_ok=True)
    tmp = OWNERSHIP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"skills": skills}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OWNERSHIP)


def ours(path: Path, name: str, owned: dict) -> bool:
    """Is the thing at this path the kit's, or somebody else's skill of the same name?

    `review-code`, `review-docs` and `review-tests` are exactly what a person would call a
    skill of their own, and `~/.claude/skills/` is shared by every project on the machine, so
    a collision is a question of time rather than of luck.

    Everything this kit installs is a symlink into the kit, which makes the question
    decidable by resolving it: ours if it points at `<kit>/skills/<name>`, somebody else's
    otherwise. An earlier version could copy instead, and a copy cannot be told from a
    hand-written skill by inspection at all — that version also unlinked whatever symlink it
    found and silently repointed a user's own skill at ours, exit 0, nothing said.

    A real directory is therefore never ours. A symlink the record claims is also ours when it
    still points where the record says this kit linked it, whether or not that path exists:
    that is this kit after a move (the link dangles) or a copy (it points at the old
    checkout), and it is repaired rather than refused. A link the user repointed elsewhere,
    dangling or not, stays theirs. The check used to rely on `resolve()` raising for a
    dangling link, which it never does, so a moved kit was refused by install and kept by
    --uninstall, with no way out but deleting links by hand.
    """
    if not path.is_symlink():
        return False
    try:
        target = path.resolve()
    except (OSError, RuntimeError):     # a symlink loop raises RuntimeError before 3.13
        return False
    if target == (KIT / "skills" / name).resolve():
        return True
    if name not in owned:
        return False
    source = owned[name].get("source") if isinstance(owned[name], dict) else None
    return bool(source) and target == Path(source).resolve()


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


def install_skills():
    todo, clashes, owned = plan_skills()
    if clashes:
        print(f"error: {len(clashes)} skill name(s) in {SKILLS_HOME} are not this kit's:",
              file=sys.stderr)
        for c in clashes:
            print(f"    {c}", file=sys.stderr)
        print("\nThey are yours, and replacing them would be irrecoverable. NOTHING HAS BEEN "
              "WRITTEN.\nRename or move them and re-run. If one is in fact a leftover from an "
              "older install\nof this kit, delete it by hand: --uninstall removes only what "
              "this check accepts as ours.", file=sys.stderr)
        sys.exit(2)

    SKILLS_HOME.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    done, moved_from = [], set()
    for skill in todo:
        out = SKILLS_HOME / skill.name
        if out.is_symlink():
            old = out.resolve()
            if old != skill.resolve() and old.exists():
                # Another checkout of this kit, still on disk. Every repository installed
                # from it now runs these skills against its own bin/ — worth saying.
                moved_from.add(str(old.parent.parent))
            # Ours, from an earlier install: replaced, so an updated kit actually reaches the
            # skills a reviewer reads. Never anything but a symlink — plan_skills refuses the
            # rest before a single write happens.
            out.unlink()
        out.symlink_to(skill)
        owned[skill.name] = {"source": str(skill), "installed": now}
        done.append(skill.name)
    save_ownership(owned)
    return SKILLS_HOME, done, sorted(moved_from)


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
    if removed:
        # Only when something changed: --uninstall on a machine where nothing was installed
        # used to create ~/.review-kit/ just to record that nothing was there.
        save_ownership({k: v for k, v in owned.items() if k not in removed})
    return removed, skipped


def install_rules(repo: Path):
    """Copy the house-rules template in, where the repo then owns it. Never overwrites."""
    dest = repo / "REVIEW-RULES.md"
    if dest.exists():
        return dest, False
    shutil.copyfile(KIT / "REVIEW-RULES.template.md", dest)
    return dest, True


def read_settings() -> dict:
    """The user's settings, or exit 2 before anything is written if they cannot be merged into.

    Checked down to the shape install_hook edits, not just the top level: `"hooks": []` is
    valid JSON, and it used to pass this check and then crash install_hook with a traceback
    after the skills, the store and the config had been written.
    """
    if not SETTINGS.exists():
        return {}
    problem = None
    try:
        cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except OSError as e:
        cfg, problem = None, f"cannot be read ({e.strerror or e})"
    except ValueError as e:
        cfg, problem = None, f"is not valid JSON ({e})"
    if problem is None:
        problem = settings_shape_problem(cfg)
    if problem:
        print(f"error: {SETTINGS} {problem}.", file=sys.stderr)
        print("A malformed settings file silently disables EVERY setting in it. "
              "Fix it before re-running. NOTHING HAS BEEN WRITTEN.", file=sys.stderr)
        sys.exit(2)
    return cfg


def settings_shape_problem(cfg):
    """Why install_hook could not merge into these settings, or None if it can."""
    if not isinstance(cfg, dict):
        return "is not a JSON object"
    hooks = cfg.get("hooks", {})
    if not isinstance(hooks, dict):
        return "has a `hooks` value that is not an object"
    post = hooks.get("PostToolUse", [])
    if not isinstance(post, list):
        return "has a `hooks.PostToolUse` value that is not a list"
    for i, entry in enumerate(post):
        inner = entry.get("hooks", []) if isinstance(entry, dict) else None
        if not isinstance(inner, list) or not all(isinstance(h, dict) for h in inner):
            return f"has a malformed `hooks.PostToolUse` entry (#{i + 1})"
    return None


def install_hook():
    """Wire the access-log hook once, globally, with no per-repository configuration.

    It carries no `REVIEW_ACCESS_ROOT`: the hook derives the store from the directory each
    tool ran in, walking up to the nearest one that has a store, which under this kit's one
    rule is the repository under review. Baking a path
    in here is what let a second install silently stop the first repository being logged —
    one hook line, pointing at whichever repo was installed last.
    """
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    cfg = read_settings()

    # Quoted: the command goes through a shell, and a kit under "~/My Tools/" would otherwise
    # run `python3 ~/My` and fail on every tool call, silently.
    cmd = f"python3 {shlex.quote(str(KIT / 'bin' / 'log_file_access.py'))}"
    post = cfg.setdefault("hooks", {}).setdefault("PostToolUse", [])

    # Deduplicate on the SCRIPT, not on an exact matcher string. MATCHER has already changed
    # once in this kit's life, and matching on it meant an upgrade appended a second entry
    # instead of updating the first — leaving a stale hook pointing at an old path, which
    # then failed on every Read, Bash, Grep, Glob, Edit and Write. The first entry that is
    # wholly ours is updated where it stands, so a re-run leaves the file byte-identical
    # even when other hooks follow it.
    def is_ours(h):
        return "log_file_access.py" in str(h.get("command", ""))
    had_ours = [any(is_ours(h) for h in e.get("hooks", [])) for e in post]
    slot = next((i for i, e in enumerate(post)
                 if e.get("hooks") and all(is_ours(h) for h in e["hooks"])), None)
    # Our hook object, keeping any keys the user added to it (a `timeout`, say).
    mine = dict(post[slot]["hooks"][0]) if slot is not None else {}
    mine.update({"type": "command", "command": cmd})
    kept = []
    for i, entry in enumerate(post):
        if i == slot:
            entry["matcher"] = MATCHER
            entry["hooks"] = [mine]
        elif had_ours[i]:
            # Only entries that held our hook are edited, and dropped only if that leaves
            # them empty: an entry of the user's, empty or not, is never touched.
            entry["hooks"] = [h for h in entry["hooks"] if not is_ours(h)]
            if not entry["hooks"]:
                continue
        kept.append(entry)
    if slot is None:
        kept.append({"matcher": MATCHER, "hooks": [mine]})
    post[:] = kept

    # Via a temp file and rename: these are the user's settings, and a crash between truncate
    # and write leaves them with the malformed file this function warns about above.
    # Through a symlink to its target: a settings.json kept in a dotfiles repository is a
    # link, and renaming over the link replaced it with a plain file. The mode is kept too.
    target = SETTINGS.resolve()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if target.exists():
        shutil.copymode(target, tmp)
    os.replace(tmp, target)
    return cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to review (default: cwd)")
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

    # Every check that can refuse runs before the first write: the settings file, then the
    # skills. A malformed settings file used to be found last, after the skills, the store
    # and the config were already written.
    if not a.no_hook:
        read_settings()
    dest, skills, moved_from = install_skills()

    store = RV.store_path(repo)
    store.mkdir(parents=True, exist_ok=True)
    summary = RV.claim_summary_name(repo)
    RV.write_config(repo, kit=str(KIT), summary=summary)

    print(f"repo:   {repo}")
    print(f"store:  {store}")
    print(f"skills: {len(skills)} symlinked into {dest}")
    for old in moved_from:
        print(f"  NOTE: these links pointed at another checkout of this kit, {old}, which still "
              "exists.\n  They now point here. A repository installed from that checkout keeps "
              "its own bin/\n  but will read these skills; re-run install.py for it from "
              "whichever checkout it should use.")
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
        print("        It takes no configuration: it logs to the store of the repository "
              "each tool ran in,\n        and writes nothing at all outside an installed "
              "repository.")
        print("\n        UNVERIFIED until your first run. A hook that is not firing is "
              "invisible until\n        `audit` reports no access log — if that happens, open "
              "/hooks once or restart the\n        session, because the settings watcher may "
              "not pick up a file it did not see at start.")

    if a.with_rules:
        print(f"\nInside {repo} this install wrote one file: REVIEW-RULES.md.")
    else:
        print(f"\nNothing was written inside {repo}.")
    print(f"The summary will be written to {repo / summary} at the end of a run — a real, "
          "committable file.")
    print("It stays in the repository during a review; reviewers are instructed not to read "
          "it.")
    if summary != RV.DEFAULT_SUMMARY:
        print(f"  Note: {RV.DEFAULT_SUMMARY} already exists and is not ours, so this install "
              f"claimed {summary} instead.")

    print(f"\nNow START A NEW SESSION IN {repo} and type /repo-audit.")
    print("That is the only supported way to run this: the access hook logs to the repository "
          "the session")
    print("is in, and `review.py preflight` refuses a review launched from anywhere else.")


if __name__ == "__main__":
    main()
