"""Paradox notes must not duplicate on a new calendar day (#1320).

``Paradox.detected_date`` is ``field(default_factory=date.today)`` and
``ParadoxDetector._build_paradox`` never passes it, so every run stamps today.
``_filename`` embeds that date, so the note path is stable per paradox *per
day* — running ``creek report --type paradox`` weekly for a year leaves 52
copies of every paradox in ``10-Liminal/Paradoxes/``.

Reproduced on ``main`` for one unchanged fragment pair::

    day 1: wrote 2026-08-10-frag-1-frag-2.md
    day 2: wrote 2026-08-11-frag-1-frag-2.md
    ON DISK: ['2026-08-10-frag-1-frag-2.md', '2026-08-11-frag-1-frag-2.md']

**Clock injection.** There is no freezegun/time_machine in this repo, and —
unlike ``tests/test_ingest_generic_idempotent.py``, which monkeypatches
``creek.ingest.generic.datetime`` — patching ``creek.generate.paradox.date``
does **nothing** here: ``default_factory=date.today`` captured the bound
``date.today`` at class-definition time, so the module attribute is never
consulted. Verified: with that attribute patched to a 1999 fake,
``Paradox().detected_date`` still returned the real today.

So "yesterday's run" is staged the only honest way available — by writing its
note through the same public path (``create_paradox_note``) with an explicit
past ``detected_date``. Those are byte-for-byte the bytes a run on that date
left behind; nothing else in the note derives from the clock.
:data:`_PRIOR_RUN_DATE` is a fixed date in the past rather than
``today - 1 day`` so the test cannot straddle a midnight rollover.

:class:`TestSecondRunSameDay` needs no clock manipulation at all: it pins that
a re-run reports nothing new, which the date-stamped filename cannot express.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import EmbeddingsConfig
from creek.generate.paradox import (
    REFLECTION_PROMPT,
    ParadoxDetector,
    duplicate_paradox_warning,
    generate_paradoxes,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    SourcePlatform,
    VoiceClassification,
)
from creek.vault.reader import iter_vault_fragments
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_PRIOR_RUN_DATE = date(2020, 1, 1)
"""Stands in for "the day the previous run happened".

Fixed rather than relative so no assertion here can straddle midnight.
"""

_PAIR = (("frag-parax-1", Confidence.MUSING), ("frag-parax-2", Confidence.SETTLED))
"""Two fragments on one thread with opposite confidence — fires Rule 2."""


# ---- Fixtures / helpers ----------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    """Scaffold a vault holding exactly one contradictory fragment pair."""
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta", "01-Fragments", "10-Liminal/Paradoxes"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    for fid, confidence in _PAIR:
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid,
                title="career ambitions",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
                voice=VoiceClassification(confidence=confidence),
                threads=["thread-career"],
            ),
            body="the same thread, settled differently",
        )
    return vault


def _notes(vault: Path) -> list[str]:
    """Every paradox note filename currently on disk, sorted."""
    return sorted(p.name for p in (vault / "10-Liminal" / "Paradoxes").glob("*.md"))


def _seed_prior_run(vault: Path, *, on: date) -> list[Path]:
    """Write the notes a ``generate_paradoxes`` run on *on* would have left.

    Uses the production detector and the production write path, so the result
    is byte-identical to that run's output — only ``detected_date`` is pinned.
    """
    fragments = [
        fragment
        for _path, fragment, _body, _raw in iter_vault_fragments(
            vault / "01-Fragments",
        )
    ]
    detector = ParadoxDetector()
    written: list[Path] = []
    for paradox in detector.detect_paradoxes(fragments):
        paradox.detected_date = on
        written.append(detector.create_paradox_note(paradox, vault))
    return written


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault with one contradictory fragment pair and no paradox notes yet."""
    return _vault(tmp_path)


# ---- The headline regression ----------------------------------------------


class TestNewCalendarDay:
    """A later run must not re-record a pair a previous run already recorded."""

    def test_prior_days_note_is_not_duplicated(self, vault: Path) -> None:
        """The #1320 repro: two runs, two dates, one unchanged pair, one note."""
        prior = _seed_prior_run(vault, on=_PRIOR_RUN_DATE)
        assert len(prior) == 1, "fixture must produce exactly one paradox"
        assert _notes(vault) == ["2020-01-01-frag-parax-1-frag-parax-2.md"]

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert written == []
        assert _notes(vault) == ["2020-01-01-frag-parax-1-frag-parax-2.md"]

    def test_the_operators_reflection_survives_the_next_run(
        self,
        vault: Path,
    ) -> None:
        """Skipping, not overwriting — the note exists to be written in.

        This is the assertion that encodes why the issue's remedy (a) — drop
        the date from the filename — was rejected. Under (a) the path becomes
        stable across runs and ``create_paradox_note``'s unconditional
        ``write_text`` clobbers the note every week, destroying whatever the
        operator wrote under :data:`REFLECTION_PROMPT`. Trading 52 harmless
        copies for silent loss of the operator's own prose is the worse bug.
        """
        (prior_note,) = _seed_prior_run(vault, on=_PRIOR_RUN_DATE)
        reflection = "\n## My reflection\n\nBoth were true in different rooms.\n"
        prior_note.write_text(
            prior_note.read_text(encoding="utf-8") + reflection,
            encoding="utf-8",
        )
        before = prior_note.read_bytes()

        generate_paradoxes(vault, EmbeddingsConfig())

        assert prior_note.read_bytes() == before
        assert reflection in prior_note.read_text(encoding="utf-8")

    def test_pair_key_ignores_fragment_order(self, vault: Path) -> None:
        """``[b, a]`` on disk suppresses ``[a, b]`` — the key is the pair set."""
        (prior_note,) = _seed_prior_run(vault, on=_PRIOR_RUN_DATE)
        post = frontmatter.loads(prior_note.read_text(encoding="utf-8"))
        assert post["fragments"] == ["frag-parax-1", "frag-parax-2"]
        post["fragments"] = ["frag-parax-2", "frag-parax-1"]
        prior_note.write_text(frontmatter.dumps(post), encoding="utf-8")

        assert generate_paradoxes(vault, EmbeddingsConfig()) == []
        assert len(_notes(vault)) == 1

    def test_a_genuinely_new_pair_is_still_written(self, vault: Path) -> None:
        """The guard skips recorded pairs only — it must not freeze the folder."""
        _seed_prior_run(vault, on=_PRIOR_RUN_DATE)
        for fid, confidence in (
            ("frag-parax-3", Confidence.MUSING),
            ("frag-parax-4", Confidence.SETTLED),
        ):
            write_fragment_file(
                vault=vault,
                fragment=Fragment(
                    id=fid,
                    title="a second tension",
                    source=FragmentSource(platform=SourcePlatform.JOURNAL),
                    voice=VoiceClassification(confidence=confidence),
                    threads=["thread-other"],
                ),
                body="another thread, settled differently",
            )

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1
        assert "frag-parax-3-frag-parax-4" in written[0].name
        assert len(_notes(vault)) == 2


class TestSecondRunSameDay:
    """No clock manipulation: a re-run must report nothing new."""

    def test_second_run_writes_nothing(self, vault: Path) -> None:
        """Run 2 returns ``[]`` rather than re-reporting a recorded paradox."""
        first = generate_paradoxes(vault, EmbeddingsConfig())
        second = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(first) == 1
        assert second == []
        assert len(_notes(vault)) == 1


# ---- The scan must survive whatever else lives in that folder --------------


class TestReadBackScanRobustness:
    """``10-Liminal/Paradoxes/`` is operator-editable and multi-producer."""

    def test_malformed_yaml_note_does_not_crash_the_scan(self, vault: Path) -> None:
        """A hand-broken note is skipped, not fatal.

        ``frontmatter.loads`` raises ``yaml.ParserError`` here, which is **not**
        a ``ValueError`` — the sibling scan at
        ``synchronicity._existing_synchronicity_pairs`` catches only
        ``(OSError, ValueError)`` and does crash on this input (tracked
        separately). This scan must not.
        """
        broken = vault / "10-Liminal" / "Paradoxes" / "2019-05-05-broken.md"
        broken.write_text(
            "---\nfragments: [a, b\ntype: paradox\n---\nbody\n",
            encoding="utf-8",
        )

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1

    def test_unreadable_entry_is_skipped(self, vault: Path) -> None:
        """A directory matching ``*.md`` raises ``OSError`` and is skipped."""
        (vault / "10-Liminal" / "Paradoxes" / "not-a-file.md").mkdir()

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1

    def test_hand_saved_paradox_note_does_not_suppress_generation(
        self,
        vault: Path,
    ) -> None:
        """``creek save --target paradox`` notes carry no ``fragments:`` key.

        They land in the same folder (``creek/save/router.py:51``) with a
        ``saved_from.contributing_fragments`` provenance block instead, so they
        must never be mistaken for a recorded detector pair.
        """
        saved = vault / "10-Liminal" / "Paradoxes" / "2019-05-05-a-contradiction.md"
        post = frontmatter.Post(
            content="A reflection naming a tension.\n",
            title="A contradiction",
            type="paradox",
            tags=["paradox"],
            privacy_tier="open",
            saved_from={
                "source_kind": "conversation",
                "source_id": "",
                "contributing_fragments": ["frag-parax-1", "frag-parax-2"],
            },
        )
        saved.write_text(frontmatter.dumps(post), encoding="utf-8")

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1

    def test_short_fragments_list_is_skipped(self, vault: Path) -> None:
        """A one-element ``fragments:`` list records no pair."""
        partial = vault / "10-Liminal" / "Paradoxes" / "2019-05-05-partial.md"
        post = frontmatter.Post(
            content="half a record\n",
            type="paradox",
            fragments=["frag-parax-1"],
        )
        partial.write_text(frontmatter.dumps(post), encoding="utf-8")

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1

    def test_missing_folder_is_not_an_error(self, tmp_path: Path) -> None:
        """A vault that has never held a paradox note still generates one."""
        bare = _vault(tmp_path)
        (bare / "10-Liminal" / "Paradoxes").rmdir()

        written = generate_paradoxes(bare, EmbeddingsConfig())

        assert len(written) == 1


# ---- Migration: vaults that already hold duplicates ------------------------


class TestExistingDuplicatesAreReportedNotDeleted:
    """Every pre-#1320 vault already holds duplicates. Name them; touch none."""

    @staticmethod
    def _seed_duplicates(vault: Path, *, days: tuple[int, ...]) -> list[Path]:
        """Stage one note per calendar day for the same unchanged pair."""
        return [
            note
            for day in days
            for note in _seed_prior_run(vault, on=date(2020, 1, day))
        ]

    def test_duplicates_are_reported_and_left_on_disk(self, vault: Path) -> None:
        """The advisory names the count; every file survives the run."""
        seeded = self._seed_duplicates(vault, days=(1, 2, 3))
        assert len(seeded) == 3
        advisories: list[str] = []

        written = generate_paradoxes(
            vault,
            EmbeddingsConfig(),
            on_warning=advisories.append,
        )

        assert written == []
        assert len(_notes(vault)) == 3
        assert all(note.exists() for note in seeded)
        assert len(advisories) == 1
        assert "#1320" in advisories[0]
        # Pin the rendered phrase, not the bare digit: "#1320" already supplies
        # a "3", so `"3" in advisory` would pass for any count at all.
        assert (
            "3 paradox notes recording only 1 distinct fragment pair(s)"
            in (advisories[0])
        )

    def test_advisory_names_the_duplicate_files(self, vault: Path) -> None:
        """An operator cannot act on a count alone — the paths are named."""
        self._seed_duplicates(vault, days=(1, 2))
        advisories: list[str] = []

        generate_paradoxes(vault, EmbeddingsConfig(), on_warning=advisories.append)

        assert "2020-01-01-frag-parax-1-frag-parax-2.md" in advisories[0]
        assert "2020-01-02-frag-parax-1-frag-parax-2.md" in advisories[0]

    def test_advisory_never_proposes_deletion(self, vault: Path) -> None:
        """Vault content is the operator's. The advisory says so, in words."""
        self._seed_duplicates(vault, days=(1, 2))
        advisories: list[str] = []

        generate_paradoxes(vault, EmbeddingsConfig(), on_warning=advisories.append)

        assert "Nothing is deleted automatically" in advisories[0]
        for verb in ("deleting", "removed", "pruned"):
            assert verb not in advisories[0].lower()

    def test_no_advisory_when_the_vault_is_clean(self, vault: Path) -> None:
        """One note per pair is the healthy state — stay quiet."""
        _seed_prior_run(vault, on=_PRIOR_RUN_DATE)
        advisories: list[str] = []

        generate_paradoxes(vault, EmbeddingsConfig(), on_warning=advisories.append)

        assert advisories == []

    def test_the_advisory_is_self_clearing(self, vault: Path) -> None:
        """Once the operator removes the strays, the run goes quiet."""
        seeded = self._seed_duplicates(vault, days=(1, 2, 3))
        for stray in seeded[1:]:
            stray.unlink()
        advisories: list[str] = []

        generate_paradoxes(vault, EmbeddingsConfig(), on_warning=advisories.append)

        assert advisories == []

    def test_advisory_sample_is_capped(self, vault: Path) -> None:
        """A year of weekly runs must not print 52 paths."""
        self._seed_duplicates(vault, days=tuple(range(1, 11)))
        advisories: list[str] = []

        generate_paradoxes(vault, EmbeddingsConfig(), on_warning=advisories.append)

        named = re.findall(r"2020-01-\d\d-frag-parax-1-frag-parax-2\.md", advisories[0])
        assert len(named) < 10
        # The count is still the true total, even though the sample is capped.
        assert (
            "10 paradox notes recording only 1 distinct fragment pair(s)"
            in (advisories[0])
        )

    def test_on_warning_is_optional(self, vault: Path) -> None:
        """Callers that predate the channel (MCP) still run unchanged."""
        self._seed_duplicates(vault, days=(1, 2))

        assert generate_paradoxes(vault, EmbeddingsConfig()) == []
        assert len(_notes(vault)) == 2


class TestDuplicateParadoxWarning:
    """The public pure detector backing the advisory (the #1305 shape)."""

    def test_reports_only_pairs_recorded_more_than_once(self, vault: Path) -> None:
        """A pair recorded once is not a duplicate."""
        _seed_prior_run(vault, on=date(2020, 1, 1))
        _seed_prior_run(vault, on=date(2020, 1, 2))

        advisory = duplicate_paradox_warning(vault)

        assert advisory is not None
        assert "2 paradox notes recording only 1 distinct fragment pair(s)" in advisory

    def test_each_duplicated_pair_is_counted_once(self, vault: Path) -> None:
        """Two duplicated pairs read as two, not four (#1394's M23)."""
        for fid, confidence in (
            ("frag-parax-3", Confidence.MUSING),
            ("frag-parax-4", Confidence.SETTLED),
        ):
            write_fragment_file(
                vault=vault,
                fragment=Fragment(
                    id=fid,
                    title="a second tension",
                    source=FragmentSource(platform=SourcePlatform.JOURNAL),
                    voice=VoiceClassification(confidence=confidence),
                    threads=["thread-other"],
                ),
                body="another thread, settled differently",
            )
        _seed_prior_run(vault, on=date(2020, 1, 1))
        _seed_prior_run(vault, on=date(2020, 1, 2))

        advisory = duplicate_paradox_warning(vault)

        assert advisory is not None
        assert "4 paradox notes recording only 2 distinct fragment pair(s)" in advisory

    def test_clean_vault_reports_nothing(self, vault: Path) -> None:
        """One note per pair is silence (#1394's M24)."""
        _seed_prior_run(vault, on=_PRIOR_RUN_DATE)

        assert duplicate_paradox_warning(vault) is None

    def test_missing_folder_reports_nothing(self, tmp_path: Path) -> None:
        """A vault with no Paradoxes folder is not a crash."""
        assert duplicate_paradox_warning(tmp_path / "nowhere") is None

    def test_advisory_names_no_fragment_prose(self, vault: Path) -> None:
        """Paths only — excerpts and tags can be intimate-derived (#1404)."""
        _seed_prior_run(vault, on=date(2020, 1, 1))
        _seed_prior_run(vault, on=date(2020, 1, 2))

        advisory = duplicate_paradox_warning(vault)

        assert advisory is not None
        assert "career ambitions" not in advisory
        assert REFLECTION_PROMPT not in advisory
