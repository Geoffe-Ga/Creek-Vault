"""Right-to-be-forgotten must reach the ``07-Voice/`` derived artifacts (#1211).

``creek purge fragment <id>`` deletes the fragment file and scrubs
references to its id, but the voice subsystem persists the fragment's
**body** — not just a reference to it — into three places under
``07-Voice/``:

- ``Register-Samples/<register>/<id>.md``, a byte-for-byte ``copy2`` of
  the fragment file (#879);
- ``<register>-profile.md``, whose ``### Sample Passages`` section is the
  exemplar bodies verbatim;
- ``Lexicon/glossary.md`` and ``Lexicon/Metaphors/<domain>.md``, whose
  context bullets quote the whole surrounding sentence verbatim.

Every assertion here is a byte sweep with :meth:`~pathlib.Path.read_bytes`
over *every* file in the vault, not a ``*.md`` text glob: a text glob
misses non-markdown residue and raises on undecodable bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest
import yaml
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.lexicon import generate_lexicon
from creek.generate.voice import (
    VoiceProfileGenerator,
    generate_register_samples,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)
from creek.purge.engine import PurgeEngine

if TYPE_CHECKING:
    from pathlib import Path

SENTINEL = "ZQXJV-SENTINEL-1211"
"""Byte sentinel unique to the purged fragment's body."""

SURVIVOR_SENTINEL = "WKPLM-SURVIVOR-1211"
"""Byte sentinel unique to the fragment that must survive the purge."""

PURGED_BODY = (
    f"The dharma teaches that {SENTINEL} runs through the current. "
    "The river flows toward the sea and the river flows on regardless."
)

SURVIVOR_BODY = (
    f"With karma in mind {SURVIVOR_SENTINEL} sits in the current where "
    "the river flows again and the river flows still, which is another matter."
)


# ---- Helpers ----


def _fragment(frag_id: str, title: str) -> Fragment:
    """Build a conviction-confidence analytical fragment (a voice exemplar)."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        ingested=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(phase=Phase.RISING, mode=Mode.EXPRESS),
        voice=VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        ),
        privacy_tier=PrivacyTier.PERSONAL,
    )


def _seed_fragment(vault: Path, frag_id: str, title: str, body: str) -> Path:
    """Write a qualifying exemplar fragment into ``01-Fragments/Journal``."""
    fragments_dir = vault / "01-Fragments" / "Journal"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content=body,
        **_fragment(frag_id, title).model_dump(mode="json"),
    )
    target = fragments_dir / f"{frag_id}.md"
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _files_containing(vault: Path, needle: str) -> list[str]:
    """Return every vault-relative path whose *bytes* contain *needle*.

    Deliberately ``read_bytes`` over ``rglob("*")`` rather than
    ``read_text`` over ``rglob("*.md")``: the derived-artifact residue
    this guards against is not confined to markdown, and a text read
    raises on the first undecodable byte instead of reporting a leak.
    """
    probe = needle.encode("utf-8")
    return sorted(
        str(path.relative_to(vault))
        for path in vault.rglob("*")
        if path.is_file() and probe in path.read_bytes()
    )


def _build_voice_vault(vault: Path) -> None:
    """Seed two exemplars and generate every ``07-Voice/`` derived artifact."""
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text(
        yaml.safe_dump({"vault_name": "test"}),
        encoding="utf-8",
    )
    _seed_fragment(vault, "frag-a", "Fragment A", PURGED_BODY)
    _seed_fragment(vault, "frag-b", "Fragment B", SURVIVOR_BODY)
    generate_register_samples(vault)
    generator = VoiceProfileGenerator()
    generator.generate_all_profiles(vault)
    generator.generate_rhetorical_patterns(vault)
    generate_lexicon(vault)


@pytest.fixture
def voice_vault(tmp_path: Path) -> Path:
    """A vault with two exemplars and a fully generated ``07-Voice/`` tree."""
    _build_voice_vault(tmp_path)
    return tmp_path


# ---- The leak ----


def test_sentinel_is_present_in_voice_artifacts_before_the_purge(
    voice_vault: Path,
) -> None:
    """Guard the guard: the fixture really does derive the body into 07-Voice.

    Without this, a green erasure assertion could mean nothing more than
    "the voice report never ran".
    """
    leaked = _files_containing(voice_vault, SENTINEL)

    assert "01-Fragments/Journal/frag-a.md" in leaked
    assert "07-Voice/analytical-profile.md" in leaked
    assert "07-Voice/Register-Samples/analytical/frag-a.md" in leaked
    assert "07-Voice/Lexicon/glossary.md" in leaked
    assert "07-Voice/Lexicon/Metaphors/water.md" in leaked


def test_purge_fragment_erases_the_body_from_every_voice_artifact(
    voice_vault: Path,
) -> None:
    """After the purge no file anywhere in the vault carries the sentinel."""
    PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert _files_containing(voice_vault, SENTINEL) == []


def test_purge_fragment_counts_the_voice_artifacts_it_removed(
    voice_vault: Path,
) -> None:
    """The audit-facing count names the four derived files actually removed."""
    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert result.voice_artifacts_removed == 4


def test_purge_fragment_spares_the_surviving_fragments_artifacts(
    voice_vault: Path,
) -> None:
    """Only the purged fragment's derived content goes; frag-b's copy stays."""
    PurgeEngine(voice_vault).purge_fragment("frag-a")

    survivor = (
        voice_vault / "07-Voice" / "Register-Samples" / "analytical" / "frag-b.md"
    )
    assert survivor.is_file()
    assert SURVIVOR_SENTINEL in survivor.read_text(encoding="utf-8")


def test_purge_source_sweeps_voice_artifacts_too(voice_vault: Path) -> None:
    """The multi-fragment path (`_purge_single`) gets the same sweep."""
    PurgeEngine(voice_vault).purge_source("journal")

    assert _files_containing(voice_vault, SENTINEL) == []
    assert _files_containing(voice_vault, SURVIVOR_SENTINEL) == []


def test_dry_run_previews_the_sweep_without_deleting(voice_vault: Path) -> None:
    """A dry run reports exactly what the real run would remove, and removes none."""
    result = PurgeEngine(voice_vault, dry_run=True).purge_fragment("frag-a")

    assert result.voice_artifacts_removed == 4
    assert "07-Voice/analytical-profile.md" in _files_containing(voice_vault, SENTINEL)


def test_audit_entry_records_the_voice_artifact_count(voice_vault: Path) -> None:
    """The outcome entry carries the count, so the log is not a false clean bill."""
    engine = PurgeEngine(voice_vault)
    engine.purge_fragment("frag-a")

    outcome = [e for e in engine.audit_log.read() if e.phase == "outcome"][-1]
    assert outcome.voice_artifacts_removed == 4


# ---- Undecodable bodies (#910 meets #1211) ----


def _corrupt_body_bytes(vault: Path, frag_id: str) -> None:
    """Make a seeded fragment's *body* invalid UTF-8, frontmatter intact.

    The bytes go at the end of the file, past the closing ``---``, so the
    YAML block still parses and the fragment still matches the purge
    criteria — the file is undecodable only where the voice sweep needs
    to read it.
    """
    frag_file = vault / "01-Fragments" / "Journal" / f"{frag_id}.md"
    frag_file.write_bytes(frag_file.read_bytes() + b"\xff\xfe")


def test_an_undecodable_body_names_the_fragment_it_could_not_sweep(
    voice_vault: Path,
) -> None:
    """A body the sweep cannot decode is reported, never silently skipped.

    The loader that feeds every purge decision hands back a *lossy* body
    for a non-UTF-8 file (U+FFFD per bad byte). Matching that against a
    profile that quotes the real bytes finds nothing — so without this
    report an incomplete erasure would be indistinguishable from a
    complete one.
    """
    _corrupt_body_bytes(voice_vault, "frag-a")

    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert result.voice_body_undecodable == ["frag-a"]


def test_an_undecodable_body_downgrades_the_audit_outcome_to_partial(
    voice_vault: Path,
) -> None:
    """The compliance log must not certify an erasure that fell short."""
    _corrupt_body_bytes(voice_vault, "frag-a")
    engine = PurgeEngine(voice_vault)

    engine.purge_fragment("frag-a")

    outcome = [e for e in engine.audit_log.read() if e.phase == "outcome"][-1]
    assert outcome.status == "partial"
    assert outcome.failure_reason == "UnicodeDecodeError"


def test_a_decodable_body_still_certifies_the_erasure_as_complete(
    voice_vault: Path,
) -> None:
    """The partial downgrade is scoped to the failure; the normal path is clean."""
    engine = PurgeEngine(voice_vault)

    result = engine.purge_fragment("frag-a")

    assert result.voice_body_undecodable == []
    outcome = [e for e in engine.audit_log.read() if e.phase == "outcome"][-1]
    assert outcome.status == "complete"
    assert outcome.failure_reason is None


def test_the_link_keyed_passes_still_erase_an_undecodable_fragment(
    voice_vault: Path,
) -> None:
    """Neither id-keyed pass reads the body, so neither may degrade with it.

    The ``Register-Samples`` copy is keyed on the filename stem and the
    lexicon notes on a ``[[<id>]]`` wikilink. All three go. Only the
    content-keyed profile — the one pass with no recorded link to key on
    — is left behind, and that shortfall is the one the result reports.
    """
    _corrupt_body_bytes(voice_vault, "frag-a")

    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    samples = voice_vault / "07-Voice" / "Register-Samples" / "analytical"
    assert not (samples / "frag-a.md").exists()
    assert not (voice_vault / "07-Voice" / "Lexicon" / "glossary.md").exists()
    assert not (
        voice_vault / "07-Voice" / "Lexicon" / "Metaphors" / "water.md"
    ).exists()
    assert result.voice_artifacts_removed == 3
    # The single documented survivor, reported rather than concealed.
    assert _files_containing(voice_vault, SENTINEL) == [
        "07-Voice/analytical-profile.md",
    ]


def test_the_undecodable_warning_names_the_id_and_nothing_else(
    voice_vault: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator gets the id; the log never gets the body or the path.

    A purge log outlives the request that produced it, so quoting the
    content being erased — or a path pointing at it — would defeat the
    erasure it is reporting on.
    """
    _corrupt_body_bytes(voice_vault, "frag-a")

    with caplog.at_level("WARNING", logger="creek.purge.engine"):
        PurgeEngine(voice_vault).purge_fragment("frag-a")

    sweep_warnings = [
        record.getMessage()
        for record in caplog.records
        if "voice-profile" in record.getMessage()
    ]
    assert len(sweep_warnings) == 1
    assert "frag-a" in sweep_warnings[0]
    assert SENTINEL not in sweep_warnings[0]
    assert str(voice_vault) not in sweep_warnings[0]


# ---- Boundaries ----


def test_rhetorical_pattern_notes_are_left_alone(voice_vault: Path) -> None:
    """``Rhetorical-Patterns/`` holds move *counts*, never body text.

    It is named in #1211's acceptance criteria, but nothing attributable
    to a fragment lands there — so deleting it would be destruction
    without a leak to justify it.
    """
    note = voice_vault / "07-Voice" / "Rhetorical-Patterns" / "analytical.md"
    assert SENTINEL not in note.read_text(encoding="utf-8")

    PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert note.is_file()


def test_operator_authored_drafts_are_scrubbed_not_deleted(
    voice_vault: Path,
) -> None:
    """``07-Voice/Drafts/`` is the operator's own writing, not a derived copy.

    A draft's provenance reference is scrubbed by the existing pass; the
    file itself must survive, because deleting the user's essay is not
    what a fragment purge was asked to do.
    """
    drafts = voice_vault / "07-Voice" / "Drafts"
    drafts.mkdir(parents=True)
    draft = drafts / "2026-01-20-essay.md"
    draft.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="An essay I wrote myself.",
                type="draft",
                source_fragments=["frag-a"],
            ),
        ),
        encoding="utf-8",
    )

    PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert draft.is_file()
    assert "frag-a" not in draft.read_text(encoding="utf-8")


def test_sweep_refuses_a_register_dir_symlinked_out_of_the_vault(
    voice_vault: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A symlinked register folder cannot steer the sweep at an outside file.

    Containment is checked on the *resolved* path and before the counter,
    so a dry run never previews a deletion the real run would refuse.
    """
    outside = tmp_path_factory.mktemp("outside")
    decoy = outside / "frag-a.md"
    decoy.write_text("not the vault's to delete", encoding="utf-8")
    samples = voice_vault / "07-Voice" / "Register-Samples"
    (samples / "smuggled").symlink_to(outside, target_is_directory=True)

    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert decoy.is_file()
    # Only the four genuine in-vault artifacts, never the smuggled decoy.
    assert result.voice_artifacts_removed == 4


def test_an_unreadable_derived_artifact_is_skipped_not_fatal(
    voice_vault: Path,
) -> None:
    """A path the sweep cannot read must not abort the erasure of the rest.

    Modelled with a *directory* named ``*.md`` — a shape ``rglob`` yields
    and ``read_bytes`` refuses with ``IsADirectoryError`` — because
    aborting here would leave the purge half-done with the fragment file
    still on disk.
    """
    (voice_vault / "07-Voice" / "Lexicon" / "impostor.md").mkdir()

    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert _files_containing(voice_vault, SENTINEL) == []
    assert result.voice_artifacts_removed == 4


def test_a_symlinked_sample_copy_loses_its_target_not_just_the_link(
    voice_vault: Path,
) -> None:
    """Deleting the alias would leave the body on disk; the sweep resolves first."""
    samples = voice_vault / "07-Voice" / "Register-Samples" / "analytical"
    real = samples / "frag-a.md"
    hidden = voice_vault / "07-Voice" / "Register-Samples" / "kept.md"
    real.rename(hidden)
    real.symlink_to(hidden)

    PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert not hidden.exists()
    assert _files_containing(voice_vault, SENTINEL) == []


def test_a_fragment_id_holding_a_path_separator_names_no_file(
    voice_vault: Path,
) -> None:
    """An id that is a subpath cannot steer the sample pass at a nested file.

    The collector writes ``f"{id}.md"`` as a *filename*, so an id with a
    separator never named a copy it wrote — and joining it would.
    """
    smuggled_id = "sub/frag-a"
    _seed_fragment(voice_vault, "smuggled", "Smuggled", "irrelevant body")
    seeded = voice_vault / "01-Fragments" / "Journal" / "smuggled.md"
    seeded.write_text(
        seeded.read_text(encoding="utf-8").replace(
            "id: smuggled",
            f"id: {smuggled_id}",
        ),
        encoding="utf-8",
    )
    nested = voice_vault / "07-Voice" / "Register-Samples" / "analytical" / "sub"
    nested.mkdir()
    decoy = nested / "frag-a.md"
    decoy.write_text("an operator's own note", encoding="utf-8")

    result = PurgeEngine(voice_vault).purge_fragment(smuggled_id)

    # The fragment really was found and purged — so the zero below is the
    # separator guard refusing, not the whole body short-circuiting.
    assert result.fragments_affected == 1
    assert not seeded.exists()
    assert decoy.is_file()
    assert result.voice_artifacts_removed == 0


def test_a_register_folder_without_this_fragments_copy_is_passed_over(
    voice_vault: Path,
) -> None:
    """The sample pass names one path per register; absent ones are not counted."""
    (voice_vault / "07-Voice" / "Register-Samples" / "confessional").mkdir()

    result = PurgeEngine(voice_vault).purge_fragment("frag-a")

    assert result.voice_artifacts_removed == 4


def test_a_fragment_with_neither_id_nor_body_contributes_nothing(
    voice_vault: Path,
) -> None:
    """Both keys the sweep works from can be missing; neither may crash it.

    A hand-written note with no ``id`` gives the id-keyed passes nothing
    to match, and an empty body gives the profile pass no needle — the
    surrounding multi-fragment purge must still complete and still erase
    the exemplars that *do* have both.
    """
    (voice_vault / "01-Fragments" / "Journal" / "bare.md").write_text(
        "---\ntype: fragment\nsource:\n  platform: journal\n---\n",
        encoding="utf-8",
    )

    result = PurgeEngine(voice_vault).purge_source("journal")

    assert result.fragments_affected == 3
    # frag-a takes four (its copy, the profile, the glossary, the metaphor
    # note); frag-b then takes only its own copy, the three shared notes
    # already being gone; the bare note takes nothing.
    assert result.voice_artifacts_removed == 5
    assert _files_containing(voice_vault, SENTINEL) == []


def test_sweep_is_a_no_op_when_the_vault_has_no_voice_folder(
    tmp_path: Path,
) -> None:
    """A vault that never ran a voice report purges exactly as it did before."""
    meta = tmp_path / "00-Creek-Meta"
    meta.mkdir(parents=True)
    (meta / "creek_config.yaml").write_text(
        yaml.safe_dump({"vault_name": "test"}),
        encoding="utf-8",
    )
    _seed_fragment(tmp_path, "frag-a", "Fragment A", PURGED_BODY)

    result = PurgeEngine(tmp_path).purge_fragment("frag-a")

    assert result.fragments_affected == 1
    assert result.voice_artifacts_removed == 0


# ---- What the operator is told ----


def _purge_via_cli(vault: Path, frag_id: str) -> str:
    """Run ``creek purge fragment`` and return its rendered output."""
    invocation = CliRunner().invoke(
        app,
        ["purge", "fragment", frag_id, "--vault", str(vault), "--yes"],
    )
    assert invocation.exit_code == 0, invocation.output
    return invocation.output


def test_the_cli_tells_the_operator_to_regenerate_the_swept_reports(
    voice_vault: Path,
) -> None:
    """A swept profile or glossary is shared, so its loss outlives one fragment.

    Deleting the note that quoted the purged fragment also drops every
    *other* fragment's retained content from it until the report runs
    again — so the follow-up command is named where the count is, not
    left to be discovered.
    """
    output = _purge_via_cli(voice_vault, "frag-a")

    assert "Voice artifacts removed: 4" in output
    assert "creek report --type voice" in output
    assert "creek report --type lexicon" in output


def test_the_cli_says_so_when_the_voice_sweep_fell_short(
    voice_vault: Path,
) -> None:
    """An incomplete erasure must be visible where the operator is looking."""
    _corrupt_body_bytes(voice_vault, "frag-a")

    output = _purge_via_cli(voice_vault, "frag-a")

    assert "INCOMPLETE" in output
    assert "frag-a" in output
