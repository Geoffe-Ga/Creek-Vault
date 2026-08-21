"""Compiled-layer target resolution: normalisation and alias awareness (#881).

Three distinct defects sit behind #881's "false compile-gap reports", and
only the first is named in the issue:

1. **The lookup key is a wikilink, not an id.** ``creek/link/eddies.py:760``
   builds ``wikilink = f"[[{eddy.title}]]"`` and merges that *string* into
   ``frag.eddies``; ``drafts.py:1926`` copies it into ``IdeaSeed.eddies``; and
   ``drafts.py:1265`` passes it straight to ``compiled.eddy(eid)``, a plain
   ``dict.get`` keyed by ``target_id``. ``"[[Messages]]"`` never equals
   ``"eddy-abc123"``, so a **fully compiled vault still logs a ``missing``
   gap for every eddy its fragments name**. Normalisation is mandatory;
   alias-indexing alone does not fix this.
2. **Linker-written pages are invisible.** ``creek/vault/writer.py`` writes
   eddy/thread pages with ``type: eddy`` / ``type: thread`` and
   ``aliases: [title]``, while ``_load_pages`` skips anything that is not
   ``type: compiled_page``.
3. **"missing" and "uncompiled" are conflated.** A page that exists but has
   never been compiled is a real gap and belongs on the backlog — under an
   honest reason, not by being reported as absent.

**The mining-regression trap this module also pins.** The naive reading of
#881 — make ``_load_pages`` accept ``type: eddy`` — breaks
``creek/generate/mining.py:1082-1090``, which switches to
``compiled.fragment_ids_for(page)`` the moment ``.thread()`` returns
something. Linker pages carry no provenance, so that returns ``()`` and
``IdeaSeed.source_fragments`` silently collapses to empty, replacing a
working fallback. ``.thread()`` / ``.eddy()`` must therefore keep returning
**only** provenance-bearing ``type: compiled_page`` objects; existence of a
linker page is reported through a separate predicate.

The "does an unresolvable target still get logged" half of #881 lives in
``tests/test_compile_gap_signal.py`` and is green before and after.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.compile_routing import (
    COMPILE_GAPS_RELPATH,
    compiled_source_ids,
    load_compiled_pages,
)
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.models import Eddy, Frequency, Thread
from tests.factories.compiled import (
    write_compiled_eddy_page,
    write_compiled_thread_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _seed(
    *,
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
) -> IdeaSeed:
    """Return a minimal :class:`IdeaSeed` naming *threads* / *eddies*."""
    return IdeaSeed(
        strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
        title="Naming what orbits",
        source_fragments=("frag-001",),
        threads=threads,
        eddies=eddies,
        frequency_affinity=(Frequency.F1,),
        brief_description="An essay waits here.",
        score=0.8,
    )


def _gap_records(vault: Path) -> list[dict[str, str]]:
    """Return every JSONL record in ``compile-gaps.jsonl`` (empty when absent)."""
    log_path = vault / COMPILE_GAPS_RELPATH
    if not log_path.is_file():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_linker_eddy(
    vault: Path,
    relpath: str,
    *,
    eddy_id: str,
    title: str,
) -> Path:
    """Write the ``type: eddy`` page ``creek link`` produces, aliases and all.

    Mirrors ``creek.vault.writer.write_eddy``: the model dump (whose ``type``
    default is the literal ``"eddy"``) plus
    ``extra_frontmatter={"aliases": [eddy.title]}``.
    """
    eddy = Eddy(id=eddy_id, title=title, description="Messages about messages.")
    metadata = eddy.model_dump(mode="json")
    metadata["aliases"] = [title]
    target = vault / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


def _write_linker_thread(
    vault: Path,
    relpath: str,
    *,
    thread_id: str,
    title: str,
) -> Path:
    """Write the ``type: thread`` page ``creek link`` produces."""
    thread = Thread(id=thread_id, title=title, description="A current.")
    metadata = thread.model_dump(mode="json")
    metadata["aliases"] = [title]
    target = vault / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **metadata)),
        encoding="utf-8",
    )
    return target


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A minimal vault with the compiled directories present."""
    for sub in ("01-Fragments", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """The skill tree ``DraftGenerator`` expects."""
    root = tmp_path / "skills"
    for sub in ("frequencies", "phases", "modes", "registers"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def llm_echo() -> Callable[[str], str]:
    """An LLM stub that echoes the prompt length."""

    def _call(prompt: str) -> str:
        return f"DRAFT({len(prompt)} chars)"

    return _call


class TestTargetNormalisation:
    """Root cause 1: the key arrives as a wikilink and must be normalised."""

    @pytest.mark.parametrize(
        "reference",
        [
            "eddy-abc123",
            "[[eddy-abc123]]",
            "[[eddy-abc123|Water as teacher]]",
            "[[eddy-abc123#Section]]",
            "  eddy-abc123  ",
        ],
        ids=["bare", "wikilink", "piped", "anchored", "padded"],
    )
    def test_a_compiled_page_resolves_through_every_reference_form(
        self,
        vault: Path,
        reference: str,
    ) -> None:
        """A genuinely compiled vault must stop reporting itself uncompiled.

        This is the case that proves the fix repairs a *lookup* bug rather
        than suppressing a report: the page is right there on disk with the
        exact ``target_id`` the reference names.
        """
        write_compiled_eddy_page(
            vault,
            target_id="eddy-abc123",
            title="Water as teacher",
            body="# Water as teacher\nWater is the teacher.\n",
            fragment_ids=("frag-a",),
        )

        page = load_compiled_pages(vault).eddy(reference)

        assert page is not None
        assert page.target_id == "eddy-abc123"

    def test_a_wikilink_reference_logs_no_gap_for_a_compiled_page(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """End-to-end: the draft path stops fabricating a backlog entry."""
        write_compiled_eddy_page(
            vault,
            target_id="eddy-abc123",
            title="Water as teacher",
            body="# Water as teacher\nWater is the teacher.\n",
            fragment_ids=("frag-a",),
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        block = gen.gather_source_material(
            _seed(eddies=("[[eddy-abc123]]",)),
            vault_path=vault,
        )

        assert "Water is the teacher" in block
        assert _gap_records(vault) == []


class TestLinkerWrittenPagesAreKnown:
    """Root cause 2 + 3: an existing-but-uncompiled page is a different gap."""

    def test_an_aliased_linker_eddy_is_not_reported_missing(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """``[[Messages]]`` names a real ``type: eddy`` page, so it is not absent.

        This is the exact shape the demo vault produces: ``creek link`` writes
        ``03-Eddies/2023-03-30-Messages.md`` with ``aliases: [Messages]`` and
        stamps ``"[[Messages]]"`` into every member fragment's ``eddies``.
        """
        _write_linker_eddy(
            vault,
            "03-Eddies/2023-03-30-Messages.md",
            eddy_id="eddy-abc123",
            title="Messages",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        gen.gather_source_material(
            _seed(eddies=("[[Messages]]",)),
            vault_path=vault,
        )

        assert [r["reason"] for r in _gap_records(vault)] != ["missing"]

    def test_an_aliased_linker_eddy_is_recorded_as_uncompiled(
        self,
        vault: Path,
        skills_root: Path,
        llm_echo: Callable[[str], str],
    ) -> None:
        """The signal survives in honest form — the backlog is not deleted.

        The page exists and has never been compiled. That is a real operator
        task; it just was not "missing". A repair that logs nothing here has
        silenced the check, which is the failure mode #881 must not produce.
        """
        _write_linker_eddy(
            vault,
            "03-Eddies/2023-03-30-Messages.md",
            eddy_id="eddy-abc123",
            title="Messages",
        )
        gen = DraftGenerator(llm=llm_echo, skills_root=skills_root)

        gen.gather_source_material(
            _seed(eddies=("[[Messages]]",)),
            vault_path=vault,
        )

        records = _gap_records(vault)
        assert [r["reason"] for r in records] == ["uncompiled"]
        assert records[0]["target_kind"] == "eddy"

    def test_page_exists_reports_a_linker_page_the_store_does_not_hold(
        self,
        vault: Path,
    ) -> None:
        """Existence is a predicate, separate from the compiled-page store."""
        _write_linker_eddy(
            vault,
            "03-Eddies/2023-03-30-Messages.md",
            eddy_id="eddy-abc123",
            title="Messages",
        )

        index = load_compiled_pages(vault)

        assert index.page_exists("eddy", "eddy-abc123") is True
        assert index.page_exists("eddy", "[[Messages]]") is True
        assert index.page_exists("eddy", "eddy-nowhere") is False


class TestOnlyProvenanceBearingPagesAreReturned:
    """The mining-regression guard. Do not weaken these assertions.

    ``mining.py:1082-1090`` reads ``compiled.fragment_ids_for(page)`` the
    moment ``.thread()`` returns non-``None``. A linker page carries no
    provenance, so admitting one into the *store* replaces mining's working
    fragment-membership fallback with an empty tuple and produces sourceless
    idea seeds — silently.
    """

    def test_eddy_returns_none_for_a_linker_page_that_page_exists_reports(
        self,
        vault: Path,
    ) -> None:
        """Known, but not returned: the two answers must diverge here."""
        _write_linker_eddy(
            vault,
            "03-Eddies/2023-03-30-Messages.md",
            eddy_id="eddy-abc123",
            title="Messages",
        )

        index = load_compiled_pages(vault)

        assert index.eddy("eddy-abc123") is None
        assert index.eddy("[[Messages]]") is None
        assert index.page_exists("eddy", "eddy-abc123") is True

    def test_thread_returns_none_for_a_linker_page(self, vault: Path) -> None:
        """Threads take the identical path and carry the identical trap."""
        _write_linker_thread(
            vault,
            "02-Threads/Active/2023-03-30-Grief.md",
            thread_id="thread-grief",
            title="Grief",
        )

        index = load_compiled_pages(vault)

        assert index.thread("thread-grief") is None
        assert index.thread("[[Grief]]") is None
        assert index.page_exists("thread", "thread-grief") is True

    def test_a_compiled_page_still_carries_its_provenance(
        self,
        vault: Path,
    ) -> None:
        """The positive half: a real compiled page still enumerates its sources."""
        write_compiled_thread_page(
            vault,
            target_id="thread-grief",
            title="Grief",
            fragment_ids=("frag-a", "frag-b"),
        )

        index = load_compiled_pages(vault)
        page = index.thread("[[Grief]]")

        assert page is not None
        assert index.fragment_ids_for(page) == ("frag-a", "frag-b")


class TestSourceTierSurveyLoosens:
    """PRIVACY DISCLOSURE: the repair is a tier *loosening*, and is meant to be.

    ``compiled_source_ids`` marks a prompt ``opaque`` whenever a named
    thread/eddy fails to resolve, and every caller fails closed to
    ``INTIMATE`` — which forces the draft onto a local model. Today
    ``[[Water as teacher]]`` never resolves, so **every** draft naming an
    alias-form eddy is opaque and local-only.

    After the fix a genuinely compiled page resolves, its real provenance ids
    enter the survey, and a prompt whose sources are all ``open`` may route to
    a cloud provider where it previously could not. That is correct — the
    survey becomes accurate instead of failing closed on a lookup bug — but it
    is a loosening and must be visible in the diff, not discovered in
    production.

    Measured before the fix, for the record:
    ``compiled_source_ids(index, eddy_ids=("[[Water as teacher]]",))`` over a
    compiled page with provenance ``("frag-open-1", "frag-open-2")`` returned
    ``opaque=True, fragment_ids=()``. Every such prompt was forced local. The
    two tests below pin where that changes and — just as importantly — where
    it does not.
    """

    def test_an_alias_reference_surveys_real_provenance(
        self,
        vault: Path,
    ) -> None:
        """After the fix the survey sees the page's actual source fragments."""
        write_compiled_eddy_page(
            vault,
            target_id="eddy-abc123",
            title="Water as teacher",
            fragment_ids=("frag-open-1", "frag-open-2"),
        )

        index = load_compiled_pages(vault)
        survey = compiled_source_ids(
            index,
            thread_ids=(),
            eddy_ids=("[[Water as teacher]]",),
        )

        assert survey.opaque is False
        assert survey.fragment_ids == ("frag-open-1", "frag-open-2")

    def test_an_uncompiled_linker_page_stays_opaque(self, vault: Path) -> None:
        """Fail-closed is preserved exactly where it is still earned.

        A linker page has no provenance record anywhere, so its prompt text
        remains unaccountable and the survey must keep saying so. This is the
        boundary of the loosening: knowing a page *exists* must not be
        mistaken for being able to enumerate its sources.
        """
        _write_linker_eddy(
            vault,
            "03-Eddies/2023-03-30-Messages.md",
            eddy_id="eddy-abc123",
            title="Messages",
        )

        index = load_compiled_pages(vault)
        survey = compiled_source_ids(
            index,
            thread_ids=(),
            eddy_ids=("[[Messages]]",),
        )

        assert survey.opaque is True

    def test_a_bypassed_index_resolves_nothing(self, vault: Path) -> None:
        """Bypass keeps empty name maps: every lookup is still a miss."""
        write_compiled_eddy_page(
            vault,
            target_id="eddy-abc123",
            title="Water as teacher",
            fragment_ids=("frag-a",),
        )
        from creek.generate.compile_routing import empty_index

        index = empty_index(bypassed=True)

        assert index.eddy("[[Water as teacher]]") is None
        assert index.page_exists("eddy", "eddy-abc123") is False
