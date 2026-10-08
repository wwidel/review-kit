# TODO

Open work that could not be done from the machine the test suite was written on. Each section
says why it matters, what to do, and what counts as done. Nothing here needs this repository's
history beyond what is committed.

Before anything else, on the new machine:

```bash
git clone https://github.com/wwidel/review-kit.git && cd review-kit && git checkout test-suite
python3 -m pip install pytest
python3 -m pytest tests -q                    # expect all to pass; ~20 s on an idle machine
```

---

## 1. Capture real hook payloads

### Why

`bin/log_file_access.py` is a Claude Code `PostToolUse` hook. Everything `review.py audit`
concludes rests on what it logs, and what it logs rests on the JSON Claude Code pipes to it on
stdin. The hook reads these fields:

| field | used for |
|---|---|
| `tool_name` | which tools are logged (Read, Bash, Grep, Glob, Edit, Write, NotebookEdit) |
| `tool_input.file_path`, `.path`, `.notebook_path`, `.command` | the paths, or the shell command to parse |
| `cwd` | where relative paths and `cd` start from, and which repository's store to write to |
| `agent_id`, falling back to `session_id` | which reviewer a record belongs to; `audit --agent` filters on it |

Every test in `tests/` feeds the hook payloads written by hand from those assumptions. If
Claude Code sends something different, all tests still pass and the live log silently degrades:

- no `agent_id` for subagents → every record carries the session id, `audit` cannot tell the
  four reviewers apart and refuses to run without `--agent`, or attributes reads wrongly;
- no `cwd` → relative paths resolve against the hook process's own directory, and after a
  `cd` reads may land in no store at all;
- a renamed tool or a new key in `tool_input` → those reads are never logged, and honest
  reviewers are reported as UNSUPPORTED CLAIMs.

What is already known: on Claude Code 2.1.288 a subagent's records carried its own `agent_id`,
and the hook ran in the session's current directory (seen in the Solution E run's access log and
transcript). That is one version and one field.

### What to do

No global settings are needed: a project-level hook in a scratch repository is enough.

1. Make a scratch repository:

   ```bash
   mkdir -p /tmp/payload-capture/src && cd /tmp/payload-capture && git init -q
   printf 'x = 1\n' > a.py && printf 'y = 2\n' > src/m.py
   printf '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}\n' > nb.ipynb
   git add -A && git -c user.email=t@t -c user.name=t commit -qm init
   ```

2. Add `.claude/settings.json` there with a capture hook that appends each payload, one per
   line, to a file outside the repository:

   ```json
   {"hooks": {"PostToolUse": [{"matcher": "Read|Bash|Grep|Glob|Edit|Write|NotebookEdit",
     "hooks": [{"type": "command",
                "command": "python3 -c \"import sys; open('/tmp/payloads.jsonl','a').write(sys.stdin.read().strip()+'\\n')\""}]}]}}
   ```

3. Start a **fresh** Claude Code session in `/tmp/payload-capture` (a hook added mid-session
   may not fire until restart) and ask it to:
   - Read `a.py` itself (main session);
   - spawn one subagent that: runs `cd src`, Reads `m.py` by relative path, runs `cat m.py`,
     Greps for `y`, Globs `*.py`, Edits `m.py`, Writes a new file, and edits `nb.ipynb` with
     NotebookEdit;
   - spawn a second subagent that Reads `a.py` (to confirm two subagents get two ids).

   `claude -p "<the same instructions>"` in that directory works too, if an interactive session
   is inconvenient.

4. Inspect `/tmp/payloads.jsonl`: one payload per tool call. Record the Claude Code version
   (`claude --version`).

5. Sanitise before committing anything:
   - replace the scratch path, home directory, session ids, agent ids and `transcript_path`
     with placeholders (`{repo}`, `{home}`, `session-1`, `agent-1`, `agent-2`);
   - **delete `tool_response` entirely**: it holds file contents and command output;
   - keep every other key, including ones the hook does not use — they are what a drift check
     compares.

6. Commit them as `tests/fixtures/payloads/<claude-version>/<tool>-<context>.json` (for
   example `2.1.288/bash-subagent-after-cd.json`), and add `tests/test_hook_payloads.py`:
   - for each fixture, substitute `{repo}` with a temporary installed repository (the
     `installed` fixture in `conftest.py`), feed it to the real hook with `conftest.hook`, and
     assert the record: the right absolute paths, the right agent id, `cmd`/`cwd`/`reads` on
     Bash records, nothing logged for Grep/Glob `path`;
   - assert the two subagents' ids differ from each other and from the main session's;
   - assert the relative Read and the `cat` after `cd src` both resolve to `src/m.py`;
   - feed the subagent's records to `audit` with a findings file declaring `src/m.py` and
     `a.py`, and assert the exit codes (0 for the subagent that read them; Write/Edit records
     must not count).

7. Add a drift check: a small script (`tests/check_payload_drift.py`, not part of the suite)
   that takes a fresh `/tmp/payloads.jsonl` and prints any key present in one but not the
   other, compared with the committed fixtures. Re-run steps 3–4 and this script after each
   Claude Code upgrade; commit a new version directory when anything changed.

8. Remove the scratch repository and `/tmp/payloads.jsonl`.

### Done when

Fixtures for one named Claude Code version are committed, `test_hook_payloads.py` passes, the
drift script exists, and `tests/README.md` says to run it after upgrading Claude Code.

---

## 2. Find out whether the kit is worth using

### The question

Is a kit audit better than the obvious cheap alternative — two fresh agents told to
"thoroughly and meticulously review the contents of this repository"? Nobody has measured it.
Every figure in DESIGN.md compares the kit with itself. The kit costs roughly 1M tokens per
audit of a few thousand lines, plus an install and ~3,650 lines of bookkeeping to maintain; two
plain agents might cost 200–400k (a guess, to be measured here). If the kit does not buy
something definite for that, teammates should not be asked to use it.

What it plausibly buys, each to be tested rather than assumed:

1. **verified output** — the reconciler re-runs each finding's evidence, so what reaches the
   reader is fewer false claims (the one advantage with any measurement: it refuted 10 of 10
   planted false findings);
2. **checked coverage** — declared `files_read`, audited against the hook log, so the report
   says what was not read;
3. **a blinded reviewer** — `review-fresh-eyes` never sees the documentation, and may catch
   framing errors the documented intent would talk a normal reviewer out of;
4. **data-science checklists** — leakage, seeds, CV protocol, numbers quoted from memory;
5. **memory across runs** — `carryover` flags regressions and misses, `coverage` rotates through
   unread files.

Against it: two kit runs on one unchanged repository disagreed on up to 43% of confirmed
defects, so its recall varies a lot, and the 4-leg design may cost more than it finds.

### Ground truth

Using the kit's own earlier findings as the answer key would be circular. Use two sources:

- **Planted defects.** Before any arm runs, someone other than the arms (a separate session,
  or a person) plants 8–12 known defects in each test repository on a branch, and writes them
  to a sealed key **outside** the repository copies the arms see. Mix kinds, each of which the
  kit's checklists claim to target and a generic reviewer could plausibly miss:
  - leakage: a scaler, encoder or imputer fitted before the train/validation split;
  - an unseeded split or model, and a seed set after the randomness it should govern;
  - an off-by-one in a fold or ensemble loop (cf. the Solution D `range(i, …)`);
  - a positional join of predictions to IDs with no alignment check;
  - a hyperparameter search that scores a fixed configuration;
  - a README number that no script reproduces, and one quoted with a different value from
    what the script prints;
  - a stale doc claim (a renamed flag or file still documented);
  - a silent dtype or index corruption (`pd.concat` without `ignore_index`, int → float NaN);
  - one framing error: the documentation and code agree, and both are wrong.

  Recall on planted defects is the cleanest number this experiment produces.
- **Adjudicated real defects.** Pool every finding from every arm on the unmodified original
  code, deduplicate, and have each pooled finding judged true/false by an adjudicator who does
  not know which arm produced it (strip arm-specific formatting first). The union of true ones
  is the reference set for "real defects found". Unique true findings per arm show what each
  method adds that the others miss.

### Corpus

The four public repositories in KAGGLE-RESULTS.md, cloned fresh at their upstream HEAD — **not**
the local copies that ran the kit, which may hold a `REVIEW-SUMMARY.md` and applied fixes. Check
that no `REVIEW-SUMMARY*.md`, `.review/` or `CLAUDE.md` mentioning reviews exists in any copy.

A first pass on two of them (Solution C, ~2.9k lines; Solution E, ~4.8k lines) is enough to decide
whether the full run is worth its cost.

### Arms

Same model for every agent in every arm, each in a fresh session, prompts fixed in advance and
committed alongside the results.

| arm | what runs |
|---|---|
| **A** | two fresh agents, independently, prompt: "Thoroughly and meticulously review the contents of this repository. Report every defect you find, with the file and line." Report = union of the two. |
| **B** | A, then one fresh verifier agent: "Here are findings from two reviewers. For each, check it against the code — re-run or re-read as needed — and keep only those you can confirm." |
| **C** | the kit: `/repo-audit --full`. |
| **D1** | the kit without `review-fresh-eyes` (three legs + reconciler). |
| **D2** | one kit leg (`review-code`) + the reconciler — the cheapest version that keeps verification. |

Run each arm **twice** per repository: the kit's measured run-to-run variance is large enough
that one run per arm cannot separate the arms. A first pass can run A, B and C once each.

The kit legs and arm B do not see each other's output. Planted-defect runs and original-code
runs are separate sessions on separate checkouts.

### What to measure, per arm per run

- **tokens** (from the session totals) and wall-clock time;
- **planted recall**: planted defects found / planted;
- **real recall**: true pooled findings this arm reported / all true pooled findings;
- **precision**: true / reported (adjudicated);
- **unique true findings**: true findings no other arm reported;
- **human verification time**: minutes for a person to check the report's top 10 findings well
  enough to act on them (time it, at least for one repository) — the kit's claimed advantage is
  that its output needs less of this;
- **coverage honesty** (kit arms only): did `audit` pass; how many files did each leg declare
  not reviewed. For A and B, ask each agent at the end which files it did not read in full, and
  check that against its transcript.

Record everything in `experiments/<date>/results.csv`, one row per (repository, arm, run), and
keep the raw reports next to it.

### Reading the result

Decide these thresholds before running, and write them here:

- If C's real and planted recall are within noise of B's at more than twice B's cost, the
  kit's value is verification and memory, not finding; ship D2 as the default mode and keep C
  for first audits.
- If B's precision is close to C's, the reconciler protocol is not adding much over a plain
  verifier prompt; simplify it.
- If D1 matches C, the blinded leg is not earning its ~25% of the cost; make it optional —
  unless its unique true findings are the framing kind no other arm finds.
- If C clearly beats B on recall or precision per token, the README can say so, with the
  numbers, and the pitch to teammates writes itself.

### Cross-run memory (a separate, later test)

Only matters for repositories audited repeatedly. On one repository: run C, fix half the
confirmed defects in a commit, introduce two regressions (re-break two earlier fixes), run C
again. Score `carryover`: are the fixed ones `plausibly-fixed`, the regressions `regressed`,
and the untouched ones `re-reported` or `missed`? Compare with what a second run of A says
about the same commit, given A's first report.

### Cost

Roughly: kit ≈ 700k + 140 × reviewable lines per audit (very rough; see README); A and B are
unknown, guess 0.3–0.6M. The first pass (two repositories; A, B, C once each, on planted
branches) is about 4–5M tokens. The full design (four repositories × five arms × two runs,
planted and original) is in the region of 40–50M. Run the first pass, then decide.

---

## 3. Smaller open items

- **CI on macOS.** `.github/workflows/tests.yml` runs on Ubuntu and macOS with Python 3.10 and
  3.14, and has not yet run. If macOS passes, update README's WARNING ("Tested on Linux and WSL
  only") to say what CI covers; if it fails, fix or document.
- **The 27,398-line repository.** DESIGN.md says `estimate` priced a 27,398-line repository at
  1.85M before a run that exhausted a weekly limit. README's WARNING lists a private 20,259-line
  repository whose estimate was ~1.8M and whose full audit cost ~3.5M. If these are the same
  repository (27,398 total lines, 20,259 reviewable), say so in DESIGN.md.
- **Release live check.** `tests/README.md` describes a five-minute manual check of the hook in
  a real session. It has not been run against the rewritten hook (the walk-up from the payload
  `cwd`, the new parser). Run it once before teammates install.
- **Merge.** All of this is on branch `test-suite`; merge to `main` once the suite passes on the
  new machine and CI.
