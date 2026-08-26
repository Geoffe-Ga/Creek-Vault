"""Containment tests for the purge engine's vault walks (#1454).

Every one of these builds a **real symlink on disk**. Mocking the walk
would only prove the guard calls the predicate; what has to be proven is
that a link an operator (or an attacker with vault write access) plants
under ``01-Fragments/`` cannot get its target read, rewritten, or named
in the append-only purge log.

Two shapes of link, because they fail differently:

* a link whose **leaf** is the ``.md`` file, caught by
  :func:`creek._containment.escaping_child`; and
* a link whose leaf is a **directory**, which ``rglob`` scandirs happily
  and which therefore needs the walk itself not to descend.

The final test is the counterweight: an alias that stays inside the vault
is still purged, so the guard cannot quietly become "drop every symlink".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.purge import PurgeEngine, engine
from creek.purge.engine import VAULT_PURGE_CONFIRMATION

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

OUTSIDE_ID = "frag-outside-the-vault"
"""The id declared by the planted out-of-vault file.

Chosen to be greppable: every assertion here is ultimately "this string
did not reach that surface".
"""


def _make_vault(tmp_path: Path) -> Path:
    """Create the minimal vault tree the purge engine requires.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root path.
    """
    vault = tmp_path / "vault"
    for relpath in (
        "00-Creek-Meta/Processing-Log",
        "01-Fragments/Conversations",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis",
        "09-Reference",
    ):
        (vault / relpath).mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# minimal marker for GAP-003\n",
        encoding="utf-8",
    )
    return vault


def _fragment_text(frag_id: str, title: str, *, platform: str = "claude") -> str:
    """Render a house-schema fragment as markdown text.

    Args:
        frag_id: Value for the ``id`` frontmatter field.
        title: Value for the ``title`` field, also the usual filename stem.
        platform: Value for ``source.platform``.

    Returns:
        The full markdown document, frontmatter included.
    """
    created = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata: dict[str, object] = {
        "id": frag_id,
        "title": title,
        "type": "fragment",
        "source": {"platform": platform, "original_file": f"{frag_id}.json"},
        "threads": [],
        "eddies": [],
        "frequency": {"primary": "F3", "secondary": []},
        "wavelength": {"phase": "rising", "confidence": 0.5},
        "voice": {"register": "reflective"},
        "privacy_tier": "open",
        "created": created,
        "ingested": created,
    }
    return frontmatter.dumps(frontmatter.Post(content="Body.", **metadata))


def _write_fragment(vault: Path, frag_id: str, title: str) -> Path:
    """Write an ordinary in-vault fragment.

    Args:
        vault: Vault root.
        frag_id: Fragment id.
        title: Fragment title and filename stem.

    Returns:
        Path to the written fragment.
    """
    target = vault / "01-Fragments" / "Conversations" / f"{title}.md"
    target.write_text(_fragment_text(frag_id, title), encoding="utf-8")
    return target


def _plant_outside_fragment(tmp_path: Path) -> Path:
    """Write a fragment-shaped file OUTSIDE any vault.

    Args:
        tmp_path: Pytest temporary directory (the vault is a child of it,
            so this sits beside the vault, never inside it).

    Returns:
        Path to the planted file.
    """
    outside = tmp_path / "outside_the_vault"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "private-notes.md"
    target.write_text(_fragment_text(OUTSIDE_ID, "Private Notes"), encoding="utf-8")
    return target


def _purge_log_text(vault: Path) -> str:
    """Return the raw bytes of the append-only purge log, decoded.

    Read as raw text rather than through :class:`PurgeAuditLog` because
    the assertion is about what was *written* — an id must not be in the
    file at all, however the reader would present it.

    Args:
        vault: Vault root.

    Returns:
        The log's contents, or ``""`` when it does not exist.
    """
    log = vault / "00-Creek-Meta" / "audit" / "purge.jsonl"
    if not log.exists():
        return ""
    return log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A symlinked .md leaf under 01-Fragments
# ---------------------------------------------------------------------------


def test_purge_classifications_never_rewrites_a_fragment_linked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """A link's target keeps its own frontmatter, byte for byte.

    ``purge_classifications`` is the write-through case: it loads each
    file the census yields and calls ``write_text`` on it, which FOLLOWS
    the symlink. Without the guard, one planted link has an out-of-vault
    document's ``frequency`` / ``wavelength`` / ``voice`` blocks stamped
    with Creek's unclassified defaults.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-inside", "Inside")
    outside = _plant_outside_fragment(tmp_path)
    before = outside.read_bytes()
    (vault / "01-Fragments" / "Conversations" / "alias.md").symlink_to(outside)

    result = PurgeEngine(vault).purge_classifications()

    assert outside.read_bytes() == before, "the link's target was rewritten"
    assert OUTSIDE_ID not in result.affected_fragment_ids
    assert result.classifications_reset == 1


def test_a_scoped_purge_never_copies_an_out_of_vault_id_into_the_purge_log(
    tmp_path: Path,
) -> None:
    """The permanent log names only ids the vault itself declared.

    ``purge.jsonl`` is append-only and survives every later purge, so an
    attacker-chosen string reaching it is durable. The id is also the
    only field the planted file gets to choose, which is what makes this
    the exfiltration end of the gap rather than the deletion end.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-inside", "Inside")
    outside = _plant_outside_fragment(tmp_path)
    (vault / "01-Fragments" / "Conversations" / "alias.md").symlink_to(outside)

    result = PurgeEngine(vault).purge_source("claude")

    assert OUTSIDE_ID not in result.affected_fragment_ids
    assert OUTSIDE_ID not in _purge_log_text(vault)
    assert result.fragments_affected == 1
    assert outside.exists(), "the link's target must survive"


def test_a_fragment_lookup_by_id_never_matches_through_an_escaping_link(
    tmp_path: Path,
) -> None:
    """``purge fragment <id>`` cannot be aimed at an out-of-vault file.

    ``_find_fragment_by_id`` iterates the same census, so naming the
    planted file's own id must find nothing rather than resolve to it.
    """
    vault = _make_vault(tmp_path)
    outside = _plant_outside_fragment(tmp_path)
    (vault / "01-Fragments" / "Conversations" / "alias.md").symlink_to(outside)

    result = PurgeEngine(vault).purge_fragment(OUTSIDE_ID)

    assert result.fragments_affected == 0
    assert result.affected_fragment_ids == []
    assert outside.exists()


def test_the_refusal_never_names_the_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log line names the link as walked, never where it points.

    #1087's no-oracle invariant: a guard that reports the resolved victim
    hands back exactly the path it refused to read.
    """
    vault = _make_vault(tmp_path)
    outside = _plant_outside_fragment(tmp_path)
    link = vault / "01-Fragments" / "Conversations" / "alias.md"
    link.symlink_to(outside)

    with caplog.at_level("WARNING"):
        PurgeEngine(vault).purge_source("claude")

    assert str(link) in caplog.text
    assert str(outside) not in caplog.text


# ---------------------------------------------------------------------------
# A symlinked DIRECTORY under 01-Fragments
# ---------------------------------------------------------------------------


def test_the_fragment_census_never_walks_through_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """``rglob`` scandirs a symlinked directory; the census must not.

    #1340 already stopped the *deletion record* walking through such a
    link (``_regular_files_under``), but the census that reads every
    fragment's frontmatter kept doing it — so the planted file's id still
    reached ``affected_fragment_ids`` and the log.

    INTERPRETER-DEPENDENT before the fix, which is half the reason for it:
    ``rglob`` recurses through a directory link on 3.11/3.12 and, since
    gh-77609, does not on 3.13. This test is therefore red on two of the
    three supported versions against the unfixed engine and green on the
    third; ``os.walk(followlinks=False)`` makes the answer the same on all
    three.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-inside", "Inside")
    outside = _plant_outside_fragment(tmp_path)
    (vault / "01-Fragments" / "ext").symlink_to(
        outside.parent,
        target_is_directory=True,
    )

    result = PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert OUTSIDE_ID not in result.affected_fragment_ids
    assert OUTSIDE_ID not in _purge_log_text(vault)
    assert outside.exists()


# ---------------------------------------------------------------------------
# The vault-wide walk that rewrites surviving notes
# ---------------------------------------------------------------------------


def test_the_reference_scrub_never_rewrites_a_note_linked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """The second walk writes too, so it needs the same guard.

    ``_list_vault_md_files`` feeds the wiki-link and provenance scrub,
    which rewrites every file it matches. A link under ``09-Reference/``
    therefore gets an out-of-vault document edited in place — the same
    defect as the census, one walk over.
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-a", "Secret Title")
    outside_dir = tmp_path / "outside_the_vault"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_note = outside_dir / "someone-elses.md"
    outside_note.write_text("link: [[Secret Title]]\nkept\n", encoding="utf-8")
    before = outside_note.read_bytes()
    (vault / "09-Reference" / "alias.md").symlink_to(outside_note)

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert outside_note.read_bytes() == before, "an outside note was rewritten"
    assert result.wikilinks_removed == 0


# ---------------------------------------------------------------------------
# The counterweight: the guard is not "drop every symlink"
# ---------------------------------------------------------------------------


def test_an_alias_that_stays_inside_the_vault_is_still_purged(
    tmp_path: Path,
) -> None:
    """Containment is about the target escaping, not the link existing.

    Without this the guard could regress into refusing every symlink and
    every test above would still pass.
    """
    vault = _make_vault(tmp_path)
    real = _write_fragment(vault, "frag-inside", "Inside")
    alias = vault / "01-Fragments" / "Conversations" / "alias.md"
    alias.symlink_to(real)

    result = PurgeEngine(vault).purge_classifications()

    # Both the real file and its in-vault alias are yielded by the walk;
    # the second load sees defaults already applied and resets nothing.
    assert result.classifications_reset == 1
    assert "frag-inside" in result.affected_fragment_ids


# ---------------------------------------------------------------------------
# The third walk: the thread/eddy count update, which also writes
# ---------------------------------------------------------------------------

OUTSIDE_THREAD_ID = "thread-outside-the-vault"
"""Id declared by the planted out-of-vault thread/eddy note.

The fragment under purge declares it in ``threads``/``eddies``, so the
count update matches on it and — before the guard — wrote through the
link to the file behind it.
"""


def _thread_text(page_id: str, count: int) -> str:
    """Render a thread/eddy page carrying a spliceable ``fragment_count``.

    A plain integer is deliberate: it is the shape
    :func:`creek.purge.engine._splice_fragment_count` accepts, so the
    write reached by a match is the byte splice ending in
    ``write_bytes`` rather than the PyYAML fallback.

    Args:
        page_id: Value for the ``id`` frontmatter field.
        count: Value for ``fragment_count``.

    Returns:
        The full markdown document, frontmatter included.
    """
    return (
        f"---\nid: {page_id}\ntype: thread\nfragment_count: {count}\n---\n"
        "Narrative body.\n"
    )


def _write_linked_fragment(vault: Path, frag_id: str, title: str) -> Path:
    """Write a fragment that declares one thread and one eddy.

    Args:
        vault: Vault root.
        frag_id: Fragment id.
        title: Fragment title and filename stem.

    Returns:
        Path to the written fragment.
    """
    target = vault / "01-Fragments" / "Conversations" / f"{title}.md"
    post = frontmatter.loads(_fragment_text(frag_id, title))
    post["threads"] = [OUTSIDE_THREAD_ID]
    post["eddies"] = [OUTSIDE_THREAD_ID]
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _plant_outside_thread(tmp_path: Path) -> Path:
    """Write a thread-shaped page OUTSIDE any vault.

    Args:
        tmp_path: Pytest temporary directory; the vault is a sibling.

    Returns:
        Path to the planted file.
    """
    outside = tmp_path / "outside_the_vault"
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "someone-elses-thread.md"
    target.write_text(_thread_text(OUTSIDE_THREAD_ID, 5), encoding="utf-8")
    return target


def test_the_count_update_never_rewrites_a_thread_linked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """``_decrement_counts`` writes, so it needs the same guard.

    The third vault walk, and the worst of the three: it matched on
    frontmatter ``id`` and then went to ``write_bytes``, which follows a
    symlink and writes the TARGET. A planted ``02-Threads/alias.md``
    whose id a purged fragment declares therefore got an arbitrary
    out-of-vault file overwritten — a write outside the vault, which
    #1454 wrongly reasoned purge could not perform.

    ``read_bytes()``, never ``exists()``: a followed write leaves the
    file present and changed.
    """
    vault = _make_vault(tmp_path)
    _write_linked_fragment(vault, "frag-a", "Inside")
    outside_thread = _plant_outside_thread(tmp_path)
    before = outside_thread.read_bytes()
    (vault / "02-Threads" / "Active" / "alias.md").symlink_to(outside_thread)

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert outside_thread.read_bytes() == before, "an outside thread was rewritten"
    assert result.threads_updated == 0


def test_the_count_update_never_rewrites_an_eddy_linked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """The eddy arm is a second call with a second folder, so it is proven too.

    ``_decrement_counts`` is invoked once for ``02-Threads`` and once for
    ``03-Eddies``; a guard applied to only one of them would leave the
    other writing through a link.
    """
    vault = _make_vault(tmp_path)
    _write_linked_fragment(vault, "frag-a", "Inside")
    outside_eddy = _plant_outside_thread(tmp_path)
    before = outside_eddy.read_bytes()
    (vault / "03-Eddies" / "alias.md").symlink_to(outside_eddy)

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert outside_eddy.read_bytes() == before, "an outside eddy was rewritten"
    assert result.eddies_updated == 0


def test_the_count_update_never_walks_through_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """A directory link under ``02-Threads`` must not be descended either.

    Interpreter-dependent before the fix, exactly as the census was:
    ``rglob`` scandirs a directory link on 3.11/3.12 and, since
    gh-77609, does not on 3.13.
    """
    vault = _make_vault(tmp_path)
    _write_linked_fragment(vault, "frag-a", "Inside")
    outside_thread = _plant_outside_thread(tmp_path)
    before = outside_thread.read_bytes()
    (vault / "02-Threads" / "ext").symlink_to(
        outside_thread.parent,
        target_is_directory=True,
    )

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert outside_thread.read_bytes() == before, "an outside thread was rewritten"
    assert result.threads_updated == 0


def test_the_count_update_refusal_never_names_the_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refusal names the link as walked, never what it points at (#1087).

    A warning that printed the resolved path would turn a purge run into
    an oracle for arbitrary filesystem paths, which is the whole reason
    the containment helper logs the walked name.
    """
    vault = _make_vault(tmp_path)
    _write_linked_fragment(vault, "frag-a", "Inside")
    outside_thread = _plant_outside_thread(tmp_path)
    link = vault / "02-Threads" / "Active" / "alias.md"
    link.symlink_to(outside_thread)

    with caplog.at_level("WARNING"):
        PurgeEngine(vault).purge_fragment("frag-a")

    assert str(link) in caplog.text
    assert str(outside_thread) not in caplog.text
    assert outside_thread.name not in caplog.text


def test_a_thread_alias_that_stays_inside_the_vault_is_still_decremented(
    tmp_path: Path,
) -> None:
    """The counterweight: the guard is containment, not "refuse every link".

    Without this the count update could regress into skipping every
    symlink — or into skipping every thread — and the three tests above
    would all still pass.
    """
    vault = _make_vault(tmp_path)
    _write_linked_fragment(vault, "frag-a", "Inside")
    real = vault / "02-Threads" / "Active" / "thread.md"
    real.write_text(_thread_text(OUTSIDE_THREAD_ID, 5), encoding="utf-8")

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert result.threads_updated == 1
    assert "fragment_count: 4" in real.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The voice sweeps: no write escapes, but the MATCH used to read outside bytes
# ---------------------------------------------------------------------------


def _spy_on_match_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every path the voice sweeps read for a substring match.

    The guard being proven here is "the read never happens", and a read
    leaves no trace on disk — the bytes are unchanged either way, and no
    deletion follows because ``_contained_voice_artifact`` already
    refuses one. So the observation point has to be the read itself.
    The symlink under test is still a real one on disk; only the byte
    reader is instrumented, and it still does its real work.

    Args:
        monkeypatch: Pytest's patching fixture.

    Returns:
        A list the sweeps append to, in call order.
    """
    seen: list[Path] = []
    real = engine._read_bytes_for_match

    def _spy(path: Path) -> bytes:
        """Record *path*, then read it exactly as the real reader would.

        Args:
            path: The file a voice sweep is about to match against.

        Returns:
            Whatever the real reader returns.
        """
        seen.append(path)
        return real(path)

    monkeypatch.setattr(engine, "_read_bytes_for_match", _spy)
    return seen


def test_the_lexicon_sweep_never_reads_a_note_linked_out_of_the_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The id-keyed voice sweep matched on bytes it should never have read.

    ``_contained_voice_artifact`` already stopped the *deletion* leaving
    ``07-Voice``, so nothing outside was ever unlinked — but the match
    ran first, so a planted ``07-Voice/Lexicon/alias.md`` had the bytes
    of whatever it pointed at pulled into memory and substring-searched.
    The walk now refuses the leaf before the read.
    """
    vault = _make_vault(tmp_path)
    (vault / "07-Voice" / "Lexicon").mkdir(parents=True, exist_ok=True)
    _write_fragment(vault, "frag-a", "Inside")
    outside_dir = tmp_path / "outside_the_vault"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_note = outside_dir / "someone-elses-lexicon.md"
    outside_note.write_text("- [[frag-a]] - a sentence\n", encoding="utf-8")
    before = outside_note.read_bytes()
    link = vault / "07-Voice" / "Lexicon" / "alias.md"
    link.symlink_to(outside_note)
    seen = _spy_on_match_reads(monkeypatch)

    with caplog.at_level("WARNING"):
        result = PurgeEngine(vault).purge_fragment("frag-a")

    assert link not in seen, "an out-of-vault note was read for a match"
    assert outside_note.read_bytes() == before
    assert result.voice_artifacts_removed == 0
    assert str(outside_note) not in caplog.text


def test_an_in_vault_lexicon_note_quoting_the_id_is_still_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterweight for the lexicon guard: containment, not link-phobia.

    Without it the swapped walk could regress into yielding nothing and
    the test above would still pass.
    """
    vault = _make_vault(tmp_path)
    lexicon = vault / "07-Voice" / "Lexicon"
    lexicon.mkdir(parents=True, exist_ok=True)
    _write_fragment(vault, "frag-a", "Inside")
    note = lexicon / "glossary.md"
    note.write_text("- [[frag-a]] - a sentence\n", encoding="utf-8")
    seen = _spy_on_match_reads(monkeypatch)

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert note in seen
    assert result.voice_artifacts_removed == 1
    assert not note.exists()


def test_the_profile_sweep_never_reads_a_profile_linked_out_of_the_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The content-keyed sibling keeps its non-recursive glob and the guard.

    ``_voice_profiles_quoting`` cannot use the walk primitive without
    widening what it claims, so it applies the same per-leaf verdict.
    """
    vault = _make_vault(tmp_path)
    (vault / "07-Voice").mkdir(parents=True, exist_ok=True)
    _write_fragment(vault, "frag-a", "Inside")
    outside_dir = tmp_path / "outside_the_vault"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_profile = outside_dir / "someone-elses.md"
    outside_profile.write_text("Body.\n", encoding="utf-8")
    before = outside_profile.read_bytes()
    link = vault / "07-Voice" / "reflective-profile.md"
    link.symlink_to(outside_profile)
    seen = _spy_on_match_reads(monkeypatch)

    with caplog.at_level("WARNING"):
        result = PurgeEngine(vault).purge_fragment("frag-a")

    assert link not in seen, "an out-of-vault profile was read for a match"
    assert outside_profile.read_bytes() == before
    assert result.voice_artifacts_removed == 0
    assert str(outside_profile) not in caplog.text


def test_an_in_vault_profile_quoting_the_body_is_still_removed(
    tmp_path: Path,
) -> None:
    """Counterweight for the profile guard: containment, not link-phobia.

    Without it the per-leaf check could regress into skipping every
    profile and the test above would still pass.
    """
    vault = _make_vault(tmp_path)
    (vault / "07-Voice").mkdir(parents=True, exist_ok=True)
    _write_fragment(vault, "frag-a", "Inside")
    profile = vault / "07-Voice" / "reflective-profile.md"
    profile.write_text("Body.\n", encoding="utf-8")

    result = PurgeEngine(vault).purge_fragment("frag-a")

    assert result.voice_artifacts_removed == 1
    assert not profile.exists()


# ---------------------------------------------------------------------------
# Observability: an escaping DIRECTORY link is refused out loud
# ---------------------------------------------------------------------------


def test_a_directory_link_out_of_the_vault_is_logged_not_silently_dropped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``followlinks=False`` refuses silently; the walk now says so.

    Nothing about what the walk *returns* changes — ``os.walk`` never
    descended the link — but a vault holding a directory link out of it
    left no trace in the run's log, unlike the equivalent walk in
    :func:`creek._containment.inspect_tree`. The message still names the
    link as walked, never its target (#1087).
    """
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "frag-a", "Inside")
    outside = _plant_outside_fragment(tmp_path)
    link = vault / "01-Fragments" / "ext"
    link.symlink_to(outside.parent, target_is_directory=True)

    with caplog.at_level("WARNING"):
        PurgeEngine(vault, dry_run=True).purge_vault(VAULT_PURGE_CONFIRMATION)

    assert "Not descending a vault directory" in caplog.text
    assert str(link) in caplog.text
    assert str(outside.parent) not in caplog.text
