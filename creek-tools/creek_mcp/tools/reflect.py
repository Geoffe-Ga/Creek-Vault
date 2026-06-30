"""``creek.reflect`` MCP tool — anchored Higher-Self margin notes (#751).

Takes one journal entry (raw ``content`` or an ``entry_ref`` fragment id) plus a
privacy-tier ceiling and returns ``{notes: [{quote, kind, note}], essay?}`` —
warm, second-person, anti-guru reflections that mirror the user's own wisdom,
grounded in retrieval over their corpus. Distinct from the essay-shaped
``draft``/``author``/``mine`` surface; this is the reflection surface Adepthood's
journal needs.

Three guarantees this module enforces:

- **INTIMATE never egresses.** The LLM callable is obtained from a *tier-keyed
  factory* (``llm_factory(tier)``). The production factory resolves through
  :class:`creek.classify.llm.router.ModelRouter`, whose
  ``_enforce_local_for_intimate`` chokepoint redirects an INTIMATE call to the
  local ``default`` model — or raises :class:`IntimateRoutingError` rather than
  egressing. This module never picks a provider or re-checks the tier itself; it
  only derives the routing tier (failing closed to INTIMATE) and hands it over.
- **Quotes are verbatim.** Every returned ``quote`` is validated to be a
  substring of the entry (whitespace-normalised). Model-supplied spans that are
  not are dropped — the client re-anchors verbatim quotes to character offsets
  itself, so a hallucinated span must never reach it.
- **Care boundary (#753).** A ``care_guard`` seam is injected; when it flags the
  entry, the tool escalates and never calls the model. The real guard lands in
  #753; until then the seam is unset (no gating) but wired.

The LLM and retrieval are injected so the tool is unit-testable with no live
calls; ``build_server`` supplies the production factory + retrieval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, to_privacy_override

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

TOOL_NAME = "creek.reflect"

_DEFAULT_MAX_NOTES = 6
_ALLOWED_KINDS = {"reframe", "fear", "longing", "value", "pattern", "tension", "gift"}


class _LLM(Protocol):
    """A prompt-completion callable returning the model's raw text."""

    def __call__(self, prompt: str) -> str:
        """Return the completion for *prompt*."""


class _LLMFactory(Protocol):
    """Builds a tier-routed LLM callable; INTIMATE is forced local by the router."""

    def __call__(self, tier: PrivacyTier) -> _LLM:
        """Return an LLM callable routed for *tier* (may raise to refuse)."""


# A retrieval callable: ``(query, vault, override) -> grounding snippets``.
_Retrieve = "Callable[[str, Path, object], list[str]]"
# A care guard: ``(entry_text) -> escalation reason or None``.
_CareGuard = "Callable[[str], str | None]"

# ``ALL`` admits intimate content, so a reflection under it must route as INTIMATE.
_CEILING_ROUTING_TIER: dict[TierCeiling, PrivacyTier] = {
    TierCeiling.OPEN: PrivacyTier.OPEN,
    TierCeiling.PERSONAL: PrivacyTier.PERSONAL,
    TierCeiling.INTIMATE: PrivacyTier.INTIMATE,
    TierCeiling.ALL: PrivacyTier.INTIMATE,
}


def _routing_tier(ceiling: TierCeiling) -> PrivacyTier:
    """Map a ceiling to the routing tier, failing closed to INTIMATE.

    The router's cloud gate keys on :class:`PrivacyTier`, never on
    :class:`TierCeiling`, so the ceiling is translated to the most-sensitive
    tier it admits. An unrecognised ceiling routes as INTIMATE (local-only).
    """
    return _CEILING_ROUTING_TIER.get(ceiling, PrivacyTier.INTIMATE)


def _normalise(text: str) -> str:
    """Collapse runs of whitespace so quote matching tolerates LLM reflow."""
    return " ".join(text.split())


def _is_verbatim(quote: str, entry: str) -> bool:
    """Return whether *quote* appears in *entry* (whitespace-normalised)."""
    needle = _normalise(quote)
    return bool(needle) and needle in _normalise(entry)


def _parse_notes(response_text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the model response into raw notes + an optional essay.

    Strips a single code fence, then parses JSON (falling back to a YAML
    safe-load for fenced YAML), and reads ``notes`` / ``essay``. Any structural
    problem degrades to ``([], None)`` rather than raising — a malformed model
    turn yields no notes, never a crash or unvalidated output.
    """
    import json

    text = response_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) >= 2 else lines)
        text = text.removesuffix("```")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        try:
            import yaml

            data = yaml.safe_load(text)
        except (ValueError, TypeError):
            return [], None
    if not isinstance(data, dict):
        return [], None
    raw_notes = data.get("notes")
    notes = (
        [n for n in raw_notes if isinstance(n, dict)]
        if isinstance(raw_notes, list)
        else []
    )
    essay = data.get("essay")
    return notes, essay if isinstance(essay, str) and essay.strip() else None


def _clean_notes(
    raw_notes: list[dict[str, Any]], entry: str, *, max_notes: int
) -> list[dict[str, str]]:
    """Keep only well-formed notes whose quote is a verbatim entry substring.

    A surviving note carries exactly ``quote`` / ``kind`` / ``note``; the kind is
    constrained to the known vocabulary (unknown kinds fall back to ``pattern``).
    """
    cleaned: list[dict[str, str]] = []
    for note in raw_notes:
        quote, kind, body = note.get("quote"), note.get("kind"), note.get("note")
        if not isinstance(quote, str) or not isinstance(body, str) or not body.strip():
            continue
        if not _is_verbatim(quote, entry):
            continue
        safe_kind = (
            kind if isinstance(kind, str) and kind in _ALLOWED_KINDS else "pattern"
        )
        cleaned.append({"quote": quote, "kind": safe_kind, "note": body})
        if len(cleaned) >= max_notes:
            break
    return cleaned


def _build_prompt(entry: str, grounding: list[str]) -> str:
    """Compose the margin-note prompt from the entry and grounding snippets."""
    sources = "\n\n".join(f"- {snippet}" for snippet in grounding) or "(none)"
    kinds = ", ".join(sorted(_ALLOWED_KINDS))
    schema = (
        'Return JSON: {"notes": [{"quote": <verbatim span copied from the ENTRY>, '
        '"kind": <one of: ' + kinds + '>, "note": <a few warm sentences>}]}. '
        "Every quote MUST be copied verbatim from the ENTRY."
    )
    return (
        "You are the writer's own Higher Self leaving warm, second-person margin "
        "notes on their journal entry. Mirror their wisdom back; never advise from "
        "above, never diagnose, never console with platitudes. Ground every note in "
        "their own words and the source fragments below.\n\n"
        + schema
        + f"\n\nSOURCE FRAGMENTS:\n{sources}\n\nENTRY:\n{entry}"
    )


def _resolve_entry(content: str | None, entry_ref: str | None, vault_path: Path) -> str:
    """Return the entry text from raw *content* or a fragment *entry_ref*.

    ``entry_ref`` is resolved to a fragment markdown file under ``01-Fragments``;
    its body becomes the entry. A blank result (no usable source) yields ``""``,
    which the caller turns into a refusal.
    """
    if content and content.strip():
        return content
    if entry_ref:
        import frontmatter

        for path in (vault_path / "01-Fragments").rglob("*.md"):
            post = frontmatter.load(path)
            if str(post.metadata.get("id", "")) == entry_ref:
                body: str = post.content
                return body
    return ""


def reflect_tool(
    *,
    vault_path: Path,
    llm_factory: _LLMFactory,
    content: str | None = None,
    entry_ref: str | None = None,
    retrieve: Callable[[str, Path, object], list[str]] | None = None,
    care_guard: Callable[[str], str | None] | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
    max_notes: int = _DEFAULT_MAX_NOTES,
) -> dict[str, Any]:
    """Return anchored Higher-Self margin notes for a single journal entry.

    Args:
        vault_path: Vault root (for retrieval grounding + the audit log).
        llm_factory: Tier-keyed LLM builder; ``llm_factory(tier)`` returns the
            completion callable. The production factory routes INTIMATE local.
        content: The raw entry text. Mutually exclusive-ish with *entry_ref*;
            *content* wins when both are given.
        entry_ref: A fragment id whose body is the entry, when *content* is
            absent.
        retrieve: ``(query, vault, override) -> snippets`` grounding source;
            defaults to the corpus retrieval specialist.
        care_guard: ``(entry) -> reason | None``; a non-``None`` reason escalates
            to a human and skips the model entirely (#753 seam).
        privacy_tier_ceiling: The ceiling; gates corpus admission and, via
            :func:`_routing_tier`, the local-vs-cloud routing tier.
        consumer: Free-form consumer id for the audit log.
        max_notes: Cap on returned notes.

    Returns:
        ``{status, tool, tier_ceiling, ...}`` — ``ok`` with ``notes`` (+ optional
        ``essay``), ``escalate`` with a ``reason``, or a structured refusal.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"has_entry_ref": entry_ref is not None},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )

    entry = _resolve_entry(content, entry_ref, vault_path)
    if not entry.strip():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason="no entry content supplied",
        )

    if care_guard is not None:
        reason = care_guard(entry)
        if reason:
            return {
                "status": "escalate",
                "tool": TOOL_NAME,
                "tier_ceiling": privacy_tier_ceiling.value,
                "reason": reason,
            }

    tier = _routing_tier(privacy_tier_ceiling)
    override = to_privacy_override(privacy_tier_ceiling)
    grounder = retrieve if retrieve is not None else _default_retrieve
    grounding = grounder(entry, vault_path, override)

    try:
        llm = llm_factory(tier)
        response_text = llm(_build_prompt(entry, grounding))
    except (IntimateRoutingError, RuntimeError) as exc:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"reflection unavailable: {type(exc).__name__}",
        )

    raw_notes, essay = _parse_notes(response_text)
    notes = _clean_notes(raw_notes, entry, max_notes=max_notes)
    result: dict[str, Any] = {
        "status": "ok" if notes else "empty",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "routed_tier": tier.value,
        "notes": notes,
    }
    if essay is not None:
        result["essay"] = essay
    return result


def _default_retrieve(query: str, vault_path: Path, override: object) -> list[str]:
    """Production grounding: top corpus fragments related to *query*.

    Lazily imports the author retrieval specialist so the server still boots
    when its (heavier) deps are unavailable; any retrieval failure degrades to
    no grounding rather than failing the reflection.
    """
    try:
        from creek.author.agents import RetrievalSpecialist

        bundle = RetrievalSpecialist().gather(query, vault_path, override=override)
        return [claim.text for claim in bundle.claims]
    except Exception:
        return []
