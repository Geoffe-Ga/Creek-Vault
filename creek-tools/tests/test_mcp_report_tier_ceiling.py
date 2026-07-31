"""``creek.report`` must honour the caller's tier ceiling in what it *writes* (#968).

``report_tool`` accepts a ``privacy_tier_ceiling``, audits it and echoes it, but
at the time these tests were written it never converted it and never threaded
it into any of the six generators it fans out to. The consequence is not a
read-side leak — the tool returns only ``report_paths``, never content — it is
**above-ceiling content distilled into an unlabelled vault artifact** that every
later reader treats as ordinary vault material.

That shapes every assertion here: *nothing* in this module asserts on
``report_tool``'s return value as evidence of exclusion. A test that did would
have passed against the unfixed code and proved nothing. The evidence is always
the bytes of the file the call wrote.

Two properties of the existing generators had to be designed around, and both
are the difference between a real test and a vacuous one:

1. ``voice``, ``lexicon`` and ``rhetorical-patterns`` already drop ``intimate``
   fragments via ``creek.generate.voice._eligible_register(allow_intimate=False)``.
   An *intimate* canary in those fixtures would have been excluded by the
   pre-existing filter and the test would have gone green without the ceiling
   being enforced at all. Their above-ceiling canary is therefore ``personal``.
2. ``rhetorical-patterns`` writes **no fragment-derived text** — three integer
   counts and nothing else — so a substring sweep over its artifact passes for
   free. Its gate is pinned by file-set (the above-ceiling fragment's register
   note must not exist) and by count identity across ceilings instead.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.models import PrivacyTier
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.report import report_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

    from creek.classify.privacy_filter import PrivacyTierOverride

runner = CliRunner()


# ---------------------------------------------------------------------------
# Canaries
#
# Plain sentinels rather than realistic prose: a leak cannot then be excused as
# "that phrase could have come from anywhere". Restricted to ``[A-Za-z0-9-]``
# because ``creek.generate.decisions._sanitize_title`` strips everything else
# out of the filename it derives from a fragment title.
# ---------------------------------------------------------------------------

_OPEN_CANARY = "CANARY-RPT-OPEN-3a71"
"""Below every ceiling under test — the mandatory positive control."""

_INTIMATE_CANARY = "CANARY-RPT-INTIMATE-8c4e"
"""Above ``ceiling=open`` for ``tags`` / ``decisions`` / ``mode-profiles``."""

_PERSONAL_CANARY = "CANARY-RPT-PERSONAL-1f95"
"""Above ``ceiling=open`` for the three voice-corpus reports (see module doc)."""

_NOTIER_CANARY = "CANARY-RPT-NOTIER-6b02"
"""Carried by a fragment whose front matter has no ``privacy_tier`` key at all."""


# Bodies for the voice-corpus fixture. ``sangha`` and ``dharma`` are members of
# ``creek.generate.lexicon.TRADITION_GLOSSARIES["buddhist"]``, and
# ``LexiconGenerator._build_borrowed_terms`` records the *whole surrounding
# sentence* verbatim with no occurrence threshold — the cheapest way to get a
# fragment's own prose copied into ``glossary.md`` byte for byte.
_OPEN_VOICE_BODY = (
    f"The sangha of {_OPEN_CANARY} gathers, and yet the room stays quiet."
)
_PERSONAL_VOICE_BODY = (
    f"The dharma of {_PERSONAL_CANARY} is quiet, but also loud. "
    "As I mentioned, it returns."
)

# The exact rhetorical-move tally ``_OPEN_VOICE_BODY`` produces: one paradox
# construction ("and yet") and nothing else. Pinned as values rather than as a
# bare "the two files match" so a gate that silently emptied the analytical
# register would fail here too.
_EXPECTED_ANALYTICAL_MOVES = {
    "Self-deprecation before insight": 0,
    "Paradox constructions": 1,
    "Callbacks to earlier points": 0,
}

_MOVE_LINE_RE = re.compile(r"^- (?P<label>[^:\n]+): (?P<count>\d+)\.$", re.MULTILINE)
"""Matches one ``- <label>: <n>.`` line of ``_format_rhetorical_moves`` output."""


# ---------------------------------------------------------------------------
# Vault-building helpers
#
# Kept in this file rather than conftest.py: every fixture here is shaped around
# one specific generator's admission conditions, and moving them to shared scope
# would invite a later edit to "simplify" one of those conditions away.
# ---------------------------------------------------------------------------


def _new_vault(root: Path, name: str) -> Path:
    """Create an empty vault skeleton under *root* and return its path.

    ``TagGardenGenerator.generate_garden`` writes ``00-Creek-Meta/Tag-Garden.md``
    without creating its parent directory, so the meta folder is part of the
    skeleton rather than something each caller remembers.

    Args:
        root: Directory the vault is created inside (typically ``tmp_path``).
        name: Vault directory name, so one test can build several vaults.

    Returns:
        The vault root.
    """
    vault = root / name
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    (vault / "01-Fragments" / "Notes").mkdir(parents=True, exist_ok=True)
    return vault


def _write_note(
    vault: Path,
    relpath: str,
    metadata: dict[str, Any],
    body: str,
) -> Path:
    """Write one markdown note with YAML front matter into the vault.

    Args:
        vault: Vault root.
        relpath: Vault-relative destination path.
        metadata: Front-matter mapping, written verbatim — a key omitted here
            is a key genuinely absent from the file, which is the distinction
            the fail-closed tier read depends on.
        body: Markdown body.

    Returns:
        The path written.
    """
    target = vault / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _fragment_metadata(
    *,
    frag_id: str,
    title: str,
    privacy_tier: str | None,
    tags: list[str] | None = None,
    mode: str | None = None,
    register: str | None = None,
    confidence: str = "settled",
) -> dict[str, Any]:
    """Build fragment front matter, omitting keys the caller did not ask for.

    Args:
        frag_id: Fragment id.
        title: Fragment title — the carrier for the ``decisions`` and
            ``mode-profiles`` canaries.
        privacy_tier: The declared tier, or ``None`` to omit the key entirely.
            ``None`` is not the same as ``"unclassified"``: the model defaults a
            missing key to ``unclassified``, and only the raw front matter can
            still tell the two apart.
        tags: Obsidian tags — the carrier for the ``tags`` canaries.
        mode: Wavelength engagement mode; set to make the fragment visible to
            ``mode-profiles``.
        register: Voice register; set to make the fragment exemplar-eligible.
        confidence: Voice confidence. Must be ``settled`` or ``conviction`` for
            ``_eligible_register`` to admit the fragment.

    Returns:
        A front-matter mapping ready for :func:`_write_note`.
    """
    meta: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F5", "secondary": []},
    }
    if privacy_tier is not None:
        meta["privacy_tier"] = privacy_tier
    if tags is not None:
        meta["tags"] = tags
    if mode is not None:
        meta["wavelength"] = {"phase": "rising", "mode": mode}
    if register is not None:
        meta["voice"] = {"voice_register": register, "confidence": confidence}
    return meta


def _build_tags_vault(root: Path) -> Path:
    """Seed one ``open`` and one ``intimate`` fragment, each with a tag canary.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "tags-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open.md",
        _fragment_metadata(
            frag_id="frag-open",
            title="Open note",
            privacy_tier="open",
            tags=[_OPEN_CANARY],
        ),
        "Open body.",
    )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-intimate.md",
        _fragment_metadata(
            frag_id="frag-intimate",
            title="Intimate note",
            privacy_tier="intimate",
            tags=[_INTIMATE_CANARY],
        ),
        "Intimate body.",
    )
    return vault


def _build_decisions_vault(root: Path) -> Path:
    """Seed two decision-signalling fragments, one ``open`` and one ``intimate``.

    Both titles open with ``"Should I"`` so ``DecisionDetector._detect_keywords``
    flags them; the canary rides in the title, which ends up verbatim in the
    generated note's filename *and* its ``title:`` front matter.
    ``08-Decisions/Active/`` is deliberately left absent so the idempotency skip
    in ``_existing_decision_fragment_ids`` cannot suppress either note.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "decisions-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open.md",
        _fragment_metadata(
            frag_id="frag-open",
            title=f"Should I keep {_OPEN_CANARY}",
            privacy_tier="open",
        ),
        "Open body.",
    )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-intimate.md",
        _fragment_metadata(
            frag_id="frag-intimate",
            title=f"Should I keep {_INTIMATE_CANARY}",
            privacy_tier="intimate",
        ),
        "Intimate body.",
    )
    return vault


def _build_mode_profiles_vault(root: Path) -> Path:
    """Seed two ``express``-mode fragments, one ``open`` and one ``intimate``.

    Both share the same mode on purpose: ``05-Wavelength/Mode-Profiles/express.md``
    is then written at *every* ceiling, so "the above-ceiling title is absent"
    cannot be satisfied by the note simply not existing.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "mode-profiles-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open.md",
        _fragment_metadata(
            frag_id="frag-open",
            title=f"Express note {_OPEN_CANARY}",
            privacy_tier="open",
            mode="express",
        ),
        "Open body.",
    )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-intimate.md",
        _fragment_metadata(
            frag_id="frag-intimate",
            title=f"Express note {_INTIMATE_CANARY}",
            privacy_tier="intimate",
            mode="express",
        ),
        "Intimate body.",
    )
    return vault


def _build_voice_vault(root: Path) -> Path:
    """Seed two exemplar-eligible fragments in *different* registers.

    The above-ceiling fragment is ``personal``, not ``intimate``: the voice
    corpus already excludes ``intimate`` regardless of the ceiling, so an
    intimate canary here would prove nothing (see the module docstring).

    The registers are segregated — ``open`` is ``analytical``, ``personal`` is
    ``confessional`` — so ``rhetorical-patterns``, which writes no
    fragment-derived text at all, can still be pinned by which per-register
    files exist.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "voice-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open-voice.md",
        _fragment_metadata(
            frag_id="frag-open-voice",
            title="Open exemplar",
            privacy_tier="open",
            register="analytical",
        ),
        _OPEN_VOICE_BODY,
    )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-personal-voice.md",
        _fragment_metadata(
            frag_id="frag-personal-voice",
            title="Personal exemplar",
            privacy_tier="personal",
            register="confessional",
        ),
        _PERSONAL_VOICE_BODY,
    )
    return vault


def _build_eddy_vault(root: Path) -> Path:
    """Seed an ``open`` fragment plus a tagged eddy note under ``03-Eddies``.

    ``TagGardenGenerator`` scans five directories, not one. An eddy carries no
    ``privacy_tier`` of its own but its tags are derived from its member
    fragments, so it is a second, independent route for the same leak.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "eddy-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open.md",
        _fragment_metadata(
            frag_id="frag-open",
            title="Open note",
            privacy_tier="open",
            tags=[_OPEN_CANARY],
        ),
        "Open body.",
    )
    _write_note(
        vault,
        "03-Eddies/eddy-canary.md",
        {
            "type": "eddy",
            "id": "eddy-canary",
            "title": "Canary eddy",
            "tags": [_INTIMATE_CANARY],
        },
        "Eddy body.",
    )
    return vault


def _build_untiered_vault(root: Path) -> Path:
    """Seed a fragment with **no** ``privacy_tier`` key beside an ``open`` one.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "untiered-vault")
    _write_note(
        vault,
        "01-Fragments/Notes/frag-open.md",
        _fragment_metadata(
            frag_id="frag-open",
            title="Open note",
            privacy_tier="open",
            tags=[_OPEN_CANARY],
        ),
        "Open body.",
    )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-notier.md",
        _fragment_metadata(
            frag_id="frag-notier",
            title="Untiered note",
            privacy_tier=None,
            tags=[_NOTIER_CANARY],
        ),
        "Untiered body.",
    )
    return vault


# ---------------------------------------------------------------------------
# Artifact inspection helpers
# ---------------------------------------------------------------------------


def _artifact_files(vault: Path, roots: tuple[str, ...]) -> list[Path]:
    """Return every file beneath the given vault-relative artifact *roots*.

    Args:
        vault: Vault root.
        roots: Vault-relative directories (or files) the report writes into.

    Returns:
        Sorted list of existing files. Absent roots contribute nothing, so a
        report that wrote no artifact yields an empty list rather than raising —
        the positive-control assertions are what catch that case.
    """
    out: list[Path] = []
    for root in roots:
        base = vault / root
        if not base.exists():
            continue
        if base.is_file():
            out.append(base)
            continue
        out.extend(sorted(p for p in base.rglob("*") if p.is_file()))
    return out


def _artifact_blob(vault: Path, roots: tuple[str, ...]) -> str:
    """Return every artifact's vault-relative path and text, concatenated.

    Paths are included alongside contents because one of the six reports —
    ``decisions`` — puts the fragment title verbatim into the *filename*. A
    contents-only sweep would miss it entirely.

    Args:
        vault: Vault root.
        roots: Vault-relative artifact roots.

    Returns:
        One string covering every artifact path and body.
    """
    parts: list[str] = []
    for path in _artifact_files(vault, roots):
        parts.append(str(path.relative_to(vault)))
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _snapshot(vault: Path) -> dict[Path, bytes]:
    """Return the full byte contents of every file in the vault.

    Bytes rather than ``(mtime_ns, size)``: a report that rewrites a file with
    same-length content inside one filesystem timestamp tick would look
    unchanged to a stat-based snapshot, and "unchanged" is exactly the state
    this snapshot is used to rule out.

    Args:
        vault: Vault root.

    Returns:
        Mapping of path to contents.
    """
    return {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}


def _changed_files(vault: Path, before: dict[Path, bytes]) -> list[Path]:
    """Return every file that is new or whose bytes differ from *before*.

    Args:
        vault: Vault root.
        before: A :func:`_snapshot` taken before the call under test.

    Returns:
        Sorted list of new-or-modified files.
    """
    return sorted(
        p for p in vault.rglob("*") if p.is_file() and before.get(p) != p.read_bytes()
    )


def _new_tags_section(garden: str) -> str:
    """Return the ``### New Tags`` block of a rendered Tag Garden.

    Args:
        garden: The full ``Tag-Garden.md`` text.

    Returns:
        The text between ``### New Tags`` and the next ``## `` heading, or the
        empty string when the section is absent.
    """
    marker = "### New Tags"
    start = garden.find(marker)
    if start == -1:
        return ""
    rest = garden[start + len(marker) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _move_counts(note: str) -> dict[str, int]:
    """Parse the ``### Rhetorical Moves`` counts out of a register note.

    Args:
        note: The full text of a ``07-Voice/Rhetorical-Patterns/<register>.md``.

    Returns:
        Mapping of move label to its integer count.
    """
    return {
        match.group("label"): int(match.group("count"))
        for match in _MOVE_LINE_RE.finditer(note)
    }


# ---------------------------------------------------------------------------
# The six report types, described once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReportCase:
    """One report type plus everything needed to judge what it wrote.

    Attributes:
        report_type: The ``report_type`` argument to ``report_tool``.
        build: Builds this report's fixture vault under a given root.
        above_canary: The sentinel carried by the above-ceiling fragment.
        below_canary: The sentinel carried by the below-ceiling fragment, or
            ``None`` for ``rhetorical-patterns``, whose artifact contains no
            fragment-derived text for a substring check to find.
        artifact_roots: Vault-relative roots this report writes into.
        positive_glob: A vault-relative glob that must match at least one file
            at ``ceiling=open`` — the proof the generator ran at all.
        forbidden_glob: A vault-relative glob that must match nothing at
            ``ceiling=open`` and something at ``ceiling=all``, or ``None`` when
            the report writes a single ceiling-independent file.
    """

    report_type: str
    build: Callable[[Path], Path]
    above_canary: str
    below_canary: str | None
    artifact_roots: tuple[str, ...]
    positive_glob: str
    forbidden_glob: str | None


_CASES: tuple[_ReportCase, ...] = (
    _ReportCase(
        report_type="tags",
        build=_build_tags_vault,
        above_canary=_INTIMATE_CANARY,
        below_canary=_OPEN_CANARY,
        artifact_roots=("00-Creek-Meta",),
        positive_glob="00-Creek-Meta/Tag-Garden.md",
        forbidden_glob=None,
    ),
    _ReportCase(
        report_type="decisions",
        build=_build_decisions_vault,
        above_canary=_INTIMATE_CANARY,
        below_canary=_OPEN_CANARY,
        artifact_roots=("08-Decisions",),
        positive_glob="08-Decisions/Active/*.md",
        forbidden_glob=None,
    ),
    _ReportCase(
        report_type="mode-profiles",
        build=_build_mode_profiles_vault,
        above_canary=_INTIMATE_CANARY,
        below_canary=_OPEN_CANARY,
        artifact_roots=("05-Wavelength",),
        positive_glob="05-Wavelength/Mode-Profiles/express.md",
        forbidden_glob=None,
    ),
    _ReportCase(
        report_type="voice",
        build=_build_voice_vault,
        above_canary=_PERSONAL_CANARY,
        below_canary=_OPEN_CANARY,
        artifact_roots=("07-Voice",),
        positive_glob="07-Voice/analytical-profile.md",
        forbidden_glob="07-Voice/confessional-profile.md",
    ),
    _ReportCase(
        report_type="lexicon",
        build=_build_voice_vault,
        above_canary=_PERSONAL_CANARY,
        below_canary=_OPEN_CANARY,
        artifact_roots=("07-Voice/Lexicon",),
        positive_glob="07-Voice/Lexicon/glossary.md",
        forbidden_glob=None,
    ),
    _ReportCase(
        report_type="rhetorical-patterns",
        build=_build_voice_vault,
        above_canary=_PERSONAL_CANARY,
        below_canary=None,
        artifact_roots=("07-Voice/Rhetorical-Patterns",),
        positive_glob="07-Voice/Rhetorical-Patterns/analytical.md",
        forbidden_glob="07-Voice/Rhetorical-Patterns/confessional.md",
    ),
)
"""Every report type ``creek.report`` exposes, in ``_VALID_TYPES`` order."""

_CASE_IDS = [case.report_type for case in _CASES]

_CASE_BY_TYPE = {case.report_type: case for case in _CASES}


# ---------------------------------------------------------------------------
# T1 / T2 — exclusion in both directions, with positive controls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_report_at_open_ceiling_excludes_above_ceiling_content(
    case: _ReportCase,
    tmp_path: Path,
) -> None:
    """No report writes above-ceiling content into its artifact at ``open``.

    Asserted against the artifact bytes and the artifact *paths*, never against
    ``report_tool``'s response. The response carries only ``report_paths``, so a
    response-level assertion is satisfied by the unfixed code and proves
    nothing; the leak has always been the file.

    The below-ceiling canary is asserted *present* in the same test, and that is
    not decoration: without it, a generator that filtered everything out — or
    crashed into writing an empty file — would satisfy the exclusion assertion
    perfectly while being an outage rather than a gate.

    ``rhetorical-patterns`` has no below-ceiling substring to assert (its
    artifact is three integers), so its positive control is the file-set: the
    ``open`` fragment's register note must exist while the ``personal``
    fragment's must not.

    Args:
        case: The report type under test and its fixture.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = case.build(tmp_path)
    result = report_tool(
        vault_path=vault,
        report_type=case.report_type,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "ok"

    blob = _artifact_blob(vault, case.artifact_roots)
    assert case.above_canary not in blob, (
        f"report_type={case.report_type!r} at privacy_tier_ceiling=open wrote "
        f"the above-ceiling sentinel {case.above_canary!r} into "
        f"{case.artifact_roots}. The response was clean — it always is — so the "
        f"only place this shows up is the artifact:\n\n{blob}"
    )
    assert list(vault.glob(case.positive_glob)), (
        f"report_type={case.report_type!r} wrote nothing matching "
        f"{case.positive_glob!r} at ceiling=open, so the exclusion assertion "
        "above is vacuous. A gate that drops everything is an outage."
    )
    if case.below_canary is not None:
        assert case.below_canary in blob, (
            f"report_type={case.report_type!r} dropped the below-ceiling "
            f"sentinel {case.below_canary!r} at ceiling=open. Admitted content "
            "must still reach the artifact."
        )
    if case.forbidden_glob is not None:
        assert not list(vault.glob(case.forbidden_glob)), (
            f"report_type={case.report_type!r} wrote "
            f"{case.forbidden_glob!r} at ceiling=open. That file exists only "
            "because an above-ceiling fragment was admitted to the corpus, "
            "even though the file's own contents name no fragment."
        )


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_report_at_all_ceiling_admits_everything(
    case: _ReportCase,
    tmp_path: Path,
) -> None:
    """``ceiling=all`` is the explicit override: nothing is filtered out.

    The permissive direction has to be pinned as hard as the restrictive one.
    Every assertion in the test above is satisfied by a generator that writes an
    empty artifact at every ceiling, so without this test "the ceiling is
    enforced" and "the report is broken" are indistinguishable.

    ``rhetorical-patterns`` is exempt from the substring half, and it has to be:
    its artifact is three integer counts, so *neither* sentinel is findable in
    it by a sweep — in either direction. A ``ceiling=all`` assertion that the
    above-ceiling canary is present would be false by construction rather than
    by regression. Its permissive-direction evidence is the ``forbidden_glob``
    assertion at the end of this test (the above-ceiling fragment's register
    note must exist at ``ceiling=all``) plus
    :func:`test_rhetorical_pattern_counts_are_identical_across_ceilings`.
    ``below_canary is None`` is the flag for that case — it marks the artifact,
    not the tier, which is why it gates both sentinels here.

    Args:
        case: The report type under test and its fixture.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = case.build(tmp_path)
    result = report_tool(
        vault_path=vault,
        report_type=case.report_type,
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    assert result["status"] == "ok"

    blob = _artifact_blob(vault, case.artifact_roots)
    if case.below_canary is not None:
        assert case.above_canary in blob, (
            f"report_type={case.report_type!r} at ceiling=all dropped "
            f"{case.above_canary!r}. ALL is the operator's explicit override; "
            "filtering under it is a regression, not caution."
        )
        assert case.below_canary in blob
    assert list(vault.glob(case.positive_glob))
    if case.forbidden_glob is not None:
        assert list(vault.glob(case.forbidden_glob)), (
            f"report_type={case.report_type!r} at ceiling=all did not write "
            f"{case.forbidden_glob!r}, so the file-set assertion at ceiling=open "
            "could be passing because the file is never written at all."
        )


def test_rhetorical_pattern_counts_are_identical_across_ceilings(
    tmp_path: Path,
) -> None:
    """The admitted register's counts do not move when the ceiling widens.

    ``rhetorical-patterns`` is the one report whose artifact contains no
    fragment-derived string, so a canary sweep over it is structurally vacuous.
    Two properties replace it: the file-set assertions in the two tests above,
    and this one — the numbers in ``analytical.md`` must be byte-identical at
    ``ceiling=open`` and ``ceiling=all``.

    That is what rules out the subtler failure the file-set check cannot see: an
    above-ceiling fragment being folded into an *admitted* register's corpus,
    which would leave the file names unchanged and only shift the counts.

    The expected tally is pinned by value as well as by equality, so a
    generator that emptied the analytical corpus (making both files trivially
    equal at zero) fails here.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    open_vault = _build_voice_vault(tmp_path / "open-ceiling")
    all_vault = _build_voice_vault(tmp_path / "all-ceiling")
    report_tool(
        vault_path=open_vault,
        report_type="rhetorical-patterns",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    report_tool(
        vault_path=all_vault,
        report_type="rhetorical-patterns",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    relpath = "07-Voice/Rhetorical-Patterns/analytical.md"
    at_open = (open_vault / relpath).read_text(encoding="utf-8")
    at_all = (all_vault / relpath).read_text(encoding="utf-8")
    assert _move_counts(at_open) == _EXPECTED_ANALYTICAL_MOVES
    assert _move_counts(at_all) == _EXPECTED_ANALYTICAL_MOVES


# ---------------------------------------------------------------------------
# T3 — the second artifact the #968 reproduction named
# ---------------------------------------------------------------------------


def _read_history(vault: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return the raw text and parsed entries of ``tag-history.json``.

    Args:
        vault: Vault root.

    Returns:
        ``(raw_text, entries)``.
    """
    path = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
    raw = path.read_text(encoding="utf-8")
    entries: list[dict[str, Any]] = json.loads(raw)
    return raw, entries


def test_tag_history_excludes_above_ceiling_tags_and_records_its_ceiling(
    tmp_path: Path,
) -> None:
    """``tag-history.json`` is a second artifact and needs its own assertions.

    The #968 reproduction found the intimate tag in *two* files. ``Tag-Garden.md``
    is regenerated from scratch each run, but the history file is append-only —
    an entry written at the wrong ceiling stays in the vault forever, and every
    later run reads it back. Asserting on the garden alone would leave that
    persistent copy unexamined.

    Both the parsed counts and the raw file text are checked, because "absent
    from ``tag_counts``" would still hold if the tag survived in some other key.
    The recorded ``tier_ceiling`` is what makes the entry interpretable at all:
    a count taken under one ceiling is not comparable to a count taken under
    another.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_tags_vault(tmp_path)
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    raw, entries = _read_history(vault)
    newest = entries[-1]
    assert _INTIMATE_CANARY not in newest["tag_counts"]
    assert newest["tag_counts"][_OPEN_CANARY] == 1
    assert _INTIMATE_CANARY not in raw, (
        "the intimate tag survives somewhere in tag-history.json:\n\n" + raw
    )
    assert newest["tier_ceiling"] == "open"


def test_tag_history_does_not_compare_growth_across_ceilings(
    tmp_path: Path,
) -> None:
    """Growth is only ever measured against an entry taken at the same ceiling.

    Two runs at different ceilings survey two different corpora. Comparing one
    against the other produces growth figures that are not merely imprecise but
    meaningless — every tag the wider ceiling admits reads as brand new, and
    every tag the narrower one dropped reads as a collapse.

    The discriminating assertion is about the **open** canary, not the intimate
    one. After a second run at ``ceiling=all``, the intimate tag is new under
    either behaviour (nothing had ever counted it). The open tag is new *only*
    if the ``open``-ceiling entry was correctly rejected as a comparison
    baseline — had it been used, the open tag would have been seen before and
    would not appear under "New Tags" at all.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_tags_vault(tmp_path)
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    _raw, entries = _read_history(vault)
    assert [entry["tier_ceiling"] for entry in entries] == ["open", "all"]

    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    new_tags = _new_tags_section(garden)
    assert _INTIMATE_CANARY in new_tags
    assert _OPEN_CANARY in new_tags, (
        "the ceiling=all run treated the earlier ceiling=open entry as its "
        "growth baseline, so a tag that was only ever counted under a narrower "
        "ceiling no longer reads as new. Growth across mixed ceilings is not a "
        "comparison, it is a category error.\n\n" + garden
    )


# ---------------------------------------------------------------------------
# T4 — a missing key is not an "unclassified" key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ceiling", "admitted"),
    [
        (TierCeiling.OPEN, False),
        (TierCeiling.PERSONAL, False),
        (TierCeiling.INTIMATE, True),
    ],
    ids=["open", "personal", "intimate"],
)
def test_fragment_with_no_privacy_tier_key_fails_closed_to_intimate(
    tmp_path: Path,
    ceiling: TierCeiling,
    admitted: bool,
) -> None:
    """A fragment with no ``privacy_tier`` key at all ranks as ``intimate``.

    **The ``personal`` row is the load-bearing one, and it is why this test
    exists.** ``Fragment`` defaults a *missing* ``privacy_tier`` to
    ``unclassified``, and ``unclassified`` ranks with ``personal`` (#876/#961) —
    so an implementation that read the tier off the validated model instead of
    the raw front matter would refuse this fragment at ``open`` and *admit* it at
    ``personal``. Both implementations pass an ``open``-only assertion. Only the
    ``personal`` row tells them apart.

    A file with no key at all carries less assurance than a pipeline-written one
    that at least says ``unclassified`` out loud, which is why the raw read has
    to fail all the way closed rather than to ``personal``.

    The ``open``-tier fragment is present at every ceiling as the positive
    control, so "excluded" cannot be satisfied by an empty garden.

    Args:
        tmp_path: pytest's per-test temporary directory.
        ceiling: The ceiling the report is run at.
        admitted: Whether the untiered fragment's tag should appear.
    """
    vault = _build_untiered_vault(tmp_path)
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=ceiling,
    )
    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    assert _OPEN_CANARY in garden
    assert (_NOTIER_CANARY in garden) is admitted, (
        "a fragment with no privacy_tier key was "
        f"{'excluded from' if admitted else 'admitted to'} the tag garden at "
        f"ceiling={ceiling.value!r}. A missing key must fail closed to "
        "intimate, not default to the model's 'unclassified'.\n\n" + garden
    )


# ---------------------------------------------------------------------------
# T5 — the four non-fragment scan directories
# ---------------------------------------------------------------------------


def test_tag_garden_excludes_untiered_eddy_tags_at_the_open_ceiling(
    tmp_path: Path,
) -> None:
    """``03-Eddies`` is scanned too, and an eddy has no tier of its own.

    ``TagGardenGenerator`` walks five directories — ``01-Fragments``,
    ``02-Threads``, ``03-Eddies``, ``04-Praxis``, ``08-Decisions`` — with a raw
    ``frontmatter.load`` and no ``Fragment`` model in sight. Gating only the
    fragment directory would leave four open routes to the same artifact, and an
    eddy's tags are *derived from its member fragments*, so this is not a
    hypothetical route: it is the same tags arriving by a different file.

    Note what this pins: because those note types carry no ``privacy_tier``, the
    fail-closed read ranks them ``intimate`` and they are dropped below
    ``ceiling=intimate``. That is a real design choice with a real cost, and
    asserting it here makes it checkable rather than a matter of taste.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_eddy_vault(tmp_path)
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    assert _OPEN_CANARY in garden
    assert _INTIMATE_CANARY not in garden, (
        "an eddy note under 03-Eddies carried an above-ceiling tag into "
        "Tag-Garden.md at ceiling=open. Gating 01-Fragments alone leaves four "
        f"other scan directories ungated.\n\n{garden}"
    )


def test_tag_garden_includes_untiered_eddy_tags_at_the_all_ceiling(
    tmp_path: Path,
) -> None:
    """The eddy route is not simply severed — ``ceiling=all`` still sees it.

    The companion to the test above. Dropping every non-fragment note
    unconditionally would satisfy that assertion and quietly gut the tag garden
    for the operator who explicitly asked for everything.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_eddy_vault(tmp_path)
    report_tool(
        vault_path=vault,
        report_type="tags",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    assert _OPEN_CANARY in garden
    assert _INTIMATE_CANARY in garden


# ---------------------------------------------------------------------------
# T6 — mutation resistance: the gate's *return value* must decide something
# ---------------------------------------------------------------------------


class _InvertingGate:
    """A ``within_ceiling`` stand-in that returns the opposite verdict.

    Wraps the real predicate and negates it, counting calls. Two distinct
    mutations die against it:

    * a generator that *calls* the gate and discards its result is unaffected by
      inversion, so its artifact still holds the below-ceiling content and the
      inverted expectation fails;
    * a generator that stopped calling the gate leaves ``call_count`` at zero.

    Attributes:
        call_count: How many times the gate was invoked.
    """

    def __init__(
        self,
        real: Callable[[Mapping[str, object], PrivacyTierOverride], bool],
    ) -> None:
        """Store the real gate and zero the counter.

        Args:
            real: The genuine ``within_ceiling`` implementation.
        """
        self._real = real
        self.call_count = 0

    def __call__(
        self,
        raw: Mapping[str, object],
        override: PrivacyTierOverride,
    ) -> bool:
        """Return the negation of the real verdict.

        Args:
            raw: The file's raw front matter.
            override: The admission ceiling.

        Returns:
            ``not within_ceiling(raw, override)``.
        """
        self.call_count += 1
        return not self._real(raw, override)


_GATE_CALL_SITES = [
    pytest.param("tags", "creek.generate.tags", id="tags"),
    pytest.param("voice", "creek.generate.voice", id="voice"),
    pytest.param("lexicon", "creek.generate.voice", id="lexicon"),
    pytest.param("decisions", "creek.generate.decisions", id="decisions"),
    pytest.param("mode-profiles", "creek.generate.wavelength", id="wavelength"),
]
"""``report_type`` → the module whose ``within_ceiling`` the walk really calls.

``lexicon`` is the odd row and deliberately so: ``generate_lexicon`` owns no
vault walk of its own — it delegates to
``creek.generate.voice.VoiceExemplarCollector.collect_all_exemplars`` — so the
gate is patched in ``creek.generate.voice`` while the call is driven through
``report_type="lexicon"``. Patching ``creek.generate.lexicon`` instead would
patch a name nothing calls, and the test would pass while the walk stayed
ungated. Between them the five rows cover all five generator modules.
"""


@pytest.mark.parametrize(("report_type", "module"), _GATE_CALL_SITES)
def test_report_generators_act_on_the_gate_verdict_not_just_call_it(
    report_type: str,
    module: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverting the gate must invert the artifact.

    Everything else in this file can be satisfied by a generator that invokes
    ``within_ceiling`` on the request path and then ignores what it said — the
    call site exists, the import exists, an AST walk finds it, and the artifact
    is whatever it always was. Only replacing the gate with one that answers
    *backwards* can tell "consulted" from "obeyed".

    Both halves are asserted. The inverted artifact must now carry the
    above-ceiling sentinel and must have lost the below-ceiling one — which
    fails for a generator that discards the verdict — and ``call_count`` must be
    non-zero, which fails for a generator that stopped consulting the gate at
    all.

    Args:
        report_type: The report driving the walk.
        module: The module whose ``within_ceiling`` name is patched.
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: pytest's patching fixture.
    """
    # Imported here rather than at module scope so the artifact-level tests
    # above fail on the leak itself rather than on a collection-time ImportError
    # while the new privacy_filter API is still absent.
    from creek.classify.privacy_filter import within_ceiling

    case = _CASE_BY_TYPE[report_type]
    vault = case.build(tmp_path)
    spy = _InvertingGate(within_ceiling)
    monkeypatch.setattr(f"{module}.within_ceiling", spy)

    report_tool(
        vault_path=vault,
        report_type=report_type,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    blob = _artifact_blob(vault, case.artifact_roots)

    assert spy.call_count > 0, (
        f"{module}.within_ceiling was never called while generating "
        f"report_type={report_type!r}. A gate off the request path enforces "
        "nothing."
    )
    assert case.above_canary in blob, (
        f"inverting {module}.within_ceiling did not change what "
        f"report_type={report_type!r} wrote: {case.above_canary!r} is still "
        "absent. The generator calls the gate and discards its verdict.\n\n"
        f"{blob}"
    )
    if case.below_canary is not None:
        assert case.below_canary not in blob, (
            f"inverting {module}.within_ceiling left {case.below_canary!r} in "
            "the artifact, so the verdict is not what decides admission.\n\n"
            f"{blob}"
        )


# ---------------------------------------------------------------------------
# T7 — mutation resistance: a parallel, ungated read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_no_file_the_report_touches_carries_above_ceiling_content(
    case: _ReportCase,
    tmp_path: Path,
) -> None:
    """Every file the call created or modified is swept, not a named list.

    The artifact-level tests above look where we already know to look. This one
    derives its file list from the filesystem — everything whose bytes differ
    from a pre-call snapshot — so it also covers an artifact nobody anticipated,
    a debug dump, a cache, and a second ungated walk spliced into an existing
    generator whose declared output looks unchanged.

    Paths are checked as well as contents because ``decisions`` writes the
    fragment title into the *filename*: a bytes-only sweep would walk straight
    past it.

    ``rhetorical-patterns`` writes no fragment-derived text, so its byte sweep
    is vacuous by construction; the ``forbidden_glob`` assertion is what carries
    that row, and it is stated here rather than left implied.

    Args:
        case: The report type under test and its fixture.
        tmp_path: pytest's per-test temporary directory.
    """
    vault = case.build(tmp_path)
    before = _snapshot(vault)
    report_tool(
        vault_path=vault,
        report_type=case.report_type,
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    changed = _changed_files(vault, before)
    assert list(vault.glob(case.positive_glob)), (
        f"report_type={case.report_type!r} produced no artifact at "
        "ceiling=open, so this sweep has nothing to sweep."
    )
    needle = case.above_canary.encode("utf-8")
    for path in changed:
        rel = str(path.relative_to(vault))
        assert case.above_canary not in rel, (
            f"report_type={case.report_type!r} created {rel!r} — the "
            "above-ceiling sentinel is in the file name."
        )
        assert needle not in path.read_bytes(), (
            f"report_type={case.report_type!r} wrote the above-ceiling "
            f"sentinel {case.above_canary!r} into {rel!r}. This file was found "
            "by diffing the vault, not from a list of expected artifacts, so a "
            "second ungated read cannot hide behind an unchanged declared "
            "output."
        )
    if case.forbidden_glob is not None:
        assert not list(vault.glob(case.forbidden_glob)), (
            f"report_type={case.report_type!r} created "
            f"{case.forbidden_glob!r}, which exists only if an above-ceiling "
            "fragment entered the corpus."
        )


# ---------------------------------------------------------------------------
# T8 — the two tier readers must agree
# ---------------------------------------------------------------------------


def _build_every_tier_vault(root: Path) -> Path:
    """Seed one fragment per :class:`PrivacyTier` plus one with no key.

    Args:
        root: Directory the vault is created inside.

    Returns:
        The vault root.
    """
    vault = _new_vault(root, "every-tier-vault")
    for tier in PrivacyTier:
        _write_note(
            vault,
            f"01-Fragments/Notes/frag-{tier.value}.md",
            _fragment_metadata(
                frag_id=f"frag-{tier.value}",
                title=f"{tier.value} note",
                privacy_tier=tier.value,
            ),
            f"{tier.value} body.",
        )
    _write_note(
        vault,
        "01-Fragments/Notes/frag-nokey.md",
        _fragment_metadata(
            frag_id="frag-nokey",
            title="No key note",
            privacy_tier=None,
        ),
        "No key body.",
    )
    return vault


def test_raw_and_model_tier_readers_agree_on_every_fragment(
    tmp_path: Path,
) -> None:
    """The new raw reader must not become a third, diverging tier opinion.

    ``creek/classify/privacy_filter.py`` already carries two fail-closed tier
    reads — ``tier_of`` (unrecognised value) and ``fragment_tier`` (absent key) —
    and its own docstring names the failure mode a third one invites: "two tools
    that disagree about the same file". ``raw_privacy_tier`` is that third
    reader, so its agreement with ``fragment_tier`` is asserted rather than
    assumed, fragment by fragment, over the shared vault loader both would
    actually see.

    The no-key fragment is checked separately and explicitly: it is the one case
    where agreeing on ``INTIMATE`` is the whole point, and where a reader that
    consulted the model would answer ``unclassified`` instead.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    from creek.classify.privacy_filter import fragment_tier, raw_privacy_tier
    from creek.vault.reader import iter_vault_fragments

    vault = _build_every_tier_vault(tmp_path)
    loaded = iter_vault_fragments(vault / "01-Fragments")
    by_id = {fragment.id: (fragment, raw) for _p, fragment, _b, raw in loaded}
    assert set(by_id) == {
        "frag-open",
        "frag-personal",
        "frag-intimate",
        "frag-unclassified",
        "frag-nokey",
    }
    for frag_id, (fragment, raw) in sorted(by_id.items()):
        assert raw_privacy_tier(raw) == fragment_tier(fragment, raw), (
            f"the two fail-closed tier readers disagree about {frag_id!r}: "
            f"raw_privacy_tier says {raw_privacy_tier(raw)!r}, fragment_tier "
            f"says {fragment_tier(fragment, raw)!r}"
        )
    nokey_fragment, nokey_raw = by_id["frag-nokey"]
    assert raw_privacy_tier(nokey_raw) is PrivacyTier.INTIMATE
    assert fragment_tier(nokey_fragment, nokey_raw) is PrivacyTier.INTIMATE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({}, PrivacyTier.INTIMATE, id="key-absent"),
        pytest.param({"privacy_tier": None}, PrivacyTier.INTIMATE, id="none"),
        pytest.param({"privacy_tier": ""}, PrivacyTier.INTIMATE, id="empty"),
        pytest.param(
            {"privacy_tier": "not-a-tier"},
            PrivacyTier.INTIMATE,
            id="unrecognised",
        ),
        pytest.param({"privacy_tier": "open"}, PrivacyTier.OPEN, id="open"),
        pytest.param(
            {"privacy_tier": "personal"},
            PrivacyTier.PERSONAL,
            id="personal",
        ),
        pytest.param(
            {"privacy_tier": "intimate"},
            PrivacyTier.INTIMATE,
            id="intimate",
        ),
        pytest.param(
            {"privacy_tier": "unclassified"},
            PrivacyTier.UNCLASSIFIED,
            id="unclassified",
        ),
    ],
)
def test_raw_privacy_tier_fails_closed_on_anything_it_cannot_read(
    raw: dict[str, object],
    expected: PrivacyTier,
) -> None:
    """Every unreadable ``privacy_tier`` resolves to ``intimate``, never ``open``.

    Four distinct ways of saying nothing — the key absent, an explicit ``None``,
    an empty string, and a value the enum has never heard of — must all land on
    the most restrictive tier. They are listed separately because they arrive
    from different places: a hand-edited note, a YAML ``privacy_tier:`` with no
    value, a template that wrote an empty field, and a future schema this build
    predates.

    An explicit ``unclassified`` is deliberately *not* folded in with them: it
    is what every pipeline-written, not-yet-classified fragment carries, and it
    ranks with ``personal`` by policy (#876) rather than by failure.

    Args:
        raw: Raw front matter to read.
        expected: The tier the reader must return.
    """
    from creek.classify.privacy_filter import raw_privacy_tier

    assert raw_privacy_tier(raw) is expected


# ---------------------------------------------------------------------------
# T10 — the CLI surface
# ---------------------------------------------------------------------------


def test_cli_report_include_tier_open_excludes_above_ceiling_tags(
    tmp_path: Path,
) -> None:
    """``creek report --type tags --include-tier open`` filters the garden.

    ``report_tool`` is not the only production caller: the CLI reaches the same
    six generators through ``_REPORT_DISPATCH``, and a fix threaded only through
    MCP would leave ``--include-tier`` on ``report`` as a flag that is parsed,
    audited, and then ignored.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_tags_vault(tmp_path)
    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "tags",
            "--vault",
            str(vault),
            "--include-tier",
            "open",
        ],
    )
    assert result.exit_code == 0, result.output
    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    assert _OPEN_CANARY in garden
    assert _INTIMATE_CANARY not in garden, (
        "creek report --include-tier open wrote an intimate fragment's tag "
        f"into the Tag Garden:\n\n{garden}"
    )


def test_cli_report_without_include_tier_stays_unfiltered(tmp_path: Path) -> None:
    """A bare ``creek report --type tags`` keeps its pre-#968 behaviour.

    The new generator parameters default to ``PrivacyTierOverride.ALL``, i.e.
    unfiltered, so that adding them is a genuine no-op for every existing
    caller. That promise is worth asserting rather than assuming: an operator
    who never passed ``--include-tier`` and suddenly finds half their tag garden
    missing has been handed a silent data-loss bug wearing a privacy fix's
    costume.

    Args:
        tmp_path: pytest's per-test temporary directory.
    """
    vault = _build_tags_vault(tmp_path)
    result = runner.invoke(
        app,
        ["report", "--type", "tags", "--vault", str(vault)],
    )
    assert result.exit_code == 0, result.output
    garden = (vault / "00-Creek-Meta" / "Tag-Garden.md").read_text(encoding="utf-8")
    assert _OPEN_CANARY in garden
    assert _INTIMATE_CANARY in garden, (
        "creek report with no --include-tier flag now filters the tag garden. "
        "The flag's absence must mean 'unchanged', not 'open'."
    )


# ---------------------------------------------------------------------------
# T11 — the production call sites must state their intent
# ---------------------------------------------------------------------------


_OVERRIDE_CALL_NAMES = frozenset(
    {
        "TagGardenGenerator",
        "generate_lexicon",
        "generate_decisions",
        "generate_mode_profiles",
        "generate_all_profiles",
        "generate_rhetorical_patterns",
        "VoiceProfileGenerator",
    },
)
"""Callables whose new privacy parameter must never be left at its default."""

_OVERRIDE_CALLER_MODULES = [
    "creek_mcp.tools.report",
    "creek.cli",
]
"""The two production surfaces that fan out to the six report generators."""


def _call_name(node: ast.Call) -> str | None:
    """Resolve a call's callee to a bare symbol name.

    Mirrors ``tests/test_mcp_read_gate.py``'s helper of the same name rather
    than importing it: a private helper in another test module is not an API,
    and duplicating six lines is cheaper than coupling two guardrails together.

    Args:
        node: The call node to inspect.

    Returns:
        ``func.id`` for a direct call, ``func.attr`` for a qualified one, or
        ``None`` when the callee is a dynamic expression.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _states_override(node: ast.Call) -> bool:
    """Return whether *node* names an ``override`` at its own or its receiver's call.

    Two shapes carry the override, and both are legitimate:

    * ``generate_decisions(vault, override=override)`` — the parameter is on the
      function itself;
    * ``VoiceProfileGenerator(override=override).generate_all_profiles(vault)`` —
      the parameter is on the constructor, and the method that consumes the
      corpus takes none. Requiring ``override=`` on ``generate_all_profiles``
      itself would force an API the design does not have.

    Args:
        node: The call node to inspect.

    Returns:
        ``True`` when an ``override`` keyword is stated at this call or at the
        constructor call the method is invoked on.
    """
    if any(keyword.arg == "override" for keyword in node.keywords):
        return True
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
        return _states_override(func.value)
    return False


def _guarded_calls(module: ModuleType) -> list[tuple[str, int, bool]]:
    """Return ``(name, lineno, states_override)`` for every guarded call site.

    Args:
        module: The imported production module to scan.

    Returns:
        One tuple per call to a name in :data:`_OVERRIDE_CALL_NAMES`.
    """
    tree = ast.parse(inspect.getsource(module))
    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _OVERRIDE_CALL_NAMES:
            assert name is not None
            found.append((name, node.lineno, _states_override(node)))
    return found


@pytest.mark.parametrize("dotted", _OVERRIDE_CALLER_MODULES)
def test_production_report_callers_always_state_an_override(dotted: str) -> None:
    """Neither production surface may fall back to the unfiltered default.

    Every new parameter defaults to ``PrivacyTierOverride.ALL`` — unfiltered —
    because that is the only default that keeps the change a no-op for the
    library's existing callers and their existing tests. A defaulted privacy
    parameter is safe exactly while every production caller states its intent
    out loud, and *this* is the test that makes that true rather than hoped-for.
    Without it, threading the ceiling through five of the six branches and
    forgetting the sixth reads, at the call site, as ordinary working code.

    The check is structural on purpose. It cannot tell whether the value passed
    is the *right* one — the behavioural tests above do that — but it is the
    only thing that catches a new branch added months from now that simply omits
    the keyword.

    Args:
        dotted: The production module to parse.
    """
    module = importlib.import_module(dotted)
    calls = _guarded_calls(module)
    assert calls, (
        f"{dotted} contains no call to any of {sorted(_OVERRIDE_CALL_NAMES)}, "
        "so this guardrail is scanning for symbols nothing invokes. Either the "
        "report fan-out moved, or the names in _OVERRIDE_CALL_NAMES are stale."
    )
    silent = [(name, lineno) for name, lineno, stated in calls if not stated]
    assert not silent, (
        f"{dotted} calls a report generator without stating an override: "
        f"{silent}. The parameter defaults to PrivacyTierOverride.ALL "
        "(unfiltered), so an omitted keyword is not a neutral omission — it is "
        "the ceiling being silently discarded at that call site."
    )


def test_every_guarded_symbol_is_actually_called_somewhere() -> None:
    """The guarded-name list describes real call sites, not aspirations.

    The per-module test above is only as strong as its name list. A typo, a
    rename, or a symbol that was never called by either surface would leave an
    entry that can never fail — and the per-module assertion would still pass on
    the strength of the other six. Asserting the union across both surfaces
    covers every name closes that hole.
    """
    called: set[str] = set()
    for dotted in _OVERRIDE_CALLER_MODULES:
        module = importlib.import_module(dotted)
        called.update(name for name, _lineno, _stated in _guarded_calls(module))
    missing = _OVERRIDE_CALL_NAMES - called
    assert not missing, (
        f"_OVERRIDE_CALL_NAMES lists {sorted(missing)}, which neither "
        f"{' nor '.join(_OVERRIDE_CALLER_MODULES)} calls. An entry no call site "
        "matches is an assertion about nothing."
    )
