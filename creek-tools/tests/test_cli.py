"""Tests for creek CLI module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.ingest.base import IngestResult

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


def test_classify_help_mentions_anthropic_consent_env() -> None:
    """Issue #320: ``--method llm`` help must surface CREEK_ANTHROPIC_CONSENT.

    The Anthropic provider blocks at runtime if the consent env var is
    unset; the requirement was previously undiscoverable from the help
    text. The help must now name the variable so users see the gate
    before they kick off a doomed classify run.
    """
    result = runner.invoke(app, ["classify", "--help"])
    assert result.exit_code == 0
    plain = _strip_ansi(result.output)
    # Rich may wrap long help text mid-token; normalise whitespace
    # before substring asserts so a soft line break never hides the
    # token from the assertion.
    normalised = re.sub(r"\s+", " ", plain)
    assert "CREEK_ANTHROPIC_CONSENT" in normalised


def _write_anthropic_config(vault: Path) -> Path:
    """Write a minimal ``creek_config.yaml`` selecting the anthropic provider."""
    config_dir = vault / "00-Creek-Meta"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "creek_config.yaml"
    config_path.write_text(
        "llm:\n  provider: anthropic\n",
        encoding="utf-8",
    )
    return config_path


def test_classify_llm_anthropic_warns_when_consent_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #320: classify must pre-flight warn about missing consent.

    When the config selects the Anthropic provider, the CLI must surface
    the consent-env-var gate BEFORE iterating fragments. Today the gate
    only fires inside ``AnthropicProvider.__init__`` once the engine
    starts processing the first fragment — by then any setup time has
    been wasted.
    """
    from creek.classify.llm.providers import AnthropicProvider

    monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)
    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    config_path = _write_anthropic_config(vault)
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))

    result = runner.invoke(app, ["classify", "--vault", str(vault), "--method", "llm"])

    # The pre-flight gate aborts the run with a clear remediation hint.
    assert result.exit_code != 0
    plain = _strip_ansi(result.output)
    normalised = re.sub(r"\s+", " ", plain)
    assert "CREEK_ANTHROPIC_CONSENT" in normalised


def test_classify_llm_anthropic_skips_warning_when_consent_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-flight gate is silent once consent is on file.

    With ``CREEK_ANTHROPIC_CONSENT`` set, the pre-flight check must not
    abort and must not print the consent remediation text — the user
    has already acknowledged the data-egress decision.
    """
    from creek.classify.llm.providers import AnthropicProvider

    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    config_path = _write_anthropic_config(vault)
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))

    result = runner.invoke(app, ["classify", "--vault", str(vault), "--method", "llm"])

    # Empty vault + consent on file → engine returns cleanly.
    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "Set CREEK_ANTHROPIC_CONSENT" not in plain


def test_classify_rules_does_not_warn_about_anthropic_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--method rules`` never touches the cloud and must not nag.

    Local-only runs must remain quiet even when the YAML's
    ``llm.provider`` is ``anthropic`` and consent is unset, because
    the rules path never opens a network connection.
    """
    from creek.classify.llm.providers import AnthropicProvider

    monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)
    monkeypatch.delenv(AnthropicProvider.API_KEY_ENV, raising=False)

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    config_path = _write_anthropic_config(vault)
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))

    result = runner.invoke(
        app, ["classify", "--vault", str(vault), "--method", "rules"]
    )

    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "CREEK_ANTHROPIC_CONSENT" not in plain


def test_classify_llm_ollama_does_not_warn_about_anthropic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Ollama-configured vault must not surface the Anthropic gate."""
    from creek.classify.llm.providers import AnthropicProvider

    monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    # No config written → defaults apply (provider=ollama).
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    result = runner.invoke(app, ["classify", "--vault", str(vault), "--method", "llm"])

    plain = _strip_ansi(result.output)
    assert "CREEK_ANTHROPIC_CONSENT" not in plain


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


def test_classify_summary_distinguishes_manual_and_prior_llm(tmp_path: Path) -> None:
    """Issue #321: the summary line labels manual vs prior-LLM preservation distinctly.

    A vault with one ``classification_method: manual`` fragment and one
    ``classification_method: llm`` fragment (left over from a prior
    partial LLM run) must produce a summary that shows ``1 manual
    preserved`` and ``1 previously LLM-classified preserved`` rather
    than collapsing both into a single misleading "2 manual preserved"
    count.
    """
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)

    for frag_id, method in (
        ("frag-cli-manual0", "manual"),
        ("frag-cli-llmold0", "llm"),
    ):
        fragment = Fragment(
            id=frag_id,
            title=f"prior {method}",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        )
        metadata = fragment.model_dump(mode="json")
        metadata["classification_method"] = method
        file = fragments_dir / f"{frag_id}.md"
        file.write_text(
            frontmatter.dumps(frontmatter.Post(content="body", **metadata)),
            encoding="utf-8",
        )

    result = runner.invoke(
        app,
        ["classify", "--vault", str(vault), "--method", "rules"],
    )
    assert result.exit_code == 0, result.output
    # Rich/Typer may hard-wrap the summary at terminal width, so
    # collapse whitespace before substring matching — otherwise the
    # assertion would fail when Rich inserts a newline mid-phrase
    # (e.g. ``LLM-classified\npreserved``).
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "1 manual preserved" in normalized
    assert "1 previously LLM-classified preserved" in normalized


# ---- Issue #317: --method llm exits non-zero on unavailable provider ----


def _seed_single_fragment(vault: Path) -> Path:
    """Write one minimal Creek fragment under *vault* and return its path.

    Local CLI-test seed. The engine-test suite has a richer
    :func:`tests.helpers.write_fragment_file` helper; this one is kept
    minimal so the CLI tests can assert on a known-clean
    ``ordinary content`` body without inheriting engine-test fixtures.
    """
    import frontmatter

    from creek.models import Fragment, FragmentSource, SourcePlatform

    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)
    fragment = Fragment(
        id="frag-cliseed00001",
        title="placeholder",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = fragments_dir / "frag.md"
    file.write_text(
        frontmatter.dumps(
            frontmatter.Post(
                content="ordinary content",
                **fragment.model_dump(mode="json"),
            ),
        ),
        encoding="utf-8",
    )
    return file


def test_classify_llm_unavailable_exits_non_zero(tmp_path: Path) -> None:
    """``creek classify --method llm`` exits non-zero when provider is down.

    Reproduces the reported symptom: prior to the fix the CLI printed
    ``Classified N of N`` and exited 0 even when the LLM provider had
    not run a single classification. The shell pipeline could not
    distinguish success from silent failure.
    """
    from unittest.mock import PropertyMock, patch

    from creek.classify.classify_engine import LLMClassifier

    vault = tmp_path / "vault"
    _seed_single_fragment(vault)

    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=False,
    ):
        result = runner.invoke(
            app,
            ["classify", "--vault", str(vault), "--method", "llm"],
        )

    assert result.exit_code != 0, result.output
    # The misleading "Classified N of N" summary must NOT appear when
    # zero fragments were classified — that is the second half of the
    # bug report.
    assert "Classified" not in result.output


def test_classify_llm_unavailable_message_names_provider(
    tmp_path: Path,
) -> None:
    """The aborted-run message names the provider so the user can fix it."""
    from unittest.mock import PropertyMock, patch

    from creek.classify.classify_engine import LLMClassifier

    vault = tmp_path / "vault"
    _seed_single_fragment(vault)

    # CreekConfig defaults to provider="ollama"; the message must say so.
    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=False,
    ):
        result = runner.invoke(
            app,
            ["classify", "--vault", str(vault), "--method", "llm"],
        )

    plain = _strip_ansi(result.output)
    assert "ollama" in plain
    assert "aborted" in plain.lower() or "unavailable" in plain.lower()


def test_classify_llm_unavailable_leaves_fragment_unstamped(
    tmp_path: Path,
) -> None:
    """No fragment ends up stamped with ``classification_method: llm``.

    The pre-fix bug rewrote every fragment with ``classification_method:
    llm`` even when the LLM never ran — corrupting the resume contract
    (next run would skip them, thinking they were already classified).
    Pin the fix: when the provider is unavailable, fragments are left
    completely untouched.
    """
    from unittest.mock import PropertyMock, patch

    import frontmatter

    from creek.classify.classify_engine import LLMClassifier

    vault = tmp_path / "vault"
    file = _seed_single_fragment(vault)
    before = frontmatter.load(str(file))
    assert "classification_method" not in before.metadata

    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=False,
    ):
        runner.invoke(
            app,
            ["classify", "--vault", str(vault), "--method", "llm"],
        )

    after = frontmatter.load(str(file))
    assert "classification_method" not in after.metadata


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


def test_link_embeddings_output_phrases_similarity_edges(tmp_path: Path) -> None:
    """Embeddings CLI output explicitly says "similarity edges", not "links".

    Issue #338 Problem B: the pre-fix output used "link(s)" for pairwise
    similarity edges stored only in the parquet cache. That implied a
    user-visible side effect in fragment frontmatter that did not exist.
    The fix is to phrase the count accurately as similarity edges
    cached in the parquet so operators know what they're being shown.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["link", "--vault", str(vault), "--method", "embeddings"],
    )
    assert result.exit_code == 0, result.output
    # New phrasing — accurate to what the embeddings method does.
    assert "similarity edge" in result.output.lower()
    assert "embeddings.parquet" in result.output


def test_link_embeddings_model_unavailable_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-load failure aborts ``creek link`` with the remediation.

    When ``sentence_transformers.SentenceTransformer(...)`` cannot be
    constructed (weights missing offline, corrupt cache, …), the
    embeddings linker raises ``EmbeddingModelUnavailableError`` whose
    message names the model and spells out the fix. The CLI must catch
    that typed error, print its remediation-rich message, and exit
    non-zero rather than reporting a clean success.
    """
    from creek.models import Fragment, FragmentSource, SourcePlatform
    from tests.helpers import write_fragment_file

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-00000000000c",
            title="A fragment to embed",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        ),
        body="Body text.",
    )

    def _raise(_model_name: str, _cache_folder: str | None) -> object:
        msg = "boom"
        raise OSError(msg)

    monkeypatch.setattr(
        "creek.link.embeddings._load_sentence_transformer",
        _raise,
    )

    result = runner.invoke(
        app,
        ["link", "--vault", str(vault), "--method", "embeddings"],
    )
    assert result.exit_code != 0
    normalized = " ".join(_strip_ansi(result.output).split())
    # The remediation string from EmbeddingModelUnavailableError.
    assert "run `creek link --method embeddings` once" in normalized
    assert "network access" in normalized


def test_link_eddies_output_phrases_eddies_written(tmp_path: Path) -> None:
    """Eddies CLI output reports both detected and written counts.

    Issue #338 Problem A: the pre-fix output said ``1 link(s)`` even when
    nothing materialised in the vault. The fix is to make the side
    effect (an eddy .md under ``03-Eddies/``) explicit in the count.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["link", "--vault", str(vault), "--method", "eddies"],
    )
    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    assert "eddies" in output_lower
    # Both halves of the contract are surfaced explicitly.
    assert "detected" in output_lower
    assert "written" in output_lower
    assert "03-eddies" in output_lower


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


def _report_vault(tmp_path: Path) -> Path:
    """Minimal vault for a report-command invocation."""
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "00-Creek-Meta/Processing-Log", "01-Fragments"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def test_report_unknown_type_errors(tmp_path: Path) -> None:
    """`report --type <unknown>` errors with the valid list, not a no-op (#716)."""
    vault = _report_vault(tmp_path)
    result = runner.invoke(
        app, ["report", "--type", "paradoxx", "--vault", str(vault)]
    )
    assert result.exit_code != 0, result.output
    assert "paradoxx" in result.output  # names the bad type
    assert "decisions" in result.output  # lists real valid types
    assert "unnamed" in result.output
    assert "Would report" not in result.output  # the silent no-op is gone


def test_report_missing_type_errors(tmp_path: Path) -> None:
    """`report` with no --type errors the same helpful way (#716)."""
    vault = _report_vault(tmp_path)
    result = runner.invoke(app, ["report", "--vault", str(vault)])
    assert result.exit_code != 0, result.output
    assert "Would report" not in result.output


def test_report_known_type_still_dispatches(tmp_path: Path) -> None:
    """A valid --type still runs its handler (no regression) (#716)."""
    vault = _report_vault(tmp_path)
    (vault / "10-Liminal" / "Unnamed").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(
        app, ["report", "--type", "unnamed", "--vault", str(vault)]
    )
    assert result.exit_code == 0, result.output


def test_report_decisions_no_candidates_is_friendly(tmp_path: Path) -> None:
    """``report --type decisions`` with no signalling fragments is friendly (#581).

    The handler is now real (not the #579 stub): an empty corpus prints the
    "no new decision candidates" message, writes nothing, and never emits the
    old "Would generate" stub text.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "No decision notes generated" in result.output
    assert "Would generate" not in result.output


def test_report_decisions_generates_note(tmp_path: Path) -> None:
    """``report --type decisions`` writes a Decision note for a signalling fragment."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True)
    (frags / "frag-decide99.md").write_text(
        '---\ntype: fragment\nid: frag-decide99\ntitle: "Should I move to the coast"\n'
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "Decision notes generated" in result.output
    assert any((vault / "08-Decisions" / "Active").glob("*.md"))


def test_report_lexicon_no_exemplars_is_friendly(tmp_path: Path) -> None:
    """``report --type lexicon`` on a vault with no exemplars is friendly (#580).

    The handler is now real (not the #579 stub), so an empty corpus prints the
    "no qualifying exemplars" message, writes no glossary, and never falls back
    to the old "Would generate" stub text.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["report", "--type", "lexicon", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "No lexicon generated" in result.output
    assert "Would generate" not in result.output
    assert not (vault / "07-Voice" / "Lexicon").exists()


def test_report_rhetorical_patterns_no_exemplars_is_friendly(tmp_path: Path) -> None:
    """``report --type rhetorical-patterns`` with no exemplars is friendly (#582)."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["report", "--type", "rhetorical-patterns", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "No rhetorical patterns written" in result.output


def test_report_rhetorical_patterns_generates(tmp_path: Path) -> None:
    """``report --type rhetorical-patterns`` writes a per-register note (#582)."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments" / "Journal"
    frags.mkdir(parents=True)
    (frags / "ex-1.md").write_text(
        '---\ntype: fragment\nid: ex-1\ntitle: "T"\n'
        "source:\n  platform: journal\n  author: self\n"
        "voice:\n  voice_register: confessional\n  confidence: conviction\n"
        "---\nThe truth is we rise; as I said before, we rise.\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["report", "--type", "rhetorical-patterns", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "Rhetorical patterns written" in result.output
    assert (vault / "07-Voice" / "Rhetorical-Patterns" / "confessional.md").exists()


def test_report_mode_profiles_no_data_is_friendly(tmp_path: Path) -> None:
    """``report --type mode-profiles`` with no classified modes is friendly (#583)."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)

    result = runner.invoke(
        app,
        ["report", "--type", "mode-profiles", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "No mode profiles written" in result.output


def test_report_mode_profiles_generates(tmp_path: Path) -> None:
    """``report --type mode-profiles`` writes a per-mode note (#583)."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments"
    frags.mkdir(parents=True)
    (frags / "m1.md").write_text(
        '---\ntype: fragment\nid: m1\ntitle: "Building momentum"\n'
        "source:\n  platform: journal\n  author: self\n"
        "wavelength:\n  mode: express\n  phase: rising\n"
        "frequency:\n  primary: F3\n---\nbody\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["report", "--type", "mode-profiles", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "Mode profiles written" in result.output
    assert (vault / "05-Wavelength" / "Mode-Profiles" / "express.md").exists()


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
    """An unrecognised report type (`summary`) errors with the valid list (#716)."""
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
    assert result.exit_code == 2
    assert "summary" in result.output
    assert "Would report" not in result.output


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


def test_skills_signature_only_flag_emits_signature_files(tmp_path: Path) -> None:
    """``--signature-only`` writes .SIGNATURE.md files alongside no SKILL ones."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--signature-only",
            "--vault",
            str(vault),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0
    assert "signature-only" in result.output
    signature_files = list(output.rglob("*.SIGNATURE.md"))
    skill_files = list(output.rglob("*.SKILL.md"))
    assert signature_files, "expected signature files to be written"
    assert not skill_files, "signature-only run must not emit .SKILL.md"


def test_skills_both_variants_can_coexist(tmp_path: Path) -> None:
    """Running with and without ``--signature-only`` produces both trees."""
    vault = tmp_path / "vault"
    vault.mkdir()
    output = tmp_path / "out"
    default_run = runner.invoke(
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
    assert default_run.exit_code == 0
    signature_run = runner.invoke(
        app,
        [
            "skills",
            "generate",
            "--generate",
            "--signature-only",
            "--vault",
            str(vault),
            "--output",
            str(output),
        ],
    )
    assert signature_run.exit_code == 0
    assert list(output.rglob("*.SKILL.md"))
    assert list(output.rglob("*.SIGNATURE.md"))


def test_mine_help() -> None:
    """Test that mine --help shows subcommand help."""
    result = runner.invoke(app, ["mine", "--help"])
    assert result.exit_code == 0
    assert "mine" in result.output.lower()


def test_mine_command(tmp_path: Path) -> None:
    """Test that mine command runs with required args."""
    result = runner.invoke(app, ["mine", "--vault", str(tmp_path)])
    assert result.exit_code == 0


def test_mine_with_phase(tmp_path: Path) -> None:
    """Test that mine command accepts a wavelength phase option."""
    result = runner.invoke(
        app,
        ["mine", "--vault", str(tmp_path), "--phase", "rising"],
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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
        lambda *_a, **_k: lambda _p: "Generated draft body.",
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


def test_draft_cohesion_flag_advertised_in_help() -> None:
    """The opt-in ``--cohesion`` flag is documented in ``creek draft --help``."""
    # Render at a wide width so the option name cannot wrap, and strip ANSI
    # so colour codes can't split the ``--cohesion`` token (CI renders the
    # rich help table coloured and at ~80 cols, breaking literal matching).
    result = runner.invoke(app, ["draft", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "--cohesion" in normalized


def test_draft_cohesion_flag_smooths_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek draft --cohesion`` runs the entity-preserving cohesion pass.

    Stubs a two-phase LLM: the first call composes a seamed body, the
    cohesion call (recognised by its ``## Cohesion directive`` block) returns
    a transitions-only smoothed body. The saved draft must carry the smoothed
    body — proving the flag wired the pass on.
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
    smoothed = "My dad watched Pluribus. And so, doubt is the spine."

    def _two_phase(prompt: str) -> str:
        if "## Cohesion directive" in prompt:
            return smoothed
        return "My dad watched Pluribus. Doubt is the spine."

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: _two_phase,
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

    result = runner.invoke(app, ["draft", "--vault", str(vault), "--cohesion"])
    assert result.exit_code == 0, result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    assert smoothed in drafts[0].read_text(encoding="utf-8")


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


# ---- Issue #340: zero-seed diagnostics -------------------------------


def test_mine_no_seeds_message_lists_per_strategy_breakdown(
    tmp_path: Path,
) -> None:
    """The zero-seed CLI output reports per-strategy diagnostics (issue #340)."""
    vault = tmp_path / "vault"
    vault.mkdir()

    result = runner.invoke(app, ["mine", "--vault", str(vault)])

    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "No idea seeds surfaced" in output
    # Every strategy gets its own line so per-strategy units stay
    # coherent (fragment counts vs. Jaccard similarity vs. binary gate).
    for name in (
        "thread_terminus",
        "liminal_cross_eddy",
        "wavelength_window",
        "resonance_chain",
    ):
        assert name in output
    # Each row still shows the score / threshold pair side-by-side so the
    # operator can tell whether a run was *close* or nowhere near.
    assert "score" in output.lower()
    assert "threshold" in output.lower()
    assert "considered" in output.lower()
    assert "kept" in output.lower()
    # Operators are pointed at the gap log for fallback reasons.
    assert "compile-gaps.jsonl" in output


def test_mine_respects_creek_config_mining_knobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek_config.yaml`` mining knobs are passed through to ``IdeaMiner``."""
    import yaml

    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        yaml.safe_dump(
            {
                "mining": {
                    "min_thread_fragments": 2,
                    "min_chain_length": 2,
                    "similarity_liminal": 0.1,
                    "similarity_resonance": 0.2,
                },
            },
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    from creek.generate.mining import IdeaMiner

    real_init = IdeaMiner.__init__

    def _spy_init(self: IdeaMiner, **kwargs: object) -> None:
        captured.update(kwargs)
        real_init(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(IdeaMiner, "__init__", _spy_init)
    result = runner.invoke(
        app,
        ["mine", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert captured["min_thread_fragments"] == 2
    assert captured["min_chain_length"] == 2
    assert captured["similarity_liminal"] == pytest.approx(0.1)
    assert captured["similarity_resonance"] == pytest.approx(0.2)


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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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


def test_draft_outline_flags_advertised_in_help() -> None:
    """``creek draft --help`` documents the issue #354 outline flags."""
    result = runner.invoke(app, ["draft", "--help"])
    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "--seed-outline" in output
    assert "--seed-outline-text" in output


def _stub_outline_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the LLM and ontology detector for outline CLI happy-path tests."""
    from creek import cli as cli_module
    from creek.classify.prompt import PromptOntology

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _p: "Stitched body.",
    )
    monkeypatch.setattr(
        cli_module,
        "_build_ontology_detector",
        lambda _vault: lambda prompt: PromptOntology(prompt=prompt),
    )


def test_draft_seed_outline_text_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-outline-text`` composes a multi-section draft and saves it."""
    _stub_outline_detector(monkeypatch)
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline-text",
            "# Title\n\n## One\nFirst.\n\n## Two\nSecond.\n",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Draft saved" in result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1


def test_draft_seed_outline_file_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-outline <file>`` reads the outline from disk and composes it."""
    _stub_outline_detector(monkeypatch)
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    outline_file = tmp_path / "outline.md"
    outline_file.write_text("## Alpha\nA.\n\n## Beta\nB.\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-outline", str(outline_file)],
    )
    assert result.exit_code == 0, result.output
    assert "Draft saved" in result.output


def test_draft_seed_outline_no_headers_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outline with no markdown headers exits 2 with a clear error."""
    _stub_outline_detector(monkeypatch)
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline-text",
            "Just a paragraph, no headers.",
        ],
    )
    assert result.exit_code == 2
    assert "no markdown headers" in result.output


def test_draft_seed_outline_mutually_exclusive_inline_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both --seed-outline and --seed-outline-text exits 2."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    outline_file = tmp_path / "outline.md"
    outline_file.write_text("## H\nB.\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline",
            str(outline_file),
            "--seed-outline-text",
            "## H\nB.\n",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_draft_seed_outline_mutually_exclusive_with_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combining outline mode with --seed-topic exits 2."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline-text",
            "## H\nB.\n",
            "--seed-topic",
            "X",
        ],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_draft_seed_outline_file_read_error_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing outline file exits 2 with a read error."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline",
            str(tmp_path / "does-not-exist.md"),
        ],
    )
    assert result.exit_code == 2
    assert "Could not read --seed-outline" in result.output


def test_draft_seed_fragment_mutually_exclusive_with_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-fragment`` plus ``--seed-topic`` exits 2 with a clear message."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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
        lambda *_a, **_k: lambda _p: "Body composed from frag-keep.",
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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
    """``--seed-frequency`` + ``--seed-phase`` writes a per-dimension blended draft.

    Per issue #351 the three dimensions now OR rather than AND; each
    contributes its slice independently and the union is the seed.
    The frontmatter records per-dimension attribution so the operator
    can trace which fragment came from which dimension.
    """
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _p: "Composed from the per-dimension blend.",
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
    # Both fragments make it in: frag-A matches all three dims; frag-B
    # matches only the F1 frequency. Order follows first-seen within
    # the union (phase first, then mode, then frequency).
    assert set(post.metadata["source_fragments"]) == {"frag-A", "frag-B"}
    # Per-dimension attribution records which fragment came from which
    # dimension — the issue #351 acceptance criterion.
    per_dim = post.metadata["per_dimension_sources"]
    assert per_dim["phase:rising"] == ["frag-A"]
    assert per_dim["mode:integrate"] == ["frag-A"]
    assert set(per_dim["frequency:F1"]) == {"frag-A", "frag-B"}


def test_draft_seed_zero_match_exits_with_honest_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dimensional filter with zero matches exits 1 — never a silent fallback.

    Per issue #351 the wording shifted to "No source material in any
    attempted dimension: <labels>" so the operator can see which
    filters to widen.
    """
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
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
    assert "No source material in any attempted dimension" in result.output
    assert "the F10 frequency" in result.output


def test_draft_seed_topic_with_frequency_filters_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--seed-topic`` + ``--seed-frequency`` saves the seed flags as provenance."""
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "Draft."
    )
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

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "No idea seeds surfaced" in result.output


# ---- Issue #352: --ontology-twist CLI flag ---------------------------------


def test_draft_ontology_twist_advertised_in_help() -> None:
    """``creek draft --help`` documents ``--ontology-twist``."""
    result = runner.invoke(app, ["draft", "--help"])
    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "--ontology-twist" in output


def test_draft_ontology_twist_without_seed_exits_with_clear_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--ontology-twist`` alone (no seed) exits 2 with a clear hint."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--ontology-twist"],
    )
    assert result.exit_code == 2
    output = _strip_ansi(result.output)
    assert "--ontology-twist requires a seed" in output


def test_draft_ontology_twist_plurality_failure_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-source twist fails fast at the CLI with the plurality error.

    Issue #352 acceptance: "fails loudly if only one source matches".
    """
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module, "_build_draft_llm", lambda *_a, **_k: lambda _p: "body"
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(vault, frag_id="frag-only", primary="F1", phase="peaking")
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-phase",
            "peaking",
            "--ontology-twist",
        ],
    )
    assert result.exit_code == 1
    output = _strip_ansi(result.output)
    assert "at least two source fragments" in output


def test_draft_ontology_twist_happy_path_writes_twist_frontmatter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--ontology-twist`` with a plural source set records twist provenance.

    Two withdrawal-phase fragments are retrieved by the explicit
    ``--seed-phase withdrawal`` filter; the target profile takes the
    withdrawal pick from the same flag, so divergence (if any) comes
    from inspecting both profiles. The acceptance criterion is that the
    frontmatter records source/target profiles + twist_dimensions.
    """
    import frontmatter as fm

    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _p: "Twisted draft body.",
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    # Two withdrawal-phase fragments so the WITHDRAWAL slice is plural;
    # both also share mode=inhabit so source.mode is deterministic.
    _write_seed_fragment(
        vault, frag_id="frag-A", primary="F1", phase="withdrawal", mode="inhabit"
    )
    _write_seed_fragment(
        vault, frag_id="frag-B", primary="F1", phase="withdrawal", mode="inhabit"
    )

    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-phase",
            "withdrawal",
            "--seed-mode",
            "express",
            "--ontology-twist",
        ],
    )
    assert result.exit_code == 0, result.output
    drafts = list((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1
    post = fm.load(str(drafts[0]))
    # Frontmatter records both profiles and the divergent dimensions.
    assert post.metadata["source_profile"]["phase"] == "withdrawal"
    assert post.metadata["source_profile"]["mode"] == "inhabit"
    assert post.metadata["target_profile"]["phase"] == "withdrawal"
    assert post.metadata["target_profile"]["mode"] == "express"
    # Only mode diverges between source and target.
    assert post.metadata["twist_dimensions"] == ["mode"]


def test_draft_max_tokens_flag_advertised_in_help() -> None:
    """``creek draft --help`` documents the --max-tokens flag."""
    result = runner.invoke(app, ["draft", "--help"])
    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "--max-tokens" in output


def test_draft_seed_empty_body_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seeded draft whose LLM returns an empty body exits 1 (no traceback)."""
    from creek import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _p: "   ",
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(vault, frag_id="frag-X", title="Topic seed", body="seedable")
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-topic", "seedable"],
    )
    assert result.exit_code == 1, result.output
    assert "empty" in _strip_ansi(result.output).lower()


def test_draft_outline_empty_section_body_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outline section with an empty LLM body exits 1 with a readable message."""
    from creek import cli as cli_module
    from creek.classify.prompt import PromptOntology

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _p: "   ",
    )
    monkeypatch.setattr(
        cli_module,
        "_build_ontology_detector",
        lambda _vault: lambda prompt: PromptOntology(prompt=prompt),
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-outline-text",
            "## One\nFirst.\n",
        ],
    )
    assert result.exit_code == 1, result.output
    output = _strip_ansi(result.output).lower()
    assert "empty" in output
    assert "traceback" not in output


def test_draft_max_tokens_threaded_to_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--max-tokens`` is forwarded to the draft-LLM factory."""
    from creek import cli as cli_module
    from creek.generate.drafts import DraftLLMResponse

    captured: dict[str, object] = {}

    def _fake_builder(max_tokens: int | None = None) -> object:
        captured["max_tokens"] = max_tokens
        return lambda _p: DraftLLMResponse(text="Generated draft body.")

    monkeypatch.setattr(cli_module, "_build_draft_llm", _fake_builder)

    def _stub_mine_all(
        _self: object,
        _vault: object,
        *,
        current_phase: object,
    ) -> list[object]:
        del current_phase
        from creek.generate.mining import IdeaSeed, MiningStrategy

        return [
            IdeaSeed(
                strategy=MiningStrategy.RESONANCE_CHAIN,
                title="An idea",
                source_fragments=(),
                threads=(),
                eddies=(),
                frequency_affinity=(),
                brief_description="A draftable idea.",
                score=0.5,
            ),
        ]

    monkeypatch.setattr(
        "creek.generate.mining.IdeaMiner.mine_all",
        _stub_mine_all,
    )
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--max-tokens", "4096"],
    )
    assert result.exit_code == 0, result.output
    assert captured["max_tokens"] == 4096


def test_build_draft_llm_falls_back_to_config_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no flag, _build_draft_llm uses the loaded config's draft.max_tokens."""
    from creek import cli as cli_module
    from creek.classify.llm.providers import AnthropicCompletion
    from creek.config import CreekConfig, DraftConfig

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli_module,
        "load_config",
        lambda *_a, **_k: CreekConfig(draft=DraftConfig(max_tokens=2048)),
    )

    class _Classifier:
        available = True

        def __init__(self, _config: object) -> None:
            pass

        def invoke_prompt_with_metadata(
            self,
            prompt: str,
            *,
            max_tokens: int | None = None,
        ) -> AnthropicCompletion:
            captured["max_tokens"] = max_tokens
            del prompt
            return AnthropicCompletion(text="body")

    monkeypatch.setattr("creek.classify.llm.LLMClassifier", _Classifier)

    llm = cli_module._build_draft_llm()
    llm("a prompt")
    assert captured["max_tokens"] == 2048


# ---- voice-check subcommand ------------------------------------------


def _voice_fixture_dir(name: str) -> Path:
    """Return the ``tests/fixtures/voice/<name>`` directory.

    Args:
        name: Sub-directory under the voice fixture root (``in_voice`` or
            ``slop``).

    Returns:
        Absolute path to the requested fixture sub-directory.
    """
    return Path(__file__).resolve().parent / "fixtures" / "voice" / name


def _scaffold_voice_vault(vault: Path) -> None:
    """Write the in-voice exemplars into *vault* as self-authored fragments.

    Mirrors the regression harness: each in-voice fixture becomes a minimal
    ``source.author: self`` fragment so the production fingerprint builder
    measures it. The set is large enough (six exemplars) to clear the
    ``min_fingerprint_fragments`` floor, so the resulting fingerprint is not
    treated as thin.

    Args:
        vault: Vault root to scaffold.
    """
    fragments_dir = vault / "01-Fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(sorted(_voice_fixture_dir("in_voice").glob("*.md"))):
        body = path.read_text(encoding="utf-8")
        fragment = (
            "---\n"
            "source:\n"
            "  author: self\n"
            "  platform: markdown\n"
            "privacy_tier: open\n"
            "---\n"
            f"{body}\n"
        )
        (fragments_dir / f"exemplar_{index:02d}.md").write_text(
            fragment,
            encoding="utf-8",
        )


def _build_and_persist_fingerprint(vault: Path) -> None:
    """Build and save the voice fingerprint for *vault* on disk.

    Uses the same production API ``creek report --type fingerprint`` drives,
    so ``voice-check`` can later load the persisted artefact.

    Args:
        vault: Vault root holding the self-authored fragments.
    """
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.fingerprint import (
        build_fingerprint,
        save_fingerprint,
    )

    config = AIStyleConfig()
    fingerprint = build_fingerprint(vault, config)
    save_fingerprint(fingerprint, vault, config)


def test_voice_check_help_advertises_options() -> None:
    """``creek voice-check --help`` documents its flags, ANSI/width-robust."""
    # Render wide and strip ANSI so the rich help table cannot wrap or
    # colour-split the option tokens (LESSON from #519/#529).
    result = runner.invoke(app, ["voice-check", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "--max-distance" in normalized
    assert "--vault" in normalized
    assert "--json" in normalized


def test_voice_check_in_voice_file_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-voice file scores below threshold and exits 0."""
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "in_voice.md"
    target.write_text(
        (_voice_fixture_dir("in_voice") / "01_morning.md").read_text(
            encoding="utf-8",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "voice_distance" in plain.lower()


def test_voice_check_slop_file_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AI-slop file diverges past threshold and exits non-zero."""
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "slop.md"
    target.write_text(
        (_voice_fixture_dir("slop") / "03_wiki_padding.md").read_text(
            encoding="utf-8",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault), "--max-distance", "0.1"],
    )

    assert result.exit_code != 0, result.output


def test_voice_check_missing_fingerprint_is_graceful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fingerprint and no eligible fragments → clear notice, no traceback."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "anything.md"
    target.write_text("Some text to check.\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault)],
    )

    # fail-open: an un-profiled vault must not block (exit 0, not 1/2).
    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output).lower()
    assert "fingerprint" in plain
    assert "report --type fingerprint" in plain


def test_voice_check_missing_file_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-existent target file exits 2 with a clear error."""
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    result = runner.invoke(
        app,
        ["voice-check", str(tmp_path / "nope.md"), "--vault", str(vault)],
    )

    assert result.exit_code == 2
    assert "not found" in _strip_ansi(result.output).lower()


def test_voice_check_json_flag_emits_machine_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` emits a parseable object carrying voice_distance + verdict."""
    import json

    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "in_voice.md"
    target.write_text(
        (_voice_fixture_dir("in_voice") / "02_boat.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(_strip_ansi(result.output))
    assert "voice_distance" in payload
    assert payload["in_voice"] is True
    assert payload["max_distance"] == pytest.approx(0.35)


def test_voice_check_json_survives_square_brackets_in_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` stays valid JSON when a finding message contains ``[...]``.

    Rich treats ``[...]`` as console markup; emitting the JSON through Rich
    would mangle a finding message such as ``"saw [link] markup"``. The
    output must be raw so ``json.loads`` round-trips the brackets intact.
    """
    import json

    from creek.generate.ai_style.model import Finding, ScanReport, Span

    bracketed = "over-used [citation] and [link] markup tells"
    crafted = ScanReport(
        findings=[
            Finding(
                tell_id="t1",
                category="rhetorical",
                feature_key="brackets",
                span=Span(0, 0),
                line=3,
                excerpt="",
                draft_rate=1.0,
                user_rate=0.0,
                direction="over",
                message=bracketed,
            ),
        ],
        deltas={},
        voice_distance=0.99,
        thin_fingerprint=False,
    )
    monkeypatch.setattr(
        "creek.generate.ai_style.scanner.scan",
        lambda *_args, **_kwargs: crafted,
    )

    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "bracketed.md"
    target.write_text("body with [brackets] in it\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault), "--json"],
    )

    # Diverging file exits 1, but the JSON body must still parse cleanly.
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["findings"][0]["message"] == bracketed


def _write_voice_distance_config(vault: Path, upper: float) -> Path:
    """Write a vault ``creek_config.yaml`` overriding ``voice_distance_upper``.

    Args:
        vault: Vault root whose ``00-Creek-Meta`` config dir receives the file.
        upper: Custom ``ai_style.voice_distance_upper`` ceiling to persist.

    Returns:
        Path to the written ``creek_config.yaml``.
    """
    config_dir = vault / "00-Creek-Meta"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "creek_config.yaml"
    config_path.write_text(
        f"ai_style:\n  voice_distance_upper: {upper}\n",
        encoding="utf-8",
    )
    return config_path


def test_voice_check_default_threshold_honors_vault_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--max-distance``, the vault's ``voice_distance_upper`` wins.

    A vault config pins ``voice_distance_upper`` to 0.10 — far below the 0.35
    default. A file whose distance sits between the two (~0.29) passes under
    the default but must DIVERGE under the vault override. This pins that the
    effective threshold is resolved from the loaded vault config at call time,
    not frozen from the default config at module import.
    """
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    config_path = _write_voice_distance_config(vault, 0.10)
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))

    # ~0.29 distance: comfortably above the 0.10 override, below the 0.35 default.
    target = tmp_path / "padded.md"
    target.write_text(
        (_voice_fixture_dir("slop") / "03_wiki_padding.md").read_text(
            encoding="utf-8",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault)],
    )

    assert result.exit_code != 0, result.output
    plain = _strip_ansi(result.output)
    # The summary reports the override (0.1000), not the 0.35 default.
    assert "0.1000" in plain
    assert "0.3500" not in plain


def test_voice_check_explicit_max_distance_overrides_vault_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``--max-distance`` beats the vault's configured ceiling.

    The vault pins ``voice_distance_upper`` to 0.10, but passing
    ``--max-distance 0.5`` must take precedence so the ~0.29 file passes.
    """
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)
    _build_and_persist_fingerprint(vault)
    config_path = _write_voice_distance_config(vault, 0.10)
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))

    target = tmp_path / "padded.md"
    target.write_text(
        (_voice_fixture_dir("slop") / "03_wiki_padding.md").read_text(
            encoding="utf-8",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault), "--max-distance", "0.5"],
    )

    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "0.5000" in plain


def test_voice_check_builds_fingerprint_when_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no persisted artefact, voice-check builds from vault fragments."""
    vault = tmp_path / "vault"
    _scaffold_voice_vault(vault)  # fragments present, but fingerprint NOT saved
    monkeypatch.delenv("CREEK_CONFIG", raising=False)

    target = tmp_path / "in_voice.md"
    target.write_text(
        (_voice_fixture_dir("in_voice") / "05_shed.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["voice-check", str(target), "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "voice_distance" in _strip_ansi(result.output).lower()


# ---- ingest fail-loud on 0 fragments (#595) ----


class _ZeroFragmentIngestor:
    """Fake ingestor: discovers inputs but parses nothing (#595)."""

    def ingest(self, _source_path: Path) -> IngestResult:
        """Return a result with inputs discovered but no fragments."""
        return IngestResult(discovered=2, fragments=[], errors=[])


class _NoInputIngestor:
    """Fake ingestor: nothing discovered at all."""

    def ingest(self, _source_path: Path) -> IngestResult:
        """Return an empty result with zero discovered inputs."""
        return IngestResult(discovered=0, fragments=[], errors=[])


def _scaffold_min_vault(tmp_path: Path) -> Path:
    """Create the minimal vault dirs VaultWriter requires."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    return vault


def test_ingest_warns_when_inputs_found_but_zero_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inputs discovered but 0 fragments → loud warning, exit 0 by default (#595)."""
    vault = _scaffold_min_vault(tmp_path)
    src = tmp_path / "in"
    src.mkdir()
    monkeypatch.setattr("creek.cli._resolve_ingestor", lambda _t: _ZeroFragmentIngestor)

    result = runner.invoke(
        app,
        ["ingest", "--type", "fake", "--input", str(src), "--vault", str(vault), "-y"],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output
    assert "0 fragments" in result.output


def test_ingest_strict_exits_nonzero_on_zero_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--strict makes the discovered-but-0-fragments case exit non-zero (#595)."""
    vault = _scaffold_min_vault(tmp_path)
    src = tmp_path / "in"
    src.mkdir()
    monkeypatch.setattr("creek.cli._resolve_ingestor", lambda _t: _ZeroFragmentIngestor)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "fake",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "-y",
            "--strict",
        ],
    )

    assert result.exit_code == 1
    assert "WARNING" in result.output


def test_ingest_no_warning_when_no_inputs_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing discovered → no false warning, exit 0 even under --strict (#595)."""
    vault = _scaffold_min_vault(tmp_path)
    src = tmp_path / "in"
    src.mkdir()
    monkeypatch.setattr("creek.cli._resolve_ingestor", lambda _t: _NoInputIngestor)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "fake",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "-y",
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.output
