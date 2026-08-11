"""#1294: every ingestor refuses a source tree that links out of itself.

---------------------------------------------------------------------------
The defect
---------------------------------------------------------------------------
All eleven entries in :data:`creek.ingest.INGESTOR_REGISTRY` discover their
inputs with an unguarded walk and read whatever the walk yields. A symlink
under the named source tree whose target lies outside it is followed, read,
and written into the vault as a durable fragment — which then feeds
classification, embeddings, cloud LLM prompts, drafts and compiled pages.
``CodeIngestor`` is worse than the rest: ``_discover_directory`` recurses
with ``item.is_dir()``, which *follows* links, so it walks an entire
out-of-tree subtree rather than surfacing the link and stopping.

---------------------------------------------------------------------------
Why these tests assert REFUSE and not SKIP
---------------------------------------------------------------------------
#1293 (PR #1360) settled the shape of the rule and this file inherits it.
The *read* path (``redact --scan``) SKIPS and counts, because refusing a
whole scan over one bad link is a denial of service on the safety pass —
the operator's only instrument for learning what is exposed. The *write*
path (``redact --apply`` / ``--review``) REFUSES. Ingest writes, so ingest
refuses. Two more reasons the write-path rule is the right one here:

* ``creek process`` with the default ``redaction.enabled: true`` ALREADY
  refuses this exact tree via ``SymlinkEscapedSourceError`` (#1087). A skip
  at the ingestor would make the SAME tree behave differently depending on
  a config toggle, which is precisely the defect #1294 names.
* ``creek ingest`` prints errors as a dim yellow list and exits 0, so
  "skip and append to ``IngestResult.errors``" is near-silent in practice.

---------------------------------------------------------------------------
Why the guard belongs at ONE chokepoint
---------------------------------------------------------------------------
``Ingestor.ingest`` is the single door all eleven ingestors go through.
``test_every_registered_ingestor_refuses_an_escaping_link`` drives straight
off the registry, so a twelfth ingestor added next year is covered without
anyone remembering to copy a check into it. N per-ingestor copies of the
predicate could never have that property — and the copies would drift, which
is the same argument #1293 used to make ``resolves_within`` public rather
than write it a second time.

---------------------------------------------------------------------------
The containment policy these tests must NOT over-tighten
---------------------------------------------------------------------------
Resolve the ROOT once; ``lstat`` (i.e. ``is_symlink()``) only the LEAF;
never resolve a non-symlink child. That is what keeps a root reached
*through* a symlinked component usable — on macOS ``tmp_path`` lives under
``/tmp`` -> ``/private/tmp`` — and resolving children too would flag every
child of every test root as escaping. An escaping ANCESTOR component is a
documented, accepted residual across #1087/#1293 and is not closed here. A
symlink whose target stays INSIDE the root is admitted: ordinary intra-tree
aliases keep working, and the over-breadth guards below pin that.

Symlinks are not portable in git (see ``tests/fixtures/symlinks/README.md``),
so every tree here is constructed at runtime under ``tmp_path``.

Imports of :mod:`creek._containment` are deliberately function-local. The
module does not exist yet, and a module-level import of a missing name is a
collection error for the whole file — it would take the regression
invariants down with it and hide the real signal. The idiom is copied from
``tests/test_cli_redact.py``'s ``SymlinkPolicy`` test.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import run_ingest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()


# ---------------------------------------------------------------------------
# Sentinels
# ---------------------------------------------------------------------------

_SENTINEL = "CANARY-INGEST-ESCAPED-1294-a7f3"
"""String that exists ONLY in a file parked outside the source root.

A sentinel rather than realistic prose, so "this reached the vault" cannot
be explained away as a phrase that could have come from anywhere.
"""

_IN_ROOT_MARKER = "CONTROL-IN-ROOT-1294-b19c"
"""String carried by the innocent, genuinely in-root file of every fixture.

Its presence in the vault on a *clean* run is the non-vacuity guard: a lane
whose fixture the ingestor never picks up would satisfy every "the sentinel
is absent" assertion perfectly while proving nothing at all.
"""


# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------

_VAULT_DIRS: tuple[str, ...] = (
    "00-Creek-Meta/Processing-Log",
    "01-Fragments/Conversations",
    "01-Fragments/Journal",
    "01-Fragments/Messages",
    "01-Fragments/Notes",
    "01-Fragments/Unsorted",
    "01-Fragments/Writing",
    "10-Liminal/Orphaned",
)
"""Minimum vault scaffold ``VaultWriter`` will accept and tombing needs.

``VaultWriter`` hard-requires ``00-Creek-Meta`` and ``01-Fragments`` and
creates the per-platform subfolder itself; ``10-Liminal/Orphaned`` is where
a soft-tomb lands, and the mass-tombing test reads it.
"""


def _make_vault(base: Path, *, name: str = "vault") -> Path:
    """Scaffold a vault whose config explicitly disables redaction.

    ``redaction.enabled: false`` is written out and named in the tests that
    matter because it is the exact operator setting that switches off the
    #1087 pipeline refusal. ``run_ingest`` never reads this file — and that
    is the point: with the redaction pass out of the picture there is
    nothing else in the call graph that could refuse, so any refusal these
    tests observe has to be the ingestor's own.

    Args:
        base: Directory to create the vault under.
        name: Vault directory name, so one test can hold two vaults.

    Returns:
        Path to the vault root.
    """
    vault = base / name
    for relative in _VAULT_DIRS:
        (vault / relative).mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "redaction:\n  enabled: false\n",
        encoding="utf-8",
    )
    return vault


def _written_vault_text(vault: Path) -> str:
    """Concatenate every regular file the run left under *vault*.

    Symlinks are skipped rather than read: a test whose source tree lives
    inside the vault would otherwise read the escaping link itself and
    report the sentinel as "reached the vault" when nothing was written.
    Every source tree in this file is parked outside the vault anyway; the
    skip keeps the helper safe if that ever changes.

    Args:
        vault: Vault root to read.

    Returns:
        The concatenated text of every regular file under *vault*.
    """
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(vault.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _vault_fragments(vault: Path) -> list[Path]:
    """Return the live fragment files under ``01-Fragments``.

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of live fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _vault_orphans(vault: Path) -> list[Path]:
    """Return the soft-tombed fragment files under ``10-Liminal/Orphaned``.

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of tombstone paths.
    """
    return sorted((vault / "10-Liminal" / "Orphaned").rglob("*.md"))


# ---------------------------------------------------------------------------
# Per-ingestor fixture builders
#
# Each builder plants BOTH an in-root innocent file the ingestor really
# picks up AND (when asked) an escaping link of the shape that ingestor's
# own discover() walks. A single-item fixture makes any "nothing leaked"
# assertion trivially true, so both halves are mandatory.
# ---------------------------------------------------------------------------


def _park_outside(base: Path, name: str, text: str) -> Path:
    """Write *text* to ``<base>/outside/<name>`` and return the path.

    Args:
        base: Per-lane scratch directory; the source root is its sibling.
        name: Filename for the out-of-tree victim file.
        text: Content, expected to carry :data:`_SENTINEL`.

    Returns:
        Path to the file parked outside the source root.
    """
    outside = base / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    victim = outside / name
    victim.write_text(text, encoding="utf-8")
    return victim


def _chatgpt_export_json(marker: str) -> str:
    """Return a one-conversation ChatGPT export carrying *marker*.

    Shaped to satisfy ``creek.ingest.chatgpt._is_chatgpt_export`` (a JSON
    list of dicts) and the tree walk in ``_parse_conversation``.

    Args:
        marker: Text placed in the user turn so the fragment carries it.

    Returns:
        Serialised JSON for one ChatGPT conversation export file.
    """
    create_time = 1700042400.0
    conversation = {
        "title": "Test Chat",
        "create_time": create_time,
        "update_time": create_time + 100.0,
        "mapping": {
            "root": {
                "id": "root",
                "message": None,
                "parent": None,
                "children": ["u1"],
            },
            "u1": {
                "id": "u1",
                "message": {
                    "id": "u1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [marker]},
                    "create_time": create_time + 10.0,
                },
                "parent": "root",
                "children": ["a1"],
            },
            "a1": {
                "id": "a1",
                "message": {
                    "id": "a1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Understood."]},
                    "create_time": create_time + 20.0,
                },
                "parent": "u1",
                "children": [],
            },
        },
    }
    return json.dumps([conversation])


def _post_html(title: str, marker: str) -> str:
    """Return a minimal Substack-style post HTML carrying *marker*.

    Args:
        title: Heading text for the post.
        marker: Body text the parsed fragment should carry.

    Returns:
        An HTML document string.
    """
    return f"<html><body><h1>{title}</h1><p>{marker}</p></body></html>\n"


def _build_markdown(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a markdown source tree; the link is nested, not top-level.

    ``MarkdownIngestor._read_directory`` uses ``rglob("*.md")``, so the
    escaping link is planted one level down to exercise the recursive arm.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    (src / "nested").mkdir(parents=True)
    (src / "innocent.md").write_text(
        f"# Ordinary notes\n\n{_IN_ROOT_MARKER}\n",
        encoding="utf-8",
    )
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret.md", f"# Secret\n\n{_SENTINEL}\n")
    link = src / "nested" / "link.md"
    link.symlink_to(victim)
    return src, link


def _build_document(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a DocumentIngestor source tree using ``.txt`` inputs.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    src.mkdir(parents=True)
    (src / "notes.txt").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret.txt", f"{_SENTINEL}\n")
    link = src / "leak.txt"
    link.symlink_to(victim)
    return src, link


def _build_generic(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a GenericIngestor source tree using an unclaimed extension.

    ``.log`` is outside ``creek.ingest.generic._CLAIMED_EXTENSIONS``
    (``.md`` / ``.json``), so the fallback ingestor really claims it.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    src.mkdir(parents=True)
    (src / "app.log").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret.log", f"{_SENTINEL}\n")
    link = src / "leak.log"
    link.symlink_to(victim)
    return src, link


def _build_code(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a CodeIngestor source tree; the link is a leaf ``README.md``.

    ``CodeIngestor._is_relevant_file`` matches on the walked entry's own
    name, so a link *named* ``README.md`` is claimed regardless of what it
    points at. The symlinked-*directory* case is a separate test.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text(f'"""{_IN_ROOT_MARKER}"""\n', encoding="utf-8")
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret-readme.md", f"# Secret\n\n{_SENTINEL}\n")
    link = src / "README.md"
    link.symlink_to(victim)
    return src, link


def _build_chatgpt(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a ChatGPT export tree; the link is a top-level ``*.json``.

    ``ChatGPTIngestor.discover`` uses ``glob("*.json")`` (not ``rglob``),
    so the link has to sit at the export root to be discovered at all.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    src.mkdir(parents=True)
    (src / "conversations.json").write_text(
        _chatgpt_export_json(_IN_ROOT_MARKER),
        encoding="utf-8",
    )
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret.json", _chatgpt_export_json(_SENTINEL))
    link = src / "leaked.json"
    link.symlink_to(victim)
    return src, link


def _build_substack(base: Path, *, escaping: bool) -> tuple[Path, Path | None]:
    """Build a Substack export tree of ``<post_id>.<slug>.html`` files.

    No ``posts.csv``: ``discover`` derives metadata from the filenames when
    the sidecar is absent (#594), which is the common shape of a current
    export and keeps the fixture minimal.

    Args:
        base: Per-lane scratch directory.
        escaping: Whether to plant the out-of-tree link.

    Returns:
        ``(source_root, link_or_None)``.
    """
    src = base / "src"
    src.mkdir(parents=True)
    (src / "1001.hello-world.html").write_text(
        _post_html("Hello World", _IN_ROOT_MARKER),
        encoding="utf-8",
    )
    if not escaping:
        return src, None
    victim = _park_outside(base, "secret.html", _post_html("Secret", _SENTINEL))
    link = src / "2002.leaked-post.html"
    link.symlink_to(victim)
    return src, link


_SOURCE_BUILDERS: dict[str, Callable[..., tuple[Path, Path | None]]] = {
    "chatgpt": _build_chatgpt,
    "code": _build_code,
    "document": _build_document,
    "generic": _build_generic,
    "markdown": _build_markdown,
    "substack": _build_substack,
}
"""Registry key -> fixture builder for the shaped, per-ingestor lanes.

Six ingestors whose real input shapes are cheap to synthesise in text.
``image`` / ``presentation`` / ``spreadsheet`` need binary fixtures and are
covered instead by the registry-driven test, which does not depend on any
ingestor actually claiming the planted file.
"""


# ---------------------------------------------------------------------------
# 1. The primary RED: assert on the WRITTEN VAULT FRAGMENT
# ---------------------------------------------------------------------------


def test_markdown_ingest_writes_no_fragment_from_outside_the_source_root(
    tmp_path: Path,
) -> None:
    """RED. ``run_ingest`` refuses, and the vault gains nothing at all.

    The proof the issue asks for is at the vault, not at the ingestor's
    in-memory result: a fragment on disk is what feeds classification,
    embeddings, cloud prompts and drafts, so "no fragment carries the
    sentinel" is the property that actually matters.

    Run through :func:`creek.ingest.pipeline.run_ingest` — the real write
    path both ``creek ingest`` and the ``creek.journal`` MCP tool use — with
    the vault's ``redaction.enabled`` explicitly ``false``. That setting is
    named on purpose: it is what bypasses the #1087 pipeline refusal
    entirely, so nothing but the ingestor's own guard can stop this run.

    Four assertions, each pinning a distinct failure:

    * the refusal happens at all (``EscapingSymlinkError``);
    * it names the link *as walked* and the root *as named*, so an operator
      can find the offending entry;
    * no written file carries the out-of-tree sentinel;
    * no fragment was written *at all* — a refusal writes nothing, rather
      than ingesting the innocent files and dropping the bad one, which is
      the skip semantics this decision deliberately rejects.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError

    vault = _make_vault(tmp_path)
    source, link = _build_markdown(tmp_path / "lane", escaping=True)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=source,
            vault_path=vault,
        )

    assert excinfo.value.path == link, (
        "the refusal names the wrong entry. It must name the link exactly "
        "as the walk found it, so the operator can locate and remove it — "
        "and never the resolved target, which is the disclosure oracle "
        f"#1087 closes.\n\npath={excinfo.value.path}\nexpected={link}"
    )
    assert excinfo.value.root == source, (
        "the refusal names the wrong source root, so an operator running "
        "several ingests cannot tell which one stopped.\n\n"
        f"root={excinfo.value.root}\nexpected={source}"
    )

    written = _written_vault_text(vault)
    assert _SENTINEL not in written, (
        "content from outside the source tree was ingested into the vault. "
        "This is the #1294 leak: a durable fragment now carries a file the "
        f"operator never named.\n\n{written}"
    )
    assert _vault_fragments(vault) == [], (
        "the run refused but still wrote fragments. A containment refusal "
        "is all-or-nothing: 'ingest the innocent files and drop the bad "
        "one' is the SKIP semantics the write path deliberately rejects, "
        "because it leaves the operator with a partially-ingested source "
        f"and an exit path that says nothing about it.\n\n"
        f"{_vault_fragments(vault)}"
    )


# ---------------------------------------------------------------------------
# 2. Non-vacuity: more than one ingestor, more than one input shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", sorted(_SOURCE_BUILDERS))
def test_each_ingestor_ingests_its_clean_control_tree(
    tmp_path: Path,
    source_type: str,
) -> None:
    """NON-VACUITY GUARD (passes at HEAD and after). The fixtures are real.

    Every "the sentinel never reached the vault" assertion in the paired
    test below is trivially true for an ingestor that discovers nothing.
    This half proves each lane's fixture is a shape that ingestor actually
    claims: without the link, the tree ingests and the in-root marker lands
    in a written fragment. Two lanes in this suite's history have fallen
    into exactly that trap with a single-item fixture.

    Args:
        tmp_path: Pytest-provided temporary directory.
        source_type: Registry key naming the ingestor and its builder.
    """
    vault = _make_vault(tmp_path)
    source, link = _SOURCE_BUILDERS[source_type](tmp_path / "lane", escaping=False)
    assert link is None  # builder contract: no link when escaping is False

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY[source_type],
        source_type=source_type,
        input_path=source,
        vault_path=vault,
    )

    assert result.discovered >= 1, (
        f"the {source_type} ingestor discovered nothing in its own control "
        "fixture, so the containment test for this lane proves nothing.\n\n"
        f"errors={result.errors}"
    )
    assert result.written >= 1, (
        f"the {source_type} ingestor discovered inputs but wrote no "
        "fragment, so the paired containment assertions would hold over a "
        f"run that produced nothing.\n\nerrors={result.errors}"
    )
    assert _IN_ROOT_MARKER in _written_vault_text(vault), (
        f"the {source_type} control fragment reached the vault without its "
        "in-root marker, so this lane cannot distinguish 'the guard "
        "refused' from 'this content never lands in a fragment "
        f"anyway'.\n\nerrors={result.errors}"
    )


@pytest.mark.parametrize("source_type", sorted(_SOURCE_BUILDERS))
def test_each_ingestor_refuses_its_own_escaping_link_shape(
    tmp_path: Path,
    source_type: str,
) -> None:
    """RED for all six lanes. Each real input shape is refused, not read.

    The same tree as the control above plus one escaping link of the shape
    *that* ingestor's ``discover`` walks — a nested ``*.md`` for markdown, a
    top-level ``*.json`` for ChatGPT (its walk is ``glob``, not ``rglob``),
    a ``<post_id>.<slug>.html`` for Substack, a leaf ``README.md`` for code,
    and so on. Measured at HEAD, every one of these leaks the sentinel into
    a written fragment.

    Args:
        tmp_path: Pytest-provided temporary directory.
        source_type: Registry key naming the ingestor and its builder.
    """
    from creek._containment import EscapingSymlinkError

    vault = _make_vault(tmp_path)
    source, link = _SOURCE_BUILDERS[source_type](tmp_path / "lane", escaping=True)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        run_ingest(
            ingestor_cls=INGESTOR_REGISTRY[source_type],
            source_type=source_type,
            input_path=source,
            vault_path=vault,
        )

    assert excinfo.value.path == link, (
        f"the {source_type} refusal names the wrong entry.\n\n"
        f"path={excinfo.value.path}\nexpected={link}"
    )
    assert excinfo.value.root == source, (
        f"the {source_type} refusal names the wrong source root.\n\n"
        f"root={excinfo.value.root}\nexpected={source}"
    )
    written = _written_vault_text(vault)
    assert _SENTINEL not in written, (
        f"the {source_type} ingestor followed a symlink out of the source "
        "tree and wrote the target's content into the vault as a "
        f"fragment.\n\n{written}"
    )
    assert _vault_fragments(vault) == [], (
        f"the {source_type} run refused but still wrote fragments; a "
        f"containment refusal writes nothing.\n\n{_vault_fragments(vault)}"
    )


# ---------------------------------------------------------------------------
# 3. Registry-driven: EVERY registered ingestor refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", sorted(INGESTOR_REGISTRY))
def test_every_registered_ingestor_refuses_an_escaping_link(
    tmp_path: Path,
    source_type: str,
) -> None:
    """RED for all eleven. The guard is the door, not eleven doormats.

    Driven straight off :data:`creek.ingest.INGESTOR_REGISTRY` so a twelfth
    ingestor is covered the day it is registered, with nobody having to
    remember. That is the property N per-ingestor copies of the predicate
    could never have, and it is the whole argument for putting the check on
    ``Ingestor.ingest`` — the single chokepoint every ingestor inherits.

    The planted link is a plain ``*.md``, and it deliberately does not match
    most of these ingestors' extension filters. It does not have to: the
    containment gate fires *before* ``discover()`` is called, so what the
    ingestor would or would not have claimed is irrelevant. If a lane here
    goes green only because the ingestor ignores ``.md``, the guard has been
    pushed down into the individual walks and the chokepoint is gone.

    ``image`` / ``presentation`` / ``spreadsheet`` are covered by this test
    and not by the shaped lanes above, because their real inputs are binary
    fixtures — and refusing before ``discover`` means no binary parser is
    reached at all.

    Args:
        tmp_path: Pytest-provided temporary directory.
        source_type: Registry key under test.
    """
    from creek._containment import EscapingSymlinkError

    source, link = _build_markdown(tmp_path / "lane", escaping=True)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        INGESTOR_REGISTRY[source_type]().ingest(source)

    assert excinfo.value.path == link, (
        f"the {source_type} ingestor refused, but named the wrong entry — "
        "which suggests the check is not the shared one.\n\n"
        f"path={excinfo.value.path}\nexpected={link}"
    )
    assert excinfo.value.root == source, (
        f"the {source_type} refusal names the wrong source root.\n\n"
        f"root={excinfo.value.root}\nexpected={source}"
    )


# ---------------------------------------------------------------------------
# 4. CodeIngestor descends INTO a symlinked directory
# ---------------------------------------------------------------------------


def test_code_ingestor_refuses_a_symlinked_directory_it_would_recurse_into(
    tmp_path: Path,
) -> None:
    """RED, and the reason the gate must check ``dirnames`` too.

    Unlike the ``rglob`` ingestors, ``creek/ingest/code.py::
    _discover_directory`` recurses by hand: ``sorted(dir_path.iterdir())``
    then ``if item.is_dir(): recurse``. ``Path.is_dir()`` FOLLOWS symlinks,
    so a link to a directory is descended into and the whole out-of-tree
    subtree is read — measured at HEAD as two discovered documents and two
    leaked fragments, where ``rglob`` would have surfaced the link and
    stopped.

    An ``os.walk(root, followlinks=False)`` gate closes this only if it
    inspects ``dirnames`` as well as ``filenames``: with ``followlinks``
    off, ``os.walk`` yields the link as an entry in ``dirnames`` and never
    descends, so a filenames-only gate would walk right past it and every
    other assertion in this file would still pass.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError

    base = tmp_path / "lane"
    vault = _make_vault(tmp_path)
    src = base / "src"
    src.mkdir(parents=True)
    (src / "main.py").write_text(f'"""{_IN_ROOT_MARKER}"""\n', encoding="utf-8")
    outside = base / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "README.md").write_text(
        f"# Secret\n\n{_SENTINEL}\n",
        encoding="utf-8",
    )
    (outside / "nested" / "deep.py").write_text(
        f'"""{_SENTINEL}"""\n',
        encoding="utf-8",
    )
    link = src / "linkdir"
    link.symlink_to(outside)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["code"],
            source_type="code",
            input_path=src,
            vault_path=vault,
        )

    assert excinfo.value.path == link, (
        "the refusal did not name the symlinked directory. A gate that "
        "only inspects os.walk's filenames never sees it, because "
        "followlinks=False reports a directory link under dirnames.\n\n"
        f"path={excinfo.value.path}\nexpected={link}"
    )
    written = _written_vault_text(vault)
    assert _SENTINEL not in written, (
        "CodeIngestor recursed through a symlinked directory and ingested "
        "an entire subtree from outside the source root — the widest leak "
        f"of the eleven.\n\n{written}"
    )
    assert _vault_fragments(vault) == [], (
        f"a refused run still wrote fragments.\n\n{_vault_fragments(vault)}"
    )


# ---------------------------------------------------------------------------
# 5. The NAMED source root is itself an escaping symlink
# ---------------------------------------------------------------------------


def test_a_named_source_root_that_is_an_escaping_symlink_is_refused(
    tmp_path: Path,
) -> None:
    """RED. ``--input <link-to-outside-dir>`` must be refused, not resolved.

    #1360's finding, in the ingest surface: a tree-walk-only guard passes
    this case completely. ``Path.is_dir()`` follows the link, so the walk
    runs over the *target's* tree, finds no links inside it, and launders
    every child as in-root. That is why the containment check must ask
    "is the named path itself an escaping link?" BEFORE it asks
    ``is_dir()`` — resolving first destroys the only evidence.

    ``root`` is deliberately not asserted here: the named root *is* the
    link, so the two fields coincide and pinning both would over-specify a
    detail the implementation is free to choose. ``path`` naming the link
    the operator typed is the load-bearing half.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError

    base = tmp_path / "lane"
    vault = _make_vault(tmp_path)
    outside = base / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.md").write_text(
        f"# Secret\n\n{_SENTINEL}\n",
        encoding="utf-8",
    )
    # The link must sit somewhere that does NOT contain the target, or it
    # does not escape its own parent and there is nothing to refuse.
    holder = base / "holder"
    holder.mkdir()
    link = holder / "linkdir"
    link.symlink_to(outside)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=link,
            vault_path=vault,
        )

    assert excinfo.value.path == link, (
        "the refusal does not name the path the operator typed.\n\n"
        f"path={excinfo.value.path}\nexpected={link}"
    )
    written = _written_vault_text(vault)
    assert _SENTINEL not in written, (
        "naming a symlinked directory as --input walked straight past the "
        "tree guard: is_dir() followed the link, the walk ran over the "
        "target's own tree, and every file in it was ingested as though it "
        f"were in root.\n\n{written}"
    )
    assert _vault_fragments(vault) == [], (
        f"a refused run still wrote fragments.\n\n{_vault_fragments(vault)}"
    )


# ---------------------------------------------------------------------------
# 6. The mass-tombing hazard — why re-raising is not "merely defensive"
# ---------------------------------------------------------------------------


def test_a_containment_refusal_does_not_tomb_previously_ingested_fragments(
    tmp_path: Path,
) -> None:
    """RED, and the load-bearing argument for propagating the refusal.

    ``Ingestor._discover_safe`` currently swallows every exception into
    ``result.errors`` and returns ``[]``. If a containment refusal
    collapsed into that arm, ``run_ingest`` would carry on with an empty
    ``seen_keys`` and reach ``tomb_missing_units``
    (``creek/ingest/pipeline.py:428``, defined at ``:267``), which soft-tombs
    every ``ledger.live_keys() - seen_keys`` whenever the ledger exists and
    ``input_path.is_dir()``. Markdown is the ledger-backed source. So the
    quiet version of this fix does not merely fail to protect the vault —
    it DELETES it: one stale symlink dropped into a journal folder would
    move every previously-ingested fragment into ``10-Liminal/Orphaned``.

    A containment refusal must never become a mass deletion. The assertions
    are on the artifacts the tombing machinery actually produces — live
    files under ``01-Fragments``, tombstones under ``10-Liminal/Orphaned``,
    and the ledger's own ``live_keys`` — rather than on a proxy such as the
    ``tombed`` counter, which a swallowed refusal would report honestly
    while the damage was already on disk.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError
    from creek.ingest.ledger import SourceLedger

    base = tmp_path / "lane"
    vault = _make_vault(tmp_path)
    journal = base / "journal"
    journal.mkdir(parents=True)
    for day in ("01", "02", "03"):
        (journal / f"2026-06-{day}.md").write_text(
            f"---\ndate: 2026-06-{day}\n---\nEntry {day}. {_IN_ROOT_MARKER}\n",
            encoding="utf-8",
        )

    first = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=journal,
        vault_path=vault,
    )
    assert first.errors == [], f"setup ingest failed.\n\n{first.errors}"
    live_before = _vault_fragments(vault)
    assert len(live_before) == 3, (
        "the setup pass did not write three fragments, so this test cannot "
        f"tell 'nothing was tombed' from 'nothing was there'.\n\n"
        f"{live_before}"
    )
    assert _vault_orphans(vault) == [], "setup pass tombed something"
    keys_before = SourceLedger.load(vault, source="markdown").live_keys()
    assert len(keys_before) == 3, (
        f"the ledger did not record three live units.\n\n{keys_before}"
    )

    # One stale link appears in an otherwise unchanged, already-ingested
    # source folder. This is the ordinary way it happens in the wild.
    victim = _park_outside(base, "secret.md", f"# Secret\n\n{_SENTINEL}\n")
    (journal / "link.md").symlink_to(victim)

    with pytest.raises(EscapingSymlinkError):
        run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=journal,
            vault_path=vault,
        )

    assert _vault_fragments(vault) == live_before, (
        "the refusal cost the operator their vault. A swallowed "
        "EscapingSymlinkError leaves seen_keys empty, and tomb_missing_units "
        "then soft-tombs every previously-ingested unit as though the "
        "source had been deleted. A containment refusal must propagate out "
        f"of run_ingest before that loop is reached.\n\n"
        f"now={_vault_fragments(vault)}\nbefore={live_before}"
    )
    assert _vault_orphans(vault) == [], (
        "fragments were moved into 10-Liminal/Orphaned by a run that "
        f"refused to read its source at all.\n\n{_vault_orphans(vault)}"
    )
    keys_after = SourceLedger.load(vault, source="markdown").live_keys()
    assert keys_after == keys_before, (
        "the ledger marked live units as tombed on a run that never "
        f"discovered anything.\n\nafter={keys_after}\nbefore={keys_before}"
    )
    assert _SENTINEL not in _written_vault_text(vault), (
        "the out-of-tree file was ingested on the second pass.\n\n"
        f"{_written_vault_text(vault)}"
    )


# ---------------------------------------------------------------------------
# 7. Ordering pin: the CLI refuses BEFORE the consent gate reads the tree
# ---------------------------------------------------------------------------


def test_cli_ingest_refuses_before_the_consent_gate_reads_the_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED. The refusal must land above ``_gate_consent``, not below it.

    ``creek/cli.py``'s ingest handler calls ``_gate_consent`` before it
    calls ``_run_ingest``. For a first-time (unconsented) source that path
    runs ``ConsentManager.get_source_summary`` ->
    ``creek/consent.py:134 _build_source_summary``, which walks
    ``source_path.rglob("*")`` and ``stat()``s every file it finds —
    following escaping links, and folding out-of-tree file counts and byte
    totals into the very summary the operator is asked to approve. That is
    an out-of-root read performed *before* anything refuses, and it is the
    ingest analogue of #1360's "refuses before reading config through the
    link". A guard that sits only inside ``_run_ingest`` blocks the write
    and still leaks the shape of the victim directory into the consent
    prompt.

    The spy is a RECORDING wrapper that delegates to the real summariser,
    never a raising sentinel: ``CliRunner`` catches a raised sentinel and
    reports exit 1, which would manufacture a pass for the exit-code
    assertion at HEAD.

    ``--yes`` is passed deliberately. Without it the non-interactive branch
    exits 1 on its own, and the exit-code assertion would hold at HEAD for
    entirely the wrong reason.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture, used to install the spy.
    """
    from creek import consent as consent_module

    vault = _make_vault(tmp_path)
    source, _link = _build_markdown(tmp_path / "lane", escaping=True)

    real_summary = consent_module._build_source_summary
    calls: list[object] = []

    def _recording_summary(source_path, exclusions):
        """Record the call, then delegate to the real summariser."""
        calls.append(source_path)
        return real_summary(source_path, exclusions)

    monkeypatch.setattr(
        consent_module,
        "_build_source_summary",
        _recording_summary,
    )

    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 1, (
        "creek ingest accepted a source tree containing a symlink that "
        "leaves it, ingested the target, and exited 0. Errors on this "
        "surface print as a dim yellow list under a zero exit, which is "
        "why the contract here is refusal rather than a collected "
        f"error.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    assert "symlink" in result.output.lower(), (
        "the refusal does not say why it refused; an operator cannot act "
        "on an unexplained exit 1, and a bare traceback swallowed by "
        f"CliRunner produces exactly this.\n\n{result.output}"
    )
    assert not calls, (
        "the consent summary ran before the refusal, so the run walked the "
        "source tree with rglob and stat()'d straight through the escaping "
        "link — folding out-of-tree file counts and byte totals into the "
        "summary the operator was asked to approve. The write was blocked "
        "later, but the out-of-root READ had already happened.\n\n"
        f"calls={calls}\nexit_code={result.exit_code}\n{result.output}"
    )
    assert _SENTINEL not in _written_vault_text(vault), (
        f"the CLI ingested the out-of-tree file.\n\n{result.output}"
    )


# ---------------------------------------------------------------------------
# 8. Over-breadth guards (must pass BEFORE and AFTER)
# ---------------------------------------------------------------------------


def test_an_intra_tree_symlink_is_still_ingested(tmp_path: Path) -> None:
    """PASSES AT HEAD AND AFTER. Containment is about escaping, not linking.

    "Refuse any symlink under the source root" would satisfy every RED in
    this file while breaking the ordinary case of an alias sitting beside
    the file it aliases. ``tests/test_cli_redact.py`` holds the equivalent
    guard for the redact surface
    (``test_redact_apply_admits_a_named_intra_tree_symlink``); this is the
    ingest half.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    base = tmp_path / "lane"
    vault = _make_vault(tmp_path)
    src = base / "src"
    src.mkdir(parents=True)
    (src / "real.md").write_text(
        f"# Real\n\n{_IN_ROOT_MARKER}\n",
        encoding="utf-8",
    )
    (src / "alias.md").symlink_to(src / "real.md")

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )

    assert result.discovered == 2, (
        "the intra-tree alias was not even discovered, so this test cannot "
        "tell an over-broad refusal from a walk that never saw the "
        f"link.\n\ndiscovered={result.discovered}\nerrors={result.errors}"
    )
    assert result.written >= 1, (
        "a symlink whose target sits inside the source root was refused or "
        "dropped; the guard is over-broad and now breaks ordinary "
        f"aliases.\n\nerrors={result.errors}"
    )
    assert _IN_ROOT_MARKER in _written_vault_text(vault), (
        "the run reported writes but the aliased content is not in the "
        f"vault.\n\nerrors={result.errors}"
    )


def test_a_source_root_reached_through_a_symlinked_component_still_ingests(
    tmp_path: Path,
) -> None:
    """PASSES AT HEAD AND AFTER. Pins resolve-the-root / lstat-the-leaf.

    The named root here is itself a symlink, but one whose target stays
    under its own parent — the portable, deterministic analogue of macOS's
    ``/tmp`` -> ``/private/tmp``, where every ``tmp_path`` in this suite
    lives. It must ingest normally.

    This is the test that goes red if the containment check is "tightened"
    into resolving non-symlink children as well: with the root resolved to
    its real location and each child resolved independently, every child of
    a root reached through a link compares as outside and the whole tree
    becomes un-ingestable. #1087 and #1293 both landed on the same policy
    for exactly this reason, and #1294 must not diverge from it.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    base = tmp_path / "lane"
    vault = _make_vault(tmp_path)
    real_root = base / "real-root"
    real_root.mkdir(parents=True)
    (real_root / "note.md").write_text(
        f"# Note\n\n{_IN_ROOT_MARKER}\n",
        encoding="utf-8",
    )
    # Target sits under the link's own parent, so the link does not escape.
    alias_root = base / "alias-root"
    alias_root.symlink_to(real_root)

    result = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=alias_root,
        vault_path=vault,
    )

    assert result.written >= 1, (
        "a source root reached through a symlinked component was refused. "
        "The policy is: resolve the ROOT once, lstat only the LEAF, and "
        "never resolve a non-symlink child — resolving children too flags "
        "every child of such a root as escaping, which on macOS is every "
        f"tmp_path in this suite.\n\nerrors={result.errors}"
    )
    assert _IN_ROOT_MARKER in _written_vault_text(vault), (
        f"nothing from the aliased root reached the vault.\n\n{result.errors}"
    )


# ---------------------------------------------------------------------------
# 9. ``recurse_symlinks`` bound pin
# ---------------------------------------------------------------------------


def test_rglob_does_not_descend_into_a_symlinked_directory(tmp_path: Path) -> None:
    """TRIPWIRE (passes today). Pin the assumption the leaf check rests on.

    The containment check is sound *per leaf* only because the walks it
    guards surface a symlinked directory as one entry and do not descend
    into it: ``Path.rglob("*")`` yields the link itself and nothing
    beneath. Confirmed on this repo's interpreter (3.12); 3.13 added an
    explicit ``recurse_symlinks`` parameter defaulting to ``False``.

    If a future Python flips that default, the leaf-only check silently
    widens from "the link is checked" to "everything under the link is
    walked unchecked" — a re-opened #1294 with no test failing anywhere.
    This is that tripwire.

    ``recurse_symlinks=`` is deliberately NOT passed: the parameter does
    not exist on 3.11 or 3.12, which CI still tests, so passing it
    unconditionally would be a TypeError on two of the three lanes.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deep.md").write_text("deep\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "plain.md").write_text("plain\n", encoding="utf-8")
    link = root / "linkdir"
    link.symlink_to(outside)

    entries = set(root.rglob("*"))

    assert link in entries, (
        "rglob no longer yields a symlinked directory at all, so the "
        "leaf-only containment check would never be offered the link to "
        f"inspect.\n\n{sorted(entries)}"
    )
    assert (link / "deep.md") not in entries, (
        "rglob descended INTO a symlinked directory. Every leaf-only "
        "containment check in creek — the redact scanner's walk (#1087), "
        "the named-path guard (#1293) and the ingest guard (#1294) — "
        "assumes it does not. The guard now inspects the link but the walk "
        "reads everything beneath it unchecked. Do not fix this by passing "
        "recurse_symlinks=False (absent on 3.11/3.12); fix the walks.\n\n"
        f"{sorted(entries)}"
    )


# ---------------------------------------------------------------------------
# 10. Dangling and looping links
# ---------------------------------------------------------------------------


def test_a_dangling_link_pointing_outside_the_root_is_refused(
    tmp_path: Path,
) -> None:
    """RED. A broken escape is still an escape.

    ``resolves_within`` uses ``resolve(strict=False)``, so a dangling link
    still resolves to a *candidate* location worth comparing. A link
    pointing outside the root is refused whether or not its target happens
    to exist today: whether the file is there is a race, not a property,
    and admitting the link means the next run — after somebody creates the
    target — reads out of root with nothing having changed in Creek.

    Asserted against the predicate rather than a full ingest on purpose:
    at HEAD ``MarkdownIngestor._read_directory`` calls ``read_bytes()`` on
    every ``rglob`` hit including a dangling link, so an ingest-level test
    would go red on a ``FileNotFoundError`` that has nothing to do with
    containment.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError, assert_source_contained

    src = tmp_path / "src"
    src.mkdir()
    (src / "innocent.md").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    link = src / "dangling.md"
    link.symlink_to(tmp_path / "outside" / "never-created.md")

    with pytest.raises(EscapingSymlinkError) as excinfo:
        assert_source_contained(src)

    assert excinfo.value.path == link, (
        "the refusal names the wrong entry.\n\n"
        f"path={excinfo.value.path}\nexpected={link}"
    )


def test_a_dangling_link_pointing_inside_the_root_is_admitted(
    tmp_path: Path,
) -> None:
    """PASSES AFTER. A broken in-tree link is not a containment failure.

    The other half of the dangling decision, and the guard against
    implementing "any link we cannot stat is an escape". A link pointing at
    a path that would sit *inside* the root is contained by the same
    ``strict=False`` reasoning that condemns the outward-pointing one; it
    is a broken alias, which is a different problem with a different owner.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import assert_source_contained

    src = tmp_path / "src"
    src.mkdir()
    (src / "innocent.md").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    (src / "dangling.md").symlink_to(src / "never-created.md")

    assert assert_source_contained(src) is None, (
        "a dangling link whose candidate target sits inside the root was "
        "refused. Containment is about where the target would be, not "
        "about whether the link resolves; refusing here turns every stale "
        "in-tree alias into a hard stop on the whole source."
    )


def test_a_symlink_loop_under_the_root_is_refused(tmp_path: Path) -> None:
    """RED. An unprovable containment is an escape.

    ``a -> b -> a`` raises ``RuntimeError`` (or ``OSError(ELOOP)``,
    depending on platform and Python version) from ``resolve``.
    ``resolves_within`` classifies both as not-contained, deliberately: the
    guard cannot prove the target is inside, and a safety check that
    admits what it cannot verify is not a safety check. Pinned here so the
    classification is a decision on the record rather than an accident of
    which exception ``resolve`` happens to raise.

    Either link may be reported — ``os.walk`` does not promise an order
    within a directory — so the assertion accepts both.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek._containment import EscapingSymlinkError, assert_source_contained

    src = tmp_path / "src"
    src.mkdir()
    a = src / "a.md"
    b = src / "b.md"
    a.symlink_to(b)
    b.symlink_to(a)

    with pytest.raises(EscapingSymlinkError) as excinfo:
        assert_source_contained(src)

    assert excinfo.value.path in (a, b), (
        "the loop was refused but the refusal names neither link in it, so "
        "the operator has nothing to delete.\n\n"
        f"path={excinfo.value.path}"
    )


# ---------------------------------------------------------------------------
# 11. Non-disclosure
# ---------------------------------------------------------------------------


def test_the_refusal_never_names_the_resolved_out_of_tree_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED. The refusal names the link the operator can see, and nothing else.

    Disclosing the resolved target is the exact oracle #1087 closes: an
    attacker who can plant a link learns whether a guessed path exists and
    what it is called, from a tool that refused to read it. This is why
    ``creek/redact/scanner.py::resolves_within`` deliberately drops the
    exception object — ``relative_to``'s message quotes the resolved
    target.

    The bare FILENAME is asserted alongside the absolute path because Rich
    truncates long paths to the console width; the same trap is documented
    at ``tests/test_cli_redact.py:638-644``, where an assertion on the
    absolute path alone would hold no matter what was printed.

    The final assertion is the non-vacuity guard: a refusal that says
    nothing at all would satisfy every non-disclosure assertion above it.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log capture — the logs must not disclose it either.
    """
    from creek._containment import EscapingSymlinkError, assert_source_contained

    base = tmp_path / "lane"
    src = base / "src"
    (src / "nested").mkdir(parents=True)
    (src / "innocent.md").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    victim = _park_outside(
        base,
        "payroll-2026-confidential.md",
        f"# Secret\n\n{_SENTINEL}\n",
    )
    link = src / "nested" / "link.md"
    link.symlink_to(victim)

    with caplog.at_level(logging.WARNING), pytest.raises(EscapingSymlinkError) as exc:
        assert_source_contained(src)

    message = str(exc.value)
    assert str(victim) not in message, (
        "the exception message spells out the resolved out-of-tree target. "
        "Report the path the operator typed or the link the walk found — "
        f"never the one it points at.\n\n{message}"
    )
    assert victim.name not in message, (
        "the exception message names the out-of-tree file. Asserted on the "
        "bare filename as well as the absolute path because Rich truncates "
        "long paths, so a str(victim) assertion alone holds no matter what "
        f"was printed.\n\n{message}"
    )
    disclosing = [
        record.getMessage()
        for record in caplog.records
        if str(victim) in record.getMessage() or victim.name in record.getMessage()
    ]
    assert not disclosing, (
        f"a log record spells out the resolved out-of-tree target.\n\n{disclosing}"
    )
    assert link.name in message or str(link) in message, (
        "the refusal names nothing at all, so every non-disclosure "
        "assertion above passes over an empty message and the operator has "
        f"no idea which entry to remove.\n\n{message}"
    )


# ---------------------------------------------------------------------------
# 12. One definition of the containment predicate
# ---------------------------------------------------------------------------


def test_the_containment_predicate_has_exactly_one_definition() -> None:
    """RED. Three consumers, one definition — the issue's explicit ask.

    #1294 asks for "one shared confinement predicate rather than a third
    copy". ``resolves_within`` was made public by #1360 so the scanner's
    walk and the named-path guard could share it; the ingest guard is the
    third consumer, and the only way to keep that honest over time is to
    assert object identity rather than equal behaviour. Two copies that
    agree today are two copies that will disagree after the next fix lands
    in one of them.

    ``named_path_escapes`` moves out of
    ``creek/redact/cli_commands.py::_named_path_escapes`` for the same
    reason. The private alias may or may not survive the move; if it does,
    it must be the same object, not a re-implementation.
    """
    from creek import _containment
    from creek.redact import cli_commands, scanner

    assert scanner.resolves_within is _containment.resolves_within, (
        "creek.redact.scanner defines its own resolves_within instead of "
        "re-exporting the canonical one. The scanner walk, the named-path "
        "guard and the ingest guard must share a single definition of "
        "'inside', or a future fix to one of them silently leaves the "
        f"others behind.\n\n{scanner.resolves_within!r}\n"
        f"{_containment.resolves_within!r}"
    )
    assert callable(_containment.named_path_escapes), (
        "creek._containment does not expose named_path_escapes, so the "
        "named-root check has no shared home and cli_commands keeps its "
        "private copy."
    )
    legacy = getattr(cli_commands, "_named_path_escapes", None)
    assert legacy is None or legacy is _containment.named_path_escapes, (
        "creek.redact.cli_commands still defines its own named-path "
        "predicate. Either drop the name or alias the canonical one; a "
        "second body is the drift this test exists to prevent.\n\n"
        f"{legacy!r}"
    )


# ---------------------------------------------------------------------------
# 13. The MCP surface refuses too
# ---------------------------------------------------------------------------


def test_mcp_ingest_tool_refuses_an_escaping_link(tmp_path: Path) -> None:
    """RED. ``creek.ingest`` over MCP returns a refusal, not a leak.

    ``creek_mcp/tools/ingest.py:100`` calls ``ingestor_cls().ingest(resolved)``
    directly, so once the gate raises, an unhandled ``EscapingSymlinkError``
    would surface to an MCP client as a transport-level crash rather than
    the structured ``status: "refused"`` every other refusal on this surface
    returns. The tool's own path confinement does not help here: the source
    is legitimately inside the vault; it is the link *under* it that leaves.

    ``privacy_tier_ceiling`` is passed explicitly. The parameter's default is
    ``TierCeiling.OPEN``, which the tool already refuses outright because the
    ingest default tier is ``personal`` — so a defaulted call would return
    ``status: "refused"`` at HEAD for an entirely unrelated reason and this
    test would pass without the fix. Asserting that the reason names the
    symlink is the second half of that guard.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek_mcp.tier_ceiling import TierCeiling
    from creek_mcp.tools.ingest import ingest_tool

    vault = _make_vault(tmp_path)
    inbox = vault / "inbox"
    inbox.mkdir()
    (inbox / "innocent.md").write_text(f"{_IN_ROOT_MARKER}\n", encoding="utf-8")
    victim = _park_outside(tmp_path / "lane", "secret.md", f"{_SENTINEL}\n")
    (inbox / "link.md").symlink_to(victim)

    response = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path="inbox",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )

    assert response["status"] == "refused", (
        "the MCP ingest tool ran an ingest over a tree that links out of "
        "itself. Refusals on this surface are structured responses, not "
        f"exceptions.\n\n{response}"
    )
    assert "symlink" in str(response.get("reason", "")).lower(), (
        f"the refusal does not say why it refused.\n\n{response}"
    )
    assert _vault_fragments(vault) == [], (
        f"a refused MCP ingest still wrote fragments.\n\n{_vault_fragments(vault)}"
    )


# ---------------------------------------------------------------------------
# 13. Mutation-battery backfill
#
# Both tests below were written because a mutation SURVIVED the suite above.
# Neither pins a new requirement; each pins a property the implementation
# already had but that nothing could tell the absence of.
# ---------------------------------------------------------------------------


def test_the_containment_walk_visits_each_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must not re-enter an intra-root symlinked directory.

    Mutation survivor: flipping ``os.walk(..., followlinks=False)`` to
    ``followlinks=True`` left every other test in this file green. It has to,
    because for an *escaping* link the answer is identical either way — the
    link is caught in ``dirnames`` at the level it sits on, before descent
    would matter. What changes is the work done on an *admitted* one.

    An intra-tree symlinked directory is legitimate and common (``latest ->
    2026-08-01`` inside an export). Following it makes the walk re-traverse
    the aliased subtree; when the alias points at an ancestor the walk
    re-enters itself and only stops when the kernel raises ``ELOOP``. Measured
    on this fixture: 3 entries with ``followlinks=False``, 48 with it on — the
    same verdict for 16x the syscalls, and the multiplier grows with depth.

    Asserted by counting walk roots rather than wall-clock time, so it cannot
    flake on a loaded machine. Spying on the module-level ``os.walk`` follows
    the precedent set by ``tests/test_cli_redact.py``'s
    ``test_redact_apply_refuses_a_named_symlinked_directory_before_walking_it``.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture, used to install the spy.
    """
    from creek import _containment

    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "note.md").write_text(_IN_ROOT_MARKER, encoding="utf-8")
    # Points at its own grandparent: intra-root, so admitted, and a cycle.
    (src / "a" / "loop").symlink_to(src)

    real_walk = _containment.os.walk
    roots: list[str] = []

    def _spy_walk(top: Any, *args: Any, **kwargs: Any) -> Any:
        """Record each walk root, then delegate to the real ``os.walk``."""
        for entry in real_walk(top, *args, **kwargs):
            roots.append(str(entry[0]))
            yield entry

    monkeypatch.setattr(_containment.os, "walk", _spy_walk)

    assert _containment.assert_source_contained(src) is None, (
        "an intra-root symlinked directory was refused. The alias never "
        "leaves the tree, so containment has nothing to object to."
    )
    assert len(roots) == len(set(roots)), (
        "the walk visited the same directory twice, so it descended through "
        "the intra-root alias instead of treating it as a leaf. On a cycle "
        "that only terminates when the kernel raises ELOOP.\n\n"
        f"roots={roots}"
    )
    assert len(roots) == 2, (
        "expected exactly the two real directories (the root and 'a'); a "
        f"different count means the walk shape changed.\n\nroots={roots}"
    )


def test_an_unreadable_markdown_entry_does_not_tomb_the_whole_source(
    tmp_path: Path,
) -> None:
    """A dangling in-tree ``*.md`` link must not orphan every fragment.

    Mutation survivor: deleting ``MarkdownIngestor._read_directory``'s
    ``is_file()`` filter left the suite green, because every other fixture
    here either refuses before discovery or contains only readable files.

    The filter is not cosmetic. ``rglob("*.md")`` yields directories and
    dangling links as readily as files, and that walk — alone among the
    eleven ingestors — called ``read_bytes()`` on whatever it got. The raise
    lands in ``_discover_safe``, which collects it and returns ``[]``; an
    empty discovery leaves ``seen_keys`` empty; and ``tomb_missing_units``
    then soft-tombs every live ledger key. So one stale alias inside the
    source silently orphans every fragment previously ingested from it.

    This is the same failure the containment refusal is routed around,
    reached through a path containment deliberately admits: the link points
    *inside* the root, so there is nothing to refuse.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = _make_vault(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    for name in ("one.md", "two.md", "three.md"):
        (src / name).write_text(
            f"# {name}\n\n{_IN_ROOT_MARKER}\n",
            encoding="utf-8",
        )

    first = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )
    assert first.written == 3, (
        "the control ingest did not write all three fragments, so the "
        f"tombing assertion below would be vacuous.\n\n{first.errors}"
    )
    before = _vault_fragments(vault)

    # Admitted by containment: the candidate target is inside the root.
    (src / "stale.md").symlink_to(src / "never-created.md")

    second = run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=src,
        vault_path=vault,
    )

    assert _vault_orphans(vault) == [], (
        "an unreadable entry in the source tombed the fragments that were "
        "still perfectly present. discover() raised, _discover_safe "
        "swallowed it into errors and returned [], and tomb_missing_units "
        f"orphaned every live ledger key.\n\n{second.errors}"
    )
    assert _vault_fragments(vault) == before, (
        "the live fragment set changed after re-ingesting an unchanged "
        f"source.\n\nbefore={before}\nafter={_vault_fragments(vault)}"
    )
