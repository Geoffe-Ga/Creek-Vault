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
| context | `--context-sizes 512,2048` | Latency at each entry size, in words. Sizes are refused unless size + a 1,536-token prompt-overhead allowance + `--num-predict` fits in `--num-ctx`. Words can tokenize to several tokens, so a size that passes can still overflow; a live run reports that trial as `context_overflow` (from Ollama's `prompt_eval_count`), never `ok`. |
| concurrency | `--concurrency 1,2,4` | Runs `N` reflections in flight together on `N` threads, the way `/v1` serves reads. |
| idle | `--idle-cycles`, `--idle-seconds` | Evict, wait, then resume. |

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
| `budget_seconds` | `min(limits.DEFAULT_TIMEOUT_SECONDS, OllamaProvider.REQUEST_TIMEOUT)`. Grounding and generation share this one budget. |
| `per_sweep.<sweep>.latency` | p50 and p95 over **ok** trials; the error rate over all trials; `generation_p95_s`, the model call's share. |
| `per_sweep.<sweep>.per_phase` | The same aggregates and a verdict per residency phase (`cold`, `warm`, `resume`), so a slow cold start is not hidden inside many warm trials. |
| `per_sweep.<sweep>.verdict` | The worse of its phases. A phase `fits` only when p95 ≤ budget **and** no trial failed; trials that all failed are `exceeds`, not missing data. |
| `verdict` | Any `exceeds` dominates. `insufficient_data` means nothing was measured. |
| `capacity` | The largest concurrency level that fits, together with every level below it. It is compared against `/v1`'s admission caps (`DEFAULT_MAX_CONCURRENCY`, `DEFAULT_MAX_PER_CONSUMER`). It reports and changes nothing. |
| `trials[].outcome` | One of `ok`, `timeout`, `oom`, `disk_full`, `provider_unavailable`, `context_overflow` or `error`. |
| `trials[].model_resident_bytes` | The model's size from `/api/ps`. This is the runtime's figure, not the harness process's RSS. |
| `quality_score` | Reserved for the B22 quality evaluation. Always `null` here. |

## Cost model

The cost model is arithmetic over an operator-supplied price sheet. It is never
a measurement: every estimate says `"kind": "model"`. It fetches nothing.

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
