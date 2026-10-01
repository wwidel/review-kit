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
                   fixes in the working tree (never committed)
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
argument. It applies a fix only when it confirmed the defect *itself* and the fix is mechanical
and local; everything else goes to the human. Fixes land in the working tree, never a commit,
and a red check reverts the whole batch rather than handing back a half-applied repository.

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

Roughly **880k–970k tokens for a full audit** of a repository of a few thousand lines, with a
floor near 145k per reviewer. Cost tracks the *protocol* far more than the repository at that
size: a 1,400-line repo cost 82% of what a 3,600-line one did. Trimming the preamble will not
rescue this — the preamble is ~18k of that 145k.

The protocol stops dominating once the repository is large. A 27,398-line repository estimates
at **1.85M**, because the source itself is then ~255k per reviewer against that 145k floor. So
`review.py estimate` runs in Stage 0 and the orchestrator puts the figure in front of the
person before spawning anything: the first attempt on that repository exhausted a weekly limit
with four reviewers running concurrently, and all four died before writing a findings file —
full cost, no artefact.

That is also why reviewers are spawned **one at a time**. Sequential execution gives up
wall-clock time and buys partial results: a limit then costs the legs that had not started,
rather than all of them, and the run resumes by skipping whatever already validates.

---

## Where the numbers on this page came from

Every figure above was measured once, by hand, from the transcripts of the development runs on
three Kaggle repositories — token totals read off the runs, the miss rate counted from two
consecutive reviews of one repository, the refutation rate from a controlled injection of ten
fabricated findings. **None of them has a script in this repository that reproduces it**, and
the runs themselves were not kept. They are recorded here because a rough, honestly-labelled
figure is more useful than silence about cost, and they should be read as one-off observations
rather than as measurements you can check.

That is a deliberate exception to this kit's own `N-1`, and the only one in it. Treat any of
these numbers as stale the moment the protocol changes: the ledger (`$STORE/ledger.jsonl`) is
what gives you figures for *your* repository, and those do have a source.

## Known limits

- **The summary is still in the working tree, and so is its history.** Everything else a
  review produces lives under `~/.review-kit/`, which no sweep of the repository reaches —
  but `REVIEW-SUMMARY.md` is deliberately committed, so `grep -r`, `git show HEAD:<name>` and
  `git log -p` all serve a previous run's conclusions. Reviewers are told by name not to read
  it. That instruction is the control, and an instruction is weaker than a construction.
- **`audit` has never run inside a live review.** Both halves are tested on fixtures — the hook
  writes the log, and `audit` fails a claimed read that the log does not support — but the two
  have never been joined by a real reviewer session. The hook must be in the settings file of
  the directory the session started in, and must be there before the session starts; until that
  holds, every reviewer's claim about what it read is taken on trust. `audit` refuses to run
  without a log rather than reporting a pass it cannot support, so the failure mode is a missing
  check, not a false one.
- **Fingerprints under-cluster.** Two reviewers citing the same defect at adjacent lines get
  different fingerprints, which is why every run produces ~100 proximity candidates for the
  reconciler to judge by hand, and why the miss rate above is an upper bound.
- **The reconciler is a single point of judgement.** It is the one stage with no independent
  check on it. The injection test is the only instrument pointed at it so far.
- **Scope is an extension allowlist.** A file whose suffix is unknown and whose name is not one
  of a handful of recognised build files (`Makefile`, `Dockerfile`, …) enters no tier. It is
  recorded under `unclassified` in the manifest rather than dropped silently, but nothing
  reviews it.
- **The bookkeeping has no test suite and no CI.** `bin/` is roughly 2,650 lines of Python,
  and the section above argues that every failure this kit has had came from that half rather
  than from the judgement half — yet every regression in it has been caught by running a
  review, not by a check that runs on its own. The fixtures in this repository's history live
  in commit messages, not in a suite.
- **Nothing measures whether a *confirmation* was earned** — only whether a refutation was.
