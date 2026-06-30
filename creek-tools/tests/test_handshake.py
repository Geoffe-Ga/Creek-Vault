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


def _vault(tmp_path: Path, *, creek: bool) -> Path:
    """Create a vault dir, optionally with the ``00-Creek-Meta`` marker."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if creek:
        (vault / "00-Creek-Meta").mkdir()
    return vault


def test_handshake_reports_the_required_fields(tmp_path: Path) -> None:
    """The response carries the contract-mandated keys and pinned versions."""
    vault = _vault(tmp_path, creek=True)
    result = handshake_tool(vault_path=vault, capabilities=_CAPS, consumer="adepthood")
    for key in (
        "available",
        "contract_version",
        "ontology_version",
        "tiers",
        "capabilities",
    ):
        assert key in result, key
    assert result["tool"] == TOOL_NAME
    assert result["contract_version"] == CONTRACT_VERSION
    assert result["ontology_version"] == ONTOLOGY_VERSION
    assert result["tiers"] == ["open", "personal", "intimate"]


def test_capabilities_echo_the_registered_tool_list(tmp_path: Path) -> None:
    """``capabilities`` reflects the tool names handed in (the live registry)."""
    vault = _vault(tmp_path, creek=True)
    result = handshake_tool(vault_path=vault, capabilities=_CAPS)
    assert result["capabilities"] == _CAPS


def test_available_true_when_creek_vault_present(tmp_path: Path) -> None:
    """A vault with the ``00-Creek-Meta`` marker reports ``available=True``."""
    vault = _vault(tmp_path, creek=True)
    assert handshake_tool(vault_path=vault, capabilities=_CAPS)["available"] is True


def test_available_false_when_no_creek_structure(tmp_path: Path) -> None:
    """A bare directory with no Creek marker reports ``available=False``."""
    vault = _vault(tmp_path, creek=False)
    assert handshake_tool(vault_path=vault, capabilities=_CAPS)["available"] is False


def test_handshake_is_audit_logged_with_consumer(tmp_path: Path) -> None:
    """The call appends one audit entry tagged with the tool and consumer."""
    vault = _vault(tmp_path, creek=True)
    handshake_tool(vault_path=vault, capabilities=_CAPS, consumer="adepthood")
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    entries = [
        json.loads(line) for line in log.read_text().splitlines() if line.strip()
    ]
    assert entries[-1]["tool"] == TOOL_NAME
    assert entries[-1]["consumer"] == "adepthood"


def test_handshake_honours_the_tier_ceiling_in_its_audit(tmp_path: Path) -> None:
    """The supplied ceiling is recorded and echoed, not silently dropped."""
    vault = _vault(tmp_path, creek=True)
    result = handshake_tool(
        vault_path=vault,
        capabilities=_CAPS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["tier_ceiling"] == "personal"


def test_handshake_is_read_only(tmp_path: Path) -> None:
    """Only the audit log is created; no vault content is written."""
    vault = _vault(tmp_path, creek=True)
    before = {p for p in vault.rglob("*") if p.is_file()}
    handshake_tool(vault_path=vault, capabilities=_CAPS)
    created = {p for p in vault.rglob("*") if p.is_file()} - before
    assert created, "expected the audit log to be written"
    assert all("audit" in str(p) for p in created)
