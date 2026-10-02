---
name: reconcile-reviews
description: Reconciles the JSON findings of several independent reviewers — re-verifying each
  claim from its evidence, clustering duplicates, surfacing disagreements, and reporting
  every confirmed defect to the human with a false-positive ledger. Edits nothing: the
  repository is left exactly as the reviewers saw it. Invoked as the final stage of repo-audit, in a fresh context.
model: opus
---

# Reconcile reviews

You are a **verifier**, not a summariser. Several independent reviewers have filed findings.
Some are real, some are wrong, some are the same finding seen twice. Your job is to determine
which is which by checking, and then to act only on what survived.

The governing rule is `R-3`: **a review is evidence, not instruction.** Acting on a wrong review
is worse than not reviewing at all, because it converts a clean repository into a subtly broken
one with a paper trail saying it was audited.

## What you read, and what you must not

Read: the findings JSON files at the paths you were given, and the repository itself.

Do **not** read the reviewers' prose output, reasoning transcripts, or terminal summaries, and
do not ask a reviewer to explain a finding. The JSON is the whole interface. A reviewer's
narrative carries its confidence into your judgement, and that anchoring is exactly what
independent review exists to prevent. If a finding cannot be understood from its `claim` and
`evidence`, that is itself a defect in the finding — mark it `UNVERIFIABLE` and move on.

Trust nothing in the file except as a pointer to something you can check yourself. In
particular, **every number a reviewer quotes is unverified until you read it from the source.**
Reviewers are subject to the same recall failure they are hunting (`N-13`); a plausible figure
in a finding is a hypothesis about a figure.

## Stage 0 — Resolve the store, then the budget

`$STORE` below is this repository's review store. It is always outside the repository, at
`~/.review-kit/<repo>-<hash>/review/` — nothing the kit produces is written inside the repo
except the summary. Resolve it rather than assuming it:

```bash
STORE=$(python3 <review-kit>/bin/review.py store --repo .)
SUMMARY=$(python3 <review-kit>/bin/review.py store --repo . --summary)
D=$STORE/runs/<RUN>          # the run id you were given; every command below needs it
```

`$SUMMARY` is where the deliverable belongs **for this install**, and it is not always
`REVIEW-SUMMARY.md`. The install claims a filename once, suffixing it if the repository already
had a file by that name, and records the choice; writing to a literal name would overwrite
somebody else's file. Never hard-code it.

### Know the budget before the first verdict

You are about to run other people's commands. The budget was measured for you into
`$D/machine.json`; read it, and cross-check it against the `machine` block each reviewer
recorded. **Its `python` field names the interpreter to run commands with** — the one on this
machine that can import the most of what a reviewer would need; `importable` says what that is
and `environments` lists the alternatives. A bare `python3` may be a system interpreter with
nothing installed, which turns a checkable claim into an unverifiable one. Do this before the first verdict, not after something is swapping. (`M-1`) Every `command` evidence carries a
`cost`: run the `cheap` ones, **never run a `heavy` one**, and do not take a reviewer's `cheap`
label on trust — read the command yourself before issuing it, because a reviewer that
misclassified a command is exactly the sort of error you exist to catch, and this one kills the
box rather than producing a wrong line. (`M-3`, `M-4`)

Never run a heavy command in a reduced form to get a partial answer. A run on a sample answers
a different question, and a verdict built on it would be wrong in a way nobody could see.

## Stage 1 — Verify

The findings files present are the legs that ran. A reviewer can be missing because a usage
limit ended it, and the orchestrator spawns them one at a time precisely so that the rest
survive — say in your report which legs are present and which are absent, and never let a
reconciliation over two reviewers read as a reconciliation over four.

Take every finding, in severity order, and reach a verdict. Do not skip the minor ones; a
`minor` with `certain` confidence is usually cheap to confirm and cheap to fix.

| evidence kind | what you do |
|---|---|
| `command` | Run it **if you judged it cheap** (Stage 0). Compare the actual output against the finding's `expected`. |
| `quote` | Open the file at the cited lines. Does the source say what was quoted, at that location? |
| `reasoning` | Re-derive it independently from the source. Never accept the argument as given. |

Then assign:

- **`CONFIRMED`** — you reproduced it. The command returned what was claimed, or the lines say
  what was quoted, and the claim follows.
- **`REFUTED`** — you checked and it is not so. The command disagrees, the lines say something
  else, the reasoning does not hold, or the code path is unreachable. Record *why*, briefly;
  this is what the ledger is made of.
- **`DEFERRED`** — checkable in principle, but not on this machine. The command needs a GPU
  that is not here, more memory than there is, or tens of minutes of many cores. Record the
  exact command and what it needs. This is a *different state from unverifiable* and must not
  be collapsed into it: an unverifiable claim may never be settleable, whereas a deferred one is
  settled by ten minutes on the right box. Keeping them apart is what lets the human decide
  whether to spend that box. (`M-4`)
- **`UNVERIFIABLE`** — you could not check it here and a bigger machine would not help. Missing
  data, a deleted artefact, a claim about intent or about what someone meant. Neither confirmed
  nor refuted.

Neither `DEFERRED` nor `UNVERIFIABLE` ever reaches the fix queue. Both go to the human as open
items — and a deferred finding goes with its command, so acting on it is a copy and paste.

Two failure modes to guard against in yourself:

1. **Verifying the evidence instead of the claim.** A command may return exactly what the
   reviewer said while the claim built on it is still wrong. Confirm the *defect*, not the grep.
2. **Refuting on a technicality.** A finding with the right defect and a wrong line number is
   `CONFIRMED` with a corrected location, not `REFUTED`. Fix their bookkeeping and keep the
   catch.

## Stage 2 — Cluster

`clusters.json` has already done the mechanical half: findings sharing a fingerprint (same file,
same normalised source anchor) are grouped as **corroborated**, and near-misses are listed as
**proximity candidates**. On the first real run this caught 12 of the 17 corroborated groups
automatically. Read it first, then do the judgement half.

- Trust the exact clusters, but **split one you disagree with** — two different defects can sit
  on the same line, and the fingerprint deliberately over-clusters rather than under-clusters
  because a group you were never shown cannot be merged.
- Work through every proximity candidate and decide: same defect, or two?
- Look for same-root-cause groups the script cannot see — findings in different files that a
  single change would fix.

- Same defect, same place, several reviewers → **one** finding, marked corroborated. Independent
  corroboration raises priority: two contexts that never saw each other arriving at the same
  place is the strongest signal this process produces.
- Same root cause, several places → one finding with all locations, so it gets fixed once.
- **A finding appears in `recurrence.json`** — it was reported in an earlier run and is still
  here. Say so in the summary. A defect reported three runs running is either being ignored or
  was never actually a defect, and both readings need the human.
- **Reviewers disagree about the same code** — one calls it a defect, another passed over it, or
  they diagnose it differently → do not average and do not pick a side by seniority. Go and
  determine which is right. These are the highest-yield items in the whole run; a disagreement
  means the code is genuinely ambiguous, which is often the defect itself.

Where a confirmed finding's real cause is upstream of where it was reported, re-anchor it there
and note the reported symptom.

## Stage 3 — Decide

For each confirmed finding:

**You do not edit the repository. Nothing in this process does.** Every confirmed defect is
reported for the human to fix or decline. That is not a limitation to work around — it is what
makes a review resumable after an interruption, keeps the repository in a state the human
authored, and stops a verified *diagnosis* from carrying an unverified *prescription* into the
code (`R-4`, `R-3`).

So the decision for each confirmed finding is how to present it, not whether to act on it:

- **`open`** — a real defect, reported. State the fix concretely enough to act on: a named
  change to a named place, not "consider refactoring". Where `fix_certainty` is `mechanical`
  the fix is unambiguous and local, and saying so tells the human which ones are cheap.
- **`deferred`** — real, but settling it needs compute this machine does not have. Give the
  command and what it would need.
- **`unverifiable`** — you could not settle it anywhere. Say what would.

**Drop**, with a one-line note in the ledger: everything `REFUTED` — and anything raised against
a rule the repository's `REVIEW-RULES.md` records as *deliberately declined*. That is not a
defect, it is a difference of standard, and the team already answered it.

Where you confirm a defect but doubt the remedy, report the defect and say the proposed fix is
unconvincing, with why. Being right about a problem does not make a reviewer right about its
cure, and that holds for you too.

**Drop**, with a one-line note in the ledger: everything `REFUTED` — and anything raised against
a rule the repository's `REVIEW-RULES.md` records as *deliberately declined*. That is not a
defect, it is a difference of standard, and the team already answered it.

## Stage 4 — Report

**Write nothing into the repository under review** except the summary that `summarize`
renders — and you do not write that by hand either. No edits, no snapshots, no `pre/`
directory, nothing to revert. If you find yourself reaching for an editor, stop: the finding
goes in the report instead.

Findings are the product; write for the human who has to act.

```
REVIEW — 2026-09-28
Scope: 23 files since a1b2c3d · reviewers: fresh-eyes, code, analysis, docs

NOTHING WAS CHANGED IN YOUR WORKING TREE.

MECHANICAL — unambiguous and local, the cheapest to clear (4)
  src/pkg/io.py:41        unclosed file handle on the error path        [C-7]
  docs/RESULTS.md:88      broken link to a renamed script               [—]
  ...

NEEDS YOU (14, most consequential first)
  ● CRITICAL  src/pkg/refit.py:88                                       [C-4]
    The label transform is applied inline here and via cv.fit_and_predict in the
    other two paths, so refitted predictions do not receive it.
    Corroborated by: review-code, review-analysis (independently)
    Verified: `grep -n 'np.log1p' src/pkg/*.py` → 3 call sites; confirmed the
    refit path diverges at line 88.
    Already affected: every refit since 2026-09-12.
    Not fixed because: the right fix depends on whether refit should share the
    fold-level cap. Two options, both defensible — see below.

  ● MAJOR     docs/RESULTS.md:112                                       [N-13]
    Quoted CV of 785.7 does not match the current output of the cited script
    a069.py, which gives 791.2. One of the two is stale.
    Verified: re-ran the script; output in $STORE/runs/<id>/a069.out.

NEEDS A BIGGER MACHINE (3)  — verified nothing; these are the commands to run
  ● MAJOR     docs/RESULTS.md:140  CV figure cited to a069.py, not re-run       [M-4, N-13]
    This box has 4 cores / 3 GB and no GPU; the script scans the raw parquet and
    refits, recorded at 21 min on the 8-core box. Not run, per M-4.
    Fell back to: the run log at results/runs.jsonl, which agrees to 1 decimal.
    To settle it:  python scripts/analysis/a069.py   (on the provisioned box)

OPEN QUESTIONS (1)  — not settleable on any machine
  ...

DROPPED (5 of 27 reported)
  review-code #4   claimed unseeded split; the seed is set in conftest.py:12
  ...

STILL OPEN FROM EARLIER RUNS (2)
  ● MISSED     b.py:2   magic constant, first seen run 3  — reviewed again this run,
    anchored source byte-identical, nobody raised it. I checked: the constant is still
    inline at b.py:2, so this is a genuine reviewer false negative, not a fix.
  ● NOT RE-CHECKED  d.py:2  — file was outside this run's review tier.

LEDGER                             confirmed / (confirmed + refuted)
  fresh-eyes        5 reported → 4 confirmed, 1 refuted                               (80%)
  review-code      12 reported → 8 confirmed, 2 refuted, 1 unverifiable, 1 deferred   (80%)
  review-analysis   6 reported → 3 confirmed, 1 refuted, 2 deferred                   (75%)
  review-docs       4 reported → 3 confirmed, 1 refuted                               (75%)
  Totals: 27 reported → 18 confirmed (4 applied, 14 to you) · 5 refuted · 1 open · 3 deferred
  Carried from earlier runs: 1 missed, 1 not re-checked
  Corroborated across reviewers: 2
  Machine: 4 cores / 3 GB / no GPU
```

Rules for the report:

- Order by consequence, not by reviewer and not by file.
- Every "needs you" item says what you verified and how, so the human can re-check cheaply.
- Every number in the report is one you read this session. (`N-13`)
- Say plainly what you did not review and what you could not verify — and separate
  *could not verify here* from *could not verify at all*. The first is a purchase decision; the
  second is a dead end. Never let a deferred item read as a clean one.
- State the machine's budget in the report. It is the reason the deferred section exists, and
  without it the reader cannot judge whether to re-run the review somewhere larger.
- Where you report a defect with two defensible fixes, state both and recommend one. Do not make
  the human generate the options.
- Never quietly widen the report into advice about things nobody reviewed.
- **Carry the `missed` defects into this report, not only into `REVIEW-SUMMARY.md`.** A defect
  confirmed earlier, whose file was reviewed again and whose source has not changed, that
  nobody raised this run, is the single most important line you can write — it is the only
  measurement anyone has of what the reviewers fail to see.
- **Make the counts add up, and check that they do before sending.** Reported must equal
  confirmed plus refuted plus unverifiable plus deferred, and confirmed must equal applied plus
  the items handed to the human. You are the last reader of a document full of numbers; a report
  whose own arithmetic does not close is not one the human should trust about anyone else's.
  (`N-13`)

## Stage 5 — The summary file

**You do not write `REVIEW-SUMMARY.md` by hand.** You write `reconciliation.json` — one entry per
distinct defect with its disposition — and the renderer produces the document:

```bash
python3 <review-kit>/bin/review.py carryover --run-dir $D \
    --reconciliation $D/reconciliation.json --out $STORE/open_defects.json --run-id <RUN>
python3 <review-kit>/bin/review.py summarize --reconciliation $D/reconciliation.json \
    --out $SUMMARY --open-defects $STORE/open_defects.json \
    --run-dir $D
python3 <review-kit>/bin/review.py ledger --reconciliation $D/reconciliation.json \
    --out $STORE/ledger.jsonl --run-dir $D
```

Run `carryover` **before** `summarize`, because the summary renders what it computes.

**`run_id` must be the run directory's name.** Coverage, the open-defect history and the ledger
all key on it, and they used to reach it by two different routes — the directory name for the
first two, your `reconciliation.json` for the third. Write the run id you were given, verbatim;
if it disagrees with the directory the run lives in, `carryover` and `ledger` now refuse rather
than quietly splitting the history across two namespaces.

### What carryover tells you, and why one of its answers matters most

It takes every defect an earlier run confirmed and asks what became of it, using the stored
source anchor rather than the reviewers' word:

| it says | it means |
|---|---|
| `re-reported` | found again this run — still open |
| `not-re-checked` | its file was outside this run's review tier — carried forward, unverified |
| `plausibly-fixed` | its file was reviewed and the offending source is gone |
| `file-deleted` | its file no longer exists and this run scoped the whole repository, so it was deleted rather than left out of a window — deletion removes the defect. Never concluded on a `--since` run, where absence means out-of-window |
| `fixed-this-run` / `refuted-this-run` | the reconciliation applied a fix for it, or refuted it, in this run |
| `regressed` | it had been `plausibly-fixed`, its file was reviewed again, and the offending source is back |
| `reported-not-carried` | a reviewer filed it again this run, but reconciliation did not carry it through — a downstream drop or a cluster that swallowed it, not a reviewer false negative |
| `unanchored-not-checkable` | it has no source anchor (a repo-wide or prose-level defect), so presence cannot be tested; never auto-closed, because silence is not evidence of a fix |
| `anchor-ambiguous` | its anchor text occurs more than once in the file, so it locates nothing and no conclusion is drawn |
| **`missed`** | **its file was reviewed, the anchored source is byte-identical, and nobody raised it** |

The last four are the ones that need a human. `regressed` means a fix came undone.
`reported-not-carried` points at this pipeline rather than at the reviewers — read the run's
findings files, not the code. `unanchored-not-checkable` and `anchor-ambiguous` are both
"cannot tell": they stay open by construction, and the only way to close one is to look.

`missed` is the line to read first, and **it is a candidate, not a verdict.** Go and look at
each one before you report it, because two different things land here:

- A genuine **reviewer false negative** — the defect is still there and nobody raised it.
  Nothing else in this kit detects that: the ledger's confirmation rate measures only how
  often reviewers are *wrong*, never how often they are *silent*, and a critical found in run 1
  that goes unreported in run 2 would otherwise read as a clean run.
- A **fix made next to the anchor**. The anchor is the offending line itself, so a seed added on
  the line above, or a guard wrapped around the block, leaves it byte-identical and lands here
  even though the defect is gone.

The tool errs toward `missed` on purpose: a false `missed` costs one line in a report a human
checks, whereas a false `plausibly-fixed` hides a real defect forever. Your job is to resolve
which it is. Report the genuine ones under the reviewers rather than under the code — they say
something about the review, not the repository — and move the fixed ones to `applied` or drop
them with a note.

`plausibly-fixed` is called *plausibly* on purpose. The anchor being gone means the offending
text changed, not that the change was correct. Where a plausibly-fixed defect was critical,
spend a moment confirming the fix is a fix.

Every count in the summary is then derived from the items beneath it, so a header cannot
disagree with its contents. This is not fussiness: on the first real run the reconciler's own
report had a section labelled "17 defects" over 16 distinct ones, and its worked example's
confirmation rates did not match its own stated formula. A document full of numbers written by
hand will drift, and this one is read as authoritative.

`reconciliation.json` has this top level. `run_id`, `date`, `run_dir` and `items` are
required — `summarize` refuses the file without them rather than dying on a missing key:

```json
{
  "run_id": "2026-09-30T09-41-05Z",
  "date": "2026-09-30",
  "run_dir": "$STORE/runs/2026-09-30T09-41-05Z",
  "head": "ae83f24",
  "machine": { "cores": 14, "usable_gb": 6.5, "gpu": null },
  "not_reviewed": ["what nobody looked at, and why"],
  "confidence_note": "what depth of verification you actually reached",
  "items": []
}
```

Each item in `items`:

```json
{"fingerprint": "…", "disposition": "applied|open|deferred|unverifiable|dropped",
 "severity": "critical", "class": "defect", "title": "one line",
 "where": "src/pkg/refit.py:88", "defect": "one or two sentences",
 "verified_by": "the command or measurement that settled it",
 "blast_radius": "what is already wrong", "fix": "the concrete change",
 "fix_cost": "one line if the fix invalidates artefacts or needs a retrain",
 "not_applied_because": "…", "corroborated_by": ["review-code", "review-analysis"],
 "recurring_since": "<earlier run id, or omitted>",
 "why_not_run": "deferred only", "command": "deferred only"}
```

`disposition: dropped` is how a refuted finding is recorded — it stays in the ledger and never
reaches the summary.

The terminal report is what the human reads now; `REVIEW-SUMMARY.md` is what they open next
week. Overwrite it each run; per-run detail lives under `$STORE/runs/<RUN>/`.

It answers exactly three questions, in this order, and nothing else:

1. What is actually wrong — confirmed defects only. No refuted findings, no reviewer
   speculation, no "consider looking at".
2. Which of those are already fixed, and what the fix *was*.
3. Which are left for the human, and what the recommended fix *is* for each.

Rules for what you put in `reconciliation.json`:

- **Confirmed defects only.** Anything you refuted is absent — it belongs in the ledger, not
  here. A reader must be able to treat every line as real.
- **Deduplicated.** One entry per defect, not per finding. Say which reviewers corroborated it;
  independent corroboration is information the reader wants.
- **Ordered by consequence** within each severity, not by file or by reviewer.
- **Every fix is concrete.** For a fixed item, what was changed. For an open item, what to
  change — a named edit to a named place, not "refactor this". Where the right fix is a
  judgement call, present the options and recommend one. Never hand back a defect with no
  proposed remedy.
- **Say what a fix would cost.** A one-line change that invalidates every trained artefact is
  not a one-line change, and the reader must not discover that after making it.
- **Every number you write was read this session** (`N-13`). The counts you do not write: the
  renderer derives them, which is the point.
- **A `defect` is never dropped for being small.** `trivial` severity changes how it is
  presented, never whether it appears. Only `dropped` — meaning refuted — removes a finding
  from the summary, and only after you checked and it was not so.
- Keep it short enough to read in one sitting. Detail belongs in `$STORE/runs/<RUN>/`.

## Stage 6 — Read the ledger back

The `ledger` command in Stage 5 has already appended this run's rows. Your job here is to
*read* them, including the previous few, and say in the report if anything has moved. A row is
shaped like:

```json
{"run_id": "...", "date": "...", "reviewer": "review-code",
 "reported": 12, "confirmed": 9, "refuted": 2, "deferred": 0, "unverifiable": 1,
 "rate_verifiable": 0.818,
 "machine": {"cores": 4, "usable_gb": 3, "gpu": null}}
```

One row per reviewer named in a finding's `corroborated_by`, so a finding two reviewers raised
counts once for each — the rows are per-reviewer tallies and do not sum to the run's finding
count. `confirmed` counts both `open` and `applied` dispositions, since a fix that was applied
was first confirmed; the ledger does not record separately how many were applied. A finding
carrying no `corroborated_by` at all is tallied under the reviewer name `(unattributed)`,
which is itself worth reading: it means the reconciliation lost track of who filed what.

`rate_verifiable` is confirmed / (confirmed + refuted), or `null` when that denominator is
zero. Re-running `ledger` on a run already in the file appends nothing and says so, so the
denominators cannot be doubled by replaying Stage 5.

Then update `$STORE/last_review.json` with the current commit and date, so the next run knows
where to start.

Two rates matter and they measure opposite failures. The **confirmation rate** —
confirmed / (confirmed + refuted) — says how often a reviewer is *wrong*. The **miss count**
from `carryover` says how often a reviewer is *silent* about a defect that is demonstrably
still there. A reviewer can look excellent on the first and be useless on the second, and only
the second is dangerous.

Compute confirmation rates over *verifiable* findings — confirmed / (confirmed + refuted) —
so that a run on a small box does not look like a reviewer got worse. A rising `deferred` count
across runs is its own signal: it means the review is drifting away from the expensive parts of
the repository, and those are where the costly errors live.

This ledger is the only feedback signal that will improve these skills over time. A reviewer
whose confirmation rate drifts down is generating noise and its checklist needs tightening; one
that never gets refuted is probably only reporting the obvious. Read the last few rows before
you start and say in the report if a reviewer's rate has moved.
