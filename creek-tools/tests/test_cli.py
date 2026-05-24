"""Tests for creek CLI module."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    import pytest

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Return *text* with ANSI escape sequences removed.

    Typer/Click's Rich formatter splits long option names like
    ``--bypass-compiled`` across colour-styled segments when CI's
    terminal supports ANSI; the literal substring then disappears
    from ``result.output``. Stripping ANSI before substring assertions
    keeps the tests environment-independent.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


def test_help() -> None:
    """Test that --help shows application help text."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Creek knowledge organization pipeline" in result.output


def test_process_help() -> None:
    """Test that process --help shows subcommand help."""
    result = runner.invoke(app, ["process", "--help"])
    assert result.exit_code == 0
    assert "process" in result.output.lower()


def test_process_command(tmp_path: Path) -> None:
    """Test that process command runs with required args."""
    source = tmp_path / "source"
    source.mkdir()
    vault = tmp_path / "vault"
    for d in [
        "00-Creek-Meta",
        "01-Fragments",
        "02-Threads",
        "03-Eddies",
    ]:
        (vault / d).mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        ["process", "--source", str(source), "--vault", str(vault), "--yes"],
    )
    assert result.exit_code == 0, result.output


def test_ingest_help() -> None:
    """Test that ingest --help shows subcommand help."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output.lower()


def test_ingest_command_requires_input(tmp_path: Path) -> None:
    """The ingest command refuses to run when --input is missing."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(
        app,
        ["ingest", "--type", "markdown", "--vault", str(vault)],
    )
    assert result.exit_code == 2


def test_ingest_command_rejects_unknown_type(tmp_path: Path) -> None:
    """An unknown --type exits 2 with a hint listing known types."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    src = tmp_path / "in"
    src.mkdir()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "nope",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    assert result.exit_code == 2
    assert "Unknown ingestor type" in result.output


def test_ingest_command_gdrive_type_redirects_to_two_stage_flow(
    tmp_path: Path,
) -> None:
    """``--type gdrive`` prints the two-stage flow rather than the generic error.

    ARCH-001: ``gdrive`` is a downloader, not an ingestor. The CLI
    must direct the operator to ``creek gdrive --download`` followed
    by the appropriate ``--type document`` / ``--type spreadsheet``
    rather than failing with the bland "Unknown ingestor type" message.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    src = tmp_path / "in"
    src.mkdir()
    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "gdrive",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    assert result.exit_code == 2
    assert "gdrive is a downloader" in result.output
    assert "creek gdrive --download" in result.output


def test_ingest_command_writes_fragments(tmp_path: Path) -> None:
    """The ingest command resolves the registry and writes fragments."""
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)

    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("# Hello\n\nFirst note about systems.\n")

    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    written = list((vault / "01-Fragments").rglob("*.md"))
    assert len(written) >= 1


def test_ingest_command_idempotent(tmp_path: Path) -> None:
    """Running ingest twice against the same source writes no new files."""
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)

    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("# Hello\n\nIdempotent run.\n")

    runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    first = sorted((vault / "01-Fragments").rglob("*.md"))
    runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    second = sorted((vault / "01-Fragments").rglob("*.md"))
    assert [p.name for p in first] == [p.name for p in second]


def test_ingest_refresh_dates_backfills_authored_at(tmp_path: Path) -> None:
    """``creek ingest --refresh-dates`` re-runs authored_at extraction (FEAT-031).

    End-to-end: ingest a markdown file that the first pass leaves
    without ``authored_at``, then drop a frontmatter ``published``
    date on the source and re-run with ``--refresh-dates``. The
    fragment file in the vault should pick up the new ``authored_at``
    without re-ingesting the body.
    """
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)
    src = tmp_path / "src"
    src.mkdir()
    note = src / "note.md"
    # Start without a frontmatter date — authored_at will be None.
    note.write_text("# Hello\n\nBody content.\n", encoding="utf-8")

    runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    fragment_files = list((vault / "01-Fragments").rglob("*.md"))
    assert len(fragment_files) == 1
    # Initial state: ``authored_at`` is serialised as ``null`` (model_dump
    # emits all fields, including the None default for FEAT-031 fragments
    # that no ingestor extracted a date for).
    before = fragment_files[0].read_text(encoding="utf-8")
    assert "authored_at: null" in before

    # Now stamp a published date on the source and re-run refresh.
    note.write_text(
        "---\npublished: 2024-03-15\n---\n# Hello\n\nBody content.\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["ingest", "--refresh-dates", "--vault", str(vault)],
    )
    assert result.exit_code == 0, result.output
    after = fragment_files[0].read_text(encoding="utf-8")
    # The refresh swapped null → ISO string; the date prefix locks the
    # value without binding to a particular UTC time-of-day rendering.
    assert "authored_at: null" not in after
    assert "2024-03-15" in after
    # Body untouched.
    assert "Body content." in after


def test_ingest_refresh_dates_is_idempotent(tmp_path: Path) -> None:
    """A second ``--refresh-dates`` pass touches zero fragments.

    Locks the FEAT-031 idempotency contract: the file bytes must be
    identical across two consecutive backfill runs.
    """
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text(
        "---\npublished: 2024-03-15\n---\n# Hello\n",
        encoding="utf-8",
    )
    runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    # First refresh pass is a no-op because authored_at is already set
    # by the initial ingest.
    runner.invoke(app, ["ingest", "--refresh-dates", "--vault", str(vault)])
    fragment_files = list((vault / "01-Fragments").rglob("*.md"))
    snapshot1 = fragment_files[0].read_bytes()
    # Second refresh: byte-identical.
    runner.invoke(app, ["ingest", "--refresh-dates", "--vault", str(vault)])
    snapshot2 = fragment_files[0].read_bytes()
    assert snapshot1 == snapshot2


def test_redact_help() -> None:
    """Test that redact --help shows subcommand help."""
    result = runner.invoke(app, ["redact", "--help"])
    assert result.exit_code == 0
    assert "redact" in result.output.lower()


def test_redact_scan(tmp_path: Path) -> None:
    """Test that redact command runs with --scan flag."""
    source = tmp_path / "src"
    source.mkdir()
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source)],
    )
    assert result.exit_code == 0


def test_redact_apply(tmp_path: Path) -> None:
    """Test that redact command runs with --apply flag."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "ok.md").write_text("nothing interesting\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source)],
    )
    assert result.exit_code == 0


def test_redact_review(tmp_path: Path) -> None:
    """Test that redact command runs with --review flag."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(vault)],
    )
    assert result.exit_code == 0


def test_redact_report(tmp_path: Path) -> None:
    """Test that redact command runs with --report flag."""
    source = tmp_path / "src"
    source.mkdir()
    result = runner.invoke(
        app,
        [
            "redact",
            "--scan",
            "--report",
            "--source",
            str(source),
        ],
    )
    assert result.exit_code == 0


def test_classify_help() -> None:
    """Test that classify --help shows subcommand help."""
    result = runner.invoke(app, ["classify", "--help"])
    assert result.exit_code == 0
    assert "classify" in result.output.lower()


def test_classify_command_runs_against_empty_vault(tmp_path: Path) -> None:
    """``creek classify`` exits 0 against a vault with no fragments."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    result = runner.invoke(app, ["classify", "--vault", str(vault)])
    assert result.exit_code == 0, result.output


def test_classify_rejects_unknown_method(tmp_path: Path) -> None:
    """An unknown ``--method`` exits with code 2."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(
        app,
        ["classify", "--vault", str(vault), "--method", "magic"],
    )
    assert result.exit_code == 2
    assert "Unknown method" in result.output


def test_classify_rejects_unknown_reatomize_direction(tmp_path: Path) -> None:
    """An unknown ``--reatomize-direction`` exits with code 2."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "classify",
            "--vault",
            str(vault),
            "--reatomize",
            "--reatomize-direction",
            "sideways",
        ],
    )
    assert result.exit_code == 2
    assert "reatomize-direction" in result.output


def test_classify_reatomize_help_lists_flag() -> None:
    """The classify help text advertises the FEAT-023 flags."""
    result = runner.invoke(app, ["classify", "--help"])
    assert result.exit_code == 0
    # Typer's Rich-formatted help inserts ANSI colour escapes between
    # consecutive characters in option names (so the literal substring
    # ``--reatomize`` does not appear contiguously when the runner
    # captures the styled stream — observed on Python 3.12/3.13 CI).
    # Strip ANSI before asserting.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--reatomize" in plain
    assert "--reatomize-direction" in plain


def test_classify_rules_writes_method_to_frontmatter(tmp_path: Path) -> None:
    """``creek classify --method rules`` stamps the method on each fragment."""
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)

    fragment = Fragment(
        id="frag-test12345678",
        title="Power and dominance",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    body = (
        "Power dominance control conquest force aggression bold fearless "
        "warrior rage impulsive rebellion."
    )
    file = fragments_dir / "fragment.md"
    file.write_text(
        frontmatter.dumps(
            frontmatter.Post(content=body, **fragment.model_dump(mode="json")),
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["classify", "--vault", str(vault), "--method", "rules"],
    )
    assert result.exit_code == 0, result.output
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"
    assert "classified_at" in reloaded.metadata


def test_classify_preserves_manual_without_force(tmp_path: Path) -> None:
    """A ``manual`` decision is not overwritten when ``--force`` is absent."""
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)

    fragment = Fragment(
        id="frag-manual000000",
        title="Hand-tagged note",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    metadata = fragment.model_dump(mode="json")
    metadata["classification_method"] = "manual"
    metadata["classified_at"] = "2026-01-01T00:00:00-08:00"
    file = fragments_dir / "manual.md"
    file.write_text(
        frontmatter.dumps(frontmatter.Post(content="body", **metadata)),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["classify", "--vault", str(vault), "--method", "rules"],
    )
    assert result.exit_code == 0
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"


def test_classify_force_overrides_manual(tmp_path: Path) -> None:
    """``--force`` overwrites ``classification_method: manual`` decisions."""
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)

    fragment = Fragment(
        id="frag-manual000001",
        title="Tag it again",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    metadata = fragment.model_dump(mode="json")
    metadata["classification_method"] = "manual"
    file = fragments_dir / "manual.md"
    file.write_text(
        frontmatter.dumps(frontmatter.Post(content="body", **metadata)),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "classify",
            "--vault",
            str(vault),
            "--method",
            "rules",
            "--force",
        ],
    )
    assert result.exit_code == 0
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"


# ---- FEAT-017b: --calibrate ----


def _stub_fixed_label_classifier_in_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace LLMClassifier with a stub that always returns cal-001's labels.

    Used by render-only CLI tests where the exact rates do not matter,
    only that the calibration mechanism runs end-to-end without
    hitting the network. The agreement rates this produces are NOT
    100% — see `test_classify_calibrate_enforce_floors_passes_on_perfect_agreement`
    for a test that asserts the floor gate with a guaranteed-passing
    synthetic report instead.
    """
    from creek.classify.llm import LLMClassificationResult
    from creek.models import (
        Authorship,
        Dosage,
        Fragment,
        FragmentSource,
        Frequency,
        FrequencyClassification,
        Mode,
        Orientation,
        Phase,
        SourcePlatform,
        VoiceClassification,
        VoiceRegister,
        WavelengthClassification,
    )

    def _classify_perfectly(
        self: object,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        del self, content
        # Build a Fragment carrying the labels the perfect classifier
        # claims; the stub mirrors the expected labels from cal-001
        # so every fixture entry agrees on at least one dimension.
        return LLMClassificationResult(
            fragment=Fragment(
                id=fragment.id,
                title=fragment.title,
                source=FragmentSource(
                    platform=SourcePlatform.MARKDOWN,
                    author=Authorship.SELF,
                ),
                frequency=FrequencyClassification(primary=Frequency.F1),
                wavelength=WavelengthClassification(
                    phase=Phase.PEAKING,
                    mode=Mode.INHABIT,
                    orientation=Orientation.FEEL,
                    dosage=Dosage.AMBIGUOUS,
                ),
                voice=VoiceClassification(voice_register=VoiceRegister.RAW),
            ),
            reasoning="stub",
        )

    from creek.classify.llm import LLMClassifier as _RealLLMClassifier

    # Patch __init__ (the public constructor) rather than the private
    # `_check_availability` hook — patching by private name silently
    # breaks if the implementation renames the method, masking the
    # test's intent. Replacing __init__ with a no-op bypasses the
    # health check while leaving `classify_with_reasoning` (the
    # contract we actually exercise) wired in below.
    monkeypatch.setattr(_RealLLMClassifier, "__init__", lambda self, **_: None)
    monkeypatch.setattr(
        _RealLLMClassifier,
        "classify_with_reasoning",
        _classify_perfectly,
    )


def test_classify_calibrate_renders_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--calibrate` runs the fixture and prints a per-dimension table."""
    _stub_fixed_label_classifier_in_cli(monkeypatch)
    fixture = (
        Path(__file__).parent / "fixtures" / "classification" / "calibration_set.yaml"
    )
    result = runner.invoke(
        app,
        [
            "classify",
            "--calibrate",
            "--calibration-fixture",
            str(fixture),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Dimension" in result.output
    assert "frequency" in result.output
    assert "Entries scored:" in result.output


def test_classify_calibrate_missing_fixture_exits_two(tmp_path: Path) -> None:
    """`--calibrate` with a bad fixture path exits with code 2."""
    missing = tmp_path / "nope.yaml"
    result = runner.invoke(
        app,
        [
            "classify",
            "--calibrate",
            "--calibration-fixture",
            str(missing),
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.output


def test_classify_calibrate_enforce_floors_passes_on_perfect_agreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--enforce-floors` exits 0 when every floor is met."""
    from creek.classify.calibration import (
        CalibrationReport,
        DimensionAgreement,
    )

    # Bypass real classification entirely by stubbing run_calibration to
    # return a guaranteed-passing report.
    def _perfect_report(*_args: object, **_kwargs: object) -> CalibrationReport:
        return CalibrationReport(
            entries=10,
            agreements=tuple(
                DimensionAgreement(dimension=d, matches=10, total=10)
                for d in (
                    "frequency",
                    "phase",
                    "mode",
                    "orientation",
                    "dosage",
                    "voice_register",
                )
            ),
        )

    monkeypatch.setattr(
        "creek.classify.calibration.run_calibration",
        _perfect_report,
    )
    fixture = (
        Path(__file__).parent / "fixtures" / "classification" / "calibration_set.yaml"
    )
    result = runner.invoke(
        app,
        [
            "classify",
            "--calibrate",
            "--calibration-fixture",
            str(fixture),
            "--enforce-floors",
        ],
    )
    assert result.exit_code == 0, result.output


def test_classify_calibrate_enforce_floors_fails_on_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--enforce-floors` exits 1 when at least one dimension is below floor."""
    from creek.classify.calibration import (
        CalibrationReport,
        DimensionAgreement,
    )

    def _below_floor_report(*_args: object, **_kwargs: object) -> CalibrationReport:
        return CalibrationReport(
            entries=10,
            agreements=(
                # Every dimension at 0% agreement → breaches every floor.
                DimensionAgreement(dimension="frequency", matches=0, total=10),
                DimensionAgreement(dimension="phase", matches=0, total=10),
                DimensionAgreement(dimension="mode", matches=0, total=10),
                DimensionAgreement(dimension="orientation", matches=0, total=10),
                DimensionAgreement(dimension="dosage", matches=0, total=10),
                DimensionAgreement(dimension="voice_register", matches=0, total=10),
            ),
        )

    monkeypatch.setattr(
        "creek.classify.calibration.run_calibration",
        _below_floor_report,
    )
    fixture = (
        Path(__file__).parent / "fixtures" / "classification" / "calibration_set.yaml"
    )
    result = runner.invoke(
        app,
        [
            "classify",
            "--calibrate",
            "--calibration-fixture",
            str(fixture),
            "--enforce-floors",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "floors breached" in result.output


def test_link_help() -> None:
    """Test that link --help shows subcommand help."""
    result = runner.invoke(app, ["link", "--help"])
    assert result.exit_code == 0
    assert "link" in result.output.lower()


def test_link_command_runs_against_empty_vault(tmp_path: Path) -> None:
    """``creek link`` exits 0 against a vault with no fragments."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    result = runner.invoke(app, ["link", "--vault", str(vault)])
    assert result.exit_code == 0, result.output


def test_link_rejects_unknown_method(tmp_path: Path) -> None:
    """An unknown ``--method`` exits with code 2."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(
        app,
        ["link", "--vault", str(vault), "--method", "graph"],
    )
    assert result.exit_code == 2
    assert "Unknown method" in result.output


def test_link_rebuild_clears_embeddings_cache(tmp_path: Path) -> None:
    """``--rebuild`` deletes the cached embeddings parquet before linking."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    cache_dir = vault / "00-Creek-Meta"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "embeddings.parquet"
    cache_path.write_bytes(b"stale-cache")

    result = runner.invoke(
        app,
        [
            "link",
            "--vault",
            str(vault),
            "--method",
            "embeddings",
            "--rebuild",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not cache_path.exists()


def test_report_help() -> None:
    """Test that report --help shows subcommand help."""
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    assert "report" in result.output.lower()


def test_report_unnamed_command(tmp_path: Path) -> None:
    """Test that report --type unnamed generates a digest."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta",
        "00-Creek-Meta/Processing-Log",
        "01-Fragments",
        "10-Liminal",
        "10-Liminal/Unnamed",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "unnamed",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Unnamed digest generated" in result.output
    assert (vault / "10-Liminal" / "Unnamed" / "Digests").is_dir()


def test_report_voice_command(tmp_path: Path) -> None:
    """Test that report --type voice generates register profiles."""
    from datetime import UTC, datetime

    import frontmatter

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

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Journal").mkdir(parents=True, exist_ok=True)
    (vault / "07-Voice").mkdir(parents=True, exist_ok=True)

    for i in range(5):
        fragment = Fragment(
            id=f"frag-{i}",
            title=f"Fragment {i}",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ingested=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            frequency=FrequencyClassification(primary=Frequency.F5),
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
            ),
            voice=VoiceClassification(
                voice_register=VoiceRegister.CONFESSIONAL,
                confidence=Confidence.CONVICTION,
            ),
            privacy_tier=PrivacyTier.PERSONAL,
        )
        body = "The creek of thought flows gently downstream. " * 20
        data = fragment.model_dump(mode="json")
        post = frontmatter.Post(content=body, **data)
        target = vault / "01-Fragments" / "Journal" / f"{fragment.id}.md"
        target.write_text(frontmatter.dumps(post), encoding="utf-8")

    result = runner.invoke(
        app,
        ["report", "--type", "voice", "--vault", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert "Voice profile" in result.output or "voice profile" in result.output
    assert (vault / "07-Voice" / "confessional-profile.md").is_file()


def test_report_wavelength_weekly_command(tmp_path: Path) -> None:
    """Test that report --type wavelength --period weekly produces a file."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "weekly",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Wavelength weekly report generated" in result.output
    assert list((vault / "05-Wavelength" / "Phase-Maps").glob("*.md"))


def test_report_wavelength_monthly_command(tmp_path: Path) -> None:
    """Test that report --type wavelength --period monthly produces a file."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "monthly",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Wavelength monthly report generated" in result.output


def test_report_wavelength_unknown_period_errors(tmp_path: Path) -> None:
    """Test that wavelength report rejects an unknown period with exit 2."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "yearly",
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 2
    assert "weekly" in result.output


def test_report_command() -> None:
    """Test that report command runs with required args."""
    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "summary",
            "--period",
            "weekly",
            "--vault",
            "/fake/vault",
        ],
    )
    assert result.exit_code == 0


def test_review_help() -> None:
    """Test that review --help shows subcommand help."""
    result = runner.invoke(app, ["review", "--help"])
    assert result.exit_code == 0
    assert "review" in result.output.lower()


def test_review_empty_queue(tmp_path: Path) -> None:
    """Empty queue: ``creek review`` exits 0 with a friendly message."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    result = runner.invoke(app, ["review", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Review queue is empty" in result.output


def test_review_list_only(tmp_path: Path) -> None:
    """``creek review --list`` prints pending entries without prompting."""
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)
    fragment = Fragment(
        id="frag-pending00000",
        title="Need a human eye",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = fragments_dir / "pending.md"
    file.write_text(
        frontmatter.dumps(
            frontmatter.Post(content="body", **fragment.model_dump(mode="json")),
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["review", "--vault", str(vault), "--list"])
    assert result.exit_code == 0, result.output
    assert "Need a human eye" in result.output


def test_operator_identity_strips_metacharacters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial USER/USERNAME values are stripped before logging."""
    from creek.cli import _operator_identity

    monkeypatch.setenv("USER", "alice;rm -rf /\nboom")
    assert _operator_identity() == "alicerm -rf boom"


def test_operator_identity_truncates_long_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pathological-length env values are truncated to the configured cap."""
    from creek.cli import _OPERATOR_IDENTITY_MAX_LEN, _operator_identity

    monkeypatch.setenv("USER", "a" * 200)
    result = _operator_identity()
    assert len(result) == _OPERATOR_IDENTITY_MAX_LEN


def test_operator_identity_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty (or all-stripped) identity falls back to ``"cli"``."""
    from creek.cli import _operator_identity

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    assert _operator_identity() == "cli"

    monkeypatch.setenv("USER", ";;;\n\n")
    assert _operator_identity() == "cli"


def test_process_aborts_when_consent_declined(tmp_path: Path) -> None:
    """Declining the consent prompt aborts the pipeline non-zero."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("Just a note.\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    result = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault)],
    )
    # Non-interactive shell with no consent on file: exit 1.
    assert result.exit_code == 1
    assert "consent" in result.output.lower() or "Non-interactive" in result.output


def test_process_records_consent_with_yes_flag(tmp_path: Path) -> None:
    """``--yes`` records consent so a second run no longer prompts."""
    import json

    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("Body\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    result = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault), "--yes"],
    )
    assert result.exit_code == 0, result.output

    log_file = vault / "00-Creek-Meta" / "Processing-Log" / "consent-log.json"
    assert log_file.exists()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    records = data["records"]
    assert any(r["source_path"] == str(src) for r in records)


def test_process_skips_prompt_when_consent_already_recorded(tmp_path: Path) -> None:
    """Second invocation against a consented source needs no ``--yes``.

    The core INC-010 invariant: once consent is on file, subsequent
    runs against that source path proceed silently. A re-prompt would
    be friction; a silent abort would be worse.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("Body\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    # First run records consent via ``--yes``.
    first = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault), "--yes"],
    )
    assert first.exit_code == 0, first.output

    # Second run, no ``--yes`` — must succeed because consent is cached.
    second = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault)],
    )
    assert second.exit_code == 0, second.output
    # And it must not have re-prompted (no "First time processing").
    assert "First time processing" not in second.output


def test_process_consent_is_per_source(tmp_path: Path) -> None:
    """A different source path still triggers the gate.

    Consent is recorded per (source_type, source_path) tuple. Granting
    consent for ``/srcA`` must not waive the gate for ``/srcB`` —
    otherwise an operator's first-source approval would silently
    extend to every future source they ingest.
    """
    src_a = tmp_path / "src_a"
    src_a.mkdir()
    (src_a / "a.md").write_text("A\n")

    src_b = tmp_path / "src_b"
    src_b.mkdir()
    (src_b / "b.md").write_text("B\n")

    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    # Consent for src_a only.
    granted = runner.invoke(
        app,
        ["process", "--source", str(src_a), "--vault", str(vault), "--yes"],
    )
    assert granted.exit_code == 0, granted.output

    # src_b without ``--yes`` and no recorded consent → exit 1.
    blocked = runner.invoke(
        app,
        ["process", "--source", str(src_b), "--vault", str(vault)],
    )
    assert blocked.exit_code == 1
    assert "consent" in blocked.output.lower() or "Non-interactive" in blocked.output


def test_process_aborts_on_unresolved_redactions(tmp_path: Path) -> None:
    """``creek process`` exits 1 with a remediation hint when secrets exist."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "leaky.md").write_text("Email me at user@example.com\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    result = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault), "--yes"],
    )
    assert result.exit_code == 1
    # rich's console may soft-wrap the remediation hint, so match the parts
    # individually rather than the literal full command.
    output = result.output.lower()
    assert "redact" in output
    assert "apply" in output


def test_review_accept_writes_manual(tmp_path: Path) -> None:
    """Accepting an entry stamps ``classification_method: manual``."""
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)
    fragment = Fragment(
        id="frag-accept000000",
        title="Accept me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = fragments_dir / "accept.md"
    file.write_text(
        frontmatter.dumps(
            frontmatter.Post(content="body", **fragment.model_dump(mode="json")),
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["review", "--vault", str(vault)],
        input="a\n",
    )
    assert result.exit_code == 0, result.output
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"


def test_purge_help() -> None:
    """Test that purge --help shows subcommand group help."""
    result = runner.invoke(app, ["purge", "--help"])
    assert result.exit_code == 0
    assert "purge" in result.output.lower()


def test_gdrive_help() -> None:
    """Test that gdrive --help shows subcommand help."""
    result = runner.invoke(app, ["gdrive", "--help"])
    assert result.exit_code == 0
    assert "gdrive" in result.output.lower()


def test_gdrive_command_without_download_is_a_noop() -> None:
    """Without --download the command exits 0 with a hint."""
    result = runner.invoke(app, ["gdrive", "--staging", "/fake/staging"])
    assert result.exit_code == 0
    assert "Nothing to do" in result.output


def test_gdrive_command_errors_when_api_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without google-api-python-client installed, --download exits 1."""
    from creek.ingest.gdrive import GoogleApiDriveClient

    monkeypatch.setattr(
        GoogleApiDriveClient,
        "is_available",
        lambda _self: False,
    )
    staging = tmp_path / "staging"
    result = runner.invoke(
        app,
        ["gdrive", "--download", "--staging", str(staging)],
    )
    assert result.exit_code == 1
    assert "Google API client unavailable" in result.output


def test_gdrive_command_revoke_removes_local_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`creek gdrive --revoke` deletes the cached OAuth token (SEC-008)."""
    token = tmp_path / "token.json"
    token.write_text('{"refresh_token": "rt-test"}', encoding="utf-8")

    from creek.config import CreekConfig, GoogleDriveConfig

    fake = CreekConfig(
        google_drive=GoogleDriveConfig(token_file=str(token)),
    )
    monkeypatch.setattr("creek.cli.load_config", lambda: fake)

    class _StubResponse:
        status_code = 200
        is_success = True

    monkeypatch.setattr(
        "creek.ingest.gdrive.httpx.post",
        lambda *_a, **_kw: _StubResponse(),
    )

    result = runner.invoke(app, ["gdrive", "--revoke"])
    assert result.exit_code == 0, result.output
    assert not token.exists()
    assert "revoked" in result.output.lower() or "token" in result.output.lower()


def test_gdrive_command_rejects_download_and_revoke_together(
    tmp_path: Path,
) -> None:
    """`--download` and `--revoke` together is rejected with a clear error."""
    result = runner.invoke(
        app,
        ["gdrive", "--download", "--revoke", "--staging", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower() or "either" in result.output.lower()


def test_gdrive_command_downloads_files_through_stub_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full download path runs end-to-end against an injected client."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from creek.ingest.gdrive import DriveFile, GoogleApiDriveClient

    drive_file = DriveFile(
        id="x",
        name="notes.md",
        mime_type="text/markdown",
        modified_time=_datetime(2026, 4, 1, tzinfo=_UTC),
        size=10,
        parent_path="",
    )

    monkeypatch.setattr(
        GoogleApiDriveClient,
        "is_available",
        lambda _self: True,
    )
    monkeypatch.setattr(
        GoogleApiDriveClient,
        "list_files",
        lambda _self: [drive_file],
    )

    def _stream(_self: object, _file_id: str, destination: Path, **_: object) -> None:
        destination.write_bytes(b"# Notes\n")

    monkeypatch.setattr(GoogleApiDriveClient, "download_to", _stream)

    staging = tmp_path / "staging"
    result = runner.invoke(
        app,
        ["gdrive", "--download", "--staging", str(staging)],
    )
    assert result.exit_code == 0, result.output
    assert "Downloaded 1" in result.output
    assert "Skipped 0" in result.output
    assert (staging / "notes.md").read_bytes() == b"# Notes\n"


def test_gdrive_command_reports_skipped_files_on_incremental_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second run on unchanged files reports them as skipped, not downloaded."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from creek.ingest.gdrive import DriveFile, GoogleApiDriveClient

    drive_file = DriveFile(
        id="x",
        name="notes.md",
        mime_type="text/markdown",
        modified_time=_datetime(2026, 4, 1, tzinfo=_UTC),
        size=10,
        parent_path="",
    )
    monkeypatch.setattr(
        GoogleApiDriveClient,
        "is_available",
        lambda _self: True,
    )
    monkeypatch.setattr(
        GoogleApiDriveClient,
        "list_files",
        lambda _self: [drive_file],
    )

    def _stream(_self: object, _file_id: str, destination: Path, **_: object) -> None:
        destination.write_bytes(b"# Notes\n")

    monkeypatch.setattr(GoogleApiDriveClient, "download_to", _stream)

    staging = tmp_path / "staging"
    runner.invoke(app, ["gdrive", "--download", "--staging", str(staging)])
    second = runner.invoke(
        app,
        ["gdrive", "--download", "--staging", str(staging)],
    )
    assert second.exit_code == 0, second.output
    assert "Downloaded 0" in second.output
    assert "Skipped 1" in second.output


def test_gdrive_command_reports_drive_api_errors_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive API HTTP errors surface as a clean message, not a traceback."""
    from creek.ingest.gdrive import GoogleApiDriveClient

    monkeypatch.setattr(
        GoogleApiDriveClient,
        "is_available",
        lambda _self: True,
    )

    def _boom(_self: object) -> list[object]:
        msg = "<HttpError 403: 'User Rate Limit Exceeded'>"
        raise RuntimeError(msg)

    monkeypatch.setattr(GoogleApiDriveClient, "list_files", _boom)
    staging = tmp_path / "staging"
    result = runner.invoke(
        app,
        ["gdrive", "--download", "--staging", str(staging)],
    )
    assert result.exit_code == 1
    assert "Google Drive download failed" in result.output
    assert "Rate Limit" in result.output


def test_skills_help() -> None:
    """Test that skills --help shows subcommand help."""
    result = runner.invoke(app, ["skills", "--help"])
    assert result.exit_code == 0
    assert "skills" in result.output.lower()


def test_skills_command(tmp_path: Path) -> None:
    """Test that ``creek skills generate`` runs with required args."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--vault",
            str(vault),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0


def test_mine_help() -> None:
    """Test that mine --help shows subcommand help."""
    result = runner.invoke(app, ["mine", "--help"])
    assert result.exit_code == 0
    assert "mine" in result.output.lower()


def test_mine_command() -> None:
    """Test that mine command runs with required args."""
    result = runner.invoke(app, ["mine", "--vault", "/fake/vault"])
    assert result.exit_code == 0


def test_mine_with_phase() -> None:
    """Test that mine command accepts a wavelength phase option."""
    result = runner.invoke(
        app,
        ["mine", "--vault", "/fake/vault", "--phase", "rising"],
    )
    assert result.exit_code == 0


def test_mine_with_unknown_phase_errors() -> None:
    """Test that an unknown phase exits with code 2."""
    result = runner.invoke(
        app,
        ["mine", "--vault", "/fake/vault", "--phase", "nonsense"],
    )
    assert result.exit_code == 2


def test_draft_help() -> None:
    """Test that draft --help shows subcommand help."""
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    assert "draft" in result.output.lower()


def test_draft_command_no_seeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that draft on an empty vault reports no seeds and exits 0."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "No idea seeds surfaced" in result.output


def test_draft_command_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that draft mines an idea, generates a draft, and saves the file.

    Stubs the LLM and the idea miner so the test exercises the full CLI
    wiring path (mine → present → generate → save → exit 0) without
    depending on the heuristic miner producing real seeds.
    """
    from creek import cli as cli_module
    from creek.generate.mining import IdeaSeed, MiningStrategy
    from creek.models import Frequency

    seed = IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Naming what orbits",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(Frequency.F1,),
        brief_description="An essay waits here.",
        score=0.8,
    )

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda: lambda _p: "Generated draft body.",
    )

    def _stub_mine_all(
        _self: object,
        _vault: object,
        *,
        current_phase: object,
    ) -> list[IdeaSeed]:
        del current_phase
        return [seed]

    monkeypatch.setattr(
        "creek.generate.mining.IdeaMiner.mine_all",
        _stub_mine_all,
    )

    vault = tmp_path / "vault"
    for sub in ("01-Fragments", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)

    result = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Naming what orbits" in result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    assert drafts[0].name.endswith("-naming-what-orbits.md")


def test_draft_command_errors_when_llm_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that draft fails fast with exit 1 when the LLM is unavailable."""
    from creek.classify.llm import LLMClassifier

    monkeypatch.setattr(LLMClassifier, "available", property(lambda _self: False))
    vault = tmp_path / "vault"
    vault.mkdir()
    result = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert result.exit_code == 1
    assert "LLM provider unavailable" in result.output


def test_draft_unknown_phase_errors() -> None:
    """Test that an unknown phase exits with code 2."""
    result = runner.invoke(
        app,
        ["draft", "--vault", "/fake/vault", "--phase", "nonsense"],
    )
    assert result.exit_code == 2


def test_mine_bypass_compiled_flag_advertised_in_help() -> None:
    """``creek mine --help`` documents the FEAT-004 escape hatch."""
    result = runner.invoke(app, ["mine", "--help"])
    assert result.exit_code == 0
    assert "--bypass-compiled" in _strip_ansi(result.output)


def test_draft_bypass_compiled_flag_advertised_in_help() -> None:
    """``creek draft --help`` documents the FEAT-004 escape hatch."""
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    assert "--bypass-compiled" in _strip_ansi(result.output)


def test_mine_bypass_compiled_warns_and_skips_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--bypass-compiled`` constructs the miner in bypass mode and warns."""
    from creek.generate.mining import IdeaMiner

    captured: dict[str, bool] = {}
    real_init = IdeaMiner.__init__

    def _spy_init(self: IdeaMiner, **kwargs: object) -> None:
        captured["bypass"] = bool(kwargs.get("bypass_compiled"))
        real_init(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(IdeaMiner, "__init__", _spy_init)
    vault = tmp_path / "vault"
    vault.mkdir()

    result = runner.invoke(
        app,
        ["mine", "--vault", str(vault), "--bypass-compiled"],
    )

    assert result.exit_code == 0
    assert captured["bypass"] is True
    assert "--bypass-compiled" in result.output
    assert "side-step" in result.output.lower()


# ---- FEAT-032 manual seeding flags -----------------------------------


def _seed_test_vault(vault: Path) -> None:
    """Scaffold the minimal vault layout the seed-CLI tests expect."""
    for sub in ("01-Fragments", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_seed_fragment(
    vault: Path,
    *,
    frag_id: str,
    primary: str = "F1",
    phase: str = "unclassified",
    mode: str = "unclassified",
    title: str = "A note",
    body: str = "Body text.",
) -> None:
    """Write a minimal fragment markdown file with the requested classification."""
    import frontmatter

    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "source": {"platform": "claude", "kind": "unclassified"},
        "created": "2026-03-01T00:00:00+00:00",
        "ingested": "2026-03-01T00:00:00+00:00",
        "frequency": {"primary": primary, "secondary": []},
        "wavelength": {
            "phase": phase,
            "mode": mode,
            "orientation": "unclassified",
            "dosage": "unclassified",
            "color": "unclassified",
            "descriptor": "",
        },
        "voice": {"voice_register": None, "confidence": None},
        "praxis_potential": "latent",
        "privacy_tier": "open",
    }
    post = frontmatter.Post(content=body, **metadata)
    (vault / "01-Fragments" / f"{frag_id}.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )


def test_aptitude_labels_cover_every_frequency_name() -> None:
    """Each ``FREQUENCY_NAMES`` part has a matching ``--seed-frequency`` label.

    Regression guard against drift between the canonical ontology source
    (``creek.generate.indexes.FREQUENCY_NAMES``) and the CLI label map.
    """
    from creek.cli import _aptitude_frequency_labels
    from creek.generate.indexes import FREQUENCY_NAMES

    labels = _aptitude_frequency_labels()
    for freq, name in FREQUENCY_NAMES.items():
        for part in name.split("/"):
            normalized = part.strip().lower()
            assert labels.get(normalized) == freq.value, (
                f"missing alias '{normalized}' -> {freq.value}"
            )


def test_draft_seed_empty_topic_falls_back_to_mining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-topic ''`` is a no-op; mining behaviour is preserved."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-topic", ""],
    )
    assert result.exit_code == 0
    assert "No idea seeds surfaced" in result.output


def test_draft_seed_flags_advertised_in_help() -> None:
    """``creek draft --help`` documents every FEAT-032 seed flag."""
    result = runner.invoke(app, ["draft", "--help"])
    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    for flag in (
        "--seed-fragment",
        "--seed-topic",
        "--seed-frequency",
        "--seed-phase",
        "--seed-mode",
    ):
        assert flag in output, f"missing {flag} in draft help"


def test_draft_seed_fragment_mutually_exclusive_with_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-fragment`` plus ``--seed-topic`` exits 2 with a clear message."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-fragment",
            "frag-A",
            "--seed-topic",
            "X",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_draft_seed_fragment_mutually_exclusive_with_frequency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-fragment`` plus ``--seed-frequency`` exits 2."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-fragment",
            "frag-A",
            "--seed-frequency",
            "F1",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_draft_seed_invalid_frequency_lists_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``--seed-frequency`` exits 2 and lists valid codes + labels."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-frequency",
            "ascending",
        ],
    )
    assert result.exit_code == 2
    output = _strip_ansi(result.output)
    assert "Unknown --seed-frequency" in output
    assert "F1" in output


def test_draft_seed_invalid_phase_lists_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``--seed-phase`` exits 2 with the valid phases listed."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-phase",
            "ascending",
        ],
    )
    assert result.exit_code == 2
    assert "rising" in result.output


def test_draft_seed_invalid_mode_lists_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``--seed-mode`` exits 2 with the valid stances listed."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-mode",
            "drifting",
        ],
    )
    assert result.exit_code == 2
    assert "inhabit" in result.output


def test_draft_seed_fragment_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-fragment`` builds a draft from one specific fragment."""
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda: lambda _p: "Body composed from frag-keep.",
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(
        vault,
        frag_id="frag-keep",
        primary="F1",
        title="Naming what orbits",
    )
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-fragment", "frag-keep"],
    )
    assert result.exit_code == 0, result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    post = fm.load(str(drafts[0]))
    assert post.metadata["seed"] == {"fragment_id": "frag-keep"}
    assert post.metadata["source_fragments"] == ["frag-keep"]


def test_draft_seed_unknown_fragment_errors_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``--seed-fragment`` exits 1 with an honest message."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-fragment", "missing"],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_draft_seed_dimensional_filters_combine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-frequency`` + ``--seed-phase`` writes a draft from the intersection."""
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda: lambda _p: "Composed from the intersection.",
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(
        vault,
        frag_id="frag-A",
        primary="F1",
        phase="rising",
        mode="integrate",
    )
    _write_seed_fragment(
        vault,
        frag_id="frag-B",
        primary="F1",
        phase="peaking",
    )
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-frequency",
            "agency",
            "--seed-phase",
            "rising",
            "--seed-mode",
            "integrate",
        ],
    )
    assert result.exit_code == 0, result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    post = fm.load(str(drafts[0]))
    seed = post.metadata["seed"]
    assert seed["frequencies"] == ["F1"]
    assert seed["phases"] == ["rising"]
    assert seed["modes"] == ["integrate"]
    assert post.metadata["source_fragments"] == ["frag-A"]


def test_draft_seed_zero_match_exits_with_honest_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dimensional filter with zero matches exits 1 — never a silent fallback."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(vault, frag_id="frag-A", primary="F1")
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-frequency",
            "F10",
        ],
    )
    assert result.exit_code == 1
    assert "No source material matches" in result.output


def test_draft_seed_topic_with_frequency_filters_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-topic`` + ``--seed-frequency`` saves the seed flags as provenance."""
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "Draft.")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(
        vault,
        frag_id="frag-A",
        primary="F2",
        title="Belonging at the edge",
        body="A note about belonging in community.",
    )
    _write_seed_fragment(
        vault,
        frag_id="frag-B",
        primary="F2",
        title="Solo travel",
        body="Going alone.",
    )
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-topic",
            "belonging",
            "--seed-frequency",
            "receptivity",
        ],
    )
    assert result.exit_code == 0, result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    post = fm.load(str(drafts[0]))
    assert post.metadata["seed"] == {
        "topic": "belonging",
        "frequencies": ["F2"],
    }
    assert post.metadata["source_fragments"] == ["frag-A"]


def test_draft_without_seed_flags_keeps_mining_behaviour(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behaviour is unchanged when no seed flags are passed."""
    from creek import cli as cli_module

    monkeypatch.setattr(cli_module, "_build_draft_llm", lambda: lambda _p: "body")
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "No idea seeds surfaced" in result.output
