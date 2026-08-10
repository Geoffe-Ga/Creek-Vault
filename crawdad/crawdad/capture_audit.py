"""Audit and purge a pre-#1052 bot-capture tree (#1264).

#1052 moved the capture write behind the allowlist + tier gate, so no *new*
record can name a non-allowlisted user or channel, and no ``intimate``/``all``
channel is written at all. It did nothing about what a pre-fix build already
wrote. An operator who ran with ``capture_enabled: true`` before that fix has a
``<vault>/<capture_subpath>/`` tree holding messages from strangers, other
bots, and channels they declared ``intimate`` — all of it untiered, and all of
it still ingestible, because ``creek.ingest.discord.stage_capture_as_data_package``
iterates every channel dir with no allowlist or tier filter and the result lands
as ``unclassified``, which ranks *with* ``personal``.

This module is the remediation helper. Two design rules bound it:

**Audit is the default; purge is explicit and dry-run first.** Deletion is
irreversible, so nothing is removed until the operator has been shown, in the
same vocabulary, exactly what would go.

**Privacy is a one-way ratchet.** Every path here can only ever make the
on-disk tree *less* exposed. Nothing widens what is ingestible and nothing
lowers a tier — the worst outcome of a bug is that a refused directory survives
and the operator deletes it by hand, which is the status quo.

The purge unit
--------------

**A whole channel directory, never an individual record.** A capture record
carries only ``author.name`` — a mutable display name, not a user id — and no
channel id at all (:func:`crawdad.capture._record_for`). So of the three things
the gate checks, only the channel-level ones are re-derivable from disk:

===========================  ====================================
Gate condition               Re-derivable from a capture record?
===========================  ====================================
channel in allowlist         only when the dir is id-labelled
channel tier admitted        only when the dir is id-labelled
author in ``allowed_user_ids``  **no** — no user id is stored
===========================  ====================================

Selectively deleting the records of a non-allowlisted *user* would therefore
mean matching on a spoofable display name and either destroying legitimate
messages or leaving the leak while reporting it fixed. Neither is acceptable,
so purge does not attempt it: it operates on directories, and the audit reports
the distinct author names per directory so the operator can see who is in there
and decide. That limitation is printed in the tool's own output, not buried
here.

Directory labels
----------------

:func:`crawdad.capture._channel_label` names each directory after the channel's
*name*, falling back to its numeric id only when the name is unusable. A name
cannot be mapped back to a channel id offline, so a name-labelled directory gets
no verdict at all (:attr:`CaptureVerdict.UNRESOLVED`) — guessing would be the
one way this helper could delete admitted data. Those directories are reported
and left alone until the operator names one explicitly with ``--channel``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from crawdad.capture import iter_channel_records
from crawdad.config import CAPTURE_ADMITTED_TIERS, DEFAULT_CHANNEL_TIER

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from crawdad.config import CrawDadConfig

# Why an explicit request for a gate-admitted directory is turned down. Purge is
# a remediation tool for pre-fix leakage, not a general delete-my-data command;
# refusing here is what makes "never deletes what the gate would admit" a
# property of the tool rather than of how carefully it was invoked.
DECLINE_ADMITTED: str = (
    "the current capture gate admits this channel, so purge will not delete it "
    "(remove it by hand if you want it gone)"
)

# Why a requested label matched nothing. Reported rather than ignored so a typo
# — or a `..` traversal attempt — is visible instead of looking like success.
DECLINE_UNKNOWN: str = "no such channel directory under the capture root"

# Fewest digits a capture directory label must have before it is read as a
# Discord channel id rather than a channel name. See :func:`_resolve_channel_id`
# for the epoch arithmetic and why the floor errs on the safe side.
_MIN_SNOWFLAKE_DIGITS: int = 15

_LIMITATION_NOTE: str = (
    "Note: purge removes whole channel directories, never individual records. A\n"
    "capture record stores the author's display name but no user id and no\n"
    "channel id, so allowed_user_ids cannot be re-evaluated from disk: an\n"
    "admitted directory may still hold messages from users who were never\n"
    "allowlisted. The authors column is how you spot them; removing them means\n"
    "removing the whole directory."
)


class CaptureVerdict(StrEnum):
    """What the *current* capture gate would do with a directory's records."""

    ADMITTED = "admitted"
    """The gate would write these records today — never purged by default."""

    REFUSED = "refused"
    """The gate would refuse these records today — purged by default."""

    UNRESOLVED = "unresolved"
    """The directory is name-labelled, so no gate verdict is derivable offline."""


@dataclass(frozen=True, slots=True)
class ChannelAudit:
    """What one channel directory under the capture root contains, and its verdict.

    Attributes:
        label: The directory name — a channel name, or its numeric id when the
            name was unusable at capture time.
        verdict: The current gate's verdict for this directory.
        reason: One sentence explaining *verdict*, safe to show an operator.
        channel_id: The channel id when *label* is numeric, else ``None``.
        declared_tier: The ``channel_privacy_tiers`` ceiling in force for
            *channel_id* (including the ``personal`` default), or ``None`` when
            the label could not be resolved to an id.
        record_count: Parseable JSON records found in the directory.
        first_timestamp: Earliest record ``timestamp``, or ``None`` if none had
            one. Compared as **strings**, not parsed — see the note below.
        last_timestamp: Latest record ``timestamp``, same caveat.
        authors: Distinct ``author.name`` values, sorted.
    """

    label: str
    verdict: CaptureVerdict
    reason: str
    channel_id: int | None
    declared_tier: str | None
    record_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    authors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PurgePlan:
    """The directories a purge would remove, and the requests it turned down.

    Attributes:
        targets: Channel directories to delete. Every :attr:`CaptureVerdict.REFUSED`
            audit, plus any :attr:`CaptureVerdict.UNRESOLVED` directory the
            operator named. Never contains an admitted directory.
        declined: ``(label, reason)`` pairs for requested labels that were not
            promoted to targets.
    """

    targets: tuple[ChannelAudit, ...] = ()
    declined: tuple[tuple[str, str], ...] = field(default=())


def _resolve_channel_id(label: str) -> int | None:
    """The channel id *label* encodes, or ``None`` if it is a channel name.

    Being all-digits is **not** enough. Discord channel names may be nothing but
    digits — ``2024``, ``420``, ``911`` are ordinary channel names — and
    :func:`crawdad.capture._channel_label` labels a directory with the channel's
    *name* whenever it is usable. Reading ``2024`` as channel id 2024 would miss
    the allowlist and mark a possibly-allowlisted channel ``REFUSED``, which
    :func:`plan_purge` then deletes with no operator confirmation at all. That is
    the one direction this tool must never fail in.

    So a label is only read as an id when it could plausibly *be* a snowflake:
    ASCII digits, no leading zero, and at least :data:`_MIN_SNOWFLAKE_DIGITS`
    long. Discord ids are ``(ms since the 2015 epoch) << 22``, so anything
    created since 2016 is 18-19 digits and even early-2015 ids are 15-16.

    The floor errs safe in both directions. A short numeric *name* becomes
    ``UNRESOLVED`` — reported, never auto-purged, still removable with
    ``--channel``. A genuine id-labelled directory from very early 2015 would
    also fall through to ``UNRESOLVED``, which costs the operator one
    ``--channel`` flag rather than costing them data.
    """
    if not (label.isascii() and label.isdigit()):
        return None
    if label.startswith("0") or len(label) < _MIN_SNOWFLAKE_DIGITS:
        return None
    return int(label)


def _verdict_for(
    channel_id: int | None, tier: str | None, *, config: CrawDadConfig
) -> tuple[CaptureVerdict, str]:
    """Apply the current capture gate to a resolved channel, returning why.

    Mirrors :func:`crawdad.bot._capture_allowed`'s channel-level conditions in
    the same order — allowlist first, then tier membership in
    :data:`~crawdad.config.CAPTURE_ADMITTED_TIERS`. The user-level condition is
    deliberately absent; see the module docstring.
    """
    if channel_id is None:
        return (
            CaptureVerdict.UNRESOLVED,
            "directory is named after the channel, not its id, so it cannot be "
            "matched against allowed_channel_ids or channel_privacy_tiers",
        )
    if channel_id not in config.allowed_channel_ids:
        return (
            CaptureVerdict.REFUSED,
            f"channel {channel_id} is not in allowed_channel_ids",
        )
    if tier not in CAPTURE_ADMITTED_TIERS:
        return (
            CaptureVerdict.REFUSED,
            f"channel {channel_id} is declared {tier!r}, which capture refuses "
            "because a capture record cannot carry that ceiling",
        )
    return (
        CaptureVerdict.ADMITTED,
        f"channel {channel_id} is allowlisted at tier {tier!r}",
    )


def _record_timestamp(record: dict[str, object]) -> str | None:
    """The record's ``timestamp`` when it is a string, else ``None``.

    Kept as a string and ordered lexicographically rather than parsed. That is
    exact **only** because of two properties of what the writer emits
    (``created_at.isoformat()`` on Discord's always-UTC, always-aware
    timestamps): every string carries the same fixed-width date-time prefix and
    the same ``+00:00`` offset, and where ``isoformat`` omits a zero microsecond
    field, the ``+`` of the offset (ASCII 43) sorts before the ``.`` of a
    microsecond field (46), so ``…:00+00:00`` still orders before
    ``…:00.5+00:00``.

    Both properties are assumptions about the writer, not guarantees of ISO-8601
    in general — mixed offsets or a ``Z`` suffix would break the ordering
    silently. If ``crawdad.capture._record_for`` ever changes its timestamp
    format, this must switch to parsed comparison. Ordering only affects the
    reported date range, never a purge decision.
    """
    raw = record.get("timestamp")
    return raw if isinstance(raw, str) else None


def _record_author(record: dict[str, object]) -> str | None:
    """The record's ``author.name`` when present and non-empty, else ``None``."""
    author = record.get("author")
    if not isinstance(author, dict):
        return None
    name = author.get("name")
    return name if isinstance(name, str) and name else None


@dataclass(frozen=True, slots=True)
class _Summary:
    """Aggregate statistics for one channel directory's records."""

    record_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    authors: tuple[str, ...]


def _summarise(channel_dir: Path) -> _Summary:
    """Count records and collect the timestamp range + distinct authors."""
    count = 0
    timestamps: list[str] = []
    authors: set[str] = set()
    for record in iter_channel_records(channel_dir):
        count += 1
        stamp = _record_timestamp(record)
        if stamp is not None:
            timestamps.append(stamp)
        author = _record_author(record)
        if author is not None:
            authors.add(author)
    return _Summary(
        record_count=count,
        first_timestamp=min(timestamps, default=None),
        last_timestamp=max(timestamps, default=None),
        authors=tuple(sorted(authors)),
    )


def _audit_channel_dir(channel_dir: Path, config: CrawDadConfig) -> ChannelAudit:
    """Audit a single channel directory against the current gate."""
    label = channel_dir.name
    channel_id = _resolve_channel_id(label)
    tier = (
        None
        if channel_id is None
        else config.attachments.channel_privacy_tiers.get(
            channel_id, DEFAULT_CHANNEL_TIER
        )
    )
    verdict, reason = _verdict_for(channel_id, tier, config=config)
    summary = _summarise(channel_dir)
    return ChannelAudit(
        label=label,
        verdict=verdict,
        reason=reason,
        channel_id=channel_id,
        declared_tier=tier,
        record_count=summary.record_count,
        first_timestamp=summary.first_timestamp,
        last_timestamp=summary.last_timestamp,
        authors=summary.authors,
    )


def audit_capture_tree(
    *, capture_dir: Path, config: CrawDadConfig
) -> tuple[ChannelAudit, ...]:
    """Audit every channel directory under *capture_dir*. Read-only.

    Symlinked entries are skipped entirely: the capture writer never creates
    one, and auditing a symlink would invite a later purge to follow it out of
    the vault. Loose files at the root are skipped too.

    Args:
        capture_dir: The ``<vault>/<capture_subpath>`` root.
        config: The live bot config supplying the allowlist and tier table.

    Returns:
        One :class:`ChannelAudit` per channel directory, sorted by label. Empty
        when the capture root does not exist.
    """
    if not capture_dir.is_dir():
        return ()
    return tuple(
        _audit_channel_dir(child, config)
        for child in sorted(capture_dir.iterdir())
        if child.is_dir() and not child.is_symlink()
    )


def list_skipped_entries(capture_dir: Path) -> tuple[str, ...]:
    """Names of entries under *capture_dir* that :func:`audit_capture_tree` ignores.

    Loose files and symlinks. Reported alongside the audit so the tool never
    silently under-states what is on disk: the audit claims to be sufficient on
    its own to decide a purge, and an entry it dropped without saying so would
    make that claim false.

    Returns:
        The sorted entry names, or empty when the capture root is absent.
    """
    if not capture_dir.is_dir():
        return ()
    return tuple(
        sorted(
            child.name
            for child in capture_dir.iterdir()
            if child.is_symlink() or not child.is_dir()
        )
    )


def plan_purge(
    *,
    audits: Sequence[ChannelAudit],
    requested_labels: Sequence[str] = (),
) -> PurgePlan:
    """Decide what a purge would delete, without touching disk.

    Every refused directory is targeted automatically — the gate's verdict on it
    is unambiguous. An unresolved directory is targeted only when the operator
    names it, because its records may be entirely legitimate. An admitted
    directory is never targeted, named or not.

    Args:
        audits: The audit of the tree, from :func:`audit_capture_tree`.
        requested_labels: Directory labels the operator asked for explicitly.

    Returns:
        The plan. Targets are unique and ordered by label.
    """
    by_label = {audit.label: audit for audit in audits}
    targets = {a.label: a for a in audits if a.verdict is CaptureVerdict.REFUSED}
    declined: list[tuple[str, str]] = []
    for label in requested_labels:
        audit = by_label.get(label)
        if audit is None:
            declined.append((label, DECLINE_UNKNOWN))
        elif audit.verdict is CaptureVerdict.ADMITTED:
            declined.append((label, DECLINE_ADMITTED))
        else:
            targets[label] = audit
    return PurgePlan(
        targets=tuple(targets[label] for label in sorted(targets)),
        declined=tuple(declined),
    )


def apply_purge(*, capture_dir: Path, plan: PurgePlan) -> tuple[str, ...]:
    """Delete the plan's target directories. Irreversible.

    Every target is confined to the capture root *before* the first deletion, so
    a plan that somehow names a path outside it removes nothing at all rather
    than part of the tree. Targets always come from :func:`audit_capture_tree`'s
    own ``iterdir`` walk in normal use; this is defence in depth against a
    future caller constructing a plan by hand.

    Args:
        capture_dir: The capture root every target must live under.
        plan: The plan from :func:`plan_purge`.

    Returns:
        The labels deleted, in plan order.

    Raises:
        ValueError: if any target resolves outside *capture_dir*. Nothing is
            deleted in that case.
    """
    root = capture_dir.resolve()
    paths: list[tuple[str, Path]] = []
    for target in plan.targets:
        path = (capture_dir / target.label).resolve()
        if path == root or not path.is_relative_to(root):
            msg = (
                f"refusing to purge {target.label!r}: it resolves outside the "
                f"capture root {capture_dir}"
            )
            raise ValueError(msg)
        paths.append((target.label, path))
    deleted: list[str] = []
    for label, path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            deleted.append(label)
    return tuple(deleted)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """Render ``<count> <noun>``, picking the singular only when *count* is 1."""
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def _format_audit_line(audit: ChannelAudit) -> str:
    """Render one channel directory as four lines: header, span, authors, reason."""
    span = (
        f"{audit.first_timestamp} .. {audit.last_timestamp}"
        if audit.first_timestamp and audit.last_timestamp
        else "no timestamps"
    )
    authors = ", ".join(audit.authors) if audit.authors else "none"
    return (
        f"  [{audit.verdict.value:<10}] {audit.label}\n"
        f"      {_plural(audit.record_count, 'record')}  {span}\n"
        f"      authors: {authors}\n"
        f"      {audit.reason}"
    )


def _format_skipped(skipped: Sequence[str]) -> str:
    """Render the skipped-entries disclosure, or empty when nothing was skipped."""
    if not skipped:
        return ""
    names = ", ".join(skipped)
    return (
        f"\n\nSkipped {_plural(len(skipped), 'entry', 'entries')} that is not a "
        f"real channel directory (a loose file or a symlink — the capture writer "
        f"creates neither, and purge never follows a symlink): {names}"
    )


def format_audit_report(
    *,
    capture_dir: Path,
    audits: Sequence[ChannelAudit],
    skipped: Sequence[str] = (),
) -> str:
    """Render the audit as operator-readable text.

    The output is designed to be sufficient on its own to decide a purge — every
    verdict carries its reason, the whole-directory limitation is stated rather
    than assumed, and anything on disk the walk ignored is disclosed.

    Args:
        capture_dir: The capture root that was audited.
        audits: The per-directory audits from :func:`audit_capture_tree`.
        skipped: Entry names from :func:`list_skipped_entries`.
    """
    header = f"Capture audit — {capture_dir}"
    if not audits:
        return (
            f"{header}\n\nno channel directories found; nothing to audit."
            f"{_format_skipped(skipped)}"
        )
    total = sum(audit.record_count for audit in audits)
    body = "\n".join(_format_audit_line(audit) for audit in audits)
    counts = ", ".join(
        f"{sum(1 for a in audits if a.verdict is verdict)} {verdict.value}"
        for verdict in CaptureVerdict
    )
    return (
        f"{header}\n\n"
        f"{_plural(len(audits), 'channel directory', 'channel directories')}, "
        f"{_plural(total, 'record')} ({counts}).\n\n"
        f"{body}\n\n"
        f"{_LIMITATION_NOTE}"
        f"{_format_skipped(skipped)}"
    )


def _format_purge_targets(plan: PurgePlan, *, applied: bool) -> str:
    """Render the target block for the purge report."""
    verb = "Deleted" if applied else "Would delete"
    total = sum(target.record_count for target in plan.targets)
    lines = "\n".join(_format_audit_line(target) for target in plan.targets)
    count = _plural(len(plan.targets), "channel directory", "channel directories")
    return f"{verb} {count} ({_plural(total, 'record')}):\n{lines}"


def format_purge_report(*, capture_dir: Path, plan: PurgePlan, applied: bool) -> str:
    """Render what a purge did, or would do, as operator-readable text.

    Args:
        capture_dir: The capture root the plan applies to.
        plan: The plan from :func:`plan_purge`.
        applied: ``True`` after :func:`apply_purge` ran; ``False`` for a dry run,
            which says so unmissably and points at ``--apply``.
    """
    mode = (
        "APPLIED (this is irreversible)"
        if applied
        else "DRY RUN (nothing was deleted; re-run with --apply to delete)"
    )
    parts = [f"Capture purge — {mode}", f"Capture root: {capture_dir}", ""]
    if plan.targets:
        parts.append(_format_purge_targets(plan, applied=applied))
    else:
        parts.append("nothing to purge: no channel directory the gate would refuse.")
    if plan.declined:
        declined = "\n".join(f"  {label} — {reason}" for label, reason in plan.declined)
        count = _plural(len(plan.declined), "request")
        parts.append(f"\nDeclined {count}:\n{declined}")
    parts.append(f"\n{_LIMITATION_NOTE}")
    return "\n".join(parts)
