"""Tests for the vault linker module.

Verifies that VaultLinker correctly reads vault fragment files,
injects wiki-links into markdown body and frontmatter for resonances,
threads, and eddies, handles batch processing, ensures idempotency,
and removes stale links.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.linker import VaultLinker
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure under tmp_path for testing."""
    dirs = [
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "01-Fragments/Unsorted",
        "02-Threads/Active",
        "03-Eddies",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def linker(vault_path: Path) -> VaultLinker:
    """Return a VaultLinker configured for the test vault."""
    return VaultLinker(vault_path=vault_path)


@pytest.fixture()
def writer(vault_path: Path) -> VaultWriter:
    """Return a VaultWriter configured for the test vault."""
    return VaultWriter(vault_path=vault_path)


@pytest.fixture()
def sample_fragment() -> Fragment:
    """Return a sample Fragment for testing."""
    return Fragment(
        id="frag-test0001",
        title="Test Conversation Fragment",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=datetime(2025, 1, 15, 10, 30, 0),
    )


@pytest.fixture()
def written_fragment(
    writer: VaultWriter,
    sample_fragment: Fragment,
) -> Path:
    """Write a sample fragment to the vault and return its path."""
    return writer.write_fragment(sample_fragment)


# ---- VaultLinker Init ----


class TestVaultLinkerInit:
    """Tests for VaultLinker initialization."""

    def test_init_stores_vault_path(self, vault_path: Path) -> None:
        """VaultLinker stores the vault path."""
        vl = VaultLinker(vault_path=vault_path)
        assert vl.vault_path == vault_path

    def test_init_nonexistent_path_raises(self, tmp_path: Path) -> None:
        """VaultLinker raises FileNotFoundError for nonexistent path."""
        bad = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError, match="Vault path does not exist"):
            VaultLinker(vault_path=bad)

    def test_init_missing_fragments_dir_raises(self, tmp_path: Path) -> None:
        """VaultLinker raises FileNotFoundError when 01-Fragments/ missing."""
        (tmp_path / "00-Creek-Meta").mkdir()
        with pytest.raises(FileNotFoundError, match="01-Fragments"):
            VaultLinker(vault_path=tmp_path)


# ---- add_resonances ----


class TestAddResonances:
    """Tests for adding resonance links to fragment files."""

    def test_adds_resonances_section_to_body(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Resonance links appear as a ## Resonances section in body."""
        linker.add_resonances(
            written_fragment,
            ["[[Related Fragment A]]", "[[Related Fragment B]]"],
        )
        content = written_fragment.read_text(encoding="utf-8")
        assert "## Resonances" in content
        assert "- [[Related Fragment A]]" in content
        assert "- [[Related Fragment B]]" in content

    def test_adds_resonances_to_frontmatter(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Resonance links also appear in frontmatter resonances list."""
        linker.add_resonances(
            written_fragment,
            ["[[Related Fragment A]]"],
        )
        post = frontmatter.load(str(written_fragment))
        assert "resonances" in post.metadata
        assert "[[Related Fragment A]]" in post.metadata["resonances"]

    def test_idempotent_no_duplicate_resonances(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Running add_resonances twice does not duplicate links."""
        links = ["[[Related Fragment A]]"]
        linker.add_resonances(written_fragment, links)
        linker.add_resonances(written_fragment, links)
        post = frontmatter.load(str(written_fragment))
        assert post.metadata["resonances"].count("[[Related Fragment A]]") == 1
        body = post.content
        assert body.count("[[Related Fragment A]]") == 1

    def test_appends_new_resonances_to_existing(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """New resonances are appended to existing ones."""
        linker.add_resonances(written_fragment, ["[[Fragment A]]"])
        linker.add_resonances(written_fragment, ["[[Fragment B]]"])
        post = frontmatter.load(str(written_fragment))
        assert "[[Fragment A]]" in post.metadata["resonances"]
        assert "[[Fragment B]]" in post.metadata["resonances"]

    def test_empty_resonances_no_section(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Adding empty resonances list does not create a section."""
        linker.add_resonances(written_fragment, [])
        content = written_fragment.read_text(encoding="utf-8")
        assert "## Resonances" not in content

    def test_nonexistent_file_raises(
        self,
        linker: VaultLinker,
        vault_path: Path,
    ) -> None:
        """add_resonances raises FileNotFoundError for missing file."""
        fake = vault_path / "01-Fragments" / "nonexistent.md"
        with pytest.raises(FileNotFoundError):
            linker.add_resonances(fake, ["[[Link]]"])


# ---- update_threads ----


class TestUpdateThreads:
    """Tests for updating thread links in frontmatter."""

    def test_updates_threads_in_frontmatter(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Thread links appear in frontmatter threads list."""
        linker.update_threads(
            written_fragment,
            ["[[Thread Alpha]]", "[[Thread Beta]]"],
        )
        post = frontmatter.load(str(written_fragment))
        assert "[[Thread Alpha]]" in post.metadata["threads"]
        assert "[[Thread Beta]]" in post.metadata["threads"]

    def test_idempotent_no_duplicate_threads(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Running update_threads twice does not duplicate links."""
        links = ["[[Thread Alpha]]"]
        linker.update_threads(written_fragment, links)
        linker.update_threads(written_fragment, links)
        post = frontmatter.load(str(written_fragment))
        assert post.metadata["threads"].count("[[Thread Alpha]]") == 1

    def test_appends_new_threads_to_existing(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """New threads are appended to existing thread links."""
        linker.update_threads(written_fragment, ["[[Thread A]]"])
        linker.update_threads(written_fragment, ["[[Thread B]]"])
        post = frontmatter.load(str(written_fragment))
        assert "[[Thread A]]" in post.metadata["threads"]
        assert "[[Thread B]]" in post.metadata["threads"]

    def test_empty_threads_preserves_existing(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Adding empty threads list preserves existing threads."""
        linker.update_threads(written_fragment, ["[[Thread A]]"])
        linker.update_threads(written_fragment, [])
        post = frontmatter.load(str(written_fragment))
        assert "[[Thread A]]" in post.metadata["threads"]


# ---- update_eddies ----


class TestUpdateEddies:
    """Tests for updating eddy links in frontmatter."""

    def test_updates_eddies_in_frontmatter(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Eddy links appear in frontmatter eddies list."""
        linker.update_eddies(
            written_fragment,
            ["[[Eddy Cluster A]]", "[[Eddy Cluster B]]"],
        )
        post = frontmatter.load(str(written_fragment))
        assert "[[Eddy Cluster A]]" in post.metadata["eddies"]
        assert "[[Eddy Cluster B]]" in post.metadata["eddies"]

    def test_idempotent_no_duplicate_eddies(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Running update_eddies twice does not duplicate links."""
        links = ["[[Eddy Cluster A]]"]
        linker.update_eddies(written_fragment, links)
        linker.update_eddies(written_fragment, links)
        post = frontmatter.load(str(written_fragment))
        assert post.metadata["eddies"].count("[[Eddy Cluster A]]") == 1

    def test_appends_new_eddies_to_existing(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """New eddies are appended to existing eddy links."""
        linker.update_eddies(written_fragment, ["[[Eddy A]]"])
        linker.update_eddies(written_fragment, ["[[Eddy B]]"])
        post = frontmatter.load(str(written_fragment))
        assert "[[Eddy A]]" in post.metadata["eddies"]
        assert "[[Eddy B]]" in post.metadata["eddies"]


# ---- link_fragment (batch convenience) ----


class TestLinkFragment:
    """Tests for the combined link_fragment method."""

    def test_link_fragment_updates_all_sections(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """link_fragment updates resonances, threads, and eddies together."""
        linker.link_fragment(
            written_fragment,
            resonances=["[[Res A]]"],
            threads=["[[Thread A]]"],
            eddies=["[[Eddy A]]"],
        )
        post = frontmatter.load(str(written_fragment))
        assert "[[Res A]]" in post.metadata["resonances"]
        assert "[[Thread A]]" in post.metadata["threads"]
        assert "[[Eddy A]]" in post.metadata["eddies"]
        assert "## Resonances" in post.content

    def test_link_fragment_with_empty_lists(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """link_fragment with all empty lists makes no changes."""
        original = written_fragment.read_text(encoding="utf-8")
        linker.link_fragment(written_fragment)
        after = written_fragment.read_text(encoding="utf-8")
        assert original == after


# ---- batch_link ----


class TestBatchLink:
    """Tests for batch processing of multiple fragment files."""

    def test_batch_link_processes_multiple_files(
        self,
        linker: VaultLinker,
        writer: VaultWriter,
    ) -> None:
        """batch_link processes a dict of file paths to link specs."""
        frag_a = Fragment(
            id="frag-batch-a",
            title="Batch A",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        frag_b = Fragment(
            id="frag-batch-b",
            title="Batch B",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        path_a = writer.write_fragment(frag_a)
        path_b = writer.write_fragment(frag_b)

        specs = {
            path_a: {
                "resonances": ["[[Res 1]]"],
                "threads": ["[[Thread 1]]"],
                "eddies": [],
            },
            path_b: {
                "resonances": [],
                "threads": [],
                "eddies": ["[[Eddy 1]]"],
            },
        }
        result = linker.batch_link(specs)
        assert result.processed == 2
        assert result.skipped == 0

        post_a = frontmatter.load(str(path_a))
        assert "[[Res 1]]" in post_a.metadata["resonances"]
        post_b = frontmatter.load(str(path_b))
        assert "[[Eddy 1]]" in post_b.metadata["eddies"]

    def test_batch_link_skips_missing_files(
        self,
        linker: VaultLinker,
        writer: VaultWriter,
        vault_path: Path,
    ) -> None:
        """batch_link skips missing files and reports them."""
        frag = Fragment(
            id="frag-batch-c",
            title="Batch C",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        real_path = writer.write_fragment(frag)
        fake_path = vault_path / "01-Fragments" / "missing.md"

        specs = {
            real_path: {"resonances": ["[[Res]]"], "threads": [], "eddies": []},
            fake_path: {"resonances": ["[[Res]]"], "threads": [], "eddies": []},
        }
        result = linker.batch_link(specs)
        assert result.processed == 1
        assert result.skipped == 1

    def test_batch_link_empty_specs(self, linker: VaultLinker) -> None:
        """batch_link with empty specs returns zero counts."""
        result = linker.batch_link({})
        assert result.processed == 0
        assert result.skipped == 0


# ---- remove_stale_links ----


class TestRemoveStaleLinks:
    """Tests for stale link removal."""

    def test_removes_stale_resonance_from_frontmatter(
        self,
        linker: VaultLinker,
        written_fragment: Path,
        vault_path: Path,
    ) -> None:
        """Stale resonance links are removed from frontmatter."""
        linker.add_resonances(
            written_fragment,
            ["[[Existing Note]]", "[[Gone Note]]"],
        )
        # Create the "Existing Note" file so it's not stale
        (vault_path / "01-Fragments" / "Existing Note.md").write_text(
            "---\ntitle: Existing Note\n---\n",
            encoding="utf-8",
        )
        removed = linker.remove_stale_links(written_fragment)
        post = frontmatter.load(str(written_fragment))
        assert "[[Existing Note]]" in post.metadata["resonances"]
        assert "[[Gone Note]]" not in post.metadata["resonances"]
        assert removed == 1

    def test_removes_stale_resonance_from_body(
        self,
        linker: VaultLinker,
        written_fragment: Path,
        vault_path: Path,
    ) -> None:
        """Stale resonance links are removed from the body section."""
        linker.add_resonances(
            written_fragment,
            ["[[Existing Note]]", "[[Gone Note]]"],
        )
        (vault_path / "01-Fragments" / "Existing Note.md").write_text(
            "---\ntitle: Existing Note\n---\n",
            encoding="utf-8",
        )
        linker.remove_stale_links(written_fragment)
        content = written_fragment.read_text(encoding="utf-8")
        assert "[[Existing Note]]" in content
        assert "[[Gone Note]]" not in content

    def test_no_stale_links_returns_zero(
        self,
        linker: VaultLinker,
        written_fragment: Path,
        vault_path: Path,
    ) -> None:
        """Returns 0 when all links are valid."""
        (vault_path / "01-Fragments" / "Valid Note.md").write_text(
            "---\ntitle: Valid Note\n---\n",
            encoding="utf-8",
        )
        linker.add_resonances(written_fragment, ["[[Valid Note]]"])
        removed = linker.remove_stale_links(written_fragment)
        assert removed == 0

    def test_removes_stale_threads_and_eddies(
        self,
        linker: VaultLinker,
        written_fragment: Path,
        vault_path: Path,
    ) -> None:
        """Stale thread and eddy links are also removed."""
        linker.update_threads(written_fragment, ["[[Thread Gone]]"])
        linker.update_eddies(written_fragment, ["[[Eddy Gone]]"])
        removed = linker.remove_stale_links(written_fragment)
        post = frontmatter.load(str(written_fragment))
        assert "[[Thread Gone]]" not in post.metadata["threads"]
        assert "[[Eddy Gone]]" not in post.metadata["eddies"]
        assert removed == 2

    def test_preserves_non_wikilink_entries(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """Non-wikilink entries in threads/eddies are preserved."""
        linker.update_threads(written_fragment, ["plain-thread-id"])
        linker.remove_stale_links(written_fragment)
        post = frontmatter.load(str(written_fragment))
        assert "plain-thread-id" in post.metadata["threads"]

    def test_removes_resonances_section_when_all_stale(
        self,
        linker: VaultLinker,
        written_fragment: Path,
    ) -> None:
        """When all resonances are stale, the ## Resonances section is removed."""
        linker.add_resonances(written_fragment, ["[[Gone A]]", "[[Gone B]]"])
        linker.remove_stale_links(written_fragment)
        content = written_fragment.read_text(encoding="utf-8")
        assert "## Resonances" not in content
