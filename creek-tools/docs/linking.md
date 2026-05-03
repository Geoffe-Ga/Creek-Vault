# Linking

`creek link` connects fragments along three orthogonal axes:

| Linker        | Surfaces       | Backed by                                  |
|---------------|----------------|--------------------------------------------|
| `embeddings`  | **Resonances** | `sentence-transformers` cosine similarity. |
| `temporal`    | **Threads**    | Sliding-window union-find over timestamps + topic stability. |
| `eddies`      | **Eddies**     | Density-based clustering on the resonance graph. |

You can run a single linker or all of them. Most users run all three after every classification pass:

```bash
creek link --vault ~/Obsidian/Creek-Vault --method embeddings
creek link --vault ~/Obsidian/Creek-Vault --method temporal
creek link --vault ~/Obsidian/Creek-Vault --method eddies
```

## Resonances (embeddings)

`creek.link.embeddings.EmbeddingLinker` encodes each fragment's body with a sentence-transformer model and emits a resonance edge whenever the cosine similarity exceeds `EmbeddingsConfig.similarity_threshold`. Resonance edges live in the fragment's frontmatter:

```yaml
links:
  resonances:
    - fragment_id: frag-9c1f3a2b8e02
      score: 0.87
      method: embeddings
      generated_at: 2026-04-28T17:35:00Z
```

### Tuning

Defaults are tuned for the `all-MiniLM-L6-v2` model. If you switch models, recalibrate via `embeddings.similarity_threshold`:

| `similarity_threshold` | Effect                                                    |
|------------------------|-----------------------------------------------------------|
| 0.85+                  | Tight — surfaces only near-paraphrases.                   |
| 0.75 (default)         | Balanced — finds genuine semantic connections.            |
| 0.65                   | Loose — pulls in distant relatives; expect noise.         |

## Threads (temporal)

A **thread** is a chain of fragments that span a topic across time. `creek.link.threads.ThreadDetector` slides a window over the timestamped fragments, joins them into a union-find structure when their topics overlap, and writes a thread note under `02-Threads/` for each connected component.

Configuration (from `LinkingConfig`):

```yaml
linking:
  temporal_window_hours: 168     # sliding window (default: 1 week)
  thread_min_fragments: 3        # minimum fragments per thread
```

Threads are **directional** (oldest → newest) and have a **terminus** — the most recent fragment. The `creek mine` strategy `thread-terminus` uses these as essay seeds (a thread that's gone quiet often means there's a synthesis waiting to be written).

## Eddies (density)

An **eddy** is a tight cluster in the resonance graph. `creek.link.eddies.EddyDetector` runs density-based clustering and writes one note per cluster under `03-Eddies/`. Where threads are *temporal*, eddies are *non-directional*: many fragments densely linked to each other, possibly years apart.

Configuration (from `LinkingConfig`):

```yaml
linking:
  eddy_min_fragments: 5          # minimum cluster size
```

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

## Cost / cadence

Embeddings run **locally** by default (CPU-only) and a 10k-fragment vault rebuilds in ~10–20 minutes. Re-running is cheap because embeddings are cached at `<vault>/00-Creek-Meta/embeddings.parquet` and only stale rows are recomputed.

## Common patterns

```bash
# After ingesting a big new export.
creek classify --vault ~/Obsidian/Creek-Vault --method rules
creek link     --vault ~/Obsidian/Creek-Vault --method embeddings
creek link     --vault ~/Obsidian/Creek-Vault --method temporal
creek link     --vault ~/Obsidian/Creek-Vault --method eddies
creek report   --type synchronicity --vault ~/Obsidian/Creek-Vault

# After tuning the embedding threshold.
creek link --vault ~/Obsidian/Creek-Vault --method embeddings --rebuild
```
