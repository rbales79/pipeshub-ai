# `downstream/base` — the carry series

This branch is **the pin plus the patches we carry**, as git commits. It is not a place to
develop: it is rebased onto upstream tags, and every commit on it is classified in its subject.

```
main             tracks upstream. never committed to.
downstream/base  this branch — the pin + carries
fix/<topic>      PR branches to upstream, cut from main
```

## Commit convention

```
DOWNSTREAM: <carry>:     permanent — incompatible with upstream by design
DOWNSTREAM: <pr-NNNN>:   filed upstream; DROP when that PR merges
DOWNSTREAM: <drop>:      believed removable; prove it at the next rebase
```

With DEP-3 trailers in the body, so `git log --grep` is the whole report:

```
Forwarded:        a PR url, or `no` / `not-needed`
Bug-Downstream:   the tracking issue
Applied-Upstream: empty = still carried. filled = DELETE AT THE NEXT REBASE.
Last-Update:      ISO date
```

**A commit may not be added without a `Forwarded:` value.** `no` and `not-needed` are legitimate
answers; *absent* is not. It costs nothing at the moment of writing, which is the only moment the
author knows.

## Where the rest lives

| | |
|---|---|
| Issues, CI, the requirements register | `A-Few-Good-Gits/knowledge-forge` (private) |
| Our contributions | PRs against `pipeshub-ai/pipeshub-ai` |
| This repo | **code only** — this branch and `fix/*`. Issues are disabled on purpose |

Issues are disabled here deliberately. This fork is public (it forks a public repo), and the
work's issues carry internal detail — bench topology, account names, findings not yet upstreamed.
GitHub itself refuses to transfer issues from a private repo to a public one, and that guard is
right. **The fork is a build input and a place to cut PR branches from, not a discussion venue.**

## Conversion status

Converting a patch to a commit is **re-authoring it against source**, not a mechanical
transform: the patch scripts are deployment tooling that `docker exec`s into a running container
and edits compiled output. Several target `backend/dist/**` JavaScript, which has no meaning in
this repo — the equivalent change has to be written against the TypeScript.

**Tranche 1 landed 2026-09-19: 24 of 43 scripts, as the 24 commits below this one.** They are the
pure-Python patches; each applied to this tree unchanged, every changed file compiles, and a second
pass of all 24 reports "already patched" and changes nothing. Converted by
`bench/patches/to-commits.py` in knowledge-forge, which is re-runnable from the pin.

**What is deliberately NOT here, and why it is not a silent gap:**

| | Count | |
|---|---|---|
| Compiled JavaScript only | 11 | `01`–`01f`, `02`, `07`, `17`, `18`, `35` — re-author in TypeScript |
| **Mixed** — edits Python *and* compiled JS | 4 | `06`, `08`, `11`, `19`. Splitting these is the next unit of work |
| **Blocked** by one of the mixed four | 4 | `09`, `10`, `26` anchor on text `08` inserts; `31` on text `06` inserts |

The blocked four are not separate work: splitting the mixed four lands them too, taking this
branch from 24 to 32 of 43 without re-authoring a single line of TypeScript.

`31` is worth naming. It applies in three parts; `31A` and `31B` write successfully and `31C`'s
anchor does not exist upstream at all — patch `06` creates it. As a script that leaves two files
edited and one not, which is the half-applied state the series exists to prevent. The converter
reverts it rather than committing two thirds of a patch. An earlier audit read the same symptom as
evidence that the base image is not the pinned source (`knowledge-forge#167`); it is not, it is an
ordering dependency inside our own series.

Tracked in `knowledge-forge#36`.
