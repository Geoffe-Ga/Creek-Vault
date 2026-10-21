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
| `host` | Capacity facts (CPUs, RAM, kernel, free disk) of the machine the **harness** ran on (`scope: harness`). `describes_model_host` is true only when the model ran there too: fake mode or a loopback endpoint. Free disk is from `--model-store-path` when given (`disk_scope: model_store`), otherwise from the harness's temp directory (`harness_temp`). |
| `budget_seconds` | `min(limits.DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT)`. Grounding and generation share this one budget. |
| `per_sweep.<sweep>.latency` | p50 and p95 over **ok** trials; the error rate over all trials; `generation_p95_s`, the model call's share. |
| `per_sweep.<sweep>.per_phase` | The same aggregates and a verdict per residency phase (`cold`, `warm`, `resume`), so a slow cold start is not hidden inside many warm trials. |
| `per_sweep.<sweep>.verdict` | The worse of its phases. A phase `fits` only when p95 ≤ budget **and** no trial failed; trials that all failed are `exceeds`, not missing data. |
| `verdict` | Any `exceeds` dominates. `insufficient_data` means nothing was measured. |
| `capacity` | The largest concurrency level that fits, together with every level below it. It is compared against `/v1`'s admission caps (`DEFAULT_MAX_CONCURRENCY`, `DEFAULT_MAX_PER_CONSUMER`). It reports and changes nothing. |
| `trials[].outcome` | One of `ok`, `timeout`, `oom`, `disk_full`, `provider_unavailable`, `context_overflow` or `error`. |
| `trials[].model_resident_bytes` | The model's size from `/api/ps`. This is the runtime's figure, not the harness process's RSS. |
| `trials[].prompt_tokens` / `output_tokens` | What the provider reported for that trial, or `null` when it reported nothing. Never derived. |
| `trials[].max_output_tokens` | The output ceiling `reflect_tool` asked the factory for, captured at the model seam. |
| `per_sweep.<sweep>.tokens`, `…per_phase.<phase>.tokens` | Measured prompt and output tokens over the **ok** trials of that group: p50, p95, mean, max, plus `reported_count`, `unreported_count` and `output_cap_exceeded_count`. |
| `tokens_per_reflection` | The run-level measured token block. See below. |
| `quality_score` | Reserved for the B22 quality evaluation. Always `null` here. |

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
