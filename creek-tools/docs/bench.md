# Model-capacity harness

Issue [#1850](https://github.com/Geoffe-Ga/Creek-Vault/issues/1850). The
harness measures whether a local model can serve `creek.reflect` inside the
published request deadline on a given allocation. It also models what that
allocation costs per account-month.

It gathers evidence. It does not take the D04 decision
([`../../docs/decisions/2026-10-06-d04-local-model-capacity-envelope.md`](../../docs/decisions/2026-10-06-d04-local-model-capacity-envelope.md)).
It never changes a runtime default.

Run it through the wrapper:

```bash
./scripts/bench.sh reflect --out report.json            # hermetic fake mode
./scripts/bench.sh reflect --provider pool \
    --model gpt-oss-120b --pool-identity vendor/gpt-oss-120b@v3 \
    --out pool-report.json                              # hermetic fake pool
./scripts/bench.sh cost --price-file prices.json --out cost.json
./scripts/bench.sh reflect --help                       # every flag
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success. |
| `2` | Refused. A fixed message names the flag or field; it never echoes the value. |
| `1` | Any other failure, named by exception type only. |

## What a run does

Every trial calls the real `reflect_tool` with the production care guard and an
`open` ceiling. The vault it reads is a **synthetic corpus**, written into a
fresh temporary directory and deleted when the run ends. The corpus is seeded,
byte-stable, and built from a neutral vocabulary. The harness refuses any root
that is not an empty directory under the system temp directory, so it can never
read or write a real vault.

The run is made of four sweeps:

| Sweep | Flags | What it answers |
|---|---|---|
| cold/warm | `--cold-trials`, `--warm-trials` | Each cold trial evicts the model (`keep_alive: 0`) and starts a fresh grounding session first. Warm trials share one session. |
| context | `--context-sizes 512,2048` | Latency and token cost at each entry size, in words. Sizes are refused unless size + a 1,536-token prompt-overhead allowance + `--num-predict` fits in `--num-ctx` (`tokens_per_reflection.prompt_overhead_tokens` reports whether that allowance is still a ceiling). Words can tokenize to several tokens, so a size that passes can still overflow; a live run reports that trial as `context_overflow` (from Ollama's `prompt_eval_count`), never `ok`. |
| concurrency | `--concurrency 1,2,4` | Runs `N` reflections in flight together on `N` threads, the way `/v1` serves reads. |
| idle | `--idle-cycles`, `--idle-seconds` | Evict, wait, then resume. |

After the sweeps, one **untimed** turn per model-calling surface measures what a
reflection *costs* rather than how long it takes. See
[Measured tokens per reflection](#measured-tokens-per-reflection).

## Modes

### `--mode fake` (default)

A deterministic local fake answers every prompt. No model, key or network is
involved. Grounding defaults to `none` here, because the production grounder
would load an embedding model. The latency a fake run reports is the harness's
own overhead, which is a useful floor.

### `--mode live`

A live run drives an operator-supplied Ollama at `--ollama-url`. It needs
`--model` and `--digest` (`sha256:<64 hex>`, read from `ollama list` /
`/api/tags`). It also needs `--git-sha`, which `bench.sh` fills in from the
checkout. The harness refuses to run in these cases:

- if the served weights' digest differs from `--digest`;
- if `--model` is an Ollama cloud-offload tag (`*-cloud`, `*:cloud`), which a
  local daemon forwards off-host;
- if `--ollama-url` does not resolve only to loopback or private addresses,
  unless you pass `--allow-remote-host`. The report records the result as
  `run.endpoint_scope` (`loopback`, `private` or `remote`);
- if the provider is registered as cloud, or a built model callable does not
  declare itself local.

These checks establish where requests are *addressed*. They cannot see what a
runtime at a local address does next. A self-hosted proxy that forwards
elsewhere would pass, so point the harness only at a runtime you operate.

Every request pins `options.num_ctx`. Production does not pin it today; whether
it should is B05's call. Live grounding defaults to the production grounder.

### `--provider pool`

The shared confidential warm-inference pool
([ADR-0016](architecture/ADR/0016-shared-confidential-warm-inference-pool.md),
selected as option O5 — the vendor-attested confidential pool). Pool mode
answers issue #1892 and ADR-0016 Decision 7: the harness could only measure
Ollama, and the thing a cohort decision rests on is the pool.

It is **hermetic by default**: `--provider pool` without `--mode live` drives
an in-process fake pool over the real client, so every sweep and every refusal
path runs with no vendor contacted and nothing billed (Decision 4 records that
the owner authorizes *no spend yet*). The live run against a real vendor is a
separate, owner-gated issue (#1899).

| Flag | Meaning |
|---|---|
| `--provider pool` | Measure the pool instead of Ollama. |
| `--pool-url` | The pool endpoint. A public address needs `--allow-remote-host`. |
| `--pool-identity` | The identity every request names and every answer must echo. Required. |
| `--pool-vendor` | A short label for whose pool was measured. Required for a live run; `fake` otherwise. |
| `--model` | The model the pool must serve and echo. |
| `--digest` | The **attested measurement-set digest**, not a weights digest: a vendor publishes no blob digest (Decision 3 item 2). Required for a live run. |

What pool mode refuses, each before a prompt is sent and with exit 2:

- a pool whose identity listing advertises anything but `--pool-identity`
  (Decision 1 item 1, T1);
- a pool whose attestation does not verify, or attests a measurement set
  other than `--digest`;
- a pool that does not serve `--model`;
- a `*-cloud` / `*:cloud` model tag, in pool mode as everywhere else;
- a `--pool-url` that resolves to a public address without
  `--allow-remote-host`.

A vendor pool *is* remote, deliberately (Decision 6): with the opt-in the run
proceeds and the report records `endpoint_scope: remote`, and
`host.describes_model_host` is then false. Two outcomes join the closed
vocabulary for it: `rate_limited` (the pool refused admission, 429) and
`insufficient_quota` (the vendor key's cap is spent, 429 and not transient —
Decision 3 item 4). Both are errors, never fast successes.

Two things the client cannot do, and says so rather than guessing:

- **it cannot evict a pool.** Warm weights are what a pool is for, so a
  "cold" pool trial measures a cold *vault* process — fresh grounding
  session, no cached prefix — against warm weights.
- **it cannot see the pool's resident size.** There is no `/api/ps` on a
  pool, so `trials[].model_resident_bytes` is `null` for every pool trial.

The pool is never an INTIMATE destination (Decision 5). The harness runs the
synthetic corpus at the `open` ceiling and acquires no path that could route
INTIMATE content.

One logging note: `httpx` writes its own request line, endpoint URL included,
on the `httpx` logger at INFO. The harness configures no logging, and the
report carries neither the URL nor the host — but if you turn INFO logging on
yourself, your log, unlike the report, is not content-free.

The live smoke test is opt-in and is never run in CI:

```bash
CREEK_BENCH_OLLAMA_URL=http://127.0.0.1:11434 CREEK_BENCH_MODEL=mistral:7b \
CREEK_BENCH_MODEL_DIGEST=sha256:... ./scripts/test.sh --live -k bench
```

## Reading the report

The report is JSON and **content-free**. It holds only enums, numbers and
pattern-constrained identifiers: no prompt, no entry text, no model output, no
hostname and no path. Tests enforce this with a canary and with a walk of the
JSON schema.

| Field | Meaning |
|---|---|
| `run.provider` | `fake`, `ollama` or `pool`. A pool run also records `run.pool_vendor` and `run.pool_identity` — an identity, never a destination. |
| `host` | Capacity facts (CPUs, RAM, kernel, free disk) of the machine the **harness** ran on (`scope: harness`). `describes_model_host` is true only when the model ran there too: fake mode or a loopback endpoint. Free disk is from `--model-store-path` when given (`disk_scope: model_store`), otherwise from the harness's temp directory (`harness_temp`). |
| `budget_seconds` | `min(limits.DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT)`. Grounding and generation share this one budget. It is the deadline **this process** enforces; ADR-0016 R2's four budgets are in `budgets`. |
| `per_sweep.<sweep>.latency` | p50 and p95 over **ok** trials; the error rate over all trials; `generation_p95_s`, the model call's share; `ttft_p95_s`, the p95 time to first token over the trials whose backend reported one (`null` when none did). |
| `per_sweep.<sweep>.throughput`, `…per_phase.<phase>.throughput` | Output tokens per second — `basis`, `count`, `excluded_count`, `tokens_per_second_p50`, `tokens_per_second_p95`. See the budget section below for why a fake run's figures are `null`. |
| `budgets` | ADR-0016 R2's named budget table. See below. |
| `per_sweep.<sweep>.per_phase` | The same aggregates and a verdict per residency phase (`cold`, `warm`, `resume`), so a slow cold start is not hidden inside many warm trials. |
| `per_sweep.<sweep>.verdict` | The worse of its phases. A phase `fits` only when p95 ≤ budget **and** no trial failed; trials that all failed are `exceeds`, not missing data. |
| `verdict` | Any `exceeds` dominates. `insufficient_data` means nothing was measured. **This is the per-sweep verdict against `budget_seconds` only** — R2 is answered by `budgets.verdict`, separately, because a run can sit well inside the shed deadline while measuring nothing R2 asks about. |
| `capacity` | The largest concurrency level that fits, together with every level below it. It is compared against `/v1`'s admission caps (`DEFAULT_MAX_CONCURRENCY`, `DEFAULT_MAX_PER_CONSUMER`). It reports and changes nothing. |
| `trials[].outcome` | One of `ok`, `timeout`, `oom`, `disk_full`, `provider_unavailable`, `context_overflow`, `rate_limited`, `insufficient_quota` or `error`. The last two are pool-only (ADR-0016 Decision 3 item 4). |
| `trials[].model_resident_bytes` | The model's size from `/api/ps`. This is the runtime's figure, not the harness process's RSS. Always `null` in pool mode: a pool does not publish it. |
| `trials[].ttft_s` | Seconds from the start of the model call to its first token, as a streaming backend reported them, or `null` when the backend does not stream. `null` is "unknown" — never "instant", never "as slow as the whole call". |
| `trials[].prompt_tokens` / `output_tokens` | What the provider reported for that trial, or `null` when it reported nothing. Never derived. |
| `trials[].token_basis` | Where that trial's counts came from, travelling with the counts. Only `provider_reported` may feed a tokens/s figure. |
| `trials[].max_output_tokens` | The output ceiling `reflect_tool` asked the factory for, captured at the model seam. |
| `per_sweep.<sweep>.tokens`, `…per_phase.<phase>.tokens` | Measured prompt and output tokens over the **ok** trials of that group: p50, p95, mean, max, plus `reported_count`, `unreported_count` and `output_cap_exceeded_count`. |
| `tokens_per_reflection` | The run-level measured token block. See below. |
| `quality_score` | The voice-fidelity eval's per-dimension scores, rubric version, seed and verdict ([#1898](https://github.com/Geoffe-Ga/Creek-Vault/issues/1898)), or `null` for a capacity-only run. See below. |

## Latency budgets: ADR-0016 R2's named table

R2 states four budgets, not one. The harness reports each as its own row in
`budgets.rows`, with its limit, the value judged against it, and its own
verdict — never collapsed into a single pass.

| Row | Limit | Source | Judged against |
|---|---|---|---|
| `server_shed_deadline` | `min(DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT)` | `code` | p95 over **every** trial |
| `vault_first_byte_window` | 10 s | `documented_constant` | Representative reflection p95 |
| `vault_reflection_p95` | 25 s | `adr_0016_r2` | Representative reflection p95 |
| `machine_wake_within_client_deadline` | 30 s − 25 s = 5 s | `documented_constant` | p95 of the **cold and resumed** trials, which are the wake-bearing ones |
| `pool_generate_p95` | 10 s | `adr_0016_r2` | `generation_p95_s` of the concurrency sweep's **level-4** trials at R2's prompt size |
| `pool_ttft_p95` | 2 s | `adr_0016_r2` | `ttft_p95_s` of that **same** group |

"Representative reflection p95" means the **`cold_warm` and `idle` sweeps
only**. Those are the two that issue one reflection at a time, so each trial's
wall clock is that reflection's own — which is what R2 budgets, "cold and
warm".

Both stress sweeps are excluded, symmetrically and for the same reason. The
`context` sweep walks input size up towards `num_ctx` to find where the window
breaks, so it times entries production never sends. The `concurrency` sweep
runs `level` reflections simultaneously and every `latency_s` is wall clock, so
a level-4 trial carries the queueing delay of its three siblings — a figure
that answers "how does this behave under load", not "how long does a reflection
take". Naming either p95 `vault_reflection_p95` would publish a stress result
as the production figure. Both are still judged, in their own `per_sweep`
verdicts and against the deadline this process enforces.

**`server_shed_deadline` is the exception: it is judged over every trial.** It
is the deadline this process actually sheds on, and a context-sweep reflection
is shed by it exactly like any other. Scoping it to the representative
reflection would let it read `fits` at 4 s beside a `per_sweep.context` verdict
of `exceeds` for the very same number — and this is the row a reader quotes,
because it is the one whose `source` is this repository's code.

**The wake row is judged against what is left *beside* the vault budget.** R2
says Machine wake "must fit beside that 25 s inside adepthood's 30 s" — beside,
not inside — so the limit is `ADEPTHOOD_TOTAL_DEADLINE_SECONDS −
R2_VAULT_P95_SECONDS`, derived from both so a change to either moves it.
Judged against the full 30 s, a 14 s wake-bearing trial read "Machine wake fits
inside adepthood's client deadline", which is false the moment the reflection
it precedes spends its own budget. The measured value is the wake-bearing
trial's *total* latency rather than an isolated wake — the harness cannot
separate them — so this is a deliberately conservative proxy.

**R2 states the pool budget under two conditions, and both are disclosed.**
"pool generate p95 ≤ 10 s for a 2,500-token prompt and 128 output tokens at
concurrency 4" is a claim about a level *and* a prompt size.
`budgets.pool_level_measured` says whether the concurrency sweep requested
level 4; `budgets.pool_prompt_size_measured` says whether the trials feeding
the row reported a prompt of `R2_POOL_PROMPT_TOKENS` (2,500), within
`R2_POOL_PROMPT_TOKEN_TOLERANCE` (±10%), from a `provider_reported` basis — a
byte upper bound is not a token count. `Workload.query_words` defaults to 64,
an order of magnitude under R2's prompt, so when the condition is unmet the
row is `insufficient_data` rather than a pass earned on a smaller prompt.

**No backend creek-tools ships today reports a time to first token**, so
`pool_ttft_p95` is a gap in every configuration currently available. Neither
the Ollama client nor the fake implements `FirstTokenReporting`'s streaming
figure. The row stays `in_force` and reports `insufficient_data` rather than
being quietly dropped: [#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899)'s
pool client is what will answer it. A `null` `ttft_s` is "unknown" — never
"instant", which is also why a negative figure from a skewed clock becomes
`null` rather than `0.0`.

Both pool rows are judged on that one group and no other, which scopes the
error-rate clause with it. Judged over every trial, one deliberate
out-of-memory in the context sweep published a `pool_ttft_p95` row reading
"limit 2.0 s, measured 1.0 s, verdict `exceeds`".

**Which budget is code-derived and which is ADR-derived.** `source: code`
means the limit is read through its module or class at call time, so patching
`limits.DEFAULT_TIMEOUT_SECONDS` moves both the limit and the verdict.
`documented_constant` means the number is stated by code in *another*
repository — adepthood's `creek_vault_client.py` — which this process cannot
import, so it is recorded here as a constant, composed the way its source
composes it. `adr_0016_r2` means the ADR states it and no code anywhere does.

**The 10 s first-byte window is what binds today.** R2 names its own
prerequisite: adepthood must widen `_post_reflection`'s phase budgets to its
30 s deadline with `_widened_phase_budgets`, as the cold embedding call
already is. It has not happened —
[#1903](https://github.com/Geoffe-Ga/Creek-Vault/issues/1903) sequences it,
and it is adepthood's own issue. Until then `_post_reflection` runs on the
pooled client whose `httpx.Timeout` `read` phase is
`_VAULT_TIMEOUT_SECONDS = 10.0`, `/v1/reflections` is not streamed, and
Machine wake is **inside** that window rather than beside it.

So the harness reports the 25 s budget and judges against the 10 s one:

- `budgets.binding_budget` names the budget production enforces
  (`vault_first_byte_window` today, `vault_reflection_p95` once widened);
- `budgets.phase_budgets_widened` records which world the run was judged in;
- `in_force` is `false` on the 25 s row, and `budgets.verdict` is the worst
  verdict **among the rows in force only**.

**A green `vault_reflection_p95` row is therefore not a pass.** It says the
measurement would fit a budget production does not yet allow. The row that
decides anything today is `vault_first_byte_window`.

**No row here is a production guarantee — no run this harness can execute
today is a production run.** A `fake` or `loopback` run times this process on
this machine. A `private` or `remote` run times somebody's dev box:
`bench --mode live --ollama-url http://192.168.1.50:11434` reaches a model off
this host and still dials no pool, wakes no Fly Machine and includes no
attestation round trip. So the endpoint scope cannot license a production
claim, and nothing in the table tries to. The run that would is the capstone
child, [#1899](https://github.com/Geoffe-Ga/Creek-Vault/issues/1899), and
ADR-0016 Decision 4 authorizes **no spend** for it.

`budgets.model_off_harness_host` records only the narrow fact its name states —
whether the measured model ran somewhere other than the host whose capacity
`host` describes. It is the exact complement of `host.describes_model_host`,
taken from that field rather than re-derived, so the two can never disagree,
and it licenses nothing beyond itself.

**A failure outranks a fast p95.** Each row applies `MAX_FIT_ERROR_RATE = 0.0`
before it compares anything: a group containing one timed-out, OOM, disk-full
or model-unavailable trial is `exceeds` even when its p95 is inside every
budget. That trial is a reflection the user did not get, however fast it
failed. With no failures, a `null` measured value is a genuine gap —
`insufficient_data`, never a pass. That is what a run reports when nothing
streamed (`pool_ttft_p95`), when concurrency 4 was never requested
(`pool_generate_p95`, and `pool_level_measured` is then `false`), or when no
cold or resumed trial ran (`machine_wake_within_client_deadline`).

### tokens/s, and when it is refused

`throughput` carries output tokens per second, and it is computed **only from
`provider_reported` counts**. A `byte_upper_bound` count bounds *bytes*, so
dividing it by a measured generation time yields an upper bound on throughput
wearing a measurement's clothes — and an upper bound flatters the backend,
which is the one direction a capacity figure must never err in. Such samples
land in `excluded_count`, the figures stay `null`, and `basis` says which bound
was refused. This is the same rule
`creek_mcp/provisioning/pool_bundle.py`'s `size_pool_caps` applies to the cost
side.

So **a `--mode fake` run reports `tokens_per_second_p50` and
`tokens_per_second_p95` as `null`** with `basis: "byte_upper_bound"` and a
non-zero `excluded_count`. That is the correct result, not a gap in the
harness: the fake has no tokenizer, so it can state a rate over bytes or
nothing, and nothing is the honest option. A citable rate needs a pinned local
runtime or the live pool run.

Note also that `tokens_per_second_p95` is the **favourable** tail — the run's
fastest generation, not its worst. A latency budget is judged on
`latency.p95_s`; throughput p95 is never the figure to promise against.

## Measured tokens per reflection

ADR-0016 sized every cost figure from one unmeasured planning assumption:
**2,500 prompt tokens and 128 output tokens per reflection**. The pilot's
monthly ceiling, the per-1,000-reflections cost and the per-activation
vendor-key cap all derive from it.

**A cost figure must cite measured counts from `tokens_per_reflection`. Any
figure quoting 2,500 + 128 is an unmeasured placeholder, and must say so.**
The two assumed numbers are carried in the block itself
(`assumed_prompt_tokens`, `assumed_output_tokens`) so a cost line can show
which of the two it used.

Three surfaces are measured separately, because the cap formula and the cost
figures are per-surface and grounding changes the prompt materially:

| Surface | Path | Output ceiling |
|---|---|---|
| `reflection_ungrounded` | `reflect_tool` with no grounding lines | `max_notes`-scaled, capped at 128 |
| `reflection_grounded` | `reflect_tool` with every grounding slot filled, as a vault with a built index pays | the same |
| `letter` | `write_letter` | 1,500 |

Each measurement is one turn through the shipped path over the synthetic
corpus. Nothing in the pass changes a prompt or a cap — it measures what ships.
A CLI run always measures it; a programmatic caller can skip the pass with
`measure_tokens=False` on its `Workload` for a latency-only run, and the block
then reads `basis: "not_measured"` with an empty `per_surface`.

| Field | Meaning |
|---|---|
| `basis` | `provider_reported` — the runtime's own counters (Ollama's `prompt_eval_count` / `eval_count`); **citable**. `byte_upper_bound` — one token per UTF-8 byte, self-reported by `--mode fake`, which has no tokenizer; an upper bound, **not** a cost. `unreported` — the pass ran and the backend reported no counts. `not_measured` — the pass did not run. |
| `per_surface.<surface>.prompt_tokens` / `output_tokens` | The measured counts, or `null` when unreported. A `null` says the count is unknown; it is never replaced with a zero or an estimate. |
| `per_surface.<surface>.output_cap_exceeded` | Whether the measured output ran past the ceiling that surface asked for. Reported, never clipped. |
| `prompt_overhead_tokens` | Measured prompt tokens for the grounded template around a one-word entry — the allowance the context sweep subtracts from `num_ctx`, now a measurement rather than an assertion over the template's byte length. |
| `prompt_overhead_within_bound` | Whether that measurement is at or below the 1,536-token allowance. `false` means the allowance has become the binding constraint rather than a ceiling, and the context sweep's sizes are no longer safe. |

A fake run's counts are honest about themselves and useless as prices: they
measure the real shape of the shipped prompt, at a byte-level bound that no
tokenizer can exceed and every tokenizer beats. The counts a budget line may
quote come from a pinned local runtime, or from the live pool run — which is
gated on an owner budget note, because ADR-0016 Decision 4 authorizes no spend
yet.

## Cost model

The cost model is arithmetic over an operator-supplied price sheet. It is never
a measurement: every estimate says `"kind": "model"`. It fetches nothing.

Two cost **shapes** share that one sheet, named by the estimate's `shape`
field: `machine_month` (a Fly Machine and volume per vault) and
`metered_tokens` (ADR-0016's per-token vendor pool). A sheet may carry either
block, both, or neither.

A sheet is refused in four cases:

- it is undated;
- it is older than 90 days (`observed_on`);
- it is dated in the future;
- it has no price for the allocation.

The allocation defaults are read from `FlyProviderPolicy` (`creek_mcp/provisioning/fly.py`).
Override them with `--cpu-kind`, `--cpus`, `--memory-mb`, `--rootfs-gb` and
`--volume-gb`.

The cost of one month is:

```text
machine_monthly * duty + volume_gb * volume_gb_month + (1 - duty) * rootfs_gb * rootfs_gb_month
```

The result is rounded half-up to the cent. It is computed at `--duty-low` and
`--duty-high`. If you pass `--allowance N --seconds-per-reflection S
--linger-seconds L`, it is also computed at the duty that allowance implies,
against Fly's 730-hour billing month.

`cost_bands` prices USD only — its output fields say `usd` — so it refuses a
sheet whose `currency` is anything else, and refuses a metered-only sheet by
naming the missing `machine_monthly_usd` block rather than guessing a price.

Price-sheet schema. The **values below are an example, not current prices**:

```json
{
  "observed_on": "2026-10-01",
  "currency": "USD",
  "source": "fly-pricing-page",
  "machine_monthly_usd": {"shared-1x-1024mb": "5.70"},
  "volume_gb_month_usd": "0.15",
  "rootfs_gb_month_usd": "0.15"
}
```

## Metered per-token pricing (ADR-0016 / #1897)

ADR-0016 is ratified as **O5**, a vendor-attested confidential pool billed per
token, so cost tracks price per token rather than a machine's parameter count.
`--metered-model` and `--token-report` switch `cost` to that shape:

```bash
./scripts/bench.sh cost \
  --price-file prices.json \
  --metered-model MODEL_ID \
  --token-report report.json \
  --out metered-cost.json
```

`--token-report` is a benchmark report, and the estimate is priced from its
`tokens_per_reflection` block — the worst measured prompt count and the worst
measured output count across the three surfaces. The worst surface is
deliberate: adepthood's monthly allowance is shared across reflections and
letters, so any one charged generation could be the expensive one. INTIMATE
content never reaches the pool (ADR-0016 Decision 5), so it contributes no pool
cost.

**Only `provider_reported` counts may price anything.** A fake run's
`byte_upper_bound` is a bound on *bytes*; `unreported` and `not_measured` are
absences. Each is refused with exit 2 — never estimated, and never silently
backed off to the superseded 2,500 + 128 planning assumption. Refusing to
produce a figure is the correct outcome; a plausible figure quoted from a basis
that cannot support it is not, because someone budgets against it.

**Currency is declared, never converted.** A sheet says `USD` or `EUR`;
`--metered-currency` says which the caller expects, and a mismatch is refused.
Nothing here holds an exchange rate.

What the estimate reports:

| Field | Meaning |
|---|---|
| `shape` | `metered_tokens`. |
| `token_basis` | The measured block's basis — always `provider_reported`, because nothing else is constructible. |
| `prompt_tokens_per_reflection` / `output_tokens_per_reflection` | The worst measured counts the figures rest on. |
| `per_volume[]` | One row per monthly fleet volume: `reflections_per_month`, `tokens_per_month`, `cost_per_1000_reflections` (R6's figure), `cost_per_month`. Defaults are ADR-0016's two volumes, 100/month (the pilot ceiling) and 10,000/month (500 accounts at the default allowance). |
| `monthly_reflection_cap` / `cost_per_account_month` | The per-account allowance priced (default 20, mirroring adepthood's `DEFAULT_MONTHLY_CAP`) and one account's bill at it. |
| `vendor_key_input_token_cap` / `vendor_key_output_token_cap` | ADR-0016 Decision 3 item 4's independent per-activation caps: `tokens spent so far + monthly cap x measured tokens per reflection x 2`, computed by `creek_mcp/provisioning/pool_bundle.py`'s `pool_cap_tokens` — the same function that sizes the real vendor key, so the reported number is the number set. Pass `--tokens-spent-input` / `--tokens-spent-output` to roll a key forward. |

**Cost per 1,000 reflections is the same at every volume.** Metering has no
fixed component, so volume buys no discount. That equality is R6's finding
about O5, not a defect: it is what makes the cap, not the volume, the brake.

Metered amounts are rounded **up** to the micro-unit. A reflection can cost
well under a cent, and rounding a price down understates a bill.

**No spend.** ADR-0016 Decision 4 authorizes none: there is no live price
fetch, no vendor call and no provisioned resource anywhere in this path. Prices
come only from the dated operator file, and the token counts only from a
hermetic or pinned-local measurement.

Metered price-sheet schema. The **values below are deliberately synthetic**;
every figure in ADR-0016's Options table was read through a web-search summary
and must be re-read from a dated sheet before it prices anything:

```json
{
  "observed_on": "2026-10-01",
  "currency": "USD",
  "source": "synthetic-fixture",
  "metered_per_million": {
    "example-model-id": {
      "input_per_million": "1.00",
      "output_per_million": "4.00"
    }
  }
}
```

## Voice-fidelity eval

Issue [#1898](https://github.com/Geoffe-Ga/Creek-Vault/issues/1898), which
unblocks the model choice in
[#1875](https://github.com/Geoffe-Ga/Creek-Vault/issues/1875). The eval is the
*quality* half of this harness: it fills the `quality_score` hook the capacity
report reserved. It gathers evidence and takes no decision — it picks no model
and adds nothing to `APPROVED_MODEL_LICENSES`.

A score is comparable **only within one rubric version and one seed**. Both are
recorded in both artifacts. Across a rubric-version bump the number means
something different, so do not compare it.

### The four dimensions

Reported separately, never averaged into one figure.

| Dimension | Basis | What it measures |
|---|---|---|
| `voice_match` | judge | How close the candidate sits to the synthetic corpus's own voice. |
| `instruction_obedience` | deterministic | Length band, required headings, banned phrases, plus the shipped `creek.lint.checks.voice_fidelity` guard over the candidate written as a draft. |
| `grounding_accuracy` | deterministic | The share of cited fragment ids the corpus actually contains. An invented citation scores zero; an uncited output scores zero too. |
| `refusal_behaviour` | deterministic | Three probes that must each be refused — the care guard's acute-distress path, an above-ceiling prompt, and an INTIMATE fragment. The expectations are computed from `acute_distress_guard` and `tier_allowed`, not written down. |

`voice_match` can report a third basis, `unsupported`, and does so by default.
[#1880](https://github.com/Geoffe-Ga/Creek-Vault/issues/1880) records that the
letter prompt carries no evidence of the writer's voice today, so scoring voice
match against it would be scoring a prompt production never supplies. A caller
states `voice_evidence_present=True` only when the prompt really carried voice
evidence; otherwise the dimension says so and the run's verdict is
`insufficient_evidence` rather than a pass or a failure.

### Blinding

Candidate outputs reach the judge as `BlindedCandidate`, which has **no model
field at all** — the blinding is structural, not a convention. The opaque id is
derived from the run's seed and the candidate's presentation slot, never from
the model tag. The shuffle is seeded, so a run is reproducible; a different seed
changes the order and the labels and nothing else, because every scorer reads
the candidate's text.

### The judge

The default judge is `HermeticJudge`: deterministic, offline, no key, scoring on
four surface-rate features of the text. The whole eval therefore runs in CI with
no API call.

A frontier judge is `RoutedJudge`, selected explicitly. It resolves its provider
through `ModelRouter.resolve_role("voice_judge", PrivacyTier.OPEN)` like every
other stage rather than constructing a client, so the INTIMATE chokepoint and
consent gating apply to it unchanged. Decision 4 on
[#1849](https://github.com/Geoffe-Ga/Creek-Vault/issues/1849) authorizes **no
spend yet**, so the only test that reaches a real judge is in the `live` lane:

```bash
CREEK_EVAL_JUDGE_PROVIDER=anthropic CREEK_EVAL_JUDGE_MODEL=<model> \
  ./scripts/test.sh --live -k live_judge
```

The routed judge's reply is parsed **strictly**: the prompt asks for "a single
number between 0 and 1 and nothing else" under a 16-token ceiling, so the reply
has to *be* a score — optionally behind a `score:` label, optionally followed by
a full stop — or `JudgeUnavailableError` is raised. A chatty judge therefore
refuses honestly instead of having a number mined out of its prose. (An earlier
lenient scan read a perfect `1.0` out of the rubric version `1.0.0`, which the
prompt used to interpolate; the version now rides only on the recorded score.)

### Starting cheap

Per the ratification comment on #1849 the owner's quality bar is **explicitly
not frontier** — a Sonnet 4.x-class model produced voice drafts he was happy
with. So `next_candidate` (and `EvalResult.next_model`) climbs a cheapest-first
ladder **only on a rubric failure**. A pass stops the climb; missing evidence
does not spend money either, because the remedy for an unsupported dimension is
to supply the evidence, not to buy a bigger model.

### Running it

The eval is a library surface, driven from Python; it has no `bench.sh`
subcommand, because supplying candidate outputs needs a model and no spend is
authorized yet.

```python
import tempfile
from pathlib import Path

from creek_mcp.bench.eval.blinding import Candidate
from creek_mcp.bench.eval.judge import HermeticJudge
from creek_mcp.bench.eval.record import write_record
from creek_mcp.bench.eval.rubric import RefusalProbe
from creek_mcp.bench.eval.runner import EvalPlan, run_eval

plan = EvalPlan(
    candidates=(
        Candidate(
            model_tag="cheapest-catalogue-model",
            text="<the candidate's reflection>",
            citations=("voice-0000",),
            refusals={probe: True for probe in RefusalProbe},
        ),
    ),
    judge=HermeticJudge(),
    seed=17,
)
with tempfile.TemporaryDirectory() as tmp:
    result = run_eval(plan, Path(tmp) / "corpus")
write_record(result.record, Path("eval.json"))
print(result.quality.verdict, result.quality.per_dimension)
```

### The corpus, and the two artifacts

The corpus is sixteen entries of prose **written for this harness** in one
invented voice. There is no real journal content in any fixture, prompt or
artifact — #1875's first constraint and #1850's privacy invariant. The root goes
through the same confinement gate as the capacity corpus (`confined_root`):
strictly under the system temp directory, absent or empty, checked before
anything is written, so a real vault is refused untouched with
`CorpusConfinementError`.

There are deliberately **two** artifacts:

- `BenchReport.quality_score` — the score, closed by construction like the rest
  of the report (enums, numbers, a semver). Shareable as evidence.
- `EvalRecord`, written by `write_record` and read back by `load_record` — the
  prompts and raw scores #1875 asks to be recorded. It is honest about being
  prompt-bearing, and everything in it is synthetic. It carries no model
  identity: the payloads it holds are the blinded ones the judge saw.
  `load_record` validates the score through pydantic rather than trusting the
  file, so a hand-edited artifact is refused instead of read back as evidence.

Splitting them is the point. Putting prompts in the report would have meant
weakening its content-free invariant to satisfy a reporting requirement.
