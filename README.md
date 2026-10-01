# Review kit

A review system for Python repositories built around data analysis and machine learning. Four
independent reviewers — one of them blinded to the documentation — then a reconciler that does
not trust any of them.

**Everything here is invoked by a person.** Nothing schedules itself, nothing hooks into git,
and no agent decides on its own that a review is due. Agents are unreliable at doing things
"periodically" — the trigger misfires, or fires often enough that the output stops being read —
so the decision to spend five agents on a review stays with whoever is paying for it.

## Install

Clone the kit somewhere, then point it at a repository:

```bash
git clone <review-kit> ~/tools/review-kit
python3 ~/tools/review-kit/bin/install.py --repo ~/code/some-project
```

Then **start a new session in that repository** and type `/repo-audit`.

**It writes nothing inside the repository.** Skills go to `~/.claude/skills/`, the access-log
hook to `~/.claude/settings.json`, and every artefact a review produces to
`~/.review-kit/<repo>-<hash>/`. The only file that ever lands in your working tree is the
summary, written at the end of a run, which is the deliverable and meant to be committed.

### The one rule

**A review runs from a session started in the repository being reviewed.** This is the only
supported mode, and the rest of the design follows from it. The access-log hook takes no
configuration because its working directory *is* the repository under review; nothing
per-repository is baked into any settings file, so installing for a second repository cannot
disturb the first. `review.py preflight` checks the rule at the start of every run and refuses
a review launched from anywhere else, because the failure is otherwise silent — reads recorded
against another repository's store, and every honest reviewer reported as having claimed a file
it never opened.

### What it touches

| path | what |
|---|---|
| `~/.claude/skills/` | the seven skills, symlinked (or copied with `--copy`) |
| `~/.claude/settings.json` | one `PostToolUse` hook line, naming no repository |
| `~/.review-kit/<repo>-<hash>/` | that repository's store, config and shield directory |
| `~/.review-kit/installed-skills.json` | which skill names this kit owns |
| the repository | **nothing**, until a run writes the summary |

Installing for another repository is the same command with a different `--repo`. It rewrites
the same global skills and the same hook line — identical both times — and creates a second
store. The first repository is untouched.

Other flags: `--copy` copies the skills instead of symlinking them (the default symlink keeps
one copy of the truth, so a `git pull` in the kit updates every repository at once);
`--no-hook` skips the access hook, which makes `review.py audit` unavailable; `--with-rules`
copies the house-rules template in as `REVIEW-RULES.md`, the one flag that writes into the
repository. `--uninstall` removes the skills this kit installed and nothing else — your stores,
summaries and the hook line stay.

### Two things it cannot promise you

- **That the hook is firing.** A hook that is not firing is invisible until `audit` reports no
  access log. If that happens, open `/hooks` once or restart the session: the settings watcher
  may not pick up a file it did not see at startup. Install says so, and so does `preflight`.
- **That `/repo-audit` only appears where you want it.** The skills are global, so the command
  shows up in every project on the machine, including ones you never intend to audit.

### If the repository already has a `REVIEW-SUMMARY.md`

It is left alone. The install claims the first free name — `REVIEW-SUMMARY-1.md`, and so on —
records the choice, and every later run reuses it. Nothing in your working tree is ever
overwritten by an install.

## The documents

| file | what it is |
|---|---|
| `README.md` | this — install, run, the commands |
| [`DESIGN.md`](DESIGN.md) | how it works, why, what has been measured, known limits |
| [`PRACTICES.md`](PRACTICES.md) | the rule vocabulary findings cite, and an optional default standard |
| [`FINDINGS.md`](FINDINGS.md) | the JSON contract every reviewer emits |
| `REVIEW-RULES.template.md` | copied into your repo by `--with-rules`; yours to edit |

## Install

```bash
python3 <review-kit>/bin/install.py --repo <the repository to review>
```

That does the two things a review needs and both are easy to get wrong by hand: it puts the
skills where Claude Code looks for them, and it writes the access-log hook into the right
settings file. It merges into an existing `settings.json` rather than replacing it, is safe to
re-run, and refuses loudly if that file is not valid JSON — a malformed settings file silently
disables every setting in it.

`PRACTICES.md`, `FINDINGS.md` and `bin/` stay where they are — the skills reference all three by
path and read them at run time, so keep the kit's checkout intact. The install records where the
kit lives, so `review.py store --repo . --kit` answers that question later even if the skills
were copied rather than symlinked.

### If the hook does not seem to fire

A hook added to an existing settings file generally takes effect immediately, and adding one
mid-session has been observed to work. It is not guaranteed for a settings file that did not
exist when the session began: the settings watcher only watches directories that already had
one. If `audit` reports no access log, open `/hooks` once to reload the configuration, or
restart the session. Everything else in the kit works without the hook — only `audit` needs it,
and it refuses to run rather than report a pass it cannot support.

**Where the state lives.** Everything a review produces is at
`~/.review-kit/<repo>-<hash>/review/` — findings, probes, manifests, the coverage record, the
open-defect history, the ledger and the access log. The directory name carries a hash of the
repository's absolute path, not just its basename, so two checkouts both called `analysis` do
not share one coverage record and one defect history.

It lives outside the repository for one reason: a reviewer sweeping the tree with `grep -r`
must not be able to pick up an earlier run's conclusions. Nothing points at it from inside the
working tree — use `review.py store --repo .` to print the path.

`REVIEW-SUMMARY.md` stays a real, committed file at the repo root — it is the deliverable. A
real file *is* found by `grep -r`, so the orchestrator runs `review.py shield` before the
reviewer stage and `unshield` after; the file is held outside the repository in between, and an
interrupted run is recovered on the next shield rather than orphaned.

The only state the kit writes into the repository under review:

```
REVIEW-SUMMARY.md     the deliverable — confirmed defects, fixed vs for you. A real,
                      committable file at the repo root, shielded while reviewers run.
                      The name is claimed at install; a repo that already had one keeps it
                      and the kit takes REVIEW-SUMMARY-1.md instead.
```

Everything else is external, at `~/.review-kit/<repo>-<hash>/`:

```
config.json           the repo, the kit's location, and the claimed summary filename
shield/               where the summary waits while reviewers run
review/
  coverage.json       per-(file, reviewer) record of what has been reviewed, and when
  open_defects.json   every confirmed defect across runs, and what became of it
  ledger.jsonl        one row per reviewer per run: reported, confirmed, refuted, deferred
  last_review.json    where the last run got to — a bookmark, not a timer
  access.jsonl        every file a tool touched, written by the hook, not by any agent
  runs/<run-id>/      manifest, machine, repo_profile, probes, per-reviewer findings,
                      clusters, recurrence, reconciliation.json, and pre/ snapshots of
                      any file that was fixed
```

## Run

You type these. Nothing invokes them for you.

```
/repo-audit              # what changed since the last review, plus a slice of the rest
/repo-audit --full       # every file, regardless of what changed
/repo-audit --since v3   # everything that changed since a given commit, tag or branch
```

The default keeps a review affordable on a repository you audit repeatedly: it reads what has
changed, plus a rotating slice of the untouched code so that settled files are not ignored
forever. Reach for `--full` on a repository you have never audited, or when you want a complete
picture rather than a cheap one — it reads everything, and costs accordingly.

Individually, when a full audit is more than you need:

```
/review-docs docs/RESULTS.md     # one file
/review-code --full              # correctness only
/review-tests                    # does the test suite defend what actually breaks
                                 # (not part of /repo-audit — run it separately)
```

`review-fresh-eyes` and `reconcile-reviews` are not run alone: the first is only meaningful
when something else has read the documentation it was denied, and the second has nothing to
reconcile.

## How it works

```
  scope ─┬─▶ fresh-eyes ─┐                         none sees another
  probe  ├─▶ code ───────┤  findings-*.json        none edits anything
  machine├─▶ analysis ───┤  (one schema,           all four on Opus
         └─▶ docs ───────┘   evidence required)
                    │
                    ▼   validate · fingerprint · cluster · coverage   ← scripted gate
                    │
                    ▼
              reconciler ──▶ reconciliation.json ──▶ REVIEW-SUMMARY.md · ledger · carryover
             (fresh context,                         fixes in the working tree,
              verifies, does not                     never committed
              summarise; reads JSON,
              never reviewer prose)
```

Agents find defects, verify defects and decide fixes. **Scripts do everything else** — scoping,
the mechanical checks, contract validation, fingerprints, clustering, coverage, the summary and
the ledger. Every failure this kit has actually suffered was in the bookkeeping half, never the
judgement half.

Four things carry the design, and **[DESIGN.md](DESIGN.md) explains each with the evidence
behind it**: independence enforced by construction rather than by instruction; one reviewer
blinded to the documentation, so it can find errors *of* the intent rather than only within it;
every finding carrying re-runnable evidence, with severity and confidence kept separate; and a
reconciler that re-runs, re-reads and re-derives instead of summarising.

DESIGN.md also carries what has been measured: ten planted false findings, all ten refuted; a
repeat run putting an upper bound on the miss rate; and the cost, which tracks the protocol
rather than the repository at roughly 880k–970k tokens per audit.

## Practices — adopting them, or not

`PRACTICES.md` is two things, and only one of them is load-bearing for the tool.

It is the **stable ID vocabulary that findings cite** — `N-13` means the same thing across runs,
which is what makes reports comparable and the ledger meaningful. You get that by using the kit,
and it costs nothing, because it is a naming scheme rather than an instruction.

It is also **a default standard, offered rather than imposed.** Three things make that real
rather than a claim:

- **The reviewers work against a repository that has adopted none of it.** A finding may carry
  `rule_id: null`; the contract says so, and says such findings are often the most interesting.
  They then stand on their evidence, which they were always required to do anyway. Reporting
  non-adoption as a defect is explicitly forbidden in every reviewer skill.
- **Your repository wins.** `CLAUDE.md` takes precedence wherever it disagrees, and a project
  adds its own rules in the `P-` namespace.
- **`install.py --with-rules`** drops `REVIEW-RULES.md` into your repo: what you have adopted,
  what you have *deliberately declined* and why, and your own `P-` rules. It is copied, never
  referenced — the kit does not read its own rules back out of your repository. Reviewers read
  it if present, and a rule recorded there as declined is dropped by the reconciler rather than
  raised again.

Roughly forty of the sixty-nine rules describe **defects** — leakage, an unseeded split, a number
quoted from memory, deserialising an artefact you did not produce. Breaking one means something
is wrong. The rest describe **house style** — one dataframe library, logic in the package rather
than in scripts, who decides what and when an agent may act. Breaking one means you work
differently. Section `A` is the most opinionated in the file; some teams find it the most
valuable part and others will want none of it, and both are reasonable.

Rules carry stable IDs (`N-13`, `C-4`) and those IDs are permanent — retire a rule by marking
it withdrawn, never by renumbering. Most carry the incident that produced them; resist tidying
those out, because "never `git stash -u`" gets ignored under deadline pressure and "never
`git stash -u` — it destroyed untracked artefacts that cost hours" does not.

Four rules do most of the work in this repo class:

- **`N-1`** every number in a document cites the script that reproduces it
- **`N-13`** never quote a number from memory; re-read it at the moment you write it
- **`C-4`** one code path per concept, and a test that asserts it stays one
- **`M-4`** a computation beyond the machine is flagged, not attempted, and not quietly shrunk


---

## What is in the kit

```
review-kit/
  PRACTICES.md                      portable rules, with stable IDs reviewers cite
  FINDINGS.md                       the JSON contract every reviewer emits
  CLAUDE.md.snippet                 working rules to paste into a project CLAUDE.md
  REVIEW-RULES.template.md          copied into a repo by install.py --with-rules; the
                                    repo then owns it — what you adopted, what you declined
  bin/
    review.py                       all the bookkeeping: scope, probe, validate, cluster,
                                    coverage, carryover, summarize, ledger
    log_file_access.py              PostToolUse hook backing `review.py audit`
    install.py                      puts the skills and the hook where they must be
  skills/
    repo-audit/SKILL.md     orchestrator — the entry point
    review-fresh-eyes/SKILL.md      blinded leg — code and data, no documentation
    review-code/SKILL.md            correctness leg
    review-analysis/SKILL.md        analytical-validity leg
    review-docs/SKILL.md            document-integrity leg
    review-tests/SKILL.md           tool — invoked by review-code, or run alone
    reconcile-reviews/SKILL.md      the verifier
```

## The tooling

Every command that needs to know which repository it is working on reads that from the
manifest or run directory it was given, so none of them depends on the shell's current
directory. `scope` and `characterize` require `--repo` because at that point nothing has
recorded it yet, and `shield`, `unshield` and `audit` require it because no manifest is
in play for them to read it from. This matters when the kit and the repository under review
are different checkouts: you can drive a review of any repo from anywhere, and pointing a
command at the wrong tree takes a deliberate `--repo` override.

`bin/review.py` does every part of a review that does not require judgement. Agents find
defects, verify defects and decide fixes; the script does the rest. This split is not
tidiness — every failure the kit has actually suffered in practice was in the bookkeeping
half, not the judgement half.

| command | does |
|---|---|
| `scope` | the review window + a three-way manifest: a *review* tier read in full (source, Markdown, and every other file under the threshold); a *tabular* reference tier — data files at any size, plus any other file over ~500 lines that is neither source nor Markdown — described by size, sha256, columns and first row, queried by script and never read; and *opaque* files (decks, PDFs, spreadsheets, images, archives, model artefacts) listed so a reviewer knows they exist but can neither read nor query them. Source and Markdown are never demoted, however long: a long script or README is where a defect hides, so `scope` names the oversized ones instead. Files it cannot place go under `unclassified`, paths it cannot open — a dangling data symlink, most often — under `unreadable`, directories pruned by name under `skipped_dirs`, and files removed in a `--since` window under `deleted`; all four are listed rather than dropped, so the manifest never claims a scope it did not achieve. Exits non-zero on an empty scope |
| `probe` | runs ~23 mechanical checks once for every reviewer — seeds, unsafe deserialisation, broad excepts, dynamic dispatch, non-atomic writes, path literals, magic numbers, plus AST checks for unparseable files, names redefined in one scope, and functions with identical bodies |
| `machine` | cores, usable memory, disk, GPU, and which frameworks are importable |
| `characterize` | signals about the repo's posture — tests, linter, CI, commit recency. A hint for calibrating severity, never a finding |
| `validate` | enforces the findings contract: missing fields, a heavy command that was run, `certain` resting on reasoning, an unread scope not declared |
| `audit` | compares `files_read` against the harness-written access log — the one check an agent cannot defeat from inside its own context. `--agent` scopes it to one reviewer's reads; `--list-agents` prints the ids in the log so each can be matched to a reviewer; `--run-dir` dates the run so a read from an earlier one cannot vouch for this one |
| `fingerprint` | stamps a stable id from the file path and a normalised source anchor, so a defect keeps its identity across runs even when lines move |
| `cluster` | groups findings that share a fingerprint (corroboration) and lists near-misses as candidates for the reconciler to judge |
| `match` | which defects also appeared in the previous run and are still open |
| `estimate` | counts the files and lines a full review would read and prices it in tokens, before anything is spawned. `--budget N` exits non-zero when the estimate exceeds N |
| `store` | prints this repo's review store; `--summary` the summary's claimed filename at the repo root; `--kit` where the kit itself lives. All three come from the install record — derive them, never hard-code them |\n| `preflight` | checks the conditions a review depends on — that the session is inside the repository under review, that the kit is installed for it, whether the hook is wired — and refuses otherwise |
| `shield` / `unshield` | move `REVIEW-SUMMARY.md` outside the repo for the reviewer stage and back afterwards; refuses a hold inside the repo, and recovers an interrupted shield |
| `coverage` | per-(file, reviewer) record of what has been reviewed, at which sha256, and how stale each file is; names files never reviewed |
| `carryover` | tracks confirmed defects across runs and classifies each as re-reported, not-re-checked, plausibly-fixed, or **missed** |
| `summarize` | renders `REVIEW-SUMMARY.md` from `reconciliation.json`, so no count in it can disagree with the items beneath it |
| `ledger` | appends per-reviewer confirmed/refuted/deferred rows, with rates computed over verifiable findings |

### Wiring up the access log

`audit` is the only defence against a reviewer overstating what it read. It needs a
`PostToolUse` hook, because the log has to be written by the harness rather than by the agent
being checked. In `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Read|Bash|Grep|Glob|Edit|Write",
        "hooks": [{ "type": "command",
                    "command": "python3 <review-kit>/bin/log_file_access.py" }] }
    ]
  }
}
```

The hook takes no configuration: it logs to the store of whichever repository the session is
in, and writes nothing at all for a directory that has no store. Without the hook, `audit`
refuses to run rather than reporting a pass it cannot support — and it ignores records
older than the run it is checking, so a read from an earlier run cannot vouch for this one.

Be clear-eyed about what this does and does not prove. It shows that a tool call touching a
file was issued — not that the content was attended to. It is a floor, not a guarantee, and it
is worth having because the failure it catches (a confident report over files nobody opened) is
the most dangerous artefact this process can produce.
