# Practices

**What this is.** Two things, and it is worth separating them before you read further.

First, it is the **stable vocabulary that review findings cite**. Every rule has a permanent ID,
and a finding that says `N-13` means the same thing in this run as it did three months ago. That
is what lets several independent reviews be merged mechanically instead of by re-reading as many
essays, and what lets the ledger say anything about trends. If you use the review kit at all, you
get this half whether you want it or not — but it costs you nothing, because it is a naming
scheme rather than an instruction.

Second, it is **a default standard for a Python repository built around data analysis and machine
learning, offered for you to adopt in whole, in part, or not at all.** Nothing here is imposed:

- The reviewers work perfectly well against a repository that has adopted none of this. They
  cite a rule wherever one applies, adopted or not, and a finding no rule covers carries
  `rule_id: null` — the contract says so explicitly: *"a finding with no rule is fine and often
  the most interesting kind"*. Either way a finding stands on its evidence, which is what it was
  always required to do.
- **A project's own `CLAUDE.md` takes precedence wherever the two disagree**, and a project adds
  its own rules in the `P-` namespace (`P-1`, `P-2`, …). Your repository wins. Always.
- **A rule can be declined in `CLAUDE.md`, by ID and with the reason.** A reviewer that knows a
  rule was considered and rejected stops raising it, and the next person to wonder gets an
  answer instead of re-litigating it. `CLAUDE.md.snippet` has a section to paste in, and the
  README's Practices section shows it filled in.

**The two halves of the list.** Roughly forty of these rules describe *defects* — leakage,
unseeded splits, a number quoted from memory, deserialising an artefact you did not produce.
Breaking one means something is wrong, and reviewers hunt them whether or not you have adopted
anything. The remaining twenty-odd describe *house style* — one dataframe library, logic in the
package rather than in scripts, who decides what and when an agent may act. Breaking one of those
means you work differently, not that you are wrong. Section `A` is the most opinionated in the
file; some teams find it the most valuable and others will want none of it. Both are reasonable.

**Why the incidents are still here.** Most of these were not designed. They are scar tissue:
someone did the other thing, it cost a day or a result, and the rule is what remains. Where that
is true the incident is named, because a rule with a reason attached survives contact with a
deadline and a bare prohibition does not. Resist the urge to tidy them out — and judge each rule
on its incident rather than on its being written down here.

IDs are permanent: retire a rule by marking it withdrawn, never by renumbering.

| prefix | area |
|---|---|
| `A` | Agency — who decides what |
| `D` | Data |
| `C` | Code |
| `T` | Testing |
| `M` | Machine and compute budget |
| `E` | Experiments |
| `N` | Numbers and how they are reported |
| `R` | Review |
| `G` | Git |

---

## A — Agency

**A-1. The human does the analysis. The agent writes the code that lets them do it.**
Write the query, the table, the chart, the knob at the top of the script that lets them re-run
it another way. Do not write the conclusion. No `Conclusion:` blocks, no `→ therefore` lines.

**A-2. When something that looks like a finding falls out, surface the number and stop.**
Before acting on an inference — changing a feature, editing a findings entry, adding a band to
a threshold — ask.

**A-3. The human's findings file is theirs. Never write in it.** Propose an entry; they
decide. The agent's own inferences go in a separate file that is explicitly not authoritative.

**A-4. Distinguish a request from a question.** When the human is describing a problem, asking
a question, or thinking out loud, the deliverable is an assessment. Report and stop; do not
apply a fix until asked.

**A-5. The agent's own errors get written down in the same place as everyone else's** — in the
document the wrong number appeared in, as a marked correction rather than a silent edit. A
silently corrected number is indistinguishable from a number that was never wrong, which
destroys the only record of how far a conclusion has drifted.

**A-6. An assumption is either checked or labelled.** "I believe the loader drops nulls" and
"the loader drops nulls" are different claims, and the human must be able to tell which one
they are reading. Before asserting what a file contains, what a function does, or what an
argument defaults to, open it. Where checking is genuinely not possible, say "assumed" in the
sentence itself rather than in a caveat at the end.

---

## D — Data

**D-1. Raw input is immutable. Never hand-edit it.** Enforce with filesystem permissions, not
with good intentions.

**D-2. Read large inputs lazily. Never load what does not fit.** Use the scanning or streaming
reader and collect only at the edge. A full read on a memory-constrained box is an OOM kill,
and an OOM kill during a long job looks exactly like a hang.

**D-3. One dataframe library, chosen deliberately, with the exceptions named.** Mixing two
halves your available idioms and doubles the ways a dtype can silently change. Where a library
forces the other one on you, say so at the import site and nowhere else.

**D-4. Raw column names live in exactly one place.** A single mapping module owns the external
spellings; everything downstream uses the internal names. A raw spelling appearing in a second
file means a rename will miss it.

**D-5. One module names every directory.** Nothing else contains a path literal.

**D-6. Times are timezone-aware throughout.** Never construct a naive datetime. Local-time
features go through one module that handles the daylight-saving transition per location.

**D-7. Credentials never enter the repository.** They live outside it, on the machine that
needs them. Directories holding fetched external data are ignored by version control. Assume
the repository will one day be public, because the cost of being wrong about that is
unbounded and the cost of being right is nothing.

**D-8. External sources must be permitted, and the permission recorded** — who confirmed it
and when. When in doubt the answer is no until the human rules.

**D-9. Regenerate, never trust, derived artefacts.** They are outputs, not inputs. Treat any
derived directory as reconstructible and check that it actually is, because the first time you
find out otherwise will be the time it was destroyed.

---

## C — Code

**C-1. Logic lives in the package. Scripts are thin command-line front ends with no logic in
them.** If a script contains something worth testing, it belongs in the package.

**C-2. Type hints on public functions; one command runs lint, format check and tests,** and it
runs before a push. A check that is three commands is a check that gets partially run.

**C-3. Know what invalidates your cache, and put one concept per module.** When the cache key
is a hash of a whole module, editing any block in that file rebuilds every block in it —
including on a docstring change. Whatever the rule is in your project, write it down, because
the failure mode is silent staleness rather than an error.

**C-4. One code path per concept, and a test that asserts it stays one.**
*The scar:* a project had three copies of "apply the label treatment" — the cross-validation
runner had it, the refit script did not, and the prediction script did not. A treated model's
predictions came out bit-identical to its untreated sibling, maximum difference 9.09e-13, and
shipped that way. Every unit test passed, because each test exercised one path. The fix was a
single function all three call, plus a regression test asserting the *structure* — that all
three route through it — and not merely the output.

**C-5. Comment the constraint, not the change.** A comment states something the code cannot
show: why a threshold is 120 seconds, which incident a guard prevents, where a magic number
came from. It does not narrate the next line or justify the diff to a reviewer.

**C-6. Deserialising an artefact is executing it.** `pickle.load`, `yaml.load` without a safe
loader, `torch.load` with weights-only disabled and `joblib.load` all run arbitrary code from
the file. This is the one remote-code-execution surface a data-analysis repository reliably
has, because model artefacts are exactly the thing that gets shared and downloaded. Load only
artefacts you produced, prefer a format that cannot execute, and never deserialise anything
that arrived over a network without a checksum you verified.

**C-7. Write outputs atomically.** Write to a temporary file and rename it into place. A
process killed mid-write leaves a truncated artefact that later reads as valid-but-wrong far
more often than it raises an error, and the downstream failure appears hours later somewhere
unrelated.

---

## T — Testing

**T-1. Gate per test, never per module.** Tests needing raw data or a built intermediate get a
marker each. An earlier version of one project gated whole modules, so a machine with no data
reported a clean run — which is precisely the situation in which the suite must not be green.

**T-2. Every pure helper is unit-tested on a hand-built fixture with the answer worked out by
hand.** A fixture whose expected value was produced by the code under test asserts only that
the code has not changed.

**T-3. Every target-dependent transform gets a leak test.** Corrupt the held-out labels and
assert the transform's outputs do not move. This is the only real defence against the single
easiest way to produce a beautiful, worthless score.

**T-4. Declare the right kind, and recompute target-dependent things per fold.** A transform
that reads the target is target-dependent even if it looks like an aggregate. Caching one
globally leaks the validation period into training and the score will look wonderful.

**T-5. Regression-test the invariants that are not about values** — that all fit paths route
through one function (C-4); that an appended log row lands inside the table rather than after
the prose. Structural invariants are the ones that break silently.

**T-6. A green suite is not a verified change.** See the C-4 scar: every test passed while a
duplicate model shipped. A suite proves that each path does what its test says, never that the
paths are the ones you think they are.

**T-7. Seeds are set and recorded, and a rerun reproduces the number.** Every source of
randomness — split construction, model init, sampling, any library's global state — is seeded
from one place and the seed is written into the run record. A result you cannot reproduce on
demand is not a result, and non-determinism will be discovered at the worst possible moment,
when two numbers disagree and neither can be re-derived.

---

## M — Machine and compute budget

**M-1. Know the machine's budget before you run anything.** Measure it; do not assume it:

```bash
nproc; free -g; df -h .; nvidia-smi -L 2>/dev/null || echo "no GPU"
```

Do this at the start of work that might involve running things, not after something is already
swapping. The check costs seconds. Being wrong about it costs the session.

**M-2. Heavy work runs on the machine provisioned for it, never on the workstation.**
*The scar:* a local box with about 3 GB usable was killed by an agent that ran a full read on
it. An out-of-memory kill during a long job does not announce itself — it looks exactly like a
hang, so the time lost is not the job's runtime but however long it takes someone to notice.

**M-3. Estimate cost from measurement, not from guess.** Where a runtime was recorded, use it.
Where none was, read what the command touches — how much data it reads, whether it fits in
memory, whether it wants a GPU — and classify before issuing it. One queue drained in nine
hours against a twelve-hour estimate and then sat idle, because the estimate was 25 minutes per
arm where the measurement said 8.2.

**M-4. A computation beyond the machine is flagged, not attempted, and not quietly shrunk.**
When something cannot be run here, say three things: what the command is, what it would need,
and that it was therefore not run. Do not run it anyway and hope. Do not silently substitute a
sample, a subset or a smaller configuration and present the result as though it were the real
one — a reduced run answers a different question, and the substitution is invisible downstream.

This matters most when the computation is a *check*. An unverified number that is clearly
labelled unverified is a manageable state: the human can see the gap and decide to spend a big
machine on it. A number assumed correct because verifying it looked expensive is not, and
neither is a dead box.

**M-5. Verify a background job through a fresh connection.** A remote-shell wrapper's exit
status says nothing about the detached job it launched. Reconnect and look.

---

## E — Experiments

**E-1. Every experiment is logged.** No unrecorded "quick test". If it was worth running it is
worth a row, because the quick test is the one whose number ends up quoted three weeks later.

**E-2. A configuration's identity includes the content hashes of its inputs**, so a stale
result can never masquerade as a current one. Already-scored configurations are skipped; an
explicit flag re-runs them.

**E-3. Pre-flight every queue with a dry run** that lists each configuration and whether it
would run or be skipped. Note that "would run" usually cannot distinguish genuinely-unrun from
inputs-have-changed — it is a prompt to look, not an answer.

**E-4. Missing prerequisites must fail loudly, not silently drop work.**
*The scar:* two queued configurations existed but their inputs had never been materialised.
The runner raised on the missing inputs and those arms were simply lost from the queue; they
sat unrun for two days. The fix attempt then scheduled the build *after* the run, and they
failed again. A queue script must either build its prerequisites first or pass the flag that
builds them, and a lost arm must be visible in the queue's exit status.

**E-5. Structure is configuration, not code.** Model specifications nest and a grid expands to
one experiment per combination, each with its own hash, so a partial sweep resumes where it
stopped instead of restarting.

**E-6. Report per-fold as well as pooled.** Folds behave differently enough that pooling hides
the story, and "which fold is harder" is usually the most informative single fact about a
model. Name the hard fold explicitly.

**E-7. An analysis is a numbered, re-runnable script**, steppable cell by cell in an editor
*and* runnable top to bottom, with its knobs in one cell near the top, recording a transcript
so a half-finished session still leaves evidence. Never a heredoc, never an inline one-liner
whose output gets pasted into a document. This is what makes N-1 possible at all.

**E-8. Charts go through one module** that owns the style, the series order and the palette.
Never more than three hues in one frame — facet instead.

**E-9. The shipping configuration is versioned, not working state.**
*The scar:* a mutable list of ensemble members was tracked in version control. A routine
`git checkout -- .` during a cleanup restored a stale 27-entry list over the 5-member
composition behind the shipping result, and the next build would have regressed **with no
error at all**. The versioned copy lives under a version number; the working copy is mutable
and ignored.

---

## N — Numbers and how they are reported

**N-1. Every number in a document cites the script that reproduces it.** Anything
unreproducible goes in no findings file. If a number turns out to have no script, either write
the script or strike the number. A strategically load-bearing figure with nothing behind it
turned out, when the script was finally written, to be wrong.

**N-2. Quote held-out numbers, never in-sample ones.** Like for like, a fine-grained grouping
can be optimistic by a wide margin in sample, and the gap grows with the grouping's
granularity — which is to say, it grows with how clever the feature looks.

**N-3. Report a robust metric and a raw one, and compare models on the robust one.** A tiny
fraction of rows routinely carries half the squared error. Read the raw number when predicting
an external score; expect a gap between the two for this reason alone.

**N-4. A validation delta is not a test delta.** Record the realised transfer ratio after every
structural change; quote a predicted external gain as a range, never a point.

**N-5. Do not apply a delta ratio as a level multiplier.** These are different quantities and
mixing them produces arithmetic impossibilities that are hard to spot because the result still
has plausible units.

**N-6. Diff the artefacts that *should* differ.** Unit tests, gates and leak tests all passed
while the C-4 duplicate shipped. What caught it was comparing two files that were supposed to
be different and finding them identical. Make this a habit, not an inspiration.

**N-7. Price a change against the exact predictions it replaces — the right rows *and* the
right baseline.** Two failures of the same shape:
- *Wrong rows.* A change priced on plausible-looking rows only, and called free; on the actual
  population it was worse.
- *Wrong baseline.* A layer priced against a capped model and judged harmful twice, by two
  independent analyses. Against what it actually replaces it pays substantially. The capped
  model could not predict a large value at all, which inflated its error on exactly the rows
  the change targets.

Ask before measuring, not after: **which predictions does this replace, and what are they
now?**

**N-8. Never condition a population on its realised outcome.** Selecting rows by the thing you
are about to measure guarantees the measurement.

**N-9. State the sample size behind a rule, especially a rule about the tail.** "Well
grounded" and "grounded on two rows" must not be confusable in the prose.

**N-10. In a heterogeneous log, an absent key is not the value `None`.** A run log that gained
fields over time returns `None` for any run predating a field. Reading that as "the treatment
was not applied" once made a correctly-treated shipping member look broken. Check the
configuration hash, which covers the treatment, or check that the number differs from the
untreated sibling's.

**N-11. A claim with no measurement is labelled a hypothesis.** Not a finding, not a "likely",
not an implication folded into a table caption.

**N-12. A chart title says what is plotted, not what it means.** "Standard deviation of
duration, by month" — not "Variance grows in summer". The title is the one place a conclusion
gets asserted without anyone noticing it was asserted.

**N-13. Never quote a number from memory. Re-read it at the moment you write it.**
This is the most common way a wrong number enters a document, and it is almost invisible,
because a recalled value is usually *close* — right order of magnitude, right sign, wrong in
the third digit or wrong by one fold. It survives proofreading and fails only when someone
tries to reproduce it. The rule has no exceptions worth carving out:

- A number computed earlier **in the same session** is still re-read. Intervening work displaces
  it, and the recalled version is the one that feels certain.
- A number quoted from an earlier document is re-read from *that document*, and the document's
  own citation (`N-1`) is followed to the script if the number is load-bearing.
- A number that appears in two places is read twice, not read once and copied — copying is how
  two places come to disagree.
- Re-running the script beats reading the log, and reading the log beats remembering. When the
  cheapest available check is the weakest one, say which one you did.

The tell that this has happened is a number stated with no file, no line and no command
anywhere near it. Treat your own fluency as the warning sign: the more natural it feels to
write the figure without looking, the more certainly you should look.

**N-14. State the precision you actually have.** A recalled or rounded number written to four
significant figures claims a precision that no longer exists. Carry the digits the source
carries, and no more.

---

## R — Review

**R-1. Fresh agents review the work when you ask them to, and independence is structural, not
requested.** Review is called for by a person, not scheduled and not triggered by an agent's
own sense that one is overdue: a trigger that fires on its own either misses the state that
mattered or fires often enough that nobody reads the output.

When one is called for, independence is enforced by construction: separate contexts, no shared
scratch, none seeing another's output. Two on different models, so a model-specific blind spot
shows up as a disagreement rather than as silent agreement. This has caught things nothing else
did.

**R-2. Blinded fresh-eyes first, then adversarial review, then reconciliation — all three,
every time.** Blinded: an agent given the data and the code but *not* the existing documents,
so it cannot anchor on existing conclusions. Adversarial: agents given the documents and told
to find errors, unsupported claims and reasoning gaps. Reconciliation: a fresh agent verifying
every claim before acting on it.

The blinded pass is the one that gets dropped for cost, and dropping it is a mistake. Every
reviewer that has read the documents is checking the work against a stated intent, so it
inherits that intent's framing and can only find errors *within* it. The blinded one is the
only leg that can find an error *of* it — a pipeline that does not do what everyone believes it
does. And it belongs in every run rather than at milestones for the plainest of reasons: agents
make errors, several contexts reading the same document can be wrong in the same direction, and
nothing else in the design would notice.

**R-3. Verify every claim a reviewer makes.** They are wrong a meaningful fraction of the
time, and acting on a wrong review is worse than not reviewing. A review is evidence, not
instruction.

**R-4. A reviewer being right does not make their proposed fix right.** One correctly
identified an unresolved question; the resolution came from changing a component they had not
mentioned, not from the experiment they proposed. Judge the diagnosis and the prescription
separately.

**R-5. Harvest and kill.** Take the reviewer's output, then end it. They are expensive and
they hold memory.

**R-6. Read the findings files before proposing an idea.** Several obvious ones are already
tested and rejected, with numbers.

---

## G — Git

**G-1. Commit or push only when asked** — except that results, scripts and documents are
pushed promptly when another machine pulls them.

**G-2. Never amend or force-push a commit another machine may have pulled.** It creates a
divergence that takes longer to untangle than the tidy history is worth.

**G-3. Never `git stash -u`.** It has destroyed untracked model artefacts that cost hours to
regenerate.

**G-4. Never `git checkout -- .` as a cleanup.** It has restored a stale tracked file over
live configuration. See E-9.

**G-5. Untracked files on a remote machine block a rebase.** Back them up to a dated
directory, clean, rebase, then push the orphans. One machine could not pull for fourteen
hours, so a patch went across by `scp`, which silently missed a second fix.

**G-6. Check that every machine is at the same commit before starting a long job.** The
pre-flight is what catches the fix that never landed.

---

## Mechanise what you can

A rule that can be a check should not be prose. Prose rules get violated by tired people and by
agents under context pressure; a failing test does not. Good candidates:

| rule | check |
|---|---|
| `D-1` raw input immutable | read-only permissions, plus a pre-commit hook |
| `D-5` one module names every directory | a test grepping for path literals elsewhere |
| `N-1` every number cites a script | a checker over `reports/*.md` wired into `make check` |
| `C-4` one code path per concept | a structural regression test, not three output tests |
| `T-7` seeds reproduce | a test running a seeded operation twice |
| `M-4` heavy work is flagged, not attempted | a runtime field in the run log, so cost is read not guessed |

Every rule you mechanise is one the review no longer has to spend attention on.
