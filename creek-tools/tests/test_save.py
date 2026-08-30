"""Tests for ``creek save`` (FEAT-009).

Covers the destination router, the frontmatter writer, the
``pre_save_filter`` helper added to ``creek/classify/privacy_filter.py``,
and the ``creek save`` CLI command itself.
"""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import pre_save_filter, tier_sensitivity
from creek.cli import app
from creek.models import PrivacyTier
from creek.save import (
    INTIMATE_STUB_RELPATH,
    TARGET_SUBDIRS,
    SaveRequest,
    SaveTarget,
    save_to_vault,
    target_directory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import ShortWriteController


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Provide a minimal vault scaffold for the save module to write under."""
    for relparts in {
        ("00-Creek-Meta",),
        ("01-Fragments",),
        *TARGET_SUBDIRS.values(),
        ("10-Liminal", "Compost", "intimate-stubs"),
    }:
        (tmp_path.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _make_request(
    target: SaveTarget,
    *,
    body: str = "An answer worth keeping.",
    title: str | None = "Why creeks compound",
    tier: PrivacyTier = PrivacyTier.OPEN,
    full_body: bool = False,
    provenance: tuple[str, ...] = ("frag-aaa", "frag-bbb"),
) -> SaveRequest:
    return SaveRequest(
        target=target,
        body=body,
        title=title,
        tier=tier,
        full_body=full_body,
        provenance=provenance,
        source_kind="manual",
        source_id="conv-001",
        saved_by="tester",
    )


# ---- Router ----


@pytest.mark.parametrize(("target", "parts"), list(TARGET_SUBDIRS.items()))
def test_router_maps_each_target_to_canonical_subdir(
    vault: Path,
    target: SaveTarget,
    parts: tuple[str, ...],
) -> None:
    """Every target type resolves to its documented vault subdirectory."""
    assert target_directory(vault, target) == vault.joinpath(*parts)


# ---- Writer: each target produces a model-conformant note at the right path ----


@pytest.mark.parametrize("target", list(SaveTarget))
def test_save_writes_note_under_correct_directory(
    vault: Path,
    target: SaveTarget,
) -> None:
    """Each target lands inside the directory the router promised."""
    path = save_to_vault(_make_request(target), vault_path=vault)
    expected_dir = target_directory(vault, target)
    assert path.parent == expected_dir
    assert path.suffix == ".md"
    post = frontmatter.load(str(path))
    # AI-as-user saves are real fragments (so the retrieval corpus can read
    # them back), not notes typed by their target name — see the dedicated
    # ``test_ai_as_user_*`` cases below.
    expected_type = "fragment" if target is SaveTarget.AI_AS_USER else target.value
    assert post["type"] == expected_type
    assert post["title"]
    saved_from = post["saved_from"]
    assert saved_from["source_kind"] == "manual"
    assert saved_from["source_id"] == "conv-001"
    assert saved_from["contributing_fragments"] == ["frag-aaa", "frag-bbb"]
    assert saved_from["saved_by"] == "tester"
    assert saved_from["saved_at"].endswith("Z")


def test_thread_save_carries_thread_model_fields(vault: Path) -> None:
    """A ``thread`` save serialises Thread-compatible frontmatter keys."""
    path = save_to_vault(
        _make_request(SaveTarget.THREAD, title="Compounding wikis"),
        vault_path=vault,
    )
    post = frontmatter.load(str(path))
    assert post["type"] == "thread"
    assert post["status"] == "active"


def test_eddy_save_carries_eddy_model_fields(vault: Path) -> None:
    """An ``eddy`` save serialises Eddy-compatible frontmatter keys."""
    path = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "eddy"
    assert "formed" in post.metadata


def test_praxis_save_carries_praxis_model_fields(vault: Path) -> None:
    """A ``praxis`` save serialises Praxis-compatible frontmatter keys."""
    path = save_to_vault(_make_request(SaveTarget.PRAXIS), vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "praxis"
    assert post["praxis_type"] == "insight"
    assert post["status"] == "proposed"


# ---- AI-as-user (FEAT-041 §7) ----


def test_ai_as_user_save_lands_attributed_fragment(vault: Path) -> None:
    """An ``ai-as-user`` save files an AI-attributed fragment under 11-Other-Authors."""
    path = save_to_vault(
        _make_request(SaveTarget.AI_AS_USER, title="On leverage and luck"),
        vault_path=vault,
    )
    assert path.parent == vault / "11-Other-Authors" / "ai-as-user"
    post = frontmatter.load(str(path))
    # The note is a real fragment so the Retrieval specialist can read it back.
    assert post["type"] == "fragment"
    assert post["source"]["author"] == "ai"
    assert post["source"]["author_slug"] == "ai-as-user"
    # Borrowed AI voice: it must never bleed into the owner's generated voice,
    # but it is an endorsed stand-in for the owner's views (kept on purpose).
    assert post["voice_weight"] == 0.0
    assert post["representativeness"] == "endorsed"


def test_ai_as_user_same_title_distinct_content_get_distinct_ids(vault: Path) -> None:
    """Two same-titled AI saves with different bodies get distinct ids (#489).

    The id appends a content digest to the title slug, so the in-frontmatter
    ``id`` no longer collides when two kept outputs share a title (the filename
    was already de-duplicated by the atomic collision-retry).
    """
    first = save_to_vault(
        _make_request(SaveTarget.AI_AS_USER, title="Same Title", body="First body."),
        vault_path=vault,
    )
    second = save_to_vault(
        _make_request(SaveTarget.AI_AS_USER, title="Same Title", body="Second body."),
        vault_path=vault,
    )

    first_id = frontmatter.load(str(first))["id"]
    second_id = frontmatter.load(str(second))["id"]
    assert first_id != second_id
    assert first_id.startswith("ai-as-user-Same-Title-")


def test_ai_as_user_same_title_same_content_is_idempotent_id(vault: Path) -> None:
    """Identical title+body yields the same id — a harmless idempotent re-save."""
    request = _make_request(
        SaveTarget.AI_AS_USER, title="Same Title", body="Identical body."
    )
    first = save_to_vault(request, vault_path=vault)
    second = save_to_vault(
        _make_request(
            SaveTarget.AI_AS_USER, title="Same Title", body="Identical body."
        ),
        vault_path=vault,
    )

    assert frontmatter.load(str(first))["id"] == frontmatter.load(str(second))["id"]


def test_ai_as_user_save_round_trips_through_the_fragment_reader(vault: Path) -> None:
    """The saved note loads as a valid Fragment via the vault reader."""
    from creek.vault.reader import try_load_fragment

    path = save_to_vault(
        _make_request(SaveTarget.AI_AS_USER, title="Compounding attention"),
        vault_path=vault,
    )

    record = try_load_fragment(path)

    assert record is not None
    fragment, _body, _raw = record
    assert fragment.source.author == "ai"
    assert fragment.source.author_slug == "ai-as-user"
    assert fragment.voice_weight == 0.0
    assert fragment.representativeness == "endorsed"


# ---- Paradox routing regression ----


def test_paradox_always_lands_in_liminal_paradoxes(vault: Path) -> None:
    """``--target paradox`` is the routing rule that cannot be overridden."""
    request = _make_request(
        SaveTarget.PARADOX,
        body="Two contradictory claims that need preserving, not resolving.",
        title="Both true at once",
    )
    path = save_to_vault(request, vault_path=vault)
    assert path.parent == vault / "10-Liminal" / "Paradoxes"


# ---- Paradox must honour the tier the operator stated (issue #1491) ----

# The title is written into the vault note in the clear by
# ``_title_only_summary`` and slugified into the filename. So every test below
# passes an explicit title that shares no substring with the canary, and keeps
# the canary off line 1. Without that, the correct fix still leaves the canary
# in the vault note and the whole battery reads as "the fix does not work".
#
# The standing instruction survives #1505 unchanged, and the reason is worth
# keeping straight. #1505 stopped an *untitled* save from deriving its title
# from the body's first line above ``open`` (``writer._fallback_title``); at
# ``open`` it still derives, and an *operator-supplied* title is still written
# verbatim at every tier, which is exactly what these tests supply. So the
# title remains a cleartext surface here by design, not by defect.
_PARADOX_TITLE = "Both true at once"
_PARADOX_SECRET = "CLEARTEXT-CANARY-1491"
_PARADOX_BODY = (
    "Two framings collide.\n\n"
    + _PARADOX_SECRET
    + " is the part that must never land in the vault."
)

_REDACTED_BODY = f"[Tier-redacted summary: {_PARADOX_TITLE}]"
"""The exact body a tier-redacted save leaves behind in the vault note.

Compared for equality rather than by substring, so a redaction that started
emitting the summary *alongside* the body would still fail here.

The trailing newline :func:`creek.classify.privacy_filter._title_only_summary`
produces is deliberately absent: ``frontmatter.dumps`` ends its template with
``.strip()`` and ``frontmatter.parse`` returns ``content.strip()``, so neither
the bytes on disk nor ``Post.content`` ever carry it.
"""


def _stub_files(vault: Path) -> list[Path]:
    """Return the intimate stubs currently under the gitignored compost dir.

    Args:
        vault: Vault root.

    Returns:
        Every ``*.md`` under ``10-Liminal/Compost/intimate-stubs``, sorted by
        name so a count assertion reads the same on every filesystem.
    """
    return sorted((vault / "10-Liminal" / "Compost" / "intimate-stubs").glob("*.md"))


def test_paradox_intimate_body_never_lands_in_the_vault(vault: Path) -> None:
    """A paradox save at ``intimate`` files an intimate note, not an open one.

    Issue #1491. :func:`~creek.save.writer.save_to_vault` opens with
    ``effective_tier = OPEN if request.target == SaveTarget.PARADOX else
    request.tier``, so the tier the operator stated is discarded before
    :func:`~creek.classify.privacy_filter.pre_save_filter` is ever consulted:
    the note is stamped ``privacy_tier: open`` and carries the body in full.

    FEAT-009's paradox rule is a *routing* rule — "always lands in
    ``10-Liminal/Paradoxes/``" — and where a note is filed says nothing about
    who may read it once it is there. Implementing the routing rule as a tier
    rule turns choosing ``--target paradox`` into an unannounced
    declassification of whatever the operator was holding.

    Three separate claims, because each can fail on its own:

    * the frontmatter tier is what every downstream consumer keys on
      (``creek state``, the MCP read gate, ``creek report``), so an ``open``
      stamp is the thing that actually widens the audience;
    * the *raw file bytes* are searched rather than ``post.content``, because
      the body is not the only place the answer can surface — frontmatter
      carries ``title`` and ``saved_from`` as well, and a leak into either is
      still a leak;
    * the filename is searched because ``_compose_base_name`` slugifies the
      title into it, and a directory listing is readable by anyone with the
      folder open, whatever the note says inside.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert post["privacy_tier"] == "intimate", (
        "a paradox save was filed at a tier the operator never asked for; "
        f"frontmatter says {post['privacy_tier']!r}"
    )
    raw = path.read_text(encoding="utf-8")
    assert _PARADOX_SECRET not in raw, (
        f"the intimate body is in the clear on disk at {path}:\n\n{raw}"
    )
    assert _PARADOX_SECRET not in path.name


def test_paradox_intimate_body_is_diverted_to_the_stub(vault: Path) -> None:
    """The intimate paradox body reaches the compost stub, not the note (#1491).

    The tier stamp on its own is not the fix.
    :func:`~creek.classify.privacy_filter.pre_save_filter` is what routes an
    intimate body into the gitignored
    ``10-Liminal/Compost/intimate-stubs/`` directory and leaves a title-only
    summary behind, and it only does that when it is handed the tier the
    operator stated — so this pins the *consequence* of the stamp rather than
    trusting the stamp to imply it.

    The body is asserted **present** in the stub as well as absent from the
    note. A "fix" that dropped the intimate body on the floor would satisfy
    every absence assertion in this file while destroying the operator's
    answer, and that is the failure mode a privacy battery is most likely to
    reward by accident.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    stubs = _stub_files(vault)
    assert len(stubs) == 1, f"expected exactly one intimate stub, got {stubs}"
    assert _PARADOX_SECRET in stubs[0].read_text(encoding="utf-8"), (
        "the intimate body reached neither the vault note nor the stub — the "
        "operator's answer was lost rather than protected"
    )

    post = frontmatter.load(str(path))
    assert post.content == _REDACTED_BODY
    pointer = post["saved_from"].get("intimate_body_pointer")
    assert pointer, "the note must record where its intimate body was stashed"
    assert (vault / pointer).exists()


def test_paradox_personal_is_summarised_and_full_body_is_personal_only(
    vault: Path,
) -> None:
    """The whole tier ladder must survive ``--target paradox`` (#1491).

    Pinning ``intimate`` alone would admit a fix that special-cased that one
    tier and left ``personal`` still force-widened to ``open``. Three saves
    into one vault, each under its own title so the filenames cannot collide:

    1. ``personal`` — summarised. The body never reaches the vault and no stub
       is written, because personal content is redacted in place rather than
       stashed off-vault.
    2. ``personal`` + ``full_body`` — the operator's explicit opt-in puts the
       body in the vault *at* ``personal``, still with no stub. This row is
       what stops the fix being "paradox redacts everything now", which would
       be a different bug wearing the same green tests.
    3. ``intimate`` + ``full_body`` — the opt-in must never escape
       ``intimate``. :func:`~creek.classify.privacy_filter.pre_save_filter`
       checks ``INTIMATE`` before it looks at ``full_body`` for exactly that
       reason, and ``--target paradox`` must not become a second way around
       it.

    The stub count is asserted after every phase rather than once at the end,
    because the stub directory accumulates across the three saves and a single
    closing count could not say *which* save wrote one.

    Args:
        vault: Minimal vault scaffold.
    """
    summarised = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.PERSONAL,
        ),
        vault_path=vault,
    )
    assert frontmatter.load(str(summarised))["privacy_tier"] == "personal"
    assert _PARADOX_SECRET not in summarised.read_text(encoding="utf-8")
    assert _stub_files(vault) == []

    opted_in = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=f"{_PARADOX_TITLE} again",
            tier=PrivacyTier.PERSONAL,
            full_body=True,
        ),
        vault_path=vault,
    )
    assert frontmatter.load(str(opted_in))["privacy_tier"] == "personal"
    assert _PARADOX_SECRET in opted_in.read_text(encoding="utf-8"), (
        "--full-body at personal must still put the body in the vault; a "
        "paradox fix that redacted it would break the opt-in instead"
    )
    assert _stub_files(vault) == []

    sealed = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=f"{_PARADOX_TITLE} third",
            tier=PrivacyTier.INTIMATE,
            full_body=True,
        ),
        vault_path=vault,
    )
    assert frontmatter.load(str(sealed))["privacy_tier"] == "intimate"
    assert _PARADOX_SECRET not in sealed.read_text(encoding="utf-8")
    stubs = _stub_files(vault)
    assert len(stubs) == 1, f"expected exactly one intimate stub, got {stubs}"
    assert _PARADOX_SECRET in stubs[0].read_text(encoding="utf-8")


def test_paradox_open_is_unchanged(vault: Path) -> None:
    """A paradox save at ``open`` still files the contradiction verbatim.

    A non-regression control for the common path, **not** a #1491 acceptance
    criterion: this passes before the fix and must keep passing after it. It
    is what separates "paradox honours the stated tier" from "paradox redacts
    everything now" — the second would satisfy every leak assertion in this
    battery while making the target useless for the job it exists to do, which
    is preserving a contradiction in full.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.PARADOX,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.OPEN,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert post["privacy_tier"] == "open"
    assert _PARADOX_SECRET in path.read_text(encoding="utf-8")
    assert _stub_files(vault) == []


# ---- The one-way ratchet: no save may weaken the tier it was given ----

_RATCHET_TIERS = (PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.INTIMATE)
"""The three tiers an operator can state on a save.

``UNCLASSIFIED`` is excluded on purpose: it is what the pipeline writes for
content nobody has classified yet, never one of the three ``creek save``
asks an operator to choose.

It is **not** excluded because the parser rejects it. ``_parse_save_tier``
in :mod:`creek.cli` is a bare ``PrivacyTier(value)``, so ``--tier
unclassified`` parses cleanly and the save executes; only the *error
message* filters ``UNCLASSIFIED`` out of the list of values it advertises.
An earlier revision of this docstring claimed "``--tier`` does not offer
it", which would have justified leaving an operator-reachable tier
untested on the strength of a guarantee the code does not make.

So the exclusion here is a statement about the advertised menu, not about
reachability, and it is scoped to *this* table. The untitled-title table
further down (:data:`_DERIVES_FROM_BODY`) deliberately covers all four
tiers, ``unclassified`` included, for exactly that reason.
"""

_RATCHET_CASES = [
    (target, tier, full_body)
    for target in SaveTarget
    for tier in _RATCHET_TIERS
    for full_body in (False, True)
]
"""Every ``(target, tier, full_body)`` combination the save surface accepts.

The full cross-product rather than the paradox rows alone, because #1491 is a
*shape* of bug — one target quietly rewriting the operator's tier — and the
only assertion that can rule the shape out everywhere is one that runs
everywhere. Seven of the eight targets are expected to pass unchanged; they
are the control that says the ratchet is a property of ``save``, not a
patch on ``paradox``.
"""

_RATCHET_IDS = [
    f"{target.value}-{tier.value}-{'fullbody' if full_body else 'summary'}"
    for target, tier, full_body in _RATCHET_CASES
]
"""Readable node ids, one per row of :data:`_RATCHET_CASES`, positionally parallel."""


def test_ratchet_table_is_not_empty() -> None:
    """The ratchet table must keep every row, and shrinking it must fail here.

    Deleting rows from a ``parametrize`` list never turns a test red — the
    deleted cases simply stop running, so privacy coverage vanishes behind a
    green gate. Asserting the table's size separately converts that deletion
    into a failure.

    The three axes are counted **separately, and never mixed into one set**.
    :class:`~creek.save.SaveTarget` and :class:`~creek.models.PrivacyTier` are
    both ``StrEnum``, so their members hash as bare strings *across* class
    boundaries: a single ``{*SaveTarget, *_RATCHET_TIERS}`` would silently
    collapse any pair that happened to share a value, and the size assertion
    would then be measuring the collision instead of the table.
    """
    assert len({target.value for target in SaveTarget}) == 8
    assert len({tier.value for tier in _RATCHET_TIERS}) == 3
    assert len(_RATCHET_CASES) == 48
    assert len(set(_RATCHET_IDS)) == 48


@pytest.mark.parametrize(
    ("target", "tier", "full_body"),
    _RATCHET_CASES,
    ids=_RATCHET_IDS,
)
def test_no_save_ever_writes_weaker_than_requested(
    vault: Path,
    target: SaveTarget,
    tier: PrivacyTier,
    full_body: bool,
) -> None:
    """``creek save`` may harden a tier, never soften one (#1491).

    The invariant the paradox branch broke, stated once for the whole surface:
    the tier recorded on disk is at least as sensitive as the tier the caller
    asked for. Ranking through
    :func:`~creek.classify.privacy_filter.tier_sensitivity` rather than a rank
    table written here is deliberate — this repository keeps four tier
    rankings that disagree about ``unclassified`` *on purpose*, and a fifth
    invented in a test file would be a fifth opinion nobody reconciled.

    ``>=`` rather than ``==`` because hardening is a legitimate outcome (a
    future rule may raise a tier on the way in) while softening never is.
    Equality for the paradox rows specifically is pinned by the tests above,
    so this row cannot be satisfied by a blanket upgrade to ``intimate``.

    The intimate rows carry a second, on-disk claim: whatever the frontmatter
    says, the canary must not be in the file. A stamp is a label, and #1491
    was a case of the label and the bytes disagreeing.

    Args:
        vault: Minimal vault scaffold.
        target: One :class:`~creek.save.SaveTarget`.
        tier: The tier the caller states.
        full_body: The ``--full-body`` opt-in.
    """
    path = save_to_vault(
        _make_request(
            target,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=tier,
            full_body=full_body,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    written = PrivacyTier(post["privacy_tier"])
    assert tier_sensitivity(written) >= tier_sensitivity(tier), (
        f"a {target.value} save asked for {tier.value} and was filed as "
        f"{written.value} — the save surface weakened the operator's tier"
    )
    if tier is PrivacyTier.INTIMATE:
        assert _PARADOX_SECRET not in path.read_text(encoding="utf-8"), (
            f"an intimate {target.value} save (full_body={full_body}) left "
            f"the body in the clear at {path}"
        )


# ---- pre_save_filter ----


def test_pre_save_filter_open_returns_full_body() -> None:
    """``open`` tier passes the body through unchanged."""
    result = pre_save_filter(
        "Full open answer.",
        tier=PrivacyTier.OPEN,
        title="Open question",
    )
    assert result.vault_body == "Full open answer."
    assert result.stub_body is None
    assert result.stub_relpath is None


def test_pre_save_filter_personal_summarises_by_default() -> None:
    """``personal`` tier writes title + summary, no full body."""
    result = pre_save_filter(
        "A sensitive personal reflection.",
        tier=PrivacyTier.PERSONAL,
        title="Personal moment",
    )
    assert "Personal moment" in result.vault_body
    assert "A sensitive personal reflection." not in result.vault_body
    assert result.stub_body is None
    assert result.stub_relpath is None


def test_pre_save_filter_personal_full_body_when_requested() -> None:
    """Explicit ``--full-body`` lets personal bodies into the vault."""
    result = pre_save_filter(
        "A personal reflection.",
        tier=PrivacyTier.PERSONAL,
        title="Personal moment",
        full_body=True,
    )
    assert result.vault_body == "A personal reflection."
    assert result.stub_body is None


def test_pre_save_filter_intimate_redirects_body_to_gitignored_stubs() -> None:
    """Intimate bodies never reach the vault; they go to the compost stub."""
    result = pre_save_filter(
        "Confessional intimate body.",
        tier=PrivacyTier.INTIMATE,
        title="Intimate moment",
    )
    assert "Intimate moment" in result.vault_body
    assert "Confessional intimate body." not in result.vault_body
    assert result.stub_body == "Confessional intimate body."
    assert result.stub_relpath is not None
    assert result.stub_relpath.parts[:3] == (
        "10-Liminal",
        "Compost",
        "intimate-stubs",
    )


def test_intimate_stub_relpath_constant_matches_gitignored_dir() -> None:
    """The published constant is the canonical gitignored stubs path."""
    assert Path("10-Liminal/Compost/intimate-stubs") == INTIMATE_STUB_RELPATH


def test_pre_save_filter_intimate_ignores_full_body() -> None:
    """``full_body=True`` must NOT widen an intimate-tier body into the vault.

    The :func:`pre_save_filter` docstring says ``full_body`` is
    "Ignored for intimate". The intimate branch fires first in the
    current implementation; pinning the invariant here means a future
    reorder of the conditionals (e.g. checking ``full_body`` before
    the tier) is caught before it can leak intimate content past the
    redactor.
    """
    result = pre_save_filter(
        "Confessional body.",
        tier=PrivacyTier.INTIMATE,
        title="Private",
        full_body=True,
    )
    assert result.stub_body == "Confessional body."
    assert "Confessional body." not in result.vault_body
    assert result.stub_relpath is not None


# ---- Every tier x --full-body: the pre_save_filter decision table (#1508) ----

_PRE_SAVE_SUMMARY = f"{_REDACTED_BODY}\n"
"""The exact ``vault_body`` a redacting :func:`pre_save_filter` call returns.

:func:`~creek.classify.privacy_filter._title_only_summary` ends its string with
a trailing newline and ``pre_save_filter`` hands that value straight back, so
the *direct-call* tests below compare against this constant.
:data:`_REDACTED_BODY` is the same string *after* ``frontmatter`` has stripped
it — ``frontmatter.dumps`` ends its template with ``.strip()`` and
``frontmatter.parse`` returns ``content.strip()`` — so the *on-disk* tests
compare against that one instead.

Derived from :data:`_REDACTED_BODY` rather than retyped, deliberately: two
independently-typed spellings of one sentence drift, and once they had, the
pair could no longer say which layer changed.
"""

_PRE_SAVE_ROWS: tuple[tuple[PrivacyTier, bool, str, str | None, bool], ...] = (
    (PrivacyTier.OPEN, False, _PARADOX_BODY, None, False),
    (PrivacyTier.OPEN, True, _PARADOX_BODY, None, False),
    (PrivacyTier.UNCLASSIFIED, False, _PRE_SAVE_SUMMARY, None, False),
    (PrivacyTier.UNCLASSIFIED, True, _PARADOX_BODY, None, False),
    (PrivacyTier.PERSONAL, False, _PRE_SAVE_SUMMARY, None, False),
    (PrivacyTier.PERSONAL, True, _PARADOX_BODY, None, False),
    (PrivacyTier.INTIMATE, False, _PRE_SAVE_SUMMARY, _PARADOX_BODY, True),
    (PrivacyTier.INTIMATE, True, _PRE_SAVE_SUMMARY, _PARADOX_BODY, True),
)
"""The whole ``pre_save_filter`` decision table, one row per ``(tier, full_body)``.

Each row is ``(tier, full_body, expected_vault_body, expected_stub_body,
expects_stub_relpath)``.

Every expectation is **declared here, by hand**. The table never calls
:func:`~creek.classify.privacy_filter.tier_sensitivity`, or anything else under
test, to compute what it expects: a table derived from the implementation
agrees with the implementation by construction and can only assert that the
code equals itself. Eight rows written down are eight decisions somebody has to
argue with.

The two ``open`` rows are the **positive control**, and they are what proves
the canary check can fire at all. On those rows the secret must be *present* in
``vault_body``, so an over-reaching "redact everything now" fix — which would
satisfy every absence assertion in this file — turns them red instead of green.

The ``unclassified`` + ``full_body=True`` row records the decided
``--full-body`` semantics: the opt-in is honoured at ``unclassified`` exactly
as it is at ``personal``. The read side already normalises ``UNCLASSIFIED`` to
``PERSONAL`` (:func:`~creek.classify.privacy_filter._effective_tier`) and then
applies the same opt-in, so a save stricter than the read it feeds would
contradict the #876/#961 ranking this table is built on. If that decision is
ever revisited, this row has to be *changed* — not deleted. A deleted row
asserts nothing and announces nothing.

A plain tuple of tuples rather than ``pytest.param`` objects, because
:func:`test_pre_save_filter_table_covers_every_tier_and_full_body` reads the
tier back out of ``row[0]`` to compare the table's tier set against
:class:`~creek.models.PrivacyTier`; a ``ParameterSet`` hides its values behind
an attribute, and that guard would have to reach into pytest internals to see
them.
"""

_PRE_SAVE_IDS = [
    f"{tier.value}-{'fullbody' if full_body else 'summary'}"
    for tier, full_body, *_rest in _PRE_SAVE_ROWS
]
"""Readable node ids, one per row of :data:`_PRE_SAVE_ROWS`, positionally parallel."""


def test_pre_save_filter_table_covers_every_tier_and_full_body() -> None:
    """The decision table must keep every row, and shrinking it must fail here.

    Deleting a row from a ``parametrize`` list never turns a test red — the
    deleted case simply stops running, so privacy coverage vanishes behind a
    green gate. Asserting the table's size separately converts that deletion
    into a failure.

    ``{row[0] for row in _PRE_SAVE_ROWS} == set(PrivacyTier)`` is the row this
    section needs most. The expectations are declared per tier, so a fifth
    :class:`~creek.models.PrivacyTier` member added later would otherwise be
    *silently untested*: it would neither appear in the table nor raise a
    ``KeyError``, and its saves could leak with the whole battery green.
    Equality forces whoever adds it to write down its two expectations —
    ``full_body`` false and true — on purpose.

    The tier count is taken over ``{tier.value ...}`` rather than over the
    members, for the reason :func:`test_ratchet_table_is_not_empty` records:
    :class:`~creek.models.PrivacyTier` is a ``StrEnum``, so its members hash as
    bare strings and a set mixed with another ``StrEnum``'s members would
    silently collapse any pair that shared a value.
    """
    assert len({tier.value for tier in PrivacyTier}) == 4
    assert {row[0] for row in _PRE_SAVE_ROWS} == set(PrivacyTier)
    assert len(_PRE_SAVE_ROWS) == 8
    assert len(set(_PRE_SAVE_IDS)) == 8


@pytest.mark.parametrize(
    ("tier", "full_body", "expected_vault_body", "expected_stub_body", "expects_stub"),
    _PRE_SAVE_ROWS,
    ids=_PRE_SAVE_IDS,
)
def test_pre_save_filter_redacts_by_rank_not_by_tier_equality(
    tier: PrivacyTier,
    full_body: bool,
    expected_vault_body: str,
    expected_stub_body: str | None,
    expects_stub: bool,
) -> None:
    """``pre_save_filter`` must branch on the tier's *rank*, not its name (#1508).

    ``unclassified`` is the row this test exists for.
    :func:`~creek.classify.privacy_filter.tier_sensitivity` ranks it ``1``,
    alongside ``personal`` (#876/#961), and the MCP ceiling admits it only at a
    ``personal``-or-broader ceiling — yet ``pre_save_filter`` branched on
    ``tier == PrivacyTier.INTIMATE`` and ``tier == PrivacyTier.PERSONAL``, so
    the one tier that matches neither name fell through to the verbatim return
    and put its body into the vault note in the clear.

    The ``intimate`` rows are checked at **both** ``full_body`` values because
    the intimate branch must stay **first**. Reordering it below the
    personal/unclassified branch would send an intimate ``full_body=False``
    save into the summary branch, which returns ``stub_body=None`` — silently
    **destroying** the operator's body rather than stashing it. That outcome
    satisfies every absence assertion a privacy battery would otherwise make,
    which is exactly why the stub body is asserted by equality here.

    Every claim is an equality (or an identity against a declared expectation),
    never a substring or a truthiness check, for the reason
    :data:`_REDACTED_BODY` records: a call that returned nothing at all must
    not be able to satisfy a row.

    Args:
        tier: The tier the caller states.
        full_body: The ``--full-body`` opt-in.
        expected_vault_body: The body this row says must reach the vault note.
        expected_stub_body: The body this row says must reach the gitignored
            stub, or ``None`` when no stub is owed.
        expects_stub: Whether this row says a stub path must be composed.
    """
    result = pre_save_filter(
        _PARADOX_BODY,
        tier=tier,
        title=_PARADOX_TITLE,
        full_body=full_body,
    )
    canary_expected = expected_vault_body == _PARADOX_BODY

    assert result.vault_body == expected_vault_body, (
        f"a {tier.value} save (full_body={full_body}) put "
        f"{result.vault_body!r} in the vault note"
    )
    assert result.stub_body == expected_stub_body, (
        f"a {tier.value} save (full_body={full_body}) stashed "
        f"{result.stub_body!r} in the stub"
    )
    assert (result.stub_relpath is not None) is expects_stub, (
        f"a {tier.value} save (full_body={full_body}) composed stub_relpath "
        f"{result.stub_relpath!r}; a stub path was expected: {expects_stub}"
    )
    assert (_PARADOX_SECRET in result.vault_body) is canary_expected, (
        f"a {tier.value} save (full_body={full_body}) should carry the canary "
        f"in its vault body: {canary_expected}; it returned "
        f"{result.vault_body!r}"
    )


@pytest.mark.parametrize("full_body", [False, True])
def test_pre_save_filter_unranked_tier_stashes_the_body(full_body: bool) -> None:
    """A tier nobody ranked must take the *intimate* branch (#1508).

    :func:`~creek.classify.privacy_filter.tier_sensitivity` fail-closes an
    unranked tier to rank ``2`` — ``intimate``'s rank — so an unrecognised
    future tier must be handled as intimate content: the vault note gets the
    title-only summary and the full body goes to the gitignored compost stub.

    **This is the only test in the suite that can see the intimate
    threshold.** Reverting the rank comparison to ``tier ==
    PrivacyTier.INTIMATE`` leaves all eight enum-member rows of
    :data:`_PRE_SAVE_ROWS` byte-identical — measured, not assumed: over the
    four members of :class:`~creek.models.PrivacyTier`, ``_TIER_RANK`` reads
    ``open`` 0, ``unclassified`` 1, ``personal`` 1, ``intimate`` 2, so
    ``rank >= 2`` and ``tier == INTIMATE`` select exactly the same two rows.
    Deleting this test therefore removes the entire safety net for that half
    of the fix, with nothing turning red.

    Both ``full_body`` values are exercised, because the mutant fails
    differently at each: at ``full_body=True`` the equality branch leaks the
    cleartext body into the vault note, and at ``full_body=False`` it discards
    the body entirely (``stub_body=None``) — a leak and a data loss from one
    line of code.

    :func:`~creek.classify.privacy_filter.pre_save_filter` is called
    **directly** rather than through :func:`~creek.save.writer.save_to_vault`,
    because the writer cannot serialise a non-enum tier into frontmatter; the
    invalid value is spelled with :func:`~typing.cast` rather than a type
    ignore, following the three live precedents at
    ``tests/test_mcp_tier_ceiling.py:187``, ``:296`` and ``:418``.

    Args:
        full_body: The ``--full-body`` opt-in, which an unranked tier must
            ignore exactly as ``intimate`` does.
    """
    unknown = cast("PrivacyTier", "not-a-tier")

    result = pre_save_filter(
        _PARADOX_BODY,
        tier=unknown,
        title=_PARADOX_TITLE,
        full_body=full_body,
    )

    assert result.vault_body == _PRE_SAVE_SUMMARY, (
        f"an unranked tier (full_body={full_body}) put {result.vault_body!r} "
        "in the vault note; a tier nobody can vouch for must be summarised"
    )
    assert result.stub_body == _PARADOX_BODY, (
        f"an unranked tier (full_body={full_body}) stashed "
        f"{result.stub_body!r}; the body must reach the stub intact rather "
        "than being dropped on the floor"
    )
    assert result.stub_relpath is not None, (
        f"an unranked tier (full_body={full_body}) composed no stub path, so "
        "the body it withheld from the vault has nowhere to go"
    )


# ---- Intimate full-body never lands in the vault tree ----


def test_intimate_save_never_writes_full_body_into_vault(vault: Path) -> None:
    """File-system inspection: vault note has no intimate body content."""
    sensitive = "INTIMATE BODY MARKER 7f3a"
    request = _make_request(
        SaveTarget.UNNAMED,
        body=sensitive,
        title="Confession",
        tier=PrivacyTier.INTIMATE,
    )
    written = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(written))
    assert sensitive not in post.content
    saved_from = post["saved_from"]
    assert saved_from.get("intimate_body_pointer", "").startswith(
        "10-Liminal/Compost/intimate-stubs/",
    )
    # The body lives only inside the compost stubs directory.
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stub_files = list(stubs_dir.glob("*.md"))
    assert stub_files, "intimate save must drop a stub"
    assert any(sensitive in f.read_text(encoding="utf-8") for f in stub_files)
    # And nowhere else under the tracked vault tree.
    leakages = [
        p
        for p in vault.rglob("*.md")
        if "intimate-stubs" not in p.parts
        and sensitive in p.read_text(encoding="utf-8")
    ]
    assert leakages == []


def test_unclassified_save_redacts_the_body_on_disk(vault: Path) -> None:
    """An ``unclassified`` save leaves a summary on disk, not the body (#1508).

    AC#1: the end-to-end proof that the rank-based fix in
    :func:`~creek.classify.privacy_filter.pre_save_filter` reaches the *bytes*
    on disk, through :mod:`creek.save.writer`, and not merely the filter's
    return value. The direct-call table above can be satisfied by a filter
    whose result the writer then ignores; this cannot.

    Four separate claims, because each can fail on its own:

    * ``post.content`` is the redacted summary, by **equality** — a redaction
      that emitted the summary *alongside* the body would pass a substring
      check while leaking everything;
    * the canary is absent from the **whole file**, frontmatter included. A
      tier stamp is a label; #1491 was a case of the label and the bytes
      disagreeing, and ``title`` / ``saved_from`` are cleartext surfaces the
      body redaction never touches;
    * **no compost stub is written**. ``unclassified`` ranks with ``personal``,
      not with ``intimate``: it is redacted in place, not stashed off-vault. A
      fix that reached the summary by routing every non-``open`` tier down the
      intimate branch would satisfy the first two claims and quietly start
      littering the gitignored stub directory with untiered content;
    * the frontmatter still says ``unclassified``. The fix redacts, it does not
      upgrade the stamp — the tier the operator stated is what gets recorded,
      and silently rewriting it to ``personal`` would be the one-way ratchet
      pinned by :func:`test_no_save_ever_writes_weaker_than_requested` running
      in the wrong direction.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.UNNAMED,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.UNCLASSIFIED,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    raw = path.read_text(encoding="utf-8")
    assert post.content == _REDACTED_BODY, (
        f"an unclassified save wrote {post.content!r} into the vault note; "
        f"the redacted summary must be exactly {_REDACTED_BODY!r}"
    )
    assert _PARADOX_SECRET not in raw, (
        f"the unclassified body is in the clear on disk at {path}:\n\n{raw}"
    )
    assert _stub_files(vault) == [], (
        "an unclassified save wrote a compost stub; only intimate bodies are "
        "stashed off-vault, and untiered content must not be filed there"
    )
    assert post["privacy_tier"] == "unclassified", (
        "an unclassified save was stamped "
        f"{post['privacy_tier']!r} — the fix redacts the body, it does not "
        "rewrite the tier the operator stated"
    )


def test_unclassified_save_honours_full_body_on_disk(vault: Path) -> None:
    """``--full-body`` at ``unclassified`` still files the body (#1508).

    The positive control at the on-disk layer, and the decided semantics
    written down: the opt-in is honoured at ``unclassified`` exactly as it is
    at ``personal``. The read side already normalises ``UNCLASSIFIED`` to
    ``PERSONAL`` (:func:`~creek.classify.privacy_filter._effective_tier`) and
    then applies :func:`~creek.classify.privacy_filter._allows_full_personal_body`,
    so making the save side stricter than the read side would contradict the
    #876/#961 ranking the fix is built on.

    Without this test the whole #1508 battery would be satisfied by "redact
    every unclassified save unconditionally", which is a different behaviour
    change wearing this one's fix and would silently break an operator opt-in
    that works at every other tier.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.UNNAMED,
            body=_PARADOX_BODY,
            title=_PARADOX_TITLE,
            tier=PrivacyTier.UNCLASSIFIED,
            full_body=True,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert _PARADOX_SECRET in path.read_text(encoding="utf-8"), (
        "--full-body at unclassified must still put the body in the vault; a "
        "#1508 fix that redacted it would break the opt-in instead"
    )
    assert post.content == _PARADOX_BODY, (
        f"an unclassified --full-body save wrote {post.content!r}; the "
        "operator's body must reach the note intact, not in part"
    )


# ---- CLI ----


runner = CliRunner()


def _scaffold_vault(root: Path) -> Path:
    """Create a vault layout sufficient for ``creek save``."""
    for parts in {
        ("00-Creek-Meta",),
        ("01-Fragments",),
        *TARGET_SUBDIRS.values(),
        ("10-Liminal", "Compost", "intimate-stubs"),
    }:
        root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    return root


def test_cli_save_help_lists_targets() -> None:
    """``creek save --help`` advertises every target type."""
    result = runner.invoke(app, ["save", "--help"])
    assert result.exit_code == 0
    for target in SaveTarget:
        assert target.value in result.output


def test_cli_save_refuses_without_tier_or_provenance(tmp_path: Path) -> None:
    """No tier + no provenance is the regression case: must refuse loudly."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("Hello", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "tier" in result.output.lower()


def test_cli_save_thread_round_trip(tmp_path: Path) -> None:
    """Integration: --target thread + --body + --provenance + --tier open works."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("A thoughtful synthesis.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--title",
            "How creeks compound",
            "--provenance",
            "frag-001,frag-002",
            "--source",
            "claude-session-xyz",
            "--source-kind",
            "claude-session",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "02-Threads" / "Active").glob("*.md"))
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post["type"] == "thread"
    assert post["title"] == "How creeks compound"
    assert post["saved_from"]["contributing_fragments"] == ["frag-001", "frag-002"]
    assert post["saved_from"]["source_kind"] == "claude-session"
    assert "A thoughtful synthesis." in post.content


def test_cli_save_observation_round_trip(tmp_path: Path) -> None:
    """Integration: --target observation files into the Observations folder (#584)."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "obs.md"
    body_file.write_text("Felt the F2 to F7 turn today.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "observation",
            "--body",
            str(body_file),
            "--title",
            "Afternoon turn",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "05-Wavelength" / "Observations").glob("*.md"))
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post["type"] == "observation"
    assert "Felt the F2 to F7 turn today." in post.content


def test_saved_observation_lands_in_decision_context_read_dir(tmp_path: Path) -> None:
    """A saved observation lands exactly where DecisionContextGatherer reads (#584).

    ``DecisionContextGatherer._current_wavelength`` scans
    ``05-Wavelength/Observations/``; routing a save there closes the
    producer→consumer loop the folder previously lacked.
    """
    vault = _scaffold_vault(tmp_path / "vault")

    path = save_to_vault(_make_request(SaveTarget.OBSERVATION), vault_path=vault)

    assert path.parent == vault / "05-Wavelength" / "Observations"


def test_cli_save_paradox_routing(tmp_path: Path) -> None:
    """A paradox save lands in 10-Liminal/Paradoxes via the CLI too."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("Two things cannot both be true.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "paradox",
            "--body",
            str(body_file),
            "--title",
            "Contradiction X",
            "--provenance",
            "frag-001",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "10-Liminal" / "Paradoxes").glob("*.md"))
    assert len(written) == 1


def test_save_falls_back_to_first_body_line_when_title_missing(vault: Path) -> None:
    """At ``open``, an omitted ``--title`` becomes the first non-empty body line.

    Scoped to ``open`` since #1505: above that tier the fallback is a
    content digest, not body text, and
    :func:`test_untitled_save_derives_its_title_from_the_body_only_at_open`
    owns the other three tiers. This row is the ``open`` affordance itself
    and must keep working.
    """
    request = SaveRequest(
        target=SaveTarget.UNNAMED,
        body="\n\n# Derived from body\n\nThe rest of the answer.",
        title=None,
        tier=PrivacyTier.OPEN,
        provenance=("frag-001",),
        source_kind="manual",
        source_id="conv-1",
        saved_by="tester",
    )
    path = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["title"] == "Derived from body"


def test_save_retries_on_filename_collision(vault: Path) -> None:
    """Two saves with the same title produce two files with counter suffixes."""
    first = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    second = save_to_vault(_make_request(SaveTarget.EDDY), vault_path=vault)
    assert first != second
    assert second.parent == first.parent


def test_intimate_stub_collision_increments_suffix(vault: Path) -> None:
    """A second intimate save with the same title gets a -1 stub suffix."""
    request = _make_request(
        SaveTarget.UNNAMED,
        body="First intimate body",
        title="repeat me",
        tier=PrivacyTier.INTIMATE,
    )
    save_to_vault(request, vault_path=vault)
    save_to_vault(
        SaveRequest(
            target=SaveTarget.UNNAMED,
            body="Second intimate body",
            title="repeat me",
            tier=PrivacyTier.INTIMATE,
            provenance=("frag-aaa",),
            source_kind="manual",
            source_id="conv-001",
            saved_by="tester",
        ),
        vault_path=vault,
    )
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stubs = sorted(stubs_dir.glob("*.md"))
    assert len(stubs) == 2
    assert any(s.stem.endswith("-1") for s in stubs)


def test_intimate_stub_records_saved_at_and_saved_by(vault: Path) -> None:
    """Stub frontmatter carries the full ``saved_from`` block.

    The stub directory is gitignored — without a ``saved_at`` field on
    the stub itself, operators recovering from disk would have no
    timestamp to reason about. The block also identifies who saved it.
    """
    request = _make_request(
        SaveTarget.UNNAMED,
        body="Intimate confession.",
        title="With timestamp",
        tier=PrivacyTier.INTIMATE,
    )
    save_to_vault(request, vault_path=vault)
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    stub = next(iter(stubs_dir.glob("*.md")))
    post = frontmatter.load(str(stub))
    saved_from = post["saved_from"]
    assert saved_from.get("saved_at")
    assert saved_from["saved_by"] == "tester"
    # The stub *is* the body, so it must not point back at itself or
    # leak an intimate_body_pointer.
    assert "intimate_body_pointer" not in saved_from


def test_cli_save_unknown_source_kind_exits_two(tmp_path: Path) -> None:
    """Reject free-form ``--source-kind`` values with a clear error."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("body", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(body_file),
            "--provenance",
            "frag-001",
            "--source-kind",
            "smoke-signal",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "source-kind" in result.output.lower()


_STUB_PREFIX = "10-Liminal/Compost/intimate-stubs"
"""The only place under the vault an intimate body may legitimately appear."""


def _files_containing(vault: Path, needle: str) -> list[str]:
    """Return every vault-relative path whose *bytes* contain *needle*.

    Walks the whole vault rather than the one subtree the leak was expected
    in. Scoping a leak check to the directory you already suspect is the #1341
    regression shape, and one save touches more than its target folder: a
    stub, an index, an audit row.

    ``read_bytes`` over ``rglob("*")`` rather than ``read_text`` over
    ``rglob("*.md")``, the same shape as
    ``tests/test_purge_voice_artifacts.py``'s helper and for the same reason:
    residue is not confined to markdown, and a text read raises on the first
    undecodable byte instead of reporting a leak.

    Args:
        vault: Vault root.
        needle: The sentinel to search for.

    Returns:
        POSIX-style vault-relative paths, sorted, so a failure message names
        the offending files in a stable order.
    """
    probe = needle.encode("utf-8")
    return sorted(
        path.relative_to(vault).as_posix()
        for path in vault.rglob("*")
        if path.is_file() and probe in path.read_bytes()
    )


def test_cli_save_paradox_intimate_writes_no_cleartext_body(tmp_path: Path) -> None:
    """The CLI transport must not file an intimate paradox body in the clear.

    Issue #1491, end to end. The writer-seam tests above pin the defect at
    :func:`~creek.save.writer.save_to_vault`; this one pins it where the
    operator actually meets it, because ``creek save`` is the surface that
    accepts ``--tier intimate`` and therefore the surface that made the
    promise.

    The leak assertion is stated over the **whole vault**, not over the
    paradox folder: the question is not "did the note keep the body" but
    "is the body anywhere a reader without intimate access can reach it", and
    ``10-Liminal/Compost/intimate-stubs/`` is the single gitignored exception.

    The scan also asserts the canary is present in *at least one* file. An
    "absent everywhere" assertion is trivially satisfied by a save that lost
    the body, which would read as a fix and be a data-loss bug.

    Args:
        tmp_path: Pytest temporary directory.
    """
    body_file = tmp_path / "answer.md"
    body_file.write_text(_PARADOX_BODY, encoding="utf-8")
    vault = _scaffold_vault(tmp_path / "vault")

    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "paradox",
            "--title",
            _PARADOX_TITLE,
            "--body",
            str(body_file),
            "--provenance",
            "frag-001",
            "--tier",
            "intimate",
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    notes = list((vault / "10-Liminal" / "Paradoxes").glob("*.md"))
    assert len(notes) == 1, result.output
    assert frontmatter.load(str(notes[0]))["privacy_tier"] == "intimate"

    carriers = _files_containing(vault, _PARADOX_SECRET)
    assert carriers, (
        "the intimate body reached no file under the vault at all — it was "
        "lost rather than sealed, which is a data-loss bug wearing a fix's "
        "costume"
    )
    stray = [path for path in carriers if not path.startswith(_STUB_PREFIX)]
    assert stray == [], (
        "the intimate body is readable outside the gitignored intimate-stubs "
        f"directory: {stray}"
    )


def test_cli_save_missing_body_path_exits_two(tmp_path: Path) -> None:
    """A nonexistent ``--body`` path is a hard error, not a silent inline body.

    Regression for the bug where ``--body /typo.md`` would file the
    path string itself as the note body. For a privacy-sensitive
    primitive that silent fallback is dangerous: the operator believes
    they filed their answer when they actually filed a path.
    """
    vault = _scaffold_vault(tmp_path / "vault")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "thread",
            "--body",
            str(tmp_path / "does-not-exist.md"),
            "--provenance",
            "frag-001",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "does not exist" in result.output.lower()
    # And nothing got written into the vault — no thread file.
    assert not list((vault / "02-Threads" / "Active").glob("*.md"))


def test_cli_save_unknown_target_exits_two(tmp_path: Path) -> None:
    """An unknown --target value exits 2 with a hint listing valid options."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("body", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "nope",
            "--body",
            str(body_file),
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "target" in result.output.lower()


# ---- _atomic_create exhaustion path ----


def test_atomic_create_raises_when_collision_retries_exhausted(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_atomic_create`` raises after ``_MAX_COLLISION_RETRIES`` collisions.

    The defensive ``RuntimeError`` is uncoverable in normal operation
    (it requires 1000 colliding files). Monkeypatching ``os.open`` to
    always raise ``FileExistsError`` exercises the exhaustion branch
    cheaply so the error message is locked down by a test.
    """
    from creek.save import writer as writer_module

    def always_exists(*_args: object, **_kwargs: object) -> int:
        raise FileExistsError

    monkeypatch.setattr(os, "open", always_exists)
    with pytest.raises(RuntimeError) as excinfo:
        writer_module._atomic_create(vault, "stuck", "content")
    message = str(excinfo.value)
    assert "stuck.md" in message
    assert str(writer_module._MAX_COLLISION_RETRIES) in message


def test_save_writes_full_body_under_short_writes(
    vault: Path,
    short_write: ShortWriteController,
) -> None:
    """A short ``os.write`` must not truncate a saved note's body (#987).

    ``_atomic_create`` writes directly to the final path, so a discarded
    byte count silently files half an answer under the operator's title —
    the most dangerous shape of this bug, because the note looks saved.
    """
    short_write.halve()
    body = "x" * 400 + "TAIL"

    path = save_to_vault(
        _make_request(SaveTarget.THREAD, body=body),
        vault_path=vault,
    )

    # Assert on the raw bytes first: a truncated note may lose its closing
    # frontmatter delimiter, and a parser error would obscure the real
    # failure (the missing tail).
    assert path.read_text(encoding="utf-8").rstrip("\n").endswith("TAIL")
    post = frontmatter.load(str(path))
    assert post.content.strip() == body


def test_save_atomic_create_leaves_no_file_when_write_makes_no_progress(
    vault: Path,
    short_write: ShortWriteController,
) -> None:
    """A stalled descriptor raises instead of filing an empty note."""
    short_write.stall()

    with pytest.raises(OSError) as excinfo:
        save_to_vault(
            _make_request(SaveTarget.THREAD, body="x" * 400 + "TAIL"),
            vault_path=vault,
        )

    assert excinfo.value.errno == errno.EIO
    assert not list(target_directory(vault, SaveTarget.THREAD).glob("*.md"))


# ---- Shared slugify helper ----


def test_slugify_filename_is_idempotent() -> None:
    """``slugify_filename(slugify_filename(x)) == slugify_filename(x)`` for any x.

    The property test exercises the truncation-at-hyphen edge case
    that a naive implementation would fail: when the cut lands on a
    ``-`` the second pass must produce the same string, not a string
    one character shorter.
    """
    from creek.save._slug import slugify_filename

    samples = [
        "",
        "Why creeks compound",
        "  leading and trailing  ",
        "weird---hyphens",
        "exclamation!points!and?question marks",
        "unicode-naïve-test",
        "a-b-c-d-e-f-g-h",  # may truncate at a hyphen with small max_length
        "ALL CAPS TITLE",
        "1234567890",
        "a" * 200,  # very long
    ]
    for sample in samples:
        once = slugify_filename(sample)
        twice = slugify_filename(once)
        assert once == twice, f"slugify_filename not idempotent for {sample!r}"


def test_slugify_filename_truncates_at_hyphen_without_breaking_idempotence() -> None:
    """A truncation that lands on a hyphen still round-trips cleanly."""
    from creek.save._slug import slugify_filename

    # "a-b-c-d-e" with max_length=4 would naively yield "a-b-" — and
    # re-slugifying "a-b-" would strip the trailing "-" and return "a-b",
    # breaking idempotence. The helper strips trailing hyphens after
    # truncation to keep the round-trip closed.
    result = slugify_filename("a-b-c-d-e", max_length=4)
    assert result == slugify_filename(result, max_length=4)
    assert not result.endswith("-")


def test_slugify_filename_used_by_both_call_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both ``_compose_base_name`` and ``_stub_relpath_for`` route through the helper.

    Runtime check — wraps :func:`slugify_filename` with a spy and
    confirms both call sites invoke it. A source-text grep was the
    first cut (PR #287 review caught it) but would also pass with a
    dead import or a comment, so the spy is the robust pin.

    ``creek.save.writer`` imports ``slugify_filename`` at module load
    time, so the spy must replace ``writer.slugify_filename`` to be
    seen. ``creek.classify.privacy_filter`` imports it function-locally
    on each call, so replacing the canonical attribute on
    ``creek.save._slug`` is enough to redirect that path.
    """
    from creek.classify import privacy_filter as classify_module
    from creek.save import _slug as slug_module
    from creek.save import writer as writer_module

    calls: list[str] = []
    real_slugify = slug_module.slugify_filename

    def spy(text: str, *, max_length: int = 64) -> str:
        calls.append(text)
        return real_slugify(text, max_length=max_length)

    monkeypatch.setattr(slug_module, "slugify_filename", spy)
    monkeypatch.setattr(writer_module, "slugify_filename", spy)

    writer_module._compose_base_name("Why this matters")
    classify_module._stub_relpath_for("Intimate moment")

    # At least one call came from each site — the helper is the
    # single source of truth, not duplicated regex logic.
    assert "Why this matters" in calls
    # The privacy_filter lowercases before calling, so check the
    # lowered form.
    assert "intimate moment" in calls


def test_stub_relpath_preserves_non_word_chars_as_hyphens() -> None:
    """Stub path slugs replace ``!``, ``?``, ``:`` (etc.) with hyphens.

    Regression for the PR #287 review concern: the pre-refactor
    ``_stub_relpath_for`` replaced non-word/non-hyphen chars with a
    hyphen; an early draft of the shared helper dropped them
    silently, which would have orphaned the
    ``intimate_body_pointer`` paths in any existing vault note whose
    title contained one of those characters. Pin the canonical
    semantics so the helper cannot regress that way again.
    """
    from creek.classify.privacy_filter import _stub_relpath_for

    assert _stub_relpath_for("hello!world").name == "hello-world.md"
    assert _stub_relpath_for("why this matters?").name == "why-this-matters.md"
    assert _stub_relpath_for("multi!!!bang").name == "multi-bang.md"


# ---- `--tier` is mandatory, never inferred (issue #1434) ----


_LEAK_SENTINEL = (
    "Derived from an intimate fragment and must never be filed in the clear."
)


def _normalise_cli_output(text: str) -> str:
    """Strip ANSI styling and collapse whitespace runs in rendered CLI text.

    Typer renders both help and usage errors through Rich, which re-wraps to
    the terminal width and can split a single flag across style spans. Either
    makes a naive substring assertion pass — or fail — for the wrong reason,
    so every assertion below about rendered text goes through here first.

    Args:
        text: Raw ``result.output`` from :class:`typer.testing.CliRunner`.

    Returns:
        The same text with ANSI escape sequences removed and every run of
        whitespace collapsed to a single space.
    """
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def test_cli_save_refuses_provenance_without_tier_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """``--provenance`` without ``--tier`` must refuse, never infer ``open``.

    This is the #1434 fail-open regression. ``_resolve_save_tier`` returns
    ``PrivacyTier.OPEN`` whenever ``--provenance`` is supplied and the body
    did not come from stdin, so a note derived from an ``intimate`` fragment
    is filed in the clear under ``10-Liminal/Unnamed`` carrying
    ``privacy_tier: open``. No tier is ever inferred: the operator states one
    or the save refuses.

    Args:
        tmp_path: Pytest temporary directory.
    """
    vault = _scaffold_vault(tmp_path / "vault")
    (vault / "01-Fragments" / "f1.md").write_text(
        "---\n"
        "id: frag-intimate-001\n"
        "type: fragment\n"
        "privacy_tier: intimate\n"
        "---\n"
        "The intimate source material.\n",
        encoding="utf-8",
    )
    body_file = tmp_path / "answer.md"
    body_file.write_text(_LEAK_SENTINEL, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "unnamed",
            "--body",
            str(body_file),
            "--title",
            "Derived from an intimate fragment",
            "--provenance",
            "frag-intimate-001",
            "--vault",
            str(vault),
        ],
    )

    # Load-bearing: the note must not exist at all.
    assert list((vault / "10-Liminal" / "Unnamed").glob("*.md")) == [], result.output
    # And the body must not have landed anywhere else under the vault either.
    # Scoping a leak check to the one subtree you expected is the #1341
    # regression shape, so this walks the whole vault root.
    leaked = [
        path
        for path in vault.rglob("*")
        if path.is_file()
        and _LEAK_SENTINEL in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []
    assert result.exit_code == 2, result.output
    assert "--tier" in _normalise_cli_output(result.output)


_SAVE_TIER_CASES = (
    pytest.param("open", "frag-intimate-001", False, 0, True, id="tier+prov+file"),
    pytest.param("open", "frag-intimate-001", True, 0, True, id="tier+prov+stdin"),
    pytest.param("open", None, False, 0, True, id="tier+noprov+file"),
    pytest.param("open", None, True, 0, True, id="tier+noprov+stdin"),
    pytest.param(None, "frag-intimate-001", False, 2, False, id="notier+prov+file"),
    pytest.param(None, "frag-intimate-001", True, 2, False, id="notier+prov+stdin"),
    pytest.param(None, None, False, 2, False, id="notier+noprov+file"),
    pytest.param(None, None, True, 2, False, id="notier+noprov+stdin"),
)
"""The full ``--tier`` x ``--provenance`` x body-source cross-product (#1434).

Eight rows, because the tier rule must not depend on the other two axes:
whether provenance was supplied and whether the body arrived from a file or
from stdin are exactly the conditions ``_resolve_save_tier`` branches on
today. Each row carries ``(tier, provenance, from_stdin, expect_exit,
expect_note)``.
"""


@pytest.mark.parametrize(
    ("tier", "provenance", "from_stdin", "expect_exit", "expect_note"),
    _SAVE_TIER_CASES,
)
def test_save_tier_behaviour_table(
    tmp_path: Path,
    tier: str | None,
    provenance: str | None,
    from_stdin: bool,
    expect_exit: int,
    expect_note: bool,
) -> None:
    """Pin the tier contract across every combination of the other inputs.

    The rule after #1434 has no exceptions: an explicit ``--tier`` files a
    note at that tier, and an omitted ``--tier`` exits 2 having written
    nothing — whatever ``--provenance`` says and wherever the body came from.
    The four no-tier rows are the specification the fix must satisfy; today
    the file-plus-provenance row files a note instead of refusing.

    Args:
        tmp_path: Pytest temporary directory.
        tier: The ``--tier`` value, or ``None`` to omit the flag entirely.
        provenance: The ``--provenance`` value, or ``None`` to omit the flag.
        from_stdin: Send the body through stdin rather than a file.
        expect_exit: The exit code the invocation must produce.
        expect_note: Whether exactly one note may exist afterwards.
    """
    vault = _scaffold_vault(tmp_path / "vault")
    body_text = "A synthesis worth filing."
    argv = ["save", "--target", "unnamed", "--title", "Table case"]
    stdin: str | None = None
    if from_stdin:
        argv += ["--body", "-"]
        stdin = body_text
    else:
        body_file = tmp_path / "table-body.md"
        body_file.write_text(body_text, encoding="utf-8")
        argv += ["--body", str(body_file)]
    if tier is not None:
        argv += ["--tier", tier]
    if provenance is not None:
        argv += ["--provenance", provenance]
    argv += ["--vault", str(vault)]

    result = runner.invoke(app, argv, input=stdin)

    written = sorted((vault / "10-Liminal" / "Unnamed").glob("*.md"))
    if expect_note:
        assert result.exit_code == expect_exit, result.output
        assert len(written) == 1, result.output
        assert frontmatter.load(str(written[0]))["privacy_tier"] == "open"
    else:
        assert written == [], f"a note was filed with no --tier: {written}"
        assert result.exit_code == expect_exit, result.output
        # The two refusals are worded differently on purpose: the stdin shape
        # has no --body path for the operator to look at, so it names stdin.
        # Asserting the wording per branch is what keeps `came_from_stdin`
        # load-bearing — collapsing both messages into one would otherwise
        # pass every other assertion here.
        refusal = _normalise_cli_output(result.output)
        if from_stdin:
            assert "when the body comes from stdin" in refusal
        else:
            assert "never infers a tier from --provenance" in refusal


def test_save_tier_case_table_keeps_the_full_cross_product() -> None:
    """The table must keep all eight rows, and shrinking it must fail here.

    Deleting rows from a parametrize list never turns a test red — the
    deleted cases simply stop running, so privacy coverage disappears behind
    a green gate. This guard converts that deletion into a failure, and the
    id check stops two rows collapsing into one by accident.
    """
    assert len(_SAVE_TIER_CASES) == 8
    assert len({case.id for case in _SAVE_TIER_CASES}) == 8


def test_save_tier_help_and_behaviour_agree(tmp_path: Path) -> None:
    """What ``--tier`` advertises and what it does must move together.

    #1434 *is* that drift: the help promised a tier that "defaults to the
    source fragments' max tier" while the code returned ``open`` without
    reading a single fragment. Asserting the help in one test and the
    behaviour in another lets the two drift apart again, so both halves are
    coupled here. The help is normalised first because Typer re-wraps it
    across terminal lines, which would let a naive substring check pass for
    the wrong reason.

    Args:
        tmp_path: Pytest temporary directory.
    """
    help_text = _normalise_cli_output(runner.invoke(app, ["save", "--help"]).output)
    assert "defaults to" not in help_text
    assert "source fragments" not in help_text
    # Tied to _SAVE_TIER_HELP verbatim rather than a bare "Required": the
    # unscoped word also appears in unrelated help (``purge vault``), so it
    # would survive the --tier row losing its own requirement notice.
    assert "Required on every save" in help_text

    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("A synthesis worth filing.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "unnamed",
            "--body",
            str(body_file),
            "--provenance",
            "frag-intimate-001",
            "--vault",
            str(vault),
        ],
    )
    assert list((vault / "10-Liminal" / "Unnamed").glob("*.md")) == []
    assert result.exit_code == 2, result.output


# ---- An untitled save must not derive its title from private content (#1505) ----

_UNTITLED_SECRET = "CLEARTEXT-CANARY-1505"
"""The sentinel that stands in for whatever the operator's first line says.

Deliberately built out of characters ``slugify_filename`` *preserves*:
``creek/save/_slug.py:80-84`` keeps ``[\\w-]`` verbatim and does not lower-case
(the docstring at line 65 says so explicitly, and ``_INVALID_CHARS_RE`` only
touches characters outside that class). So this exact string survives the trip
through ``_compose_base_name`` into the filename. A canary containing spaces,
punctuation, or upper-case letters that the slug rewrote would make every
``in path.name`` assertion below pass for the wrong reason — vacuously green
against a filename that is still leaking, just in a different spelling.
"""

_UNTITLED_BODY = (
    f"{_UNTITLED_SECRET} is the body's first line.\n\n"
    "The rest of the answer, which nobody derives a title from."
)
"""A body whose secret sits on line 1 — the line ``_derive_title`` reads.

The mirror image of :data:`_PARADOX_BODY`, which keeps its canary *off* line 1
precisely so the #1491 battery could not be confused by this defect. Here the
canary is on line 1 on purpose: that is the whole exposure.
"""

_UNTITLED_REDACTED_BODY = "[Tier-redacted summary: (untitled)]"
"""The exact body a tier-redacted *untitled* save leaves in the vault note.

``(untitled)`` rather than a title, because
:func:`~creek.classify.privacy_filter.pre_save_filter` is handed
``request.title`` — the **raw** operator title, ``None`` here — and never the
derived one. That is why the body is already safe at HEAD and why no red
assertion in this section touches it.

Compared for equality rather than by substring, for the same reason
:data:`_REDACTED_BODY` is: a redaction that began emitting the summary
*alongside* the body would satisfy a substring check while leaking everything.
The trailing newline ``_title_only_summary`` emits is absent for the reason
recorded there — ``frontmatter.dumps`` ends on ``.strip()``.
"""

_OPERATOR_SUPPLIED_TITLE = "Both true at once"
"""An explicit title, which the fix must keep writing verbatim at every tier."""

_OPERATOR_TITLED_BODY = (
    f"A first line that is safe to look at.\n\n{_UNTITLED_SECRET} sits below the fold."
)
"""Body for the operator-titled control: canary deliberately *not* on line 1.

If the canary were on line 1 the control could not distinguish "the operator's
title was honoured" from "the derived title happened to be safe".
"""

_DERIVES_FROM_BODY: dict[PrivacyTier, bool] = {
    PrivacyTier.OPEN: True,
    PrivacyTier.UNCLASSIFIED: False,
    PrivacyTier.PERSONAL: False,
    PrivacyTier.INTIMATE: False,
}
"""Whether an untitled save may take its title from the body, per tier.

A **literal declaration of the intended behaviour**, written out by hand. It
never calls :func:`~creek.classify.privacy_filter.tier_sensitivity`, or any
other function under test, to compute its own expectations — a table derived
from the implementation agrees with the implementation by construction and can
only ever assert that the code equals itself. Four rows written down are four
decisions somebody has to argue with.

The single ``True`` row — ``OPEN`` — is a live **positive control**, not
filler. It expands to sixteen of the sixty-four cases below (eight targets
times two ``full_body`` values), so a quarter of this battery is asserting
that the derivation still *happens*. Deriving a title from the first line
is a *feature* at
``open``: it is what makes ``creek save --target thread <<< "…"`` usable
without a flag. A fix that over-reaches and returns the ``untitled <target>
<digest>`` fallback unconditionally would satisfy every leak assertion in this
file while quietly destroying that affordance, and the ``open`` rows are what
turn that outcome red instead of green.

``UNCLASSIFIED`` maps to ``False`` because
:func:`~creek.classify.privacy_filter.tier_sensitivity` ranks it ``1`` (#876):
untiered content is content nobody has vouched for. Note what this row does
**not** claim — see
:func:`test_untitled_redacted_body_is_unchanged_by_the_title_fix`.
"""

_UNTITLED_CASES: list[tuple[SaveTarget, PrivacyTier, bool]] = [
    (target, tier, full_body)
    for target in SaveTarget
    for tier in _DERIVES_FROM_BODY
    for full_body in (False, True)
]
"""Every ``(target, tier, full_body)`` an untitled save can be made under.

``full_body`` is the third axis and it is **mandatory**, not decoration. The
obvious wrong fix is a guard spelled ``if tier_sensitivity(tier) > 0 and not
request.full_body``, reasoning that ``--full-body`` is an operator opt-in to
putting the content in the vault. It is not an opt-in to putting the content in
the *filename*: ``--full-body`` relaxes ``pre_save_filter``'s body redaction at
``personal`` and nothing else. Without this axis a 32-row battery goes green
while every ``personal --full-body`` untitled save keeps writing line 1 into
the title, the filename, and — on ``ai-as-user`` — the fragment id.

The full target cross-product for the same reason :data:`_RATCHET_CASES` takes
it: #1505 is a property of the writer's title fallback, which every target
shares, so an assertion that ran on one target would be a patch on that target.
"""

_UNTITLED_IDS = [
    f"{target.value}-{tier.value}-{'fullbody' if full_body else 'summary'}"
    for target, tier, full_body in _UNTITLED_CASES
]
"""Readable node ids, one per row of :data:`_UNTITLED_CASES`, positionally parallel."""

_UNTITLED_REDACTED_CASES = (
    pytest.param(PrivacyTier.INTIMATE, False, id="intimate-summary"),
    pytest.param(PrivacyTier.INTIMATE, True, id="intimate-fullbody"),
    pytest.param(PrivacyTier.PERSONAL, False, id="personal-summary"),
    pytest.param(PrivacyTier.UNCLASSIFIED, False, id="unclassified-summary"),
)
"""The ``(tier, full_body)`` rows whose vault body is redacted *today*.

Exactly the rows :func:`~creek.classify.privacy_filter.pre_save_filter`
redacts: ``INTIMATE`` unconditionally, and ``PERSONAL`` / ``UNCLASSIFIED`` —
which rank together at ``1`` (#876/#961) — without the ``--full-body`` opt-in.
``personal --full-body`` and ``unclassified --full-body`` are excluded because
their bodies reach the vault in the clear **by the decided ``--full-body``
semantics**, not by defect, so asserting redaction there would fail for a
reason that has nothing to do with #1505.

**#1508** is the change that moved ``unclassified-summary`` into this table: it
replaced the filter's equality-on-two-members branch with a rank test read off
:data:`~creek.classify.privacy_filter._TIER_RANK`.
"""


def test_untitled_title_table_covers_every_target_tier_and_full_body() -> None:
    """The untitled table must keep every row, and shrinking it must fail here.

    Deleting rows from a ``parametrize`` list never turns a test red — the
    deleted cases simply stop running, so privacy coverage vanishes behind a
    green gate. Asserting the table's size separately converts that deletion
    into a failure.

    The axes are counted **separately, and never mixed into one set**, for the
    reason :func:`test_ratchet_table_is_not_empty` records:
    :class:`~creek.save.SaveTarget` and :class:`~creek.models.PrivacyTier` are
    both ``StrEnum``, so their members hash as bare strings *across* class
    boundaries. A single ``{*SaveTarget, *_DERIVES_FROM_BODY}`` would silently
    collapse any pair that happened to share a value, and the size assertion
    would then be measuring the collision instead of the table.

    ``set(_DERIVES_FROM_BODY) == set(PrivacyTier)`` is the row this file needs
    most. The expectations are declared per tier, so a fifth ``PrivacyTier``
    member added later would otherwise be *silently untested*: it would neither
    appear in the cross-product nor raise a ``KeyError``, and the new tier's
    untitled saves would leak with the whole battery green. Equality forces
    whoever adds it to write down a ``True`` or a ``False`` on purpose.
    """
    assert len({target.value for target in SaveTarget}) == 8
    assert len({tier.value for tier in _DERIVES_FROM_BODY}) == 4
    assert set(_DERIVES_FROM_BODY) == set(PrivacyTier)
    assert len(_UNTITLED_CASES) == 64
    assert len(set(_UNTITLED_IDS)) == 64
    # The sibling regression table below is guarded here too, for the same
    # reason: four rows that quietly became three would never announce it.
    assert len(_UNTITLED_REDACTED_CASES) == 4


@pytest.mark.parametrize(
    ("target", "tier", "full_body"),
    _UNTITLED_CASES,
    ids=_UNTITLED_IDS,
)
def test_untitled_save_derives_its_title_from_the_body_only_at_open(
    vault: Path,
    target: SaveTarget,
    tier: PrivacyTier,
    full_body: bool,
) -> None:
    """An untitled save above ``open`` must not title itself from line 1 (#1505).

    :func:`~creek.save.writer._shape_for_target` falls back to
    ``_derive_title(request.body)`` whenever no title was supplied, and that
    helper returns the body's **first non-empty line**. Above ``open`` that
    single string is then copied onto three separate surfaces, each with its
    own audience:

    1. the frontmatter ``title:`` — rendered by Obsidian's file explorer, its
       search, and every backlink pane, next to a note stamped
       ``privacy_tier: intimate``;
    2. the **filename** — ``_compose_base_name`` slugifies the title into it,
       and a directory listing is readable by anyone with the folder open,
       whatever the note says inside. It also survives into any backup, sync
       client, or ``ls`` output that never opens the file at all;
    3. on ``--target ai-as-user``, the Fragment ``id`` composed by
       ``_shape_ai_as_user_fragment`` out of the title slug — a *second*
       frontmatter copy of the leak, and the one that is the note's stable
       handle, so it is what other notes, Dataview queries and the Retrieval
       specialist quote back. A fix that cleaned only the filename would leave
       this one behind, which is why it is asserted separately.

    Deliberately *not* asserted: the vault note **body**. ``pre_save_filter``
    is handed ``request.title`` — the raw operator title, ``None`` here — and
    never the derived one, so an untitled ``intimate``/``personal`` body is
    already the literal :data:`_UNTITLED_REDACTED_BODY` at HEAD. A body
    assertion here would be green before the fix and would misreport this
    battery's meaning; it is pinned as a regression instead, further down.

    Every claim compares ``is expected`` against :data:`_DERIVES_FROM_BODY`
    rather than asserting a bare ``not in``. Identity against a declared
    expectation is what makes the ``open`` rows a **positive control**: on
    those rows the canary must be *present*, so a fallback that fires
    unconditionally turns them red instead of sailing through.

    The filename claim is only meaningful because ``slugify_filename``
    preserves case and keeps every ``[\\w-]`` character
    (``creek/save/_slug.py:80-84``) — see :data:`_UNTITLED_SECRET`.

    Args:
        vault: Minimal vault scaffold.
        target: One :class:`~creek.save.SaveTarget`.
        tier: The tier the caller states.
        full_body: The ``--full-body`` opt-in, which must not relax the guard.
    """
    expected = _DERIVES_FROM_BODY[tier]

    path = save_to_vault(
        _make_request(
            target,
            body=_UNTITLED_BODY,
            title=None,
            tier=tier,
            full_body=full_body,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert (_UNTITLED_SECRET in str(post["title"])) is expected, (
        f"frontmatter title of an untitled {target.value} save at "
        f"{tier.value} (full_body={full_body}) is {post['title']!r}; "
        f"carrying the body's first line should be {expected}"
    )
    assert (_UNTITLED_SECRET in path.name) is expected, (
        f"the filename of an untitled {target.value} save at {tier.value} "
        f"(full_body={full_body}) is {path.name!r}; carrying the body's "
        f"first line should be {expected}"
    )
    if target is SaveTarget.AI_AS_USER:
        assert (_UNTITLED_SECRET in str(post["id"])) is expected, (
            "the fragment id of an untitled ai-as-user save at "
            f"{tier.value} (full_body={full_body}) is {post['id']!r} — "
            "`creek index` copies this id into the vault-wide "
            ".id-index.jsonl, which is read at the open tier"
        )


def test_untitled_intimate_fallback_title_has_a_fixed_shape(vault: Path) -> None:
    """The replacement title must be mechanical, and pinned to *this* shape (#1505).

    Absence is not enough. "Do not use the first line" leaves open a family of
    plausible-sounding successors that are the same bug with a shorter
    reach — the first *word*, the longest noun phrase, an extracted keyword,
    a one-line LLM summary. Each of those still derives the title from the
    protected content and would satisfy a test that only checked the canary
    was gone, because a canary is one token and a summary can leak a sentence
    without repeating it.

    So the shape is pinned instead: a fixed literal, the target's own name,
    and eight hex digits of a content digest. Nothing in that grammar has room
    for a word of the body. It stays compatible with the digest the fix uses
    for disambiguation — see
    :func:`test_untitled_intimate_saves_disambiguate_rather_than_collide` — so
    the two tests constrain the same string from opposite directions rather
    than fighting.

    ``fullmatch`` rather than ``search``: a prefixed or suffixed title would
    otherwise pass while carrying arbitrary text alongside the safe part.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=_UNTITLED_BODY,
            title=None,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    title = str(frontmatter.load(str(path))["title"])
    assert re.fullmatch(r"untitled [a-z-]+ [0-9a-f]{8}", title), (
        f"an untitled intimate save titled itself {title!r}; the fallback "
        "must be 'untitled <target> <8 hex digits>' and nothing else"
    )


def test_untitled_intimate_saves_disambiguate_rather_than_collide(
    vault: Path,
) -> None:
    """Two untitled intimate saves on one day must not fight over one filename.

    A shape guard on the fix rather than a #1505 red row: it passes at HEAD
    (where distinct first lines already yield distinct slugs) and must keep
    passing afterwards. What it forbids is the *simplification* — dropping the
    content digest for a bare ``untitled <target>``, whose filename stem is
    then identical for every untitled save of that target on a given day.

    That is not a cosmetic outcome. ``_compose_base_name`` already prefixes the
    date, so a digest-free fallback makes ``_atomic_create`` walk ``-1``,
    ``-2``, … on every subsequent save, and at
    :data:`~creek.save.writer._MAX_COLLISION_RETRIES` (``writer.py:49``, 1000)
    it **raises** ``RuntimeError`` — an intimate save refused outright, whose
    body is already sitting in a compost stub the vault note will now never
    point at. Silently slower, then abruptly lossy.

    Three saves, because two cannot separate "distinct bodies get distinct
    names" from "identical bodies still get filed". Bodies one and three are
    byte-identical, so the digest is identical too, so the third *must* land
    on the collision suffix rather than overwrite or vanish.

    The stem is also asserted to be the first's plus exactly ``-1``, which
    pins that the date is not embedded a second time inside the title: a
    fallback spelled ``untitled <target> <date>`` would produce
    ``2026-08-13-untitled-thread-2026-08-13.md``, and comparing whole stems is
    what makes that visible instead of a substring check that shrugs.

    Args:
        vault: Minimal vault scaffold.
    """
    stems = [
        save_to_vault(
            _make_request(
                SaveTarget.THREAD,
                body=body,
                title=None,
                tier=PrivacyTier.INTIMATE,
            ),
            vault_path=vault,
        ).stem
        for body in ("alpha one", "beta two", "alpha one")
    ]

    assert stems[0] != stems[1], (
        "two untitled intimate saves with different bodies share the stem "
        f"{stems[0]!r}; every later save now pays a collision walk"
    )
    assert stems[2] == f"{stems[0]}-1", (
        f"the repeat of the first body landed at {stems[2]!r}, expected "
        f"{stems[0]}-1 — the collision suffix is the only thing standing "
        "between identical untitled saves and a RuntimeError"
    )


@pytest.mark.parametrize(("tier", "full_body"), _UNTITLED_REDACTED_CASES)
def test_untitled_redacted_body_is_unchanged_by_the_title_fix(
    vault: Path,
    tier: PrivacyTier,
    full_body: bool,
) -> None:
    """Fixing the title must not disturb the body redaction (regression, not red).

    Green before the #1505 fix and green after. It exists because the fix
    changes the string that flows into ``_shape_for_target``, and the same
    request object also feeds :func:`~creek.classify.privacy_filter.pre_save_filter`
    — so the tempting "tidy-up" of routing the *derived* title into the filter
    as well would rewrite this body from ``(untitled)`` to the first line of
    the content, turning the summary itself into the leak. Equality, not
    substring, for the reason :data:`_REDACTED_BODY` records.

    **What this table covers.** ``intimate`` unconditionally, plus
    ``personal`` and ``unclassified`` without the ``--full-body`` opt-in.
    ``unclassified`` joined it in **#1508**, which replaced
    ``pre_save_filter``'s equality-on-two-members branch with a rank test read
    off :data:`~creek.classify.privacy_filter._TIER_RANK` — so
    ``tier_sensitivity(tier) == 1`` now redacts whichever tier carries that
    rank, instead of only the one named ``personal``. The
    ``unclassified-summary`` row is therefore red until that rank test lands
    and green from then on, while the other three rows stay green throughout —
    deliberately, because a privacy defect with a test defending it is a defect
    nobody can close.

    Args:
        vault: Minimal vault scaffold.
        tier: A tier whose untitled body is redacted today.
        full_body: The ``--full-body`` opt-in.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=_UNTITLED_BODY,
            title=None,
            tier=tier,
            full_body=full_body,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert post.content == _UNTITLED_REDACTED_BODY, (
        f"an untitled {tier.value} save (full_body={full_body}) wrote "
        f"{post.content!r} into the vault note; the redacted summary must "
        f"stay exactly {_UNTITLED_REDACTED_BODY!r}"
    )


def test_untitled_intimate_still_lands_the_full_body_in_the_stub(
    vault: Path,
) -> None:
    """Suppressing the title must not drop the operator's answer (#1505).

    The failure mode a privacy battery is most likely to reward by accident:
    every absence assertion in this section is satisfied just as well by a
    save that wrote nothing at all. So the body is asserted **present**, in
    full, at the one place it is allowed to be.

    The stub stem is ``intimate-<digest>`` rather than a bare ``intimate``:
    since #1509 an untitled save is addressed by a digest of its own body,
    because reading the raw title made **every** untitled save in a vault
    land on one stem and climb ``_atomic_create``'s counter ladder until
    ``_MAX_COLLISION_RETRIES`` raised — losing the save outright, since the
    stub is written before the vault note. Nothing renames existing stubs,
    so pointers already written into vault notes still resolve; that
    compatibility promise is pinned by
    :func:`test_a_pre_change_intimate_stub_is_neither_renamed_nor_clobbered`.

    The assertion is deliberately made against the note's own pointer
    rather than a hardcoded digest, so it states the contract that matters
    — the note resolves to its body — without pinning the hash function.

    Args:
        vault: Minimal vault scaffold.
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=_UNTITLED_BODY,
            title=None,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    present = _stub_files(vault)
    assert len(present) == 1, f"expected exactly one stub, found {present}"
    stub = present[0]
    assert stub.name.startswith("intimate-"), (
        f"the untitled stub is not body-addressed: {stub.name}"
    )
    assert not _UNTITLED_COUNTER_SUFFIX_RE.search(stub.name), (
        f"the untitled save fell back to the counter ladder: {stub.name}"
    )
    assert _UNTITLED_SECRET in stub.read_text(encoding="utf-8"), (
        "the intimate body reached neither the vault note nor the stub — the "
        "operator's answer was lost rather than protected"
    )
    pointer = frontmatter.load(str(path))["saved_from"].get("intimate_body_pointer")
    assert pointer == str(INTIMATE_STUB_RELPATH / stub.name), (
        f"the note points at {pointer!r}, which is not where the body went"
    )


@pytest.mark.parametrize("tier", list(PrivacyTier))
def test_operator_title_is_still_written_verbatim_at_every_tier(
    vault: Path,
    tier: PrivacyTier,
) -> None:
    """An explicit ``--title`` is intended behaviour at every tier (AC #2, #1505).

    The other half of the contract, and the reason the fix keys on "no title
    was supplied" rather than on the tier alone. A title the operator typed is
    a string they chose to put in a filename; #1505 is about a string the
    *writer* chose for them out of content they were protecting. Redacting the
    former would be a different bug wearing this one's fix, and it would land
    at ``intimate`` — precisely where an over-cautious patch is most tempting.

    Both surfaces are checked, because the title reaches them by two different
    routes and a fix could break either alone: the frontmatter through
    ``_shape_for_target``, the filename through ``_compose_base_name``.

    The expected slug is written out as a literal rather than computed by
    calling ``slugify_filename``. Calling it would make the assertion agree
    with the slugifier by construction and would no longer notice a fix that
    started lower-casing or stripping the operator's title on its way to disk;
    the literal preserves the case ``creek/save/_slug.py:65`` promises.

    Args:
        vault: Minimal vault scaffold.
        tier: Every :class:`~creek.models.PrivacyTier`, ``unclassified``
            included — it is reachable from ``--tier`` (see
            :data:`_RATCHET_TIERS`).
    """
    path = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=_OPERATOR_TITLED_BODY,
            title=_OPERATOR_SUPPLIED_TITLE,
            tier=tier,
        ),
        vault_path=vault,
    )

    post = frontmatter.load(str(path))
    assert post["title"] == _OPERATOR_SUPPLIED_TITLE, (
        f"a {tier.value} save with an explicit title was filed as "
        f"{post['title']!r} — the operator's own words were rewritten"
    )
    assert "Both-true-at-once" in path.name, (
        f"a {tier.value} save with an explicit title landed at {path.name!r}; "
        "the operator's title must still slugify into the filename"
    )
    assert _UNTITLED_SECRET not in path.name


# ---- untitled intimate stubs are body-addressed, not counter-laddered (#1509) ----


_UNTITLED_COUNTER_SUFFIX_RE = re.compile(r"-\d+\.md$")
"""Matches the ``_atomic_create`` counter ladder (``intimate-1.md``).

Untitled saves must not reach it: before #1509 every untitled intimate
save in a vault's lifetime targeted the single stem ``intimate`` and
climbed this ladder until ``_MAX_COLLISION_RETRIES`` raised, losing the
save entirely at attempt 1000 (``writer.py:117`` writes the stub before
the vault note, so that abort is lossy).
"""


def test_two_untitled_intimate_saves_land_in_distinct_stubs(vault: Path) -> None:
    """Different untitled bodies get different stub filenames (#1509).

    Asserts the FILENAMES, not merely pointer resolution: the pointer
    already resolves correctly today, because ``save_to_vault`` derives
    it from ``_atomic_create``'s returned path. What was broken is the
    naming, so a test that only checked pointers would be vacuous.

    Args:
        vault: Minimal vault scaffold.
    """
    first = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=f"{_UNTITLED_SECRET} first body.\n",
            title=None,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )
    second = save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=f"{_UNTITLED_SECRET} a wholly different body.\n",
            title=None,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    stubs = sorted(p.name for p in _stub_files(vault))
    assert len(stubs) == 2, f"expected two distinct stubs, found {stubs}"
    assert not any(_UNTITLED_COUNTER_SUFFIX_RE.search(name) for name in stubs), (
        f"an untitled save fell back to the counter ladder: {stubs}"
    )
    pointers = {
        frontmatter.load(str(path))["saved_from"].get("intimate_body_pointer")
        for path in (first, second)
    }
    assert len(pointers) == 2, f"both notes point at the same stub: {pointers}"
    for pointer in pointers:
        assert (vault / str(pointer)).exists(), f"dangling pointer {pointer!r}"


def test_many_untitled_intimate_saves_never_reach_the_counter_ladder(
    vault: Path,
) -> None:
    """N distinct untitled bodies produce N stubs, none counter-suffixed.

    Args:
        vault: Minimal vault scaffold.
    """
    count = 12
    for index in range(count):
        save_to_vault(
            _make_request(
                SaveTarget.THREAD,
                body=f"{_UNTITLED_SECRET} body number {index}.\n",
                title=None,
                tier=PrivacyTier.INTIMATE,
            ),
            vault_path=vault,
        )

    names = sorted(p.name for p in _stub_files(vault))
    assert len(names) == count, f"expected {count} stubs, found {names}"
    laddered = [n for n in names if _UNTITLED_COUNTER_SUFFIX_RE.search(n)]
    assert not laddered, f"these untitled saves laddered onto one stem: {laddered}"


def test_byte_identical_untitled_bodies_still_ladder(vault: Path) -> None:
    """Identical bodies are a genuine clash and must still use the counter.

    Body-addressed naming must not make ``_MAX_COLLISION_RETRIES``
    unreachable — that guard is what stops an unbounded scan, and its own
    test would go vacuous if nothing could ever collide again (#1509).

    Args:
        vault: Minimal vault scaffold.
    """
    for _ in range(2):
        save_to_vault(
            _make_request(
                SaveTarget.THREAD,
                body=_UNTITLED_BODY,
                title=None,
                tier=PrivacyTier.INTIMATE,
            ),
            vault_path=vault,
        )

    names = sorted(p.name for p in _stub_files(vault))
    assert len(names) == 2, f"expected a stem and its -1 ladder step, got {names}"
    assert any(_UNTITLED_COUNTER_SUFFIX_RE.search(n) for n in names), (
        f"identical bodies did not collide, so the retry guard is unreachable: {names}"
    )


def test_a_pre_change_intimate_stub_is_neither_renamed_nor_clobbered(
    vault: Path,
) -> None:
    """An existing ``intimate.md`` stub survives a new untitled save (#1509).

    Nothing migrates pointers, so a stub written before this change must
    keep its name and its bytes: purge resolves stored pointer strings
    literally.

    Args:
        vault: Minimal vault scaffold.
    """
    legacy_dir = vault / INTIMATE_STUB_RELPATH
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy = legacy_dir / "intimate.md"
    legacy.write_text("a body saved before #1509\n", encoding="utf-8")
    legacy_bytes = legacy.read_bytes()

    save_to_vault(
        _make_request(
            SaveTarget.THREAD,
            body=f"{_UNTITLED_SECRET} brand new body.\n",
            title=None,
            tier=PrivacyTier.INTIMATE,
        ),
        vault_path=vault,
    )

    assert legacy.exists(), "the pre-change stub was renamed away"
    assert legacy.read_bytes() == legacy_bytes, "the pre-change stub was rewritten"


def test_a_title_that_slugifies_to_nothing_is_also_body_addressed(
    vault: Path,
) -> None:
    """An all-punctuation title must not land back on the bare stem (#1509).

    ``"!!!"`` is non-empty after ``.strip().lower()`` but ``slugify_filename``
    reduces it to ``""``, so gating on the raw title left this input falling
    through to the bare ``intimate`` stem — the same collision, through a
    narrower door. Found in review of the first fix.

    Args:
        vault: Minimal vault scaffold.
    """
    for punctuation in ("!!!", "---", "..."):
        save_to_vault(
            _make_request(
                SaveTarget.THREAD,
                body=f"{_UNTITLED_SECRET} body for {punctuation}.\n",
                title=punctuation,
                tier=PrivacyTier.INTIMATE,
            ),
            vault_path=vault,
        )

    names = sorted(p.name for p in _stub_files(vault))
    assert len(names) == 3, f"expected three distinct stubs, found {names}"
    assert "intimate.md" not in names, (
        f"a punctuation-only title landed on the bare stem: {names}"
    )
    assert not any(_UNTITLED_COUNTER_SUFFIX_RE.search(n) for n in names), (
        f"a punctuation-only title fell back to the counter ladder: {names}"
    )
