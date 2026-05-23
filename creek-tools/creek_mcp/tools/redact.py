"""``creek.redact.scan`` MCP tool — read-only safety pass over a staging dir.

The scan tool walks *input_path* (relative to the vault root), invokes
:class:`creek.redact.scanner.RedactionScanner` from the loaded
:class:`creek.config.CreekConfig`, and returns a structured report plus
a markdown summary suitable for embedding in a Discord reply.

The tool is **read-only**: it never writes to the scanned files and
never invokes the redactor. ``--apply`` lives behind the CLI for a
deliberate human-driven step. The default tier is :data:`TierCeiling.OPEN`
because the scan only reports counts + line numbers + salted hashes —
no matched text and no file body content leaves the tool.

FEAT-027 introduces this tool so CrawDad can run the safety pass on
Discord attachments staged under ``00-Creek-Meta/Inbound/`` before any
``creek.ingest`` call is dispatched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek.config import load_config
from creek.redact.scanner import RedactionMatch, RedactionScanner, ScanSummary
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

TOOL_NAME = "creek.redact.scan"


def redact_scan_tool(
    *,
    vault_path: Path,
    input_path: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Scan *input_path* for sensitive data and return a structured report.

    Args:
        vault_path: Vault root used for path validation and audit logging.
        input_path: Directory to scan; may be absolute or relative to the
            vault root. The path must resolve *inside* the vault to
            prevent the MCP surface from scanning arbitrary disk content.
        privacy_tier_ceiling: Caller's tier ceiling. Recorded in the
            audit entry; the scan never returns matched text so the
            ceiling does not gate the body.
        consumer: Identifier of the calling client (recorded in audit).

    Returns:
        A dict with ``status`` (``ok`` / ``empty`` / ``refused``), the
        scan statistics, a list of findings (hash + location + severity
        — never the matched text), and a human-readable markdown summary.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"input_path": input_path},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )

    resolved = _resolve_within_vault(vault_path, input_path)
    if resolved is None:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"input_path {input_path!r} resolves outside the vault root; "
                "the scan tool only operates on vault-relative paths."
            ),
        )

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

    status = "empty" if not summary.matches else "ok"
    return {
        "status": status,
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "input_path": str(resolved.relative_to(vault_path)),
        "statistics": {
            "files_scanned": summary.files_scanned,
            "files_skipped_binary": summary.files_skipped_binary,
            "files_skipped_extension": summary.files_skipped_extension,
            "total_findings": len(summary.matches),
        },
        "findings": [_finding_to_dict(m, vault_path) for m in summary.matches],
        "report_markdown": scanner.generate_markdown_summary(summary),
    }


def _resolve_within_vault(vault_path: Path, input_path: str) -> Path | None:
    """Return *input_path* resolved inside *vault_path*, or ``None`` if outside.

    Accepts either an absolute path inside the vault or a vault-relative
    path. Uses ``Path.resolve(strict=False)`` so non-existent staging
    directories still validate (existence is checked separately so the
    caller gets a clean ``input_path not found`` message instead of a
    silent ``None`` collapse).
    """
    vault_resolved = vault_path.resolve()
    candidate = Path(input_path)
    if not candidate.is_absolute():
        candidate = vault_resolved / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(vault_resolved)
    except ValueError:
        return None
    return resolved


def _finding_to_dict(match: RedactionMatch, vault_path: Path) -> dict[str, Any]:
    """Convert a :class:`RedactionMatch` into a JSON-friendly dict.

    The ``file_path`` is rendered relative to the vault root so the
    Discord reply does not leak absolute filesystem paths.
    """
    from creek.redact.patterns import PATTERN_METADATA

    info = PATTERN_METADATA.get(match.match_type)
    severity = info.severity if info else "unknown"
    try:
        rel = match.file_path.resolve().relative_to(vault_path.resolve())
        path_str = str(rel)
    except ValueError:
        path_str = str(match.file_path)
    return {
        "file_path": path_str,
        "line_number": match.line_number,
        "match_type": match.match_type,
        "severity": severity,
        "salted_hash": match.salted_hash,
    }
