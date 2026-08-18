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

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

import frontmatter
import pytest

from creek.cli import _run_ingest
from creek.ingest import INGESTOR_REGISTRY
from creek.models import (
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.writer import VaultWriter
from tests.helpers import write_fragment_file

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


# ---- The guard's shape, checked mechanically rather than counted ------------

_BARE_LOAD_CALLEES: Final[frozenset[str]] = frozenset(
    {"frontmatter.load", "frontmatter.loads"},
)
"""Callees that *are* the load: bracketing one of these is the house shape."""

_HELPER_CALLEES: Final[frozenset[str]] = frozenset(
    {"try_load_fragment", "_read_fragment"},
)
"""Callees whose own body holds the load (``_read_fragment`` aliases the first)."""

_HELPER_BRACKETING_SITES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("vault/reader.py", "iter_vault_fragments"),
        ("classify/review_runner.py", "_read_entry"),
        ("classify/classify_engine.py", "_load_classifiable_fragment"),
        ("author/checks.py", "_scan_subtree_for_cited"),
    },
)
"""Every site that brackets :func:`creek.vault.reader.try_load_fragment`.

:data:`creek.vault.reader.FRONTMATTER_LOAD_ERRORS` enumerates exactly these in
prose, and used to pair the list with a hand-counted tally of the remaining
sites — a tally that was wrong. Pinned here instead, where it is checked.
"""


def _catches_load_errors(node: ast.Try) -> bool:
    """Report whether *node* has a handler naming ``FRONTMATTER_LOAD_ERRORS``."""
    return any(
        isinstance(name, ast.Name) and name.id == "FRONTMATTER_LOAD_ERRORS"
        for handler in node.handlers
        if handler.type is not None
        for name in ast.walk(handler.type)
    )


def _callee_name(node: ast.expr) -> str:
    """Return the dotted name of *node*, or its node type when it is not a name."""
    if isinstance(node, ast.Attribute):
        return f"{_callee_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return type(node).__name__


def _bracketed_callee(body: list[ast.stmt]) -> str | None:
    """Return the callee of a ``try`` *body* that is one single call statement.

    ``None`` means the bracket holds something other than one bare call — the
    shape the guard's docstring forbids, because a wider bracket would swallow
    a genuine programming-error ``TypeError``.
    """
    if len(body) != 1:
        return None
    statement = body[0]
    if not isinstance(statement, (ast.Assign, ast.Return)):
        return None
    if not isinstance(statement.value, ast.Call):
        return None
    return _callee_name(statement.value.func)


def _collect_guards(
    node: ast.AST,
    module: str,
    function: str,
    found: list[tuple[str, str, str | None]],
) -> None:
    """Append ``(module, function, bracketed callee)`` for each guard under *node*."""
    for child in ast.iter_child_nodes(node):
        name = function
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = child.name
        elif isinstance(child, ast.Try) and _catches_load_errors(child):
            found.append((module, function, _bracketed_callee(child.body)))
        _collect_guards(child, module, name, found)


def _frontmatter_guard_sites() -> list[tuple[str, str, str | None]]:
    """Walk the ``creek`` package for every ``FRONTMATTER_LOAD_ERRORS`` guard."""
    import creek

    package_root = Path(creek.__file__).parent
    found: list[tuple[str, str, str | None]] = []
    for path in sorted(package_root.rglob("*.py")):
        module = path.relative_to(package_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _collect_guards(tree, module, "<module>", found)
    return found


def test_every_frontmatter_guard_brackets_exactly_one_load() -> None:
    """Every guard wraps its load statement and nothing else (#1546).

    :data:`creek.vault.reader.FRONTMATTER_LOAD_ERRORS` documents two rules:
    the bracket holds the load statement alone, and the only sites that
    bracket :func:`~creek.vault.reader.try_load_fragment` instead of a bare
    ``frontmatter.load`` are the ones it names. Both were prose, and the
    paragraph stating them also carried a hand-counted tally of the package's
    uses that was simply wrong. A count nobody can check becomes false
    silently; this test checks the claims instead, so a guard that widens its
    bracket — or a fifth caller that starts bracketing the helper — fails here
    rather than quietly outdating the docstring (#1548 will move sites).
    """
    sites = _frontmatter_guard_sites()

    # Positive control: the walk really parsed the package and found guards.
    assert len(sites) > len(_HELPER_BRACKETING_SITES)
    bare_load_sites = [site for site in sites if site[2] in _BARE_LOAD_CALLEES]
    assert bare_load_sites, "no bare-load guard found — the walk missed the tree"

    over_wide = [
        (module, function, callee)
        for module, function, callee in sites
        if callee not in _BARE_LOAD_CALLEES | _HELPER_CALLEES
    ]
    assert over_wide == [], f"guard brackets more than its load: {over_wide}"

    helper_sites = {
        (module, function)
        for module, function, callee in sites
        if callee in _HELPER_CALLEES
    }
    assert helper_sites == set(_HELPER_BRACKETING_SITES)


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
    corrupt_header = (
        f"id: {fragment.id}\ntype: fragment\n" + NONSTRING_KEY_HEADERS[header_name]
    )
    _write_note(written, corrupt_header)
    # The restore arm needs a tombstone that *exists and declares this id*, or
    # it passes for the unrelated reason that ``10-Liminal/Orphaned/`` is
    # empty — which is what made it vacuous before. Written corrupt, so the
    # only thing standing between the id and its tombstone is the header.
    orphan = _write_note(
        writer_vault / "10-Liminal" / "Orphaned" / f"tomb-{fragment.id}.md",
        corrupt_header,
    )

    assert writer.find_fragment(fragment.id) is None
    assert writer.update_fragment(fragment, body="new") is None
    assert writer.tomb_fragment(fragment.id) is None
    assert writer.restore_fragment(fragment) is None
    # ...and the tombstone really is sitting there, declaring the id in text.
    assert fragment.id in orphan.read_text(encoding="utf-8")


def test_restore_fragment_finds_a_readable_tombstone(writer_vault: Path) -> None:
    """Positive control for the restore arm above.

    Proves the ``None`` there is caused by the unreadable header and not by a
    restore path that can never find anything: same writer, same orphan
    directory, a tombstone whose only difference is that its frontmatter
    parses.
    """
    writer = VaultWriter(vault_path=writer_vault)
    fragment = _good_fragment(1)
    writer.write_fragment(fragment, body="hello")
    assert writer.tomb_fragment(fragment.id) is not None

    restored = writer.restore_fragment(fragment)

    assert restored is not None
    assert restored.parent.name != "Orphaned"


# ---- The verify-then-load race must stay in the OSError family (#1475) ------


def test_load_post_or_report_keeps_an_oserror_an_oserror(tmp_path: Path) -> None:
    """A file that vanished mid-run raises ``OSError``, never ``ValueError``.

    The three write paths are all reached from the per-unit loop in
    :mod:`creek.ingest.pipeline`, whose handlers are ``except (OSError,
    KeyError)``. Before the guard existed these loads were bare, so the
    ``FileNotFoundError`` of a file an editor or sync client removed mid-run
    was reported against that one unit and the batch carried on. Re-raising it
    as a ``ValueError`` would walk past that handler and end the whole run, so
    the wrapper splits: ``OSError`` stays an ``OSError`` (with the path named),
    and ``ValueError`` is reserved for the parse-shaped failures.
    """
    from creek.vault.writer import _load_post_or_report

    missing = tmp_path / "vanished.md"

    with pytest.raises(OSError) as excinfo:
        _load_post_or_report(missing)

    assert not isinstance(excinfo.value, ValueError)
    assert str(missing) in str(excinfo.value)


def _race_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault a markdown ingest run needs."""
    vault = tmp_path / "vault"
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
        "personal/journal",
    ):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _ingest_markdown(vault: Path, target: Path) -> tuple[int, list[str], int]:
    """Run one markdown ingest pass over *target* (a directory = full source)."""
    return _run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=target,
        vault_path=vault,
    )


def _arm_vanishing_file(
    monkeypatch: pytest.MonkeyPatch,
    doomed_id: str,
) -> dict[str, bool]:
    """Delete the located file for *doomed_id* the instant it is located.

    A genuine race, not a mocked exception: the file is really unlinked
    between the id verifier proving it and the write path loading it, so
    ``frontmatter.load`` raises a real ``FileNotFoundError`` out of ``open``.
    That is the window :func:`creek.vault.writer._load_post_or_report`'s
    docstring describes — an editor or sync client rewriting a live Obsidian
    vault under a running ingest.
    """
    real = VaultWriter._find_existing_locked
    state = {"fired": False}

    def racing(
        self: VaultWriter,
        model_id: str,
        target_dir: Path,
    ) -> Path | None:
        found = real(self, model_id, target_dir)
        if found is not None and model_id == doomed_id and not state["fired"]:
            state["fired"] = True
            found.unlink()
        return found

    monkeypatch.setattr(VaultWriter, "_find_existing_locked", racing)
    return state


def _fragment_ids_by_path(vault: Path) -> dict[Path, str]:
    """Map every live fragment file to the id its frontmatter declares."""
    return {
        path: str(frontmatter.load(str(path))["id"])
        for path in sorted((vault / "01-Fragments").rglob("*.md"))
    }


def test_vanished_file_mid_update_costs_one_fragment_not_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update_fragment`` losing its file is reported per unit; the batch runs on."""
    vault = _race_vault(tmp_path)
    journal = vault / "personal" / "journal"
    (journal / "one.md").write_text(
        "---\ndate: 2026-06-01\n---\nfirst body\n", encoding="utf-8"
    )
    (journal / "two.md").write_text(
        "---\ndate: 2026-06-02\n---\nsecond body\n", encoding="utf-8"
    )
    _, errors, _ = _ingest_markdown(vault, journal)
    assert errors == []
    by_path = _fragment_ids_by_path(vault)
    assert len(by_path) == 2  # positive control: two real fragments to race.
    doomed_path, doomed_id = next(iter(by_path.items()))
    survivor_path = next(path for path in by_path if path != doomed_path)

    # Edit both source units so both take the update path.
    (journal / "one.md").write_text(
        "---\ndate: 2026-06-01\n---\nfirst body REWRITTEN\n", encoding="utf-8"
    )
    (journal / "two.md").write_text(
        "---\ndate: 2026-06-02\n---\nsecond body REWRITTEN\n", encoding="utf-8"
    )
    state = _arm_vanishing_file(monkeypatch, doomed_id)

    written, errors, _ = _ingest_markdown(vault, journal)

    assert state["fired"], "the race never fired; the test proves nothing"
    assert len(errors) == 1, errors
    assert doomed_id in errors[0]
    assert "Unreadable frontmatter" in errors[0]
    assert str(doomed_path) in errors[0]
    # The batch continued: the other unit was written, not abandoned.
    assert written == 1
    assert "REWRITTEN" in survivor_path.read_text(encoding="utf-8")


def test_vanished_file_mid_tomb_costs_one_fragment_not_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tomb_fragment`` losing its file is reported per unit; the batch runs on."""
    vault = _race_vault(tmp_path)
    journal = vault / "personal" / "journal"
    # Ingested one at a time, so the doomed fragment's source unit is known by
    # construction rather than guessed back from a derived filename.
    (journal / "one.md").write_text(
        "---\ndate: 2026-06-01\n---\nfirst body\n", encoding="utf-8"
    )
    _, errors, _ = _ingest_markdown(vault, journal)
    assert errors == []
    first = _fragment_ids_by_path(vault)
    assert len(first) == 1  # positive control.
    doomed_path, doomed_id = next(iter(first.items()))
    (journal / "two.md").write_text(
        "---\ndate: 2026-06-02\n---\nsecond body\n", encoding="utf-8"
    )
    _, errors, _ = _ingest_markdown(vault, journal)
    assert errors == []
    assert len(_fragment_ids_by_path(vault)) == 2  # positive control.

    # ``one.md`` is gone, so its fragment is tombed — and the tomb path loses
    # the file between locating it and loading it.
    (journal / "one.md").unlink()
    state = _arm_vanishing_file(monkeypatch, doomed_id)

    _, errors, _ = _ingest_markdown(vault, journal)

    assert state["fired"], "the race never fired; the test proves nothing"
    assert len(errors) == 1, errors
    assert "Unreadable frontmatter" in errors[0]
    assert str(doomed_path) in errors[0]
    # The surviving unit was still processed: it is live and unchanged.
    survivors = _fragment_ids_by_path(vault)
    assert len(survivors) == 1
    assert doomed_id not in survivors.values()


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
    """Every loader in the battery above still returns something readable.

    Without this, a loader that returned ``None`` unconditionally would pass
    the skip test and the whole battery would be vacuous. All ten are covered,
    not the four whose sentinel is cheapest to disprove: each of the remaining
    six needs a note shaped for *it* — a valid fragment record, a provenance
    list, an over-ceiling voice distance — and skipping them is exactly how a
    vacuous arm hides.
    """
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

    note = _write_note(tmp_path / "good.md", "type: fragment\ntitle: Fine\n")
    for label, loader in (
        ("state._safe_post", state._safe_post),
        ("mining._safe_post", mining._safe_post),
        ("compost_scan._safe_post", compost_scan._safe_post),
        ("wavelength._safe_post", wavelength._safe_post),
        ("decisions._load_post", decisions._load_post),
        (
            "skills._safe_load_post",
            lambda path: skills._safe_load_post(path, label="skill"),
        ),
    ):
        assert loader(note) is not None, f"{label} lost a readable note"

    # The two fragment-record loaders need a real fragment, not any old post.
    vault = tmp_path / "vault"
    fragment_file = write_fragment_file(
        vault=vault,
        fragment=_good_fragment(9),
        body="a readable body",
    )
    assert unnamed_mod._load_fragment(fragment_file) is not None
    assert voice_mod._load_fragment_with_body(fragment_file) is not None

    # ``_load_existing_provenance`` returns ``[]`` for *both* "unreadable" and
    # "no provenance", so its control must assert a non-empty list.
    page = _write_note(
        tmp_path / "page.md",
        "provenance:\n"
        "  - claim_id: claim-001\n"
        "    claim_excerpt: A claim worth tracing\n"
        "    fragment_ids: [frag-nonstr000009]\n"
        "    compiled_at: 2026-01-01T00:00:00+00:00\n"
        "    compile_method: rules\n",
    )
    assert compile_engine._load_existing_provenance(page) != []

    # ``voice_fidelity._scan_draft`` returns ``None`` for "unreadable" *and*
    # for "on voice", so its control needs a draft that must be reported.
    off_voice = _write_note(tmp_path / "draft.md", "voice_distance: 99.0\n")
    assert _scan_voice_fidelity_draft(voice_fidelity, off_voice) is not None


# ---- The two sites #924 named that this PR reaches (#1475 blockers B/C) -----


def _write_voice_fingerprint(vault: Path, *, fragment_count: int = 12) -> Path:
    """Write a non-empty voice fingerprint under *vault* and return its path.

    :func:`creek.lint.checks.voice_fidelity.run` returns early when the loaded
    fingerprint's ``fragment_count`` is zero — *before* it reaches
    :func:`~creek.lint.checks.voice_fidelity._scan_draft`, the one remaining
    direct ``frontmatter.load`` on the lint runner's path. Without this file
    the runner-level test below walks *past* that site rather than through it,
    and would keep passing if its guard were removed.
    """
    from creek.generate.ai_style.fingerprint import FINGERPRINT_VERSION

    path = vault / "00-Creek-Meta" / "voice-fingerprint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": FINGERPRINT_VERSION,
                "fragment_count": fragment_count,
                "features": {
                    "em_dash_density": {"rate": 0.01, "support": fragment_count},
                },
            },
        ),
        encoding="utf-8",
    )
    return path


def _tagged_vault(tmp_path: Path, header_name: str) -> Path:
    """Build a vault holding one tagged fragment, a fingerprint, and bad notes.

    The fingerprint is what makes the voice-fidelity check *run* rather than
    bail on "no profile yet"; see :func:`_write_voice_fingerprint`.
    """
    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault,
        fragment=_good_fragment(3),
        body="a readable body",
        extras={"tags": ["alpha"]},
    )
    _write_voice_fingerprint(vault)
    _scatter_bad_notes(vault / "01-Fragments" / "Notes")
    _write_note(
        vault / "01-Fragments" / "Notes" / "bad-only.md",
        NONSTRING_KEY_HEADERS[header_name],
    )
    return vault


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_tag_scan_survives_nonstring_key_note(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The tag garden's scan skips the unreadable note and keeps its tally.

    This load was the last unguarded one of the four ``#924`` named, and it
    is the one that made this PR's headline claim untrue: ``creek lint`` calls
    it through :func:`creek.lint.checks.tags.run`.
    """
    from creek.generate.tags import TagGardenGenerator

    vault = _tagged_vault(tmp_path, header_name)

    scan = TagGardenGenerator(vault_path=vault).scan_tags()

    # Positive control: the readable fragment's tag is still counted.
    assert scan.tag_counts.get("alpha") == 1


def test_lint_run_survives_a_nonstring_key_note(tmp_path: Path) -> None:
    """``creek lint`` completes over a vault holding an unreadable note.

    The headline claim, proven at the surface that carries it.
    :mod:`creek.lint.runner` has no per-check ``except`` — every check it
    calls runs bare — so any check that raises ends the whole command. Run
    here through the real runner with its default deterministic set, so a
    future check that reintroduces an unguarded load fails this test.

    The vault carries a voice fingerprint deliberately: without one the
    voice-fidelity check short-circuits on "no profile yet" and this test
    never reaches ``_scan_draft``'s load at all, so the assertions below name
    that check's own tally as a second positive control.
    """
    from creek.lint.runner import LintRunner

    vault = _tagged_vault(tmp_path, "date_key")
    _scatter_bad_notes(vault / "10-Liminal" / "Compost", prefix="compost-bad")
    _scatter_bad_notes(vault / "07-Voice" / "Drafts", prefix="draft-bad")
    _write_note(
        vault / "07-Voice" / "Drafts" / "off-voice.md",
        "voice_distance: 99.0\n",
    )

    report = LintRunner(vault_path=vault).run()

    # Positive control: real checks really ran, and the tag survived.
    results = {result.name: result for result in report.results}
    assert results, "no checks ran"
    assert "1 tag(s) tracked" in results["tags"].summary
    # Positive control: voice-fidelity got past its "no fingerprint" exit and
    # read every draft, so the unreadable three went through its guard.
    assert "4 draft(s) scanned; 1 above" in results["voice-fidelity"].summary


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_find_decision_by_id_steps_over_unreadable_note(
    tmp_path: Path,
    header_name: str,
) -> None:
    """One hand-edited decision note must not hide every other decision."""
    from creek.generate.decisions import DecisionDetector

    decisions_dir = tmp_path / "08-Decisions"
    active = decisions_dir / "Active"
    active.mkdir(parents=True)
    # Sorted before the target, so an unguarded load aborts before reaching it.
    _write_note(active / "aaa-bad.md", NONSTRING_KEY_HEADERS[header_name])
    wanted = _write_note(active / "zzz-good.md", "id: dec-000001\ntype: decision\n")

    found = DecisionDetector._find_decision_by_id("dec-000001", decisions_dir)

    assert found == wanted


@pytest.mark.parametrize("header_name", _HEADER_IDS)
def test_fingerprint_corpus_survives_nonstring_key_note(
    tmp_path: Path,
    header_name: str,
) -> None:
    """The voice fingerprint's corpus walk keeps its samples (#924).

    Its guard was the narrow ``(OSError, yaml.YAMLError)``, so the splat's
    ``TypeError`` went straight through and cost the whole fingerprint its
    run rather than costing one note its sample.
    """
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.fingerprint import _eligible_texts

    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault,
        fragment=_good_fragment(4),
        body="a readable body of prose that the fingerprint can measure",
    )
    _write_note(
        vault / "01-Fragments" / "Notes" / f"bad-{header_name}.md",
        NONSTRING_KEY_HEADERS[header_name],
    )

    texts = _eligible_texts(vault, AIStyleConfig(), include_intimate=True)

    # Positive control: the readable fragment is still in the corpus.
    assert len(texts) == 1


# ---- The accepted cost of header-only reading (#1416) -----------------------


def test_header_only_readers_require_the_fence_on_line_one(tmp_path: Path) -> None:
    """Pin the one narrowing the ``read_header_meta`` conversions accept.

    ``frontmatter.loads`` tolerates blank lines above the opening ``---``;
    :func:`creek.vault.links.read_header_meta` does not (#1416), and two of
    the five converted sites read operator-editable folders. The narrowing is
    accepted rather than reverted for three reasons: **Obsidian itself** only
    recognises frontmatter whose fence opens line 1, so such a note has no
    properties in the editor the vault lives in either; the consequence at
    both sites is a skip, not a loss — the compost note goes unlisted, the
    paradox pair is not recognised as recorded and may be re-proposed as a
    duplicate (the case #1320's advisory already reports); and #1416 made
    exactly this trade for the sibling ``10-Liminal/Synchronicities/`` folder,
    so reverting these two would split the rule across three sibling folders.

    Pinned here so the cost is a decision on the record, not a surprise.
    """
    from creek.generate.paradox import _recorded_pair
    from creek.lint.checks import compost as compost_check

    vault = tmp_path / "vault"
    compost_dir = vault / "10-Liminal" / "Compost"
    compost_dir.mkdir(parents=True)
    good_compost = compost_dir / "kept.md"
    good_compost.write_text(
        "---\ntype: compost\ntitle: Kept\n---\nbody\n", encoding="utf-8"
    )
    (compost_dir / "shifted.md").write_text(
        "\n---\ntype: compost\ntitle: Shifted\n---\nbody\n", encoding="utf-8"
    )

    result = compost_check.run(vault)

    # Positive control first: the fence-on-line-1 note is still listed.
    assert any("Kept" in finding for finding in result.findings)
    assert not any("Shifted" in finding for finding in result.findings)

    paradox_dir = vault / "10-Liminal" / "Paradoxes"
    paradox_dir.mkdir(parents=True)
    good_pair = paradox_dir / "pair.md"
    good_pair.write_text(
        "---\nfragments: [frag-a, frag-b]\n---\nbody\n", encoding="utf-8"
    )
    shifted_pair = paradox_dir / "shifted.md"
    shifted_pair.write_text(
        "\n---\nfragments: [frag-a, frag-b]\n---\nbody\n", encoding="utf-8"
    )

    assert _recorded_pair(good_pair) == frozenset({"frag-a", "frag-b"})
    assert _recorded_pair(shifted_pair) is None


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
