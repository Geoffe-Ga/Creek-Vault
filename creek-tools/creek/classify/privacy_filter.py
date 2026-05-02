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

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.audit import AuditLog
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from creek.models import Fragment


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


def override_elevates(override: PrivacyTierOverride | None) -> bool:
    """Return whether *override* expands access beyond the default.

    The default policy is "include open + personal-as-summary, exclude
    intimate". Anything other than ``None`` or ``OPEN`` raises the bar
    and therefore obliges the caller to write an audit entry.
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
    """Return the fragment's privacy tier as a :class:`PrivacyTier`."""
    return PrivacyTier(fragment.privacy_tier)


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
