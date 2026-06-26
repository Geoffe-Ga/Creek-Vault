---
name: model-setup
description: >-
  Configure per-stage LLM model routing for a Creek vault and perform the
  secure setup. Use when the user asks to "set up / configure my models",
  "/model-setup", "which model for classification / writing / frontend / the
  writing desk", "route classification local and writing to Sonnet", "wire up
  per-stage routing", or "pull the ollama models for creek". The skill detects
  the current `llm:` block, the environment (key presence + cloud consent), and
  Ollama reachability; writes the per-stage `llm:` block into
  `<vault>/00-Creek-Meta/creek_config.yaml` (config only); guides the user to
  set `ANTHROPIC_API_KEY` / `CREEK_CLOUD_CONSENT` in their environment; and
  offers to `ollama pull` the local models. It NEVER reads, echoes, logs, or
  writes an API key into any file. Do NOT use to edit routing source/tests
  (that is the per-stage routing code in `creek/classify/llm/`).
metadata:
  author: Geoff
  version: 1.0.0
---

# model-setup

Hand-holds the Creek vault owner through configuring **per-stage LLM model
routing** — and performs the secure operations on their behalf. The routing
infrastructure already exists (`LLMRoutingConfig` / `ModelRouter` / the
`Intimate`-never-cloud chokepoint, epics #642 + #643); this skill only
*configures* it.

## What this does — and what it will NEVER touch

| Does (secure ops it performs) | NEVER touches |
|---|---|
| Reads the current `llm:` block + env/Ollama state | Reads, echoes, logs, or stores an **API key** |
| Writes the per-stage `llm:` block to `creek_config.yaml` (config only) | Writes a key or consent value into **any file** |
| Offers + runs `ollama pull` (local) after confirmation | Sets a secret *for* the user (they do it in their own shell) |

> **Anti-bypass (verbatim — do not paraphrase).** The API key and cloud-consent
> values live in the environment ONLY. This skill MUST NOT read, echo, log,
> print, or write an API key into `creek_config.yaml` or any other file.
> Writing the `llm:` config block and pulling Ollama models are the only
> operations the skill performs on the user's behalf; setting secrets is guided
> but performed by the user in their own shell. If at any point completing a
> step would require putting a secret into a file, STOP and ask the user to set
> it in their environment instead.

The **target config + the assignment→stage mapping** live in
[`references/target-config.md`](references/target-config.md) — read it before
Step 3.

---

## Step 1 — Locate the vault & read current state

1. Resolve the vault. Ask if ambiguous; the config is at
   `<vault>/00-Creek-Meta/creek_config.yaml` (`VAULT_CONFIG_RELPATH`). Find
   candidates with `find ~ -name creek_config.yaml 2>/dev/null` (ignore
   `/tmp` pytest hits).
2. Read the current `llm:` block (it may be a legacy flat block — `{provider,
   model, …}` with no stage keys — which `LLMRoutingConfig` auto-promotes to
   `default`).
3. Detect the environment **without reading any value**:
   ```bash
   [ -n "$ANTHROPIC_API_KEY" ] && echo "ANTHROPIC_API_KEY: set" || echo "ANTHROPIC_API_KEY: MISSING"
   echo "CREEK_CLOUD_CONSENT=${CREEK_CLOUD_CONSENT:-unset}"   # also honors legacy CREEK_ANTHROPIC_CONSENT
   ```
4. Detect the local provider: `ollama list` (is the daemon up; what is already
   pulled). If `ollama` is not installed, note it for Step 5.

## Step 2 — Explain the per-stage plan

Show the **assignment → stage** table from
[`references/target-config.md`](references/target-config.md): classification →
local `qwen3:8b`; generation → `anthropic`/`claude-sonnet-4-6`; frontend → local
`qwen3:8b` (or `qwen3:14b` on an always-on box); writing-desk roles split
local/Sonnet. State plainly which stages will use a **cloud** provider (so the
user knows where consent + key are required) and which are local.

## Step 3 — Write the `llm:` block (the secure config op)

1. Show the **target `llm:` YAML** (`references/target-config.md`) and a **diff**
   against the current block.
2. Validate every `provider` against the live registry before writing — only
   `anthropic` / `ollama` / `openai` / `gemini` are valid (`provider` is checked
   at config-load time, so a typo would otherwise fail the next run). Use only
   the model ids `qwen3:8b` / `qwen3:14b` / `mistral` / `claude-sonnet-4-6` (and
   `claude-haiku-4-5` only if the user explicitly opts in). **Never invent ids.**
3. Get explicit confirmation, then **round-trip the YAML** so every unrelated
   key survives — only the `llm:` block changes. There is no key field in the
   config; if a step ever tempts you to add one, STOP (see anti-bypass).

## Step 4 — Guide secrets into the environment (never YAML)

The generation stage and the `voice_drafter` / `voice_line_editor` desk roles
use Anthropic, so they need a key **and** consent **in the environment**. Print
this (and optionally offer to run the `export`s as a session prefix), but
**never read the value back**:

```text
  export ANTHROPIC_API_KEY=sk-ant-...    # your key — paste in YOUR shell; the skill never sees it
  export CREEK_CLOUD_CONSENT=1           # acknowledges cloud egress for generation + voice roles
```

Then verify **presence only**:
```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo "ANTHROPIC_API_KEY: set" || echo "ANTHROPIC_API_KEY: MISSING"
echo "CREEK_CLOUD_CONSENT=${CREEK_CLOUD_CONSENT:-unset}"
```
For a persistent setup, point the user at their shell profile (`~/.zshrc`) — you
may *suggest* the lines, but they add them. Tell the user that in a Claude Code
session they can prefix a command with `! ` to run an interactive `export` so
its effect lands in this session without the skill ever seeing the value.

## Step 5 — Local provider (Ollama)

Local pulls are a secure op — **offer, confirm, then run**:
```bash
ollama list                 # daemon up? what is already pulled?
ollama pull qwen3:8b        # classification / frontend / mechanical desk roles
ollama pull qwen3:14b       # only on the always-on mini/VPS frontend
```
If `ollama` is missing, link the install (https://ollama.com) — do not auto-install a
system package without asking.

## Step 6 — Verify & summarize

1. Per-stage availability: the configured cloud stages are usable only when the
   key + consent are present; local stages need a reachable Ollama. Surface any
   stage that would fall back.
2. Optional live smoke (only with the key + consent set), per the README:
   `cd creek-tools && ./scripts/test.sh --integration -k anthropic` (or
   `CREEK_SMOKE_MODEL=<id> ... -k <provider>`).
3. Print a **per-stage summary table**: stage → provider → model → ready? Then
   tell the user the Intimate guarantee below is already in force.

---

## The Intimate-never-cloud guarantee is automatic

`ModelRouter._enforce_local_for_intimate` is the single tier gate (#647): an
`Intimate`-tier fragment bound for a cloud provider is **redirected to the local
`default`** (with a WARNING), or — if `default` is also cloud — the run fails
loudly with `IntimateRoutingError` rather than egressing. **Explain this; never
ask the user to enforce it.** Keep `default` local (`ollama`) so the redirect
always has somewhere safe to land.

## Known gap — per-tier classification routing (do NOT promise it)

Assignment #1 wants "Haiku for non-Intimate, local for Intimate" in one classify
run. **This is not yet wired.** Every classification call site resolves the stage
with **no tier** (`resolve("classification")`, tier `None`), so the chokepoint
never fires per fragment during classification. Configure `classification` to a
single safe local model (`qwen3:8b`) and **say so honestly** — the per-fragment
tier threading is the same work the author-desk issues **#660** (tier-filter
retrieval) and **#661** (route the voice call by content tier) did for voicing;
classification needs the analogous wiring before a tier split is possible. Do
**not** write a config that pretends to split classification by tier.

## "Five assignments, four listed"

The operator's note says *five* but lists *four*. The missing fifth is
embeddings/linking, which are **deliberately local-only** (sentence-transformers;
`EmbeddingsConfig`, ADR-0004) — there is no cloud backend selection there by
design, so there is nothing to route. Mention this so the user isn't left
wondering.

## See also

- `creek-cli` — how to actually run `creek` (uv, working dir, consent) once
  configured; do not duplicate its invocation contract.
- README "Per-stage model routing" + ADR-0003 (provider decoupling) / ADR-0004
  (embeddings stay local) for the authoritative reference.
