"""Tests for creek CLI module."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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
    monkeypatch.setattr(
        GoogleApiDriveClient,
        "get_media",
        lambda _self, _file_id: b"# Notes\n",
    )

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
    monkeypatch.setattr(
        GoogleApiDriveClient,
        "get_media",
        lambda _self, _file_id: b"# Notes\n",
    )

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
