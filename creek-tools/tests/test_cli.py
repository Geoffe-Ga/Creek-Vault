"""Tests for creek CLI module."""

from typer.testing import CliRunner

from creek.cli import app

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


def test_process_command() -> None:
    """Test that process command runs with required args."""
    result = runner.invoke(
        app,
        ["process", "--source", "/fake/test", "--vault", "/fake/vault"],
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


def test_redact_scan() -> None:
    """Test that redact command runs with --scan flag."""
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", "/fake/src", "--vault", "/fake/vault"],
    )
    assert result.exit_code == 0


def test_redact_apply() -> None:
    """Test that redact command runs with --apply flag."""
    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", "/fake/src", "--vault", "/fake/vault"],
    )
    assert result.exit_code == 0


def test_redact_review() -> None:
    """Test that redact command runs with --review flag."""
    result = runner.invoke(
        app,
        [
            "redact",
            "--review",
            "--source",
            "/fake/src",
            "--vault",
            "/fake/vault",
        ],
    )
    assert result.exit_code == 0


def test_redact_report() -> None:
    """Test that redact command runs with --report flag."""
    result = runner.invoke(
        app,
        [
            "redact",
            "--scan",
            "--report",
            "--source",
            "/fake/src",
            "--vault",
            "/fake/vault",
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
    """Test that purge --help shows subcommand help."""
    result = runner.invoke(app, ["purge", "--help"])
    assert result.exit_code == 0
    assert "purge" in result.output.lower()


def test_purge_command() -> None:
    """Test that purge command runs with required args."""
    result = runner.invoke(
        app,
        ["purge", "--vault", "/fake/vault", "--target", "fragments"],
    )
    assert result.exit_code == 0


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


def test_skills_command() -> None:
    """Test that skills command runs with required args."""
    result = runner.invoke(
        app,
        [
            "skills",
            "--generate",
            "--vault",
            "/fake/vault",
            "--output",
            "/fake/out",
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


def test_mine_with_strategy() -> None:
    """Test that mine command runs with --strategy option."""
    result = runner.invoke(
        app,
        ["mine", "--vault", "/fake/vault", "--strategy", "frequency"],
    )
    assert result.exit_code == 0
