"""Single source of truth for tier filtering in generation flows.

Section 13.2 of the Creek Ontology promises that intimate fragments are
excluded from generation prompts by default and that personal fragments
contribute summaries (not full bodies). This module owns the *one*
implementation of that promise so ``mine``, ``draft``, ``report``, and
``skills`` cannot drift out of agreement with each other.

The filter accepts an optional :class:`PrivacyTierOverride` representing
the operator-supplied ``--include-tier`` flag. When the override raises
the included tier above the default, the caller is responsible for
writing an entry to the privacy audit log via
:func:`record_privacy_override`; the helper exposes
:func:`override_elevates` so callers can detect when an audit is owed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

from creek.audit import AuditLog
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from creek.models import Fragment

logger = logging.getLogger(__name__)


PRIVACY_AUDIT_RELPATH = Path("00-Creek-Meta/audit/privacy.jsonl")
"""Canonical privacy-override audit log location under the vault root."""


class PrivacyTierOverride(StrEnum):
    """``--include-tier`` flag values.

    The values are ordered so that ``OPEN`` is the most restrictive
    (default) and ``ALL`` is the broadest override; the comparison is
    done by name, not lexically.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    ALL = "all"


def override_elevates(
    override: PrivacyTierOverride | None,
) -> TypeGuard[PrivacyTierOverride]:
    """Return whether *override* expands access beyond the default.

    The default policy is "include open + personal-as-summary, exclude
    intimate". Anything other than ``None`` or ``OPEN`` raises the bar
    and therefore obliges the caller to write an audit entry.

    Returns a :class:`~typing.TypeGuard` so callers that branch on this
    predicate get the narrowed non-``None`` type for free — encoding the
    fact that ``None`` and ``OPEN`` are operationally equivalent at the
    type level prevents the redundant ``override is None`` guard the
    PR #193 review flagged from ever reappearing.
    """
    if override is None:
        return False
    return override is not PrivacyTierOverride.OPEN


def _summarize_personal(fragment: Fragment) -> str:
    """Replace a personal fragment's body with a title-only summary.

    Title-only is the v1 contract documented in SEC-006; richer summaries
    can land later without changing the call sites.
    """
    title = fragment.title.strip() or fragment.id
    return f"[Personal-tier summary: {title}]"


def _allows_intimate(override: PrivacyTierOverride | None) -> bool:
    """Return ``True`` when intimate fragments may pass through."""
    return override in (PrivacyTierOverride.INTIMATE, PrivacyTierOverride.ALL)


def _allows_full_personal_body(override: PrivacyTierOverride | None) -> bool:
    """Return ``True`` when personal fragments contribute their full body."""
    return override in (
        PrivacyTierOverride.PERSONAL,
        PrivacyTierOverride.INTIMATE,
        PrivacyTierOverride.ALL,
    )


def filter_fragments_by_tier(
    fragments: Iterable[tuple[Fragment, str]],
    *,
    override: PrivacyTierOverride | None = None,
) -> Iterator[tuple[Fragment, str]]:
    """Yield ``(fragment, body)`` pairs honouring tier policy.

    Default behaviour:

    * ``intimate`` → excluded.
    * ``personal`` → included with body replaced by a title-only summary.
    * ``open`` / ``public`` → included with full body.
    * ``unclassified`` → treated as ``open`` (pass through with full
      body). Fragments without an explicit privacy tier are presumed
      non-sensitive; the classifier should backfill an explicit tier
      before they enter sensitive flows. Operators uncomfortable with
      this default should run ``creek classify`` first so every
      fragment carries a deliberate tier.

    Override semantics:

    * ``OPEN`` (or ``None``): default behaviour.
    * ``PERSONAL``: personal bodies pass through unredacted; intimate
      remains excluded.
    * ``INTIMATE`` / ``ALL``: every tier passes through with its full
      body.

    Args:
        fragments: Iterable of ``(fragment, body)`` pairs from the
            caller's vault scan.
        override: Optional :class:`PrivacyTierOverride` from
            ``--include-tier``.
    """
    for fragment, body in fragments:
        tier = _tier_of(fragment)
        if tier == PrivacyTier.INTIMATE and not _allows_intimate(override):
            continue
        if tier == PrivacyTier.PERSONAL and not _allows_full_personal_body(override):
            yield fragment, _summarize_personal(fragment)
            continue
        yield fragment, body


def _tier_of(fragment: Fragment) -> PrivacyTier:
    """Return the fragment's privacy tier as a :class:`PrivacyTier`.

    Pydantic's :class:`~creek.models.Fragment` validator constrains
    ``privacy_tier`` to the enum values, so unrecognised strings should
    not normally reach this helper. The defensive ``except`` exists for
    fragments that bypassed Pydantic validation (e.g. legacy data hand-
    edited in the vault, or a future schema migration that adds a tier
    we don't yet know about). Failing closed to ``INTIMATE`` ensures an
    unknown classification is treated as the most-restrictive tier
    rather than silently defaulting to ``open`` and exposing the body.
    """
    try:
        return PrivacyTier(fragment.privacy_tier)
    except ValueError:
        logger.warning(
            "Fragment %s carries unrecognised privacy_tier %r; "
            "treating as INTIMATE for fail-closed filtering. "
            "Re-run `creek classify` to assign a recognised tier.",
            fragment.id,
            fragment.privacy_tier,
        )
        return PrivacyTier.INTIMATE


def record_privacy_override(
    *,
    vault_path: Path,
    command: str,
    fragment_ids: Iterable[str],
    operator: str,
    override: PrivacyTierOverride,
) -> None:
    """Append a privacy-override audit entry to ``audit/privacy.jsonl``.

    Args:
        vault_path: Vault root under which the audit log lives.
        command: CLI subcommand name (e.g. ``"mine"``, ``"draft"``).
        fragment_ids: Fragment IDs included by the override.
        operator: Identity of the operator that issued the override.
        override: The :class:`PrivacyTierOverride` value that was
            applied.
    """
    log = AuditLog(vault_path / PRIVACY_AUDIT_RELPATH)
    payload: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "command": command,
        "operator": operator,
        "include_tier": override.value,
        "fragment_ids": list(fragment_ids),
    }
    log.append(payload)


@dataclass(frozen=True)
class PreSaveFilterResult:
    """Outcome of :func:`pre_save_filter`.

    Attributes:
        vault_body: Markdown body to write into the vault note. For
            non-open tiers this is a title-only summary.
        stub_body: Full body destined for the gitignored intimate-stub
            file, or ``None`` when the tier does not require off-vault
            stashing.
        stub_relpath: Vault-relative path under which the stub will be
            written (``10-Liminal/Compost/intimate-stubs/<slug>.md``),
            or ``None`` when no stub is needed.
    """

    vault_body: str
    stub_body: str | None
    stub_relpath: Path | None


_PRE_SAVE_INTIMATE_STUB_RELPATH = Path("10-Liminal/Compost/intimate-stubs")
"""Mirror of :data:`creek.save.router.INTIMATE_STUB_RELPATH`.

Duplicated here to keep ``privacy_filter`` free of a circular import
back into the save module. The save module owns the canonical
constant; this private mirror is only used to compose the stub
relative path returned to callers.
"""


def _title_only_summary(title: str | None) -> str:
    """Return the body that gets written when only the title is safe."""
    safe_title = (title or "").strip() or "(untitled)"
    return f"[Tier-redacted summary: {safe_title}]\n"


def _stub_relpath_for(title: str | None) -> Path:
    """Compose the gitignored stub path for an intimate body."""
    raw = (title or "intimate").strip().lower() or "intimate"
    slug = re.sub(r"[^\w\-]+", "-", raw).strip("-") or "intimate"
    return _PRE_SAVE_INTIMATE_STUB_RELPATH / f"{slug[:64]}.md"


def pre_save_filter(
    body: str,
    *,
    tier: PrivacyTier,
    title: str | None,
    full_body: bool = False,
) -> PreSaveFilterResult:
    """Apply tier-aware redaction to a ``creek save`` body.

    The contract follows FEAT-009's "privacy enforcement" block:

    * ``open`` — full body is written into the vault.
    * ``personal`` — body is replaced with a title-only summary unless
      *full_body* is explicitly ``True``.
    * ``intimate`` — body is replaced with a title-only summary in the
      vault, and the full body is routed to the gitignored
      ``10-Liminal/Compost/intimate-stubs/`` directory.

    Args:
        body: The raw answer body the operator wants to file back.
        tier: Privacy tier inherited from provenance or supplied via
            ``--tier``.
        title: Optional title — used to compose the title-only summary
            and the stub filename.
        full_body: When ``True``, allow personal-tier bodies through
            unredacted. Ignored for ``intimate``.

    Returns:
        A :class:`PreSaveFilterResult` describing what to write where.
    """
    if tier == PrivacyTier.INTIMATE:
        return PreSaveFilterResult(
            vault_body=_title_only_summary(title),
            stub_body=body,
            stub_relpath=_stub_relpath_for(title),
        )
    if tier == PrivacyTier.PERSONAL and not full_body:
        return PreSaveFilterResult(
            vault_body=_title_only_summary(title),
            stub_body=None,
            stub_relpath=None,
        )
    return PreSaveFilterResult(
        vault_body=body,
        stub_body=None,
        stub_relpath=None,
    )


def parse_include_tier(value: str | None) -> PrivacyTierOverride | None:
    """Parse a CLI ``--include-tier`` value into the typed enum.

    Returns ``None`` for an unset flag so the call site can short-circuit
    without a noisy comparison; raises :class:`ValueError` with the
    canonical option list when the value is malformed so the CLI can
    re-raise with ``typer.Exit(2)``.
    """
    if value is None:
        return None
    try:
        return PrivacyTierOverride(value.lower())
    except ValueError as exc:
        msg = (
            f"Unknown --include-tier {value!r}. "
            f"Use one of: {', '.join(member.value for member in PrivacyTierOverride)}."
        )
        raise ValueError(msg) from exc
