"""``creek.classify`` MCP tool — re-classify existing fragments (FEAT-011).

Wraps :func:`creek.classify.classify_engine.run_classify`. The work is
in-place: existing fragment frontmatter is updated; no new content tier
is *created*. The wrapper still records the operation under the audit
log, with ``affected_fragment_ids`` left empty because the engine does
not return them today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek._fslock import VaultLockTimeoutError
from creek.classify.classify_engine import (
    LLMProviderUnavailableError,
    RequestScopedClassifier,
    run_classify,
)
from creek.classify.constants import LLM_METHOD
from creek.classify.llm.providers import RequestKeyRejectedError
from creek.classify.llm.router import IntimateRoutingError
from creek.config import load_vault_config
from creek.models import PrivacyTier
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON
from creek_mcp.audit import MCPAuditLog
from creek_mcp.model_pin import (
    build_request_scoped_provider,
    model_pin_enforced,
    pin_refuses_stage,
    pinned_model_serves,
    request_key_admitted,
)
from creek_mcp.provenance import (
    request_key_inference_source,
    stage_inference_source,
)
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.llm.request_key import RequestKey
    from creek.config import CreekConfig

TOOL_NAME = "creek.classify"
_VALID_METHODS = ("rules", "llm")
_UNPINNED_DETAIL = (
    "only the pinned local model may classify while a model package is "
    "configured or the server runs in container mode"
)
"""Why a pinned-mode LLM pass refused. Fixed text: it never names the URL."""

REQUEST_KEY_REJECTED_REASON = (
    "the writer's request-scoped provider key was refused by its vendor"
)
"""Why a request-keyed LLM pass stopped (#1877). Fixed text: never the key.

The ``/v1`` job worker matches this exact string to publish
``failure_code: request_key_rejected``, so it is a constant, not prose.
"""


def _require_pinned_classifier(config: CreekConfig) -> None:
    """Refuse an LLM pass whose classification stage is not the pinned model.

    The model-pin rule of :mod:`creek_mcp.model_pin` (#1849), applied before
    any fragment is read. Only the tier-less ``classification`` stage is
    checked: the router redirects only a *cloud* stage for INTIMATE
    fragments, so when this stage is the pinned local model the INTIMATE
    route resolves to the same one, and when it is not, the pass is refused
    here anyway. The config is loaded once per call and handed to the
    engine, so the stage checked is the stage every fragment uses.

    A cloud stage is refused from its config before any provider is built:
    its constructor raises a bare ``RuntimeError`` when the key or consent
    is missing, which is the normal state in a container, and that would
    escape this tool instead of becoming its structured refusal. Any other
    build failure is turned into the same refusal.

    Args:
        config: The classified vault's own configuration.

    Raises:
        LLMProviderUnavailableError: In a pin mode, when the stage is not the
            pinned model served at its digest over loopback.
    """
    from creek.classify.llm.providers import build_provider

    if not model_pin_enforced():
        return
    cfg = config.model_router.resolve("classification")
    unpinned = LLMProviderUnavailableError(
        provider=cfg.provider, detail=_UNPINNED_DETAIL
    )
    if pin_refuses_stage(cfg):
        raise unpinned
    try:
        provider = build_provider(cfg)
    except RuntimeError as exc:
        raise unpinned from exc
    if not pinned_model_serves(provider):
        raise unpinned


def _request_scoped_classifier(
    config: CreekConfig, request_key: RequestKey | None
) -> RequestScopedClassifier | None:
    """Return the writer's provider for this LLM pass, or ``None`` (ADR-0015).

    ``None`` — the configured, pinned routing exactly as before — unless a
    request key was supplied *and* this managed vault admits it. The INTIMATE
    route is not the key's to decide: it is the router's local route, judged
    here against the model pin so the engine can mark it unavailable up front.

    Args:
        config: The classified vault's configuration.
        request_key: The writer's request-scoped key, or ``None``.

    Returns:
        The request-scoped provider for OPEN/PERSONAL fragments, or ``None``.
    """
    if request_key is None or not request_key_admitted(
        request_key.stage_config(), request_key
    ):
        return None
    return RequestScopedClassifier(
        provider=build_request_scoped_provider(request_key),
        config=request_key.stage_config(),
        intimate_route_serves=_intimate_route_serves(config),
    )


def _intimate_route_serves(config: CreekConfig) -> bool:
    """Return whether the vault's own INTIMATE classification route may serve.

    Outside both pin modes that is the classifier's own availability probe, so
    ``True`` here defers to it. In a pin mode the route must be the pinned
    model at its digest over loopback, exactly as :func:`_require_pinned_classifier`
    demands of the whole pass when no request key is involved.

    Args:
        config: The classified vault's configuration.

    Returns:
        ``False`` when a pin mode applies and the INTIMATE route is not the
        pinned model, or when no local INTIMATE route exists.
    """
    from creek.classify.llm.providers import build_provider

    if not model_pin_enforced():
        return True
    try:
        cfg = config.model_router.resolve("classification", PrivacyTier.INTIMATE)
    except IntimateRoutingError:
        return False
    if pin_refuses_stage(cfg):
        return False
    try:
        provider = build_provider(cfg)
    except (RuntimeError, ValueError):
        return False
    return pinned_model_serves(provider)


def _pass_inference_source(
    config: CreekConfig,
    method: str,
    request_scoped: RequestScopedClassifier | None,
    request_key: RequestKey | None,
) -> str | None:
    """Return the provenance value for one classification pass, or ``None``.

    One pass, one configured stage, so one label: the route is decided before
    the walk starts and does not change per fragment. A tier *redirection* can
    still send an INTIMATE fragment to the local ``default`` mid-pass, which is
    why the label is a statement about the route the pass was **admitted on**
    rather than a count of what each fragment did — and it never contradicts a
    socket spy, because the only destination such a pass dials is the one named
    here.

    Args:
        config: The classified vault's own configuration.
        method: The classifier that ran.
        request_scoped: The writer's provider for this pass, or ``None``.
        request_key: The writer's request-scoped key, or ``None``.

    Returns:
        A published provenance value, or ``None`` for a pass that called no
        model (``method=rules``) or a route this contract does not label.
    """
    if method != LLM_METHOD:
        return None
    if request_scoped is not None and request_key is not None:
        return request_key_inference_source(request_key)
    return stage_inference_source(
        config.model_router.resolve("classification").provider
    )


def _rules_fallback(
    *,
    vault_path: Path,
    force: bool,
    retier: bool,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
    reason: str,
) -> dict[str, Any]:
    """Run the ``rules`` pass in place of an unavailable ``llm`` one.

    Args:
        vault_path: Vault root to classify.
        force: Passed through to the rules pass.
        retier: Passed through to the rules pass.
        privacy_tier_ceiling: Passed through to the rules pass.
        consumer: Passed through to the rules pass.
        reason: Why the ``llm`` pass was unavailable — the refusal's text.

    Returns:
        The rules pass's result, marked with ``fallback_from`` and
        ``fallback_reason`` on success; a rules refusal (a busy vault)
        is returned unmarked, since nothing fell back.
    """
    result = classify_tool(
        vault_path=vault_path,
        method="rules",
        force=force,
        retier=retier,
        privacy_tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    if result.get("status") == "ok":
        result["fallback_from"] = LLM_METHOD
        result["fallback_reason"] = reason
    return result


def classify_tool(
    *,
    vault_path: Path,
    method: str = "rules",
    force: bool = False,
    retier: bool = False,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    request_key: RequestKey | None = None,
    fallback_to_rules: bool = False,
) -> dict[str, Any]:
    """Run the chosen classifier and return per-method counts.

    Classification rewrites the frontmatter of existing fragments. The
    tier-ceiling parameter is recorded for the audit trail; it does not
    gate execution because classify never creates new fragments — it
    only updates the labels on existing ones. It is also not the
    *widening* operation the name might suggest: the tier pass is
    escalate-only, so a run can move a fragment out of a remote
    consumer's reach and never into it.

    All of that is about the *response* and the frontmatter, and it says
    nothing about the prompt channel — where the ceiling is not a control
    either. With ``method="llm"`` every fragment's **title and body**
    under ``01-Fragments`` is shown to the configured classification
    provider whatever ceiling was declared, ``open`` included — both are
    interpolated into the prompt by ``build_classification_prompt``, each
    capped at 8192 chars by ``_sanitise_for_prompt``. ``INTIMATE`` is
    redirected to a local provider by
    ``ModelRouter._enforce_local_for_intimate`` (#647/#666); ``PERSONAL``
    is not. See ``creek_mcp.read_gate._CLASSIFY_PROMPT_CHANNEL_RATIONALE``
    for the full statement of what does and does not gate that walk
    (#1274). With a model package configured, or in container mode, the
    stage must also be the pinned local model (#1849), so in those modes no
    fragment text reaches a cloud provider at any tier.

    Args:
        vault_path: Vault root to classify.
        method: ``"rules"`` or ``"llm"``.
        force: Overwrite ``classification_method: manual`` decisions.
        retier: Re-derive the privacy tier of fragments that already
            carry a concrete one, persisting it only when the new
            verdict is *stricter* (issue #1106). Surfaced here because
            it is the only setting under which a caller who declared the
            wrong tier at write time can be corrected — a case
            ``POST /v1/uploads`` makes routine, since it requires an
            explicit tier and never re-derives it (#1497).
        privacy_tier_ceiling: Recorded for the audit trail.
        consumer: Who is calling, for the audit trail.
        request_key: A writer's request-scoped key (ADR-0015), or ``None``.
            On a managed vault, an LLM pass classifies OPEN and PERSONAL
            fragments with the writer's provider; INTIMATE fragments still
            take the router's local route or are left unclassified. Never
            written to the audit record.
        fallback_to_rules: When an ``llm`` pass has no available classifier,
            run the model-free ``rules`` pass instead of refusing. The result
            then carries ``fallback_from: "llm"`` and ``fallback_reason`` (the
            refusal's fixed text), so the caller can report the classifier
            that actually ran. Set by the ``/v1`` job worker on a managed
            vault, which ships no local model and refuses cloud consent: there
            a keyless ``llm`` job could otherwise never classify anything.

    Returns:
        A structured result: per-method counts on success, or a
        refusal for an unknown method, an unreachable provider, or a
        request-scoped key its vendor refused
        (:data:`REQUEST_KEY_REJECTED_REASON`, #1877).
    """
    if method not in _VALID_METHODS:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unknown method {method!r}; supported: {', '.join(_VALID_METHODS)}"
            ),
        )
    # This config selects llm.provider — i.e. which model is shown the
    # vault's text — so it must be the classified vault's own file (#1409).
    config = load_vault_config(vault_path)
    try:
        request_scoped = (
            _request_scoped_classifier(config, request_key)
            if method == LLM_METHOD
            else None
        )
        if method == LLM_METHOD and request_scoped is None:
            _require_pinned_classifier(config)
        summary = run_classify(
            vault_path=vault_path,
            config=config,
            method=method,
            force=force,
            retier=retier,
            request_scoped=request_scoped,
        )
    except LLMProviderUnavailableError as exc:
        # The engine refuses to iterate when the configured LLM provider is
        # unreachable. Translate the no-work-started condition to a stable
        # shape instead of an unhandled ``RuntimeError`` traceback.
        if fallback_to_rules and method == LLM_METHOD:
            return _rules_fallback(
                vault_path=vault_path,
                force=force,
                retier=retier,
                privacy_tier_ceiling=privacy_tier_ceiling,
                consumer=consumer,
                reason=str(exc),
            )
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )
    except VaultLockTimeoutError:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=CONTENT_MUTATION_BUSY_REASON,
        )
    except RequestKeyRejectedError:
        # The writer's key was refused: the pass stopped at the first refusal
        # rather than reporting a completed pass that classified nothing. The
        # redacted error is already in the log; the reason here is fixed text.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=REQUEST_KEY_REJECTED_REASON,
        )
    # Classify rewrites existing frontmatter in place; it does not
    # produce a new file, so ``created_path`` is omitted from the audit
    # entry (per the audit-schema convention documented in docs/mcp.md).
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"method": method, "force": force, "retier": retier},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "total": summary.total,
        "classified": summary.classified,
        "preserved_manual": summary.preserved_manual,
        # Issue #321: prior-LLM-preserved fragments used to roll into
        # ``preserved_manual``, misattributing automated state to the
        # operator. Surface them as a separate field so downstream
        # consumers can present the two reasons distinctly.
        "preserved_llm": summary.preserved_llm,
        "skipped_high_confidence": summary.skipped_high_confidence,
        # Issue #1356: the LLM-failed skips used to roll into the field above,
        # so a run against a downed provider reported itself as a run in which
        # the rules were confident. Surfaced separately because only this one
        # tells the consumer the corpus is under-classified and the pass is
        # worth repeating.
        "llm_call_failed": summary.llm_call_failed,
        # Issue #876: how many fragments this run gave a real privacy
        # tier. Surfaced separately from ``classified`` because the tier
        # pass also runs on fragments the resume short-circuit preserved.
        "privacy_tiers_assigned": summary.privacy_tiers_assigned,
        # Issue #1106 / #1570: the subset of the above where an
        # already-recorded tier was replaced by a stricter one. Reported
        # separately because ``privacy_tiers_assigned`` alone cannot tell
        # "this fragment had no tier" from "this fragment had the wrong
        # one", and only the second is evidence that ``retier`` did work.
        "retiered": summary.retiered,
        # Issue #877: how many fragments this run raised to a stronger
        # praxis potential. Surfaced for the same reason as the tier count
        # — the field previously had no producer at all, and a silent
        # producer is how that bug survived.
        "praxis_marked": summary.praxis_marked,
        # Issue #878: how many fragments this run gained a hashtag ``tags``
        # entry on. Same reasoning again — the field had no producer at all
        # until #878, and an invisible producer is how that bug survived
        # 35,330 fragments and an empty Tag Garden.
        "tags_extracted": summary.tags_extracted,
        # Issue #1357: how many fragments this run refused to treat as
        # already-LLM-classified because the pre-#1358 weighted soft-failure
        # path stamped them without any LLM running. Surfaced here for the
        # same reason as on the CLI: every other field on this payload counts
        # such a fragment as done.
        "healed_unearned_llm": summary.healed_unearned_llm,
        "errors": list(summary.errors),
        # ADR-0016 Decision 1 item 11: the route this pass was admitted on,
        # or ``None`` for a pass that called no model at all. Absent from the
        # wire rather than guessed — see :func:`_pass_inference_source`.
        "inference_source": _pass_inference_source(
            config, method, request_scoped, request_key
        ),
    }
