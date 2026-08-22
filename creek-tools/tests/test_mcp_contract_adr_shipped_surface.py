"""The Adepthood↔Creek contract ADRs may not drift from the shipped surface (#875).

``docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md`` is the document
Adepthood's own contract copy mirrors, and it rotted for five contract minors
without a single test noticing. By 2026-08-21 it declared contract version
``0.4.0`` while its own change log's top row said ``0.9.0``; it labelled
``creek.reflect``, ``creek.wheel``, the Adepthood-journal path and the
Creek-side care guardrail *Planned* after all four had shipped; it said a
network transport was "out of scope for this contract version" after
``--transport network`` had become a real choice; and — the load-bearing
omission — it did not mention the remote tier-ceiling cap at all, so a reader
would conclude a remote consumer could request ``intimate``.

Nothing here is exotic. Every rotted claim was an **enumerable** one: a version
string, a tool name, a status word, a transport, a set of ceilings. Prose is
not what rotted, and this module does not police prose. It pins the enumerable
claims to live code objects and asserts equality in **both** directions, so a
missing row and an invented row both fail. The pattern is
``tests/test_seeding_docs_capability_set.py``'s, deliberately: one working
precedent for docs-vs-code gates is better than two designs.

**Fencing.** Each machine-checked region is marked with the same invisible HTML
comment fences that gate uses::

    <!-- capability-set: mcp-capability-tools -->
    | Capability … | MCP tool | Status | Reference |
    ...
    <!-- /capability-set -->

HTML comments render as nothing, so a reader never sees them and the parser is
unambiguous. Scoping matters as much as the assertions: the change log
legitimately quotes historical version strings (``0.1.0`` … ``0.9.0``) and the
word "planned" about tools that *were* planned in 2026-06, and none of that is
in a fence, so none of it is checked.

:func:`test_every_named_fence_has_a_body` is the guard against the failure mode
a docs gate is most prone to — a renamed or deleted fence turning the whole
module into a green no-op. A gate that reports it did nothing is not a passing
gate.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.api.models import CAPABILITY_SINCE_MINOR, Capability
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.policy import REMOTE_ADMITTED_CEILINGS
from creek_mcp.server import SERVER_NAME, _build_arg_parser, build_server
from creek_mcp.tools.handshake import handshake_tool
from tests.v1_api_support import seed_vault

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""``<repo-root>``: ``tests`` -> ``creek-tools`` -> the root that owns ``docs``."""

MCP_ADR: Final[Path] = (
    REPO_ROOT / "docs" / "decisions" / "2026-06-30-adepthood-creek-mcp-contract.md"
)
HTTP_ADR: Final[Path] = (
    REPO_ROOT / "docs" / "decisions" / "2026-07-31-adepthood-http-application-api.md"
)
API_DOC: Final[Path] = Path(__file__).resolve().parents[1] / "docs" / "api.md"

_FENCE_TEMPLATE: Final[str] = (
    r"<!-- capability-set: {name} -->(?P<body>.*?)<!-- /capability-set -->"
)
_CODE_SPAN: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")

_DEAD_CEILING_SPELLING: Final[str] = "_REMOTE_ADMITTED_CEILINGS"
"""The name #875 and #1094 both quote for the remote cap. It does not exist.

The constant is public and lives in :mod:`creek_mcp.policy`; it was never
underscore-prefixed and was never in ``server.py``. Quoting a symbol that is
not in the tree is the precise defect this module exists to make impossible.
"""

NON_ADEPTHOOD_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "creek.state.read",
        "creek.state.render",
        "creek.lint",
        "creek.mine",
        "creek.draft",
        "creek.author",
        "creek.save",
        "creek.redact.scan",
        "creek.link",
        "creek.report",
        "creek.skills.refresh",
        "creek.compile",
        "creek.purge.fragment",
        "creek.purge.source",
        "creek.purge.classifications",
        "creek.purge.daterange",
        "creek.purge.vault",
    }
)
"""Registered tools that are deliberately outside the Adepthood surface.

Spelled as an explicit deny-list rather than an allow-list of documented tools,
because the direction matters: subtracting this from the live registration set
leaves the tools the ADR **must** document, so a new Adepthood-facing tool
lands in the required set by default and fails until it has a row. An
allow-list would have let it pass in silence, which is how the ADR came to be
missing ``creek.journal`` and ``creek.upload`` in the first place.

``creek.purge.*`` is the clearest case: it is elevated-token-gated and the ADR
says in prose that Adepthood is not expected to hold that token.
"""

_FENCE_NAMES: Final[tuple[tuple[Path, str], ...]] = (
    (MCP_ADR, "contract-versions"),
    (MCP_ADR, "mcp-transports"),
    (MCP_ADR, "mcp-capability-tools"),
    (MCP_ADR, "handshake-example"),
    (MCP_ADR, "remote-ceiling-cap"),
    (API_DOC, "v1-capabilities"),
    (API_DOC, "capabilities-states"),
    (HTTP_ADR, "v1-capabilities"),
    (HTTP_ADR, "capabilities-states"),
)
"""Every fence this module reads, so a renamed one fails loudly rather than silently."""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded, initialised vault.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Yields:
        The vault root.
    """
    yield seed_vault(tmp_path)


# --------------------------------------------------------------------------- #
# Fence parsing
# --------------------------------------------------------------------------- #


def _fence_body(doc: Path, name: str) -> str:
    """Return the raw text between the ``name`` fences in *doc*.

    Args:
        doc: The markdown file to read.
        name: The fence label.

    Returns:
        The fenced body, verbatim.

    Raises:
        AssertionError: If *doc* has no fence with that label.
    """
    match = re.search(
        _FENCE_TEMPLATE.format(name=re.escape(name)),
        doc.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, (
        f"{doc.name} has no <!-- capability-set: {name} --> fence; the "
        "machine-checked region was renamed or deleted"
    )
    return match.group("body")


def _fenced_rows(doc: Path, name: str) -> list[list[str]]:
    """Return the body rows of the markdown table fenced as *name*.

    Args:
        doc: The markdown file to read.
        name: The fence label.

    Returns:
        One list of raw cell strings per body row, header and separator rows
        dropped.

    Raises:
        AssertionError: If the fenced region holds no table body.
    """
    rows: list[list[str]] = []
    for line in _fence_body(doc, name).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue  # separator row
        rows.append(cells)
    assert len(rows) > 1, f"the {name} table in {doc.name} has no body rows"
    return rows[1:]  # drop the header


def _first_code_span(cell: str) -> str:
    """Return the first backtick-quoted token in *cell*.

    Args:
        cell: One markdown table cell.

    Returns:
        The token inside the first pair of backticks.

    Raises:
        AssertionError: If the cell quotes nothing.
    """
    spans = _CODE_SPAN.findall(cell)
    assert spans, f"expected a `code span` in the cell {cell!r}"
    return str(spans[0])


def _registered_tools(vault_path: Path) -> set[str]:
    """Return the tool names ``build_server`` actually registers.

    Args:
        vault_path: A seeded vault the server is built over.

    Returns:
        Every name reachable through ``list_tools()``.
    """
    server = build_server(
        vault_path=vault_path,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    return {tool.name for tool in asyncio.run(server.list_tools())}


# --------------------------------------------------------------------------- #
# The gate must not be a no-op
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("doc", "name"), _FENCE_NAMES)
def test_every_named_fence_has_a_body(doc: Path, name: str) -> None:
    """Each fence this module reads exists and is non-empty.

    Without this, renaming a fence would turn its assertions into vacuous
    passes over an empty string -- a gate reporting that it did nothing, which
    is the one failure mode a docs gate cannot self-detect.

    Args:
        doc: The markdown file the fence lives in.
        name: The fence label.
    """
    assert _fence_body(doc, name).strip(), f"{doc.name}: fence {name} is empty"


# --------------------------------------------------------------------------- #
# 1-2. The capability table names registered tools, and none of them is Planned
# --------------------------------------------------------------------------- #


def _documented_tools() -> dict[str, str]:
    """Return the ADR's tool-name -> status-cell map.

    Returns:
        One entry per row of the ``mcp-capability-tools`` table.
    """
    return {
        _first_code_span(row[1]): row[2]
        for row in _fenced_rows(MCP_ADR, "mcp-capability-tools")
    }


def test_the_capability_table_names_only_registered_tools(vault: Path) -> None:
    """Every tool the ADR's capability table names is really registered.

    Args:
        vault: A seeded vault.
    """
    assert set(_documented_tools()) <= _registered_tools(vault), (
        "the ADR's capability table names an MCP tool that build_server does "
        "not register"
    )


def test_every_adepthood_tool_registered_is_in_the_table(vault: Path) -> None:
    """No Adepthood-surface tool may ship without a row.

    The required set is *every* registered tool minus
    :data:`NON_ADEPTHOOD_TOOLS`, so a new Adepthood-facing tool fails here by
    default. This is the assertion that would have caught ``creek.journal``
    (#754) and ``creek.upload`` (#1023) being absent from the ADR.

    Args:
        vault: A seeded vault.
    """
    required = _registered_tools(vault) - NON_ADEPTHOOD_TOOLS
    assert required == set(_documented_tools()), (
        "the ADR's capability table and the registered Adepthood surface "
        "disagree; add a row, or add the tool to NON_ADEPTHOOD_TOOLS with a "
        "reason"
    )


def test_no_registered_tool_is_labelled_planned(vault: Path) -> None:
    """A registered tool may not be described as *Planned* anywhere.

    "Planned" is the single word that rotted hardest: it survived on
    ``creek.reflect``, ``creek.wheel`` and the journal path for four closed
    issues. Both the table's status cells and the document's own section
    headings are checked, because the stale labels lived in both.

    Args:
        vault: A seeded vault.
    """
    registered = _registered_tools(vault)
    for tool, status in _documented_tools().items():
        if tool in registered:
            assert "planned" not in status.lower(), (
                f"{tool} is registered but its ADR row still says {status!r}"
            )
    headings = [
        line
        for line in MCP_ADR.read_text(encoding="utf-8").splitlines()
        if line.startswith("#") and "planned" in line.lower()
    ]
    assert not headings, f"stale *Planned* section headings: {headings}"


# --------------------------------------------------------------------------- #
# 3. Version strings
# --------------------------------------------------------------------------- #


def _header_value(label: str) -> str:
    """Return the fenced header bullet's backticked value for *label*.

    Args:
        label: The bolded bullet label, e.g. ``"Contract version"``.

    Returns:
        The token inside the first backticks after the label.

    Raises:
        AssertionError: If the header block has no such bullet.
    """
    for line in _fence_body(MCP_ADR, "contract-versions").splitlines():
        if f"**{label}**" in line:
            return _first_code_span(line.split(f"**{label}**", 1)[1])
    pytest.fail(f"the contract-versions fence has no **{label}** bullet")


def test_the_adr_publishes_the_current_contract_and_ontology_versions() -> None:
    """The ADR header equals the runtime constants.

    This fires on every future bump, which is the point: the header said
    ``0.4.0`` for five minors because nothing compared it to
    :data:`creek_mcp.contract.CONTRACT_VERSION`.
    """
    assert _header_value("Contract version") == CONTRACT_VERSION
    assert _header_value("Ontology version") == ONTOLOGY_VERSION


# --------------------------------------------------------------------------- #
# 4. The handshake example
# --------------------------------------------------------------------------- #


def _handshake_example() -> dict[str, Any]:
    """Return the ADR's handshake example, parsed.

    Returns:
        The decoded JSON object inside the ``handshake-example`` fence.
    """
    body = _fence_body(MCP_ADR, "handshake-example")
    match = re.search(r"```json\n(?P<payload>.*?)```", body, re.DOTALL)
    assert match is not None, "the handshake-example fence holds no ```json block"
    parsed: dict[str, Any] = json.loads(match.group("payload"))
    return parsed


def test_the_handshake_example_matches_the_handshake_tool_keys(vault: Path) -> None:
    """The documented example's key set equals a real handshake's.

    Keys only, not values: the example's ``capabilities`` list is abbreviated
    with ``"..."`` on purpose, and ``available`` depends on the vault. The two
    version strings are compared separately below, because those are the fields
    a consumer negotiates on.

    Args:
        vault: A seeded vault.
    """
    live = handshake_tool(
        vault_path=vault,
        capabilities=sorted(_registered_tools(vault)),
        server_name=SERVER_NAME,
    )
    assert set(_handshake_example()) == set(live), (
        "the ADR's handshake example documents a different key set than "
        "handshake_tool returns"
    )


def test_the_handshake_example_publishes_the_current_versions() -> None:
    """The example's two version strings equal the runtime constants."""
    example = _handshake_example()
    assert example["contract_version"] == CONTRACT_VERSION
    assert example["ontology_version"] == ONTOLOGY_VERSION


# --------------------------------------------------------------------------- #
# 5. Transports
# --------------------------------------------------------------------------- #


def _shipped_transports() -> set[str]:
    """Return the ``--transport`` choices the server really offers.

    Read out of the parser's own rendered help rather than its private
    ``_actions``, so this asserts against published output.

    Returns:
        The choice names, e.g. ``{"stdio", "network"}``.
    """
    help_text = _build_arg_parser().format_help()
    match = re.search(r"--transport \{([^}]+)\}", help_text)
    assert match is not None, "--transport no longer renders its choices in --help"
    return {choice.strip() for choice in match.group(1).split(",")}


def test_the_transport_table_matches_the_shipped_choices() -> None:
    """The ADR's transport table equals ``--transport``'s real choices.

    This is the assertion that would have caught the ADR claiming a network
    transport was "out of scope for this contract version" after #755 shipped
    one.
    """
    documented = {
        _first_code_span(row[0]) for row in _fenced_rows(MCP_ADR, "mcp-transports")
    }
    assert documented == _shipped_transports()


# --------------------------------------------------------------------------- #
# 6. The remote ceiling cap -- named correctly, and listing the real set
# --------------------------------------------------------------------------- #


def test_the_adr_names_the_remote_ceiling_cap_by_its_real_constant() -> None:
    """The tier-ceiling fence names the live constant and not the dead one."""
    body = _fence_body(MCP_ADR, "remote-ceiling-cap")
    assert "REMOTE_ADMITTED_CEILINGS" in body
    assert "creek_mcp/policy.py" in body
    assert _DEAD_CEILING_SPELLING not in body, (
        "the ADR quotes a constant that does not exist in the tree"
    )


def test_the_adr_lists_exactly_the_remote_admitted_ceilings() -> None:
    """The fenced ceiling table equals :data:`REMOTE_ADMITTED_CEILINGS`.

    The security-relevant arm. Admitting ``intimate`` remotely would have to
    move this table too, so the document can never quietly keep promising a
    flat refusal the code stopped making.
    """
    documented = {
        _first_code_span(row[0]) for row in _fenced_rows(MCP_ADR, "remote-ceiling-cap")
    }
    assert documented == {ceiling.value for ceiling in REMOTE_ADMITTED_CEILINGS}


# --------------------------------------------------------------------------- #
# 7. The /v1 capability list, in both documents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("doc", [API_DOC, HTTP_ADR])
def test_the_v1_capability_table_equals_the_enum(doc: Path) -> None:
    """Both documents' capability tables equal ``Capability`` and its minors.

    Args:
        doc: The markdown file whose ``v1-capabilities`` fence is checked.
    """
    documented = {
        _first_code_span(row[0]): _first_code_span(row[1])
        for row in _fenced_rows(doc, "v1-capabilities")
    }
    expected = {
        capability.value: CAPABILITY_SINCE_MINOR[capability]
        for capability in Capability
    }
    assert documented == expected


# --------------------------------------------------------------------------- #
# 8. Row (c): the two capability-state tables must agree, and neither may say "-"
# --------------------------------------------------------------------------- #


def _incompatible_available_cell(doc: Path) -> str:
    """Return the ``vault.available`` cell of the ``incompatible`` row.

    Args:
        doc: The markdown file whose ``capabilities-states`` fence is read.

    Returns:
        The raw cell text.

    Raises:
        AssertionError: If the table has no ``incompatible`` row.
    """
    for row in _fenced_rows(doc, "capabilities-states"):
        if "incompatible" in row[0]:
            return row[1]
    pytest.fail(f"{doc.name}'s capabilities-states table has no incompatible row")


def test_the_two_capability_state_tables_agree_on_row_c() -> None:
    """``docs/api.md`` and the HTTP ADR render the same cell for ``incompatible``.

    #1150's actual requirement, in its enumerable form: the two documents must
    not diverge from each other, and neither may render the cell as an em dash.
    ``vault.available`` is computed unconditionally and emitted at every status,
    so "unspecified" is not one of the things it can be.
    """
    api_cell = _incompatible_available_cell(API_DOC)
    adr_cell = _incompatible_available_cell(HTTP_ADR)
    assert api_cell == adr_cell
    assert api_cell.strip() != "—"


# --------------------------------------------------------------------------- #
# 9. The change log covers every minor the header claims it covers
# --------------------------------------------------------------------------- #


def _change_log_versions() -> set[str]:
    """Return the contract versions the MCP ADR's change log has rows for.

    Returns:
        Every backtick-quoted version string in the first column of the table
        under ``## Change log``. Rows whose first cell quotes no version — the
        ``*(no contract change)*`` documentation rows — are skipped.

    Raises:
        AssertionError: If the document has no change-log table body.
    """
    text = MCP_ADR.read_text(encoding="utf-8")
    start = text.index("\n## Change log\n")
    tail = text[start + 1 :]
    end = tail.find("\n## ")
    section = tail if end == -1 else tail[:end]

    versions: set[str] = set()
    body_rows = 0
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        first = stripped.strip("|").split("|")[0].strip()
        if set(first) <= {"-", ":"} and first:
            continue  # separator row
        body_rows += 1
        spans = _CODE_SPAN.findall(first)
        if spans:
            versions.add(str(spans[0]))
    assert body_rows > 1, "the MCP ADR's change log has no table body"
    return versions


def _minors_through_current() -> set[str]:
    """Return every ``0.N.0`` string from ``0.1.0`` up to the live contract.

    Returns:
        The full inclusive range the ADR header promises is recorded.
    """
    highest = int(CONTRACT_VERSION.split(".")[1])
    return {f"0.{minor}.0" for minor in range(1, highest + 1)}


def test_the_change_log_has_a_row_for_every_minor_up_to_the_current_one() -> None:
    """Every minor from ``0.1.0`` to ``CONTRACT_VERSION`` has a change-log row.

    This is the assertion behind the header's claim that "each minor between
    ``0.1.0`` and ``0.9.0`` is recorded in the change log with the change that
    earned it". It would have caught the four minors — ``0.5.0``-``0.8.0`` —
    that this document silently omitted while a note further down claimed they
    had moved the ``/v1`` surface only, which was false for three of them.
    """
    missing = _minors_through_current() - _change_log_versions()
    assert not missing, (
        f"the MCP ADR's change log has no row for {sorted(missing)}; the header "
        "claims every minor up to the current contract version is recorded"
    )
