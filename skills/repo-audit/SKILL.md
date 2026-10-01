---
name: repo-audit
description: Whole-repository review for Python data-analysis and machine-learning repos.
  Runs four independent reviewers in parallel fresh contexts — blinded fresh-eyes, code
  correctness, analytical validity, document integrity — then a fresh reconciler that
  re-verifies every claim, applies only what it confirmed, and reports the rest. Invoke ONLY
  when the user explicitly asks for a review; never start it on your own judgement, on a
  schedule, or from a git hook.
model: opus
---

# Repo audit

Four independent reviewers, then a reconciler that does not trust any of them.

**Bookkeeping is `bin/review.py`, not you.** Scoping, the file manifest, contract validation,
scope auditing, fingerprints, clustering, recurrence, the summary and the ledger are all
computed. Every failure this kit has actually suffered — a reviewer violating the contract, a
reviewer overstating what it read, counts that did not add up, findings that could not be
matched across runs — was in the bookkeeping half. Do not do that half by hand.

**This runs when the human asks for it, and at no other time.** It costs five agents and a
verification pass that re-runs commands, so it is not a hook, not a schedule, and not
something to launch because the diff looks big or the last run was a while ago. If you think a
review is warranted, say so in one line and stop — the decision to spend is the user's. (`A-4`)

The value comes from **independence**, not from the checklists. Four fresh contexts that
cannot see each other's conclusions disagree in informative places; one context running four
passes in sequence anchors on its own first pass and finds nothing the first pass missed. Every
instruction below that looks like bureaucracy is protecting that property.

You are the orchestrator. You do not review anything yourself, and you do not summarise what
the reviewers say — the reconciler reads their files directly, and your summary would be a
fourth opinion nobody asked for.

## Stage 0 — Scope, machine, posture

Run the tooling. Do not compute any of this by hand.

**Shield first, before anything is scoped.** `REVIEW-SUMMARY.md` is a real committed file, so
`grep -r` and `rg` find it, and a reviewer that reads last run's summary is no longer
independent — which has happened twice. Shielding *after* `scope` is worse than not shielding:
the manifest then lists a file the pipeline has removed, and every reviewer is charged a
SCOPE GAP for failing to read it.

**Run `preflight` first and stop if it fails.** It checks the one condition everything else
rests on: that this session is *in* the repository being reviewed. The access-log hook follows
the working directory, so a review launched from somewhere else records its reads against
another repository's store, and `audit` then reports every honest reviewer as having claimed a
file it never opened. It also confirms the kit is installed for this repo and tells you
whether the hook is wired.

**Derive every path; hard-code none of them.** The store, the summary filename and the kit's
own location all come from the install record — `store`, `store --summary`, `store --kit`. The
summary is not always `REVIEW-SUMMARY.md`: if the repository already had a file by that name,
the install claimed a suffixed one rather than overwriting it.

```bash
python3 <review-kit>/bin/review.py preflight --repo .   # STOP if this fails
STORE=$(python3 <review-kit>/bin/review.py store --repo .)
SUMMARY=$(python3 <review-kit>/bin/review.py store --repo . --summary)
RUN=$(date -u +%Y-%m-%dT%H-%M-%SZ); D=$STORE/runs/$RUN; mkdir -p $D
python3 <review-kit>/bin/review.py shield --repo .
python3 <review-kit>/bin/review.py scope --repo . --out $D --list [--since <ref>]
python3 <review-kit>/bin/review.py machine --out $D/machine.json
python3 <review-kit>/bin/review.py characterize --repo . --out $D/repo_profile.json
python3 <review-kit>/bin/review.py probe --manifest $D/manifest.json --out $D/probes.json
python3 <review-kit>/bin/review.py estimate --manifest $D/manifest.json
cat $STORE/last_review.json 2>/dev/null || echo "no previous review"
```

### Show the estimate and WAIT

`estimate` prints the two numbers that decide what this run costs — how many files will be
read and how many lines they hold — and turns them into a token figure. **Put that figure in
front of the person and stop.** Do not spawn anything until they say to go ahead.

This is not a formality. A fan-out over a 27,000-line repository was estimated afterwards at
1.85M tokens; it exhausted a weekly limit and every reviewer died before writing its findings
file, so the run cost everything and produced nothing. The estimate takes a second and is the
only point at which that is cheap to avoid.

Say what it would cost, say what the smaller options are — a `--since` window, one fewer
reviewer leg, a single-purpose skill on the files they actually care about — and let them
decide. If they decline, stop; there is nothing to clean up but the `unshield`.

`scope` writes `manifest.json` in **three tiers**. The *review tier* is read in full: source,
Markdown, and anything else under the threshold. The *reference tier* is tabular data at any
size plus any other file over ~500 lines that is neither source nor Markdown; each is
described — size, sha256, columns, first row — and **queried with a script, never read**. The
*opaque tier* is listed only. Source and Markdown are never demoted however long they run,
because the longest script and the longest README are where a defect hides; `scope` prints
the oversized ones as a note instead. On a repository
whose committed CSVs run to 274,000 lines against 3,700 lines of code, the difference is the
whole token budget. It exits non-zero on an empty scope, so you cannot review nothing by
accident.

`probe` runs the mechanical static checks **once, for everybody**: seeding calls, unsafe
deserialisation, broad excepts, dynamic dispatch, non-atomic writes, path literals, magic
numbers, plus AST checks for files that do not parse, names redefined in one scope, and
functions with identical bodies. Without it, every reviewer runs the same greps and each
grep's output lands in four separate contexts. Hand `probes.json` to all four.

Be clear with them about what a probe is: **a place to look, never a finding.** The table is
deliberately over-inclusive — read this run's own counts from `$D/probes.json` rather than
quoting a figure from anywhere else; most of the hits will be fine. A reviewer must never
file a finding because a probe matched, and must never file one without having read the
code around it.

`characterize` reports signals about the repository's posture — tests, linter config, CI,
commit recency. **Treat its verdict as a hint and nothing more.** The default assumption for
this kit is a live repository under active development, and the human states otherwise if it
is not. Pass what you know to the reviewers: a frozen archive and a live repo deserve different
severities for the same hygiene item, but the same severity for the same defect.

### Choosing the window

- **Default**: everything changed since `$STORE/last_review.json`, plus one rotating slice of
  the stable core. Without the rotation, settled code is never re-read and the interesting new
  code gets a thin pass as the repo grows; with it, the core is covered over several cycles.
  The marker records where the last review got to so this one can be scoped against it — it is
  a bookmark, not a timer, and its age is never a reason to start a run.
  Pick the slice least recently reviewed and name it in the report.
- **No marker, or a repository being reviewed for the first time**: there is no window to diff
  against, so the choice is the user's, not a silent default. If the repository is small enough
  to read (roughly under 60 source files), treat it as `--full` and say that is what you are
  doing. If it is larger, propose a starting slice — the entry points and whatever the results
  depend on — and ask before spending the agents.
- **`--full`**: the whole repository. Expensive, and the user asks for it explicitly.
- **`--since <ref>`**: an explicit window.

There is no quick mode. A review that skips the adaptive pass costs the same and finds less.
If the window is empty, say so and stop. Never review nothing. If the window is very large
(say more than ~60 files), split it and tell the user you are doing so: a reviewer given more
than it can read will silently read a fraction and file a clean-looking report.

`machine` has already measured the budget into `$D/machine.json`. Pass those numbers to every
reviewer so that none of them has to discover the limit by hitting it, and do not measure it
again by hand. (`M-1`)

If the project's `CLAUDE.md` names a larger machine where heavy work belongs, say so in the
prompts too — not as an invitation to use it, but so reviewers phrase deferred commands for it.
No reviewer and no reconciler runs heavy work anywhere during a review. A review is a read of a
state, not a re-run of the project. (`M-2`, `M-4`)

## Stage 1 — Four reviewers, in parallel, blind to each other

`REVIEW-SUMMARY.md` is a real, committed file, so `grep -r` and `rg` find it — and a reviewer
that reads last run's summary is no longer independent, which is the single property this whole
design buys. It has happened twice: a reviewer and a reconciler each swept the repository and
picked up prior conclusions. Both disclosed it honestly, but "they said they re-derived it" is
not something you can check. `shield` moves the file outside the repository for the duration
and `unshield` puts it back. If an earlier run was interrupted mid-shield, `shield` restores it
before shielding again, so the file is never orphaned.

(The rest of the store is already outside the repository entirely, under `~/.review-kit/`,
so nothing else is reachable from inside the tree. Only the summary needs moving.)

### Spawn them ONE AT A TIME, never in parallel

Launch one reviewer, wait for it to finish, harvest and validate its file, and only then
launch the next. Parallelism buys wall-clock time and loses the whole run when a usage limit
lands partway through: four concurrent reviewers over a large repository hit a weekly limit
together and all four died before writing anything, so the run cost its full budget and left
no artefact at all. Run sequentially and a limit costs you the reviewers that had not started
yet — the ones that finished have their findings on disk, and the run can be resumed.

**Resume rather than restart.** Before launching each leg, check whether its findings file is
already there and already validates:

```bash
for leg in code analysis docs fresh-eyes; do
  f=$D/findings-$leg.json
  if [ -s "$f" ] && python3 <review-kit>/bin/review.py validate \
        --findings "$f" --manifest $D/manifest.json >/dev/null 2>&1; then
    echo "$leg: already done, skipping"
  else
    echo "$leg: to run"
  fi
done
```

A leg that is already done is not run again. Re-running one costs its full price and buys a
second opinion from the same reviewer, which is worth nothing — it is the *other* legs that
make this an audit.

Order them so that the most valuable leg goes first, because it is the one most likely to
survive a limit: **fresh-eyes, then code, then analysis, then docs.** Fresh-eyes leads because
it is the only leg that can find an error *of* the stated intent rather than within it, and
because nothing else in this design would notice what it misses.

**All four run on Opus.** Review is the work this kit exists to do; a cheaper model on any
leg buys nothing worth having and makes a disagreement uninterpretable — you cannot tell a
genuine catch from a capability gap.

If a limit does land mid-run, say so plainly in the handover: which legs produced findings,
which did not, and that the reconciliation covers only the legs that ran. A partial audit
honestly labelled is useful. A partial audit presented as a full one is the clean report over
unread code that this whole design exists to prevent.

Non-negotiable properties. Every one of these has a way of eroding under time pressure, and
each erosion silently converts this into an ordinary single-agent review:

- **No reviewer sees another's output, prompt, or existence.** Do not tell them they are part of
  an audit, do not mention what the others cover, and do not pass one's findings to another.
  Running them one after another does not change this: the second reviewer must not be told
  that a first one ran, still less what it found. Sequential execution is a budget decision,
  not a pipeline — independence is unaffected because each context starts empty either way.
- **Each writes to its own file** in `$STORE/runs/$RUN/` and prints only a one-line summary.
- **No reviewer edits anything.** Concurrent edits would invalidate the line numbers the others
  are citing at that moment, and the reconciler needs an unmodified tree to verify against.
- **Subagents do not inherit skills.** Each prompt must name the skill file by path and say to
  read it. Naming the skill is not enough.
- Give each the same scope, the path to `<review-kit>/FINDINGS.md`, and the measured machine budget.

The prompts, in shape — one per reviewer:

> Read `<review-kit>/FINDINGS.md`, then read `<review-kit>/skills/review-code/SKILL.md` and follow it
> exactly. Also read `<review-kit>/PRACTICES.md` for the rule IDs you will cite.
> Scope: the files changed between `<base>` and HEAD, listed below, plus any file they call
> into. Read each in full.
> Rules, in order of precedence: the repository's `CLAUDE.md`, then its `REVIEW-RULES.md` if
> present (what this team adopted, what they declined, their own `P-` rules), then the kit's
> `PRACTICES.md` as the default. A repository that has adopted nothing is fine — findings then
> carry `rule_id: null`. Never report non-adoption as a finding.
> The manifest at `<D>/manifest.json` has three tiers: read the review tier in full; the
> reference tier is described there and is to be queried with a script, never read — list those
> in `scope.files_queried`. `<D>/probes.json` holds the mechanical checks, already run: treat
> each hit as a place to look, never as a finding, and read the code before filing anything.
> This machine has `<cores>` cores, `<gb>` GB usable, `<gpu or "no GPU">`. Do not run anything
> heavier than that: classify a command before issuing it, never run a heavy one to check
> something, and never substitute a sample for the real run. Record what you deferred and what
> it would need.
> Write your findings JSON to `$STORE/runs/<RUN>/findings-code.json`. Print one line only.
> Do not edit any file. Do not print your findings.
> `<file list>`

…and the same for `review-analysis` → `findings-analysis.json` and `review-docs` →
`findings-docs.json`.

### The blinded fresh-eyes reviewer runs every time

It follows `<review-kit>/skills/review-fresh-eyes/SKILL.md` like the others — name the path in its
prompt. It gets **the code and the data but not the documents**: no README, no reports, no
findings files. Its output goes in the same schema, with `reviewer: "fresh-eyes"`.

This is not the optional luxury it looks like. The other three reviewers are checking work
against a stated intent, so they inherit that intent's framing and can only find errors *within*
it. The blinded one is the only leg that can find an error *of* it — a pipeline that does not do
what everyone believes it does. And the reason it runs on every review rather than at milestones
is simply that agents make errors: three contexts that all read the same README can all be wrong
in the same direction, and nothing else in this design would notice. (`R-2`)

Give it the review-tier file list **minus the documents**, and tell it plainly that
documentation has been withheld on purpose so it does not go looking for it.

### What this audit does not cover

There is no dedicated test-suite reviewer in the four. `review-code` reads test files as code
and may call `review-tests` on one specific question, but nothing in a full audit asks *"does
this suite defend what actually breaks?"* — and a suite can be green, well-formed and still
guard none of the paths where a wrong number gets produced.

Say so in the handover rather than letting a clean audit imply otherwise, and offer
`/review-tests` as a separate run when the repository has a suite worth judging. Adding it as a
fifth reviewer would change what a review costs, which is the user's decision, not this
skill's.

### While they run

Wait. Do not start reading the repository yourself — you would form the opinion the reconciler
is supposed to form independently. When each returns, harvest its file and let the agent end.
(`R-5`)

**Gate every file through the validator before you accept it.** This is not a formality: the
first real run of this kit had one reviewer violate the contract in 9 of 16 findings and
another silently read 57% of its declared scope, and neither was caught until reconciliation.

```bash
for f in $D/findings-*.json; do
  python3 <review-kit>/bin/review.py validate --findings $f --manifest $D/manifest.json
  python3 <review-kit>/bin/review.py fingerprint --findings $f --manifest $D/manifest.json
done

# Scope honesty, only if the hook is wired up. Four reviewers write into one log, so each
# findings file is audited against ITS OWN agent id — without that, every reviewer's reads
# vouch for every other's claims, and `audit` refuses to run rather than pretend otherwise.
python3 <review-kit>/bin/review.py audit --repo . --access-log $STORE/access.jsonl --list-agents
# Match each id to a reviewer by the paths it touched, then, per file:
python3 <review-kit>/bin/review.py audit --findings $D/findings-code.json --repo . \
    --access-log $STORE/access.jsonl --agent <that reviewer's id> --run-dir $D
python3 <review-kit>/bin/review.py cluster --run-dir $D
python3 <review-kit>/bin/review.py match --run-dir $D --prev-run <previous run dir>   # skip on a first review
python3 <review-kit>/bin/review.py coverage --run-dir $D --out $STORE/coverage.json --run-id $RUN
```

`coverage` records, per **(file, reviewer)** pair, who has read what and at which sha256. The
pairing matters: `review-docs` opening a `.py` file for its comments is not `review-code`
having reviewed it, and collapsing the two would let a file be considered covered on the
strength of the wrong reviewer's glance. It also names files that have **never** been reviewed
and how many runs each file is stale by — read that output, and say in the handover if
something has gone several runs without a look.

`validate` fails on missing fields, an unread scope not declared in `not_reviewed`, a heavy
command that was run anyway, and a `certain` confidence resting on reasoning or on a command
that was never executed. `audit` compares `files_read` against the harness-written access log, which is the one check a
reviewer cannot defeat from inside its own context. **Pass `--agent`**: all four reviewers write
into one log concurrently, so without it every reviewer's reads vouch for every other's claims.
`audit` refuses to run on a multi-agent log when it is omitted.

**Pass `--run-dir` too.** The access log is appended to for the life of the repository, so
without a floor on the timestamp a read from an earlier run vouches for a file this one never
opened — and on a second audit of the same repository that covers most of the scope. `--run-dir`
takes the floor from the run's own `manifest.json`, and `audit` refuses to run without either
that or an explicit `--since`.

The ids are the harness's, not names you choose, so `audit --list-agents` prints the ones in
the log together with what each touched; match them to reviewers by those paths. **If the
mapping cannot be established, do not guess it.** Skip `audit` and say in the handover that
scope honesty went unchecked this run — an unchecked claim that is labelled unchecked is a
manageable state, and a reviewer credited with another's reads is not.

A reviewer whose file fails gets **re-run once**, with the validator's output quoted to it. Do
not paper over a failure — a reviewer that quietly skipped its scope produces the most dangerous
artefact this process can make, a clean report over unread code.

**Restore the summary before reconciling** — the reconciler is entitled to the previous run's
conclusions and needs them to judge `carryover`:

```bash
python3 <review-kit>/bin/review.py unshield --repo .
```

## Stage 2 — Reconcile, in a fresh context

Spawn **one** agent, not in parallel with anything:

> Read `<review-kit>/skills/reconcile-reviews/SKILL.md` and follow it exactly. Also read
> `<review-kit>/FINDINGS.md` and `<review-kit>/PRACTICES.md`.
> The findings files are `$STORE/runs/<RUN>/findings-*.json`, already validated and
> fingerprinted. In the same directory: `clusters.json` (corroborated groups, plus proximity
> candidates you must judge by hand), `recurrence.json` (defects also reported last run),
> `manifest.json` (the three-tier scope), `machine.json`, `probes.json` and `repo_profile.json`.
> `$STORE/open_defects.json` carries defects earlier runs confirmed — run `carryover` against
> it before `summarize`, as your skill's Stage 6 sets out.
> Base commit for this review: `<base>`. Run id: `<RUN>`. Suite at `<review-kit>`.
> This repository has `<a test suite / no test suite and no linter config>`; your post-fix
> check is limited accordingly.

It must be **fresh** — not one of the reviewers continued, and not you. A reviewer asked to
reconcile will confirm its own findings, which is the one thing the stage exists to prevent.

It reads the JSON only, never the reviewers' prose. It re-verifies every claim from its
evidence, clusters duplicates, applies only what it confirmed and that is mechanical, runs
whatever check the repository actually provides afterwards, reports the rest, and writes the
summary, the carryover record and the ledger.

## Stage 3 — Hand over

Relay the reconciler's report to the user. Add nothing of your own except:

- What was in scope and what was not, including the rotating slice.
- Which reviewers ran, on which models.
- Any reviewer that failed validation and was re-run, and whether it passed the second time.
- Anything the scope audit flagged as an unsupported `files_read` claim.
- Anything `coverage` reports as never reviewed or badly stale.
  protected or merely requested.
- **Every defect the reconciler classified as `missed`** — confirmed in an earlier run, its file
  reviewed again this run, its source unchanged, and not re-reported. That is a reviewer false
  negative and it is the most important line in the report.
- What was deferred for compute, and what machine would settle it. This is a standing purchase
  decision for the user and it should never be buried in the report's tail.
- Any movement in the ledger's confirmation rates since the last few runs.

Then stop. The fixes are in the working tree, uncommitted; committing is the user's call.
(`G-1`, `A-4`)

## When not to use this

- **On a single file** — run `review-docs` or `review-code` directly. A full audit costs five
  agents and its benefit is disagreement between them, which needs enough surface to disagree
  about.
- **On work in progress.** Review a state someone is prepared to defend. Reviewing a half-built
  branch generates findings about things that were going to change anyway, which trains everyone
  to skim the report.
- **When you already know what is wrong.** Fix it. A review is for finding what you did not
  know, and using it as a to-do list wastes the one thing it is good at.
- **On your own initiative.** Offer; do not start. An unrequested run of this size is a cost
  the user did not agree to, and a report they did not ask for is a report they will not read.

## Cost

Four reviewers plus the reconciler — five agents — and the reconciler's verification
re-runs commands and may re-run an analysis script. This is not a pre-commit hook and not
a background job. The user decides when it is worth spending; your part is to make the offer
cheap to accept and to report honestly afterwards.

If it is finding nothing across several runs, do not relax the checklists — check the ledger
first. A high refutation rate means the reviewers are generating noise; a run with no findings
and honest denominators means the repository is in good shape, and that is a result worth
recording rather than a failure to report.
