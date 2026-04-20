"""Tests for creek CLI module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


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
        ["process", "--source", str(source), "--vault", str(vault)],
    )
    assert result.exit_code == 0


def test_ingest_help() -> None:
    """Test that ingest --help shows subcommand help."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output.lower()


def test_ingest_command() -> None:
    """Test that ingest command runs with required args."""
    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            "/fake/in",
            "--vault",
            "/fake/vault",
        ],
    )
    assert result.exit_code == 0


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


def test_classify_command() -> None:
    """Test that classify command runs with required args."""
    result = runner.invoke(app, ["classify", "--vault", "/fake/vault"])
    assert result.exit_code == 0


def test_classify_with_options() -> None:
    """Test that classify command runs with all options."""
    result = runner.invoke(
        app,
        [
            "classify",
            "--vault",
            "/fake/vault",
            "--method",
            "llm",
            "--batch-size",
            "25",
        ],
    )
    assert result.exit_code == 0


def test_link_help() -> None:
    """Test that link --help shows subcommand help."""
    result = runner.invoke(app, ["link", "--help"])
    assert result.exit_code == 0
    assert "link" in result.output.lower()


def test_link_command() -> None:
    """Test that link command runs with required args."""
    result = runner.invoke(app, ["link", "--vault", "/fake/vault"])
    assert result.exit_code == 0


def test_link_with_method() -> None:
    """Test that link command runs with --method option."""
    result = runner.invoke(
        app,
        ["link", "--vault", "/fake/vault", "--method", "graph"],
    )
    assert result.exit_code == 0


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


def test_review_command() -> None:
    """Test that review command runs with required args."""
    result = runner.invoke(app, ["review", "--vault", "/fake/vault"])
    assert result.exit_code == 0


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


def test_gdrive_command() -> None:
    """Test that gdrive command runs with --download flag."""
    result = runner.invoke(
        app,
        ["gdrive", "--download", "--staging", "/fake/staging"],
    )
    assert result.exit_code == 0


def test_skills_help() -> None:
    """Test that skills --help shows subcommand help."""
    result = runner.invoke(app, ["skills", "--help"])
    assert result.exit_code == 0
    assert "skills" in result.output.lower()


def test_skills_command(tmp_path: Path) -> None:
    """Test that skills command runs with required args."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "skills",
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
