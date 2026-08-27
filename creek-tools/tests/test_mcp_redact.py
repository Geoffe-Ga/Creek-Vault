"""Tests for the ``creek.redact.scan`` MCP tool (FEAT-027).

Covers: an empty staging dir, findings present, findings absent, refusal
for paths outside the vault, refusal for missing paths, audit logging,
and scanning a single file (not a directory).

#972 adds the other half of the story. Every test above scans under
``00-Creek-Meta/Inbound/`` — the only subtree FEAT-027 ever asked for — so
none of them can see what the tool does when a caller aims it somewhere
else. What it does is answer: an ``open``-ceiling caller pointed at
``01-Fragments`` gets back the *filenames* of intimate fragments, and Creek
fragment filenames are slugified titles, so the filename **is** the
payload. The tests added below pin the two things that close it: the scope
gate (:func:`~creek_mcp.tools.redact._refuse_outside_scan_scope`) and the
single fail-closed relativiser
(:func:`~creek_mcp.tools.redact._vault_relative`) that every path in the
response is rendered through. Six of the original seven are deliberately
unchanged, and they double as the FEAT-027 regression: the staging subtree
must stay admitted at every ceiling, or CrawDad's safety pass stops
working. The seventh,
:func:`test_scan_refuses_absolute_path_outside_vault`, keeps its
confinement assertion and gives up its *wording* assertion, because the
scope gate subsumes it: an un-admitted caller must not be able to tell an
outside-the-vault path from an in-vault out-of-scope one, and the reason
why is written out there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from creek.config import CreekConfig
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling, refusal_response
from creek_mcp.tools.redact import (
    _OUT_OF_SCOPE_REASON,
    _OUTSIDE_VAULT_PLACEHOLDER,
    _vault_relative,
    redact_scan_tool,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOOL_NAME = "creek.redact.scan"
"""The registered tool name, echoed by every response this file inspects.

Spelled out rather than imported from the module under test: the refusal
payloads below are compared for *equality*, and an equality assertion built
out of the implementation's own constants can only ever agree with itself.
"""

_STAGING_ROOT = "00-Creek-Meta/Inbound"
"""The FEAT-027 staging subtree: the one scope admitted at every ceiling."""

_FRAGMENTS_ROOT = "01-Fragments"
"""The classified corpus: in the vault, out of the scan's scope."""

_OUTWARD_LINK_RELPATH = f"{_FRAGMENTS_ROOT}/Notes/escape.md"
"""Where :func:`_plant_outward_symlink` parks a link pointing off the vault.

Spelled as an ordinary fragment path because that is the point: the string a
caller sends says nothing about whether the file behind it is a link, so any
difference in the answer is a fact about the vault rather than about the
request.
"""

_PII_LINE = "Reach me at someone@example.com any time.\n"
"""One real ``email`` match, so a scan of the file yields ``status="ok"``.

Findings are what make the path-rendering assertions non-vacuous — a file
with nothing in it produces no ``### `path``` heading to inspect.
"""

_INTIMATE_FRONT_MATTER = "---\ntype: fragment\nprivacy_tier: intimate\n---\n\n"
"""Front matter marking a seeded file as intimate.

The scan reads no per-file tiers — that is precisely why the gate treats
every out-of-scope target as if it held intimate content — so this is
documentation of the scenario rather than an input to the gate.
"""

_FILENAME_CANARY = "CANARY-REDACT-TITLE-8f14"
"""Sentinel carried in a fragment's **file stem**, never in its body.

A plain sentinel rather than realistic prose, so a leak cannot be excused as
"that phrase could have come from anywhere". It lives in the filename
because that is where this tool's leak is: bodies are only ever reported as
salted hashes, and the tool was already careful about those.
"""

_OUTSIDE_CANARY = "CANARY-REDACT-OUTSIDE-3b90"
"""Sentinel for the components of a path the relativiser cannot render.

Every component of the probe path in
:func:`test_an_unrenderable_path_falls_back_to_a_non_disclosing_placeholder`
carries it, so "no component of the input survived into the placeholder"
cannot be satisfied — or falsified — by an accidental substring collision
with ordinary English wording.
"""

_HEADING_PATTERN = re.compile(r"^### `(?P<path>.+)`$", re.MULTILINE)
"""Matches ``generate_markdown_summary``'s per-file headings.

Anchored on the backticks so the ``### By Severity`` heading in the same
document is not mistaken for a path.
"""

_ADMITTED_SCANS = [
    *((_STAGING_ROOT, ceiling) for ceiling in TierCeiling),
    (_FRAGMENTS_ROOT, TierCeiling.INTIMATE),
    (_FRAGMENTS_ROOT, TierCeiling.ALL),
]
"""Every ``(input_path, ceiling)`` pair the scope gate admits.

Both halves matter: the staging subtree is admitted at *every* ceiling
(FEAT-027), and the rest of the vault is admitted only where
``tier_allowed(INTIMATE, ceiling)`` holds. A path-rendering test that
covered only the first half would leave the response shape unchecked on
exactly the scans that carry fragment filenames.
"""


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Vault skeleton with the audit dir + Inbound staging dir and a stub config."""
    (tmp_path / "00-Creek-Meta" / "audit").mkdir(parents=True)
    (tmp_path / "00-Creek-Meta" / "Inbound").mkdir(parents=True)

    monkeypatch.setattr(
        "creek_mcp.tools.redact.load_vault_config",
        lambda _vault_path, **_kwargs: CreekConfig(),
    )
    yield tmp_path


def _read_audit(vault: Path) -> list[dict[str, object]]:
    path = vault / MCP_AUDIT_RELPATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_pii_file(target: Path, *, front_matter: str = "") -> Path:
    """Write a scannable markdown file carrying exactly one real PII match.

    Args:
        target: Destination path; parents are created.
        front_matter: Optional YAML block prepended to the body. Staged
            attachments carry none; seeded fragments carry
            :data:`_INTIMATE_FRONT_MATTER`.

    Returns:
        The path written, so a caller can symlink to it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{front_matter}{_PII_LINE}", encoding="utf-8")
    return target


def _plant_outward_symlink(vault: Path, tmp_path: Path) -> str:
    """Park a symlink at :data:`_OUTWARD_LINK_RELPATH` pointing off the vault.

    This is the third branch of the out-of-scope story, and the only one that
    leaves the tool through ``resolve_within_vault``'s ``None`` arm:
    resolution follows the link, lands outside the root, and so never reaches
    the scope gate at all.

    Args:
        vault: Vault root the *link* is planted inside.
        tmp_path: pytest's per-test directory, which is also the vault root.
            The link's target is written to a **sibling** of it, so "outside
            the vault" is true by construction rather than by a hard-coded
            path a later fixture change could quietly move back inside.

    Returns:
        The vault-relative ``input_path`` naming the link, so a caller sends
        an ordinary-looking fragment path and nothing else.
    """
    target = _write_pii_file(tmp_path.parent / "off-vault" / "target.md")
    link = vault / _OUTWARD_LINK_RELPATH
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return _OUTWARD_LINK_RELPATH


def _markdown_headings(report_markdown: str) -> list[str]:
    """Return the file paths ``report_markdown`` renders as ``### `…``` headings.

    Args:
        report_markdown: The tool's markdown summary.

    Returns:
        One string per rendered file section, in document order. This is the
        field CrawDad posts verbatim into a Discord channel
        (``crawdad/crawdad/bot.py::_format_scan_section``), so it is the
        surface an absolute path escapes through.
    """
    return _HEADING_PATTERN.findall(report_markdown)


@pytest.fixture
def scannable_vault(vault: Path) -> Path:
    """Seed one PII-bearing file in the staging subtree and one under ``01-Fragments``.

    Two carriers, because every assertion below needs both directions at
    once: the staged file is the positive control that keeps FEAT-027 alive,
    and the fragment is the thing an ``open``-ceiling caller must not be able
    to name. The fragment's *file stem* carries :data:`_FILENAME_CANARY`
    while its body does not, which is what makes "the canary appeared in the
    response" mean "a filename leaked" and nothing else.
    """
    _write_pii_file(vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg" / "staged.md")
    _write_pii_file(
        vault / "01-Fragments" / "Notes" / f"my-{_FILENAME_CANARY}-note.md",
        front_matter=_INTIMATE_FRONT_MATTER,
    )
    return vault


def test_scan_returns_empty_when_no_findings(vault: Path) -> None:
    """A clean staging directory yields ``status="empty"`` with no findings."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg1"
    staging.mkdir(parents=True)
    (staging / "note.md").write_text("# Title\n\nSome safe markdown text.\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg1",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result["status"] == "empty"
    assert result["tool"] == "creek.redact.scan"
    assert result["statistics"]["total_findings"] == 0
    assert result["statistics"]["files_scanned"] == 1
    assert result["findings"] == []
    assert "Redaction Scan Summary" in result["report_markdown"]


def test_scan_reports_findings_when_pii_present(vault: Path) -> None:
    """A staging file with an email address surfaces a structured finding."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg2"
    staging.mkdir(parents=True)
    (staging / "leak.md").write_text(
        "Reach me at someone@example.com any time.\n",
    )

    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg2",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result["status"] == "ok"
    assert result["statistics"]["total_findings"] >= 1
    finding = result["findings"][0]
    assert finding["match_type"] == "email"
    assert finding["severity"] in {"medium", "high"}
    # File path is rendered relative to the vault root — no absolute leak.
    assert finding["file_path"] == "00-Creek-Meta/Inbound/ch1/msg2/leak.md"
    # The matched text itself is never returned, only a salted hash.
    assert "someone@example.com" not in json.dumps(result)
    assert len(finding["salted_hash"]) == 64  # SHA-256 hex


def test_scan_refuses_absolute_path_outside_vault(
    vault: Path,
    tmp_path: Path,
) -> None:
    """Paths outside the vault root are refused, never scanned.

    The confinement property is unchanged and is what the four-key equality
    below states: no ``statistics`` block, no ``findings``, nothing derived
    from *outside* at all, so the path is refused without being opened.

    What changed is the **wording**, and it changed for a reason worth
    recording here rather than in the commit message. This test used to assert
    ``"outside the vault" in result["reason"]``, which pinned a second,
    distinguishable refusal shape for an un-admitted caller: an in-vault
    target they are not scoped to answers :data:`_OUT_OF_SCOPE_REASON`, while
    a target that escapes the vault answered a different message with their
    own path interpolated into it. That difference is reachable from *inside*
    the vault — a symlink under ``01-Fragments/`` whose target is elsewhere on
    disk resolves out and takes the escape arm — so the pair was an oracle: a
    caller refused ``01-Fragments`` could still ask, one slugified title at a
    time, which of them are outward links.
    :func:`test_out_of_scope_refusals_are_indistinguishable_from_an_outward_symlink`
    is the reproduction; this test is the same rule stated on the plainest
    possible target.

    The precise message is not deleted, only withheld: it survives at
    ``ceiling=intimate`` and ``all``, where the caller was already admitted to
    everything and it is the one operationally useful diagnostic. That is
    pinned by
    :func:`test_an_outward_symlink_still_reports_precisely_at_an_admitting_ceiling`,
    and without it this test could be satisfied by dropping the message
    outright.
    """
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(outside),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        "an open-ceiling caller can tell an outside-the-vault path from an "
        "in-vault out-of-scope one by the refusal alone, so the refusal "
        f"discriminates on something the caller was not admitted to.\n\n{result}"
    )


def test_scan_refuses_a_nul_byte_path_without_raising(vault: Path) -> None:
    """The scan tool refuses an unresolvable path rather than raising (#1089).

    The companion to the ingest case. Both tools route caller-supplied paths
    through the same ``resolve_within_vault``, so one unguarded ``resolve()``
    was a raise on both surfaces; both are asserted so a future edit that
    guards only one call site is caught.
    """
    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/no\x00pe",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused", (
        f"a NUL-byte input_path did not produce a structured refusal: {result}"
    )


def test_scan_refuses_missing_path(vault: Path) -> None:
    """A non-existent vault-relative path is refused cleanly."""
    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/does/not/exist",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "not found" in result["reason"]


def test_scan_writes_audit_entry(vault: Path) -> None:
    """Every invocation appends one entry to ``mcp.jsonl`` with the tool name."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg3"
    staging.mkdir(parents=True)
    (staging / "note.txt").write_text("nothing to see here\n")

    redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg3",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="crawdad",
    )

    entries = _read_audit(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.redact.scan"
    assert entry["tier_ceiling"] == "personal"
    assert entry["consumer"] == "crawdad"


def test_scan_accepts_single_file_path(vault: Path) -> None:
    """A file (not a directory) is scanned as a one-file batch."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg4"
    staging.mkdir(parents=True)
    target = staging / "single.md"
    target.write_text("Plain notes, no secrets.\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(target),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "empty"
    assert result["statistics"]["files_scanned"] == 1


def test_scan_accepts_absolute_path_inside_vault(vault: Path) -> None:
    """An absolute path inside the vault is accepted (not just relative)."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg5"
    staging.mkdir(parents=True)
    (staging / "note.md").write_text("clean\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(staging),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "empty"
    assert result["input_path"].startswith("00-Creek-Meta/Inbound/")


# ---------------------------------------------------------------------------
# Scope, not a per-file tier filter (#972)
#
# The scan reads no per-file tiers — it is a regex pass over bytes — so it has
# nothing to rank a fragment with. The gate therefore decides *where*, not
# *what*: the FEAT-027 staging subtree is admitted at every ceiling, and every
# other in-vault target is ranked as if it held intimate content, because for
# all this tool knows it does.
# ---------------------------------------------------------------------------


def test_scan_refuses_the_fragments_subtree_at_the_open_ceiling(
    scannable_vault: Path,
) -> None:
    """An ``open``-ceiling caller cannot aim the scan at the classified corpus.

    The whole of #972 in one call. ``resolve_within_vault`` confines
    *input_path* to the vault, which is true and insufficient: the vault is
    where the fragments are, and a finding names the file it came from.

    Asserted as **equality** with the canonical four-key payload rather than
    as ``status == "refused"``, because the specific thing that must not come
    back is a ``statistics`` block. A refusal carrying ``files_scanned: 0``
    would be a differential oracle in a tidier wrapper — the caller learns
    nothing from a scan they were refused, unless the refusal itself counts
    the files it declined to scan.

    ``_OUT_OF_SCOPE_REASON`` is imported rather than spelled out, following
    the ``_ABOVE_CEILING_REASON`` precedent in ``tests/test_mcp_read_gate.py``:
    the reason is a fixed module constant precisely so it cannot grow an
    interpolated copy of the caller's path, and a literal here would let the
    constant drift while the test kept passing against the old wording.
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_FRAGMENTS_ROOT,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        "creek.redact.scan's out-of-scope refusal is not the canonical "
        "four-key payload. Every extra key is derived from a subtree the "
        f"caller was refused.\n\n{result}"
    )


def test_scan_refuses_out_of_scope_paths_identically_whether_or_not_they_exist(
    vault: Path,
) -> None:
    """The refusal is not an existence oracle over slugified fragment titles.

    Today the two calls below differ: the absent path answers ``refused`` /
    ``"input_path not found: …"`` and the present one answers ``ok``. That
    difference is the whole attack — a caller who cannot read a fragment can
    still ask "is there one called ``my-affair-with-dana``?" and read the
    answer off the status line, one slugified title at a time.

    Closing it is what forces the gate to sit **above** the existence check
    rather than below it, and asserting the two responses are ``==`` is what
    pins that ordering: a gate placed after ``resolved.exists()`` would
    produce two different refusals and still look like a fix.

    The ``refused`` assertion at the end guards the degenerate direction —
    two identical ``ok`` responses would also be equal.
    """
    probe = "01-Fragments/Notes/my-secret-thing.md"

    absent = redact_scan_tool(
        vault_path=vault,
        input_path=probe,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    _write_pii_file(
        vault / "01-Fragments" / "Notes" / "my-secret-thing.md",
        front_matter=_INTIMATE_FRONT_MATTER,
    )
    present = redact_scan_tool(
        vault_path=vault,
        input_path=probe,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert absent == present, (
        "creek.redact.scan answers differently for an out-of-scope path that "
        "exists and one that does not, so the refusal is a one-bit oracle "
        "over fragment filenames — which are slugified titles.\n\n"
        f"absent:  {absent}\npresent: {present}"
    )
    assert absent["status"] == "refused"


def test_out_of_scope_refusals_are_indistinguishable_from_an_outward_symlink(
    scannable_vault: Path,
    tmp_path: Path,
) -> None:
    """One refusal, three targets: the un-admitted caller learns nothing.

    The sibling test above closed the *existence* half of the oracle by
    moving the scope gate above ``resolved.exists()``. It could not see this
    half, because a third arm sits **above** the gate entirely: when
    ``resolve_within_vault`` returns ``None``, ``redact_scan_tool`` refuses
    with its own path-interpolated "resolves outside the vault root" message
    before scope is ever consulted. So an ``open``-ceiling caller aiming at
    ``01-Fragments/Notes/`` today gets two answers, not one:

    * ``plain.md`` (an ordinary file) and ``absent.md`` (nothing there at all)
      both answer :data:`_OUT_OF_SCOPE_REASON` — already indistinguishable;
    * ``escape.md`` (a symlink whose target is off the vault) answers the
      outside-the-vault message instead.

    The third is the branch that fails today, and it is an oracle in its own
    right: a caller who is not admitted to ``01-Fragments`` at all learns
    that there is an outward link at exactly the path they named. It also
    falsifies the manifest rationale in ``creek_mcp/read_gate.py``, which
    claims this refusal "is neither an existence nor a rank oracle".

    Asserted as three-way equality rather than as three reason strings,
    because the refusal payload carries no path at all once the arms agree —
    there is then nothing left for the responses to differ *in*, and equality
    is the only assertion that says so without enumerating the fields.

    The second assertion pins **which** way they collapsed. Equality alone
    would also be satisfied by routing the in-vault targets into the
    outside-the-vault message, which is the wrong direction: that message
    interpolates the caller's own path and asserts something about the
    filesystem, while :data:`_OUT_OF_SCOPE_REASON` is fixed text that is true
    of all three — a path escaping the vault is by definition outside the
    staging subtree.
    """
    _write_pii_file(
        scannable_vault / _FRAGMENTS_ROOT / "Notes" / "plain.md",
        front_matter=_INTIMATE_FRONT_MATTER,
    )
    outward = _plant_outward_symlink(scannable_vault, tmp_path)

    present, absent, escaping = [
        redact_scan_tool(
            vault_path=scannable_vault,
            input_path=probe,
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer="crawdad",
        )
        for probe in (
            f"{_FRAGMENTS_ROOT}/Notes/plain.md",
            f"{_FRAGMENTS_ROOT}/Notes/absent.md",
            outward,
        )
    ]

    assert present == absent == escaping, (
        "creek.redact.scan answers an out-of-scope path differently when it "
        "happens to be a symlink pointing off the vault, so the refusal tells "
        "an un-admitted caller which fragment paths are outward links.\n\n"
        f"present:  {present}\nabsent:   {absent}\nescaping: {escaping}"
    )
    assert escaping == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        "the three refusals agree on the wrong message: the outside-the-vault "
        "wording interpolates the caller's path and asserts a fact about the "
        "filesystem, where the out-of-scope reason is fixed text that is true "
        f"of every one of them.\n\n{escaping}"
    )


@pytest.mark.parametrize("ceiling", [TierCeiling.INTIMATE, TierCeiling.ALL])
def test_an_outward_symlink_still_reports_precisely_at_an_admitting_ceiling(
    scannable_vault: Path,
    tmp_path: Path,
    ceiling: TierCeiling,
) -> None:
    """The collapse is scoped to callers who were not admitted to everything.

    Without this test the one above it is satisfiable by deleting the
    outside-the-vault message from the tool entirely, which would trade an
    oracle for an outage of a different kind: the operator running their own
    safety pass over their own vault would be told "out of scope" about a path
    that is not out of scope, it is *gone* — a link into a stale sync folder,
    a moved drive, a broken restore. That diagnostic is the one actionable
    fact in the whole refusal and it costs nothing here, because a caller at
    ``intimate`` or ``all`` is already admitted to every in-vault target the
    precise message could distinguish this one from.

    Parametrised over exactly the two ceilings ``tier_allowed(INTIMATE, …)``
    admits, matching
    :func:`test_scan_admits_the_fragments_subtree_at_intimate_and_all`, so the
    boundary between the two behaviours is the same predicate the gate uses
    rather than a second, private copy of it.

    The inequality against :data:`_OUT_OF_SCOPE_REASON` is asserted alongside
    the substring: a reason that managed to contain "outside the vault" *and*
    equal the out-of-scope text would mean the two messages had been merged,
    which is the collapse this test exists to forbid.
    """
    outward = _plant_outward_symlink(scannable_vault, tmp_path)

    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=outward,
        privacy_tier_ceiling=ceiling,
    )

    assert result["status"] == "refused"
    assert result["reason"] != _OUT_OF_SCOPE_REASON, (
        f"at ceiling={ceiling.value!r} the caller is admitted to every "
        "in-vault target, so collapsing the outside-the-vault refusal into "
        "the out-of-scope one withholds nothing and costs the operator their "
        f"one actionable diagnostic.\n\n{result}"
    )
    assert "outside the vault" in result["reason"], (
        "an admitting ceiling no longer reports that the path resolved off "
        f"the vault.\n\n{result}"
    )


def test_scan_refuses_a_staged_symlink_whose_target_is_out_of_scope(
    scannable_vault: Path,
) -> None:
    """A symlink parked in the staging dir cannot smuggle the scan out of scope.

    ``resolve_within_vault`` resolves the caller's path before returning it,
    so the gate sees the real target rather than the staging-shaped name the
    caller used. That is the property being pinned: a gate that matched on
    the *input string* instead would admit this call, and staging is a
    directory CrawDad writes attachments into — a caller who can stage a file
    can stage a symlink.
    """
    sneaky = scannable_vault / "00-Creek-Meta" / "Inbound" / "sneaky"
    sneaky.symlink_to(scannable_vault / _FRAGMENTS_ROOT, target_is_directory=True)

    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path="00-Creek-Meta/Inbound/sneaky",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        "a symlink under 00-Creek-Meta/Inbound/ pointed the scan at "
        f"01-Fragments and the gate admitted it.\n\n{result}"
    )


def test_a_sibling_prefix_directory_is_not_mistaken_for_the_staging_subtree(
    vault: Path,
) -> None:
    """``00-Creek-Meta/InboundX/`` is a sibling of the staging dir, not a child.

    A regression pin rather than a reproduction: this passes today and is
    expected to. ``_refuse_outside_scan_scope``'s docstring claims that
    matching with :meth:`~pathlib.Path.is_relative_to` "rather than a string
    prefix" is what keeps ``Inbound-other/`` from being mistaken for a child,
    and that claim had no test — so the cheapest possible "simplification",
    ``str(resolved).startswith(str(staging))``, would have kept every other
    test in this file green while admitting the directory below at
    ``ceiling=open``.

    The target is derived from :data:`_STAGING_ROOT` with one character
    appended, so a future move of the staging subtree carries its own
    near-miss with it instead of leaving this test aimed at a path that no
    longer neighbours anything.

    Seeded with a PII file so an admitting implementation would answer
    ``status="ok"`` with a finding in it, rather than an empty scan that could
    be mistaken for a refusal at a glance.
    """
    near_miss = f"{_STAGING_ROOT}X"
    _write_pii_file(vault / near_miss / "leak.md")

    result = redact_scan_tool(
        vault_path=vault,
        input_path=near_miss,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        f"{near_miss!r} shares a string prefix with the staging subtree but "
        "is not inside it, and the scope gate admitted it — so containment is "
        f"being decided on characters rather than on path components.\n\n{result}"
    )


@pytest.mark.parametrize("as_absolute", [False, True])
def test_the_vault_root_itself_is_out_of_scope(
    scannable_vault: Path,
    as_absolute: bool,
) -> None:
    """The widest version of #972: aim the scan at everything.

    The second untested half of ``_refuse_outside_scan_scope``'s containment
    claim, and like the sibling-prefix case it passes today. The docstring
    records that "the subtree root itself still counts" as inside; the
    converse — that the *vault* root does not count as inside the staging
    subtree, even though the staging subtree is inside it — is the direction
    that matters, because a vault-root scan sweeps ``01-Fragments`` wholesale
    and returns the slugified title of every fragment with a stray email
    address in it. Any containment check written the wrong way round, or a
    gate that special-cased "the target contains the staging dir", would open
    it.

    Both spellings are exercised because ``resolve_within_vault`` reaches the
    root through two different branches — ``"."`` is joined under the resolved
    root, an absolute path is taken as given — and only one of them would be
    covered by testing either alone.

    :func:`scannable_vault` seeds a PII-bearing intimate fragment under
    ``01-Fragments/Notes/``, so an admitting implementation would return
    :data:`_FILENAME_CANARY` here instead of the empty scan that would make
    this assertion cheap.
    """
    input_path = str(scannable_vault) if as_absolute else "."

    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=input_path,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result == refusal_response(
        tool=_TOOL_NAME,
        ceiling=TierCeiling.OPEN,
        reason=_OUT_OF_SCOPE_REASON,
    ), (
        "an open-ceiling caller scanned the vault root, which contains the "
        "staging subtree and everything else — 01-Fragments included — so the "
        f"scope gate admitted the whole corpus in one call.\n\n{result}"
    )


@pytest.mark.parametrize("ceiling", list(TierCeiling))
def test_scan_admits_the_staging_subtree_at_every_ceiling(
    scannable_vault: Path,
    ceiling: TierCeiling,
) -> None:
    """FEAT-027 survives the fix, at the lowest ceiling as well as the highest.

    This is #972's recoverability criterion, executed rather than asserted in
    prose. CrawDad runs the safety pass on Discord attachments staged under
    ``00-Creek-Meta/Inbound/`` before dispatching ``creek.ingest``, and it
    calls at ``ceiling=open``. A scope gate that refused everything would
    satisfy every leak assertion in this file and break the one caller the
    tool was built for.

    Parametrised over all four ceilings so the admission cannot be
    implemented as a quirk of one branch — the staging subtree is admitted
    because of *where* it is, not because of what the caller declared.
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_STAGING_ROOT,
        privacy_tier_ceiling=ceiling,
        consumer="crawdad",
    )

    assert result["status"] == "ok"
    assert result["statistics"]["total_findings"] >= 1
    assert result["input_path"] == _STAGING_ROOT


@pytest.mark.parametrize("ceiling", [TierCeiling.INTIMATE, TierCeiling.ALL])
def test_scan_admits_the_fragments_subtree_at_intimate_and_all(
    scannable_vault: Path,
    ceiling: TierCeiling,
) -> None:
    """The corpus is out of *scope*, not out of *reach*.

    Without this test the open-ceiling refusal above is vacuous: a tool that
    refused ``01-Fragments`` at every ceiling would pass it while making the
    operator's own safety pass over their own vault impossible. The scan
    ranks an unranked target as intimate, so ``tier_allowed(INTIMATE, …)``
    decides — which is exactly ``intimate`` and ``all``, the two ceilings a
    remote consumer token can never send (#1082).
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_FRAGMENTS_ROOT,
        privacy_tier_ceiling=ceiling,
    )

    assert result["status"] == "ok"
    assert result["statistics"]["total_findings"] >= 1
    assert result["input_path"] == _FRAGMENTS_ROOT


def test_scan_at_the_open_ceiling_returns_no_intimate_filename_canary(
    scannable_vault: Path,
) -> None:
    """The issue's acceptance criterion, probed at the tool boundary.

    Asserted against ``json.dumps(result, default=str)`` rather than against
    ``findings[].file_path``, because #972 reproduced in **two** fields that
    disagreed with each other: ``findings`` rendered a vault-relative path
    and ``report_markdown`` rendered an absolute one. A key-by-key check
    would have covered whichever field the author happened to think of, and
    ``report_markdown`` is the field CrawDad posts into a Discord channel.

    The canary lives in the fragment's file stem and nowhere else (see
    :func:`scannable_vault`), so a hit here can only mean the tool named a
    file the caller was not admitted to.
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_FRAGMENTS_ROOT,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    serialised = json.dumps(result, default=str)
    assert _FILENAME_CANARY not in serialised, (
        "creek.redact.scan returned an intimate fragment's filename to an "
        "open-ceiling caller. Creek filenames are slugified titles, so this "
        f"is the title.\n\n{serialised}"
    )


@pytest.mark.parametrize(("input_path", "ceiling"), _ADMITTED_SCANS)
def test_report_markdown_paths_are_vault_relative_at_every_admitted_ceiling(
    scannable_vault: Path,
    input_path: str,
    ceiling: TierCeiling,
) -> None:
    """``report_markdown`` renders no absolute path and no vault root.

    ``_finding_to_dict``'s docstring has always claimed paths are "rendered
    relative to the vault root so the Discord reply does not leak absolute
    filesystem paths". ``report_markdown`` is built by
    ``generate_markdown_summary`` and never went through that function, so
    the claim was true of one field and false of the other — and it is the
    false one CrawDad posts verbatim into a channel.

    Both directions are asserted because they fail independently: a heading
    can be non-absolute and still contain the vault root (a relative path
    built from the wrong base), and the vault root can be absent from a
    heading that is still absolute (a differently-rooted temp path). The
    non-empty check keeps the whole test from passing over a summary with no
    findings in it.
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=input_path,
        privacy_tier_ceiling=ceiling,
        consumer="crawdad",
    )
    report = result["report_markdown"]
    headings = _markdown_headings(report)

    assert headings, (
        f"the {input_path!r} scan at ceiling={ceiling.value!r} rendered no "
        f"file sections, so this test asserts nothing.\n\n{report}"
    )
    absolute = [heading for heading in headings if Path(heading).is_absolute()]
    assert absolute == [], (
        "report_markdown renders absolute filesystem paths, which CrawDad "
        "posts into a Discord channel verbatim — leaking the operator's home "
        f"directory along with the filename.\n\n{absolute}"
    )
    assert str(scannable_vault.resolve()) not in report, (
        "report_markdown embeds the vault root. Every path in the response "
        "must go through _vault_relative, which renders the path as scanned "
        f"and relative to the vault.\n\n{report}"
    )


@pytest.mark.parametrize(("input_path", "ceiling"), _ADMITTED_SCANS)
def test_findings_and_report_markdown_render_the_same_paths(
    scannable_vault: Path,
    input_path: str,
    ceiling: TierCeiling,
) -> None:
    """One relativiser owns every path in the response, so the two fields agree.

    The invariant, rather than a second copy of the previous test's
    assertions: ``findings[].file_path`` and the ``### `…``` headings are two
    renderings of the same set of files, so they must be the same set of
    strings. Today they are not — one is vault-relative and the other
    absolute — which is the #846/#848 signature of a guardrail applied on one
    path out of two.

    Set equality is what makes this survive the fix rather than merely
    describing it: any future field that renders paths differently from its
    sibling fails here, without this test knowing anything about how either
    one is built.
    """
    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=input_path,
        privacy_tier_ceiling=ceiling,
        consumer="crawdad",
    )
    finding_paths = {str(finding["file_path"]) for finding in result["findings"]}
    heading_paths = set(_markdown_headings(result["report_markdown"]))

    assert finding_paths, (
        f"the {input_path!r} scan at ceiling={ceiling.value!r} produced no "
        "findings, so this test asserts nothing"
    )
    assert finding_paths == heading_paths, (
        "findings[].file_path and report_markdown's headings name the same "
        "files in two different renderings, so a reviewer checking one "
        "learns nothing about the other.\n\n"
        f"findings: {sorted(finding_paths)}\nheadings: {sorted(heading_paths)}"
    )


def test_a_symlinked_child_renders_as_its_staged_path_not_its_target(
    scannable_vault: Path,
) -> None:
    """A staged symlink is reported as the staged path it was scanned as.

    ``_finding_to_dict`` calls ``match.file_path.resolve()`` before
    relativising. For a symlinked child of the staging dir that resolves the
    link, so a scan confined to ``00-Creek-Meta/Inbound`` reports
    ``01-Fragments/Notes/<slugified intimate title>.md`` — the exact
    disclosure the scope gate above exists to prevent, arriving through a
    scan the gate correctly admitted. The fix is that the relativiser renders
    the path **as scanned** and never resolves it.

    Deliberately *not* asserted: that ``sneaky.md`` appears in the response.
    #1087 may stop scanning symlinked children altogether, and this test must
    survive that decision either way; the positive control for "the staging
    subtree is still scanned" is
    ``test_scan_admits_the_staging_subtree_at_every_ceiling``.
    """
    staged = scannable_vault / "00-Creek-Meta" / "Inbound" / "ch1" / "sneaky.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.symlink_to(
        scannable_vault / _FRAGMENTS_ROOT / "Notes" / f"my-{_FILENAME_CANARY}-note.md",
    )

    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_STAGING_ROOT,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    serialised = json.dumps(result, default=str)
    assert _FILENAME_CANARY not in serialised, (
        "a scan confined to the staging subtree reported a symlink's target "
        "under 01-Fragments, disclosing an intimate fragment's slugified "
        f"title at ceiling=open.\n\n{serialised}"
    )
    assert str(scannable_vault.resolve()) not in serialised, (
        f"the response embeds the vault root.\n\n{serialised}"
    )


def test_an_unrenderable_path_falls_back_to_a_non_disclosing_placeholder(
    tmp_path: Path,
) -> None:
    """The relativiser fails closed on a path it cannot express vault-relative.

    Rendering is the last thing that happens to a path, so its fallback arm
    is where a "shouldn't happen" case turns into the disclosure the whole
    gate was built to stop. ``_finding_to_dict``'s current fallback is
    ``str(match.file_path)`` — the absolute path, in full, precisely when
    something unexpected happened.

    Asserted twice: the exact placeholder constant, and the absence of every
    component of the input. The second is the one that matters, because a
    placeholder that helpfully appended the offending filename would satisfy
    the first if the constant were written to include it. Every component of
    the probe path is sentinel-shaped so that a plausible English placeholder
    cannot collide with one by accident.
    """
    outside = Path(f"/{_OUTSIDE_CANARY}-dir/{_OUTSIDE_CANARY}-secret-thing.md")

    rendered = _vault_relative(outside, tmp_path)

    assert rendered == _OUTSIDE_VAULT_PLACEHOLDER
    for part in outside.parts[1:]:
        assert part not in rendered, (
            f"the fallback placeholder discloses {part!r} from a path it "
            f"could not render: {rendered!r}"
        )


# ---------------------------------------------------------------------------
# The walk does not follow an escaping symlink (#1087)
#
# #972 stopped the response from *naming* a symlink's target; the residual it
# recorded was that the walk still *read* it. ``scan_batch`` selects candidates
# with ``child.is_file()``, and ``is_file()`` follows symlinks, so the target's
# PII types, line numbers, and existence still reached this tool's caller.
#
# #1087 closed it in ``_scannable_candidates``, the scanner module's one
# filesystem walk: a child that is itself a symlink must resolve under the
# already-resolved scan root or it is declined unopened. The test below pins
# that from the MCP surface — the shared locator means it holds for
# ``creek redact`` and ``creek process`` too, but each surface asserts its own
# statistics contract, and ``files_scanned`` is this one's.
# ---------------------------------------------------------------------------

_STAGED_PII_RELPATH = f"{_STAGING_ROOT}/ch1/msg/staged.md"
"""The one PII-bearing file :func:`scannable_vault` seeds inside the staging dir.

Written down once because two assertions have to agree about it: the set of
files a confined scan may report, and the number of files it may have opened.
"""


@pytest.mark.parametrize("ceiling", list(TierCeiling))
def test_scan_does_not_read_a_symlink_escaping_the_scan_root(
    scannable_vault: Path,
    ceiling: TierCeiling,
) -> None:
    """A staged symlink's out-of-root target is not opened, reported or counted.

    The scan below is one the scope gate *correctly admits*: the staging
    subtree is admitted at every ceiling (FEAT-027), and the caller asked for
    nothing else. What escapes is the walk. A link parked at
    ``00-Creek-Meta/Inbound/ch1/sneaky.md`` pointing at an intimate fragment
    is opened, its findings are attributed to the staged path, and it adds one
    to ``files_scanned`` — which is an existence-and-readability bit about a
    file the caller was refused, the property named in
    ``creek_mcp/read_gate.py``.

    Parametrised over every ceiling because the acceptance criterion is "at
    any ceiling": containment at the walk is a property of *where the scan is
    pointed*, never of what the caller declared, so an implementation that
    only held at ``open`` would be a second ranking rule in disguise.

    The **behavioural** assertion is ``files_scanned == 1``; today it is ``2``.
    The finding-set equality is the second behavioural assertion and doubles as
    the non-vacuity guard: the in-root control (:data:`_STAGED_PII_RELPATH`) is
    legitimately scanned and legitimately reports PII, so "scanned nothing" and
    "refused everything" both fail here rather than passing quietly. Neither
    ``findings == []`` nor ``files_scanned == 0`` would be true after a correct
    fix.

    The canary assertion already passes today — #972 stopped
    :func:`~creek_mcp.tools.redact._vault_relative` resolving the link before
    rendering it — and is kept as a non-regression pin, so a fix that skipped
    the file while restoring the resolving renderer could not go green.

    Args:
        scannable_vault: Used exactly as-is; this test adds only the link, so
            the fixture's own staged file remains the positive control.
        ceiling: The caller's declared tier ceiling.
    """
    staged = scannable_vault / "00-Creek-Meta" / "Inbound" / "ch1" / "sneaky.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.symlink_to(
        scannable_vault / _FRAGMENTS_ROOT / "Notes" / f"my-{_FILENAME_CANARY}-note.md",
    )

    result = redact_scan_tool(
        vault_path=scannable_vault,
        input_path=_STAGING_ROOT,
        privacy_tier_ceiling=ceiling,
        consumer="crawdad",
    )

    assert result["status"] == "ok", (
        f"the staging subtree must stay admitted at ceiling={ceiling.value!r} "
        "(FEAT-027) or there is no scan for the rest of this test to measure."
        f"\n\n{result}"
    )
    assert result["statistics"]["files_scanned"] == 1, (
        "creek.redact.scan opened a symlinked child whose target resolves "
        "outside the scanned root and counted it, so files_scanned carries an "
        "existence-and-readability bit about an out-of-scope file.\n\n"
        f"statistics: {result['statistics']}"
    )
    finding_paths = {str(finding["file_path"]) for finding in result["findings"]}
    assert finding_paths == {_STAGED_PII_RELPATH}, (
        "a scan confined to the staging subtree must report exactly the "
        "in-root file it was pointed at: anything extra was read through the "
        "symlink, and anything missing means the walk stopped scanning the "
        f"subtree the tool exists for.\n\nfindings: {sorted(finding_paths)}"
    )
    serialised = json.dumps(result, default=str)
    assert _FILENAME_CANARY not in serialised, (
        "the response names the symlink target's intimate fragment — and a "
        f"Creek filename is a slugified title — at ceiling={ceiling.value!r}."
        f"\n\n{serialised}"
    )


def test_scan_accepts_a_vault_root_with_a_symlinked_component(vault: Path) -> None:
    """A vault reached through a symlink is scanned, not crashed on.

    ``redact_scan_tool`` renders ``input_path`` with
    ``resolved.relative_to(vault_path)`` where ``resolved`` has been resolved
    and ``vault_path`` has not, so any vault root containing a symlinked
    component raises an unhandled ``ValueError`` straight out of the MCP
    boundary. This is not exotic: ``/tmp`` is a symlink to ``/private/tmp``
    on macOS, and a vault under a synced or home-relative link hits it too.

    Constructed explicitly rather than relying on the platform, so the test
    means the same thing on every CI runner. The ``vault`` fixture is taken
    only for its stubbed ``load_config``; the root actually passed to the
    tool is the symlink.
    """
    real = vault / "real-vault"
    _write_pii_file(real / "00-Creek-Meta" / "Inbound" / "ch1" / "staged.md")
    linked = vault / "linked-vault"
    linked.symlink_to(real, target_is_directory=True)

    result = redact_scan_tool(
        vault_path=linked,
        input_path=_STAGING_ROOT,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result["status"] == "ok"
    assert result["input_path"] == _STAGING_ROOT
    assert result["statistics"]["total_findings"] >= 1
