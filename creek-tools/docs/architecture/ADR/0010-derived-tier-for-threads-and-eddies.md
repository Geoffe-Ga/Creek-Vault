# ADR-0010: A thread's or eddy's privacy tier is derived from its members

- **Status**: Accepted
- **Date**: 2026-08-10
- **Driving issues**: #1284 (voice-skill tree), following #969
  (`creek.state.render`) and #971 / PR #1286 (the fragment half of the same
  gate). Conceptual sibling: #931 / PR #1283 (ancestry titles in the compile
  prompt).

## Context

`Thread` and `Eddy` (`creek/models.py`) carry **no `privacy_tier` field**.
Every other tiered surface in the pipeline reads a tier off the note it is
about; these two have nothing to read.

They are not, however, tier-free content. Their titles are computed from their
member fragments (`creek/link/naming.py::cluster_title`, reached from
`creek/link/threads.py` and `creek/link/eddies.py`), as are their descriptions
and their member lists. An eddy whose members are `personal` produces a title
carrying that content's vocabulary. Two generators render those titles into
artifacts a caller can hold:

- `creek/generate/state.py` — the `creek state` audit report (#969).
- `creek/generate/skills.py` — the voice-skill tree, where the title
  *slugified* is the SKILL **filename**, which `creek.skills.refresh` returns
  to its caller in `skill_paths` (#1284).

So a decision was unavoidable, and it changes which files the tree emits. Three
answers were available, all defensible:

1. **Fail closed on the note.** `within_ceiling` on the raw frontmatter; no
   `privacy_tier` key means `INTIMATE`. This is the shape #968 used for the
   report generators, and it is the cheapest to reason about.
2. **Exclude the categories wholesale above some ceiling.** Simple, coarse.
3. **Derive the tier from the members**, the way the title itself is derived.

## Decision

**Option 3. A thread or eddy ranks at the maximum tier of every fragment whose
`threads` / `eddies` wikilinks name its title; the empty set reduces to
`INTIMATE`.**

Because the reduction (`max_source_tier`) and the cutoff
(`tier_within_override`) both rank by `tier_sensitivity`, that is equivalent to
the sentence worth quoting:

> A thread or eddy is admitted only when **every fragment naming it** would
> itself be admitted at this ceiling. One that no fragment names is admitted
> only at `ceiling=intimate` / `all`.

Two properties of the implementation are load-bearing rather than incidental:

- **The reduction runs over the unfiltered corpus.** Narrowing the fragments
  first and deriving second would resolve an eddy with one `open` and one
  `intimate` member to `open`. That is leak (3) of the three #969 reproduced.
- **The reduction is shared, not re-derived.** `derived_link_tiers`,
  `wikilink_targets` and `admit_by_derived_tier` live in
  `creek/generate/state_tiers.py` and both generators call them. An eddy the
  state report withholds and the skill tree emits is not a difference of
  opinion between two modules; it is a leak.

What each caller *does* choose independently is the per-fragment tier reader it
feeds in. `creek/generate/state.py` uses `raw_privacy_tier` (a missing
`privacy_tier` key fails closed to `INTIMATE`); `creek/generate/skills.py` uses
`tier_of` (a missing key is the model's `UNCLASSIFIED`, which ranks with
`PERSONAL` per #876). Each generator ranks a thread's members with the same
reader it already uses for the thread's own fragments — a generator that ranked
a fragment one way and the eddy that fragment names another way would be
incoherent with itself, which is worse than differing from its sibling. The
divergence is pinned by
`tests/test_skills_tier_ceiling.py::test_a_thread_whose_members_are_keyless_is_admitted_at_the_personal_ceiling`.

## Consequences

This only ever emits **less**. Nothing that was withheld before is emitted now.

- At `ceiling=open`, a thread with a single `personal` member disappears.
- On a vault that has never been through `creek classify`, every thread and
  eddy disappears at `ceiling=open`, because an untiered fragment ranks with
  `personal`. `--include-tier personal` recovers them.
- An **orphaned** thread or eddy — one no fragment links back to, which a
  partial or interrupted `creek link` run leaves behind — has no evidence at
  all and is withheld below `ceiling=intimate`. This is the sharpest cost of
  the decision and the most likely to surprise an operator.
- An admitted eddy's rendered "Member threads" line is intersected with the
  threads the same snapshot admitted. Gating the walk alone would have closed
  the front door and left this one open, since an eddy admitted on its members
  can name a thread withheld on *its* members. Withheld entries are dropped
  rather than counted: a `"+2 withheld"` marker would restore the cardinality
  oracle the gate exists to remove.
- `skill_count` no longer moves with above-ceiling thread or eddy cardinality.

Every exclusion is recoverable by widening the ceiling, and
`_log_ceiling_withheld` already names the remedy, so none of it is silent.

Options 1 and 2 were rejected for the same reason: both empty two whole
categories at every ceiling below `intimate`, on a fully-classified all-`open`
vault where nothing is actually sensitive. That is an outage, not a gate.
Option 3 costs one extra pass over the fragment corpus, which the skill
generator already walks once.

## Revisit predicate

Revisit this ADR when **`Thread` and `Eddy` gain a real `privacy_tier` field
stamped at link time**. At that point:

- the derivation becomes a migration fallback for un-migrated vaults rather
  than the rule;
- the orphan case stops meaning "no evidence" and starts meaning "the note says
  so", which removes this decision's sharpest cost;
- `admit_by_derived_tier` should shrink to `within_ceiling` on the note's own
  frontmatter, and the two callers' reader divergence above disappears with it.

Until then, do **not** narrow the derivation to the already-admitted corpus to
save the extra read — that reintroduces the mixed-member leak this closed.
