# Target per-stage `llm:` config + assignment mapping

The exact config the `model-setup` skill writes into
`<vault>/00-Creek-Meta/creek_config.yaml`, and how it maps to the operator's
four model assignments. **Config only — no secrets ever appear here or in the
YAML.**

## Assignment → stage mapping

| # | Assignment | Stage(s) | Provider / model | Notes the skill must state |
|---|---|---|---|---|
| 1 | Classification (multi-dim tagging) | `classification`, and `default` as fallback | `ollama` / `qwen3:8b` | Replaces the `mistral` provider-default. Mistral Small 3 is a *speed fallback* for big batches. **Haiku-for-non-Intimate is a GAP** (see #660/#661) — do **not** encode a per-tier split. |
| 2 | Writing / generation | `generation` | `anthropic` / `claude-sonnet-4-6` | Needs `ANTHROPIC_API_KEY` + `CREEK_CLOUD_CONSENT=1` in the **environment**. |
| 3 | Frontend (OpenClaw) | `frontend` | `ollama` / `qwen3:8b` (or `qwen3:14b` on the always-on mini/VPS) | Same weight as #1, different persona/system-prompt; reserved stage (no consumer wired yet). |
| 4 | Writing Desk | `writing_desk` role map | mechanical roles → `ollama`/`qwen3:8b`; `voice_drafter` + `voice_line_editor` → `anthropic`/`claude-sonnet-4-6` | Mechanical roles: `outliner`, `researcher`, `fact_checker`, `structure_editor`, `variant_generator`. |

> Stale premises to correct while explaining: *"default is mistral"* is the
> **Ollama provider** default (used only when `model:` is unset), not a vault
> setting — the skill writes explicit ids, so it is moot. *"config doc shows the
> Haiku string"* is wrong — `claude-haiku-4-5` is a valid id the operator may set
> explicitly, but it is not wired as a default anywhere.

## Target `llm:` block (write verbatim; preserve all other config keys)

```yaml
llm:
  default:        { provider: ollama,    model: qwen3:8b }       # fallback for any unset stage
  classification: { provider: ollama,    model: qwen3:8b }       # local, Intimate-safe
  generation:     { provider: anthropic, model: claude-sonnet-4-6 }
  frontend:       { provider: ollama,    model: qwen3:8b }       # qwen3:14b on the always-on box
  writing_desk:
    outliner:           { provider: ollama,    model: qwen3:8b }
    researcher:         { provider: ollama,    model: qwen3:8b }
    fact_checker:       { provider: ollama,    model: qwen3:8b }
    structure_editor:   { provider: ollama,    model: qwen3:8b }
    variant_generator:  { provider: ollama,    model: qwen3:8b }
    voice_drafter:      { provider: anthropic, model: claude-sonnet-4-6 }
    voice_line_editor:  { provider: anthropic, model: claude-sonnet-4-6 }
```

## Valid values (never invent any)

- **Providers** (`known_providers()`): `anthropic`, `ollama`, `openai`, `gemini`.
  `provider` is validated at config-load; a typo fails the next run, so catch it
  before writing.
- **Model ids the skill may write:** `qwen3:8b`, `qwen3:14b`, `mistral`,
  `claude-sonnet-4-6`. Only with explicit operator opt-in: `claude-haiku-4-5`.
- A cloud stage with no `ANTHROPIC_API_KEY` + `CREEK_CLOUD_CONSENT` in the env is
  *configured but unavailable* — its provider's `available` check fails and the
  desk/pipeline falls back (deterministic, or the local `default`). That is
  expected, not an error.

## Secrets — guided, user-performed, NEVER written

```text
  export ANTHROPIC_API_KEY=sk-ant-...    # your key — paste in YOUR shell; the skill never sees it
  export CREEK_CLOUD_CONSENT=1           # acknowledges cloud egress for generation + voice roles
```
Verify presence only:
```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo "ANTHROPIC_API_KEY: set" || echo "ANTHROPIC_API_KEY: MISSING"
echo "CREEK_CLOUD_CONSENT=${CREEK_CLOUD_CONSENT:-unset}"
```

## Ollama (local secure op — offer, confirm, then run)

```bash
ollama list                 # daemon up; already-pulled models
ollama pull qwen3:8b        # classification / frontend / mechanical desk roles
ollama pull qwen3:14b       # only on the always-on mini/VPS frontend
```
