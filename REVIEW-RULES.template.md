# Review rules for this repository

Copied here by `<review-kit>/bin/install.py --with-rules`. **It is yours now.** Edit it, prune it,
or delete it — nothing reads it from the review kit, and the kit works without it.

Reviewers read this file when it exists. Where it disagrees with the kit's `PRACTICES.md`, this
file wins; where it is silent, the kit's default applies; where neither covers a defect, the
finding simply carries no rule id, which is normal and expected.

---

## What we have adopted

Delete the lines that are not true of this repository. Being honest here is worth more than being
comprehensive: a rule listed but not followed teaches everyone to skim the list.

| section | adopted? | note |
|---|---|---|
| `A` — agency: who decides what, when an agent may act | | the most opinionated section; keep it only if you mean it |
| `D` — data: immutability, lazy reads, one place for names and paths | | |
| `C` — code: one code path per concept, comment the constraint | | |
| `T` — testing: leak tests, seeds, per-test gating | | |
| `M` — machine: know the budget, flag heavy work rather than attempting it | | |
| `E` — experiments: log everything, re-runnable analysis scripts | | |
| `N` — numbers: cite the script, never quote from memory, right baseline | | usually the last section anyone regrets adopting |
| `R` — review: independence, verify every claim | | |
| `G` — git | | |

## Rules we have deliberately declined

List them with the reason. A reviewer that knows a rule was considered and rejected will stop
raising it, and the next person to wonder gets an answer instead of re-litigating.

- `D-3` — *example:* we use pandas and polars deliberately; the boundary is documented in
  `io/README.md`.

## Our own rules

Use the `P-` namespace. Give each an ID, a statement, and — most importantly — the incident or
reason behind it. A rule with a reason attached survives a deadline; a bare prohibition does not.

**P-1.** *(statement)*
*(why: what went wrong, or what it costs to do otherwise)*

**P-2.**

## Where the real specifics live

Machine names, data locations, submission limits, deploy steps and anything else particular to
this project belong in `CLAUDE.md`, not here. This file is about *standards*; that one is about
*this repository*. Where the two disagree, `CLAUDE.md` wins.
