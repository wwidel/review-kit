# Review kit

## WARNING

**A full audit is expensive, and its cost can only be guessed very roughly.** The best guess
so far:

```
tokens ≈ 700k + 140 × reviewable lines
```

**This is a very rough approximation, not a price.** It is a line drawn through the smallest
and largest of five completed audits, not a statistical fit, and one of the five landed a third
away from it. The per-line term rests almost entirely on the one large audit. What a repository
costs depends on more than its size, and a repository unlike these five may land further off,
in either direction. Budget for more than it says.

Small repositories cost close to the fixed part, because the protocol itself is expensive;
large ones are dominated by the source being read, re-read and verified. A single audit can use
up a weekly usage limit, and one already has.

| repository | reviewable lines | actual total | formula |
|---|---:|---:|---:|
| Solution B | 1,397 | ~0.90M | 0.90M |
| Solution C | 2,921 | ~1.04M | 1.11M |
| Solution D | 3,719 | ~1.13M | 1.22M |
| Solution E | 4,760 | ~1.04M | 1.37M |
| a private repository | 20,259 | ~3.5M | 3.54M |

Before it spawns any reviewer, `/repo-audit` shows `review.py estimate`'s reviewable line count
and token figure, and stops for your go-ahead. That token figure can run low on large
repositories: for the 20,259-line one it came to about 1.8M, against ~3.5M actually spent. Apply
the formula to the line count too, and go by the larger of the two. To spend less, use
`--since <ref>` or run one single-purpose skill (see [Run](#run)).

**Used on Linux and WSL only.** The test suite passes on Ubuntu and macOS (CI, Python 3.10 and
3.14), but no real audit has been run on a Mac. `review.py machine` reads memory from
`/proc/meminfo`, so on a Mac it reports no memory figure and the compute-budget check has less
to go on. Native Windows does not work: `machine` calls `os.statvfs`, which Windows lacks.

## What it is

A review system for Python repositories built around data analysis and machine learning. Four
independent reviewers — one of them blinded to the documentation — then a reconciler that does
not trust any of them. For what it finds in practice, see
[KAGGLE-RESULTS.md](KAGGLE-RESULTS.md): the kit is being re-run on five gold-medal Kaggle
solutions, and their results will be added there.

**Everything here is invoked by a person.** Nothing schedules itself, nothing hooks into git,
and no agent decides on its own that a review is due. Agents are unreliable at doing things
"periodically" — the trigger misfires, or fires often enough that the output stops being read —
so the decision to spend five agents on a review stays with whoever is paying for it.

## Install

You need Python 3.10 or newer, and nothing else: the scripts use only the standard library.
Clone the kit somewhere, then point it at a repository:

```bash
git clone https://github.com/wwidel/review-kit.git ~/tools/review-kit
python3 ~/tools/review-kit/bin/install.py --repo ~/code/some-project
```

Then **start a new session in that repository** and type `/repo-audit`.

**Two files ever land in your working tree, and you choose both.** Skills go to
`~/.claude/skills/`, the access-log hook to `~/.claude/settings.json`, and every artefact a
review produces to `~/.review-kit/<repo>-<hash>/`. Inside the repository you get:

- `REVIEW-SUMMARY.md`, written at the end of a run — the deliverable, meant to be committed.
  Nothing writes it until you have run a review.
- `REVIEW-RULES.md`, only if you pass `--with-rules`, and never over an existing file.

A plain `install.py --repo <path>` writes nothing into the repository at all.

### The one rule

**A review runs from a session started in the repository being reviewed.** This is the only
supported mode, and the rest of the design follows from it. The access-log hook takes no
configuration because the directory each tool runs in *is* the repository under review, or a
subdirectory of it — the hook walks up to the nearest directory with a store; nothing
per-repository is baked into any settings file, so installing for a second repository cannot
disturb the first. `review.py preflight` checks the rule at the start of every run and refuses
a review launched from anywhere else, because the failure is otherwise silent — reads recorded
against another repository's store, and every honest reviewer reported as having claimed a file
it never opened.

### What it touches

| path | what |
|---|---|
| `~/.claude/skills/` | the seven skills, symlinked into the kit |
| `~/.claude/settings.json` | one `PostToolUse` hook line, naming no repository |
| `~/.review-kit/<repo>-<hash>/` | that repository's store and config |
| `~/.review-kit/installed-skills.json` | which skill names this kit owns |
| the repository | **nothing** — unless you pass `--with-rules`, which adds `REVIEW-RULES.md`. A run later writes the summary |

Installing for another repository is the same command with a different `--repo`. It rewrites
the same global skills and the same hook line — identical both times — and creates a second
store. The first repository is untouched.

Other flags: `--no-hook` skips the access hook, which makes `review.py audit` unavailable;
`--with-rules` copies the house-rules template in as `REVIEW-RULES.md`, the one flag that
writes into the repository, and it never overwrites an existing file. `--uninstall` removes the skills this kit installed and nothing else — your stores,
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
| [`KAGGLE-RESULTS.md`](KAGGLE-RESULTS.md) | what it finds in five gold-medal Kaggle solutions (re-audit pending) |
| [`PRACTICES.md`](PRACTICES.md) | the rule vocabulary findings cite, and an optional default standard |
| [`FINDINGS.md`](FINDINGS.md) | the JSON contract every reviewer emits |
| `REVIEW-RULES.template.md` | copied into your repo by `--with-rules`; yours to edit |

## Keeping the kit's checkout

`PRACTICES.md`, `FINDINGS.md` and `bin/` stay where they are — the skills reference all three by
path and read them at run time, so the kit's checkout must stay intact. The install records
where it lives, so `review.py store --repo . --kit` answers that question later. If you move or
copy the checkout, re-run `install.py --repo <path>` from the new location for each repository:
it repoints its own skill links, says so if they pointed at another checkout that still exists,
and leaves anyone else's links alone.

`install.py` merges into an existing `settings.json` rather than replacing it, is safe to
re-run, and refuses loudly if that file is not valid JSON — a malformed settings file silently
disables every setting in it.

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

`REVIEW-SUMMARY.md` stays a real, committed file at the repo root — it is the deliverable, and
it stays there during a review. A real file *is* found by `grep -r`, so every reviewer is told
by name not to read it. That instruction is the protection: moving the file aside, which an
earlier version did, only hid the working-tree copy, because `git show HEAD:REVIEW-SUMMARY.md`
and `git log -p` serve it regardless — and it cost a marker file, a crash-recovery path and a
way to strand the deliverable outside the repository. The reconciler, which comes after the
reviewers, may read it.

The only state the kit writes into the repository under review:

```
REVIEW-SUMMARY.md     the deliverable — confirmed defects, fixed vs for you. A real,
                      committable file at the repo root. Reviewers are told not to read it.
                      The name is claimed at install; a repo that already had one keeps it
                      and the kit takes REVIEW-SUMMARY-1.md instead.
```

Everything else is external, at `~/.review-kit/<repo>-<hash>/`:

```
config.json           the repo, the kit's location, and the claimed summary filename
review/
  coverage.json       per-(file, reviewer) record of what has been reviewed, and when
  open_defects.json   every confirmed defect across runs, and what became of it
  ledger.jsonl        one row per reviewer per run: reported, confirmed, refuted, deferred
  last_review.json    where the last run got to — a bookmark, not a timer
  access.jsonl        every file a tool touched, and every reading command, written by the
                      hook, not by any agent
  runs/<run-id>/      manifest, machine, repo_profile, probes, per-reviewer findings,
                      clusters, recurrence, reconciliation.json and tree-before.json
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
/review-analysis REPORT.md       # are the numbers in it true
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
             (fresh context,                         reports only, edits nothing,
              verifies, does not                     never committed
              summarise; reads JSON,
              never reviewer prose)
```

Agents find defects, verify defects and decide fixes. **Scripts do everything else** — scoping,
the mechanical checks, contract validation, fingerprints, clustering, coverage, the summary and
the ledger. Every failure this kit has actually suffered was in the bookkeeping half, never the
judgement half.

Five things carry the design, and **[DESIGN.md](DESIGN.md) explains each with the evidence
behind it**: independence enforced by construction rather than by instruction; one reviewer
blinded to the documentation, so it can find errors *of* the intent rather than only within it;
every finding carrying re-runnable evidence, with severity and confidence kept separate; a
compute budget measured before anything runs, so heavy work is flagged rather than attempted;
and a reconciler that re-runs, re-reads and re-derives instead of summarising.

DESIGN.md also carries what has been measured: ten planted false findings, all ten refuted; a
repeat run putting an upper bound on the miss rate; and the cost, roughly 700k tokens plus 140
per reviewable line (see [WARNING](#warning)).

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
  what you have *deliberately declined* and why, and your own `P-` rules.

### Where rules come from, and which wins

Three sources, in this precedence order. Every reviewer and the reconciler is told to read them
in exactly this sequence:

| | file | lives in | who owns it |
|---|---|---|---|
| 1 | `CLAUDE.md` | your repository | you — the kit never writes or modifies it |
| 2 | `REVIEW-RULES.md` | your repository | you, after `--with-rules` copies the template in |
| 3 | `PRACTICES.md` | the kit | the kit — one copy, shared by every repository it reviews |

`CLAUDE.md` is the last word wherever anything disagrees; it holds operational specifics —
machine names, data locations, submission limits. `REVIEW-RULES.md` holds *standards*: which of
the kit's rules this repository has adopted. `PRACTICES.md` is the default underneath both, and
the stable ID vocabulary all three cite.

Details worth knowing about `REVIEW-RULES.md`:

- **It is optional and the kit works without it.** No `--with-rules`, no file, no problem —
  findings then carry `rule_id: null`.
- **It is copied, never referenced.** The kit does not read its own template back out of your
  repository, and nothing in `bin/` reads `REVIEW-RULES.md` at all — only the agents do. There
  is no validation of it and no schema.
- **`--with-rules` never overwrites.** A repository that already has a `REVIEW-RULES.md` keeps
  it untouched, and the install says so.
- **It is yours to commit.** That is why it goes in the repository rather than into the external
  store: adoption is a team decision, it belongs in version control beside the code it governs,
  and it should travel to anyone who clones the repo. A copy under `~/` on one machine would be
  none of those things.
- **A rule recorded there as declined is not a finding.** Reviewers are told not to raise it,
  and the reconciler drops one that slips through rather than re-litigating it.
- **It is reviewed like any other document.** It sits in the review tier, so `review-docs` reads
  it as prose as well as rules — a rules file that has gone stale is a real defect, and this is
  how it gets caught. (`REVIEW-SUMMARY.md` is the one file `scope` leaves out, because it is the
  kit's own output rather than yours.)
- **The template ships blank.** Its adoption table has an empty `adopted?` column and the
  instruction is to delete the rows that are not true of your repository. Until you edit it, a
  reviewer cannot tell "we adopted nothing" from "nobody has filled this in" — so fill it in, or
  delete it, rather than leaving it as shipped.

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
  README.md                         this — install, run, the commands
  DESIGN.md                         how it works, why, what has been measured, known limits
  KAGGLE-RESULTS.md                 what it finds in five gold-medal Kaggle solutions
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
    repo-audit/SKILL.md             orchestrator — the entry point
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
directory. Five require `--repo` — `preflight`, `store`, `scope`, `characterize` and `audit` — because
at those points there is no manifest or run directory for them to read it from. This matters when the kit and the repository under review
are different checkouts: you can drive a review of any repo from anywhere, and pointing a
command at the wrong tree takes a deliberate `--repo` override.

`bin/review.py` does every part of a review that does not require judgement. Agents find
defects, verify defects and decide fixes; the script does the rest. This split is not
tidiness — every failure the kit has actually suffered in practice was in the bookkeeping
half, not the judgement half.

| command | does |
|---|---|
| `scope` | the review window + a three-way manifest: a *review* tier read in full (source, Markdown, and every other file under the threshold); a *tabular* reference tier — data files at any size, plus any other file over ~500 lines that is neither source nor Markdown — described by size, sha256, columns and first row, queried by script and never read; and *opaque* files (decks, PDFs, spreadsheets, images, archives, model artefacts) listed so a reviewer knows they exist but can neither read nor query them. Source and Markdown are never demoted, however long: a long script or README is where a defect hides, so `scope` names the oversized ones instead. A file with an unknown suffix that git tracks is sniffed rather than dropped — binary content makes it opaque, text is placed by length like any other file; an untracked one (an ignored `mlruns/`, a DVC cache) goes under `unclassified`. Paths it cannot open — a dangling data symlink, most often — under `unreadable`, directories pruned by name under `skipped_dirs`, and files removed in a `--since` window under `deleted`; all four are listed rather than dropped, so the manifest never claims a scope it did not achieve. Exits non-zero on an empty scope |
| `probe` | runs ~23 mechanical checks once for every reviewer — seeds, unsafe deserialisation, broad excepts, dynamic dispatch, non-atomic writes, path literals, magic numbers, plus AST checks for unparseable files, names redefined in one scope, and functions with identical bodies |
| `machine` | cores, usable memory, disk, GPU, and which frameworks are importable |
| `characterize` | signals about the repo's posture — tests, linter, CI, commit recency. A hint for calibrating severity, never a finding |
| `validate` | enforces the findings contract: missing fields, a heavy command that was run, `certain` resting on reasoning, an unread scope not declared |
| `audit` | compares `files_read` against the harness-written access log — the one check an agent cannot defeat from inside its own context. `--agent` scopes it to one reviewer's reads; `--list-agents` prints the ids in the log so each can be matched to a reviewer; `--run-dir` dates the run so a read from an earlier one cannot vouch for this one. Exits 1 on an UNSUPPORTED CLAIM — a file named in no reading command — and 3 on UNRESOLVED, a file named in a reading command the hook could not follow, printed so a person can settle it |
| `residue` | `--save` snapshots `git status`, ignored files included, before the reviewers run; afterwards it names what the run created or changed as git sees it, and exits 1 if there is any. It cannot see inside an untracked or ignored directory that already existed, or an edit to an existing ignored file |
| `fingerprint` | stamps a stable id from the file path and a normalised source anchor, so a defect keeps its identity across runs even when lines move |
| `cluster` | groups findings that share a fingerprint (corroboration) and lists near-misses as candidates for the reconciler to judge |
| `match` | which defects also appeared in the previous run and are still open |
| `estimate` | counts the files and lines a full review would read and prices it in tokens, before anything is spawned. `--budget N` exits non-zero when the estimate exceeds N |
| `store` | prints this repo's review store; `--summary` the summary's claimed filename at the repo root; `--kit` where the kit itself lives. All three come from the install record — derive them, never hard-code them |
| `preflight` | checks the conditions a review depends on — that the session is inside the repository under review, that the kit is installed for it, whether the hook is wired — and refuses otherwise |
| `coverage` | per-(file, reviewer) record of what has been reviewed, at which sha256, and how stale each file is; names files never reviewed, and counts opaque ones separately since no reviewer can read them |
| `carryover` | tracks confirmed defects across runs and classifies each into one of eleven outcomes — `re-reported`, `not-re-checked`, `plausibly-fixed`, `file-deleted`, `fixed-this-run`, `refuted-this-run`, `regressed`, `reported-not-carried`, `unanchored-not-checkable`, `anchor-ambiguous`, and **`missed`**. The reconcile-reviews skill says which of them need a human |
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
      { "matcher": "Read|Bash|Grep|Glob|Edit|Write|NotebookEdit",
        "hooks": [{ "type": "command",
                    "command": "python3 <review-kit>/bin/log_file_access.py" }] }
    ]
  }
}
```

The hook takes no configuration: it logs to the store of the repository the tool ran in, walking
up from the tool's directory so that a `cd src` keeps logging, and writes nothing at all where
no directory on the way up has a store. Writes and edits are logged too, but `audit` does not
count them as reads, and nor does it count a record with no agent id. For a Bash command it
follows `cd` (scoped to its subshell), simple `VAR=value` assignments, `for` loops, brace lists
and a plain `find … -exec cat` or `find … | xargs cat`, and resolves each name against the
directory the shell would be in, so `cd src && for f in a b; do cat -n $f.py; done` logs both
files. It credits a file only to a reading command whose output reaches the reviewer: not `wc`,
`ls` or `echo`, not `grep -l` or `grep -q`, and not a reader whose output went to a file, into
`| wc`, or out of a whole loop or `if` block redirected away. It cannot tell whether the command
printed anything: `grep` with no match, `false && cat`, `python3 a.py` and partial reads such as
`head -n1` still count. So `audit` catches careless over-claiming, not a reviewer set on
deceiving it; DESIGN.md lists the cases. It also stores
the raw command, which is what lets `audit` tell a read it could not follow from a file no
command named. Without the hook, `audit` refuses to run rather than reporting a pass it cannot
support — and it ignores records older than the run it is checking, so a read from an earlier
run cannot vouch for this one.

Be clear-eyed about what this does and does not prove. It shows that a tool call touching a
file was issued — not that the content was attended to. It is a floor, not a guarantee, and it
is worth having because the failure it catches (a confident report over files nobody opened) is
the most dangerous artefact this process can produce.
