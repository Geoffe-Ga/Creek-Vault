"""Tests for ``creek save`` (FEAT-009).

Covers the destination router, the frontmatter writer, the
``pre_save_filter`` helper added to ``creek/classify/privacy_filter.py``,
and the ``creek save`` CLI command itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import pre_save_filter
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


def test_paradox_tier_is_open_even_if_caller_passes_intimate(vault: Path) -> None:
    """Per the FEAT, a paradox save records *the fact* of the contradiction.

    Tier-filtering for the paradox target is forced to ``open`` so the
    body lands in the vault unredacted — what's preserved is the
    contradiction, not a tier-protected summary. This is a deliberate
    privacy trade-off the operator opts into by choosing ``paradox``;
    the CLI emits a stderr warning when ``--tier intimate`` (or
    ``personal``) is combined with ``--target paradox`` so the
    behaviour isn't silent.
    """
    paradox_body = "Both A and not-A appear true under different framings."
    request = _make_request(
        SaveTarget.PARADOX,
        body=paradox_body,
        tier=PrivacyTier.INTIMATE,
    )
    path = save_to_vault(request, vault_path=vault)
    post = frontmatter.load(str(path))
    assert post["type"] == "paradox"
    assert post["privacy_tier"] == "open"
    # Force-to-open must actually let the body through, otherwise the
    # rule would be a no-op: assert the body content lands in the
    # vault note rather than just trusting the tier field.
    assert paradox_body in post.content
    # And nothing gets diverted to the intimate-stubs directory.
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    assert not list(stubs_dir.glob("*.md"))


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
    """When ``--title`` is omitted, the first non-empty body line becomes it."""
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


def test_cli_save_paradox_intimate_warns_about_tier_widening(tmp_path: Path) -> None:
    """``--target paradox --tier intimate`` warns that the body is unprotected.

    Paradox saves force tier=open per the FEAT — the body lands in
    the vault unredacted. A user passing ``--tier intimate`` for
    protection deserves a visible heads-up rather than silent
    widening; the CLI emits a yellow stderr note explaining the
    consequence and pointing at unprotected target alternatives.
    """
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("Two contradictory framings.", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "paradox",
            "--body",
            str(body_file),
            "--title",
            "Both true",
            "--provenance",
            "frag-001",
            "--tier",
            "intimate",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "tier=open" in result.output.lower() or "widened" in result.output.lower()
    # Open-tier paradox: nothing diverted to the intimate-stubs dir.
    stubs_dir = vault / "10-Liminal" / "Compost" / "intimate-stubs"
    assert not list(stubs_dir.glob("*.md"))


def test_cli_save_paradox_open_does_not_warn(tmp_path: Path) -> None:
    """``--target paradox --tier open`` is the expected path; no warning."""
    vault = _scaffold_vault(tmp_path / "vault")
    body_file = tmp_path / "answer.md"
    body_file.write_text("contradiction body", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "save",
            "--target",
            "paradox",
            "--body",
            str(body_file),
            "--provenance",
            "frag-001",
            "--tier",
            "open",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "widened" not in result.output.lower()


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

    monkeypatch.setattr(writer_module.os, "open", always_exists)
    with pytest.raises(RuntimeError) as excinfo:
        writer_module._atomic_create(vault, "stuck", "content")
    message = str(excinfo.value)
    assert "stuck.md" in message
    assert str(writer_module._MAX_COLLISION_RETRIES) in message


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
