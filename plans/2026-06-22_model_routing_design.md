# Per-stage LLM model routing — design (Issue #644)

**SPEC:** `plan/2026-06-22_CREEK_VAULT_MODEL_ROUTING_PLAN.md` · **Epic:** #642 ·
**Status:** awaiting maintainer approval (gates #645–#650).

All paths are relative to `creek-tools/`. Every claim is `file:line`-cited
against the tree at the time of writing; verify before implementing.

---

## 1. Current-state map — every LLM call site

LLM routing today is **single-provider-global**: one `LLMConfig` instance
(`creek/config.py:92`) on `CreekConfig.llm` (`creek/config.py:1274`) is read by
every call site. There is no per-stage, per-tier, or (except the author voice
tier) per-agent override.

| # | Call site | `file:line` | Reads | Stage |
|---|---|---|---|---|
| 1 | `LLMClassifier(config=config.llm)` | `creek/pipeline.py:169` | `config.llm` | classification |
| 2 | `LLMClassifier(config=config.llm)` | `creek/cli.py:1145` | `config.llm` | classification |
| 3 | `LLMClassifier(config.llm)` | `creek/cli.py:2501` | `config.llm` | classification (review) |
| 4 | `LLMClassifier(config=config.llm)` | `creek/classify/classify_engine.py:213` | `config.llm` | classification |
| 5 | provider string for error/preflight | `creek/classify/classify_engine.py:223-224`, `creek/cli.py:889` | `config.llm.provider` | classification / preflight |
| 6 | `LLMClassifier(config)` | `creek/classify/weighted.py:836` | `LLMConfig` (passed down) | classification (weighted) |
| 7 | `LLMClassifier(config)` | `creek/classify/prompt.py:334` | `LLMConfig` (passed down) | ontology-detection |
| 8 | `detect_ontology(prompt, config.llm)` | `creek/cli.py:2600` | `config.llm` | ontology-detection |
| 9 | `default_llm(config.llm)` | `creek/cli.py:1299` | `config.llm` | generation (compile/draft) |
| 10 | `AnthropicProvider(config)` inside `default_llm()` | `creek/compile/engine.py:523` | `LLMConfig` | generation (**hard-wired Anthropic**) |
| 11 | `AnthropicProvider(config=config.llm)` (compost verifier) | `creek/cli.py:4096` | `config.llm` | generation (compost) |
| 12 | `AuthorLLMClient.from_config(...)` → `build_provider(effective)` | `creek/author/client.py:56,79` | `LLMConfig` + `AuthorConfig.voice_model` | generation (Writing Desk) |

**The single construction funnel.** Every provider is built by one factory:
`build_provider(config: LLMConfig) -> LLMProvider` (`creek/classify/llm/providers.py:1116`),
dispatching on `_PROVIDER_REGISTRY` (`providers.py:1105`). `LLMClassifier`
calls it internally (`creek/classify/llm/orchestrator.py:118`); `author/client.py:79`
calls it directly. Two sites bypass the factory and hard-wire Anthropic
(`compile/engine.py:523`, `cli.py:4096`) — the routing work must move these onto
the factory.

**Cloud/local already discriminated.** Each provider class carries a class-level
`is_cloud` flag — `AnthropicProvider` `True` (`providers.py:169`), `OllamaProvider`
`False` (`providers.py:534`), `OpenAIProvider` `True` (`providers.py:631`),
`GeminiProvider` `True` (`providers.py:878`) — surfaced by
`provider_is_cloud(provider: str) -> bool` (`providers.py:1143`). **This is the
predicate the Intimate-never-cloud rule will use.**

**Tier flow today.** Privacy tier does **not** reach any provider constructor.
Two upstream gates already exist:
- Classification: `privacy_filter.py` drops `INTIMATE` fragments *before* the LLM
  call (`creek/classify/llm/privacy_filter.py:134`: `if tier == PrivacyTier.INTIMATE
  and not _allows_intimate(override): continue`).
- Generation/voice: `Fragment.voice_proxy_eligible` (`creek/models.py:821`) is
  `False` for `INTIMATE`, and `generate/voice.py:228` excludes it.

So the leak risk the SPEC guards against is **latent** today (the upstream gates
mostly cover it) but unenforced *at the routing layer* — exactly why the SPEC
wants it pinned at one chokepoint, with `--include-tier intimate` overrides and
future call sites in mind.

**Consent gate.** `has_cloud_consent()` (`creek/classify/llm/consent.py:28`) and
`require_cloud_consent()` (`consent.py:63`); each cloud provider checks it in
`_missing_prerequisite()` (`providers.py:232,681,926`); a CLI preflight gates the
whole run (`cli.py:857-912`). The router does **not** replace this — it composes
with it.

---

## 2. Proposed config schema

Mirror the decoupled-block precedent `EmbeddingsConfig` (`creek/config.py:161`,
field at `config.py:1277`). Introduce one new model, `LLMRoutingConfig`, that
**accepts either the legacy flat shape or the new per-stage map** and replaces
the type of `CreekConfig.llm`. `LLMConfig` itself is unchanged.

```python
# creek/config.py — new model; LLMConfig (config.py:92) stays as-is.

_STAGE_KEYS = ("default", "classification", "generation", "frontend")

class LLMRoutingConfig(BaseModel):
    """Per-stage LLM routing (#642). Backward-compatible with a flat llm block.

    A YAML `llm:` block is accepted in two shapes:
      * legacy flat — `{provider, model, ...}` (an LLMConfig) → promoted to
        `default`, so every stage resolves to it (pre-#642 behaviour);
      * per-stage map — `{default, classification, generation, frontend,
        writing_desk}`.
    Unset stages fall back to `default`; `default` falls back to a plain
    `LLMConfig()` (local Ollama).
    """

    default: LLMConfig = Field(default_factory=LLMConfig)
    classification: LLMConfig | None = None
    generation: LLMConfig | None = None
    frontend: LLMConfig | None = None
    writing_desk: dict[str, LLMConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_flat(cls, data: object) -> object:
        """Promote a legacy flat LLMConfig mapping to `{default: <it>}`.

        Discriminates on keys: any `LLMConfig` field present at the top level
        (`provider`, `model`, `ollama_url`, `api_base`, `batch_size`,
        `max_concurrent`, `unclassified_threshold`) with no stage key means the
        operator wrote the old flat block — wrap it as `default`.
        """
        if not isinstance(data, dict):
            return data
        stage_keys = set(_STAGE_KEYS) | {"writing_desk"}
        legacy_keys = set(LLMConfig.model_fields)
        if data.keys() & legacy_keys and not (data.keys() & stage_keys):
            return {"default": data}
        return data

    def for_stage(self, stage: str) -> LLMConfig:
        """Resolve a stage to its LLMConfig, falling back to `default`."""
        return getattr(self, stage, None) or self.default
```

`CreekConfig.llm` changes type only:

```python
# creek/config.py:1274
llm: LLMRoutingConfig = Field(default_factory=LLMRoutingConfig)   # was: LLMConfig
```

Sample `creek_config.yaml` (new shape):

```yaml
llm:
  default:        { provider: ollama,    model: qwen3:8b }
  classification: { provider: ollama,    model: qwen3:8b }      # Intimate-safe
  generation:     { provider: anthropic, model: claude-sonnet-4-6 }
  frontend:       { provider: ollama,    model: qwen3:14b }
  writing_desk:
    outliner:      { provider: ollama,    model: qwen3:8b }
    voice_drafter: { provider: anthropic, model: claude-sonnet-4-6 }
```

Legacy block still valid (promoted to `default`, every stage resolves to it):

```yaml
llm: { provider: ollama, model: qwen3:8b }
```

---

## 3. Routing chokepoint

One class, `ModelRouter`, is the sole resolver of `(stage, tier) → LLMConfig`.
Provider construction stays in `build_provider`; the router decides *which
config* to hand it. Proposed home: `creek/classify/llm/router.py` (beside
`build_provider` / `provider_is_cloud`).

```python
# creek/classify/llm/router.py
from creek.classify.llm.providers import provider_is_cloud, build_provider
from creek.models import PrivacyTier

class ModelRouter:
    """Resolve (stage, tier) → LLMConfig — the ONLY place tier gates provider."""

    def __init__(self, routing: LLMRoutingConfig) -> None:
        self._routing = routing

    def resolve(self, stage: str, tier: PrivacyTier | None = None) -> LLMConfig:
        cfg = self._routing.for_stage(stage)
        return self._enforce_local_for_intimate(cfg, tier)

    def resolve_role(self, role: str, tier: PrivacyTier | None = None) -> LLMConfig:
        cfg = self._routing.writing_desk.get(role) or self._routing.for_stage("generation")
        return self._enforce_local_for_intimate(cfg, tier)

    def _enforce_local_for_intimate(
        self, cfg: LLMConfig, tier: PrivacyTier | None
    ) -> LLMConfig:
        """Intimate-never-cloud — enforced here and NOWHERE else."""
        if tier == PrivacyTier.INTIMATE and provider_is_cloud(cfg.provider):
            local = self._routing.for_stage("default")
            if provider_is_cloud(local.provider):
                raise IntimateRoutingError(stage_provider=cfg.provider)  # fail loud
            return local
        return cfg

    def provider_for(self, stage: str, tier: PrivacyTier | None = None):
        return build_provider(self.resolve(stage, tier))
```

Enforcement semantics (SPEC §3, Requirements): when an `INTIMATE` fragment hits a
stage configured for a cloud provider, the router **redirects to the local
default**; if even the default is cloud, it **raises loudly** (`IntimateRoutingError`)
rather than silently egressing. The decision uses the existing
`provider_is_cloud` (`providers.py:1143`) — no new cloud/local list. This is the
*single* place tier influences provider choice; call sites never re-check tier.

---

## 4. Writing Desk role map

The role→model map is `LLMRoutingConfig.writing_desk: dict[str, LLMConfig]`
(§2), resolved by `ModelRouter.resolve_role(role, tier)` (§3). Fallback chain:
**role entry → `generation` stage → `default`**, then the Intimate chokepoint.

| Role | Config key | Default tier (unset) |
|---|---|---|
| outliner | `writing_desk.outliner` | generation |
| researcher | `writing_desk.researcher` | generation |
| fact_checker | `writing_desk.fact_checker` | generation |
| structure_editor | `writing_desk.structure_editor` | generation |
| variant_generator | `writing_desk.variant_generator` | generation |
| voice_drafter | `writing_desk.voice_drafter` | generation |
| voice_line_editor | `writing_desk.voice_line_editor` | generation |

Backward-compat with the existing per-agent tiers: `AuthorConfig.voice_model`
(`config.py:913`), `synthesis_model` (`config.py:919`), `reflection_model`
(`config.py:926`) stay honored. `resolve_voice_model(author, llm)`
(`author/client.py:20`) keeps working; the recommended forward path is that the
`voice_drafter`/`voice_line_editor` roles supersede `voice_model` when both are
set (documented in #650), with `voice_model` as the fallback so no existing
config breaks. (Confirm precedence in #648 — flagged in §6.)

---

## 5. Migration & tests

**Backward compatibility.**
- YAML: the `_accept_legacy_flat` validator (§2) promotes a flat `llm` block to
  `default`; every stage resolves to it → identical to today.
- Call sites (#646): each reads `router.resolve("classification" | "generation",
  tier)` instead of `config.llm`; for a flat config this returns the same object.
- Author (#649): `resolve_role` falls back to `generation`, which for a flat
  config is `default` = the old `llm` — `AuthorConfig.voice_model` still applies.
- The two hard-wired Anthropic sites (`compile/engine.py:523`, `cli.py:4096`)
  move onto `router.resolve("generation", tier)` so they stop ignoring config.

**Env-var override (verified — no shim needed).** Contrary to the SPEC's
"nested via `__`" assumption, `CREEK_LLM__PROVIDER` does **not** work today:
`CreekConfig.model_config` (`config.py:1226`) sets `env_prefix="CREEK_"` but **no
`env_nested_delimiter`**, so `CREEK_LLM__PROVIDER` is silently ignored (verified:
it leaves `llm.provider` at the `ollama` default). The only working env override
for the block is the **JSON-string form** `CREEK_LLM='{"provider":"anthropic"}'`.
That JSON is parsed before validation and flows through `_accept_legacy_flat`,
which promotes the flat mapping to `{default: …}` — so the real back-compat
surface is preserved by the model validator **with no extra shim**. The new shape
also works via JSON: `CREEK_LLM='{"default":{"provider":"anthropic"}}'`. A
regression test pins `CREEK_LLM='{"provider":…}'` → `resolve(stage).provider`.

**Tests (new, ≥90% branch coverage on new code):**
1. `test_legacy_flat_llm_block_resolves_every_stage` — a flat `llm` config →
   `resolve(stage)` returns it for `classification`/`generation`/`frontend`.
2. `test_per_stage_routing_resolves_independently` — classification→ollama,
   generation→anthropic resolve to distinct providers.
3. `test_unset_stage_falls_back_to_default`.
4. **`test_intimate_tier_never_routes_to_cloud`** (the guard): `INTIMATE` +
   stage configured for anthropic → `resolve` returns a local provider and **no
   `AnthropicProvider` is constructed** (assert on construction, not a string);
   the negative-control breaks the rule and asserts the test fails.
5. `test_intimate_with_cloud_only_config_raises` — `IntimateRoutingError` when
   even `default` is cloud (fail-loud path).
6. `test_non_intimate_honors_configured_cloud` — `OPEN`/`PERSONAL` still route to
   the configured cloud provider.
7. `test_role_resolution_fallback_chain` and `test_role_resolution_is_tier_gated`
   (#648).
8. `test_author_voice_model_back_compat` — legacy `AuthorConfig.voice_model`
   unchanged (#649).

---

## 6. Resolved decisions (maintainer-approved 2026-06-24)

1. **Env-var back-compat — NO SHIM (premise corrected).** The approval asked to
   "keep `CREEK_LLM__PROVIDER` working," but verification (see §5) shows it never
   worked: no `env_nested_delimiter` is configured, so `CREEK_LLM__PROVIDER` is
   silently ignored today. The genuine env back-compat surface is the JSON-string
   form `CREEK_LLM='{"provider":…}'`, which `_accept_legacy_flat` already
   preserves by promoting it to `default`. **No shim is built** (it would
   preserve a phantom); #645 instead adds a regression test pinning the JSON-flat
   env override. The operator's intent — "existing env-based setups keep working"
   — is honored. (If a future `CREEK_LLM__DEFAULT__PROVIDER` nested override is
   desired, that is a separate opt-in: set `env_nested_delimiter="__"`.)
2. **Role vs `voice_model` precedence — ROLE WINS.** When both
   `writing_desk.voice_drafter` and `AuthorConfig.voice_model` are set, the role
   entry wins; `voice_model` is the fallback. No existing config breaks (#648).
3. **`INTIMATE` enforcement — REDIRECT TO LOCAL + WARN.** When an `INTIMATE`
   fragment hits a stage configured for a cloud provider, the chokepoint
   redirects to the local `default` provider **and emits one WARNING** (audit
   trail). It raises `IntimateRoutingError` only when even `default` is cloud
   (no safe local exists). Never emits a cloud call for `INTIMATE` (#647).
4. **Stage taxonomy — FOLD.** Ontology-detection (`prompt.py:334`,
   `cli.py:2600`) folds into `classification`; compost-verification
   (`cli.py:4096`) folds into `generation`. Stage set stays small
   (`default` / `classification` / `generation` / `frontend`); revisit only if an
   operator needs them split.
5. **`frontend` stage — SCHEMA ONLY.** Define the `frontend` field now
   (forward-compat for OpenClaw via `creek-tools-mcp`) but wire no call site
   until the frontend lands (#645 defines it; no consumer yet).
