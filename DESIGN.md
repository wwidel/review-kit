# How the kit works, and why

The README tells you how to run a review. This tells you why it is shaped the way it is, what
it is defending against, and what has actually been measured rather than hoped for.

---

## The shape

```
                          ┌──────────────────────────────────────────┐
  STAGE 0  ── scope ─────▶│ manifest.json   review / tabular / opaque│
  (scripted, no           │ probes.json     23 mechanical checks     │
   judgement)             │ machine.json    cores · RAM · GPU        │
                          │ repo_profile    tests · linter · age     │
                          └──────────────────┬───────────────────────┘
                                             │  same inputs to all four
                       ┌─────────────────────┼─────────────────────┐
                       ▼          ▼          ▼          ▼
  STAGE 1        ┌──────────┐┌─────────┐┌──────────┐┌─────────┐
  four fresh     │fresh-eyes││  code   ││ analysis ││  docs   │   ── none sees another
  contexts, one  │ NO DOCS  ││correct- ││ numbers  ││ prose   │      none edits anything
  at a time      │          ││  ness   ││ ↔ scripts││         │      all on Opus
                 └────┬─────┘└────┬────┘└────┬─────┘└────┬────┘
                      └───────────┴──────────┴───────────┘
                                   │  findings-*.json   (one schema, evidence required)
                                   ▼
                    ┌──────────────────────────────────────┐
  THE GATE          │ validate    contract, scope honesty   │   ← a reviewer that fails
  (scripted)        │ fingerprint stable id + source anchor │      is re-run, not patched
                    │ cluster     corroboration + near-miss │
                    │ coverage    who read what, when       │
                    └──────────────────┬───────────────────┘
                                       ▼
  STAGE 2                     ┌────────────────┐
  one fresh context,          │   reconciler   │  re-runs every command
  reads JSON only —           │  VERIFIES, does│  re-reads every quote
  never reviewer prose        │  not summarise │  re-derives every argument
                              └───────┬────────┘
                                      │  reconciliation.json
                                      ▼
                    ┌──────────────────────────────────────┐
  RENDER            │ carryover  missed · regressed · …     │
  (scripted, so no  │ summarize  REVIEW-SUMMARY.md derived  │
   count can lie)   │ ledger     confirmed / refuted rates  │
                    └──────────────────┬───────────────────┘
                                       ▼
                   reports only — nothing in the repo is edited
                   + REVIEW-SUMMARY.md  + open_defects.json
```

The split is deliberate: **agents find defects, verify defects and decide fixes. Scripts do
everything else.** Every failure this kit has actually suffered was in the bookkeeping half —
a reviewer violating the contract, a reviewer overstating what it read, counts that did not add
up, fingerprints an agent retyped and abbreviated. None was in the judgement half.

---

## The five decisions that carry it

### 1. Independence is structural, not requested

The reviewers are separate agents that cannot see each other's output, are not told the others
exist, and never edit anything. Running the passes sequentially in one context — which is how
most audit tooling works — means the later ones anchor on the first one's conclusions, and the
disagreements that carry the signal never appear.

This is fragile in practice and has to be defended by construction. Twice a reviewer swept the
repository with `grep -r` and picked up a previous run's `REVIEW-SUMMARY.md`. Both disclosed it; that
is lucky, not a control. So the review store lives wholly outside the repository, and
every reviewer is told by name not to read the summary.

### 2. One reviewer is blinded

`review-fresh-eyes` gets the code and the data and **not the documentation**. Every other
reviewer is checking work against a stated intent, so it inherits that intent's framing and can
only find errors *within* it. The blinded leg is the only one that can find an error *of* it — a
pipeline that does not do what everyone, including its authors, believes it does.

This is not theoretical. On one repository the README said two features had been dropped, the
code's own comment said "(Dropped)", and a sibling model's column list omitted them. They were
still being trained on. **Only the blinded reviewer noticed**, and it could not cite the README
because it had never seen one; the reconciler closed the loop.

### 3. Every finding carries re-runnable evidence

A command plus what the reader should see, or a verbatim quote plus its location, or — last
resort — an argument. Severity (*if true, how bad*) and confidence (*how sure am I*) are
separate fields, because conflating them is the most common reviewer error. Evidence that is
only an argument, or a command that was not actually run, caps confidence below `certain`
however sure it sounds.

### 4. The reconciler verifies rather than summarises

It reads the JSON and nothing else — no reviewer prose, because prose transmits confidence and
re-introduces the anchoring. It re-runs each command, re-reads each quote, re-derives each
argument. It then reports — it does not act. Nothing in a review edits the repository, which
buys three things at once: an interrupted run resumes by scanning for the files each stage
produces, the working tree stays something a human authored, and a verified *diagnosis* never
carries an unverified *prescription* into the code (`R-4`).

A reviewer being right about a problem does not make its proposed fix right, and the two are
judged separately.

### 5. Nothing heavy runs during a review

Reviewers and the reconciler measure the machine first, classify every command before issuing
it, and refuse anything beyond the box — no trial runs, no timeout-and-kill, and no shrinking a
job to a sample and reporting the result as though it were the real one. A number that needs a
bigger machine is *deferred*, not assumed: it reaches the report with the command that would
settle it, kept separate from findings that are unverifiable in principle.

An unverified number that is clearly labelled unverified is a manageable state. A dead
workstation halfway through a review is not.

---

## What has actually been measured

Three repositories, five runs. These are observations, not claims.

### False positives: the reconciler catches them

Ten fabricated findings were injected into a completed run and the reconciler was re-run blind,
with no hint that anything had been planted. **It refuted all ten**, spanning a deliberate
difficulty range:

| planted claim | how it fell |
|---|---|
| a misquoted line (`shuffle=True` where the source says `False`) | read the line |
| "this constant is never used to seed anything" | the next two lines seed with it |
| a command whose stated output was wrong | ran the command; it returned something else |
| "these priors sum to 0.9997" | they sum to exactly 1.0 — it did the arithmetic |
| "pandas `.dt.weekday` is 1-indexed" | it is 0-indexed; acting on this would have *added* a bug |
| a real defect attributed to a false mechanism | reproduced the mechanism and showed the proposed fix would not have repaired it |

Confirmation rates moved from 100% to **86–93%** the moment there was anything to refute. The
earlier all-confirming runs were not rubber-stamping; those repositories genuinely contained
few false findings.

### False negatives: partly measured, and harder

The same repository was reviewed twice, unchanged, with fresh contexts. Of the 54 defects the
first run confirmed, **31 were re-raised by the second and 23 were not** — but the misses cluster on findings
whose anchor is a wide line range or a whole-document omission, where a single-line fingerprint
represents the defect badly. So 43% is an upper bound inflated by the anchoring mechanism, not
a clean reviewer miss rate. **All three criticals were re-found.**

`carryover` separates *reviewer missed it* from *reconciler dropped it*: a defect a reviewer
filed but the reconciler did not carry through is a downstream drop, and the two need different
fixes.

### The ledger

Each run appends to `$STORE/ledger.jsonl`, under `~/.review-kit/<repo>-<hash>/review/`. Per reviewer: how many findings were reported,
confirmed, refuted and unverifiable, over what denominator.

This is the only feedback signal that improves the reviewers over time. A confirmation rate
drifting down means a reviewer is generating noise and its checklist needs tightening. A
reviewer that is never refuted is probably reporting only the obvious. Findings counted as a
*rate* over a denominator — three findings in 140 functions versus three in twelve — are
comparable across runs in a way raw counts never are.

`$STORE/last_review.json` records where the last run got to, so the next one can be scoped
against it. It is a bookmark, not a timer: nothing reads its age and decides to run.

### Cost

A full audit costs roughly **520k tokens plus 145 per reviewable line**. That is a least-squares
line through six completed audits: five public Kaggle solutions (693 to 4,794 lines, 0.53M to
1.10M, re-run on 2026-10-12) and one private repository (20,259 lines, ~3.5M, on an earlier
version of the kit). It replaces 700k + 140, a line drawn through the smallest and largest of
the first five audits. It lands within about 15% of each of the six, but its slope rests almost
entirely on the large one, and the Kaggle figures leave out the orchestrating session's own
tokens. It is a very rough approximation. The README's WARNING has the table. At a few thousand
lines the fixed protocol cost dominates: a 1,400-line repository cost 77% of what a 3,700-line
one did. Trimming the preamble will not rescue this, because the preamble is ~18k of a
reviewer's ~145k floor.

Past that size the repository takes over. The 20,259-line audit cost ~3.5M, about twice what
`review.py estimate` would have priced it at. `estimate` charges each reviewer its floor plus
one read of the source, and on a large repository the source is re-read and re-verified many
times over. Its line count is right; its token figure runs low on large repositories.

The figure goes in front of the person before anything is spawned, which is why `estimate`
runs in Stage 0. The first attempt on a 27,398-line repository, which `estimate` put at 1.85M,
exhausted a weekly limit with four reviewers running concurrently. All four died before writing a findings file: full cost, no artefact.

That is also why reviewers are spawned **one at a time**. Sequential execution gives up
wall-clock time and buys partial results: a limit then costs the legs that had not started,
rather than all of them, and the run resumes by skipping whatever already validates.

---

## Where the numbers on this page came from

Every figure above was measured once, by hand. The miss rate and refutation rate come from the
transcripts of the development runs on three Kaggle repositories: two consecutive reviews of one
repository, and a controlled injection of ten fabricated findings. The cost formula comes from
the token totals of six completed audits: five public Kaggle solutions, each run's per-agent
counts recorded in the Cost section of its `REVIEW-SUMMARY.md`, and one private repository run
earlier. **None of them has a script in this repository that reproduces it**, and the runs
themselves are not in this repository. They are recorded here because a rough,
honestly-labelled figure is more useful than silence about cost, and they should be read as
one-off observations rather than as measurements you can check.

That is a deliberate exception to this kit's own `N-1`, and the only one in it. Treat any of
these numbers as stale the moment the protocol changes: the ledger (`$STORE/ledger.jsonl`) is
what gives you figures for *your* repository, and those do have a source.

## Known limits

- **The summary is still in the working tree, and so is its history.** Everything else a
  review produces lives under `~/.review-kit/`, which no sweep of the repository reaches —
  but `REVIEW-SUMMARY.md` is deliberately committed, so `grep -r`, `git show HEAD:<name>` and
  `git log -p` all serve a previous run's conclusions. Reviewers are told by name not to read
  it. That instruction is the control, and an instruction is weaker than a construction.
- **`audit` reads commands, not the reads themselves.** Its first live run reported 70 of 164
  declared reads as unsupported, and the transcripts showed every one had happened: the hook
  resolved bare names against the repository root, so `cd src && cat -n train.py` and
  `for f in …; do cat -n $f; done` logged nothing. It now follows `cd`, subshells, variables,
  `for` loops, brace lists and simple `find`, credits only output that reached the reviewer,
  and replaying that run's commands through it supports every declared read. A command
  it still cannot follow — a `$(...)`, a shell function — is reported as UNRESOLVED with the
  command attached, for a person to settle, rather than as a false accusation. The log also
  proves only that a reading command was issued, not that the content was attended to.
- **`audit` catches careless over-claiming, not a reviewer set on deceiving it.** The hook
  reads the command, not its output, so some commands that show little or none of a file
  still credit it: `grep` with no match, `python3 a.py` (output, not source), `diff` of
  identical files, `cmp`, `tail` past the end, `| sed d`, `sed -n 0p`, `awk 'NR==0'`, commands
  that never run (`false && cat`, `if false; then cat …`), a path in a `python3 -c` comment,
  and partial reads (`head -n1`, `sed -n 1p`, `cut -c1`). A path merely mentioned in a reading
  command (`echo src/x.py; cat a.py`) turns a false claim about it into UNRESOLVED rather than
  UNSUPPORTED. A static parser cannot close these; `tests/test_hook_parser.py` pins each one, so
  a change to any of them is deliberate.
- **Fingerprints under-cluster.** Two reviewers citing the same defect at adjacent lines get
  different fingerprints, which is why every run produces ~100 proximity candidates for the
  reconciler to judge by hand, and why the miss rate above is an upper bound.
- **The reconciler is a single point of judgement.** It is the one stage with no independent
  check on it. The injection test is the only instrument pointed at it so far.
- **Unknown suffixes are placed only when git tracks them.** A tracked file whose suffix and
  name the kit does not know is opaque if its first 8 KiB hold a NUL byte, and is placed by
  length otherwise. An untracked one goes under `unclassified` and nothing reviews it — which
  keeps an ignored tree of extensionless output out of every tier, and also means a new,
  not-yet-added script with an odd suffix goes unread.
- **`residue` sees only what git sees.** It compares `git status` before and after, ignored
  files included, with untracked and ignored directories collapsed to one entry each. A file
  created inside a directory that was already untracked or ignored — a `.pytest_cache/` left
  by an earlier run, a new file in `.venv/` — does not show, and nor does an edit to an
  existing ignored file, which is not hashed. Outside a git repository it does not run.
- **The tests check the bookkeeping, not the judgement, and not Claude Code.** `tests/` covers
  the scripts in `bin/`, and `.github/workflows/tests.yml` runs it on Ubuntu and macOS with
  Python 3.10 and 3.14. It cannot check what reviewers or the reconciler conclude, and it feeds
  the hook hand-built payloads, so a change in what Claude Code sends would pass it; the live
  check in `tests/README.md` is the guard against that.
- **Nothing measures whether a *confirmation* was earned** — only whether a refutation was.
