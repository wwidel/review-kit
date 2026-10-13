# The findings contract

Every reviewer in this kit emits one JSON file in this exact shape. The reconciler reads
nothing else from them — no prose report, no narrative, no reasoning transcript. This is
deliberate: prose carries the reviewer's confidence into the reconciler's judgement, which is
the anchoring that independent review exists to prevent.

Write the file to the path the orchestrator gives you and print **only** a one-line summary to
your own output.

## Schema

```json
{
  "reviewer": "review-code",
  "run_id": "2026-09-28T14-03-11Z",
  "scope": {
    "mode": "since-last-review",
    "base": "a1b2c3d",
    "files_in_scope": 23,
    "files_read": ["src/pkg/cv.py", "src/pkg/refit.py", "…every review-tier path you opened"],
    "files_queried": ["data/train.csv", "…reference-tier files you aggregated with a script"]
  },
  "stack": {
    "language": "python",
    "detected": ["polars", "lightgbm", "pytest", "ruff"]
  },
  "machine": {
    "cores": 4,
    "usable_gb": 3,
    "gpu": null,
    "free_disk_gb": 42,
    "heavy_work_goes_to": "the provisioned box named in the project's CLAUDE.md, if any"
  },
  "sections_skipped": [
    { "section": "web-security", "reason": "no HTTP server, no request handlers in repo" }
  ],
  "denominators": {
    "functions_scanned": 140,
    "scripts_scanned": 18,
    "numbers_checked": 0,
    "numbers_deferred": 0
  },
  "not_reviewed": [
    "src/pkg/legacy/tuning.py — out of the scope window, not read",
    { "path": "src/pkg/legacy/sweep.py", "reason": "out of the scope window, not read" },
    "reports/Q3.pptx — opaque; any figures in it are unchecked"
  ],
  "findings": [
    {
      "id": "review-code-001",
      "rule_id": "C-4",
      "file": "src/pkg/refit.py",
      "line": 88,
      "lines": "88-104",
      "claim": "refit_predict applies the label transform inline instead of calling cv.fit_and_predict, so a change to the transform reaches two of the three fit paths.",
      "class": "defect",
      "evidence": {
        "kind": "command",
        "command": "grep -n 'np.log1p' src/pkg/*.py",
        "expected": "three separate call sites in cv.py, refit.py, submit.py",
        "cost": "cheap",
        "ran": true
      },
      "severity": "critical",
      "confidence": "certain",
      "blast_radius": "every refitted prediction since the transform was introduced",
      "fix": "delete the inline transform in refit.py:88-104 and call cv.fit_and_predict",
      "fix_certainty": "judgment"
    }
  ]
}
```

A fifth file in the same shape, `findings-static.json`, is written by `bin/detect.py` rather
than by an agent: `reviewer` is `static`, every finding is `likely` with the quoted site as
evidence, and two extra fields, `detector` and `detector_kind`, name the check that fired.

## Field rules

**`rule_id`** — the rule this violates, or `null`. Three sources, in precedence order: a `P-`
rule from the repository's own `REVIEW-RULES.md`, a rule from the kit's `PRACTICES.md`, or
nothing. A finding with no rule is fine and often the most interesting kind; do not invent a rule
to fill the field, and never raise a finding *because* a repository has not adopted the kit's
defaults — adoption is optional by design.

**`scope.files_read` vs `scope.files_queried`** — the manifest sorts every file into one of
three dispositions, and your scope fields must match.

- **Review tier** — code and prose, read in full. List them in `files_read`.
- **Reference tier, `kind: "tabular"`** — data files and anything too long to read. The manifest
  carries each one's size, columns and first row; you interrogate them with a script rather than
  pulling them into context. List them in `files_queried`. Putting one in `files_read` fails
  validation — not to be pedantic, but because a run of this kit once declared 274,025 lines of
  CSV as "read", which is either untrue or ruinously expensive and there is no third possibility.
- **Reference tier, `kind: "opaque"`** — decks, PDFs, spreadsheets, images, archives and model
  artefacts. **Do not attempt to read or convert these.** They belong in neither scope list.
  What they need instead is a line in `not_reviewed`, because a deck or a PDF is exactly where an
  uncited number hides, and the honest statement "`reports/Q3.pptx` was not read; any figures in
  it are unchecked" is worth more than silence. A directory of committed `.pkl` files is itself
  worth remarking on (`C-6`).

**`class`** — is something *wrong*, or merely untidy? This is the field that decides whether a
finding can ever be compacted away, so get it right.

- `defect` — an error, a contradiction, an inconsistency, or a violation of a stated rule.
  Something in the repository is not what it should be. **A `defect` is always reported in
  full, at any severity.** Never suppressed, never summarised into a count, never dropped for
  being small. A one-character typo that makes a documented command fail is a `defect`.
- `hygiene` — nothing is wrong, but the code is harder to own than it needs to be: an opaque
  block, a magic number with no provenance, dead code, a missing comment on a constraint.
  These are real and worth filing; they are also the ones that may be presented compactly.

When you cannot decide, it is a `defect`. The asymmetry is deliberate: an over-reported
hygiene item costs a table row, a suppressed defect costs a result.

**`claim`** — one sentence stating the defect. Not the fix, not the risk, not a question. If
you cannot state it in one sentence you have not finished diagnosing it.

**`evidence`** — the single most important field, and the reason this kit works. Three kinds,
in descending order of worth:

| kind | contents | reconciler does |
|---|---|---|
| `command` | a shell command plus what the reader should see | re-runs it, if `cost` is `cheap` |
| `quote` | verbatim source text plus its file and line | re-reads those lines |
| `reasoning` | an argument, when neither of the above is possible | re-derives it, and caps confidence |

A finding whose evidence is `reasoning` is **automatically capped at `likely`**, never
`certain`, no matter how sure you are. Prefer `command` whenever a command exists — a grep that
returns three hits where there should be one is worth more than a paragraph.

Do not fabricate a command you did not run. The reconciler runs it, and a command that does not
reproduce what you claimed refutes the finding outright.

**`cost`** and **`ran`** — on `command` evidence, whether the command is affordable on this
machine, and whether you actually ran it.

- `cheap` — seconds to a couple of minutes, comfortably inside the measured budget in
  `machine`. Run it. `ran: true`.
- `heavy` — needs a GPU this machine lacks, more memory than it has, or many minutes of many
  cores. **Do not run it.** Set `ran: false` and add `cost_basis` saying what it would need and
  how you know. (`M-3`, `M-4`)

A `heavy` command is still worth recording — it tells the reconciler and the human exactly what
to run on a bigger machine, which is far more useful than an unexplained gap. What is not
acceptable is running it anyway, or silently running a shrunken version on a sample and
reporting the result as though it were the real one. A reduced run answers a different question.

When you set `ran: false`, the finding's `confidence` is capped at `likely`, exactly as for
`reasoning` evidence — you have a hypothesis about what the command would show, not a result.

**`severity`** — *if the claim is true*, how bad is it.

- `critical` — a wrong number ships, data is lost, or a result is invalid. Fix before anything
  else.
- `major` — a real defect with bounded damage, or a rule violation that will produce a critical
  one later.
- `minor` — a real but bounded problem: a bad error message, an unclamped edge case that
  cannot currently be reached, a misleading name on something load-bearing.
- `trivial` — true, verified, and almost certainly not worth anyone's afternoon: a redundant
  assignment, an unused import, a docstring typo. **`trivial` changes how a finding is
  *presented*, never whether it is reported** — a `defect` at `trivial` severity still appears
  in the summary, in the compact table.

**`confidence`** — *how sure you are the claim is true*. Orthogonal to severity, and conflating
the two is the most common reviewer error. A speculative critical and a certain minor are both
normal and get handled completely differently.

- `certain` — the evidence settles it. You ran the command; the lines say what you say they say.
- `likely` — the evidence strongly indicates it but a reading exists in which it is fine.
- `speculative` — worth a look, not worth asserting. Use freely; the reconciler filters, and a
  speculative finding costs a line whereas a missed one costs a result.

**`blast_radius`** — what is already wrong if the claim is true. "Every refitted prediction
since March" is what turns a finding into a priority. Omit if genuinely nothing downstream.

**`fix`** — one concrete action. Not "consider refactoring"; a named change to a named place.
A finding is not finished until it has a handle someone could pick up.

**`fingerprint`**, **`fingerprint_anchor`**, **`fingerprint_anchor_line`**, **`fingerprint_line`** — do not write these by
hand. `bin/review.py fingerprint` stamps them from the file path and a normalised snippet of the
source the finding points at, so the same defect gets the same id across reruns even when line
numbers move, and a defect that was actually fixed stops matching. The *anchor text* is stored
alongside the hash, not merely the hash: a hash cannot be reversed, and without the text a later
run cannot ask the one question that matters — is this defect still here, or did someone fix it?
Two anchors are stored, and the difference matters: the two-line window gives the fingerprint its
uniqueness, while the single offending line is what a later run tests for presence. Testing the
pair meant that editing the *next*, unrelated line made the anchor absent and a live defect was
recorded as fixed. That is what lets a later run distinguish "fixed" from "nobody looked" from
"looked and missed it."

**`fix_certainty`**:

- `mechanical` — the fix is unambiguous and local, and any competent reader would write the
  same one. A typo, a wrong variable, a missing `seed=`, an unclosed file, a broken link. Only
  these are the ones a report can call out as cheap to clear. Nothing in this kit applies
  them for you — the reconciler edits no files.
- `judgment` — the right fix depends on intent, or there is more than one defensible option, or
  it touches an interface. Reported to the human, never applied.

When in doubt it is `judgment`. Practice R-4: being right about the problem does not make you
right about the fix.

## Denominators

`denominators` and `not_reviewed` exist so that findings can be read as a *rate* rather than a
count, and so that one review can be compared against the last. Three findings in 140
functions is a different report from three findings in 12, and a reviewer that quietly read a
third of its scope must say so rather than producing a clean-looking report.

Use whichever keys fit your remit — `functions_scanned`, `scripts_scanned`, `blocks_scanned`,
`opaque_blocks`, `opaque_weighted`, `numbers_checked`, `numbers_deferred`, `tests_collected`.
The set is open; what matters is that a count always arrives with what it is a count *of*.

**One entry per file, and each entry leads with the path.** Either `"path/to/file.py — why"`
or `{"path": "...", "reason": "..."}`. A directory does not declare the files under it, and
prose that merely mentions a filename does not declare it either: `validate` checks each
unread in-scope path against the heads of these entries and fails the file otherwise. The rule
is strict because the loose version let "I looked at the legacy package, honest" stand in for
coverage of eleven files.

Be honest in `not_reviewed`, and put anything you could not check for budget reasons there too,
naming the command and what it would need. An admitted gap is useful; a silent one poisons the
trend.

## What not to put in the file

- No summary section, no counts, no prose report. The reconciler computes those.
- No positive findings ("✅ error handling looks good"). They are noise in a merge and they are
  what `denominators` is for.
- No duplicates of the same defect at different lines. One finding, with `lines` spanning it.
- No finding you could not state a `claim` for. Practice N-11: an unmeasured claim is a
  hypothesis — if it is a hypothesis, mark it `speculative` and say so in the claim itself.
