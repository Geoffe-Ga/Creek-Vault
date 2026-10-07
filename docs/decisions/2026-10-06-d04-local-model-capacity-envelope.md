# D04: Local Model Capacity Envelope

- **Status**: Proposed. Not approved. Do not assume approval.
- **Date**: 2026-10-06
- **Driving issue**: [#1850](https://github.com/Geoffe-Ga/Creek-Vault/issues/1850). Downstream: [#1849](https://github.com/Geoffe-Ga/Creek-Vault/issues/1849) (B05, local model runtime).
- **Evidence tooling**: [`creek-tools/docs/bench.md`](../../creek-tools/docs/bench.md), [`creek-tools/creek_mcp/bench/`](../../creek-tools/creek_mcp/bench/)

This record lists the owner decisions that the capacity evidence feeds. It
makes **no numeric recommendation**. Numbers wait for real-hardware runs.

## Context

The managed vault is allocated by `FlyProviderPolicy` defaults in
`creek_mcp/provisioning/fly.py`: one shared CPU, 1024 MB, a 1 GB rootfs and a
5 GB volume. The image does not install or start a model runtime. Three gaps in
the code at `a5d28a5` shape the decisions below.

1. **One shared deadline.** The HTTP deadline is
   `limits.DEFAULT_TIMEOUT_SECONDS`. It applies alongside
   `OllamaProvider.REQUEST_TIMEOUT` and the Adepthood client's total deadline.
   All three are 30 s, and grounding, embedding and generation spend from that
   single budget. The model has no allowance of its own. The harness judges
   against the tighter server-side value and records generation's share of
   each trial separately.
2. **Admission caps with no capacity basis.** `DEFAULT_MAX_CONCURRENCY = 32`
   and `DEFAULT_MAX_PER_CONSUMER = 8` are request-admission numbers. The
   report's `capacity` block compares them with the largest concurrency level
   that measurably fits.
3. **No pinned context window.** Production sends only `num_predict`, so the
   context size is whatever the runtime defaults to. The harness pins `num_ctx`
   on every request. Whether production should pin it too belongs to B05.

## Decisions pending (owner)

1. **Model and license.** Choose the candidate models, their quantizations and
   license ids, and confirm each is eligible as local-only under the B01
   boundary.
2. **Allocation.** Either keep `shared-cpu-1x / 1024 MB` or resize. A resize
   raises spend and is a per-account migration, which needs a volume-safe plan.
3. **Concurrency caps.** Decide whether to lower `limits.py` caps to the
   measured fitting level. This narrows behaviour, so it should wait for real
   numbers.
4. **Sync vs async reflection protocol.** If no candidate fits the shared 30 s
   budget at p95, the options are a per-stage budget or an asynchronous job
   protocol. Both change the `/v1` contract and are ADR-governed.
5. **USD per account-month and the allowance.** Approve the band produced by
   `bench.sh cost` and the monthly reflection allowance (C19). The current
   adepthood default cap is 20 per month.

## Evidence plan (AC17)

For each candidate, run `./scripts/bench.sh reflect --mode live` on each target
tier: Fly `shared-cpu-1x` and `-2x`, `performance`, and 1, 2 and 4 GB. Pass
`--cpu-kind`, sweep `--context-sizes` up to the promised input size, use
`--concurrency 1,2,4,8`, and include at least one idle cycle. Price each tier
with `bench.sh cost` against a dated price sheet. These runs spend money and
need Fly credentials, so they are escalated, not performed by this change.

## Rollback (AC25)

If a deployed envelope stops fitting:

- lower the concurrency caps, or
- suspend the reflection feature.

**Never fall back to a cloud model.** The harness applies the same rule as far
as it can check it. It refuses:

- a provider registered as cloud;
- an Ollama cloud-offload model tag;
- an endpoint that does not resolve to loopback or private addresses, unless
  the operator opts in with `--allow-remote-host` (recorded in the report).

It cannot see past a local address it is pointed at.
