"""``creek.handshake`` MCP tool — capability/version negotiation (#750).

A connecting client (Adepthood) calls ``creek.handshake`` first to learn, in one
round-trip: whether a Creek vault is present, the contract + ontology versions
the server speaks, the privacy-tier model, and the list of registered tools. The
call must be read-only, audit-logged, and work with no LLM provider configured.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.handshake import TOOL_NAME, handshake_tool

if TYPE_CHECKING:
    from pathlib import Path

_CAPS = ["creek.classify", "creek.handshake", "creek.ingest"]
_SERVER = "creek-tools-mcp"


def _vault(tmp_path: Path, *, creek: bool) -> Path:
    """Create a vault dir, optionally with the ``00-Creek-Meta`` marker."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if creek:
        (vault / "00-Creek-Meta").mkdir()
    return vault


def _call(vault: Path, **overrides: object) -> dict[str, object]:
    """Invoke ``handshake_tool`` with the standard test defaults.

    ``capabilities`` and ``server_name`` are injected by ``build_server`` in
    production; the helper supplies stand-ins so each test overrides only the
    field it exercises.
    """
    kwargs: dict[str, object] = {
        "vault_path": vault,
        "capabilities": _CAPS,
        "server_name": _SERVER,
    }
    kwargs.update(overrides)
    return handshake_tool(**kwargs)  # type: ignore[arg-type]


def _audit_entries(vault: Path) -> list[dict[str, object]]:
    """Return the parsed MCP audit-log entries under *vault*."""
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_handshake_reports_the_required_fields(tmp_path: Path) -> None:
    """The response carries the contract-mandated keys and pinned versions."""
    result = _call(_vault(tmp_path, creek=True), consumer="adepthood")
    for key in (
        "available",
        "contract_version",
        "ontology_version",
        "tiers",
        "capabilities",
    ):
        assert key in result, key
    assert result["tool"] == TOOL_NAME
    assert result["server"] == _SERVER
    assert result["contract_version"] == CONTRACT_VERSION
    assert result["ontology_version"] == ONTOLOGY_VERSION
    assert result["tiers"] == ["open", "personal", "intimate"]


def test_capabilities_echo_the_registered_tool_list(tmp_path: Path) -> None:
    """``capabilities`` reflects the tool names handed in (the live registry)."""
    assert _call(_vault(tmp_path, creek=True))["capabilities"] == _CAPS


def test_available_true_when_creek_vault_present(tmp_path: Path) -> None:
    """A vault with the ``00-Creek-Meta`` marker reports ``available=True``."""
    assert _call(_vault(tmp_path, creek=True))["available"] is True


def test_available_false_when_no_creek_structure(tmp_path: Path) -> None:
    """A bare directory with no Creek marker reports ``available=False``."""
    assert _call(_vault(tmp_path, creek=False))["available"] is False


def test_handshake_is_audit_logged_with_consumer(tmp_path: Path) -> None:
    """The call appends one audit entry tagged with the tool and consumer."""
    vault = _vault(tmp_path, creek=True)
    _call(vault, consumer="adepthood")
    last = _audit_entries(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["consumer"] == "adepthood"


def test_handshake_records_the_ceiling_in_its_audit_and_echoes_it(
    tmp_path: Path,
) -> None:
    """The supplied ceiling is both echoed in the response and written to the log."""
    vault = _vault(tmp_path, creek=True)
    result = _call(vault, privacy_tier_ceiling=TierCeiling.PERSONAL)
    assert result["tier_ceiling"] == "personal"
    assert _audit_entries(vault)[-1]["tier_ceiling"] == "personal"


def test_handshake_is_read_only(tmp_path: Path) -> None:
    """Only the audit log is created; no vault content is written."""
    vault = _vault(tmp_path, creek=True)
    before = {p for p in vault.rglob("*") if p.is_file()}
    _call(vault)
    created = {p for p in vault.rglob("*") if p.is_file()} - before
    assert created, "expected the audit log to be written"
    assert all("audit" in str(p) for p in created)
