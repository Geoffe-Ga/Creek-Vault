"""``creek.handshake`` MCP tool — capability/version negotiation (#750).

A connecting client (Adepthood) calls ``creek.handshake`` first to learn, in one
round-trip: whether a Creek vault is present, the contract + ontology versions
the server speaks, the privacy-tier model, and the list of registered tools. The
call must be read-only, audit-logged, and work with no LLM provider configured.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING

import pytest

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.policy import REMOTE_ADMITTED_CEILINGS, Transport
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import handshake as handshake_module
from creek_mcp.tools.handshake import CREEK_MARKER, TOOL_NAME, handshake_tool

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

    ``capabilities``, ``server_name`` and ``transport`` are injected by
    ``build_server`` in production; the helper supplies stand-ins so each test
    overrides only the field it exercises. The ``transport`` stand-in is
    ``stdio`` because that is what most of this file's cases are about; the
    transport cases below always name it explicitly on both sides.
    """
    kwargs: dict[str, object] = {
        "vault_path": vault,
        "capabilities": _CAPS,
        "server_name": _SERVER,
        "transport": Transport.STDIO,
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


def test_handshake_does_not_scaffold_the_marker_it_reports_missing(
    tmp_path: Path,
) -> None:
    """THE DOUBLE-CALL HONESTY TEST — two calls, both ``available=False`` (#1108).

    ``available`` is read off the presence of ``00-Creek-Meta``, and the audit
    append underneath reaches ``mkdir(parents=True, exist_ok=True)`` on
    ``00-Creek-Meta/audit/``. Ordering makes the *first* call honest — it
    computes ``available`` before appending — so a single call passes whether or
    not the bug is present. It takes two to see that the first call created the
    very marker the second one then finds.

    Args:
        tmp_path: Pytest's per-test directory, hosting a bare vault.
    """
    vault = _vault(tmp_path, creek=False)
    first = _call(vault)
    second = _call(vault)
    assert first["available"] is False
    assert second["available"] is False
    assert not (vault / CREEK_MARKER).exists()


def test_handshake_does_not_scaffold_a_vault_root_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """An entirely absent ``vault_path`` is left absent (#1108).

    The path an unconfigured ``creek-tools-mcp`` hands in falls back to the
    process's working directory, so the ``parents=True`` in the audit append
    would materialise a whole vault-shaped tree wherever the server happened to
    be started. Distinct from the test above, which starts from a directory that
    already exists: this one pins that *nothing* is created, not merely that the
    marker is not.

    Args:
        tmp_path: Pytest's per-test directory, whose child is never created.
    """
    absent = tmp_path / "nope" / "deeper"
    assert _call(absent)["available"] is False
    assert not absent.exists()


def test_handshake_against_an_absent_vault_records_the_call_in_the_process_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The trail survives the vault's absence by moving, not by disappearing.

    Dropping the entry silently would trade one dishonesty for another: an
    operator polling an uninitialised vault would see no evidence they had ever
    called. There is nowhere in the vault to write it — the vault is the thing
    that does not exist — so the record goes to the process log, naming the tool
    and the path that was probed.

    Args:
        tmp_path: Pytest's per-test directory, hosting a bare vault.
        caplog: Captures the process log for the duration of the call.
    """
    vault = _vault(tmp_path, creek=False)
    with caplog.at_level(logging.INFO, logger="creek_mcp.tools.handshake"):
        _call(vault, consumer="adepthood")
    messages = [record.getMessage() for record in caplog.records]
    assert any(TOOL_NAME in message and str(vault) in message for message in messages)


def test_handshake_still_audits_when_the_vault_is_present(tmp_path: Path) -> None:
    """The absent-vault skip is scoped to absent vaults and nothing else (#1108).

    The guard added for #1108 sits directly in front of the only audit append
    this tool makes, so a guard with an inverted or over-broad condition would
    silently stop auditing every handshake — losing the trail the tool has
    always kept. Pinned separately from
    ``test_handshake_is_audit_logged_with_consumer`` because that test asserts
    what the entry *says*; this one asserts that exactly one is written at all.

    Args:
        tmp_path: Pytest's per-test directory, hosting a scaffolded vault.
    """
    vault = _vault(tmp_path, creek=True)
    _call(vault)
    _call(vault)
    entries = _audit_entries(vault)
    assert len(entries) == 2
    assert {entry["tool"] for entry in entries} == {TOOL_NAME}


def test_the_transport_is_not_a_module_level_constant() -> None:
    """The channel is a per-process fact, so it may not be a module constant (#1583).

    ``TRANSPORT = "stdio"`` was a literal emitted unconditionally, so a consumer
    connected over streamable-HTTP was told ``"transport": "stdio"``. Pinning the
    *absence* of the binding — rather than only the reported value — is what stops
    the two-places-drift being reintroduced: a constant here and the operator's
    ``--transport`` there is exactly how the field came to disagree with reality.
    """
    assert not hasattr(handshake_module, "TRANSPORT"), (
        "creek_mcp.tools.handshake must not hold a module-level TRANSPORT "
        "constant; the transport is stated by build_server per process"
    )


def test_a_network_handshake_reports_the_network_transport(tmp_path: Path) -> None:
    """A consumer served over the network is told ``network`` (#1583).

    The whole point of the field: a streamable-HTTP consumer used to be handed
    ``"transport": "stdio"`` because the value was a module constant. Pinned
    against the literal string rather than the enum member so the *wire* value
    is what is asserted — a consumer parses JSON, not a Python enum.
    """
    result = _call(_vault(tmp_path, creek=True), transport=Transport.NETWORK)
    assert result["transport"] == "network"


def test_a_stdio_handshake_still_reports_stdio(tmp_path: Path) -> None:
    """The local consumer keeps its answer, so the fix cannot be a swapped literal.

    Asserted alongside the network case on purpose: a fix that hardcoded
    ``"network"`` instead of ``"stdio"`` would satisfy the issue's headline and
    be exactly as wrong.
    """
    result = _call(_vault(tmp_path, creek=True), transport=Transport.STDIO)
    assert result["transport"] == "stdio"


def test_a_network_handshake_offers_only_the_remotely_admitted_ceilings(
    tmp_path: Path,
) -> None:
    """``tier_model.ceilings`` stops advertising doors the remote cap bolts shut.

    The second, higher-stakes instance of the same defect. ``TierCeiling`` has
    four members and the handshake published all four unconditionally, so a
    network consumer was told it could request ``intimate`` and ``all`` when
    :data:`~creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` refuses both before
    dispatch. Compared against that constant rather than a literal list, so the
    advertisement cannot drift from the cap it describes.
    """
    tier_model = _call(_vault(tmp_path, creek=True), transport=Transport.NETWORK)[
        "tier_model"
    ]
    assert isinstance(tier_model, dict)
    assert set(tier_model["ceilings"]) == {
        ceiling.value for ceiling in REMOTE_ADMITTED_CEILINGS
    }
    assert TierCeiling.INTIMATE.value not in tier_model["ceilings"]
    assert TierCeiling.ALL.value not in tier_model["ceilings"]


def test_a_stdio_handshake_still_offers_every_ceiling(tmp_path: Path) -> None:
    """A local caller is not capped, so it is still offered all four ceilings.

    The remote cap is about the *network*, not about the tier — narrowing the
    published list for a local consumer would be a different lie, and would take
    ``intimate`` away from the operator's own vault.
    """
    tier_model = _call(_vault(tmp_path, creek=True), transport=Transport.STDIO)[
        "tier_model"
    ]
    assert isinstance(tier_model, dict)
    assert tier_model["ceilings"] == [ceiling.value for ceiling in TierCeiling]


def test_the_transport_argument_has_no_default(tmp_path: Path) -> None:
    """``handshake_tool`` refuses to guess the channel it is answering on.

    A default would let the next adapter reintroduce #1583 by omission, which is
    exactly how the constant survived as long as it did — the same fail-closed
    reasoning that gives :class:`~creek_mcp.policy.CallerIdentity` no default for
    ``is_remote``.
    """
    parameter = inspect.signature(handshake_tool).parameters["transport"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError, match="transport"):
        handshake_tool(  # type: ignore[call-arg]
            vault_path=_vault(tmp_path, creek=True),
            capabilities=_CAPS,
            server_name=_SERVER,
        )
