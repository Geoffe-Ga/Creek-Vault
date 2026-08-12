# Linking

`creek link` connects fragments along four orthogonal axes:

| Linker        | Surfaces       | Backed by                                  |
|---------------|----------------|--------------------------------------------|
| `embeddings`  | **Resonances** | `sentence-transformers` cosine similarity. |
| `temporal`    | Temporal links | Proximity edges between fragments within `temporal_window_hours` of each other. |
| `threads`     | **Threads**    | Sliding-window union-find over timestamps + topic stability. |
| `eddies`      | **Eddies**     | Density-based clustering on the resonance graph. |

`temporal` and `threads` are distinct methods with distinct windows: `temporal` only draws proximity edges between fragments, while `threads` is what writes the pages under `02-Threads/`.

You can run a single linker or all of them. Most users run all four after every classification pass:

```bash
creek link --vault ~/Obsidian/Creek-Vault --method embeddings
creek link --vault ~/Obsidian/Creek-Vault --method temporal
creek link --vault ~/Obsidian/Creek-Vault --method threads
creek link --vault ~/Obsidian/Creek-Vault --method eddies
```

## Resonances (embeddings)

`creek.link.embeddings.EmbeddingLinker` encodes each fragment's title with a sentence-transformer model and emits a resonance edge whenever the cosine similarity meets or exceeds `EmbeddingsConfig.similarity_threshold` — a pair sitting exactly on the threshold resonates.

> **Resonance edges are not persisted.** They are computed in memory, counted, and dropped. There is no resonance writer anywhere in the codebase and `Fragment` has no `resonances` field — only `threads` and `eddies`. The only thing `--method embeddings` writes is the vector cache at `00-Creek-Meta/embeddings.parquet`, which the `threads` and `eddies` linkers then reuse. Earlier revisions of this page documented a `links: resonances:` frontmatter block; no version of Creek has ever written one. Persisting resonances is unbuilt work, not a regression.

### Reading the run summary

`creek link --method embeddings` reports three counts, and they legitimately differ:

| Count      | Meaning                                                                        |
|------------|---------------------------------------------------------------------------------|
| `scanned`  | Fragments loaded from the vault — the corpus size.                              |
| `computed` | Vectors the local model actually produced this run — cache misses only.         |
| `cached`   | Rows in `00-Creek-Meta/embeddings.parquet` after the run.                       |

`computed` is zero on a warm cache — that's normal, not a failure. It's the number this line used to report as "embedded" regardless of whether the model did anything. `cached` is the whole cache rewritten from fresh hits plus any newly-computed vectors, so a fully warm run reports the entire corpus under `cached` while `computed` sits at zero.

`no vectors written` after a run that reports vectors `computed` means the cache write itself failed — check the log for the warning. Linking still succeeded and the resonance count is still accurate; the only cost is that those vectors get recomputed next run instead of served from cache. On an empty vault, `no vectors written` just means there was nothing to cache.

### Tuning

Defaults are tuned for the `all-MiniLM-L6-v2` model. If you switch models, recalibrate via `embeddings.similarity_threshold`:

| `similarity_threshold` | Effect                                                    |
|------------------------|-----------------------------------------------------------|
| 0.85+                  | Tight — surfaces only near-paraphrases.                   |
| 0.75 (default)         | Balanced — finds genuine semantic connections.            |
| 0.65                   | Loose — pulls in distant relatives; expect noise.         |

## Temporal proximity

`creek link --method temporal` draws proximity edges between fragments authored close together. It writes no pages of its own.

```yaml
linking:
  temporal_window_hours: 168     # proximity window (default: 1 week)
```

`temporal_window_hours` feeds **only** this linker. Thread detection has always used its own window — see `thread_window_days` below.

## Threads

A **thread** is a chain of fragments that span a topic across time. `creek.link.threads.ThreadDetector` slides a window over the timestamped fragments, joins them into a union-find structure when their topics overlap, and writes a thread note under `02-Threads/{Active,Dormant,Resolved}/` for each connected component.

Configuration (from `LinkingConfig`):

```yaml
linking:
  thread_window_days: 30                    # sliding window for pairing
  thread_similarity_threshold: 0.6          # cosine a pair must exceed to union
  thread_union_without_embeddings: false    # don't union on frequency alone
  thread_min_fragments: 3                   # minimum fragments per thread
  thread_split_similarity_step: 0.1         # tightening step when over the ceiling
```

Threads are **directional** (oldest → newest) and have a **terminus** — the most recent fragment. The `creek mine` strategy `thread-terminus` uses these as essay seeds (a thread that's gone quiet often means there's a synthesis waiting to be written).

## Eddies (density)

An **eddy** is a tight cluster in the resonance graph. `creek.link.eddies.EddyDetector` runs density-based clustering and writes one note per cluster under `03-Eddies/`. Where threads are *temporal*, eddies are *non-directional*: many fragments densely linked to each other, possibly years apart.

Configuration (from `LinkingConfig`):

```yaml
linking:
  eddy_eps: 0.3                   # max cosine DISTANCE for a DBSCAN neighbour
  eddy_min_samples: 5             # neighbours needed to be a core point
  eddy_correlation_threshold: 0.3 # above this |Spearman|, it's a thread, not an eddy
  eddy_min_fragments: 5           # minimum cluster size
  eddy_split_eps_step: 0.05       # tightening step when over the ceiling
```

### Tuning eddy density

| `eddy_eps` | Effect                                                            |
|------------|-------------------------------------------------------------------|
| 0.20       | Tight — an edge only at cosine ≥ 0.80; many small, sharp eddies.   |
| 0.30 (default) | Balanced — an edge at cosine ≥ 0.70.                          |
| 0.40       | Loose — an edge at cosine ≥ 0.60; expect large, diffuse eddies.    |

Eddy ids are **content-stable**: derived from the sorted member fragment ids, so re-detecting the same cluster over the same corpus produces the same id and therefore updates the same page instead of minting a new one. A membership change deliberately yields a new id — a different membership is a different eddy.

## Message streams and cluster size

Both detectors assume a corpus whose clusters are separated by low-density regions. A continuous chat stream is not such a corpus: short messages from one channel are semantically homogeneous and temporally unbroken, so the epsilon-graph over them is one connected component and the sliding windows chain end to end. Left alone, an entire message corpus collapses into a single eddy and a single thread — whatever thresholds are chosen. Full reasoning: [ADR-0008](architecture/ADR/0008-bounding-cluster-degeneration-in-message-streams.md).

Three mechanisms bound this.

**Segmentation.** Before any similarity graph is built, fragments from a `stream_platforms` platform are cut into conversation episodes and clustered independently. Every other fragment shares one domain, so cross-source eddies and multi-year threads over long-form material are unchanged.

```yaml
linking:
  stream_platforms: [discord, email]   # [] disables segmentation entirely
  stream_episode_max_gap_hours: 24     # silence that ends an episode
  stream_episode_max_span_days: 30     # backstop for a never-idle channel
```

**Naming.** Titles come from TF-IDF-scored distinctive terms, so a term carried by most of the corpus can never become a title. When suppression leaves no term standing — the normal outcome for identically-titled messages — the cluster is named structurally from its dominant source and date span, e.g. `Aspiring Drag Queens, Mar 2023`. Titles are made unique within a run, since a page title is interpolated straight into a `[[wiki-link]]`, and every generated page now carries a non-empty `description`.

**Cluster-size ceiling.** A guardrail, not the cure:

```yaml
linking:
  cluster_size_ceiling: 500       # never split below this many members
  cluster_max_fraction: 0.10      # max share of the corpus; 1.0 opts out
  cluster_split_max_depth: 3      # re-clustering rounds; 0 disables splitting
```

The effective ceiling is `max(cluster_size_ceiling, floor(corpus_size × cluster_max_fraction))`, so ordinary vaults never trip it. A cluster over the ceiling is re-clustered at a tighter `eddy_eps` / `thread_similarity_threshold`; one that is still oversized after the budget is spent is **discarded to noise** — its members carry no `eddies:` / `threads:` link at all. Each discard logs a `WARNING` naming the domain and the size, and the count is reported by `creek link`.

## Synchronicities

`creek.generate.synchronicity.SynchronicityDetector` (run via `creek report --type synchronicity`) flags resonance edges that are **surprising** — fragments from very different sources or registers that the embeddings consider similar. These are the most interesting findings in practice; they often surface unconscious connections between, say, a therapy reflection and a software design note. Output lands in `05-Wavelength/Synchronicities/`.

The exact criteria for "surprising" — cosine similarity > 0.9, different source types, > 30 days apart, and "still working on X"-style status updates filtered out — live in [`emergence.md`](emergence.md) alongside the other §10 emergence sub-systems.

## Reading the graph

Every linker writes both directions of every edge. The fragment frontmatter shows local neighbours; the thread / eddy notes show connected components; and `creek report --type linking` prints aggregate statistics:

```
Resonances:        12,431 edges across  3,206 fragments
Threads:                89 chains, longest: 47 fragments
Eddies:                 26 clusters, largest: 312 fragments
Synchronicities:       143 surprising edges
Mean fragment fan-out: 7.7
```

`creek link` itself also reports the largest cluster it emitted, which is the one number that tells you whether detection degenerated:

```
17 eddy(ies) detected, 17 eddy file(s) written to 03-Eddies/, 4120 fragment(s)
updated with `eddies:` wiki-links, largest cluster: 312 fragment(s).
```

Re-clustering and discards are only mentioned when they happened:

```
… largest cluster: 480 fragment(s), 2 oversized cluster(s) re-clustered,
61 fragment(s) discarded as unsplittable.
```

## What `creek process` runs

`creek process`'s link stage is not a fifth linker — it calls the same `run_link` entry point this page documents, twice, in this order:

```
creek link --method eddies      # then
creek link --method threads
```

Three consequences worth knowing:

- **`embeddings` and `temporal` are not run.** Neither persists anything a subsequent stage needs: `temporal` writes nothing at all, and the vector cache `embeddings` would warm is written by the `eddies` pass anyway. Running them would only add the O(n²) `find_resonances` pass for a result nothing can store. Run `creek link --method embeddings` explicitly if you want the resonance count reported.
- **The scope is the whole vault, not the files you just ingested.** `run_link` reloads every fragment under `01-Fragments/`, which is the only way a new note can join an eddy of older notes and the only way membership-derived cluster ids stay coherent. The cost is one DBSCAN pass plus one union-find pass per `creek process`.
- **The two calls stay two calls deliberately.** Frontmatter is rewritten from the whole in-memory fragment model, so a stage that ran off a shared, once-loaded fragment list would overwrite the other stage's wiki-links with an empty list. Each call re-reads what the previous one wrote.

### Stale pages on a growing corpus

Eddy and thread ids are derived from their sorted member fragment ids. That is what makes re-running idempotent on an *unchanged* corpus — the same membership re-mints the same id and the same filename. But when membership changes (you ingest one more note into a cluster) the id changes too, so a **new** page is written and the previous one is left behind, orphaned, with no fragment linking to it. On the `creek link` path that is an occasional manual annoyance; with `creek process` on a schedule it accumulates. `creek lint`'s broken-links check is the current detector; there is no reaper yet.

## Cost / cadence

Embeddings run **locally** by default (CPU-only) and a 10k-fragment vault rebuilds in ~10–20 minutes. Re-running is cheap because embeddings are cached at `<vault>/00-Creek-Meta/embeddings.parquet` and only stale rows are recomputed.

## Common patterns

```bash
# After ingesting a big new export.
creek classify --vault ~/Obsidian/Creek-Vault --method rules
creek link     --vault ~/Obsidian/Creek-Vault --method embeddings
creek link     --vault ~/Obsidian/Creek-Vault --method temporal
creek link     --vault ~/Obsidian/Creek-Vault --method threads
creek link     --vault ~/Obsidian/Creek-Vault --method eddies
creek report   --type synchronicity --vault ~/Obsidian/Creek-Vault

# After tuning the embedding threshold.
creek link --vault ~/Obsidian/Creek-Vault --method embeddings --rebuild
```
