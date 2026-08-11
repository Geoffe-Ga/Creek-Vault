"""Tests for the shared vault fragment reader."""

from __future__ import annotations

import ast
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from creek import author as author_pkg
from creek.author import agents as author_agents
from creek.author import checks as author_checks
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault import authors as vault_authors
from creek.vault import reader as vault_reader
from creek.vault.reader import iter_vault_fragments, try_load_fragment
from tests.helpers import write_fragment_file


def test_try_load_fragment_returns_triple(tmp_path: Path) -> None:
    """Valid fragments come back as ``(fragment, body, raw_metadata)``."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-readok000001",
        title="Readable",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = write_fragment_file(vault=vault, fragment=fragment, body="hello")

    record = try_load_fragment(file)
    assert record is not None
    loaded_fragment, body, raw = record
    assert loaded_fragment.id == fragment.id
    assert body.strip() == "hello"
    assert raw["type"] == "fragment"


def test_try_load_fragment_returns_none_for_non_fragment(tmp_path: Path) -> None:
    """A markdown file without ``type: fragment`` is skipped silently."""
    file = tmp_path / "note.md"
    file.write_text("---\nnot: a fragment\n---\nplain body\n", encoding="utf-8")

    assert try_load_fragment(file) is None


def test_try_load_fragment_returns_none_for_invalid_schema(tmp_path: Path) -> None:
    """Files claiming ``type: fragment`` but missing required keys skip silently."""
    file = tmp_path / "broken.md"
    file.write_text(
        "---\ntype: fragment\nid: frag-broken000001\n---\nbody\n",
        encoding="utf-8",
    )

    # The Fragment schema requires ``title`` and ``source``; this entry
    # has neither, so model validation fails and we get ``None`` rather
    # than a raised exception.
    assert try_load_fragment(file) is None


def test_try_load_fragment_propagates_oserror(tmp_path: Path) -> None:
    """Real I/O failures propagate so the caller can surface them."""
    missing = tmp_path / "does-not-exist.md"
    with pytest.raises(OSError):
        try_load_fragment(missing)


def test_iter_vault_fragments_skips_non_fragments(tmp_path: Path) -> None:
    """``iter_vault_fragments`` returns only valid Creek fragments."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-mixed000001",
        title="Real one",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    write_fragment_file(vault=vault, fragment=fragment, body="body")

    fragments_dir = vault / "01-Fragments" / "Notes"
    (fragments_dir / "plain.md").write_text("# Just a note\n", encoding="utf-8")
    (fragments_dir / "broken-yaml.md").write_text(
        "---\nbroken: [\n",
        encoding="utf-8",
    )

    records = iter_vault_fragments(vault / "01-Fragments")
    titles = [record[1].title for record in records]
    assert titles == ["Real one"]


def test_iter_vault_fragments_returns_empty_for_missing_root(tmp_path: Path) -> None:
    """Missing ``01-Fragments`` returns an empty list, not an error."""
    assert iter_vault_fragments(tmp_path / "no-such-dir") == []


def test_try_load_fragment_without_hierarchy_keys_defaults_to_root(
    tmp_path: Path,
) -> None:
    """Pre-FEAT-020 fragments load as root documents without crash or warning.

    Acceptance criterion: "Pre-existing fragments (no hierarchy fields
    in frontmatter) load with the documented defaults — no crash, no
    warning spam." Loads a hand-crafted legacy frontmatter blob (no
    ``parent_id`` / ``child_ids`` / ``level`` / ``structural_path``
    keys) with ``warnings`` promoted to errors so any incidental
    deprecation noise from the schema would fail the test.
    """
    import warnings

    legacy = tmp_path / "legacy.md"
    legacy.write_text(
        "---\n"
        "type: fragment\n"
        "id: frag-legacy000007\n"
        "title: Pre-FEAT-020 fragment\n"
        "source:\n"
        "  platform: claude\n"
        "---\n"
        "Body of the legacy fragment.\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        record = try_load_fragment(legacy)

    assert record is not None
    fragment, body, raw = record
    assert fragment.parent_id is None
    assert fragment.child_ids == []
    assert fragment.level == "document"
    assert fragment.structural_path == []
    assert body.strip() == "Body of the legacy fragment."
    # Legacy frontmatter must remain untouched — raw should not have
    # been mutated to inject the new keys (callers that round-trip via
    # ``raw`` would otherwise stamp defaults into untouched vault
    # files and explode the audit diff on the next pass).
    assert "parent_id" not in raw
    assert "level" not in raw


def test_fragment_with_offsetless_frontmatter_timestamp_loads_tz_aware(
    tmp_path: Path,
) -> None:
    """An offsetless ``ingested:`` loads coerced to LA — and is not dropped.

    PyYAML parses the unquoted frontmatter line
    ``ingested: 2024-01-01 12:00:00`` into a *naive* datetime, which
    before issue #976 flowed straight through
    :meth:`Fragment.model_validate` and detonated later, at whichever
    consumer first compared it against a tz-aware timestamp from a
    neighbouring fragment. This is the on-disk round trip: the frontmatter
    is hand-written rather than dumped from a :class:`Fragment`, because a
    dumped model would already carry an offset and prove nothing.

    Assertion (a) — ``record is not None`` — is deliberate, not padding.
    :func:`try_load_fragment` swallows ``ValidationError`` and returns
    ``None`` at DEBUG level, so "hardening" the model to *reject* naive
    timestamps rather than coerce them would silently delete every
    offsetless fragment from every vault scan with no error surfaced
    anywhere. This test pins coerce-over-reject.
    """
    fragment_file = tmp_path / "offsetless.md"
    fragment_file.write_text(
        "---\n"
        "type: fragment\n"
        "id: frag-offsetless01\n"
        "title: Offsetless timestamp\n"
        "source:\n"
        "  platform: journal\n"
        "ingested: 2024-01-01 12:00:00\n"
        "---\n"
        "Body of the offsetless fragment.\n",
        encoding="utf-8",
    )

    record = try_load_fragment(fragment_file)

    # (a) The fragment survives the load — naive timestamps are repaired,
    # never grounds for silently discarding vault content.
    assert record is not None
    fragment, _body, _raw = record
    # (b) …and it is repaired to LA, not merely stamped with some offset.
    la = ZoneInfo("America/Los_Angeles")
    assert fragment.ingested.tzinfo is not None
    assert fragment.ingested.utcoffset() == la.utcoffset(fragment.ingested)
    # Attached, not converted: the wall clock the file recorded survives.
    assert fragment.ingested.hour == 12
    assert fragment.ingested.date().isoformat() == "2024-01-01"


def test_try_load_fragment_round_trips_hierarchy_fields(tmp_path: Path) -> None:
    """A fragment carrying every hierarchy field round-trips through reader."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-hier-readok",
        title="Hierarchical",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        parent_id="frag-hier-parent",
        child_ids=["frag-hier-kidaa", "frag-hier-kidbb"],
        level="section",
        structural_path=["Essay", "Part 2"],
    )
    file = write_fragment_file(vault=vault, fragment=fragment, body="body")

    record = try_load_fragment(file)
    assert record is not None
    loaded_fragment, _body, raw = record
    assert loaded_fragment.parent_id == "frag-hier-parent"
    assert loaded_fragment.child_ids == ["frag-hier-kidaa", "frag-hier-kidbb"]
    assert loaded_fragment.level == "section"
    assert loaded_fragment.structural_path == ["Essay", "Part 2"]
    # raw_metadata must surface every hierarchy key — classify/link
    # engines pass raw forward when rewriting frontmatter, so dropping
    # any one would silently nuke that field on the next write.
    assert raw["parent_id"] == "frag-hier-parent"
    assert raw["child_ids"] == ["frag-hier-kidaa", "frag-hier-kidbb"]
    assert raw["level"] == "section"
    assert raw["structural_path"] == ["Essay", "Part 2"]


# ---------------------------------------------------------------------------
# #1341 — one definition of "the corpus", or the leak gate goes blind again
#
# ``creek/author/agents.py`` listed the three subtrees the desk reads;
# ``creek/author/checks.py`` hardcoded ``01-Fragments`` and read one. The two
# lists disagreeing is the whole defect, so the fix is not "add two strings to
# the gate" — it is "there is only one list, and it lives under ``creek.vault``
# with the rest of the vault-layout knowledge."
# ---------------------------------------------------------------------------

_CORPUS_ROOTS = ("01-Fragments", "09-Reference", "11-Other-Authors")
"""The three vault subtrees the Writing Desk draws evidence from.

Spelled out here rather than imported so the AST sweep below fails on what it
is meant to find — a hardcoded root in a desk module — instead of on an import
error. Drift between this literal and the shipped constant is what
:func:`test_the_leak_gate_and_the_specialists_read_one_corpus_definition`
catches.
"""


def _author_package_root() -> Path:
    """Return the on-disk directory holding the ``creek.author`` package.

    Derived from the imported package rather than a repository-relative path so
    the sweep follows the installed package wherever the test runs from.

    Returns:
        The directory containing ``creek/author/__init__.py``.
    """
    origin = author_pkg.__file__
    assert origin is not None  # a regular package always has a file origin
    return Path(origin).parent


def _non_docstring_strings(tree: ast.Module) -> list[str]:
    """Return every string constant in *tree* that is not in a docstring position.

    A docstring — module, class, function, or the PEP-257-style *attribute*
    docstring this repo writes after an assignment — is a bare expression
    statement whose value is a string constant. That is a superset of what
    :func:`ast.get_docstring` reports (which covers only ``Module`` /
    ``ClassDef`` / ``FunctionDef`` / ``AsyncFunctionDef``), so skipping bare
    ``Expr(Constant(str))`` statements excludes every kind at once. Prose is
    allowed to name a vault folder; *code* is not.

    Args:
        tree: A parsed module.

    Returns:
        The remaining string constants, in walk order.
    """
    docstring_positions = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_positions
    ]


def test_the_leak_gate_and_the_specialists_read_one_corpus_definition() -> None:
    """The gate and the specialists must share one corpus-subtree object (#1341).

    Asserted by **identity**, not equality: two hardcoded lists that happen to
    match today would satisfy ``==`` and then drift apart on the next edit —
    which is precisely how ``check_privacy_compliance`` came to police one
    subtree while ``_load_corpus`` gathered from three. ``is`` can only be
    satisfied by both modules reading the same object.

    The definition belongs beside the rest of the vault-layout knowledge in
    :mod:`creek.vault.reader`, and its ``11-Other-Authors`` entry must be the
    already-canonical :data:`creek.vault.authors.OTHER_AUTHORS_DIR` rather than
    a fourth copy of that string.

    Today this fails with ``AttributeError`` — the constant does not exist yet.
    """
    assert tuple(vault_reader.CORPUS_SUBDIRS) == _CORPUS_ROOTS
    assert author_checks.CORPUS_SUBDIRS is vault_reader.CORPUS_SUBDIRS
    assert author_agents.CORPUS_SUBDIRS is vault_reader.CORPUS_SUBDIRS
    assert vault_reader.CORPUS_SUBDIRS[2] == vault_authors.OTHER_AUTHORS_DIR


def test_no_desk_module_hardcodes_a_corpus_root() -> None:
    """No module under ``creek/author/`` may spell a corpus root in code (#1341).

    The forcing function for the constant above: an AST sweep of every desk
    module for a string constant — outside any docstring — containing one of
    the three corpus roots. A second literal is a second source of truth, and
    the first one that fell out of step is what made the HARD privacy gate
    blind to ``09-Reference`` and ``11-Other-Authors``.

    Only the three exact root strings are matched. A shape-based rule such as
    ``^\\d{2}-[A-Za-z]`` would reject ``creek/author/contracts.py``'s
    legitimate ``("00-Creek-Meta", "Skills", "mediums")`` tuple, which names a
    *different* part of the vault and is not the constant under discussion.

    Today this fails naming two offenders: ``agents.py``'s ``_CORPUS_SUBDIRS``
    tuple and ``checks.py``'s ``vault / "01-Fragments"``.
    """
    offenders: list[str] = []
    for module_path in sorted(_author_package_root().rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        offenders.extend(
            f"{module_path.name}: {text!r}"
            for text in _non_docstring_strings(tree)
            if any(root in text for root in _CORPUS_ROOTS)
        )

    assert offenders == [], (
        "Desk modules must import the corpus subtrees from creek.vault.reader "
        f"instead of spelling them: {offenders}"
    )
