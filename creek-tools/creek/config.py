"""Creek configuration module — loads creek_config.yaml to Pydantic Settings models.

Provides typed configuration for every subsystem in the Creek pipeline:
LLM, embeddings, OCR, linking, classification, redaction, Google Drive,
and source paths. Configuration values are loaded from a YAML file and
can be overridden by environment variables prefixed with ``CREEK_``.

API keys (e.g. ``ANTHROPIC_API_KEY``) are **never** stored in the YAML
file — they must come from environment variables.
"""

import logging
import os
import re
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from creek.classify.llm.router import ModelRouter

logger = logging.getLogger(__name__)

CONFIG_PATH_ENV_VAR = "CREEK_CONFIG"
"""Environment variable that, when set, supplies the default config path.

Honoured by :func:`load_config` when no explicit ``config_path``
argument is given. Subprocess-friendly: any spawned process inherits
the variable, so callers (e.g. CrawDad spawning ``creek-tools-mcp``)
can keep the configured vault config discoverable regardless of cwd.
"""

VAULT_CONFIG_RELPATH = Path("00-Creek-Meta") / "creek_config.yaml"
"""Canonical location of ``creek_config.yaml`` inside a scaffolded vault.

Mirrors the path written by ``creek init --vault <path>`` so callers
can resolve a vault's config without re-deriving the layout.
"""


def resolve_config_path(
    vault: Path | None,
    explicit: Path | None,
) -> Path | None:
    """Pick the effective ``creek_config.yaml`` path from CLI/env/vault inputs.

    Resolution order, highest precedence first:

    1. ``explicit`` — typically a ``--config <path>`` CLI flag.
    2. The ``CREEK_CONFIG`` environment variable, when set and
       non-empty. A non-existent target raises ``FileNotFoundError``,
       preserving the INC-008 "explicit env var declares intent"
       contract even when ``--vault`` is also supplied (issue #322).
    3. ``<vault>/00-Creek-Meta/creek_config.yaml``, when *vault* is
       supplied and the file exists.
    4. ``None`` — callers fall back to :func:`load_config`'s historical
       cwd-default + warning behaviour.

    Args:
        vault: Vault root from ``--vault``, or ``None``.
        explicit: Operator-supplied config path (e.g. ``--config``), or
            ``None``.

    Returns:
        The resolved config path, or ``None`` to defer to
        :func:`load_config`'s default discovery.

    Raises:
        FileNotFoundError: When ``CREEK_CONFIG`` names a path that does
            not exist on disk.
    """
    if explicit is not None:
        return explicit
    env_value = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
    if env_value:
        env_path = Path(env_value)
        if not env_path.exists():
            msg = (
                f"{CONFIG_PATH_ENV_VAR} points to {env_path}, but no "
                "file exists there. Unset the environment variable or "
                "correct the path."
            )
            raise FileNotFoundError(msg)
        return env_path
    if vault is not None:
        candidate = vault / VAULT_CONFIG_RELPATH
        if candidate.exists():
            return candidate
    return None


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: str = "ollama"
    """LLM backend — ``ollama`` (local), ``anthropic``, ``openai``, ``gemini``,
    or ``enclave`` (attested GPU-CC)."""

    model: str | None = None
    """Model identifier recognised by the chosen provider.

    ``None`` (the default) means "use the provider's own default model" —
    each provider resolves it locally (e.g. ``mistral`` for Ollama). An
    explicit value is always honored verbatim, whatever the provider.
    """

    ollama_url: str = "http://localhost:11434"
    """Base URL for the Ollama API server."""

    api_base: str | None = None
    """Optional base-URL override for a cloud provider's SDK (e.g. an
    OpenAI-compatible gateway). ``None`` lets the SDK use its default endpoint
    or its own ``*_BASE_URL`` environment variable. Never holds a secret."""

    api_key_env: str | None = None
    """BYOK: name of the environment variable holding this stage's API key.

    "Bring your own key" — point the ``generation`` (or any) stage at a
    **user-supplied** provider key held in a named environment variable (e.g.
    ``CREEK_BYOK_ANTHROPIC_KEY``) instead of the operator's default
    (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GOOGLE_API_KEY``). This holds
    a **variable name, never a key value**, so the key stays in the environment
    only — never persisted in config or logged. ``None`` uses the provider's
    default env var (behaviour unchanged). Does not relax the ``ModelRouter``
    INTIMATE chokepoint: BYOK only affects OPEN/PERSONAL cloud stages; INTIMATE
    is still redirected to a local/enclave provider before any key is read."""

    enclave_url: str | None = None
    """Base URL of the attested GPU-CC enclave endpoint (provider ``enclave``).

    The enclave provider verifies remote attestation at ``{enclave_url}/attestation``
    before sending any prompt to ``{enclave_url}/generate``. ``None`` means the
    enclave is unconfigured and the provider refuses to run. Never holds a secret."""

    enclave_expected_measurement: str | None = None
    """The attestation measurement the enclave must present before any egress.

    This is the "attest, then mount" policy: the ``enclave`` provider refuses to
    send a prompt unless the enclave's attested measurement equals this value, so
    ``is_cloud=False`` is defended in code — a mis-set flag cannot leak intimate
    content because the measurement gate stands in front of every call. ``None``
    means no attestation policy is configured, and the provider fails closed."""

    enclave_attestation_pubkey: str | None = None
    """Hex-encoded Ed25519 **public** key that anchors the enclave attestation.

    The root of trust: the enclave signs each attestation quote (its measurement
    bound to a fresh, caller-supplied nonce) with the matching private key, and
    the provider verifies that signature against this public key before any prompt
    egresses. This is what makes the attestation *cryptographic* rather than a
    bare string match — an impersonator without the private key cannot forge a
    quote, and the nonce defeats replay. It is a **public** key, so it is not a
    secret; ``None`` means no trust anchor is configured and the provider fails
    closed. See ADR-0006."""

    batch_size: int = Field(default=50, ge=1)
    """Number of items to process per LLM batch call.

    Must be at least 1: a value below 1 would feed ``encode(batch_size=0)`` in
    the embeddings pass (``creek/link/embeddings.py``) and process nothing, so
    the misconfig fails fast at load time instead of silently doing no work."""

    max_concurrent: int = Field(default=5, ge=1)
    """Maximum number of concurrent LLM requests.

    Must be at least 1: a value below 1 would build a ``BoundedSemaphore(0)``
    in the per-provider classify pool (PR #799) and hang forever on the first
    ``acquire()``, so the misconfig fails fast at load time instead."""

    unclassified_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    """Per-dimension confidence floor below which Mode / Orientation /
    Dosage default to ``unclassified`` rather than the model's pick (FEAT-017).

    Frequency, Phase, and Voice Register are not gated by this knob —
    they are more stable signals empirically and using them is the
    point of having an LLM pass at all. The 0.55 default is deliberately
    lenient; tighten it once a calibration set exists (FEAT-017b) and
    the per-dimension agreement rate on your corpus is known.
    """

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        """Fail fast at config-load on an unregistered provider.

        Validates against the live provider registry so a typo like
        ``provider: anthropics`` raises here — at parse time — instead of
        surfacing as a ``ValueError`` from ``build_provider`` mid-run. The
        valid set is read from the registry, never re-listed, so it cannot
        drift.

        Args:
            value: The configured provider string.

        Returns:
            *value* unchanged when it names a registered provider.

        Raises:
            ValueError: When *value* names no registered provider.
        """
        from creek.classify.llm.providers import known_providers

        known = known_providers()
        if value not in known:
            joined = ", ".join(known)
            msg = f"unknown LLM provider {value!r}; expected one of: {joined}"
            raise ValueError(msg)
        return value

    @field_validator("api_key_env")
    @classmethod
    def _api_key_env_is_a_name(cls, value: str | None) -> str | None:
        """Reject an ``api_key_env`` that looks like a pasted key, not a var name.

        The field's whole safety property is "holds a variable name, never a
        secret." An accidentally-pasted key would be serialized into
        ``creek_config.yaml`` and echoed into "not set" error messages, so it is
        rejected fail-fast at config-load: the value must be a valid environment
        variable identifier and must not begin with a known provider-key prefix.

        Args:
            value: The configured ``api_key_env`` (``None`` when unset).

        Returns:
            The trimmed variable name, or ``None`` when unset/blank.

        Raises:
            ValueError: When the value is not a plausible env var name.
        """
        if value is None:
            return None
        name = value.strip()
        if not name:
            return None
        looks_like_key = name.startswith(("sk-", "sk_", "AIza", "AKIA"))
        if looks_like_key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            msg = (
                "api_key_env must be an environment variable NAME "
                "(e.g. CREEK_BYOK_ANTHROPIC_KEY), not an API key value"
            )
            raise ValueError(msg)
        return name


_LLM_SCALAR_STAGES = ("default", "classification", "generation", "frontend")
"""Stages that resolve to a single :class:`LLMConfig` (``writing_desk`` is a
role *map*, handled separately by the router in #648)."""


class LLMRoutingConfig(BaseModel):
    """Per-stage LLM routing (#642), backward-compatible with a flat block.

    A vault's ``llm:`` block is accepted in two shapes:

    * **legacy flat** — ``{provider, model, …}`` (an :class:`LLMConfig`) is
      promoted to ``default`` so every stage resolves to it (pre-#642
      behaviour, and the shape an env override ``CREEK_LLM='{"provider":…}'``
      produces);
    * **per-stage map** — ``{default, classification, generation, frontend,
      writing_desk}``.

    Unset scalar stages fall back to :attr:`default`; :attr:`default` itself
    falls back to a plain :class:`LLMConfig` (local Ollama). The per-stage
    *resolution* and the ``Intimate``-never-cloud rule live in
    :class:`~creek.classify.llm.router.ModelRouter`, not here — this model only
    holds and normalises the config.
    """

    default: LLMConfig = Field(default_factory=LLMConfig)
    """The fallback config for any stage without its own entry."""

    classification: LLMConfig | None = None
    """Override for the classification stage; ``None`` falls back to default."""

    generation: LLMConfig | None = None
    """Override for the generation stage; ``None`` falls back to default."""

    frontend: LLMConfig | None = None
    """Reserved for the OpenClaw frontend (schema-only until it lands, #642)."""

    writing_desk: dict[str, LLMConfig] = Field(default_factory=dict)
    """Writing Desk role -> model map (resolved by the router in #648)."""

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_flat(cls, data: object) -> object:
        """Promote a legacy flat ``LLMConfig`` (or its mapping) to ``default``.

        A bare :class:`LLMConfig` instance — e.g.
        ``CreekConfig(llm=LLMConfig(...))`` — or a flat mapping carrying any
        :class:`LLMConfig` field with no per-stage key is the old single-block
        shape; wrap it as ``{"default": <it>}`` so it keeps working unchanged.

        Args:
            data: The raw input to validation (mapping, ``LLMConfig``, or other).

        Returns:
            ``data`` unchanged, or ``{"default": data}`` when it is a legacy
            flat block.
        """
        if isinstance(data, LLMConfig):
            return {"default": data}
        if not isinstance(data, dict):
            return data
        stage_keys = {*_LLM_SCALAR_STAGES, "writing_desk"}
        legacy_keys = set(LLMConfig.model_fields)
        keys = set(data)
        if keys & legacy_keys and not (keys & stage_keys):
            return {"default": data}
        return data

    def for_stage(self, stage: str) -> LLMConfig:
        """Return the :class:`LLMConfig` for *stage*, falling back to default.

        Args:
            stage: A scalar stage name (``default`` / ``classification`` /
                ``generation`` / ``frontend``). Any other value (including a
                role name or a typo) safely resolves to :attr:`default`.

        Returns:
            The stage's configured :class:`LLMConfig`, or :attr:`default`.
        """
        if stage in _LLM_SCALAR_STAGES:
            return getattr(self, stage) or self.default
        return self.default

    def iter_stage_configs(self) -> "Iterator[tuple[str, LLMConfig]]":
        """Yield ``(stage_label, LLMConfig)`` for every configured stage.

        ``default`` first, then each set scalar-stage override, then every
        ``writing_desk`` role (labelled ``writing_desk.<role>``). The label lets
        callers name the offending stage in messages.

        Yields:
            ``(label, config)`` pairs across all configured stages.
        """
        yield "default", self.default
        for stage in _LLM_SCALAR_STAGES:
            if stage != "default" and (cfg := getattr(self, stage)) is not None:
                yield stage, cfg
        for role, cfg in self.writing_desk.items():
            yield f"writing_desk.{role}", cfg

    def all_configs(self) -> list[LLMConfig]:
        """Return every distinct :class:`LLMConfig` this routing can resolve.

        Used by the cloud-consent preflight so a cloud provider configured for
        *any* stage (not just ``default``) is caught before the run starts.

        Returns:
            ``default`` plus each set scalar-stage override and every
            ``writing_desk`` role config.
        """
        return [cfg for _, cfg in self.iter_stage_configs()]


class EmbeddingsConfig(BaseModel):
    """Embedding model configuration.

    Embeddings are local-only by design — unlike ``llm.provider``, there is no
    cloud backend selection here, so vault text never leaves the device for
    linking (see ``docs/architecture/ADR/0004-embeddings-stay-local.md``).
    """

    model: str = "all-MiniLM-L6-v2"
    """Sentence-transformer model used to generate embeddings (always local).

    Changing it invalidates the on-disk vector cache: every fragment must be
    re-embedded and ``similarity_threshold`` re-calibrated for the new model.
    """

    similarity_threshold: float = 0.75
    """Minimum cosine similarity for linking fragments."""

    resonance_method: Literal["exact", "topk"] = "exact"
    """Resonance-discovery strategy.

    ``exact`` (default) emits every above-``similarity_threshold`` pair — the
    validated, exhaustive path. ``topk`` keeps only each fragment's
    ``resonance_top_k`` most-similar above-threshold neighbours, bounding the
    edge set to O(n·k) for large vaults; its output is always a subset of the
    exact path's edges.
    """

    resonance_top_k: int = Field(default=10, ge=1)
    """Per-fragment neighbour cap when ``resonance_method = "topk"``."""

    cache_dir: str | None = None
    """Local directory for caching downloaded models."""

    batch_size: int = 32
    """Number of texts to encode per batch."""


class OCRConfig(BaseModel):
    """OCR configuration."""

    enabled: bool = True
    """Whether to run OCR on image-based sources."""

    engine: str = "pytesseract"
    """OCR engine to use."""

    languages: list[str] = Field(default_factory=lambda: ["eng"])
    """Tesseract language codes for OCR."""

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    """Minimum OCR confidence below which a fragment lands in the review queue.

    Image and scanned-PDF ingestors compare the per-page confidence
    reported by the engine to this threshold; a fragment whose OCR
    score falls below it is tagged ``review: pending_review`` in
    frontmatter so a human can verify the recovered text.
    """


class LinkingConfig(BaseModel):
    """Linking pipeline configuration."""

    temporal_window_hours: int = 168
    """Time window (hours) for temporal proximity linking (default 1 week)."""

    thread_min_fragments: int = 3
    """Minimum fragments required to form a Thread."""

    eddy_min_fragments: int = 5
    """Minimum fragments required to form an Eddy."""

    exchange_max_gap_minutes: int = Field(default=30, ge=1)
    """Maximum minute-gap between consecutive messages still grouped into
    the same ``exchange`` by the FEAT-022 aggregator. Inclusive boundary.
    """

    burst_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    """Cosine-similarity floor below which two consecutive exchanges start
    separate ``burst``-level parents in the FEAT-022 aggregator.
    Inclusive boundary.
    """

    session_max_gap_minutes: int = Field(default=360, ge=1)
    """Maximum minute-gap between consecutive bursts still grouped into the
    same ``session`` by the FEAT-022 aggregator. Inclusive boundary.
    """

    hierarchy_sibling_skip_window: int = Field(default=2, ge=0)
    """FEAT-024 sibling-suppression window for hierarchy-aware linking.

    With re-atomization (FEAT-020..023) every parent fragment shares the
    vault with its own children, and adjacent children of one parent are
    usually topically continuous — so naive cosine similarity emits noise
    instead of meaningful resonances. The embedding linker drops pairs
    that are ancestor/descendant unconditionally, and pairs that share a
    parent and sit within this many positions of each other in the
    parent's ``child_ids`` list. ``0`` disables sibling suppression
    entirely (ancestor suppression still applies); the default of ``2``
    skips immediate and one-removed neighbours on either side.
    """

    cross_source_aggregation: bool = False
    """When ``True``, the FEAT-027 aggregator drops the source-identity
    gate so a single exchange/burst/session may span multiple sources
    once the temporal/similarity thresholds permit. Each parent records
    every contributing source key as a ``source/<key>`` tag while child
    fragments retain their original :class:`~creek.models.FragmentSource`
    pointers. ``False`` (default) preserves the single-source behaviour
    introduced by FEAT-022.
    """

    # ---- Detector thresholds (issue #880) ----
    #
    # Every default below is the exact value of the module-private constant
    # it replaces, so a vault whose creek_config.yaml predates these keys
    # clusters identically after the upgrade.

    eddy_eps: float = Field(default=0.3, gt=0.0, lt=1.0)
    """Maximum cosine *distance* for a DBSCAN neighbour in eddy detection.

    ``0.3`` means an edge exists between two fragments at cosine similarity
    ``>= 0.70``. Lowering it tightens every cluster; it cannot, on its own,
    separate a continuous message stream (see ``stream_platforms``).
    """

    eddy_min_samples: int = Field(default=5, ge=1)
    """Minimum neighbourhood size for a DBSCAN core point in eddy detection."""

    eddy_correlation_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    """Absolute Spearman ceiling above which a cluster is thread-like.

    A candidate eddy whose content drift correlates with chronological rank
    at or above this value is a directional progression, and is filtered out
    of the eddy set rather than emitted as a topic cluster.
    """

    thread_window_days: int = Field(default=30, ge=1)
    """Sliding-window width, in days, for thread detection.

    Distinct from ``temporal_window_hours``, which feeds only the temporal
    *proximity* linker and has never influenced threads.
    """

    thread_similarity_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    """Cosine similarity a pair must strictly exceed to join one thread."""

    thread_union_without_embeddings: bool = False
    """Whether a pair missing an embedding may union on frequency alone.

    When the detector has *no* embeddings at all, thread detection falls
    back to frequency agreement by design. On a *partially* embedded vault
    the same fallback fires per pair, silently chaining fragments the
    similarity gate would have rejected — so it is closed by default and
    must be opted into.
    """

    # ---- Cluster-size guardrail (issue #880) ----

    cluster_size_ceiling: int = Field(default=500, ge=1)
    """Absolute member count below which a cluster is never split.

    The effective ceiling is ``max(cluster_size_ceiling, floor(corpus_size *
    cluster_max_fraction))``: the absolute floor dominates ordinary vaults,
    so the guardrail stays inert until a corpus is large enough for
    degeneration to matter.
    """

    cluster_max_fraction: float = Field(default=0.10, gt=0.0, le=1.0)
    """Largest share of the corpus a single eddy or thread may hold.

    ``1.0`` is the documented opt-out: one cluster may then span everything.
    """

    cluster_split_max_depth: int = Field(default=3, ge=0)
    """Re-clustering rounds allowed per oversized cluster.

    ``0`` disables splitting entirely, sending any oversized cluster
    straight to noise.
    """

    eddy_split_eps_step: float = Field(default=0.05, gt=0.0, lt=1.0)
    """Amount ``eddy_eps`` is tightened by on each re-clustering round."""

    thread_split_similarity_step: float = Field(default=0.1, gt=0.0, lt=1.0)
    """Amount ``thread_similarity_threshold`` is raised on each round."""

    # ---- Message-stream segmentation (issue #880) ----

    stream_platforms: list[str] = Field(
        default_factory=lambda: ["discord", "email"],
    )
    """Source platforms whose fragments are cut into conversation episodes.

    A continuous chat stream violates the precondition both detectors rely
    on — clusters separated by low-density regions — so it is partitioned
    into independent clustering domains *before* any similarity graph is
    built. The default names the two platforms Creek already routes to the
    ``01-Fragments/Messages/`` subfolder. An empty list disables
    segmentation. Values must be :class:`~creek.models.SourcePlatform`
    members.
    """

    stream_episode_max_gap_hours: int = Field(default=24, ge=1)
    """Inactivity gap, in hours, that ends a conversation episode.

    The conversational rule: people stop talking, and that silence is the
    real topic boundary. Inclusive — a gap exactly this long does not cut.
    """

    stream_episode_max_span_days: int = Field(default=30, ge=1)
    """Maximum span, in days, of a single conversation episode.

    The backstop for a channel that never falls idle, so a permanently-busy
    channel yields channel-month units rather than one multi-year blob.
    Inclusive — a span exactly this long does not cut.
    """

    @field_validator("stream_platforms")
    @classmethod
    def _validate_stream_platforms(cls, value: list[str]) -> list[str]:
        """Reject platform names that no ingestor can ever produce.

        Args:
            value: The configured platform values.

        Returns:
            The value unchanged when every entry names a
            :class:`~creek.models.SourcePlatform` member.

        Raises:
            ValueError: If any entry is not a known source platform — a
                typo would otherwise disable segmentation silently.
        """
        from creek.models import SourcePlatform

        known = {platform.value for platform in SourcePlatform}
        unknown = [name for name in value if name not in known]
        if unknown:
            msg = (
                f"unknown source platform(s) in stream_platforms: "
                f"{', '.join(sorted(unknown))}; "
                f"valid values are {', '.join(sorted(known))}"
            )
            raise ValueError(msg)
        return value


class ClassificationConfig(BaseModel):
    """Classification pipeline configuration."""

    confidence_threshold: float = 0.7
    """Minimum confidence score for automatic classification."""

    reclassify_on_edit_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    """Body-similarity floor below which an edited mutable source unit is
    flagged for re-classification (#675 / SPEC R1).

    When a journal/markdown fragment is updated in place, its body is compared
    to the prior version (``difflib`` ratio in ``[0, 1]``). At/above this
    threshold the edit is *trivial* (typo, whitespace) and classifications are
    preserved; below it the change is *material* and the fragment's
    ``classification_method`` is cleared so the next classify pass re-does only
    that fragment (cooperating with OPS-001, no global ``--force``). ``0.0``
    disables flagging (always preserve)."""

    auto_classify_sources: list[str] = Field(
        default_factory=lambda: ["claude", "chatgpt", "discord"],
    )
    """Sources that are auto-classified without human review."""

    human_review_sources: list[str] = Field(
        default_factory=lambda: ["journal"],
    )
    """Sources that require human review after classification."""

    reatomize: bool = False
    """Opt-in switch for FEAT-023 confidence-driven re-atomization.

    When ``False`` (default) the classify pipeline behaves exactly as
    it did before FEAT-023: a single pass over each fragment, no
    structural decomposition. When ``True``, low-confidence /
    ``unclassified`` results trigger the zoom-in splitter (FEAT-021) or
    zoom-out aggregator (FEAT-022), recursing until a leaf reaches the
    confidence threshold, a terminal level (sentence / session), or
    :attr:`reatomize_max_depth`.
    """

    reatomize_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    """Confidence floor that triggers re-atomization (FEAT-023).

    ``None`` (default) means "use :attr:`confidence_threshold`" — the
    same floor that already gates a paid LLM call. Set this lower than
    the main threshold to keep the rules-vs-LLM gate at one place but
    only fire re-atomization on truly hopeless fragments.
    """

    reatomize_max_depth: int = Field(default=4, ge=1)
    """Hard ceiling on FEAT-023 recursion depth.

    Caps unbounded zoom-in on a fractal document. Counted from the
    root fragment (depth 0). Once reached, the orchestrator accepts
    whatever classification it has, even if still ``unclassified``.
    """

    reatomize_direction: Literal["auto", "split", "aggregate"] = "auto"
    """FEAT-023 direction-choice override.

    ``"auto"`` (default) routes by ``source.platform`` and ``level``
    per the heuristic in :mod:`creek.classify.reatomize`. ``"split"``
    or ``"aggregate"`` force a single direction regardless of source
    — useful for triage runs over a known-uniform corpus.
    """

    weighted_classification: bool = False
    """Opt-in switch for weighted classification across the holarchy.

    When ``False`` (default) the classify pipeline writes a single
    canonical pick per dimension to
    :attr:`Fragment.frequency` / :attr:`Fragment.wavelength` /
    :attr:`Fragment.voice` and leaves :attr:`Fragment.weighted` as
    ``None``. When ``True``, every fragment whose classification
    actually reaches the LLM also gets a
    :class:`~creek.classify.weighted.WeightedFragmentClassification`
    persisted to :attr:`Fragment.weighted`; the legacy single-pick
    fields are derived from the top weighted entries via
    :meth:`WeightedFragmentClassification.to_legacy` so downstream
    consumers (lint, compile, voice-skill generation) keep working
    unchanged.

    Rule-confident fragments are NOT re-classified —
    :attr:`Fragment.weighted` stays ``None`` for those even with this
    flag on. This preserves the FEAT-017 cost gate that already keeps
    the LLM from re-running over high-confidence rule picks. A fragment
    whose weighted call reaches the LLM but *fails* (provider
    unavailable, transport error, unparseable payload) likewise keeps
    :attr:`Fragment.weighted` as ``None``, along with its honest
    ``rules`` provenance, rather than persisting a fabricated all-zero
    profile (#1330).
    """


class ContextConfig(BaseModel):
    """Non-user content handling configuration.

    Controls how content authored by others (e.g. Discord messages,
    collaborative documents) is represented in the vault.
    """

    mode: str = "context_metadata"
    """Handling mode: ``context_metadata``, ``low_priority``, or ``skip``.

    - ``context_metadata``: Store others' content as context in the user's
      fragment; do not index separately.
    - ``low_priority``: Ingest as separate fragments with ``author: other``
      and reduced quality scores; exclude from voice proxy.
    - ``skip``: Drop others' content entirely; preserve only user's words.
    """

    quality_penalty: float = 0.5
    """Multiplier applied to quality scores for ``low_priority`` fragments."""

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate that *v* is a recognised content handling mode.

        Args:
            v: Mode string to validate.

        Returns:
            The validated mode string.

        Raises:
            ValueError: If the mode is not one of the allowed values.
        """
        allowed = {"context_metadata", "low_priority", "skip"}
        if v not in allowed:
            msg = f"Invalid context mode: {v!r}. Must be one of {allowed}"
            raise ValueError(msg)
        return v

    @field_validator("quality_penalty")
    @classmethod
    def validate_quality_penalty(cls, v: float) -> float:
        """Validate quality penalty is in [0.0, 1.0].

        Args:
            v: Penalty multiplier to validate.

        Returns:
            The validated penalty value.

        Raises:
            ValueError: If the penalty is outside the valid range.
        """
        if not 0.0 <= v <= 1.0:
            msg = f"quality_penalty must be in [0.0, 1.0], got {v}"
            raise ValueError(msg)
        return v


class RedactionConfig(BaseModel):
    """Redaction scanner configuration."""

    enabled: bool = True
    """Whether to run the PII redaction scanner."""

    dry_run: bool = False
    """If ``True``, report redactions but do not apply them."""

    custom_patterns: dict[str, str] = Field(default_factory=dict)
    """Extra regex patterns (name -> pattern) for the scanner."""

    false_positive_allowlist: list[str] = Field(default_factory=list)
    """Strings that should never be flagged as PII."""

    supported_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".json",
            ".py",
            ".env",
            ".yaml",
            ".toml",
            ".csv",
        ],
    )
    """File extensions the scanner will process."""

    exclude_patterns: list[str] = Field(
        default_factory=lambda: [".git", "node_modules"],
    )
    """Directory name patterns to exclude from recursive scanning."""

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    """Confidence threshold for the generic high-entropy secret detector.

    Higher values demand more entropy before flagging a substring; ``0.0``
    catches everything that looks base64-ish, ``1.0`` requires near-random
    output. The default ``0.6`` corresponds to exactly 3.7 bits/char —
    the ``2.5`` floor plus ``0.6`` of the span up to the ``4.5`` ceiling —
    which suppresses most natural-language false positives. Raising it is
    the supported way to trade detector reach for fewer false positives:
    it narrows both the whole-run and the sub-run window gate.
    """

    replacement_template: str = "[REDACTED:{name}]"
    """Format string used by :class:`creek.redact.redactor.Redactor`.

    Must contain the ``{name}`` placeholder so the matched pattern's name
    is interpolated into the marker. Other format placeholders are
    rejected at validation time to surface typos early.
    """

    @field_validator("replacement_template")
    @classmethod
    def validate_replacement_template(cls, v: str) -> str:
        """Reject templates lacking ``{name}`` or carrying other placeholders."""
        # Format with a sentinel; if substitution didn't change the string,
        # `{name}` was absent. KeyError/IndexError/ValueError surface unknown
        # placeholders like `{type}` or malformed format specs.
        try:
            formatted = v.format(name="\x00creek_name_sentinel\x00")
        except (KeyError, IndexError, ValueError) as exc:
            msg = (
                f"replacement_template {v!r} has invalid placeholders; "
                "only '{name}' is supported."
            )
            raise ValueError(msg) from exc
        if formatted == v:
            msg = f"replacement_template {v!r} must include the '{{name}}' placeholder."
            raise ValueError(msg)
        return v


_READONLY_SCOPES: set[str] = {
    "https://www.googleapis.com/auth/drive.readonly",
}


class GoogleDriveConfig(BaseModel):
    """Google Drive configuration (READ-ONLY scopes enforced)."""

    credentials_file: str = "credentials.json"
    """Path to the OAuth2 credentials file."""

    token_file: str = "token.json"
    """Path to the cached OAuth2 token file."""

    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    """OAuth2 scopes — **must** be read-only."""

    staging_dir: str = "google-drive-export/"
    """Local directory for staging downloaded files."""

    @field_validator("scopes")
    @classmethod
    def validate_readonly_scopes(cls, v: list[str]) -> list[str]:
        """Enforce read-only scopes.

        Args:
            v: List of OAuth2 scope strings.

        Returns:
            The validated list of scopes.

        Raises:
            ValueError: If any scope is not in the read-only allowlist.
        """
        for scope in v:
            if scope not in _READONLY_SCOPES:
                msg = f"Only read-only scopes allowed. Got: {scope}"
                raise ValueError(msg)
        return v


class DiscordCleaningConfig(BaseModel):
    """Discord message cleaning configuration."""

    filter_bot_messages: bool = True
    """Whether to filter out messages from bots."""

    strip_emoji: bool = False
    """Whether to strip emoji characters from messages."""

    filter_commands: bool = True
    """Whether to filter out bot command messages (e.g. ``!help``)."""

    min_message_length: int = 10
    """Minimum character length for a message to be kept."""


class ChatbotCleaningConfig(BaseModel):
    """Chatbot export cleaning configuration.

    Controls which noise types are filtered from chatbot conversation
    exports before fragment extraction.
    """

    filter_system_prompts: bool = True
    """Whether to filter out system prompt content."""

    filter_tool_outputs: bool = True
    """Whether to filter out tool/function call outputs."""

    filter_regenerations: bool = True
    """Whether to filter out regenerated responses."""

    min_human_turn_length: int = 20
    """Minimum character count for human turns (below = skip)."""

    code_block_threshold: float = 0.9
    """Code-block ratio above which a response is flagged as code-only."""

    max_abandoned_turns: int = 2
    """Max turn pairs for abandoned conversation detection."""


class MarkdownCleaningConfig(BaseModel):
    """Markdown file cleaning configuration."""

    skip_empty_files: bool = True
    """Whether to skip files with no meaningful body content."""

    min_body_length: int = 50
    """Minimum character length for the body to be considered non-empty."""


class GoogleDriveCleaningConfig(BaseModel):
    """Google Drive document cleaning configuration."""

    deduplicate: bool = True
    """Whether to deduplicate downloaded documents."""

    filter_empty_docs: bool = True
    """Whether to filter out empty or placeholder documents."""

    max_collaboration_ratio: float = 0.9
    """Maximum ratio of non-owner edits before a doc is flagged as collaborative."""


class ValidationConfig(BaseModel):
    """Content validation configuration."""

    min_characters: int = 20
    """Minimum character count for valid content."""

    min_words: int = 5
    """Minimum word count for valid content."""

    max_stop_word_ratio: float = 0.8
    """Maximum ratio of stop words before content is flagged as low-quality."""

    require_metadata: bool = True
    """Whether to require metadata fields (title, date, source)."""


class QualityConfig(BaseModel):
    """Quality scoring configuration."""

    accept_threshold: float = 0.7
    """Minimum quality score to automatically accept content."""

    skip_threshold: float = 0.3
    """Quality score below which content is automatically skipped."""


class DeduplicationConfig(BaseModel):
    """Deduplication configuration."""

    strategy: str = "fuzzy"
    """Matching strategy — ``exact`` or ``fuzzy``."""

    similarity_threshold: float = 0.85
    """Minimum similarity score for fuzzy deduplication."""


class HygieneConfig(BaseModel):
    """Data hygiene configuration."""

    track_orphans: bool = True
    """Whether to track orphaned fragments with no connections."""

    staleness_days: int = 90
    """Number of days before a fragment is considered stale."""


class CleaningConfig(BaseModel):
    """Top-level cleaning pipeline configuration."""

    discord: DiscordCleaningConfig = Field(
        default_factory=DiscordCleaningConfig,
    )
    """Discord message cleaning settings."""

    chatbot: ChatbotCleaningConfig = Field(
        default_factory=ChatbotCleaningConfig,
    )
    """Chatbot export cleaning settings."""

    markdown: MarkdownCleaningConfig = Field(
        default_factory=MarkdownCleaningConfig,
    )
    """Markdown file cleaning settings."""

    google_drive: GoogleDriveCleaningConfig = Field(
        default_factory=GoogleDriveCleaningConfig,
    )
    """Google Drive document cleaning settings."""

    validation: ValidationConfig = Field(
        default_factory=ValidationConfig,
    )
    """Content validation settings."""

    quality: QualityConfig = Field(
        default_factory=QualityConfig,
    )
    """Quality scoring settings."""

    deduplication: DeduplicationConfig = Field(
        default_factory=DeduplicationConfig,
    )
    """Deduplication settings."""

    hygiene: HygieneConfig = Field(
        default_factory=HygieneConfig,
    )
    """Data hygiene settings."""


class CompostConfig(BaseModel):
    """Compost detection configuration (FEAT-018).

    Replaces the legacy five-phrase abandonment keyword regex with a
    two-stage pipeline: an embedding-similarity gate against curated
    exemplars (``creek/generate/exemplars/compost.yaml``), followed by
    an optional LLM verifier that returns ``yes`` / ``no`` /
    ``ambiguous``. Ambiguous verdicts route to a review queue rather
    than the canonical compost folder.
    """

    embedding_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    """Cosine-similarity floor above which a fragment is sent to the
    verifier. Deliberately wide-net (0.6) — the verifier owns precision.
    Tighten once a calibration set exists for your real corpus.
    """

    llm_verification: bool = True
    """When ``True``, candidates that pass the embedding gate are sent
    to the LLM verifier. When ``False``, the embedding gate alone
    decides acceptance (faster + offline, but more false positives).
    """

    review_queue_relpath: str = "10-Liminal/Compost/Review"
    """Vault-relative path under which ``ambiguous`` verdicts are filed.
    Operators triage this queue manually and either promote to canonical
    compost or delete.
    """

    skip_paradox: bool = True
    """When ``True``, fragments tagged ``paradox`` or living under
    ``10-Liminal/Paradoxes/`` are skipped — paradoxes are contradiction-
    holding notes by design, not compost candidates.
    """

    exemplars_relpath: str | None = None
    """Vault-relative path to a custom exemplars YAML. When ``None``,
    the packaged default (``creek/generate/exemplars/compost.yaml``)
    is used. Override only after running ``creek compost calibrate``
    against your corpus has shown the defaults under-recall.
    """


class MiningConfig(BaseModel):
    """Configuration for ``creek mine`` thresholds (issue #340).

    The four mining strategies use independent thresholds that were
    previously hard-coded in :mod:`creek.generate.mining`. Exposing them
    here lets an operator calibrate against their actual corpus size
    without monkey-patching defaults — the canonical fix for
    "188 fragments, zero seeds" surfaced in issue #340.

    Defaults mirror the module-level constants so behaviour is
    unchanged for users who do not set the ``mining:`` section in
    ``creek_config.yaml``.
    """

    min_thread_fragments: int = Field(default=10, ge=1)
    """Threads with strictly more fragments than this become terminus
    candidates. Default ``10`` matches
    :data:`creek.generate.mining.DEFAULT_MIN_THREAD_FRAGMENTS`. Lower
    this on a small-N corpus where the typical thread is only a handful
    of fragments long.
    """

    min_chain_length: int = Field(default=3, ge=2)
    """Minimum number of fragments in a resonance chain for it to
    surface as a seed. Default ``3`` matches
    :data:`creek.generate.mining.DEFAULT_MIN_CHAIN_LENGTH`. ``2`` is
    permitted (a pair is technically a degenerate chain) for tuning
    against very small synchronicity sets.
    """

    similarity_liminal: float = Field(default=0.35, ge=0.0, le=1.0)
    """Minimum Jaccard overlap between a liminal fragment and an eddy.
    Default ``0.35`` matches
    :data:`creek.generate.mining.DEFAULT_SIMILARITY_LIMINAL`. Increase
    for stricter, fewer matches; decrease to admit weaker echoes.
    """

    similarity_resonance: float = Field(default=0.6, ge=0.0, le=1.0)
    """Minimum similarity for two fragments to share a resonance-chain
    edge. Default ``0.6`` matches
    :data:`creek.generate.mining.DEFAULT_SIMILARITY_RESONANCE`. The
    miner reads synchronicity records from
    ``10-Liminal/Synchronicities/`` — embeddings-only edges (those
    that never landed in a synchronicity note) do not contribute, so
    lowering this knob alone will not surface chains from an
    unpopulated synchronicities folder.
    """


class DraftConfig(BaseModel):
    """Configuration for the ``creek draft`` command.

    Carries the bidirectional grounding-guard thresholds and the default
    LLM ``max_tokens`` ceiling. The grounding guard surfaces two failure
    modes after a draft is generated:

    * **Too derivative** — at least one draft paragraph paraphrases a
      source-fragment paragraph closely (cosine similarity above
      :attr:`derivative_upper`).
    * **Too ungrounded** — fewer than :attr:`grounding_fraction_lower` of
      the draft's paragraphs clear the per-paragraph :attr:`grounding_lower`
      similarity floor against *any* source paragraph, i.e. the draft is
      conceptually invented.

    The two grounding knobs are independent: :attr:`grounding_lower`
    decides whether a single paragraph counts as grounded, while
    :attr:`grounding_fraction_lower` decides what share of the draft's
    paragraphs must be grounded for the draft to pass. An operator can
    demand a strict per-paragraph anchor (e.g. 0.4) while tolerating a
    lenient grounded fraction (e.g. 0.2), or the reverse.

    Defaults are deliberately lenient (0.85 / 0.30 / 0.30); operators
    tighten once they have a calibration set for their own corpus. All
    three knobs are cosine similarities or fractions and so must live in
    ``[0.0, 1.0]``.
    """

    derivative_upper: float = Field(default=0.85, ge=0.0, le=1.0)
    """Paragraph cosine-similarity ceiling above which a draft paragraph
    is treated as a paraphrase of a source paragraph and the draft is
    flagged ``too derivative``."""

    grounding_lower: float = Field(default=0.30, ge=0.0, le=1.0)
    """Per-paragraph cosine-similarity floor a draft paragraph must clear
    against *some* source paragraph to count as grounded. Independent of
    :attr:`grounding_fraction_lower`, which governs the whole-draft
    grounded fraction."""

    grounding_fraction_lower: float = Field(default=0.30, ge=0.0, le=1.0)
    """Minimum acceptable fraction of grounded paragraphs across the whole
    draft. A draft whose grounded fraction sits below this value is
    flagged ``too ungrounded``, even when every grounded paragraph
    individually cleared :attr:`grounding_lower`."""

    max_tokens: int | None = Field(default=None, gt=0)
    """Default ``max_tokens`` ceiling for the draft LLM call.

    ``None`` keeps the provider's built-in default (1024 for the
    Anthropic provider). Operators who routinely want longer essays set
    this so they don't have to pass ``--max-tokens`` on every run; the
    CLI flag overrides it per invocation. A draft cut off at this ceiling
    is flagged ``truncated`` in frontmatter and warned about on stderr."""

    cohesion: bool = Field(default=False)
    """Opt-in switch for the no-fabrication cohesion pass.

    Default ``False`` so merging the pass changes no current behaviour: a
    single-topic draft is composed exactly as before unless the operator
    passes ``creek draft --cohesion`` (or sets this to ``true`` in
    ``draft:``). When enabled — and only with the LLM available, never on
    the ``--no-llm`` path — a post-composition pass smooths abrupt seams by
    inserting transitions. The rewrite is gated by a deterministic
    entity-preservation guard (no new proper noun, number, or bespoke
    ontology term) plus a biographical-grounding re-check; any violation
    falls back to the pre-cohesion body."""


AIStyleCategory = Literal[
    "mechanical",
    "lexical",
    "rhetorical",
    "discourse",
    "citation",
]
"""The five FEAT-040 tell families. Defined here (rather than in the
``ai_style`` package) so :class:`AIStyleConfig` can validate config values
against it at parse time without importing that package — which would
create an import cycle, since ``ai_style`` imports this module. The
``ai_style`` model re-exports this as ``Category``."""


def _default_enabled_categories() -> list[AIStyleCategory]:
    """Return the default enabled tell categories — the four *voice* families.

    ``citation`` is intentionally excluded by default: citation-integrity tells
    are voice-neutral (factual integrity, not idiolect), so mixing their
    zero contributions into the scalar ``voice_distance`` would dilute the
    idiolect signal. They register normally and are surfaced by enabling the
    ``citation`` category explicitly (e.g. a "lint my draft" surface).
    """
    categories: list[AIStyleCategory] = [
        "mechanical",
        "lexical",
        "rhetorical",
        "discourse",
    ]
    return categories


def _default_category_weights() -> dict[AIStyleCategory, float]:
    """Return the default per-category divergence weights."""
    weights: dict[AIStyleCategory, float] = {
        "mechanical": 0.5,
        "lexical": 1.0,
        "rhetorical": 1.0,
        "discourse": 1.0,
        "citation": 0.5,
    }
    return weights


def _default_authorship_weights() -> dict[str, float]:
    """Return default per-platform weights for fingerprint aggregation.

    Purely user-authored sources are weighted highest; conversation
    platforms lower because only their user-turn portion is fingerprinted
    and that text is noisier.
    """
    return {
        "journal": 1.0,
        "markdown": 1.0,
        "substack": 1.0,
        "essay": 1.0,
        "email": 0.8,
        "chatgpt": 0.5,
        "claude": 0.5,
        "discord": 0.6,
    }


class AuthorConfig(BaseModel):
    """Configuration for the FEAT-041 Creek Writing Desk author subsystem."""

    max_author_rounds: int = Field(default=3, ge=1, le=10)
    """Maximum Conductor voice/reflect rounds before escalation (bounded
    ``[1, 10]``). The Conductor loops on a ``REVISE`` verdict up to this many
    times; model ids for the desk's LLM calls come from the ``llm`` section,
    never hard-coded."""

    graph_breadth_bound: int = Field(default=25, ge=1)
    """Max fragments the Graph agent's backlink walk expands at each depth
    level (open question #4). Config-bounded, not hard-coded."""

    graph_depth_bound: int = Field(default=2, ge=0)
    """Max backlink hops the Graph agent walks from its seed (``0`` = seed
    only). Config-bounded, not hard-coded."""

    retrieval_top_k: int = Field(default=5, ge=1)
    """How many top-ranked fragments the Retrieval agent surfaces as evidence."""

    voice_model: str | None = None
    """Per-agent model override for the voice call (#474). ``None`` (default)
    falls back to the shared ``llm.model`` so no model id is hard-coded in the
    desk; set it to point the owner-voice render at a stronger/cheaper tier than
    the rest of the pipeline."""

    synthesis_model: str | None = None
    """Reserved per-agent model override for the synthesis step (#474).
    Synthesis is deterministic today (it merges grounded claims with no LLM
    call), so this is documented-but-dormant: ``None`` falls back to
    ``llm.model`` and nothing reads it yet. Reserved so a future LLM synthesis
    can be tiered without a config migration."""

    reflection_model: str | None = None
    """Reserved per-agent model override for the reflection step (#474). The
    reflection node is a deterministic judge today, so like
    :attr:`synthesis_model` this is reserved-and-dormant: ``None`` falls back to
    ``llm.model``."""


class AIStyleConfig(BaseModel):
    """Configuration for the FEAT-040 AI-style / voice-fidelity subsystem.

    The subsystem measures how far generated text diverges from the
    user's own writing (their *voice fingerprint*) rather than from a
    generic baseline. A "tell" fires for a feature only when the draft's
    measured rate differs from the user's stored rate by more than
    :attr:`default_margin` (or a per-feature override). The scalar
    ``voice_distance`` aggregates those divergences, weighted by
    category. Everything here is intentionally conservative: the headline
    correctness property is that the user's *own* writing must not flag.
    """

    enabled: bool = True
    """Master switch. When ``False`` the scanner returns an empty report."""

    voice_distance_upper: float = Field(default=0.35, ge=0.0, le=1.0)
    """Distance above which a draft is considered to diverge from the
    user's voice enough to warrant a rewrite pass / lint finding. Capped at
    1.0 because ``voice_distance`` is always ``< 1.0``; a higher threshold
    would silently disable the check."""

    citation_network_checks: bool = False
    """Opt-in toggle for live external-link checking by the ``citation`` tells.

    Off by default so the scanner is fully offline (ISBN/DOI checksum +
    syntax only). When ``True``, a wired resolver may probe outbound links
    (cached, rate-limited, timeout-bounded); documented false-positive hosts
    (paywalls, libraries, the low-PMID artefact) are never reported."""

    voice_distance_target: float = Field(default=0.25, ge=0.0, le=1.0)
    """Target distance the de-slop rewrite loop drives toward — distinct from
    the accepted-divergence ceiling :attr:`voice_distance_upper`. The loop
    keeps rewriting while distance exceeds this target (up to
    :attr:`max_revision_passes`), so a mannered draft below the permissive
    ceiling is still stripped rather than merely measured. Clamped to
    ``voice_distance_upper`` when configured above it (the target never exceeds
    the ceiling), so lowering the ceiling reproduces the pre-target behaviour."""

    default_margin: float = Field(default=0.5, ge=0.0)
    """Default divergence margin, in absolute rate units, a feature must
    exceed before it produces a :class:`~creek.generate.ai_style.model.Finding`.
    Rates are per-1000-words, so ``0.5`` means "half an occurrence per
    thousand words above the user's own rate"."""

    signature_polarity_threshold: float = Field(default=3.0, ge=0.0)
    """Per-1000-words rate at or above which a feature is treated as one of
    *this user's* signatures, deriving its polarity per-user (#635): the
    concern flips from over-use (``avoid``) to under-use (``signature``), so a
    draft that strips a feature the user characteristically employs raises
    voice distance instead of lowering it. Below this rate the feature keeps
    its declared (avoid) polarity. Never-legitimate artifacts (tells with
    ``margin == 0.0``: placeholder dates, fabricated citations) are exempt and
    stay ``avoid`` regardless of the user's measured rate."""

    min_fingerprint_fragments: int = Field(default=5, ge=1)
    """Below this many user-authored fragments the fingerprint is treated
    as thin: divergence contributions are softened toward the generic
    prior and reports carry a ``thin_fingerprint`` flag rather than
    flagging aggressively."""

    thin_fingerprint_softening: float = Field(default=0.25, ge=0.0, le=1.0)
    """Multiplier applied to distance contributions when the fingerprint
    is thin (see :attr:`min_fingerprint_fragments`). ``0.25`` keeps a
    quarter of the signal so obvious tells still surface while the
    measurement is unreliable."""

    calibration_fp_floor: float = Field(default=0.05, ge=0.0, le=1.0)
    """Maximum acceptable false-positive rate on the user's own writing
    during calibration. Above this, :func:`calibrate` reports failure."""

    enabled_categories: list[AIStyleCategory] = Field(
        default_factory=_default_enabled_categories,
    )
    """Tell categories the scanner runs. Trimming this disables a whole
    family of detectors without touching the registry. Typed against
    :data:`AIStyleCategory` so a typo in YAML/JSON config is rejected at
    parse time rather than silently producing an empty tell list."""

    category_weights: dict[AIStyleCategory, float] = Field(
        default_factory=_default_category_weights,
    )
    """Per-category weight applied to each feature's divergence when
    summing ``voice_distance``. Mechanical tells are down-weighted because
    they are auto-repaired and should not dominate the human-vs-voice
    score."""

    feature_weights: dict[str, float] = Field(default_factory=dict)
    """Optional per-``feature_key`` weight overrides. A key present here
    wins over its category weight."""

    authorship_weights: dict[str, float] = Field(
        default_factory=_default_authorship_weights,
    )
    """Per-source-platform weight used when aggregating the voice
    fingerprint (FEAT-040.2). Genuinely user-authored sources (journal,
    markdown, substack) are weighted highest; conversation platforms
    (chatgpt, claude) lower, because only their user-turn survives the
    authorship filter and that text is noisier."""

    authorship_default_weight: float = Field(default=0.75, ge=0.0)
    """Weight for a source platform not listed in
    :attr:`authorship_weights`."""

    fingerprint_path: str = "00-Creek-Meta/voice-fingerprint.json"
    """Vault-relative path where the voice fingerprint is persisted."""

    prevent_in_prompt: bool = True
    """Master switch for FEAT-040.8 prompt prevention. When ``True`` a
    ``## Voice targets`` preamble (the user's measured voice as positive
    targets plus a personalised avoid-list) is injected into draft prompts.
    Prevention is the primary lever — it is cheaper and more faithful than
    repairing AI-voice after the fact."""

    include_measured_targets: bool = True
    """Whether the preamble states the user's *measured* habits as positive
    targets (e.g. "uses em-dashes freely (~4.2 per 1000 words)"). When
    ``False`` the preamble keeps only the personalised avoid-list. Disabling
    is useful before the fingerprint is trustworthy."""

    preamble_max_chars: int = Field(default=1200, ge=0)
    """Length cap for the injected preamble, in characters. The avoid-list is
    trimmed to fit (the measured targets are always kept) so an overlong
    avoid-list never induces the stilted "evasion" voice we are escaping.
    ``0`` disables the cap."""

    max_revision_passes: int = Field(default=1, ge=0)
    """Maximum FEAT-040.9 voice-fidelity rewrite passes after composition.
    Each pass measures voice distance, rewrites toward the user's measured
    voice, then re-measures; the loop stops early once distance falls under
    :attr:`voice_distance_upper` or a pass fails to improve. ``0`` disables
    the rewrite loop (sanitize + measure only). Kept small (default ``1``)
    because rewriting is bounded and regression-guarded, not iterative
    polishing."""

    def weight_for(self, *, category: str, feature_key: str) -> float:
        """Resolve the divergence weight for a feature.

        A per-feature override in :attr:`feature_weights` wins; otherwise
        the feature inherits its :attr:`category_weights` entry, defaulting
        to ``1.0`` when the category is unlisted.

        Args:
            category: The tell's category.
            feature_key: The tell's ``feature_key``.

        Returns:
            The weight to apply to this feature's divergence.
        """
        if feature_key in self.feature_weights:
            return self.feature_weights[feature_key]
        # `category` is a plain str (the caller may pass an unknown value);
        # compare against the validated Literal keys without a cast.
        for key, weight in self.category_weights.items():
            if key == category:
                return weight
        return 1.0

    @model_validator(mode="after")
    def _clamp_target_to_ceiling(self) -> "AIStyleConfig":
        """Keep ``voice_distance_target`` at or below ``voice_distance_upper``.

        Clamping (rather than rejecting) means a config that lowers the ceiling
        below the default target reproduces the pre-target behaviour — the loop
        simply drives to the ceiling — instead of raising a validation error.
        """
        if self.voice_distance_target > self.voice_distance_upper:
            self.voice_distance_target = self.voice_distance_upper
        return self


class LintConfig(BaseModel):
    """Configuration for ``creek lint`` checks (FEAT-008, FEAT-025).

    Today only the paradox check carries a knob; this class exists so
    additional check-level toggles can land without expanding the
    top-level :class:`CreekConfig` surface.
    """

    paradox_cross_level: bool = False
    """FEAT-025 opt-in for cross-level paradox detection.

    ``False`` (default): the paradox check skips pairs whose
    :attr:`creek.models.Fragment.level` values differ — a paragraph
    contradicting the section it lives inside is rhetorical structure,
    not the contradiction `10-Liminal/Paradoxes/` is for.

    ``True``: restore the pre-FEAT-025 behaviour. Useful when the
    operator wants the cross-level pairs surfaced (e.g. to audit a
    re-atomized vault for splitter mistakes).
    """


class SourcePaths(BaseModel):
    """Source data paths (relative to ``source_drive``)."""

    claude: str = "chatbot-exports/claude/"
    """Claude conversation exports."""

    chatgpt: str = "chatbot-exports/chatgpt/"
    """ChatGPT conversation exports."""

    discord: str = "discord-export/"
    """Discord message exports."""

    gdrive: str = "google-drive-export/"
    """Google Drive staged files."""

    aptitude: str = "projects/aptitude/course-files/"
    """APTITUDE course materials."""

    essays: str = "writing/substack/"
    """Published essays (Substack)."""

    journal: str = "personal/journal/"
    """Personal journal entries."""

    code: str = "projects/"
    """Code project directories."""


def _default_privacy_tier_authority() -> dict[str, float]:
    """Default per-privacy-tier voice-authority multipliers.

    ``OPEN`` (publishable, public-facing) work carries the most authority
    over the voice proxy; ``PERSONAL`` is the baseline; ``UNCLASSIFIED`` is
    treated cautiously below baseline; ``INTIMATE`` is ``0.0`` (it is also
    gated out entirely by default).
    """
    return {"open": 1.5, "personal": 1.0, "unclassified": 0.75, "intimate": 0.0}


def _default_representativeness_authority() -> dict[str, float]:
    """Default per-representativeness voice-authority multipliers.

    The owner's own (``self``) and explicitly ``endorsed`` material outrank
    ``aspirational`` (voices they want to grow into) and ``reference``
    (borrowed) material, which keeps its existing near-zero influence.
    """
    return {"self": 1.0, "endorsed": 0.9, "aspirational": 0.6, "reference": 0.3}


def _default_platform_authority() -> dict[str, float]:
    """Default per-``source.platform`` voice-authority multipliers (#633).

    Audience-facing platforms (essays, Substack posts) outrank private ones
    (journals, DMs, chats) so the voice fingerprint is built primarily from how
    the user writes *for an audience* rather than their texting average. Missing
    platforms fall back to ``1.0`` so a new platform never silently zeroes out.
    """
    return {
        "essay": 2.0,
        "substack": 2.0,
        "journal": 0.1,
        "messages": 0.4,
        "discord": 0.4,
        "chatgpt": 0.3,
        "claude": 0.3,
    }


def _default_audience_authority() -> dict[str, float]:
    """Default per-``audience``-axis voice-authority multipliers (#634).

    The primary audience signal once the FEAT/#634 audience classifier has
    run: a fragment explicitly classified ``audience-facing`` dominates the
    voice fingerprint, ``private`` material is strongly down-weighted, and
    ``mixed`` (the unclassified default and the ambiguous verdict) is neutral
    at ``1.0`` so legacy vaults keep relying on the platform heuristic until
    they are re-classified. Missing values fall back to ``1.0``.
    """
    return {"audience-facing": 2.0, "private": 0.1, "mixed": 1.0}


class VoiceAudienceWeightingConfig(BaseModel):
    """Graduated audience-authority weighting for the voice proxy.

    Replaces the historical binary privacy gate with a multiplier so that
    public-facing fragments dominate how drafts sound. The multiplier is the
    product of a per-``privacy_tier`` and a per-``representativeness`` factor;
    it is applied on top of (not in place of) the ``voice_weight`` gate.
    Disabling it makes every authority ``1.0`` — i.e. the pre-weighting ranking.
    """

    enabled: bool = True
    """Master switch; ``False`` makes every fragment's authority ``1.0``."""

    privacy_tier_authority: dict[str, float] = Field(
        default_factory=_default_privacy_tier_authority,
    )
    """Multiplier per ``privacy_tier`` value (missing tiers default to 1.0)."""

    representativeness_authority: dict[str, float] = Field(
        default_factory=_default_representativeness_authority,
    )
    """Multiplier per ``representativeness`` value (missing default to 1.0)."""

    platform_authority: dict[str, float] = Field(
        default_factory=_default_platform_authority,
    )
    """Multiplier per ``source.platform`` (missing platforms default to 1.0).

    The reliable audience signal: audience-facing platforms (essay, substack)
    outweigh private ones (journal, DMs, chat). Used to scope the voice
    fingerprint to audience-facing documents (#633) on top of the
    privacy/representativeness factors."""

    audience_authority: dict[str, float] = Field(
        default_factory=_default_audience_authority,
    )
    """Multiplier per persisted ``audience`` axis (missing default to 1.0).

    The primary audience signal once the #634 classifier has tagged fragments:
    ``audience-facing`` content dominates the fingerprint, ``private`` is
    down-weighted, ``mixed`` stays neutral. Layered on top of the #633
    platform/privacy/representativeness factors so an unclassified vault
    (every fragment ``mixed`` → 1.0) still falls back to the platform
    heuristic, while a classified vault is scoped by the real axis."""


def _default_sync_sources() -> dict[str, bool]:
    """Default per-source enable toggles for ``creek sync`` (#676).

    The journal and Google Drive are the regularly-updated sources enabled
    by default; the rest are present-but-off so an operator opts in per source.
    """
    return {
        "journal": True,
        "gdrive": True,
        "discord": False,
        "chatgpt": False,
        "claude": False,
        "essays": False,
    }


class SyncConfig(BaseModel):
    """Scheduling configuration for ``creek sync`` (#676 / SPEC R3).

    Holds the per-source enable toggles and the two-tier cadence. The cadence
    values are config-tunable (decision #5) even though the skeleton command
    does not schedule anything yet — a later issue emits the launchd/systemd
    units that read them.
    """

    tier_a_interval_minutes: int = Field(default=30, ge=1)
    """How often the cheap Tier-A pass (pull → incremental ingest → rules
    classify) should run, in minutes (default 30)."""

    tier_b_hour: int = Field(default=3, ge=0, le=23)
    """Local hour (0-23) for the nightly Tier-B pass (LLM classify -> link ->
    index); default 3 (~03:00)."""

    sources: dict[str, bool] = Field(default_factory=_default_sync_sources)
    """Per-source enable toggles; only enabled sources are pulled in Tier A."""

    def enabled_sources(self) -> list[str]:
        """Return the enabled source names in stable (sorted) order."""
        return sorted(name for name, on in self.sources.items() if on)


class DiscordModeToggle(BaseModel):
    """On/off toggle for one Discord ingest mode (#685)."""

    enabled: bool = False
    """Whether this mode is enabled. Opt-in per mode."""


class DiscordSourceConfig(BaseModel):
    """How Discord content reaches the vault — three explicit modes (#685).

    A bot **cannot** read DMs (Discord API limit), so the modes differ by
    coverage and compliance (SPEC R5, decision #3):

    * ``data_package`` — ingest a manually-downloaded Data Package. The clean,
      ToS-compliant fallback; on by default.
    * ``exporter`` — a user-token exporter (DMs + servers). **Off by default**;
      enabling it is a ToS-caveated, opt-in choice.
    * ``bot_capture`` — a bot logging the servers/channels it is in. Clean, but
      cannot see DMs. Off by default.

    Config holds only the mode toggles — the Discord token lives in the
    environment, never here.
    """

    data_package: DiscordModeToggle = Field(
        default_factory=lambda: DiscordModeToggle(enabled=True),
    )
    """The clean Data Package fallback — enabled by default."""

    exporter: DiscordModeToggle = Field(default_factory=DiscordModeToggle)
    """User-token exporter — OFF by default (decision #3); ToS-caveated."""

    exporter_binary: str | None = None
    """Absolute path to the operator-provided user-token exporter binary (#686).

    Non-secret. The exporter is invoked read-only with the Discord token passed
    via the environment only, never on the command line or in this config."""

    exporter_timeout_seconds: int = Field(default=600, ge=1)
    """Hard timeout for the exporter subprocess so a hung binary cannot block
    the run indefinitely (#686)."""

    bot_capture: DiscordModeToggle = Field(default_factory=DiscordModeToggle)
    """Bot message capture for servers/channels — off by default."""

    capture_subpath: str = "discord-capture"
    """Vault-relative dir the bot-capture path writes and Tier-A ingest reads (#687).

    The bot appends ``<capture_subpath>/<channel>/<date>.jsonl`` (relative to the
    vault root) in the lowercase Discord message schema the ``DiscordIngestor``
    already reads; ``creek sync`` reshapes it into the Data-Package layout. Must
    be vault-relative — never absolute, never parent-traversing."""

    @field_validator("exporter_binary")
    @classmethod
    def _validate_exporter_binary(cls, value: str | None) -> str | None:
        """Require an absolute path so the binary resolves predictably (#686)."""
        if value is not None and not Path(value).is_absolute():
            msg = f"exporter_binary must be an absolute path, got {value!r}."
            raise ValueError(msg)
        return value

    @field_validator("capture_subpath")
    @classmethod
    def _validate_capture_subpath(cls, value: str) -> str:
        """Refuse absolute or parent-traversing capture paths — stay in vault (#687)."""
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            msg = f"capture_subpath must be vault-relative, got {value!r}."
            raise ValueError(msg)
        return value


class CreekConfig(BaseSettings):
    """Top-level Creek configuration.

    Values are loaded from a YAML file and can be overridden by
    environment variables prefixed with ``CREEK_`` (e.g.
    ``CREEK_VAULT_PATH``, ``CREEK_TIMEZONE``).
    """

    model_config = SettingsConfigDict(
        env_prefix="CREEK_",
    )

    vault_path: Path = Path()
    """Path to the Obsidian vault root."""

    source_drive: Path = Path()
    """Path to the mounted source drive containing raw exports."""

    timezone: str = "America/Los_Angeles"
    """IANA timezone for timestamp normalisation."""

    llm: LLMRoutingConfig = Field(default_factory=LLMRoutingConfig)
    """Per-stage LLM routing (#642). A legacy flat ``llm`` block is promoted to
    ``default`` by :class:`LLMRoutingConfig`, so existing configs keep working;
    resolve a stage's provider+model via :attr:`model_router`."""

    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    """Embedding model settings."""

    ocr: OCRConfig = Field(default_factory=OCRConfig)
    """OCR processing settings."""

    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    """Fragment linking settings."""

    classification: ClassificationConfig = Field(
        default_factory=ClassificationConfig,
    )
    """Classification pipeline settings."""

    context: ContextConfig = Field(default_factory=ContextConfig)
    """Non-user content handling settings."""

    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    """PII redaction scanner settings."""

    google_drive: GoogleDriveConfig = Field(
        default_factory=GoogleDriveConfig,
    )
    """Google Drive connector settings."""

    cleaning: CleaningConfig = Field(default_factory=CleaningConfig)
    """Data cleaning pipeline settings."""

    compost: CompostConfig = Field(default_factory=CompostConfig)
    """Compost detection settings (FEAT-018: embedding gate + verifier)."""

    lint: LintConfig = Field(default_factory=LintConfig)
    """Lint check toggles (FEAT-008 / FEAT-025)."""

    mining: MiningConfig = Field(default_factory=MiningConfig)
    """Idea-mining thresholds (issue #340)."""

    draft: DraftConfig = Field(default_factory=DraftConfig)
    """Bidirectional grounding-guard thresholds (issue #355)."""

    ai_style: AIStyleConfig = Field(default_factory=AIStyleConfig)
    """AI-style / voice-fidelity thresholds and weights (FEAT-040)."""

    author: AuthorConfig = Field(default_factory=AuthorConfig)
    """Creek Writing Desk author-subsystem settings (FEAT-041)."""

    voice_audience_weighting: VoiceAudienceWeightingConfig = Field(
        default_factory=VoiceAudienceWeightingConfig,
    )
    """Graduated audience-authority weighting for the voice proxy."""

    sources: SourcePaths = Field(default_factory=SourcePaths)
    """Source data path mappings."""

    sync: SyncConfig = Field(default_factory=SyncConfig)
    """Scheduling config for ``creek sync`` — per-source toggles + cadence."""

    discord: DiscordSourceConfig = Field(default_factory=DiscordSourceConfig)
    """Discord ingest mode toggles (data_package | exporter | bot_capture)."""

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Validate that *v* is a recognised IANA timezone.

        Args:
            v: Timezone string to validate.

        Returns:
            The validated timezone string.

        Raises:
            ValueError: If the timezone is not recognised by ``zoneinfo``.
        """
        try:
            ZoneInfo(v)
        except KeyError as exc:
            msg = f"Invalid timezone: {v}"
            raise ValueError(msg) from exc
        return v

    @cached_property
    def model_router(self) -> "ModelRouter":
        """Return a :class:`~creek.classify.llm.router.ModelRouter` for this run.

        The single resolver every LLM call site uses to map a pipeline stage
        (and, from #647, a fragment tier) to a provider+model. Imported lazily
        to avoid a config<->router import cycle, and cached so the several call
        sites in one command share one router instance.
        """
        from creek.classify.llm.router import ModelRouter

        return ModelRouter(self.llm)


def load_config(
    config_path: Path | None = None,
    *,
    warn_on_missing: bool = True,
) -> CreekConfig:
    """Load configuration from a YAML file with environment variable overrides.

    If *config_path* does not exist, returns a ``CreekConfig`` populated
    entirely from defaults and environment variables and (per ARCH-002)
    emits a ``WARNING`` so the operator knows their data-handling
    decisions are being made by the defaults rather than their own
    config. Pass ``warn_on_missing=False`` to silence the warning when
    the caller has already established that the missing file is
    expected (e.g. ``creek init`` runs before any config exists).

    Args:
        config_path: Path to a ``creek_config.yaml`` file. When ``None``
            (the default), :func:`load_config` consults the
            ``CREEK_CONFIG`` environment variable; if that is unset or
            empty, it falls back to ``creek_config.yaml`` in the
            current directory.
        warn_on_missing: When ``True`` (default), log a WARNING if the
            file does not exist. Suppress for CLI commands that
            legitimately operate in the no-config state.

    Returns:
        A fully-validated ``CreekConfig`` instance.

    Raises:
        FileNotFoundError: When ``CREEK_CONFIG`` is set to a path that
            does not exist. The explicit env var declares intent, so a
            silent fallback to defaults would mask the misconfiguration.
    """
    if config_path is None:
        env_value = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
        if env_value:
            config_path = Path(env_value)
            if not config_path.exists():
                msg = (
                    f"{CONFIG_PATH_ENV_VAR} points to {config_path}, "
                    "but no file exists there. Unset the environment "
                    "variable or correct the path."
                )
                raise FileNotFoundError(msg)
        else:
            config_path = Path("creek_config.yaml")

    if config_path.exists():
        with config_path.open() as f:
            data: dict[str, object] = yaml.safe_load(f) or {}
        return CreekConfig.model_validate(data)

    if warn_on_missing:
        logger.warning(
            "Config file %s not found; running with built-in defaults. "
            "Run `creek init --vault <vault>` to write a starter config, "
            "set %s to an absolute config path, or pass --config <path> "
            "explicitly. Pipeline behaviour (privacy, redaction, "
            "cleaning) depends on this file — silent defaults are "
            "usually not what you want.",
            config_path,
            CONFIG_PATH_ENV_VAR,
        )
    return CreekConfig()


_ANTHROPIC_CONSENT_NOTE: str = """\
# -----------------------------------------------------------------------------
# Anthropic cloud classification (opt-in) -- issue #320
# -----------------------------------------------------------------------------
# Switching ``llm.provider`` below to ``anthropic`` enables cloud-based
# classification. Fragment content will be sent to Anthropic's servers,
# so the provider refuses to start unless TWO environment variables are
# set explicitly:
#
#   export ANTHROPIC_API_KEY=sk-ant-...        # your API key
#   export CREEK_ANTHROPIC_CONSENT=1           # consent to data egress
#
# Without ``CREEK_ANTHROPIC_CONSENT`` the ``creek classify --method llm``
# run will abort before iterating any fragments. The default provider
# (``ollama``) is fully local and needs neither variable.
# -----------------------------------------------------------------------------
"""
"""Operator-facing notice prepended to generated configs.

Discoverability is the whole point: fresh users who flip
``llm.provider`` to ``anthropic`` should learn about the
``CREEK_ANTHROPIC_CONSENT`` gate from the config file itself rather
than from a runtime failure mid-classify. The block is YAML-comment-
only so it survives a round-trip through ``yaml.safe_load`` without
introducing any new keys (issue #320).
"""


def generate_default_config(output_path: Path) -> None:
    """Generate a default ``creek_config.yaml`` file.

    Serialises the default ``CreekConfig`` to YAML and writes it to
    *output_path*, providing a starting template that users can customise.

    The output is prefixed with :data:`_ANTHROPIC_CONSENT_NOTE` so a
    first-time user sees the ``CREEK_ANTHROPIC_CONSENT`` requirement
    before flipping ``llm.provider`` to ``anthropic`` (issue #320).

    Args:
        output_path: Destination file path for the generated YAML.
    """
    data: dict[str, object] = CreekConfig().model_dump(mode="json")
    with output_path.open("w") as f:
        f.write(_ANTHROPIC_CONSENT_NOTE)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
