"""``creek.redact.scan`` MCP tool — read-only safety pass over a staging dir.

The scan tool walks *input_path* (relative to the vault root), invokes
:class:`creek.redact.scanner.RedactionScanner` from the loaded
:class:`creek.config.CreekConfig`, and returns a structured report plus
a markdown summary suitable for embedding in a Discord reply.

The tool is **read-only**: it never writes to the scanned files and
never invokes the redactor. ``--apply`` lives behind the CLI for a
deliberate human-driven step, and no matched text ever leaves the tool —
findings carry counts, line numbers and salted hashes only.

FEAT-027 introduces this tool so CrawDad can run the safety pass on
Discord attachments staged under ``00-Creek-Meta/Inbound/`` before any
``creek.ingest`` call is dispatched.

**Scope, not a per-file tier filter (#972).** The scan is a regex pass over
bytes: it opens no front matter and reads no ``privacy_tier`` from anything
it walks, so it has nothing to rank a fragment *with*. The gate
(:func:`_refuse_outside_scan_scope`) therefore decides *where* the tool may
look rather than *what* it may report, in two parts. The staging subtree
above is admitted at **every** ceiling — it is the reason the tool exists,
and CrawDad's safety pass runs there at the channel's configured ceiling
(``crawdad/crawdad/bot.py::_channel_tier``): ``personal`` by default and
``open`` only for a channel an operator explicitly mapped to it, so
admission has to hold at the lowest of them. Every other in-vault target is
ranked as if it held ``intimate`` content, because for all this tool knows
it does, so only ``ceiling=intimate`` or ``all`` admits it. That is coarser
than a tier filter on purpose: ``resolve_within_vault`` confines
*input_path* to the whole vault, which is true and insufficient, since the
vault is where the fragments are and a finding names the file it came from
— and Creek fragment filenames are slugified titles. Reproduced before the
gate: a fragment at ``privacy_tier: intimate`` surfaced as
``01-Fragments/Notes/my-affair-with-dana.md`` at ``ceiling=open``.

The gate sits **above** the existence check, and that ordering is
load-bearing. Below it, ``"input_path not found: …"`` versus a successful
scan is a one-bit existence oracle over every slugified title in the vault:
a caller who cannot read ``01-Fragments`` could still ask, one filename at
a time, what is in it. The refusal is derived only from the caller's own
*input_path* and their own declared ceiling, so it carries the canonical
four keys and nothing else — a ``statistics`` block reporting
``files_scanned: 0`` would put the same differential back in a tidier
wrapper.

One refusal necessarily sits *above* the gate rather than below it:
``resolve_within_vault`` answers ``None`` for a path that lands off the
vault, and there is no resolved path left for the gate to measure. Moving
the gate up instead would measure an uncollapsed ``../`` traversal against
the staging subtree *by name*, which is the one thing resolution is there
to prevent — so the arm stays where it is and its *reason* varies instead.
:func:`_outside_vault_reason` is that decision, and the oracle it closes.

**Every path is rendered as scanned**, bar one documented echo.
:func:`_vault_relative` is the single owner of ``findings[].file_path``, of
the ``### `…``` headings inside ``report_markdown`` (the field CrawDad posts
verbatim into a Discord channel), and of the response's own ``input_path``
echo, so a reviewer who checks one has checked all three. It never resolves
the path it renders, which is the second and independent half of the
disclosure:
:meth:`~creek.redact.scanner.RedactionScanner.scan_batch` reaches its
children through ``rglob``, which yields symlinks unresolved, so a resolving
renderer reported a link staged at ``00-Creek-Meta/Inbound/ch1/sneaky.md``
under its *target's* name — an intimate fragment's slugified title, at
``ceiling=open``, out of a scan the scope gate correctly admitted. The echo
is the exception, and it is an exception only in wording: it renders
``resolved``, which :func:`~creek_mcp.path_confinement.resolve_within_vault`
has already followed, so it is as-*resolved*. Nothing leaks through it
because the only target ever echoed is one the scope gate admitted — a
symlink out of the staging subtree is refused above the echo, never renamed
by it.

Residual, tracked by **#1087**: ``scan_batch`` still *follows* symlinked
children, so such a target's PII types and line numbers are still reported
even though its path no longer is — and it counts toward ``files_scanned``
whether or not it yields a finding, so the residual carries an
existence-and-readability bit about the target as well as finding detail.
It is bounded to symlinked **files**: ``rglob`` does not descend into
symlinked directories and ``scan_batch`` keeps only ``is_file()`` children,
so a link to ``01-Fragments/`` staged as a directory yields nothing at all.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from creek.config import load_config
from creek.models import PrivacyTier
from creek.redact.scanner import RedactionMatch, RedactionScanner, ScanSummary
from creek_mcp.audit import MCPAuditLog
from creek_mcp.path_confinement import resolve_within_vault
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, tier_allowed

TOOL_NAME = "creek.redact.scan"

_STAGING_SUBDIR = Path("00-Creek-Meta/Inbound")
"""The FEAT-027 staging subtree — the one scope admitted at every ceiling."""

_OUT_OF_SCOPE_REASON = (
    "creek.redact.scan is scoped to the 00-Creek-Meta/Inbound/ staging "
    "subtree, which every ceiling admits; the scan reads no per-file privacy "
    "tier, so any other vault path is ranked as intimate content and needs a "
    "ceiling of intimate or all."
)
"""The one refusal reason for an out-of-scope target.

*One*, deliberately: :func:`_outside_vault_reason` routes a path that
resolves off the vault here too whenever the caller is not admitted
vault-wide, since such a path is outside the staging subtree as surely as an
ordinary fragment is.

Fixed, in the same spirit as
:data:`creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON` and for the
opposite reason: that one names no tier because the tier was derived from
content the caller cannot read, while this one names the rule outright
because nothing here was derived from anything — the rule is a property of
the *caller's own* path and ceiling, and it is the single fact a CrawDad
operator can act on. Nothing is interpolated into it, so the refusals for a
target that exists, one that does not, and one whose symlink leads off the
vault entirely are byte-identical.
"""

_OUTSIDE_VAULT_PLACEHOLDER = "<path outside vault>"
"""Rendering for a path :func:`_vault_relative` cannot express vault-relative.

Rendering is the last thing that happens to a path, so its fallback arm is
where a "cannot happen" case would become the disclosure the scope gate
exists to prevent — the arm it replaces returned the absolute path, in full,
precisely when something unexpected had happened.

The value has to survive a ``str(Path(...))`` round trip unchanged, because
:func:`_relativised_summary` carries it as a :class:`~pathlib.Path` into
:meth:`~creek.redact.scanner.RedactionScanner.generate_markdown_summary`:
the empty string would come back as ``"."`` and a leading slash would come
back looking absolute.
"""


def redact_scan_tool(
    *,
    vault_path: Path,
    input_path: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Scan *input_path* for sensitive data and return a structured report.

    Args:
        vault_path: Vault root used for path validation, scope resolution,
            path rendering and audit logging.
        input_path: Directory or file to scan; may be absolute or relative
            to the vault root. The path must resolve *inside* the vault and
            *within the scan's scope* — see :func:`_refuse_outside_scan_scope`
            for what the ceiling decides and what it cannot, and
            :func:`_outside_vault_reason` for why failing the first of those
            two looks, to an un-admitted caller, exactly like failing the
            second.
        privacy_tier_ceiling: Caller's tier ceiling. Recorded in the audit
            entry, and the admission half of the scope gate.
        consumer: Identifier of the calling client (recorded in audit).

    Returns:
        A dict with ``status`` (``ok`` / ``empty`` / ``refused``), the scan
        statistics, a list of findings (hash + location + severity — never
        the matched text), and a human-readable markdown summary. Every path
        in it is vault-relative and rendered as scanned, except the
        ``input_path`` echo, which renders the already-resolved target — see
        the module docstring for why that is safe here and not in general.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"input_path": input_path},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )

    resolved = resolve_within_vault(vault_path, input_path)
    if resolved is None:
        # Stays above the scope gate, which has no resolved path to measure
        # here; the reason varies instead. See _outside_vault_reason.
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=_outside_vault_reason(input_path, privacy_tier_ceiling),
        )

    # Above the existence check on purpose: the two answers below it differ
    # per target, and that difference is an existence oracle over slugified
    # fragment titles. See the module docstring.
    out_of_scope = _refuse_outside_scan_scope(
        resolved=resolved,
        vault_path=vault_path,
        ceiling=privacy_tier_ceiling,
    )
    if out_of_scope is not None:
        return out_of_scope

    if not resolved.exists():
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"input_path not found: {input_path}",
        )

    config = load_config()
    scanner = RedactionScanner(config.redaction)

    if resolved.is_file():
        matches = scanner.scan_file(resolved)
        summary = ScanSummary(matches=matches, files_scanned=1)
    else:
        summary = scanner.scan_batch(resolved)

    scanned = _relativised_summary(summary, vault_path)
    status = "empty" if not scanned.matches else "ok"
    return {
        "status": status,
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "input_path": _vault_relative(resolved, vault_path),
        "statistics": {
            "files_scanned": scanned.files_scanned,
            "files_skipped_binary": scanned.files_skipped_binary,
            "files_skipped_extension": scanned.files_skipped_extension,
            "total_findings": len(scanned.matches),
        },
        "findings": [_finding_to_dict(match) for match in scanned.matches],
        "report_markdown": scanner.generate_markdown_summary(scanned),
    }


def _admitted_vault_wide(ceiling: TierCeiling) -> bool:
    """Return ``True`` when *ceiling* admits every in-vault target.

    The one admission question this tool asks, written once because two
    refusal arms need the same answer and a second copy is how they drift
    apart: the scope gate below, and the off-vault arm above it that must not
    be more precise than the gate would have been. The scan reads no
    per-file tier, so everything outside the staging subtree is ranked as if
    it held ``intimate`` content — which makes "admitted to that rank" and
    "admitted to the whole vault" the same predicate rather than two.

    Args:
        ceiling: The caller's declared ceiling.

    Returns:
        ``True`` for ``intimate`` and ``all``, decided by
        :func:`~creek_mcp.tier_ceiling.tier_allowed` so this module grows no
        private copy of the ranking the rest of the surface uses.
    """
    return tier_allowed(PrivacyTier.INTIMATE, ceiling)


def _outside_vault_reason(input_path: str, ceiling: TierCeiling) -> str:
    """Return the refusal reason for an *input_path* that resolves off the vault.

    Two reasons, because the precise one is an oracle for a caller who is not
    admitted vault-wide. Escaping the vault is reachable from *inside* it: a
    symlink parked at ``01-Fragments/Notes/x.md`` pointing at another disk
    resolves out and takes this arm, while the ordinary file beside it and
    the name of a file that does not exist at all both take the scope gate's.
    A distinguishable message here therefore answers "is the path you named
    an outward link?" about a subtree the gate refuses to say anything about
    — one guessed slugified title at a time, which is the same probe the gate
    was placed above the existence check to stop. Collapsing onto
    :data:`_OUT_OF_SCOPE_REASON` gives up nothing true: whatever resolves off
    the vault is, by definition, outside the staging subtree.

    Withheld rather than deleted. At ``intimate`` or ``all`` the caller is
    already admitted to every in-vault target the precise message could
    distinguish this one from, so it discloses nothing there — and it is the
    only actionable fact in the refusal, because a link into a stale sync
    folder or a moved drive is *broken*, not out of scope, and "out of scope"
    would send an operator looking for a permissions problem they do not have.

    The price, worth stating because CrawDad pays it by default: its channels
    run at ``personal`` unless mapped, so a misconfigured staging root reaches
    a CrawDad operator as the out-of-scope rule rather than as "off the
    vault". The audit trail still records the ``input_path`` the call aimed
    at, and a local operator can reproduce the precise message at
    ``ceiling=intimate``.

    Args:
        input_path: The caller's own path string. Interpolated into the
            precise message only; the collapsed one is fixed text, so the
            refusals for an outward link, an ordinary out-of-scope file and
            a path that names nothing are byte-identical.
        ceiling: The caller's declared ceiling.

    Returns:
        The precise outside-the-vault message when *ceiling* admits the whole
        vault, and :data:`_OUT_OF_SCOPE_REASON` when it does not.
    """
    # Deliberately less precise than the arm below, on the argument above —
    # not an oversight to be tightened back up without reading it.
    if not _admitted_vault_wide(ceiling):
        return _OUT_OF_SCOPE_REASON
    return (
        f"input_path {input_path!r} resolves outside the vault root; "
        "the scan tool only operates on vault-relative paths."
    )


def _refuse_outside_scan_scope(
    *,
    resolved: Path,
    vault_path: Path,
    ceiling: TierCeiling,
) -> dict[str, Any] | None:
    """Return the out-of-scope refusal for *resolved*, or ``None`` to admit.

    Admission is decided by *where* the target is and never by what is in it,
    because the scan opens no front matter and so has no per-file tier to
    consult. The staging subtree is admitted unconditionally; every other
    in-vault target is admitted only to a caller
    :func:`_admitted_vault_wide` clears, which is where that ranking is
    written down — once, for this arm and for the off-vault arm above it.

    Args:
        resolved: *input_path* as returned by
            :func:`~creek_mcp.path_confinement.resolve_within_vault` — already
            confined to the vault and already symlink-resolved, which is what
            stops a link parked under ``Inbound/`` naming its way into scope.
            Matched with :meth:`~pathlib.Path.is_relative_to` rather than a
            string prefix, so ``Inbound-other/`` is not mistaken for a child
            and the subtree root itself still counts.
        vault_path: Vault root. Resolved here so a root reached through a
            symlinked component still matches its own staging subtree.
        ceiling: The caller's declared ceiling.

    Returns:
        ``None`` when the scan may proceed, so a call site reads as an early
        return on a non-``None`` result. Otherwise the canonical four-key
        :func:`~creek_mcp.tier_ceiling.refusal_response` carrying
        :data:`_OUT_OF_SCOPE_REASON` — and nothing else. Anything further
        would be derived from a subtree the caller was just refused.
    """
    if resolved.is_relative_to(vault_path.resolve() / _STAGING_SUBDIR):
        return None
    if _admitted_vault_wide(ceiling):
        return None
    return refusal_response(
        tool=TOOL_NAME,
        ceiling=ceiling,
        reason=_OUT_OF_SCOPE_REASON,
    )


def _vault_relative(path: Path, vault_path: Path) -> str:
    """Render *path* **as scanned**, relative to the vault root.

    *path* is deliberately not resolved. ``rglob`` yields symlinked children
    unresolved, so resolving here reports a link staged under ``Inbound/``
    beneath its target's name — the disclosure described in the module
    docstring, arriving through a scan the scope gate correctly admitted. The
    path a finding names must be the path the scanner opened.

    The vault *root* is resolved, because that is what every scanned path
    descends from: they are all built from
    :func:`~creek_mcp.path_confinement.resolve_within_vault`'s already-resolved
    output. Comparing them against an unresolved root is what raised an
    unhandled ``ValueError`` out of the MCP boundary for any vault whose root
    has a symlinked component — ``/tmp`` on macOS, or a synced folder reached
    through a link.

    Args:
        path: A path the scan produced, or the scan target itself. The
            latter arrives already resolved from
            :func:`~creek_mcp.path_confinement.resolve_within_vault`, so the
            response's ``input_path`` echo is strictly as-*resolved* rather
            than as-scanned — safe only because the scope gate refuses an
            out-of-scope symlink before there is anything to echo.
        vault_path: Vault root, as the caller supplied it.

    Returns:
        The vault-relative rendering, or :data:`_OUTSIDE_VAULT_PLACEHOLDER`
        when *path* cannot be expressed relative to the root at all — never
        the absolute path.
    """
    try:
        return str(path.relative_to(vault_path.resolve()))
    except ValueError:
        return _OUTSIDE_VAULT_PLACEHOLDER


def _relativised_summary(summary: ScanSummary, vault_path: Path) -> ScanSummary:
    """Return *summary* with every match's ``file_path`` rendered for the caller.

    One relativised summary feeds both renderings of the response —
    ``findings`` via :func:`_finding_to_dict` and ``report_markdown`` via
    :meth:`~creek.redact.scanner.RedactionScanner.generate_markdown_summary`
    — so the two cannot name the same file in two different ways, which is
    what they did before #972: one vault-relative, the other absolute.

    Never hand the result to
    :meth:`~creek.redact.scanner.RedactionScanner.generate_review_queue`: it
    calls ``extract_context(fm.file_path)`` and would reopen a vault-relative
    path against the process cwd. For the same reason the CLI
    (``creek/redact/cli_commands.py``) keeps rendering absolute paths, and
    that is deliberate rather than an oversight — a local operator needs real
    paths, and ``--apply`` reopens the files named in the summary it is given.

    Args:
        summary: The scan result, holding absolute on-disk paths.
        vault_path: Vault root the paths are rendered against.

    Returns:
        A new :class:`~creek.redact.scanner.ScanSummary` carrying the same
        matches and the same three file counters, with each ``file_path``
        replaced by its :func:`_vault_relative` rendering.
    """
    rendered = [
        match.model_copy(
            update={"file_path": Path(_vault_relative(match.file_path, vault_path))},
        )
        for match in summary.matches
    ]
    return replace(summary, matches=rendered)


def _finding_to_dict(match: RedactionMatch) -> dict[str, Any]:
    """Convert a :class:`RedactionMatch` into a JSON-friendly dict.

    ``file_path`` is emitted verbatim: *match* has already been through
    :func:`_relativised_summary`, which is what finally made this docstring's
    long-standing claim — that findings are rendered relative to the vault
    root, so the Discord reply leaks no absolute filesystem paths — true of
    ``report_markdown`` as well as of ``findings``.

    Args:
        match: One finding from the relativised summary.

    Returns:
        The finding's location, type, severity and salted hash. Never the
        matched text.
    """
    from creek.redact.patterns import PATTERN_METADATA

    info = PATTERN_METADATA.get(match.match_type)
    severity = info.severity if info else "unknown"
    return {
        "file_path": str(match.file_path),
        "line_number": match.line_number,
        "match_type": match.match_type,
        "severity": severity,
        "salted_hash": match.salted_hash,
    }
