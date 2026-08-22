# ADR-0008: Bounding cluster degeneration in message streams — segment and rename first, cap only as a guardrail

- **Status**: Accepted
- **Date**: 2026-08-08
- **Driving issue**: #880 (eddy/thread mega-cluster on the demo vault). Related: #857 (O(n²) thread pairing), #790 (DBSCAN performance + brute-force equivalence contract), #718 (content-stable thread ids), #730 (wiki-link aliases).

## Context

On the demo vault `creek-demo-2026-06-07` — 35,330 fragments, 30,795 of them
Discord messages — both detectors collapsed the message corpus into a single
page:

- `link --method eddies` emitted 17 eddies, one of which
  (`03-Eddies/2023-03-30-Messages.md`) had `fragment_count: 30795` — **every
  message in the vault**, ~87% of the corpus — with `description: ''` and
  `threads: []`.
- `link --method threads` emitted 195 threads, one of which (`Messages`,
  `thread-ebdfeb4c`) had `fragment_count: 30108`, spanning `2020-09-26` to
  `2026-05-20`, also with `description: ''`.

The compiled layer is the retrieval surface: `creek draft`, `creek mine`, and
the state report all read eddies and threads. A single page holding 87% of the
vault makes that surface useless for the message corpus, and the `eddies:`
wiki-link written onto all 30,795 member fragments points at that one page.

Three independent mechanisms produced this, and each had to be understood
before a fix could be chosen.

### 1. The eddy mega-cluster is DBSCAN behaving correctly

`EddyDetector._dbscan` builds an epsilon-graph (`cosine_neighbours(vectors,
1.0 - eps)`; at `eps = 0.3` an edge exists at cosine ≥ 0.70) and `_expand_cluster`
extends the frontier through every point that is itself a core point. That is
**unbounded transitive density-reachability**: one label propagates across the
entire density-connected component with no size bound at all. The only size
guard in the module was a *floor* (`if len(members) < min_fragments: continue`).

Short chat messages are semantically homogeneous, so neighbouring messages sit
well inside 0.30 cosine distance and effectively every message is a core point.
The epsilon-graph over 30,795 Discord fragments is therefore *one connected
component*, even though its diameter is enormous. DBSCAN then returns it as one
cluster — which is exactly what DBSCAN is defined to do.

The single rejection path, `_has_temporal_direction`, filters clusters whose
content drift correlates with chronological rank (|Spearman| ≥ 0.3). A
seven-year scattered blob has near-zero correlation, so it never fired.

**This is the decisive fact for the whole ADR:** DBSCAN's precondition is that
clusters are *separated by low-density regions*. A continuous chat stream has no
such separator. There is nothing in the data for any `eps` to find. **No
threshold value fixes this** — not a lower `eps`, not a higher `min_samples`,
not a per-source variant of either. Tuning is not on the table because the
algorithm's contract, not its parameterisation, is what has been violated.

### 2. The thread mega-cluster is an unbounded transitive closure

`ThreadDetector` bounds *pairing* to a 30-day sliding window (`break` once the
window is exceeded) but the union that follows is unbounded, and `_UnionFind.union`
merges roots unconditionally. Overlapping windows therefore take the transitive
closure end to end, chaining 2020-09-26 → 2026-05-20 into a single root of
30,108 members. The window bounds which pairs are *examined*; it does not bound
how far a component may *reach*.

The topic gate behind the union — frequency overlap, then cosine >
`similarity_threshold` (0.6) — did not help: every demo message classifies F2,
so the frequency gate was a no-op on this corpus. Worse, the gate **failed
open**: a pair missing an embedding unioned on frequency agreement alone.

### 3. The name was a filename leak, and a docstring/implementation divergence

Why "Messages"? `DiscordIngestor.generate_frontmatter` emitted no `title`, so
`assemble_ingested_fragment` fell back to `Path(source_path).stem`. `discover`
only ever reads `<channel_dir>/messages.json`, so **every** Discord fragment in
every vault got `title: messages`. Since `Fragment` carries no body or content
field, its title is the *only* text the linker ever sees.

`EddyDetector._generate_title` then took a `Counter` over member titles and its
three most common words. `most_common(3)` over 30,795 identical titles yields
`[("messages", 30795)]` → `"Messages"`.

The damning detail, and the reason this is recorded rather than quietly fixed:
the docstring claimed *"TF-IDF-lite: content-word frequency inside the cluster
scaled by inverse document frequency"*. Real IDF would have given "messages" a
document frequency of 30795/35330 and an inverse document frequency of ~0.137,
suppressing it outright. **The documented algorithm would not have produced this
bug. The implemented one contained no IDF whatsoever.** A docstring that
describes an algorithm the code does not implement is a defect class that
recurs, survives review, and is invisible to tests written from the code — so it
is named here explicitly.

Separately, neither `_build_eddy` nor `_build_thread` ever passed a
`description`, so it fell back to the model default `""` on 212 of 214
linker-written pages in the demo vault.

## Options considered

The issue proposed three, and they are not alternatives at the same level.

**Option 1 — Cluster-size ceiling with recursive split.** Any cluster above N
members is re-clustered at a tighter parameter until the parts fit; the
unsplittable remainder becomes noise.

**Option 2 — Segment message streams before clustering.** Pre-partition
platform-continuous streams into conversation episodes and cluster the episodes,
not the raw stream.

**Option 3 — Per-source thresholds.** A stricter `eps` / similarity for
message-platform fragments than for essays and notes.

**Option 3 is rejected outright.** It is the tuning answer, and §1 already
disposes of tuning: a stricter threshold on a corpus with no low-density
separator produces a smaller-diameter connected component, not two clusters. It
would also add a second, source-conditional calibration to maintain against a
single embedding model, for no structural gain.

**Option 1 alone is symptom treatment, and the failure is worth spelling out.**
Recursively tightening `eps` on a 30,795-member component does not discover
latent structure, because there is none to discover — it slices a *continuous
semantic manifold* at whatever arbitrary distance the schedule happens to reach.
At the configured ceiling that is roughly sixty subclusters, none of which
corresponds to any conversation, topic, or period a human would recognise. Every
one of them is still named from the same 30,795 identical `title: messages`
strings, so all sixty are still called "Messages", and all sixty still collide
on the same `[[Messages]]` wiki-link written onto their members. The trade is
**one meaningless page for sixty meaningless pages** — strictly worse, while
appearing to satisfy the acceptance criterion. A size cap that passes its own
test while making the product worse is the exact outcome this ADR exists to
prevent.

## Decision

Land **three layers, in dependency order**, and adopt options 1 and 2 together
with an explicit statement of which is the cure and which is the guardrail.

### Layer A — Naming (`creek/link/naming.py`) — root fix

Implement the IDF the docstring already promised. `distinctive_terms` scores a
term as `tf * log(corpus_size / (1 + df))` and **hard-drops** any term whose
document frequency reaches `ubiquity_floor` (0.6). A term carried by 60% or more
of the corpus can never become a title, by construction. IDF is deliberately not
applied below `min_idf_corpus` (20) documents, because document frequency over a
handful of documents is noise, and a two-cluster corpus necessarily puts each
cluster's own topic word at `df / n == 0.5` through no fault of its own.

Suppression can leave a cluster with *no text at all* — which is precisely the
message case, since `Fragment` has no body and every title is identical. So
`structural_label` names such a cluster from what remains distinctive: the
dominant source identity (channel, else conversation id, else interlocutor, else
platform) plus the date span. `"Aspiring Drag Queens, Mar 2023"` replaces
`"Messages"`. That data is present and varied on disk today.

The same module derives a never-empty `description` (count, dominant source,
span, recurring terms) and enforces title uniqueness within a run via
`disambiguate`, because the eddy assigner interpolates the title straight into
`[[…]]`.

**Why this layer is independently necessary:** existing vaults keep
`title: messages` until they are re-ingested. A source-side fix does not repair
what is already on disk.

### Layer B — Clustering domains (`creek/link/segments.py`) — root fix

Stop building one epsilon-graph over the whole chat stream. Before any
similarity graph exists, the corpus is partitioned into independent clustering
domains: a fragment from a configured `stream_platforms` platform belongs to a
conversation episode keyed on `(platform, series, episode_index)`, where
`series` is the channel / conversation id / interlocutor. The series is cut by
an **inactivity gap** (the conversational rule — people stop talking, and that
silence *is* the topic boundary) with a **span backstop** for a channel that
never falls idle. Every non-stream fragment lands in one shared domain, so
cross-source resonance, cross-platform eddies, and multi-year threads over
long-form material are untouched.

This removes the precondition violation rather than tuning around it, and it
**strictly reduces work**: clustering `d` domains of `n/d` fragments costs
`O(n²/d)` against `O(n²)`, in both `cosine_neighbours` and the thread detector's
windowed inner loop. It therefore relieves #857 rather than regressing it.

Segmentation is deliberately narrow — it touches only the corpus shape that
breaks the algorithms.

**Why this layer is independently necessary:** naming alone would give a
30,795-member mega-cluster a *good* name. It would still be one useless page.

### Layer C — Cluster-size ceiling (`creek/link/cluster_limits.py`) — guardrail

A config-keyed ceiling with bounded recursive re-clustering, applied strictly
**outside** `_dbscan` and outside the thread union-find: it only ever
re-partitions a membership list a detector has already produced, and the caller
injects a `recluster` callable that takes the tightened parameter as an
*argument*, so no detector's state is mutated mid-run. This placement is what
keeps the #790 brute-force-equivalence contract green — that oracle compares raw
`_dbscan` output, which the guardrail never sees.

Termination is governed by **two independent bounds, both load-bearing**: the
depth budget (`cluster_split_max_depth`), and the tightening schedule leaving
its valid range (epsilon may approach but never reach 0.0; similarity may
approach but never reach 1.0). A depth-only guard would keep re-clustering at a
degenerate parameter where nothing is a neighbour and every member silently
becomes noise.

The ceiling is kept and justified as the **machine-checkable invariant** behind
acceptance criterion 1 — *no emitted cluster exceeds
`max(cluster_size_ceiling, floor(corpus_size × cluster_max_fraction))`* — and as
protection for corpora nobody has looked at yet. It is a floor on badness, not a
definition of goodness.

**Why this layer is independently necessary:** layers A and B fix the two
degeneration modes we found. The ceiling is what holds when a corpus degenerates
in a way we did not predict.

### Product decision: an unsplittable cluster is discarded, loudly

A cluster still over the ceiling once **both** bounds are exhausted is discarded
to noise. Its members carry no `eddies:` / `threads:` frontmatter link at all.

That is real data loss in the compiled layer and it is accepted deliberately: an
unusable mega-page is worse than no page, and with segmentation in place the
path should be unreachable. It is never silent — each discard logs a WARNING
naming the clustering domain and the size, and the discarded fragment count is
surfaced to the operator as `LinkSummary.oversized_discarded`, rendered by
`creek link` alongside the largest emitted cluster.

**No `discard_unsplittable` escape hatch is provided.** A knob that turns the
discard back into a mega-page would restore the exact defect this ADR closes.
The supported opt-out is `cluster_max_fraction: 1.0`, which disables the ceiling
itself and is documented as such.

### Source-side fix

`DiscordIngestor.generate_frontmatter` now emits a real `title`
(`"<channel> <YYYY-MM-DD>"`), so vaults stop manufacturing 30k identical strings
at the source. This is safe for existing vaults: `generate_fragment_id` hashes
source path, timestamp and content — never the title — so no fragment ids churn.

### `Eddy.id` becomes content-stable

Eddy ids are now derived from the sorted member fragment ids
(`eddy-<8 hex of SHA-256>`), mirroring `_stable_thread_id` (#718). Re-detecting
the same cluster on the same corpus yields the same id, and therefore the same
page filename, so `VaultWriter.write_eddy` updates the existing page instead of
minting a fresh one each run. This matters *more* after this change, not less,
because segmentation and splitting produce many smaller clusters. Membership
changes deliberately yield a new id — a different membership is a different
eddy. The model's random default remains for hand-constructed eddies, which have
no membership to hash.

### Every previously-hardcoded threshold becomes config

The five module-private constants the detectors used (`eddy_eps`,
`eddy_min_samples`, `eddy_correlation_threshold`, `thread_window_days`,
`thread_similarity_threshold`) are exposed on `LinkingConfig` with defaults
equal to the exact constants they replace, so an upgraded vault clusters
identically. Both orchestrators (`creek.link.link_engine` and
`creek.link.linker`) construct detectors through a single
`from_linking_config` path, so the same vault cannot cluster differently
depending on which one ran. The fail-open union on missing embeddings is closed
by default behind `thread_union_without_embeddings: false`.

## Consequences

- **Positive**: a chat corpus yields per-conversation, per-episode eddies and
  threads with interpretable names and non-empty descriptions; the compiled
  layer becomes usable for retrieval over messages; the largest cluster is now
  visible in `creek link` output, so degeneration cannot recur invisibly;
  segmentation reduces the quadratic pairing cost that #857 tracks; every
  clustering threshold is now inspectable and tunable from
  `creek_config.yaml`.
- **Negative**: a cluster that survives the split budget is discarded, and its
  fragments lose their eddy/thread links entirely — accepted, logged, and
  counted, but genuinely lossy. Segmentation prevents multi-episode threads
  *within* a single chat series, which is the correct trade for a chat stream
  but does mean a genuinely long-running conversation topic now appears as
  several episode-scoped threads. `Eddy.id` values change for every existing
  vault on the next run, so previously written eddy pages are superseded rather
  than updated once.
- **Neutral**: `cluster_max_fraction: 1.0` and `stream_platforms: []` restore
  the pre-#880 unbounded behaviour for anyone who wants it. `stream_platforms`
  defaults to `["discord", "email"]` only — the two platforms Creek already
  routes to `01-Fragments/Messages/` — so chat *transcripts* (Claude, ChatGPT)
  remain long-form material in the shared domain.

## Reopening criteria

Revisit this decision when any of the following holds:

- A corpus is observed where episode segmentation fragments a genuinely
  coherent long-running conversation badly enough that operators want
  cross-episode thread stitching.
- `oversized_discarded` is non-zero on a real vault *after* segmentation, which
  would mean a degeneration mode neither layer A nor layer B covers.
- `Fragment` gains a body/content field. The entire naming problem is downstream
  of the fact that a fragment's title is the only text the linker can see;
  real content would make `distinctive_terms` far stronger and could retire the
  structural fallback for many clusters.
