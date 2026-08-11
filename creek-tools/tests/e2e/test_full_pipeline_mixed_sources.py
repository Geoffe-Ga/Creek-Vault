"""End-to-end guard: one ingestor's output per source file (issue #1304).

``Pipeline._run_ingestion`` used to hand the whole source tree to every
entry in :data:`creek.ingest.INGESTOR_REGISTRY` and keep everything each
one returned. Extensions claimed by more than one ingestor therefore
landed in the vault **twice or three times** — and never as an overwrite,
because :func:`creek.vault.writer` de-collides colliding filenames with a
``-N`` suffix. Measured on this branch's parent (``3796c39``):

* ``{note.html, log.txt, a.md}`` -> **5** fragment files, zero errors.
* The seven-file mixed tree below -> **14** fragments.

Every assertion here is on **persisted vault state**, not on which
ingestor the router picked: the defect was duplicate files on disk, so
that is what must be counted. Fragments are attributed back to their
source via the ``source.original_file`` frontmatter key that every
ingestor writes.

Deliberately *not* asserted: one fragment per source file. Several
ingestors correctly emit N>1 for a single file — ``SpreadsheetIngestor``
emits one per non-empty sheet (see issue #1305), ``CodeIngestor`` one per
module/function/class, the chat ingestors one per turn pair. The
invariant is one *ingestor* per source file.

``test_chat_exports_still_produce_fragments`` is green before the fix and
must stay green: the chat ingestors claim ``.json`` by sniffing content,
not by extension, so any extension-table router would silently zero them.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import frontmatter
import pytest

from creek.config import CreekConfig
from creek.pipeline import Pipeline

pytestmark = pytest.mark.e2e

_CHATGPT_EXPORT = (
    '[{"title": "A chat about creeks", "create_time": 1700000000.0, '
    '"mapping": {"root": {"id": "root", "message": null, "parent": null, '
    '"children": ["u1"]}, '
    '"u1": {"id": "u1", "parent": "root", "children": ["a1"], "message": '
    '{"id": "u1", "author": {"role": "user"}, "create_time": 1700000000.0, '
    '"content": {"content_type": "text", "parts": ["What is an eddy?"]}}}, '
    '"a1": {"id": "a1", "parent": "u1", "children": [], "message": '
    '{"id": "a1", "author": {"role": "assistant"}, "create_time": 1700000001.0, '
    '"content": {"content_type": "text", "parts": ["A slow swirl of water."]}}}}}]'
)

_DISCORD_MESSAGES = (
    '[{"ID": "1", "Timestamp": "2024-01-01T00:00:00+00:00", '
    '"Contents": "First message in the channel.", "Attachments": ""}, '
    '{"ID": "2", "Timestamp": "2024-01-01T00:01:00+00:00", '
    '"Contents": "Second message in the channel.", "Attachments": ""}]'
)


def _fragment_files(vault: Path) -> list[Path]:
    """Return every fragment file the pipeline persisted, sorted by path."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _fragments_per_source(vault: Path) -> Counter[str]:
    """Count persisted fragment files per source file basename.

    Reads ``source.original_file`` from each fragment's frontmatter —
    the provenance key every ingestor writes — so a source file ingested
    twice shows up as a count of two regardless of how the vault writer
    de-collided the two filenames.

    Args:
        vault: Root of the vault the pipeline wrote into.

    Returns:
        Basename of each source file mapped to how many fragment files
        in the vault cite it.
    """
    counts: Counter[str] = Counter()
    for path in _fragment_files(vault):
        source = frontmatter.load(path).metadata.get("source", {})
        original = source.get("original_file") if isinstance(source, dict) else None
        counts[Path(str(original)).name if original else "<unknown>"] += 1
    return counts


def _body_for(vault: Path, source_name: str) -> str:
    """Return the body of the single fragment ingested from *source_name*.

    Args:
        vault: Root of the vault the pipeline wrote into.
        source_name: Basename of the source file to look up.

    Returns:
        The fragment body text.

    Raises:
        AssertionError: When the vault holds anything other than exactly
            one fragment for *source_name*.
    """
    bodies = []
    for path in _fragment_files(vault):
        post = frontmatter.load(path)
        source = post.metadata.get("source", {})
        original = source.get("original_file") if isinstance(source, dict) else None
        if original and Path(str(original)).name == source_name:
            bodies.append(post.content)
    assert len(bodies) == 1, f"expected exactly one fragment for {source_name}"
    return bodies[0]


def _write_mixed_tree(source: Path) -> None:
    """Populate *source* with one file per contested-extension family.

    Covers every overlap the registry actually has: ``.md`` (code x
    markdown, via the README/ADR patterns ``CodeIngestor`` claims),
    ``.html`` three ways (substack x document x generic), ``.csv``
    (spreadsheet x generic), ``.py`` (code x generic), plus a plain
    ``.json`` that no ingestor claims at all.

    Args:
        source: Empty source directory to fill.
    """
    (source / "README.md").write_text(
        "---\ntitle: My Readme\ntags: [creek]\n---\n\n# Readme\n\nReadme body.\n",
        encoding="utf-8",
    )
    adr = source / "docs" / "architecture" / "ADR"
    adr.mkdir(parents=True)
    (adr / "0001-thing.md").write_text(
        "# ADR 1: Thing\n\nWe decided the thing.\n", encoding="utf-8"
    )
    (source / "163000000.my-post.html").write_text(
        "<html><body><h1>My Post</h1><p>A substack post body.</p></body></html>",
        encoding="utf-8",
    )
    (source / "plain.html").write_text(
        "<html><body><p>Plain html body here.</p></body></html>", encoding="utf-8"
    )
    (source / "sheet.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    (source / "mod.py").write_text(
        '"""Module docstring."""\n\n\ndef f() -> int:\n'
        '    """Return one."""\n    return 1\n',
        encoding="utf-8",
    )
    (source / "notes.json").write_text('{"unclaimed": true}\n', encoding="utf-8")


def test_process_writes_one_fragment_per_source_file(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """The issue's own scenario: three files must write three fragments.

    Pre-fix this wrote five — ``log.txt`` and ``note.html`` were each
    claimed by both ``DocumentIngestor`` and ``GenericIngestor``.
    """
    (synthetic_source / "note.html").write_text(
        "<html><body><h1>Title</h1><p>Hello world body text.</p></body></html>",
        encoding="utf-8",
    )
    (synthetic_source / "log.txt").write_text(
        "plain text line one\nline two\n", encoding="utf-8"
    )
    (synthetic_source / "a.md").write_text(
        "# A\n\nSome markdown body.\n", encoding="utf-8"
    )

    result = Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    assert result.errors == []
    assert result.fragments_created == 3
    files = _fragment_files(synthetic_vault)
    assert len(files) == 3, [p.name for p in files]
    assert result.fragments_created == len(files)
    assert _fragments_per_source(synthetic_vault) == Counter(
        {"note.html": 1, "log.txt": 1, "a.md": 1}
    )
    # The vault writer de-collides duplicates with a ``-N`` suffix rather
    # than overwriting, so a ``-1`` stem is the on-disk signature of the
    # defect and must not appear.
    assert not [p.name for p in files if p.stem.endswith("-1")]


def test_contested_text_files_keep_the_specialist_rendering(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """The surviving body must be the specialist's, not the fallback's.

    Arbitration decides which ingestor's *body* reaches the vault, and
    the body feeds the fragment id, so the winner is load-bearing:
    ``DocumentIngestor`` renders ``log.txt`` with its first line promoted
    to a heading, while ``GenericIngestor`` dumps the text verbatim and
    wraps ``.html`` in a fenced code block.
    """
    (synthetic_source / "log.txt").write_text(
        "plain text line one\nline two\n", encoding="utf-8"
    )
    (synthetic_source / "plain.html").write_text(
        "<html><body><p>Plain html body here.</p></body></html>", encoding="utf-8"
    )

    Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    assert _body_for(synthetic_vault, "log.txt").startswith("# plain text line one")
    html_body = _body_for(synthetic_vault, "plain.html")
    assert "Plain html body here." in html_body
    assert "```html" not in html_body


def test_mixed_extension_tree_routes_one_ingestor_per_file(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Every contested extension resolves to exactly one ingestor.

    Pre-fix this tree produced 14 fragments. Note ``mod.py`` keeps
    **two** — ``CodeIngestor`` emits one per module and one per function
    — which is why the invariant is one ingestor per file and never one
    fragment per file (cf. ``SpreadsheetIngestor``'s per-sheet model,
    issue #1305).
    """
    _write_mixed_tree(synthetic_source)

    result = Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    assert _fragments_per_source(synthetic_vault) == Counter(
        {
            "README.md": 1,
            "0001-thing.md": 1,
            "163000000.my-post.html": 1,
            "plain.html": 1,
            "sheet.csv": 1,
            "mod.py": 2,
        }
    )
    assert len(_fragment_files(synthetic_vault)) == 7
    assert result.fragments_created == 7
    # ``.md`` goes to MarkdownIngestor, which splits YAML frontmatter out
    # of the body; CodeIngestor would have left the ``---`` block inline.
    assert not _body_for(synthetic_vault, "README.md").startswith("---")


def test_unclaimed_source_files_are_reported_not_dropped(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """A file no ingestor claims is surfaced rather than vanishing.

    A plain ``.json`` that is not a chat export has always produced zero
    fragments — the sniffers reject it and ``GenericIngestor`` excludes
    ``.json`` outright. Routing does not fix that, but it must stop it
    being silent.
    """
    (synthetic_source / "notes.json").write_text('{"a": 1}\n', encoding="utf-8")
    (synthetic_source / "kept.md").write_text("# Kept\n\nBody.\n", encoding="utf-8")

    result = Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    assert [Path(p).name for p in result.unclaimed_sources] == ["notes.json"]
    assert result.fragments_created == 1


def test_contested_sources_are_reported_for_migration(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Arbitrated files are named so an operator can find stale duplicates.

    A vault ingested before this change already holds the losing
    ingestor's fragment, and nothing re-derives or removes it. Reporting
    the contested paths is what makes those orphans findable.
    """
    (synthetic_source / "log.txt").write_text("only a line\n", encoding="utf-8")
    (synthetic_source / "solo.md").write_text("# Solo\n\nBody.\n", encoding="utf-8")

    result = Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    assert [Path(p).name for p in result.contested_sources] == ["log.txt"]


def test_chat_exports_still_produce_fragments(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Content- and structure-sniffed ingestors must survive routing.

    Green before the fix and required to stay green. ``ChatGPTIngestor``
    and ``DiscordIngestor`` both claim ``.json`` paths that no extension
    table routes to them, so any extension-keyed router would drop their
    output to zero — silently, because ``GenericIngestor`` excludes
    ``.json`` and cannot pick up the slack.
    """
    (synthetic_source / "conversations.json").write_text(
        _CHATGPT_EXPORT, encoding="utf-8"
    )
    channel = synthetic_source / "messages" / "c123456"
    channel.mkdir(parents=True)
    (channel / "messages.json").write_text(_DISCORD_MESSAGES, encoding="utf-8")
    (channel / "channel.json").write_text(
        '{"id": "c123456", "name": "general"}', encoding="utf-8"
    )

    result = Pipeline(config=CreekConfig()).run(
        source_path=synthetic_source, vault_path=synthetic_vault
    )

    platforms = Counter(
        str(frontmatter.load(p).metadata.get("source", {}).get("platform"))
        for p in _fragment_files(synthetic_vault)
    )
    assert platforms["chatgpt"] >= 1, platforms
    assert platforms["discord"] >= 1, platforms
    assert result.fragments_created == sum(platforms.values())
