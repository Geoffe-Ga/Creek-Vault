"""``creek.classify.entry`` — the per-entry classification read (#874).

The tool answers one question: what classification does *this* fragment
currently carry on disk? It computes nothing. That is the whole design, and
the first test in this module is why.

**The issue's headline design was empty, and this module proves it
executably.** #874 asked for classification to be returned *inline* on
``creek.journal``. But ``run_ingest`` never runs the frequency/phase
classifier — the only ``creek.classify`` symbol the ingest pipeline touches is
``privacy_pass.escalate`` — so an inline field would have emitted
``{frequency: "unclassified", phase: "unclassified", privacy_tier: <the tier
the caller itself just sent>}`` on every call, forever, with every one of its
tests passing. A constant in disguise.
``test_a_journal_ingested_fragment_carries_no_classification_on_disk``
characterises that pre-state independently of the tool, which is what makes
the ``unclassified`` assertions further down non-circular, and it is why the
*classified*-fragment test is the load-bearing one: only that test is red for
a reason a constant-returning implementation cannot satisfy.

The gate story is ``creek.state.read``'s, not ``creek.reflect``'s: the target
is caller-*addressed* and singular, so the tool **refuses** rather than
excluding, on the shared ``refuse_above_ceiling`` primitive with the generic
reason. See the module docstring of ``creek_mcp.tools.classify_entry``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.policy import Transport
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON
from creek_mcp.server import build_server
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.classify_entry import TOOL_NAME, entry_classification_tool
from creek_mcp.tools.journal import journal_ingest_tool

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-06-20T10:00:00+00:00"

# Trips FREQUENCY_SIGNALS[F1] and WAVELENGTH_PHASE_SIGNALS[RISING] hard enough
# to clear RuleClassifier.PRIMARY_THRESHOLD on both axes. Verified by
# _seed_classified_fragment's own assertions rather than assumed: a fixture
# matching no keyword yields ``unclassified`` legitimately and would take the
# whole non-vacuity guarantee with it.
_CLASSIFIABLE = (
    "Survival and safety were the whole question: the threat was real, the "
    "danger close, and my instinct for shelter took over. Something is "
    "emerging now, building and growing, an awakening momentum I can feel "
    "gathering."
)

_UNCLASSIFIABLE = "Ordinary note. Nothing here matches any signal dictionary."

# Shortest rendering of a planted frontmatter value that cannot plausibly
# collide with a hex fragment id, so the echo sweep stays strict rather than
# flaky. Values below it are covered by the clamp assertion alone.
_DISTINCTIVE_ECHO_CHARS = 8


def _vault(tmp_path: Path) -> Path:
    """Create the minimum vault layout the writer, ledger and audit need."""
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _ingest(
    vault: Path,
    *,
    content: str,
    external_id: str,
    tier: str = "open",
) -> str:
    """Write one journal entry into *vault* and return its fragment id."""
    result = journal_ingest_tool(
        vault_path=vault,
        content=content,
        external_id=external_id,
        timestamp=_TS,
        tier=tier,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok", result
    fragment_id = result["fragment_id"]
    assert isinstance(fragment_id, str)
    return fragment_id


def _fragment_path(vault: Path, fragment_id: str) -> Path:
    """Return the on-disk path of the fragment carrying *fragment_id*."""
    for path in sorted((vault / "01-Fragments").rglob("*.md")):
        if frontmatter.load(path).metadata.get("id") == fragment_id:
            return path
    msg = f"no fragment file carries id {fragment_id!r}"
    raise AssertionError(msg)


def _audit_lines(vault: Path) -> list[dict[str, object]]:
    """Return every parsed MCP audit entry written under *vault*."""
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_a_journal_ingested_fragment_carries_no_classification_on_disk(
    tmp_path: Path,
) -> None:
    """Ingest does not classify: the pre-state is unclassified and unstamped.

    Green at HEAD and green after this change — it characterises
    ``run_ingest``, not the new tool. Load-bearing three times over: it is the
    executable proof that #874's inline-on-journal design was a constant; it
    makes the tool's ``unclassified`` assertions non-circular by pinning the
    pre-state independently; and it pins the ``classification_method``
    *absence* case (the key is missing, not present-and-empty), which is the
    input to the clamp's ``"none"`` branch.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="char-1")

    metadata = frontmatter.load(_fragment_path(vault, fragment_id)).metadata

    assert metadata["frequency"]["primary"] == "unclassified"
    assert metadata["wavelength"]["phase"] == "unclassified"
    assert "classification_method" not in metadata


def _seed_classified_fragment(vault: Path, *, external_id: str = "cls-1") -> str:
    """Ingest a rule-classifiable entry, run the rules pass, return its id.

    The two asserts are the non-vacuity guard for AC5: they prove *on disk*
    that this fixture really trips ``RuleClassifier`` on both axes. Without
    them a fixture matching no keyword would yield ``unclassified``
    legitimately and the "reports its real frequency" test would pass against
    a constant-returning implementation.
    """
    fragment_id = _ingest(vault, content=_CLASSIFIABLE, external_id=external_id)
    summary = classify_tool(
        vault_path=vault,
        method="rules",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert summary["status"] == "ok", summary
    metadata = frontmatter.load(_fragment_path(vault, fragment_id)).metadata
    assert metadata["frequency"]["primary"] != "unclassified", metadata
    assert metadata["wavelength"]["phase"] != "unclassified", metadata
    return fragment_id


def test_classify_entry_reports_the_persisted_classification(tmp_path: Path) -> None:
    """A rules-classified fragment reports its real frequency and phase.

    The load-bearing test. ``!=`` rather than ``== "F1"`` on purpose: the
    assertion has to be one a constant-returning mutant cannot satisfy, and
    pinning the exact rule verdict would couple this module to the signal
    dictionaries instead.
    """
    vault = _vault(tmp_path)
    fragment_id = _seed_classified_fragment(vault)

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert set(result) == {
        "status",
        "tool",
        "tier_ceiling",
        "entry_ref",
        "frequency",
        "phase",
        "privacy_tier",
        "classification_method",
    }
    assert result["status"] == "ok"
    assert result["tool"] == "creek.classify.entry"
    assert result["tier_ceiling"] == "personal"
    assert result["entry_ref"] == fragment_id
    assert result["frequency"] != "unclassified"
    assert result["phase"] != "unclassified"
    assert result["privacy_tier"] == "open"
    assert result["classification_method"] == "rules"


def test_the_gate_refuses_a_fragment_above_the_ceiling(tmp_path: Path) -> None:
    """An intimate fragment probed at ceiling=open earns the generic refusal.

    Exactly the canonical four keys, and the reason is *imported* rather than
    retyped so a reworded constant moves this test with it.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(
        vault, content=_CLASSIFIABLE, external_id="gate-1", tier="intimate"
    )

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert result == {
        "status": "refused",
        "tool": TOOL_NAME,
        "tier_ceiling": "open",
        "reason": GENERIC_ABOVE_CEILING_REASON,
    }


def test_creek_classify_entry_is_registered(tmp_path: Path) -> None:
    """The tool reaches the live MCP surface under its published name.

    Registration is what makes ``creek.handshake`` advertise the capability —
    the list is ``server.list_tools()`` rather than a hardcoded set — so this
    is also what discharges the handshake criterion.
    """
    server = build_server(
        transport=Transport.STDIO,
        vault_path=_vault(tmp_path),
        draft_llm_factory=lambda _tier: lambda _prompt: "ignored",
    )
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert TOOL_NAME in names


def _rewrite_metadata(path: Path, **changes: object) -> None:
    """Rewrite *path*'s frontmatter in place; a ``None`` value deletes the key.

    Hand-editing is the point for two of its callers: a fragment with **no**
    ``privacy_tier`` key at all and a fragment carrying a garbage
    ``classification_method`` are both states the pipeline never writes and a
    hand-edited or legacy vault certainly does.
    """
    post = frontmatter.load(path)
    for key, value in changes.items():
        if value is None:
            post.metadata.pop(key, None)
        else:
            post.metadata[key] = value
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _fragment_digests(vault: Path) -> dict[str, tuple[int, str]]:
    """Return ``{relative path: (mtime_ns, sha256)}`` for every fragment file.

    A mapping rather than a list so a file that appeared, vanished or moved
    shows up as a changed key rather than as an off-by-one.
    """
    root = vault / "01-Fragments"
    return {
        str(path.relative_to(root)): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*.md"))
    }


def _tool_audit_lines(vault: Path) -> list[dict[str, object]]:
    """Return only the audit entries this tool wrote."""
    return [line for line in _audit_lines(vault) if line.get("tool") == TOOL_NAME]


def _tool_audit_raw(vault: Path) -> str:
    """Return the raw on-disk bytes of the lines this tool wrote.

    Scoped to this tool's own lines rather than the whole log, because the log
    is shared: ``creek.journal`` is a *write* verb and records
    ``affected_fragment_ids`` by contract, so a whole-file sweep would fail on
    another tool's legitimate record and prove nothing about this one.
    """
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return ""
    return "\n".join(
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("tool") == TOOL_NAME
    )


def test_an_unclassified_fragment_reports_the_literal_unclassified_strings(
    tmp_path: Path,
) -> None:
    """Never null, never omitted, never invented — and never a mutant's constant.

    Non-circular because
    ``test_a_journal_ingested_fragment_carries_no_classification_on_disk``
    pins the same pre-state on disk, independently of this tool.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="unc-1")

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert result["status"] == "ok"
    assert result["frequency"] == "unclassified"
    assert result["phase"] == "unclassified"
    assert result["classification_method"] == "none"


def test_the_method_sentinel_distinguishes_no_pass_from_a_declined_pass(
    tmp_path: Path,
) -> None:
    """The whole purpose of ``classification_method``, asserted as a contrast.

    The stamp is written *unconditionally* on any classify write, so a fragment
    the rules genuinely cannot place still comes back stamped ``rules`` — and
    that is what makes ``none`` mean something. Reusing ``"unclassified"`` as
    the sentinel would collapse the two rows of this test into one.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="sent-1")

    before = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert before["classification_method"] == "none"

    classify_tool(
        vault_path=vault, method="rules", privacy_tier_ceiling=TierCeiling.ALL
    )
    after = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    # A pass ran and still could not place it: the verdict is unchanged, the
    # provenance is not. Exactly the distinction the sentinel exists to draw.
    assert after["frequency"] == "unclassified"
    assert after["classification_method"] == "rules"


@pytest.mark.parametrize(
    "planted",
    [
        "not-a-method",
        "<script>alert(1)</script>",
        "rules; DROP TABLE fragments",
        "x" * 4096,
        "",
        "RULES",
        42,
        {"method": "rules"},
    ],
)
def test_a_hand_edited_classification_method_is_clamped_to_none(
    tmp_path: Path,
    planted: object,
) -> None:
    """Raw frontmatter never reaches the wire unclamped.

    This is a **safety** control, not validation politeness: the frontmatter is
    arbitrary user-controlled bytes, and echoing it would put an unbounded
    content channel on the Adepthood wire out of a file the caller may be
    admitted to only at rank level. The 4096-character case is that channel
    made concrete; the ``"RULES"`` case pins that the clamp is exact rather
    than case-folded; the non-string cases pin that a YAML integer or mapping
    does not crash the read.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="clamp-1")
    _rewrite_metadata(_fragment_path(vault, fragment_id), classification_method=planted)

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert result["classification_method"] == "none"
    # The clamp's whole job: the planted bytes reach no published field, not
    # just the one that would have carried them. The echo sweep runs only for
    # renderings long enough to be distinctive -- ``""`` is a substring of
    # every string and ``42`` could collide with a hex fragment id, which would
    # make this assertion flaky rather than strict. Those two cases are covered
    # by the clamp assertion above, which is the binding one.
    rendered = str(planted)
    if len(rendered) >= _DISTINCTIVE_ECHO_CHARS:
        assert rendered not in json.dumps(result, default=str)


def test_a_blank_entry_ref_is_refused_without_an_audit_append(
    tmp_path: Path,
) -> None:
    """A malformed call earns a specific reason and leaves no trail.

    Matches ``creek.journal``'s ``_validated_entry_tier`` convention: there is
    no ceiling-versus-content event to record, because nothing was read. The
    reason is derived entirely from the caller's own input, so it is no oracle.
    """
    vault = _vault(tmp_path)

    for blank in ("", "   ", "\t\n"):
        result = entry_classification_tool(
            vault_path=vault,
            entry_ref=blank,
            privacy_tier_ceiling=TierCeiling.ALL,
        )
        assert result == {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": "all",
            "reason": "entry_ref must not be blank",
        }

    assert not _tool_audit_lines(vault)


def test_an_unresolvable_entry_ref_fails_closed_below_the_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """An id that resolves to nothing is refused, not answered.

    ``max_source_tier`` over an empty survey yields ``INTIMATE``, so this is
    the branch that makes every locator divergence fail safe — and it is why
    the not-found reason is unreachable below ``ceiling=intimate``, which in
    turn is why the existence oracle does not exist at the ordinary ceilings.
    """
    vault = _vault(tmp_path)
    _ingest(vault, content=_UNCLASSIFIABLE, external_id="present-1")

    for ceiling in (TierCeiling.OPEN, TierCeiling.PERSONAL):
        result = entry_classification_tool(
            vault_path=vault,
            entry_ref="frag-does-not-exist",
            privacy_tier_ceiling=ceiling,
        )
        assert result == {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": ceiling.value,
            "reason": GENERIC_ABOVE_CEILING_REASON,
        }


def test_an_unresolvable_entry_ref_is_not_found_at_the_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """Verbatim ``creek.reflect``'s spelling — one vocabulary across the surface."""
    vault = _vault(tmp_path)

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref="frag-does-not-exist",
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )

    assert result == {
        "status": "refused",
        "tool": TOOL_NAME,
        "tier_ceiling": "intimate",
        "reason": "entry_ref not found",
    }


def test_a_fragment_with_no_privacy_tier_key_fails_closed_to_intimate(
    tmp_path: Path,
) -> None:
    """A *missing* key ranks INTIMATE, distinctly from an explicit unclassified.

    This is #1033, and it is the whole reason the gate goes through
    ``source_tiers``/``fragment_tier`` rather than reading
    ``fragment.privacy_tier`` off the validated model — which defaults to
    ``unclassified`` (rank 1) and therefore fails **open** at
    ``ceiling=personal``. The contrast below is what makes that non-vacuous:
    the same fragment, differing only in whether the key is present, must be
    refused in one case and admitted in the other at the identical ceiling.
    """
    vault = _vault(tmp_path)
    missing_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="nokey-1")
    _rewrite_metadata(_fragment_path(vault, missing_id), privacy_tier=None)

    explicit_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="explicit-1")
    _rewrite_metadata(_fragment_path(vault, explicit_id), privacy_tier="unclassified")

    missing = entry_classification_tool(
        vault_path=vault,
        entry_ref=missing_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    explicit = entry_classification_tool(
        vault_path=vault,
        entry_ref=explicit_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert missing["status"] == "refused"
    assert missing["reason"] == GENERIC_ABOVE_CEILING_REASON
    assert explicit["status"] == "ok"
    assert explicit["privacy_tier"] == "unclassified"

    # And the missing-key fragment IS reachable at intimate, reporting the
    # fail-closed reading rather than the model's open-failing default.
    at_intimate = entry_classification_tool(
        vault_path=vault,
        entry_ref=missing_id,
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert at_intimate["status"] == "ok"
    assert at_intimate["privacy_tier"] == "intimate"


# The exhaustive ceiling matrix: 3 declared ceilings x 4 persisted fragment
# tiers = 12 cells, no holes, boring cells included. ``admitted`` is derived
# from creek_mcp.tier_ceiling._TIER_RANK's published policy, written out cell
# by cell rather than computed, so a mutation to that table cannot silently
# move the expectation along with the behaviour.
#
# The two cells that carry #961 are called out by name below: ``unclassified``
# ranks with ``personal``, so it is ADMITTED at ceiling=personal and REFUSED at
# ceiling=open. Both directions are asserted -- a mutant ranking it with
# ``open`` would hand an open-ceiling consumer content nobody has classified,
# and would pass a one-directional test.
_CEILING_MATRIX = [
    ("open", "open", True),
    ("open", "personal", False),
    ("open", "intimate", False),
    ("open", "unclassified", False),
    ("personal", "open", True),
    ("personal", "personal", True),
    ("personal", "intimate", False),
    ("personal", "unclassified", True),
    ("intimate", "open", True),
    ("intimate", "personal", True),
    ("intimate", "intimate", True),
    ("intimate", "unclassified", True),
]

_CEILING_MATRIX_CELLS = 12


def test_the_ceiling_matrix_has_no_holes() -> None:
    """Every ceiling x tier pair is declared, and the list is non-empty.

    Guards the failure mode where narrowing the parametrisation makes the
    privacy tests vanish behind a green gate: an emptied list reports zero
    failures and zero skips, which reads exactly like success.
    """
    assert len(_CEILING_MATRIX) == _CEILING_MATRIX_CELLS
    ceilings = {ceiling for ceiling, _tier, _ok in _CEILING_MATRIX}
    tiers = {tier for _ceiling, tier, _ok in _CEILING_MATRIX}
    assert ceilings == {"open", "personal", "intimate"}
    assert tiers == {"open", "personal", "intimate", "unclassified"}
    assert len({(c, t) for c, t, _ in _CEILING_MATRIX}) == _CEILING_MATRIX_CELLS


@pytest.mark.parametrize(("ceiling", "tier", "admitted"), _CEILING_MATRIX)
def test_the_ceiling_gate_admits_exactly_the_declared_cells(
    tmp_path: Path,
    ceiling: str,
    tier: str,
    *,
    admitted: bool,
) -> None:
    """One cell of the 12-cell ceiling matrix, refusal side and success side.

    The refusal is asserted as an exact four-key payload, so an extra key
    "for debugging" -- which would be derived from content the caller is not
    admitted to -- fails here rather than shipping.
    """
    vault = _vault(tmp_path)
    # ``unclassified`` is not a tier ``creek.journal`` will accept on the wire,
    # so it is planted afterwards. That is not a synthetic state: it is exactly
    # what every pipeline-written, not-yet-classified fragment carries.
    write_tier = "open" if tier == "unclassified" else tier
    fragment_id = _ingest(
        vault,
        content=_UNCLASSIFIABLE,
        external_id=f"m-{ceiling}-{tier}",
        tier=write_tier,
    )
    if tier == "unclassified":
        _rewrite_metadata(_fragment_path(vault, fragment_id), privacy_tier=tier)

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling(ceiling),
    )

    if admitted:
        assert result["status"] == "ok"
        assert result["privacy_tier"] == tier
        assert result["tier_ceiling"] == ceiling
    else:
        assert result == {
            "status": "refused",
            "tool": TOOL_NAME,
            "tier_ceiling": ceiling,
            "reason": GENERIC_ABOVE_CEILING_REASON,
        }


def test_unclassified_ranks_with_personal_in_both_directions(
    tmp_path: Path,
) -> None:
    """#961 named, so the two cells that carry it cannot be edited by accident.

    The matrix above already covers both, but only as two anonymous rows. This
    states the policy out loud: an untiered fragment is content nobody has
    vouched for, so an ``open``-ceiling caller -- including a remote one -- must
    not reach it, while a ``personal`` caller may.
    """
    vault = _vault(tmp_path)
    fragment_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="rank-1")
    _rewrite_metadata(_fragment_path(vault, fragment_id), privacy_tier="unclassified")

    refused = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    admitted = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert refused["status"] == "refused"
    assert admitted["status"] == "ok"
    assert admitted["privacy_tier"] == "unclassified"


def test_the_audit_trail_records_a_probe_without_naming_its_target(
    tmp_path: Path,
) -> None:
    """Exactly one append per call past the blank check, and no id in the bytes.

    Asserted against the **bytes on disk**, not the response, because the trail
    is what is served onward through other surfaces. ``args`` carries
    ``has_entry_ref`` rather than the id, and that rule has a mechanism:
    ``summarise_args`` passes any string of at most 64 characters through
    verbatim, and a ``frag-`` id is about 17 -- so naming it would write every
    probed target into ``00-Creek-Meta/audit/mcp.jsonl``. A probing consumer
    must show up as a rate, never as a named target.
    """
    vault = _vault(tmp_path)
    intimate_id = _ingest(
        vault, content=_UNCLASSIFIABLE, external_id="aud-1", tier="intimate"
    )
    open_id = _ingest(vault, content=_UNCLASSIFIABLE, external_id="aud-2")

    # Path 1 -- blank: no append.
    entry_classification_tool(
        vault_path=vault, entry_ref="  ", privacy_tier_ceiling=TierCeiling.ALL
    )
    assert not _tool_audit_lines(vault)

    # Path 2 -- not found: exactly one.
    entry_classification_tool(
        vault_path=vault,
        entry_ref="frag-nope",
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert len(_tool_audit_lines(vault)) == 1

    # Path 3 -- above ceiling: exactly one more.
    entry_classification_tool(
        vault_path=vault,
        entry_ref=intimate_id,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert len(_tool_audit_lines(vault)) == 2

    # Path 4 -- ok: exactly one more.
    entry_classification_tool(
        vault_path=vault,
        entry_ref=open_id,
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="adepthood",
    )
    lines = _tool_audit_lines(vault)
    assert len(lines) == 3

    for line in lines:
        assert line["args_summary"] == {"has_entry_ref": True}
        assert line["affected_fragment_ids"] == []
    assert lines[-1]["consumer"] == "adepthood"
    assert lines[-1]["tier_ceiling"] == "open"

    # The bytes themselves, not just the parsed view: no probed id anywhere,
    # and the two ids are indistinguishable from each other in the trail. The
    # refusal outcome is absent too -- the trail records the attempt, never
    # whether it succeeded.
    raw = _tool_audit_raw(vault)
    assert raw
    assert intimate_id not in raw
    assert open_id not in raw
    assert "frag-nope" not in raw
    assert "refused" not in raw


def test_the_tool_builds_no_llm_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure read constructs no provider, so no ceiling can be walked around.

    **Both** entry points are patched. ``creek.classify.llm`` re-exports
    ``build_provider``, so a module that imported the name directly would sail
    past a patch of only the package-level one, and vice versa; patching one is
    not enough.

    The structural half of this guarantee is already free and stronger: layer
    (g) of ``tests/test_mcp_read_gate.py`` derives its LLM-backed set **by
    signature** -- every GATED tool whose module defines a function taking both
    ``llm_factory`` and ``privacy_tier_ceiling`` -- so this tool is out of that
    set by construction and cannot silently become model-backed. What this test
    adds is the deferred-import case a signature cannot see.
    """

    def _explode(*_args: object, **_kwargs: object) -> object:
        msg = "creek.classify.entry must construct no LLM provider"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "creek.classify.llm.providers.build_provider", _explode, raising=True
    )
    monkeypatch.setattr("creek.classify.llm.build_provider", _explode, raising=True)

    vault = _vault(tmp_path)
    fragment_id = _seed_classified_fragment(vault, external_id="noegress-1")

    result = entry_classification_tool(
        vault_path=vault,
        entry_ref=fragment_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert result["status"] == "ok"


def test_the_tool_writes_nothing_under_01_fragments(tmp_path: Path) -> None:
    """Read-only on the success path **and** on the refusal path.

    Path, mtime and content digest, so a rewrite that happened to produce
    byte-identical output would still show up as a changed mtime, and a file
    that appeared or moved shows up as a changed key.
    """
    vault = _vault(tmp_path)
    open_id = _seed_classified_fragment(vault, external_id="ro-open")
    intimate_id = _ingest(
        vault, content=_CLASSIFIABLE, external_id="ro-int", tier="intimate"
    )

    before = _fragment_digests(vault)
    assert len(before) == 2

    ok = entry_classification_tool(
        vault_path=vault,
        entry_ref=open_id,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert ok["status"] == "ok"
    assert _fragment_digests(vault) == before

    refused = entry_classification_tool(
        vault_path=vault,
        entry_ref=intimate_id,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert refused["status"] == "refused"
    assert _fragment_digests(vault) == before
