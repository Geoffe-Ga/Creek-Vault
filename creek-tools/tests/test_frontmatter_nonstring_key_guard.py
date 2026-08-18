"""A non-string YAML frontmatter key must not abort a vault walk (#1475, #924).

``frontmatter.load``/``loads`` end in ``Post(content, handler, **metadata)``.
The splat means a header whose mapping carries a **non-string key** — a bare
YAML date (``2024-05-01:``), a bool (``true:``), or an int (``1:``), all three
valid ``SafeLoader`` output and all three plausible in a hand-authored Obsidian
note — raises a builtin ``TypeError: keywords must be strings``. That is not an
``OSError``, not a ``ValueError``, and not a ``yaml.YAMLError``, so it escaped
every guard tuple in the tree.

These tests pin the two halves of the fix:

* metadata-only readers moved to :func:`creek.vault.links.read_header_meta`,
  which parses the header with ``yaml.safe_load`` and never splats, so the
  crash is structurally impossible rather than merely caught;
* readers that need the body widened their guard to
  :data:`creek.vault.reader.FRONTMATTER_LOAD_ERRORS` **around the load
  statement only**, so a genuine programming-error ``TypeError`` raised by
  validation or model code still propagates.

Every whole-directory walk asserts a **non-zero** count of surviving good
files. Without that positive control an empty walk — a mis-built fixture, a
subtree the loader does not visit — would satisfy "the bad file did not crash
it" vacuously.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

import frontmatter
import pytest

from creek.models import (
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.writer import VaultWriter
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

# The three headers that crash ``frontmatter.load``. Each is a *whole*
# frontmatter block so a fixture can be written verbatim.
NONSTRING_KEY_HEADERS: Final[dict[str, str]] = {
    "date_key": "2024-05-01: reflection\n",
    "bool_key": "true: yes\n",
    "int_key": "1: x\n",
}

_HEADER_IDS: Final[tuple[str, ...]] = tuple(NONSTRING_KEY_HEADERS)


def _write_note(path: Path, header_body: str, *, body: str = "note body\n") -> Path:
    """Write a markdown note whose frontmatter is *header_body* verbatim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header_body}---\n{body}", encoding="utf-8")
    return path


def _scatter_bad_notes(directory: Path, *, prefix: str = "bad") -> list[Path]:
    """Write one note per non-string-key shape into *directory*."""
    return [
        _write_note(directory / f"{prefix}-{name}.md", header)
        for name, header in NONSTRING_KEY_HEADERS.items()
    ]


def _good_fragment(index: int) -> Fragment:
    """Return a valid fragment whose id is stable across runs."""
    return Fragment(
        id=f"frag-nonstr{index:06d}",
        title=f"Readable {index}",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=PrivacyTier.OPEN,
    )


# ---- The upstream behaviour these guards exist for --------------------------


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_frontmatter_load_still_raises_bare_typeerror(
    tmp_path: Path,
    header_name: str,
) -> None:
    """Pin the upstream defect: the splat raises ``TypeError``, not a subclass.

    If python-frontmatter ever stops splatting, this test fails loudly and the
    widened guards below can be reconsidered — rather than silently becoming
    dead defensive padding nobody can justify.
    """
    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS[header_name])
    with pytest.raises(TypeError, match="keywords must be strings"):
        frontmatter.load(str(note))


# ---- The shared fragment reader (the P1 blast radius) -----------------------


def test_try_load_fragment_still_propagates_typeerror(tmp_path: Path) -> None:
    """The shared loader keeps raising, so each caller picks its own tolerance.

    Swallowing here would force the HARD privacy gate in
    :mod:`creek.author.checks` to the same silent-skip policy as a lint check.
    """
    from creek.vault.reader import try_load_fragment

    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS["date_key"])
    with pytest.raises(TypeError):
        try_load_fragment(note)


def test_iter_vault_fragments_survives_nonstring_key_notes(tmp_path: Path) -> None:
    """One hand-edited note must not abort the shared corpus walk."""
    from creek.vault.reader import iter_vault_fragments

    vault = tmp_path / "vault"
    for index in range(2):
        write_fragment_file(vault=vault, fragment=_good_fragment(index), body="hello")
    fragments_root = vault / "01-Fragments"
    _scatter_bad_notes(fragments_root / "Notes")

    loaded = iter_vault_fragments(fragments_root)

    # Positive control: the walk really did reach the good files.
    assert len(loaded) == 2
    assert {frag.id for _path, frag, _body, _raw in loaded} == {
        _good_fragment(0).id,
        _good_fragment(1).id,
    }


def test_frontmatter_load_errors_covers_the_four_unreadable_shapes() -> None:
    """The shared tuple names ``TypeError`` alongside the house three."""
    import yaml

    from creek.vault.reader import FRONTMATTER_LOAD_ERRORS

    assert set(FRONTMATTER_LOAD_ERRORS) == {
        OSError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    }


# ---- Vault writer -----------------------------------------------------------


@pytest.fixture()
def writer_vault(tmp_path: Path) -> Path:
    """Create the minimal vault tree :class:`VaultWriter` requires."""
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_rebuild_index_survives_nonstring_key_sibling(writer_vault: Path) -> None:
    """A corrupt neighbour must not take down id lookup for the directory."""
    writer = VaultWriter(vault_path=writer_vault)
    fragment = _good_fragment(0)
    written = writer.write_fragment(fragment, body="hello")
    _scatter_bad_notes(written.parent)
    # Drop the id index so the next lookup has to rebuild it by scanning.
    (written.parent / ".id-index.jsonl").unlink(missing_ok=True)

    found = writer.find_fragment(fragment.id)

    # Positive control: the rebuild scanned real files, not an empty directory.
    assert found is not None
    assert found.name == written.name


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_load_post_or_report_names_the_unreadable_file(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The write paths' loader fails loudly *and* says which file is at fault.

    ``update_fragment`` / ``tomb_fragment`` / ``restore_fragment`` need the
    body, so they cannot use ``read_header_meta``, and they must not adopt the
    read paths' skip-and-continue policy — silently declining to update a
    fragment loses an operator's edit. What changes is the diagnostic: a bare
    ``TypeError: keywords must be strings`` with no path becomes a
    ``ValueError`` naming the file.
    """
    from creek.vault.writer import _load_post_or_report

    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS[header_name])

    with pytest.raises(ValueError, match=re.escape(str(note))):
        _load_post_or_report(note)


def test_load_post_or_report_returns_the_document(tmp_path: Path) -> None:
    """Positive control: the same loader still returns header *and* body."""
    from creek.vault.writer import _load_post_or_report

    note = _write_note(tmp_path / "good.md", "id: frag-x\n", body="the body\n")

    post = _load_post_or_report(note)

    assert post["id"] == "frag-x"
    assert post.content.strip() == "the body"


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_corrupt_fragment_goes_invisible_rather_than_crashing(
    writer_vault: Path,
    header_name: str,
) -> None:
    """Characterisation of the surviving behaviour, and of its known cost.

    Before this change a fragment file carrying a non-string frontmatter key
    took down :meth:`~creek.vault.writer.VaultWriter._rebuild_index`, and with
    it every id lookup in that directory. Now the directory keeps working and
    only the corrupt file is lost to the index.

    "Lost to the index" is not a happy ending: the id it declares no longer
    resolves, so the next ``write_fragment`` writes a duplicate. That is
    **issue #1543**, deliberately not fixed here — the obvious remedy
    (``read_header_meta`` in the verifier) is a measured 6x regression on a
    per-index-hit path, and the right fix is the bounded byte-scan for ``id``
    that ``_find_in_dir_locked``'s docstring already names. This test exists so
    that behaviour cannot drift silently while #1543 is open.
    """
    writer = VaultWriter(vault_path=writer_vault)
    fragment = _good_fragment(0)
    written = writer.write_fragment(fragment, body="hello")
    _write_note(
        written,
        f"id: {fragment.id}\ntype: fragment\n" + NONSTRING_KEY_HEADERS[header_name],
    )

    assert writer.find_fragment(fragment.id) is None
    assert writer.update_fragment(fragment, body="new") is None
    assert writer.tomb_fragment(fragment.id) is None
    assert writer.restore_fragment(fragment) is None


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_file_declares_id_is_false_for_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The id verifier answers "no" rather than raising."""
    from creek.vault.writer import _file_declares_id

    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS[header_name])

    assert _file_declares_id(note, "frag-nonstr000000") is False


# ---- The "load a Post or None" helpers across the package -------------------

_PostLoader = Callable[["Path"], object]


def _post_loaders() -> list[tuple[str, _PostLoader]]:
    """Return every ``(label, callable)`` that loads one note into a Post."""
    from creek.compile import engine as compile_engine
    from creek.generate import (
        compost_scan,
        decisions,
        mining,
        skills,
        state,
        wavelength,
    )
    from creek.generate import unnamed as unnamed_mod
    from creek.generate import voice as voice_mod
    from creek.lint.checks import voice_fidelity

    return [
        ("state._safe_post", state._safe_post),
        ("mining._safe_post", mining._safe_post),
        ("compost_scan._safe_post", compost_scan._safe_post),
        ("wavelength._safe_post", wavelength._safe_post),
        ("decisions._load_post", decisions._load_post),
        (
            "skills._safe_load_post",
            lambda path: skills._safe_load_post(path, label="skill"),
        ),
        ("unnamed._load_fragment", unnamed_mod._load_fragment),
        (
            "voice._load_fragment_with_body",
            voice_mod._load_fragment_with_body,
        ),
        (
            "compile._load_existing_provenance",
            compile_engine._load_existing_provenance,
        ),
        (
            "voice_fidelity._scan_draft",
            lambda path: _scan_voice_fidelity_draft(voice_fidelity, path),
        ),
    ]


def _scan_voice_fidelity_draft(module: object, path: Path) -> object:
    """Call ``voice_fidelity._scan_draft`` with a throwaway fingerprint/config."""
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.model import VoiceFingerprint

    return module._scan_draft(  # type: ignore[attr-defined]
        path,
        VoiceFingerprint(),
        AIStyleConfig(),
    )


@pytest.mark.parametrize("header_name", _HEADER_IDS)
@pytest.mark.parametrize(
    ("label", "loader"),
    _post_loaders(),
    ids=[label for label, _loader in _post_loaders()],
)
def test_post_loaders_skip_nonstring_key_notes(
    tmp_path: Path,
    header_name: str,
    label: str,
    loader: _PostLoader,
) -> None:
    """Every per-note loader returns its "unreadable" sentinel, never raises."""
    note = _write_note(
        tmp_path / f"{label.replace('.', '-')}-{header_name}.md",
        NONSTRING_KEY_HEADERS[header_name],
    )

    result = loader(note)

    assert result in (None, [], ()), f"{label} should skip, got {result!r}"


def test_post_loaders_positive_control(tmp_path: Path) -> None:
    """The same loaders still return something for a well-formed note.

    Without this, a loader that returned ``None`` unconditionally would pass
    the skip test above and the whole battery would be vacuous.
    """
    from creek.generate import compost_scan, mining, state, wavelength

    note = _write_note(tmp_path / "good.md", "type: fragment\ntitle: Fine\n")
    for label, loader in (
        ("state._safe_post", state._safe_post),
        ("mining._safe_post", mining._safe_post),
        ("compost_scan._safe_post", compost_scan._safe_post),
        ("wavelength._safe_post", wavelength._safe_post),
    ):
        assert loader(note) is not None, f"{label} lost a readable note"


# ---- Metadata-only readers now on read_header_meta --------------------------


def test_lint_paradox_load_fragments_survives(tmp_path: Path) -> None:
    """The paradox lint walk keeps its good fragments and drops the bad note."""
    from creek.lint.checks.paradox import _load_fragments

    fragments_dir = tmp_path / "01-Fragments"
    for index in range(2):
        write_fragment_file(vault=tmp_path, fragment=_good_fragment(index), body="hi")
    _scatter_bad_notes(fragments_dir / "Notes")

    loaded = _load_fragments(tmp_path)

    assert len(loaded) == 2


def test_lint_compost_check_survives(tmp_path: Path) -> None:
    """``creek lint``'s compost check reports the good notes and skips the bad."""
    from creek.lint.checks.compost import run

    compost_dir = tmp_path / "10-Liminal" / "Compost"
    _write_note(compost_dir / "good.md", "type: compost\ntitle: Kept\n")
    _scatter_bad_notes(compost_dir)

    result = run(tmp_path)

    assert result.findings == ["- `Kept` (`10-Liminal/Compost/good.md`)"]


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_draft_grounding_scan_skips_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """A malformed draft is treated as clean rather than aborting the check."""
    from creek.config import DraftConfig
    from creek.lint.checks.draft_grounding import _scan_draft

    note = _write_note(tmp_path / "draft.md", NONSTRING_KEY_HEADERS[header_name])

    assert _scan_draft(note, DraftConfig()) is None


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_recorded_paradox_pair_skips_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """A paradox note with a date key records no pair instead of crashing."""
    from creek.generate.paradox import _recorded_pair

    note = _write_note(tmp_path / "paradox.md", NONSTRING_KEY_HEADERS[header_name])

    assert _recorded_pair(note) is None


def test_recorded_paradox_pair_positive_control(tmp_path: Path) -> None:
    """The same reader still recovers a real recorded pair."""
    from creek.generate.paradox import _recorded_pair

    note = _write_note(
        tmp_path / "paradox.md",
        "fragments:\n  - '[[frag-a]]'\n  - '[[frag-b]]'\n",
    )

    assert _recorded_pair(note) == frozenset({"frag-a", "frag-b"})


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_voice_sample_manifest_skips_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """An unreadable register summary prunes nothing rather than raising."""
    from creek.generate.voice import _SUMMARY_FILENAME, _read_sample_manifest

    _write_note(tmp_path / _SUMMARY_FILENAME, NONSTRING_KEY_HEADERS[header_name])

    assert _read_sample_manifest(tmp_path) == frozenset()


# ---- Directory scans that must not lose their run ---------------------------


def test_compost_note_and_thread_scans_survive(tmp_path: Path) -> None:
    """Both compost-report scans keep their good notes."""
    from creek.generate.compost import CompostTracker

    compost_dir = tmp_path / "compost"
    _write_note(compost_dir / "good.md", "type: compost\ntitle: Kept\n")
    _scatter_bad_notes(compost_dir)
    notes = CompostTracker._load_existing_compost_notes(
        compost_dir,
        compost_dir / "_Compost-Report.md",
    )
    assert notes == [("Kept", "good")]

    threads_dir = tmp_path / "threads"
    _write_note(
        threads_dir / "t.md",
        "type: thread\nstatus: active\ntitle: Live\nid: thread-1\n",
    )
    _scatter_bad_notes(threads_dir)
    assert CompostTracker._load_active_threads(threads_dir) == [
        ("Live", "thread-1"),
    ]


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_recorded_compost_source_skips_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The compost note-path resolver steps over a note it cannot parse."""
    from creek.generate.compost import _recorded_compost_source

    note = _write_note(tmp_path / "note.md", NONSTRING_KEY_HEADERS[header_name])

    assert _recorded_compost_source(note) is None


def test_compiled_page_load_survives(tmp_path: Path) -> None:
    """The compiled-page router keeps loading pages past a corrupt neighbour."""
    from creek.generate.compile_routing import _load_pages

    root = tmp_path / "02-Threads"
    _write_note(
        root / "good.md",
        "type: compiled_page\ntarget_kind: thread\ntarget_id: thread-1\n"
        "title: Compiled\n",
    )
    _scatter_bad_notes(root)

    pages = _load_pages(root, "thread")

    assert set(pages) == {"thread-1"}


# ---- The HARD privacy gate --------------------------------------------------


def test_cited_tier_scan_survives_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The leak gate keeps walking, but says out loud which file it dropped.

    Widening this guard converts a crash into a silent skip, and a silent skip
    in a HARD gate is a fail-open (#926). The WARNING is the mitigation this
    PR ships: the operator learns a cited-tier scan was incomplete.
    """
    from creek.author.checks import _scan_subtree_for_cited

    root = tmp_path / "01-Fragments"
    write_fragment_file(vault=tmp_path, fragment=_good_fragment(0), body="secret text")
    bad = _scatter_bad_notes(root / "Notes")

    resolved: dict[str, object] = {}
    with caplog.at_level("WARNING"):
        _scan_subtree_for_cited(root, {_good_fragment(0).id}, resolved)  # type: ignore[arg-type]

    # Positive control: the good fragment was still resolved.
    assert set(resolved) == {_good_fragment(0).id}
    assert any(bad[0].name in record.getMessage() for record in caplog.records)


# ---- Classify entry points --------------------------------------------------


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_classify_load_classifiable_fragment_records_the_error(
    tmp_path: Path,
    header_name: str,
) -> None:
    """Classify records an ``errors`` entry instead of aborting the run."""
    from creek.classify.classify_engine import _load_classifiable_fragment, _RunCounts

    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS[header_name])
    counts = _RunCounts()

    assert _load_classifiable_fragment(md_file=note, counts=counts) is None
    assert any(note.name in message for message in counts.errors)


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_review_runner_read_entry_skips_nonstring_key(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The review runner skips the note rather than dying mid-review."""
    from creek.classify.review_runner import _read_entry

    note = _write_note(tmp_path / "bad.md", NONSTRING_KEY_HEADERS[header_name])

    assert _read_entry(note) is None


def test_unnamed_digest_survives_nonstring_key(tmp_path: Path) -> None:
    """The weekly Unnamed digest keeps its good fragments (the #924 narrow tuple)."""
    from creek.generate.unnamed import _load_fragment

    fragment = _good_fragment(0)
    written = write_fragment_file(vault=tmp_path, fragment=fragment, body="hello")
    bad = _write_note(written.parent / "bad.md", NONSTRING_KEY_HEADERS["date_key"])

    assert _load_fragment(bad) is None
    # Positive control: the loader is not simply broken for every input.
    good = _load_fragment(written)
    assert good is not None
    assert good[0].id == fragment.id
