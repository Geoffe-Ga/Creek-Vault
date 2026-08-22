"""Tests for creek CLI module."""

from __future__ import annotations

import re
from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app, console
from creek.ingest.base import IngestResult
from creek.ingest.gdrive import DownloadResult, DriveFile

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Must match ``_TEST_TERMINAL_COLUMNS`` in ``tests/conftest.py``.
_PINNED_TERMINAL_COLUMNS = 200


def _strip_ansi(text: str) -> str:
    """Return *text* with ANSI escape sequences removed.

    Typer/Click's Rich formatter splits long option names like
    ``--bypass-compiled`` across colour-styled segments when CI's
    terminal supports ANSI; the literal substring then disappears
    from ``result.output``. Stripping ANSI before substring assertions
    keeps the tests environment-independent.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


def test_cli_console_width_is_pinned_at_import_time() -> None:
    """creek's console must not freeze at 80 columns in an xdist worker (#1141).

    Rich caches ``COLUMNS`` into ``Console._width`` at construction and then
    returns that cached value from every ``size`` lookup, so a console built
    while ``COLUMNS=80`` stays 80 columns wide no matter what a fixture sets
    afterwards. :mod:`creek.cli` builds its console at import — i.e. during
    collection — and on Linux every pytest-xdist worker inherits
    ``COLUMNS=80`` from readline's C-level ``putenv``. The result was Rich
    hard-wrapping CLI output mid-phrase and splitting the substrings the CLI
    tests assert on.

    ``tests/conftest.py`` closes the window by pinning the width in
    ``pytest_configure``, which runs before collection. This asserts that the
    pin actually reached the console, rather than trusting the ordering.
    """
    assert console.width == _PINNED_TERMINAL_COLUMNS


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


def _process_argv(source: Path, vault: Path) -> list[str]:
    """Return the ``creek process`` argv the #1303 tests share.

    Args:
        source: Source directory to ingest.
        vault: Vault root to write into.

    Returns:
        Argv for ``CliRunner.invoke``.
    """
    return [
        "process",
        "--source",
        str(source),
        "--vault",
        str(vault),
        "--yes",
        "--no-llm",
    ]


def test_process_summary_reports_persistence_not_a_bare_link_count(
    tmp_path: Path,
) -> None:
    """``creek process`` must not print an undifferentiated ``Links found``.

    That line was the operator-facing face of #1303: it summed four
    in-memory counts over a link graph the pipeline then discarded, two
    of which (resonance and temporal edges) nothing in this codebase can
    persist. It is replaced by the same per-stage phrasing ``creek link``
    uses, plus a total that counts pages written to disk.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# A note\n\nBody.\n", encoding="utf-8")
    vault = tmp_path / "vault"
    for name in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / name).mkdir(parents=True, exist_ok=True)

    result = runner.invoke(
        app,
        _process_argv(source, vault),
    )
    assert result.exit_code == 0, result.output
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "Links found:" not in normalized
    assert "Eddies linker:" in normalized
    assert "Threads linker:" in normalized
    assert "Link artefacts persisted: 0" in normalized, (
        "a corpus too small to cluster must report zero persisted artefacts, "
        "not a non-zero in-memory count"
    )


def test_process_into_unscaffolded_vault_reports_honest_zero(
    tmp_path: Path,
) -> None:
    """A half-set-up vault must degrade to zero, not crash (#1303).

    Stage 5 now writes to the vault, so it inherits ``VaultWriter``'s
    scaffold requirement and a new way to fail. Verified by execution
    rather than assumed: on a bare vault the fragment write earlier in
    the run lands nothing under ``01-Fragments/``, so ``_load_fragments``
    returns empty and ``run_link`` short-circuits before it ever reaches
    ``_materialise_link_models`` (whose own ``FileNotFoundError`` swallow
    is covered directly in ``tests/test_link_engine.py``). What this test
    pins is the end-to-end guarantee: exit 0, and an honest zero rather
    than a traceback or an invented count.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# A note\n\nBody.\n", encoding="utf-8")
    vault = tmp_path / "bare-vault"
    vault.mkdir()

    result = runner.invoke(
        app,
        _process_argv(source, vault),
    )
    assert result.exit_code == 0, result.output
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "Link artefacts persisted: 0" in normalized


def test_process_embedding_model_unavailable_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-load failure aborts ``creek process`` with the remediation.

    ``creek link`` has always handled this; ``creek process`` did not need
    to, because its link stage never loaded the model for real work. Since
    #1303 it does, so the same typed error is reachable here — and must
    print the remediation rather than a traceback.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# A note\n\nBody.\n", encoding="utf-8")
    vault = tmp_path / "vault"
    for name in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / name).mkdir(parents=True, exist_ok=True)

    def _raise(_model_name: str, _cache_folder: str | None) -> object:
        msg = "boom"
        raise OSError(msg)

    monkeypatch.setattr("creek.link.embeddings._load_sentence_transformer", _raise)

    result = runner.invoke(
        app,
        _process_argv(source, vault),
    )
    assert result.exit_code != 0
    normalized = " ".join(_strip_ansi(result.output).split())
    assert "Embedding linker aborted" in normalized
    assert "network access" in normalized


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


def test_ingest_command_extracts_hashtags_into_frontmatter_tags(
    tmp_path: Path,
) -> None:
    """``creek ingest`` writes body hashtags to ``tags`` (AC-1, issue #878).

    The bug: ``Fragment.tags`` defaulted to ``[]`` and *no* pipeline
    stage ever set it, so 2000/2000 sampled fragments of the operator's
    35,330-fragment vault read ``tags: []`` and
    ``00-Creek-Meta/Tag-Garden.md`` read ``*No tags found in vault.*``.
    Three consumers — the Tag Garden (``creek/generate/tags.py``), the
    orphan-tag lint (``creek/lint/checks/tags.py``) and the tag-driven
    branches of ``creek/generate/compost.py`` — were structurally
    unreachable.

    This is the acceptance criterion end to end: a real ``creek ingest``
    invocation over a real source note, asserted against the file that
    lands in the vault rather than against any in-process object. The
    expected value is an exact list, in body order, so the test cannot
    false-green on "some tags exist".
    """
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)

    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text(
        "# Hello\n\nFirst note about systems.\n\n#recovery #writing\n",
        encoding="utf-8",
    )

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
    assert len(written) == 1, written
    post = frontmatter.load(str(written[0]))
    assert post["tags"] == ["recovery", "writing"]


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

    The source lives **inside** the vault. Since #1575 an out-of-vault
    source records ``external/<digest>/<basename>``, which by design names
    nothing on disk, so the backfill can no longer reopen one and reports it
    through ``RefreshDatesResult.missing_source`` instead. That downgrade is
    pinned by ``test_refresh_dates_reports_an_out_of_vault_source_as_missing``
    in ``tests/test_ingest_source_path_privacy.py`` rather than left to be
    discovered, and restoring the out-of-vault half is tracked separately.
    What this test is *about* — that the backfill rewrites ``authored_at``
    without touching the body — is unchanged by where the source sits.
    """
    vault = tmp_path / "vault"
    for d in ["00-Creek-Meta", "01-Fragments/Notes"]:
        (vault / d).mkdir(parents=True)
    src = vault / "00-Inbox"
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


def test_classify_rejects_retired_aggregate_direction(tmp_path: Path) -> None:
    """``--reatomize-direction aggregate`` is refused, and says why (#1342).

    FEAT-022's zoom-out aggregator had zero production callers, so
    ``aggregate`` was a silent no-op: the flag was accepted, the run
    proceeded, and nothing aggregated. ADR-0011 retires the operator, and
    a retired value must fail loudly rather than keep pretending. The
    message has to name the retirement and the issue so an operator whose
    script still passes the flag learns where the decision lives instead
    of just seeing "unknown value".
    """
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
            "aggregate",
        ],
    )
    assert result.exit_code == 2, result.output
    # Rich styles and hard-wraps console output, so strip ANSI and collapse
    # whitespace before substring-asserting (same idiom as the help tests
    # above). Assert on wrapping-robust single tokens, never a phrase.
    normalised = re.sub(r"\s+", " ", _strip_ansi(result.output)).lower()
    assert "retired" in normalised
    assert "1342" in normalised


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


# ----- Issue #1337: the embeddings sentence must match the parquet -----------

_SEEDED_FRAGMENTS = 4
"""Fragment count for the #1337 seeded-vault fixtures.

Every expected number below is derived from this constant or read back off
the parquet, so growing the fixture can never turn a test vacuous.
"""

_CACHED_VECTORS_RE = re.compile(
    r"(\d+) vector\(s\) cached in 00-Creek-Meta/embeddings\.parquet",
)
"""Recovers the operator-facing cached-vector count from the CLI sentence."""

_EMBEDDING_CACHE_COLUMNS = {
    "fragment_id",
    "content_hash",
    "model_name",
    "embedding",
    "computed_at",
}
"""The parquet's entire schema — vectors and their freshness keys, no edges."""


class _FlatEncoder:
    """Deterministic stand-in for the sentence-transformer model.

    Row ``i`` is ``[1.0, 0.001 * i]``, so every pair of fragments sits well
    above the default 0.75 cosine threshold and the similarity-edge count is
    a stable ``C(n, 2)``. The shared conftest mock returns random
    384-dimension vectors instead, which are near-orthogonal — under it "0
    edges" is the usual answer, which is useless for a test about the edge
    clause. ``EmbeddingLinker.generate_embeddings`` wraps whatever ``encode``
    returns in ``numpy.asarray``, so a nested list is an exact stand-in for
    the real model's ndarray.
    """

    def encode(
        self,
        texts: str | list[str],
        **_kwargs: object,
    ) -> list[list[float]]:
        """Return one deterministic vector per entry in *texts*.

        Args:
            texts: A single text, or the batch the linker passes.
            **_kwargs: ``show_progress_bar`` / ``batch_size``; ignored.

        Returns:
            One ``[1.0, 0.001 * i]`` row per input text.
        """
        batch = [texts] if isinstance(texts, str) else list(texts)
        return [[1.0, 0.001 * index] for index, _text in enumerate(batch)]


def _seed_embeddings_vault(vault: Path, *, count: int) -> Path:
    """Seed *vault* with *count* fragments plus the meta directory.

    Args:
        vault: Vault root to create.
        count: Number of fragment files to write under ``01-Fragments/``.

    Returns:
        The embeddings parquet path — which does not exist yet, so a test
        can assert on its absence as well as its contents.
    """
    from creek.link.embeddings import embeddings_cache_path
    from creek.models import Fragment, FragmentSource, SourcePlatform
    from tests.helpers import write_fragment_file

    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    for index in range(count):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-1337vector{index:02d}",
                title=f"Vector fragment {index}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body=f"Body of vector fragment {index}.",
        )
    return embeddings_cache_path(vault)


def _run_link_embeddings(vault: Path) -> tuple[int, str]:
    """Run ``creek link --method embeddings`` under the deterministic encoder.

    Args:
        vault: Vault root to link.

    Returns:
        ``(exit_code, output)``, the output ANSI-stripped and
        whitespace-collapsed: the honest sentence is longer than the pinned
        200-column test terminal, so Rich wraps it mid-clause and raw
        substring assertions would fail for the wrong reason.
    """
    from unittest.mock import patch

    with patch(
        "creek.link.embeddings.EmbeddingLinker.load_model",
        return_value=_FlatEncoder(),
    ):
        result = runner.invoke(
            app,
            ["link", "--vault", str(vault), "--method", "embeddings"],
        )
    return result.exit_code, " ".join(_strip_ansi(result.output).split())


def test_link_embeddings_output_phrases_similarity_edges(tmp_path: Path) -> None:
    """Embeddings CLI output explicitly says "similarity edges", not "links".

    Issue #338 Problem B: the pre-fix output used "link(s)" for pairwise
    similarity edges that never reached fragment frontmatter. That
    implied a user-visible side effect that did not exist.

    Issue #1303 finished the job: the #338 wording said the *edges* were
    cached in the parquet, when in fact only the *vectors* are — the
    edges are computed and dropped, because no resonance writer exists.
    The output must therefore name both halves. The three vocabulary
    assertions below are unchanged from #338/#1303 on purpose: the
    accurate vocabulary has to survive the rewording, not be replaced by
    it. They read the whitespace-normalised output because the honest
    sentence no longer fits the pinned terminal width.

    Issue #1337 ties the sentence to the artifact, and the tie runs both
    ways. The vault is *seeded*, so a parquet genuinely exists (the
    pre-#1337 version of this test ran against an empty vault where no
    parquet was ever written, and still asserted the word
    "embeddings.parquet" appeared — it would have passed with the cache
    deleted from the codebase). The cached count printed to the operator
    is compared against ``num_rows`` on disk, derived not hardcoded, and
    the column set is asserted exactly. Add an edge column to the cache
    later and the schema assertion fires; claim cached edges in the
    sentence without adding that column and the wording assertion fires.
    Neither half can move without the other.
    """
    import pyarrow.parquet as pq

    vault = tmp_path / "vault"
    cache_path = _seed_embeddings_vault(vault, count=_SEEDED_FRAGMENTS)

    exit_code, output = _run_link_embeddings(vault)

    assert exit_code == 0, output
    # #338 phrasing — accurate to what the embeddings method does.
    assert "similarity edge" in output.lower()
    assert "embeddings.parquet" in output
    # #1303: and it must not claim the edges themselves were persisted.
    assert "not persisted" in output.lower()

    # #1337 tie, half one: the number the operator reads is the number of
    # rows the artifact actually holds.
    assert cache_path.exists(), output
    table = pq.read_table(cache_path)
    match = _CACHED_VECTORS_RE.search(output)
    assert match is not None, f"no cached-vector count in output: {output}"
    assert int(match.group(1)) == table.num_rows

    # #1337 tie, half two: the cache stores vectors and freshness keys and
    # nothing else. An added edge column fires here.
    assert set(table.column_names) == _EMBEDDING_CACHE_COLUMNS

    # Because there is no edge column, the sentence may not claim the edges
    # were cached — only the vectors were.
    assert "edge(s) cached" not in output
    assert "edges cached" not in output


def test_link_embeddings_warm_run_reports_zero_vectors_computed(
    tmp_path: Path,
) -> None:
    """A second identical run must report the vectors it did *not* compute.

    Issue #1337, lie one: the sentence read ``{fragment_count} fragment(s)
    embedded``, so a fully warm cache still claimed the whole corpus had
    been embedded even though ``LinkSummary.fragments_embedded`` was ``0``
    and the local model was never invoked. ``N fragment(s) embedded`` is
    the exact substring the defect printed, so its absence is asserted
    directly rather than inferred from the replacement wording.

    The cached count is still tied to ``num_rows``: a warm run rewrites the
    same rows, so the parquet must hold the whole corpus even though this
    run computed none of it.
    """
    import pyarrow.parquet as pq

    vault = tmp_path / "vault"
    cache_path = _seed_embeddings_vault(vault, count=_SEEDED_FRAGMENTS)

    cold_code, cold_output = _run_link_embeddings(vault)
    assert cold_code == 0, cold_output
    expected_cold = (
        f"{_SEEDED_FRAGMENTS} fragment(s) scanned, "
        f"{_SEEDED_FRAGMENTS} vector(s) computed,"
    )
    assert expected_cold in cold_output

    warm_code, warm_output = _run_link_embeddings(vault)
    assert warm_code == 0, warm_output
    expected_warm = f"{_SEEDED_FRAGMENTS} fragment(s) scanned, 0 vector(s) computed,"
    assert expected_warm in warm_output
    assert f"{_SEEDED_FRAGMENTS} fragment(s) embedded" not in warm_output

    table = pq.read_table(cache_path)
    assert table.num_rows == _SEEDED_FRAGMENTS
    match = _CACHED_VECTORS_RE.search(warm_output)
    assert match is not None, f"no cached-vector count in output: {warm_output}"
    assert int(match.group(1)) == table.num_rows


def test_link_embeddings_empty_vault_admits_nothing_was_cached(
    tmp_path: Path,
) -> None:
    """An empty vault must not claim vectors reached the parquet.

    Issue #1337, lie two: ``run_link`` returns early for a fragmentless
    vault, so ``_persist_cache`` never runs and no parquet is created — yet
    the sentence still ended "vectors cached in
    00-Creek-Meta/embeddings.parquet". The disk assertion and the wording
    assertion live in the same test on purpose: separated, one could be
    weakened without the other noticing, which is exactly how the claim
    and the artifact drifted apart in the first place.
    """
    from creek.link.embeddings import embeddings_cache_path

    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    cache_path = embeddings_cache_path(vault)

    exit_code, output = _run_link_embeddings(vault)

    assert exit_code == 0, output
    assert not cache_path.exists()
    assert "no vectors written to 00-Creek-Meta/embeddings.parquet" in output
    assert "vector(s) cached in" not in output
    assert "0 fragment(s) scanned, 0 vector(s) computed," in output


def test_link_embeddings_cache_write_failure_is_admitted(tmp_path: Path) -> None:
    """A swallowed cache-write error must reach the operator as a zero count.

    Issue #1337, lie three: ``_persist_cache`` catches ``OSError`` (disk
    full, read-only volume) and logs a warning, so linking exits 0 with no
    parquet on disk while the sentence claimed the vectors were cached.
    Graceful degradation is deliberate and stays — losing the cache only
    costs a recompute — so the exit code is pinned at 0. The fix is not to
    fail the run but to stop lying about it: the honest count *is* the
    remedy for the swallowed error, making it visible to the operator
    instead of only to the log.

    The run's real work is asserted too, so an implementation that reported
    ``0 vector(s) computed`` alongside the failed write — conflating
    "computed" with "persisted" — would fail here.
    """
    from unittest.mock import patch

    vault = tmp_path / "vault"
    cache_path = _seed_embeddings_vault(vault, count=_SEEDED_FRAGMENTS)

    with patch(
        "creek.link.embeddings.EmbeddingLinker.save_cache",
        side_effect=OSError("disk full"),
    ):
        exit_code, output = _run_link_embeddings(vault)

    assert exit_code == 0, output
    assert not cache_path.exists()
    assert "no vectors written to 00-Creek-Meta/embeddings.parquet" in output
    assert "vector(s) cached in" not in output
    expected_work = (
        f"{_SEEDED_FRAGMENTS} fragment(s) scanned, "
        f"{_SEEDED_FRAGMENTS} vector(s) computed,"
    )
    assert expected_work in output


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


def test_link_summary_reports_cluster_health() -> None:
    """Issue #880: the largest cluster, splits and discards are operator-visible.

    A single eddy holding 87% of the vault used to be invisible from the
    CLI — the summary reported how many eddies were detected but never how
    big any of them was. Discarded fragments carry no wiki-link at all, so
    that count is named explicitly rather than folded into a total.
    """
    from creek.cli import _format_link_summary
    from creek.link.link_engine import LinkSummary

    rendered = _format_link_summary(
        LinkSummary(
            method="eddies",
            fragment_count=700,
            link_count=3,
            eddies_detected=3,
            eddies_written=3,
            member_fragments_updated=36,
            largest_cluster_fragments=12,
            clusters_split=6,
            oversized_discarded=521,
        ),
    )
    assert "largest cluster: 12 fragment(s)" in rendered
    assert "6 oversized cluster(s) re-clustered" in rendered
    assert "521 fragment(s) discarded as unsplittable" in rendered


def test_link_summary_stays_terse_for_a_healthy_run() -> None:
    """No splits and no discards means no extra clauses beyond the largest."""
    from creek.cli import _format_link_summary
    from creek.link.link_engine import LinkSummary

    rendered = _format_link_summary(
        LinkSummary(
            method="threads",
            fragment_count=40,
            link_count=2,
            threads_detected=2,
            threads_written=2,
            member_fragments_updated=8,
            largest_cluster_fragments=5,
        ),
    )
    assert "largest cluster: 5 fragment(s)" in rendered
    assert "re-clustered" not in rendered
    assert "discarded" not in rendered


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
    result = runner.invoke(app, ["report", "--type", "paradoxx", "--vault", str(vault)])
    assert result.exit_code == 2, result.output  # Unix invalid-argument convention
    assert "paradoxx" in result.output  # names the bad type
    assert "decisions" in result.output  # lists real valid types
    assert "unnamed" in result.output
    assert "Would report" not in result.output  # the silent no-op is gone


def test_report_missing_type_errors(tmp_path: Path) -> None:
    """`report` with no --type errors with a human-readable message (#716)."""
    vault = _report_vault(tmp_path)
    result = runner.invoke(app, ["report", "--vault", str(vault)])
    assert result.exit_code == 2, result.output
    assert "--type is required" in result.output  # not a leaked Python "None"
    assert "None" not in result.output
    assert "Would report" not in result.output


def test_report_known_type_still_dispatches(tmp_path: Path) -> None:
    """A valid --type still runs its handler (no regression) (#716)."""
    vault = _report_vault(tmp_path)
    (vault / "10-Liminal" / "Unnamed").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["report", "--type", "unnamed", "--vault", str(vault)])
    assert result.exit_code == 0, result.output


def test_report_paradox_is_wired(tmp_path: Path) -> None:
    """`report --type paradox` runs the generator, not the unknown-type error (#711)."""
    vault = _report_vault(tmp_path)  # note-writer mkdirs its own folder
    result = runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Unknown report type" not in result.output  # now a real handler
    assert "no paradox notes generated" in result.output.lower()  # empty vault path


def test_report_synchronicity_is_wired(tmp_path: Path) -> None:
    """`report --type synchronicity` runs the generator, not the error (#711)."""
    vault = _report_vault(tmp_path)
    result = runner.invoke(
        app, ["report", "--type", "synchronicity", "--vault", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "Unknown report type" not in result.output
    assert "no synchronicity notes generated" in result.output.lower()


def test_report_paradox_writes_notes(tmp_path: Path) -> None:
    """`report --type paradox` writes notes + reports success when found (#711)."""
    from creek.models import (
        Confidence,
        Fragment,
        FragmentSource,
        SourcePlatform,
        VoiceClassification,
    )
    from tests.helpers import write_fragment_file

    vault = _report_vault(tmp_path)
    for fid, conf in (
        ("frag-cli-parax-aa", Confidence.MUSING),
        ("frag-cli-parax-bb", Confidence.SETTLED),
    ):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid,
                title="career ambitions",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
                voice=VoiceClassification(confidence=conf),
                threads=["thread-career"],
            ),
            body="a contradiction worth holding",
        )
    result = runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "paradox notes generated" in result.output.lower()  # success branch
    assert sorted((vault / "10-Liminal" / "Paradoxes").glob("*.md"))


def _seed_paradox_pair(vault: Path) -> None:
    """Write two fragments whose opposite confidence on one thread is a paradox."""
    from creek.models import (
        Confidence,
        Fragment,
        FragmentSource,
        SourcePlatform,
        VoiceClassification,
    )
    from tests.helpers import write_fragment_file

    for fid, conf in (
        ("frag-cli-dupe-aa", Confidence.MUSING),
        ("frag-cli-dupe-bb", Confidence.SETTLED),
    ):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid,
                title="career ambitions",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
                voice=VoiceClassification(confidence=conf),
                threads=["thread-career"],
            ),
            body="a contradiction worth holding",
        )


def test_report_paradox_rerun_writes_no_second_copy(tmp_path: Path) -> None:
    """A second `report --type paradox` run records nothing new (#1320).

    The CLI-level pin on the fix: the empty-result line must not claim no
    contradictory pair was found when one was found and already recorded.
    """
    vault = _report_vault(tmp_path)
    _seed_paradox_pair(vault)
    runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])

    result = runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "already recorded" in result.output
    assert len(list((vault / "10-Liminal" / "Paradoxes").glob("*.md"))) == 1


def test_report_paradox_reports_pre_existing_duplicates(tmp_path: Path) -> None:
    """Pre-#1320 duplicate copies are named on the console and left on disk."""
    import frontmatter

    vault = _report_vault(tmp_path)
    _seed_paradox_pair(vault)
    runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])
    (original,) = (vault / "10-Liminal" / "Paradoxes").glob("*.md")
    stray = original.with_name("2020-01-01-frag-cli-dupe-aa-frag-cli-dupe-bb.md")
    post = frontmatter.loads(original.read_text(encoding="utf-8"))
    post["detected_date"] = "2020-01-01"
    stray.write_text(frontmatter.dumps(post), encoding="utf-8")

    result = runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "#1320" in result.output
    assert "Nothing is deleted automatically" in result.output
    assert stray.exists()
    assert original.exists()


def test_report_paradox_is_quiet_when_no_duplicates(tmp_path: Path) -> None:
    """An operator must not be accused of duplicates on every clean run."""
    vault = _report_vault(tmp_path)
    _seed_paradox_pair(vault)
    runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])

    result = runner.invoke(app, ["report", "--type", "paradox", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "#1320" not in result.output


def test_report_synchronicity_writes_notes(tmp_path: Path) -> None:
    """`report --type synchronicity` writes notes + reports success (#711, #726).

    Mirrors the paradox success-path test: a cross-source, >30-day pair with an
    identical crafted embedding cache (cosine 1.0 > the 0.9 threshold) yields one
    synchronicity note and the bold-green success message.
    """
    from datetime import UTC, datetime

    from creek.config import EmbeddingsConfig
    from creek.link.embeddings import (
        CachedEmbedding,
        EmbeddingLinker,
        embeddings_cache_path,
    )
    from creek.models import Fragment, FragmentSource, SourcePlatform
    from tests.helpers import write_fragment_file

    vault = _report_vault(tmp_path)
    pairs = (
        ("frag-synx-cli-aa", SourcePlatform.DISCORD, datetime(2025, 1, 5, tzinfo=UTC)),
        ("frag-synx-cli-bb", SourcePlatform.JOURNAL, datetime(2025, 4, 20, tzinfo=UTC)),
    )
    for fid, platform, created in pairs:
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid,
                title="the river remembers every stone it has touched",
                source=FragmentSource(platform=platform),
                created=created,
                authored_at=created,  # the gap filter reads effective_authored_at
            ),
            body="a near-identical meaning arriving from a different source",
        )
    # Identical vectors → cosine 1.0 > the 0.9 synchronicity threshold.
    config = EmbeddingsConfig()
    now = datetime.now(tz=UTC)
    entries = {
        fid: CachedEmbedding(
            fragment_id=fid,
            content_hash="h",
            model_name=config.model,
            vector=[1.0, 0.0, 0.0, 0.0],
            computed_at=now,
        )
        for fid, _platform, _created in pairs
    }
    EmbeddingLinker(config).save_cache(entries, embeddings_cache_path(vault))

    result = runner.invoke(
        app, ["report", "--type", "synchronicity", "--vault", str(vault)]
    )
    assert result.exit_code == 0, result.output
    assert "synchronicity notes generated" in result.output.lower()  # success branch
    assert sorted((vault / "10-Liminal" / "Synchronicities").glob("*.md"))


def test_report_decisions_no_candidates_is_friendly(tmp_path: Path) -> None:
    """``report --type decisions`` with no signalling fragments is friendly (#581).

    The handler is now real (not the #579 stub): an empty corpus prints the
    "no new decision candidates" message, writes nothing, and never emits the
    old "Would generate" stub text.

    This is also the ``withheld == 0`` arm of #1487, and the two assertions
    added for it are what keep the withheld notice *conditional*. The notice
    must not appear — on stdout or stderr — for a vault where nothing was
    refused, and the empty-vault tail must keep saying "no new decision
    candidates found" rather than being flattened into the ``withheld > 0``
    wording. ``result.output`` is deliberate for the negative: it folds stderr
    in under click 8.4, so it also proves no spurious ``logger.warning`` fired.
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
    assert "withheld" not in result.output, (
        "#1487: nothing was refused on this vault, so the withheld notice "
        f"must not print at all. output={result.output!r}"
    )
    assert "nonewdecisioncandidatesfound." in _squashed(result.stdout), (
        "#1487: the withheld == 0 tail must stay byte-identical; only the "
        f"withheld > 0 arm gets new wording. stdout={result.stdout!r}"
    )


def test_report_decisions_generates_note(tmp_path: Path) -> None:
    """``report --type decisions`` writes a Decision note for a signalling fragment."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True)
    # ``privacy_tier: open`` is stated rather than omitted (#1431). The screen
    # ``generate_decisions`` now applies reads the *raw* front matter and fails
    # closed, so a note with no ``privacy_tier:`` key ranks intimate and is
    # withheld. Omitting the key here was never realistic vault state:
    # ``VaultWriter._write_model`` (creek/vault/writer.py:1385) serialises
    # ``model.model_dump(mode="json")``, so every pipeline-written fragment
    # carries the key explicitly. The keyless case is not swept under the rug —
    # ``test_report_decisions_withholds_a_fragment_with_no_privacy_tier_key``
    # below pins it.
    (frags / "frag-decide99.md").write_text(
        '---\ntype: fragment\nid: frag-decide99\ntitle: "Should I move to the coast"\n'
        "privacy_tier: open\n"
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


def test_report_decisions_withholds_a_fragment_with_no_privacy_tier_key(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A keyless fragment is withheld, and the withholding is announced (#1431).

    The decisions screen reads
    :func:`creek.classify.privacy_filter.raw_privacy_tier`, which fails closed:
    a note whose front matter has no ``privacy_tier:`` key ranks ``intimate``
    rather than the model's ``unclassified``. That is the same answer
    ``tests/test_mcp_report_tier_ceiling.py``'s
    ``test_fragment_with_no_privacy_tier_key_fails_closed_to_intimate`` already
    pins for report artifacts, and picking the model reader instead would leave
    the #1431 filename leak reproducible for exactly this fragment shape.

    The consequence is deliberately made loud rather than silent. ``creek
    report`` otherwise prints "no new decision candidates found", which for a
    hand-written or legacy vault would be a false statement produced by the
    fix; the withheld count is what distinguishes "nothing to report" from
    "something was refused". Only the count is disclosed — never an id and
    never a title, since the title is the leaking value.

    Since #1487 that count reaches the operator on **stdout** as well as the
    log, and the two strings come from one
    :func:`creek.generate.decisions.withheld_notice` so they cannot drift.
    This test keeps pinning the log half — ``creek`` is a library as well as a
    CLI, and the warning is what a non-CLI embedder sees. The stdout half is
    pinned by
    ``test_report_decisions_announces_the_withheld_count_on_stdout`` below.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True)
    (frags / "frag-notier.md").write_text(
        '---\ntype: fragment\nid: frag-notier\ntitle: "Should I move to the coast"\n'
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="creek.generate.decisions"):
        result = runner.invoke(
            app,
            ["report", "--type", "decisions", "--vault", str(vault)],
        )

    assert result.exit_code == 0, result.output
    assert "No decision notes generated" in result.output
    assert not list((vault / "08-Decisions").rglob("*.md"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("withheld" in r.getMessage() for r in warnings), (
        "the fail-closed screen dropped the only candidate without saying so; "
        f"records={[r.getMessage() for r in caplog.records]}"
    )
    assert any("1" in r.getMessage() for r in warnings), (
        "the warning must carry the withheld count so an operator can tell "
        "'nothing to report' from 'something was refused'."
    )
    assert not any("coast" in r.getMessage() for r in caplog.records), (
        "the warning must carry a count only — logging the title would move "
        "the leak from the filename to the log."
    )


# ---------------------------------------------------------------------------
# #1431 — the intimate title must not reach stdout either
# ---------------------------------------------------------------------------

_INTIMATE_DECISION_CANARY = "Jane-Doe-of-Springfield-Illinois"
"""Sentinel carried in an intimate fragment's title.

Restricted to ``[A-Za-z0-9-]`` because
``creek.generate.decisions._sanitize_title`` strips everything else out of the
filename it derives from the title, and the filename is the surface under test.
"""


def _seed_intimate_decision_vault(tmp_path: Path, *, with_open: bool = True) -> Path:
    """Build a vault whose only decision candidate is an ``intimate`` fragment.

    A second, ``open`` fragment rides along as the positive control: without it
    a "the canary is absent" assertion is satisfied by a command that printed
    nothing at all. ``with_open=False`` drops that control deliberately, for
    the #1487 tests that need a vault where *every* fragment is refused and the
    report therefore takes its empty branch — the arm where the headline says
    "no new candidates among the fragments this report could read".

    Args:
        tmp_path: Pytest temporary directory.
        with_open: Write the ``open`` positive-control fragment. Keyword-only
            and defaulting to ``True`` so the #1431 callers are unchanged.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True)
    (frags / "frag-intimate.md").write_text(
        "---\ntype: fragment\nid: frag-intimate\n"
        f'title: "Should I leave my marriage with {_INTIMATE_DECISION_CANARY}"\n'
        "privacy_tier: intimate\n"
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )
    if with_open:
        (frags / "frag-open.md").write_text(
            "---\ntype: fragment\nid: frag-open\n"
            'title: "Should I buy a bicycle"\n'
            "privacy_tier: open\n"
            "source:\n  platform: journal\n  author: self\n---\nbody\n",
            encoding="utf-8",
        )
    return vault


def _write_keyless_decision_fragment(vault: Path, frag_id: str, title: str) -> Path:
    """Write a hand-authored decision fragment with **no** ``privacy_tier:`` key.

    This shape cannot be produced by dumping a :class:`~creek.models.Fragment`
    — ``VaultWriter._write_model`` serialises ``model_dump(mode="json")`` and
    therefore always states the key — so it is written literally. It is the
    fragment shape the #1487 report actually meets in a hand-written or legacy
    vault: ``raw_privacy_tier`` fails closed to ``intimate``, the fragment is
    withheld, and before the fix the operator was told "no new decision
    candidates found".

    Args:
        vault: Vault root (must already exist).
        frag_id: Fragment id, also the filename stem.
        title: Fragment title. Must open with "Should I" or the detector never
            flags it.

    Returns:
        The path written.
    """
    frags = vault / "01-Fragments" / "Conversations"
    frags.mkdir(parents=True, exist_ok=True)
    path = frags / f"{frag_id}.md"
    path.write_text(
        f"---\ntype: fragment\nid: {frag_id}\n"
        f'title: "{title}"\n'
        "source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )
    return path


def _squashed(text: str) -> str:
    """Return *text* with ANSI escapes removed and every whitespace run deleted.

    Whitespace is *deleted*, not collapsed to single spaces, and that is the
    whole point. Rich hard-wraps the ``Decision notes generated (N): <path>``
    line at the console width, and it wraps mid-token: at 80 columns the
    pre-fix output is
    ``'…with-Jane-Doe-of-Sprin\\ngfield-Illinois.md'``. A plain
    ``canary not in result.output`` therefore passes while the title is printed
    in full — a vacuous assertion that would have declared #1431 fixed.

    Args:
        text: Captured CLI output.

    Returns:
        The output with ANSI stripped and all whitespace removed.
    """
    return "".join(_strip_ansi(text).split())


def test_report_decisions_never_echoes_an_intimate_title_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek report --type decisions`` must not print an intimate title (#1431).

    ``_report_decisions`` joins ``str(p.relative_to(vault_path))`` for every
    written path into a Rich message, so suppressing only the *file* while
    still echoing its name is not a fix — the title would reach the terminal,
    the scrollback and any captured build log regardless.

    With no ``--include-tier`` the report runs unfiltered
    (``PrivacyTierOverride.ALL``), which is the ceiling the defect lives at.

    The console width is pinned to 80 for the duration so the wrap hazard
    :func:`_squashed` guards against is actually exercised rather than merely
    described; ``tests/conftest.py`` otherwise pins it wide enough that the
    line fits and the guard would never be tested.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    vault = _seed_intimate_decision_vault(tmp_path)
    monkeypatch.setattr(console, "width", 80)

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    squashed = _squashed(result.output)
    assert "Should-I-buy-a-bicycle" in squashed, (
        "the open fragment's note was not announced, so the exclusion "
        f"assertion below is vacuous. output={result.output!r}"
    )
    assert _INTIMATE_DECISION_CANARY not in squashed, (
        "an intimate fragment's title was echoed to stdout by `creek report "
        f"--type decisions`. output={result.output!r}"
    )
    leaked = [
        str(p.relative_to(vault))
        for p in (vault / "08-Decisions").rglob("*")
        if _INTIMATE_DECISION_CANARY in str(p.relative_to(vault))
    ]
    assert not leaked, f"intimate title in an 08-Decisions filename: {leaked}"


def test_fill_report_decisions_step_never_echoes_an_intimate_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``creek fill`` ``report/decisions`` step is gated too (#1431).

    ``fill`` is the surface the issue was reported against, and it is the
    strictest one: ``_build_fill_steps`` states ``PrivacyTierOverride.ALL`` for
    every step because ``fill`` has no ``--include-tier`` of its own, so the
    ceiling gate admits everything and only the unconditional screen stands
    between an intimate title and the vault tree.

    The production ``_build_fill_steps`` plan is built and its real
    ``report/decisions`` lambda invoked, rather than driving the whole command:
    ``fill``'s first step instantiates a ``SentenceTransformer`` and reaches
    for model weights over the network. Nothing is stubbed in exchange — the
    callable invoked here is the production lambda with the production
    arguments.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.cli import _build_fill_steps, _load_config_for_vault

    vault = _seed_intimate_decision_vault(tmp_path)
    monkeypatch.setattr(console, "width", 80)

    steps = dict(
        _build_fill_steps(vault, _load_config_for_vault(vault), with_compost=False),
    )
    assert "report/decisions" in steps
    with console.capture() as captured:
        steps["report/decisions"]()

    squashed = _squashed(captured.get())
    assert "Should-I-buy-a-bicycle" in squashed, (
        "the open fragment's note was not announced by the fill step, so the "
        f"exclusion assertion below is vacuous. output={captured.get()!r}"
    )
    assert _INTIMATE_DECISION_CANARY not in squashed, (
        "the `creek fill` report/decisions step echoed an intimate fragment's "
        f"title. output={captured.get()!r}"
    )
    leaked = [
        str(p.relative_to(vault))
        for p in (vault / "08-Decisions").rglob("*")
        if _INTIMATE_DECISION_CANARY in str(p.relative_to(vault))
    ]
    assert not leaked, f"intimate title in an 08-Decisions filename: {leaked}"


# ---------------------------------------------------------------------------
# #1487 — the withheld count must reach the operator on stdout, in both branches
#
# Every positive assertion below targets ``result.stdout`` (or
# ``console.capture()``), never ``result.output``. Under click 8.4
# ``Result.output`` folds stderr in, so ``"withheld" in result.output`` is
# already True at HEAD purely from the ``logger.warning`` — a vacuous assertion
# that would declare #1487 fixed while the operator's terminal still says "no
# new decision candidates found". The negatives use ``result.output``, where
# folding stderr in makes the guard strictly stronger.
# ---------------------------------------------------------------------------

_WITHHELD_HEADLINE = (
    "No decision notes generated: no new candidates among the fragments "
    "this report could read."
)
"""Headline for the empty branch when something *was* refused (#1487).

The ``No decision notes generated`` prefix is byte-identical to the
``withheld == 0`` headline pinned above; only the tail differs, because saying
"no new decision candidates found" over a vault the report could not fully read
is the false statement this issue exists to remove.
"""

_WITHHELD_NOTICE_TAIL = "A tier already recorded as intimate is never lowered."
"""Closing sentence of the withheld notice (#1487).

Asserted alongside the count-bearing head so that a truncated or reworded
notice cannot satisfy the tests by printing its first clause alone. The exact
full wording is pinned once, in
``tests/test_decisions.py::test_withheld_notice_states_the_exact_remedy``.
"""


def _withheld_notice_head(count: int) -> str:
    """Return the count-bearing opening clause of the #1487 withheld notice.

    Args:
        count: The number of withheld fragments the notice must state.

    Returns:
        The literal (unsquashed) opening clause.
    """
    return f"{count} fragment(s) withheld from the decisions report"


def test_report_decisions_announces_the_withheld_count_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless-only vault says so on stdout, not just in the log (#1487).

    The reported defect, exactly: one hand-authored fragment with no
    ``privacy_tier:`` key, the fail-closed screen refuses it, and stdout says
    "no new decision candidates found" — a false statement, because there *was*
    a candidate and the report simply could not read it. The count reached
    ``logger.warning`` and died there.

    Kills three mutants:

    * deleting the ``console.print`` while leaving the ``logger.warning`` —
      the assertions read ``result.stdout``, which excludes the log;
    * ``if (notice := …) is not None:`` degraded to ``if False:``;
    * keeping the ``withheld == 0`` tail on the ``withheld > 0`` branch.

    Only the *count* may be disclosed: the canary assertion pins that the
    withheld fragment's title never appears, in the sanitiser-proof
    ``[A-Za-z0-9-]`` shape and through :func:`_squashed`, so an 80-column
    mid-token wrap cannot hide a leak.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    _write_keyless_decision_fragment(
        vault,
        "frag-notier",
        f"Should I leave my marriage with {_INTIMATE_DECISION_CANARY}",
    )
    monkeypatch.setattr(console, "width", 80)

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    squashed = _squashed(result.stdout)
    assert _squashed(_withheld_notice_head(1)) in squashed, (
        "#1487: the withheld count never reached stdout; the operator was "
        f"told nothing was found. stdout={result.stdout!r}"
    )
    assert _squashed(_WITHHELD_NOTICE_TAIL) in squashed, (
        "#1487: the notice was truncated — the operator got a count with no "
        f"remedy and no ratchet caveat. stdout={result.stdout!r}"
    )
    assert _squashed(_WITHHELD_HEADLINE) in squashed, (
        "#1487: the empty-branch headline must stop claiming 'no new decision "
        f"candidates found' when a fragment was refused. stdout={result.stdout!r}"
    )
    assert _INTIMATE_DECISION_CANARY not in _squashed(result.output), (
        "#1487: the notice must disclose a count and nothing else — naming "
        f"the fragment moves the leak, it does not fix it. output={result.output!r}"
    )
    assert not list((vault / "08-Decisions").rglob("*.md"))


def test_report_decisions_announces_the_withheld_count_alongside_written_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notice prints in the *non-empty* branch too (#1487).

    This is the half of the defect that survives a literal reading of AC1. On a
    mixed vault the report writes ``Should-I-buy-a-bicycle`` and, before the
    fix, says nothing whatsoever about the fragment it refused: the operator
    sees a green success line and has no way to know the report was partial.
    Fixing only the empty branch leaves this exactly as it was.

    Kills the mutant that prints the notice from inside the ``if not
    notes:`` early return — one exit path, two branches, one notice.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    vault = _seed_intimate_decision_vault(tmp_path)
    monkeypatch.setattr(console, "width", 80)

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    squashed = _squashed(result.stdout)
    assert "Decisionnotesgenerated" in squashed, (
        "the open fragment's note was not announced, so the withheld "
        "assertion below proves nothing about the success branch. "
        f"stdout={result.stdout!r}"
    )
    assert "Should-I-buy-a-bicycle" in squashed, (
        f"the admitted note was not named on stdout. stdout={result.stdout!r}"
    )
    assert _squashed(_withheld_notice_head(1)) in squashed, (
        "#1487: a partial report announced its successes and stayed silent "
        f"about its refusal. stdout={result.stdout!r}"
    )
    assert _squashed(_WITHHELD_NOTICE_TAIL) in squashed, (
        f"#1487: the notice was truncated. stdout={result.stdout!r}"
    )
    assert _INTIMATE_DECISION_CANARY not in _squashed(result.output), (
        "#1431/#1487: the withheld fragment's title reached the terminal. "
        f"output={result.output!r}"
    )


def test_report_decisions_withheld_notice_states_the_real_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two withheld fragments are reported as two, not as one (#1487).

    The count is interpolated, never spelled. This is the killer for a notice
    that hardcodes ``1`` — or that reports ``len(something_else)`` — and it is
    why the negative half matters as much as the positive: ``"2 fragment(s)"``
    present is satisfied by a notice printed twice, once per fragment, each
    claiming ``1``.

    The two withheld fragments are deliberately different shapes — one
    explicitly ``privacy_tier: intimate``, one keyless and failing closed —
    because the count must span both refusal reasons the notice names.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    vault = _seed_intimate_decision_vault(tmp_path, with_open=False)
    _write_keyless_decision_fragment(
        vault,
        "frag-notier",
        "Should I sell the house",
    )
    monkeypatch.setattr(console, "width", 80)

    result = runner.invoke(
        app,
        ["report", "--type", "decisions", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    squashed = _squashed(result.stdout)
    assert _squashed(_withheld_notice_head(2)) in squashed, (
        "#1487: two fragments were refused and the notice did not say two. "
        f"stdout={result.stdout!r}"
    )
    assert _squashed(_withheld_notice_head(1)) not in _squashed(result.output), (
        "#1487: the withheld count is hardcoded or per-fragment — the notice "
        f"claimed one refusal on a vault with two. output={result.output!r}"
    )
    assert _squashed(_WITHHELD_HEADLINE) in squashed, (
        "#1487: nothing could be read, so the headline must not claim there "
        f"were no candidates. stdout={result.stdout!r}"
    )
    assert not list((vault / "08-Decisions").rglob("*.md"))


def test_fill_report_decisions_step_announces_the_withheld_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``creek fill`` ``report/decisions`` step announces refusals too (#1487).

    ``fill`` is the unattended surface, and the one that states
    ``PrivacyTierOverride.ALL`` for every step because it has no
    ``--include-tier`` of its own — so it is the run most likely to refuse
    something and the run least likely to have anybody watching a log. The
    notice must reach the same console the step's other output goes to.

    Kills the mutant that puts the ``console.print`` in the ``creek report``
    command body rather than in the shared ``_report_decisions`` helper both
    surfaces call.

    The production ``_build_fill_steps`` plan is built and its real
    ``report/decisions`` lambda invoked, rather than driving the whole command:
    ``fill``'s first step instantiates a ``SentenceTransformer`` and reaches
    for model weights over the network.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.cli import _build_fill_steps, _load_config_for_vault

    vault = _seed_intimate_decision_vault(tmp_path)
    monkeypatch.setattr(console, "width", 80)

    steps = dict(
        _build_fill_steps(vault, _load_config_for_vault(vault), with_compost=False),
    )
    assert "report/decisions" in steps
    with console.capture() as captured:
        steps["report/decisions"]()

    squashed = _squashed(captured.get())
    assert "Should-I-buy-a-bicycle" in squashed, (
        "the open fragment's note was not announced by the fill step, so the "
        f"assertions below prove nothing. output={captured.get()!r}"
    )
    assert _squashed(_withheld_notice_head(1)) in squashed, (
        "#1487: the fill step refused a fragment without telling the console. "
        f"output={captured.get()!r}"
    )
    assert _squashed(_WITHHELD_NOTICE_TAIL) in squashed, (
        f"#1487: the notice was truncated. output={captured.get()!r}"
    )
    assert _INTIMATE_DECISION_CANARY not in squashed, (
        "#1431/#1487: the withheld fragment's title reached the console. "
        f"output={captured.get()!r}"
    )


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


def _seed_rhetorical_patterns_vault(vault: Path, *, tier_line: str) -> Path:
    """Seed *vault* with one exemplar-qualifying fragment for the patterns report.

    *tier_line* is spliced into the frontmatter verbatim, so a caller passes
    either ``"privacy_tier: open\\n"`` or ``""`` — the empty string being the
    only way to produce a file that never declared a tier at all, which is the
    state #1212 is about and which no model-serialising helper can express.

    Args:
        vault: Vault root to create.
        tier_line: The ``privacy_tier`` frontmatter line, or ``""`` to omit it.

    Returns:
        *vault*, for chaining.
    """
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    frags = vault / "01-Fragments" / "Journal"
    frags.mkdir(parents=True, exist_ok=True)
    (frags / "ex-1.md").write_text(
        '---\ntype: fragment\nid: ex-1\ntitle: "T"\n'
        f"{tier_line}"
        "source:\n  platform: journal\n  author: self\n"
        "voice:\n  voice_register: confessional\n  confidence: conviction\n"
        "---\nThe truth is we rise; as I said before, we rise.\n",
        encoding="utf-8",
    )
    return vault


def test_report_rhetorical_patterns_generates(tmp_path: Path) -> None:
    """``report --type rhetorical-patterns`` writes a per-register note (#582).

    The fixture states ``privacy_tier: open`` explicitly (#1212). It used to
    omit the key, which made this test depend on the very defect #1212 fixes:
    an untiered fragment reaching the voice corpus at the CLI's default
    ``ALL`` ceiling. Stating the tier keeps the test about *rhetorical-pattern
    generation* rather than about tier admission, which
    ``test_report_rhetorical_patterns_refuses_an_untiered_fragment`` owns.
    """
    vault = _seed_rhetorical_patterns_vault(
        tmp_path / "vault",
        tier_line="privacy_tier: open\n",
    )

    result = runner.invoke(
        app,
        ["report", "--type", "rhetorical-patterns", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert "Rhetorical patterns written" in result.output
    assert (vault / "07-Voice" / "Rhetorical-Patterns" / "confessional.md").exists()


def test_report_rhetorical_patterns_refuses_an_untiered_fragment(
    tmp_path: Path,
) -> None:
    """A fragment with no ``privacy_tier`` key produces no patterns (#1212 AC1).

    AC1 names ``rhetorical-patterns`` explicitly, and this is the surface as
    the operator meets it: a bare ``creek report --type rhetorical-patterns``,
    with no ``--include-tier``, which the CLI resolves to
    ``PrivacyTierOverride.ALL``. At that ceiling the raw-frontmatter gate
    short-circuits to "admit", leaving only the model-reading consent gate —
    which sees Pydantic's ``unclassified`` default and cannot tell that the
    file said nothing at all.

    Both arms run, over two vaults identical but for that one frontmatter
    line, because the negative arm alone is worthless: ``No rhetorical
    patterns written`` is also what a vault the walk never reached would
    print, and what a mis-typed fixture, a bad body, or an unqualifying
    confidence would print. The positive arm is what proves the refusal is
    about the tier.
    """
    untiered = _seed_rhetorical_patterns_vault(
        tmp_path / "untiered-vault",
        tier_line="",
    )
    tiered = _seed_rhetorical_patterns_vault(
        tmp_path / "tiered-vault",
        tier_line="privacy_tier: open\n",
    )

    refused = runner.invoke(
        app,
        ["report", "--type", "rhetorical-patterns", "--vault", str(untiered)],
    )
    admitted = runner.invoke(
        app,
        ["report", "--type", "rhetorical-patterns", "--vault", str(tiered)],
    )

    assert admitted.exit_code == 0, admitted.output
    assert "Rhetorical patterns written" in admitted.output, (
        "the tiered arm wrote nothing, so the untiered arm's silence says "
        f"nothing about the tier. output={admitted.output!r}"
    )
    assert (tiered / "07-Voice" / "Rhetorical-Patterns" / "confessional.md").is_file()

    assert refused.exit_code == 0, refused.output
    assert "No rhetorical patterns written" in refused.output
    assert not (untiered / "07-Voice" / "Rhetorical-Patterns").exists()


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


# ---- Issue #879: report --type voice must populate Register-Samples -------
#
# ``VoiceExemplarCollector.save_exemplars`` had no production caller, so
# ``07-Voice/Register-Samples/`` was only ever written by tests: a real
# vault got ``<register>-profile.md`` and an empty samples tree. These
# tests pin the CLI wiring end to end — the copies exist, they carry the
# source body rather than an empty one, both privacy gates still hold,
# and a re-run neither duplicates nor orphans.

_SAMPLES_SUBPATH = ("07-Voice", "Register-Samples")
"""Vault-relative location of the persisted register samples."""


def _voice_body(marker: str) -> str:
    """Return a distinctive multi-sentence exemplar body carrying *marker*."""
    return (
        f"{marker} the creek of thought flows gently downstream. "
        "It gathers what it passes and it carries the sediment on. "
    ) * 20


def _write_voice_fragment(
    vault: Path,
    frag_id: str,
    *,
    tier: str | None = "personal",
    register: str = "confessional",
    confidence: str = "conviction",
    body: str | None = None,
    title: str | None = None,
    file_stem: str | None = None,
) -> Path:
    """Write one exemplar-eligible fragment with hand-built frontmatter.

    Built from a literal template rather than ``Fragment.model_dump``
    (which always emits a ``privacy_tier``) so ``tier=None`` produces a
    file with the key genuinely **absent** — the legacy / hand-edited
    shape that ``raw_privacy_tier`` fails closed to ``intimate``, and the
    only shape that can tell a raw-frontmatter ceiling read apart from a
    model-defaulted one. Same reasoning as
    ``tests/test_cli_fill.py::_seed_fragment``.

    Args:
        vault: Vault root.
        frag_id: Fragment id (also the file stem, unless *file_stem* says
            otherwise).
        tier: ``privacy_tier`` value, or ``None`` to omit the key entirely.
        register: ``voice.voice_register`` value.
        confidence: ``voice.confidence`` value.
        body: Markdown body; defaults to a marked exemplar body.
        title: ``title`` value. Defaults to ``f"Fragment {frag_id}"``, which
            *contains* the id — so a test asserting that a title has been
            scrubbed must pass a title that shares no substring with the id,
            or it proves nothing the id assertion did not already prove.
        file_stem: Filename stem to write under, when it must differ from
            *frag_id*. The two diverge only for ids that cannot themselves
            be filenames (an over-long id would raise ``OSError`` here,
            before the code under test ever saw it).

    Returns:
        Path of the written fragment file.
    """
    folder = vault / "01-Fragments" / "Journal"
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = f"privacy_tier: {tier}\n" if tier is not None else ""
    resolved_title = f"Fragment {frag_id}" if title is None else title
    target = folder / f"{file_stem or frag_id}.md"
    target.write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{resolved_title}"\n'
        f"source:\n  platform: journal\n  author: self\n"
        f"frequency:\n  primary: F5\n"
        f"wavelength:\n  phase: rising\n  mode: express\n"
        f"voice:\n  voice_register: {register}\n  confidence: {confidence}\n"
        f"{tier_line}---\n{body if body is not None else _voice_body(frag_id)}\n",
        encoding="utf-8",
    )
    return target


def _samples_root(vault: Path) -> Path:
    """Return the ``07-Voice/Register-Samples`` root for *vault*."""
    return vault.joinpath(*_SAMPLES_SUBPATH)


def _sample_relpaths(vault: Path) -> list[str]:
    """Return every file under ``Register-Samples`` as sorted relative paths."""
    root = _samples_root(vault)
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _copied_names(vault: Path, register: str = "confessional") -> list[str]:
    """Return the sorted exemplar filenames in *register*, minus the summary."""
    register_dir = _samples_root(vault) / register
    if not register_dir.is_dir():
        return []
    return sorted(p.name for p in register_dir.glob("*.md") if p.name != "_Summary.md")


def _run_report_voice(vault: Path, *args: str) -> None:
    """Invoke ``creek report --type voice`` on *vault* and assert it exits 0."""
    result = runner.invoke(
        app,
        ["report", "--type", "voice", "--vault", str(vault), *args],
    )
    assert result.exit_code == 0, result.output


def _voice_vault(tmp_path: Path) -> Path:
    """Return a scaffolded, empty vault root ready for voice fragments."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    return vault


def test_report_voice_writes_register_samples(tmp_path: Path) -> None:
    """``report --type voice`` copies ranked exemplars into Register-Samples.

    The headline defect of #879: the command wrote
    ``07-Voice/confessional-profile.md`` and left
    ``07-Voice/Register-Samples/`` completely empty, because
    ``save_exemplars`` had no production caller at all.
    """
    vault = _voice_vault(tmp_path)
    for index in range(6):
        _write_voice_fragment(vault, f"frag-rs-{index}")

    _run_report_voice(vault)

    register_dir = _samples_root(vault) / "confessional"
    assert (register_dir / "_Summary.md").is_file()
    assert _copied_names(vault) == [f"frag-rs-{index}.md" for index in range(6)]


def test_report_voice_register_samples_carry_the_source_body(
    tmp_path: Path,
) -> None:
    """Each copied sample is the source file verbatim, never an empty body.

    ``_persist_fragment`` only copies the source when
    ``self._records[fragment.id]`` exists, and ``_records`` is populated
    **only** by ``collect_exemplars``. An implementation that builds a
    second collector for the save, or calls ``save_exemplars`` without a
    prior ``collect_exemplars`` on the same instance, silently falls back
    to ``frontmatter.Post(content="", ...)`` and writes body-less samples
    that pass every other assertion in this module. Byte equality is the
    assertion that catches it.
    """
    import frontmatter

    vault = _voice_vault(tmp_path)
    source = _write_voice_fragment(
        vault,
        "frag-body-1",
        body=_voice_body("unmistakable-exemplar-marker"),
    )

    _run_report_voice(vault)

    copy = _samples_root(vault) / "confessional" / "frag-body-1.md"
    assert copy.is_file()
    copied = frontmatter.load(str(copy))
    assert copied.content == frontmatter.load(str(source)).content
    assert copied.content.strip() != ""
    assert "unmistakable-exemplar-marker" in copied.content
    assert copy.read_bytes() == source.read_bytes()


def test_report_voice_register_samples_exclude_intimate_by_default(
    tmp_path: Path,
) -> None:
    """A bare run copies no intimate fragment, and names none in the summary.

    ``allow_intimate`` is the voice proxy's own consent gate and is not
    CLI-exposed, so ``intimate`` content must stay out of the samples tree
    even on an otherwise unfiltered run. The ``open`` control fragment is
    what stops this passing vacuously on an implementation that copies
    nothing at all.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-open-ctl", tier="open")
    _write_voice_fragment(vault, "frag-intimate", tier="intimate")

    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-open-ctl.md"]
    written = "\n".join(
        path.read_text(encoding="utf-8") for path in _samples_root(vault).rglob("*.md")
    )
    assert "frag-intimate" not in written


def test_report_voice_register_samples_honour_the_tier_ceiling(
    tmp_path: Path,
) -> None:
    """``--include-tier open`` admits only ``open`` fragments to the samples.

    On ``report`` the flag NARROWS (an absent flag means unfiltered), so
    the ``personal`` fragment sits one rank above the declared ceiling and
    must not be copied — while the ``open`` control proves the run copied
    anything at all.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-open", tier="open")
    _write_voice_fragment(vault, "frag-personal", tier="personal")

    _run_report_voice(vault, "--include-tier", "open")

    assert _copied_names(vault) == ["frag-open.md"]


def test_report_voice_register_samples_fail_closed_on_an_absent_tier(
    tmp_path: Path,
) -> None:
    """A fragment with no ``privacy_tier`` key at all is never copied.

    The ceiling is asked of the **raw** frontmatter
    (``privacy_filter.within_ceiling`` → ``raw_privacy_tier``), which
    fails closed to ``intimate`` when the key is absent — a hand-edited or
    legacy note carries less assurance than a pipeline-written one that
    says ``unclassified`` out loud.

    ``personal`` is the ceiling that makes this discriminating, and it is
    the only one that does. A raw read ranks the untiered file with
    ``intimate`` (rank 2) and excludes it; reading the tier through the
    validated ``Fragment`` instead would apply the model's
    ``unclassified`` default, which ranks with ``personal`` (rank 1,
    #876) and would let it straight through. At ``--include-tier open``
    both readings exclude it, so that ceiling proves nothing here. The
    file is therefore written with the key genuinely stripped rather than
    via ``model_dump``, which always emits one.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-personal-ctl", tier="personal")
    _write_voice_fragment(vault, "frag-untiered", tier=None)

    _run_report_voice(vault, "--include-tier", "personal")

    assert _copied_names(vault) == ["frag-personal-ctl.md"]


def test_report_voice_register_samples_are_idempotent(tmp_path: Path) -> None:
    """A second run writes the same set of files, and no more of them."""
    vault = _voice_vault(tmp_path)
    for index in range(3):
        _write_voice_fragment(vault, f"frag-idem-{index}")

    _run_report_voice(vault)
    first = _sample_relpaths(vault)
    assert first, "the first run wrote nothing into Register-Samples"

    _run_report_voice(vault)

    assert _sample_relpaths(vault) == first


def test_report_voice_prunes_samples_whose_fragment_is_gone(
    tmp_path: Path,
) -> None:
    """A sample whose source fragment left the corpus is removed on re-run.

    ``save_exemplars`` only ever ``mkdir``s and writes, so a fragment that
    drops out of the corpus leaves its copy behind forever — a deleted or
    purged fragment keeps a full-bodied copy of itself in the voice
    samples tree indefinitely.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-keep")
    dropped = _write_voice_fragment(vault, "frag-drop")

    _run_report_voice(vault)
    assert _copied_names(vault) == ["frag-drop.md", "frag-keep.md"]

    dropped.unlink()
    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-keep.md"]


def test_report_voice_prunes_a_sample_that_becomes_intimate(
    tmp_path: Path,
) -> None:
    """Re-tiering a fragment to ``intimate`` removes its existing sample.

    The privacy face of the same stale-copy bug: without pruning, a
    fragment reclassified upward keeps a verbatim copy of its body in
    ``Register-Samples`` that no gate will ever look at again.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-keep", tier="open")
    _write_voice_fragment(vault, "frag-secret", tier="open")

    _run_report_voice(vault)
    assert _copied_names(vault) == ["frag-keep.md", "frag-secret.md"]

    _write_voice_fragment(vault, "frag-secret", tier="intimate")
    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-keep.md"]
    written = "\n".join(
        path.read_text(encoding="utf-8") for path in _samples_root(vault).rglob("*.md")
    )
    assert "frag-secret" not in written


def test_report_voice_pruning_spares_operator_notes(tmp_path: Path) -> None:
    """Pruning removes stale samples only — never the operator's own files.

    ``Register-Samples/<register>/`` is a vault folder an operator can put
    notes in. A prune that clears anything it did not write turns a
    housekeeping pass into data loss, and one that clears ``_Summary.md``
    deletes the note it is about to rewrite.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-a")
    _write_voice_fragment(vault, "frag-b")

    _run_report_voice(vault)
    register_dir = _samples_root(vault) / "confessional"
    notes = register_dir / "My-Notes.md"
    notes.write_text("# My reading notes\n\nHand written.\n", encoding="utf-8")

    _run_report_voice(vault)

    assert notes.read_text(encoding="utf-8") == "# My reading notes\n\nHand written.\n"
    assert (register_dir / "_Summary.md").is_file()
    assert (register_dir / "frag-a.md").is_file()
    assert (register_dir / "frag-b.md").is_file()


# ---- Issue #879, second pass: security-review findings --------------------
#
# Five defects the first pass left behind. Three of them share a root
# cause: ``_prune_stale_copies`` decides "is this file mine to delete?"
# with ``_is_generated_sample`` — "parses as ``type: fragment`` and its
# ``id`` equals its own stem" — which is a *shape* test, not a record of
# authorship. It answers "no" for a file this tool wrote (once ``creek
# purge`` scrubs the id) and "yes" for a file it did not (an operator's
# curated fragment). The fix replaces the heuristic with a manifest kept
# in ``_Summary.md``; these tests are written against the behaviour, not
# the manifest's internals.


def _samples_text(vault: Path) -> str:
    """Return every byte of markdown currently under ``Register-Samples``.

    The residue assertions need to search the *whole* samples tree, not
    just the copies: the summary note is markdown too, and it is where
    the ids and titles of departed fragments were found to survive.
    """
    return "\n".join(
        path.read_text(encoding="utf-8") for path in _samples_root(vault).rglob("*.md")
    )


def _load_register_summary(
    vault: Path,
    register: str = "confessional",
) -> frontmatter.Post:
    """Load the ``_Summary.md`` note for *register*.

    Args:
        vault: Vault root.
        register: Canonical voice register whose summary note to read.

    Returns:
        The parsed summary note.
    """
    return frontmatter.load(str(_samples_root(vault) / register / "_Summary.md"))


def test_report_voice_rewrites_the_summary_of_an_emptied_register(
    tmp_path: Path,
) -> None:
    """A register that loses every exemplar must stop naming them.

    ``save_exemplars`` prunes and then ``continue``s on an empty bucket,
    *before* ``_write_summary`` — so the summary an earlier run wrote
    survives the emptying intact. It goes on claiming the old
    ``exemplar_count`` and goes on wikilinking the departed fragment by
    both id **and title**, which is the residue that matters: the body
    copy is pruned correctly, and the title is the part of a fragment
    most likely to be self-describing.

    ``test_report_voice_prunes_a_sample_that_becomes_intimate`` cannot
    catch this. Its register keeps another exemplar, so the summary is
    rewritten as a side effect of the register still being non-empty.
    Here the re-tiered fragment is the register's **sole** exemplar.

    The fix is to rewrite the summary, not to invent a new rendering:
    ``_render_summary_body`` already renders an empty cohort as
    ``_No exemplars collected._`` (pinned by
    ``tests/test_voice_exemplars.py::...::
    test_summary_body_lists_zero_exemplars_when_empty``). Nor may the fix
    start writing summaries for registers that never had one —
    ``tests/test_voice_exemplars.py::TestSaveExemplars::
    test_skips_empty_registers`` holds that line.
    """
    vault = _voice_vault(tmp_path)
    # A second register keeps the run itself non-empty, so this test cannot
    # pass by way of the command doing nothing at all.
    _write_voice_fragment(vault, "frag-ctl", tier="open")
    secret_title = "My affair with the neighbour"
    _write_voice_fragment(
        vault,
        "frag-secret",
        tier="open",
        register="analytical",
        title=secret_title,
    )

    _run_report_voice(vault)
    assert _copied_names(vault, "analytical") == ["frag-secret.md"]
    assert _load_register_summary(vault, "analytical")["exemplar_count"] == 1

    _write_voice_fragment(
        vault,
        "frag-secret",
        tier="intimate",
        register="analytical",
        title=secret_title,
    )
    _run_report_voice(vault)

    assert _copied_names(vault, "analytical") == []
    residue = _samples_text(vault)
    assert "frag-secret" not in residue
    assert secret_title not in residue
    emptied = _load_register_summary(vault, "analytical")
    assert emptied["exemplar_count"] == 0
    assert emptied["conviction_count"] == 0
    assert emptied["settled_count"] == 0
    assert "_No exemplars collected._" in emptied.content
    # The control register is untouched: the rewrite is scoped, not a wipe.
    assert _copied_names(vault) == ["frag-ctl.md"]


def test_report_voice_pruning_spares_a_curated_fragment_copy(
    tmp_path: Path,
) -> None:
    """A fragment the operator curated into the folder must survive a re-run.

    ``_is_generated_sample`` returns ``True`` for *any* fragment note whose
    ``id`` equals its stem — which is exactly the shape of a fragment an
    operator copies in to keep as a permanent stylistic exemplar. The
    first ``creek report --type voice`` (or ``creek fill``) after that
    deletes it, irrecoverably, with no prompt and no dry-run.

    ``test_report_voice_pruning_spares_operator_notes`` covers only the
    easy case: a note with no frontmatter at all, which no id-equals-stem
    check could ever claim. This one is a *real fragment file*, byte for
    byte, and the prune has no honest way to tell it from its own output
    — unless it stops guessing and consults a record of what it actually
    wrote.

    ``exploring`` confidence keeps the curated fragment out of the corpus,
    so it is never in ``keep_ids`` and the prune has to make a real
    decision about it.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-a")
    _write_voice_fragment(vault, "frag-b")

    _run_report_voice(vault)
    register_dir = _samples_root(vault) / "confessional"

    curated_source = _write_voice_fragment(
        vault,
        "frag-curated",
        confidence="exploring",
    )
    curated = register_dir / "frag-curated.md"
    curated.write_bytes(curated_source.read_bytes())

    _run_report_voice(vault)

    assert curated.is_file(), "the prune deleted a fragment the operator curated"
    assert curated.read_bytes() == curated_source.read_bytes()
    assert _copied_names(vault) == ["frag-a.md", "frag-b.md", "frag-curated.md"]


def test_report_voice_prunes_a_copy_whose_id_purge_has_scrubbed(
    tmp_path: Path,
) -> None:
    """``creek purge``'s id-scrub must not make a stale copy immortal.

    ``purge fragment`` deletes the source and then walks every ``.md`` in
    the vault rewriting the fragment's id to ``[purged]``. That walk
    reaches ``07-Voice/Register-Samples/<register>/<id>.md``, whose id is
    now the YAML list ``["purged"]`` — so ``Fragment.model_validate``
    rejects it, ``_is_generated_sample`` answers ``False``, and the prune
    that exists precisely to remove this file refuses to touch it. The
    verbatim body of a purged fragment then survives every subsequent
    run, permanently.

    Scope (do not widen): that ``purge`` does not itself reach
    ``07-Voice/`` synchronously is a **pre-existing** gap, verified at
    unmodified HEAD — ``purge`` already leaves a purged body in
    ``07-Voice/<register>-profile.md`` — and is tracked as issue #1211.
    This test pins only the part #879 owns: its own prune must not be
    disarmed by the scrub.
    """
    vault = _voice_vault(tmp_path)
    marker = "purge-residue-marker-9f2a"
    _write_voice_fragment(vault, "frag-purge-keep", body=_voice_body("keeper"))
    _write_voice_fragment(vault, "frag-purge-gone", body=_voice_body(marker))

    _run_report_voice(vault)
    assert _copied_names(vault) == ["frag-purge-gone.md", "frag-purge-keep.md"]

    purged = runner.invoke(
        app,
        ["purge", "fragment", "frag-purge-gone", "--vault", str(vault), "--yes"],
    )
    assert purged.exit_code == 0, purged.output

    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-purge-keep.md"]
    assert marker not in _samples_text(vault)


def test_report_voice_survives_an_unwritably_long_fragment_id(
    tmp_path: Path,
) -> None:
    """One unusable id costs one exemplar, not the whole command.

    ``_is_safe_sample_stem`` bounds an id's *content* but not its
    *length*, so a 300-character id passes the guard and reaches
    ``shutil.copy2``, which raises ``OSError`` (``[Errno 63] File name
    too long``; ``ENAMETOOLONG`` on every filesystem with a 255-byte
    ``NAME_MAX``). Nothing catches it, so ``creek report --type voice``
    dies with a traceback — defeating the guard's own stated intent, that
    "one unusable id is one unusable exemplar rather than a malformed
    call, so the collector skips it and goes on saving the rest".

    The long id sorts before ``frag-ok`` and is therefore persisted
    first, so the surviving copy is a real assertion rather than an
    artefact of ordering.

    The id lives in the frontmatter of a *short-named* file: a source
    file named after a 300-character id could not be written to disk at
    all, and the defect is about ids, not paths.
    """
    vault = _voice_vault(tmp_path)
    over_long_id = "a" * 300
    _write_voice_fragment(vault, "frag-ok")
    _write_voice_fragment(vault, over_long_id, file_stem="over-long-id")

    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-ok.md"]
    assert (_samples_root(vault) / "confessional" / "_Summary.md").is_file()


def test_report_voice_summary_stamps_an_unfiltered_tier_ceiling(
    tmp_path: Path,
) -> None:
    """A bare run records ``tier_ceiling: all`` in the summary.

    Regression pin for existing-but-unasserted behaviour, not a RED test.
    The stamp is the sole mitigation for a real caveat that
    ``_write_summary`` documents: a narrower ``--include-tier`` run
    surveys a smaller corpus *and* prunes samples a wider run produced,
    so two summaries taken at two ceilings describe two different corpora
    and their counts are not comparable. Without the stamp there is
    nothing on disk that says which corpus a given count came from,
    which makes it load-bearing rather than cosmetic.

    ``report``'s ``--include-tier`` narrows rather than widens, so an
    absent flag means unfiltered — ``PrivacyTierOverride.ALL``.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-ceiling-all", tier="personal")

    _run_report_voice(vault)

    assert _load_register_summary(vault)["tier_ceiling"] == "all"


def test_report_voice_summary_stamps_a_narrowed_tier_ceiling(
    tmp_path: Path,
) -> None:
    """``--include-tier open`` records ``tier_ceiling: open``.

    The other half of the pin above: the stamp must track the declared
    ceiling, not be a constant. Together the two tests kill the mutant
    that hardcodes either value.
    """
    vault = _voice_vault(tmp_path)
    _write_voice_fragment(vault, "frag-ceiling-open", tier="open")

    _run_report_voice(vault, "--include-tier", "open")

    assert _load_register_summary(vault)["tier_ceiling"] == "open"


def test_report_voice_narrowed_rerun_retracts_a_wider_runs_sample(
    tmp_path: Path,
) -> None:
    """A narrower ``--include-tier`` re-run deletes what a wider run wrote.

    ``docs/generation.md`` states this as a privacy remedy, not a
    convenience: "A narrower ``--include-tier`` prunes what a wider run
    wrote. This is the only way to retract above-ceiling copies after a
    broad run." Nothing else retracts a persisted sample — there is no
    un-persist flag, and deleting the source fragment is precisely the
    move that used to leave its verbatim body behind. If this does not
    hold, the documented remedy for "I ran ``report --type voice``
    unfiltered on a vault I meant to keep narrow" does not exist.

    ``test_report_voice_register_samples_honour_the_tier_ceiling`` cannot
    catch a regression here: it proves only that a narrow ceiling excludes
    on **write**, starting from an empty folder. Retraction is a different
    path. The copy is already on disk; this run never sees the fragment at
    all, because the ceiling drops it during ``collect_exemplars`` and so
    its id never reaches ``keep_ids``; and the only thing entitling the
    prune to delete the file is the digest the *wider* run recorded in
    ``_Summary.md``. Break that manifest round-trip — write it under
    another key, record ids the next run cannot reproduce, skip it when
    the ceiling changes — and the above-ceiling copy becomes permanent
    while every write-side ceiling test stays green.

    The ``open`` fragment is the control at both ends: it proves the wide
    run wrote more than the sample under test, and that the narrow re-run
    *retracted* rather than merely emptied the samples tree.
    """
    vault = _voice_vault(tmp_path)
    # A title sharing no substring with the id, so the title assertion
    # below proves something the id assertion does not: the summary
    # wikilinks a departed exemplar by both, and the title is the half a
    # deleted body copy does not take with it.
    retracted_title = "Midnight at the cannery"
    _write_voice_fragment(
        vault,
        "frag-wide-only",
        tier="personal",
        title=retracted_title,
    )
    _write_voice_fragment(vault, "frag-both-runs", tier="open")

    _run_report_voice(vault)

    assert _copied_names(vault) == ["frag-both-runs.md", "frag-wide-only.md"]
    wide = _load_register_summary(vault)
    assert wide["tier_ceiling"] == "all"
    assert wide["exemplar_count"] == 2

    _run_report_voice(vault, "--include-tier", "open")

    assert _copied_names(vault) == ["frag-both-runs.md"]
    residue = _samples_text(vault)
    assert "frag-wide-only" not in residue
    assert retracted_title not in residue
    # The control is still named in the tree it survived, so the
    # retraction cannot have been a wipe of the samples folder.
    assert "frag-both-runs" in residue
    narrowed = _load_register_summary(vault)
    assert narrowed["tier_ceiling"] == "open"
    assert narrowed["exemplar_count"] == 1


def _write_wavelength_fragment(vault: Path, frag_id: str) -> None:
    """Write one fragment carrying a classified wavelength phase (#719)."""
    from creek.models import (
        Fragment,
        FragmentSource,
        Phase,
        SourcePlatform,
        WavelengthClassification,
    )
    from creek.time import now_la
    from tests.helpers import write_fragment_file

    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=frag_id,
            title="Wavelength note",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            authored_at=now_la(),
            wavelength=WavelengthClassification(phase=Phase.RISING),
        ),
        body="A reflective entry.",
    )


def test_report_wavelength_weekly_command(tmp_path: Path) -> None:
    """report --type wavelength --period weekly writes a descriptive phase-map."""
    vault = tmp_path / "vault"
    _write_wavelength_fragment(vault, "frag-wavecli00001")
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
    assert "non-prescriptive" in result.output.lower()
    assert list((vault / "05-Wavelength" / "Phase-Maps").glob("*.md"))


def test_report_wavelength_monthly_command(tmp_path: Path) -> None:
    """report --type wavelength --period monthly writes a descriptive phase-map."""
    vault = tmp_path / "vault"
    _write_wavelength_fragment(vault, "frag-wavecli00002")
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
    assert "non-prescriptive" in result.output.lower()
    assert list((vault / "05-Wavelength" / "Phase-Maps").glob("*.md"))


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


def test_process_exits_cleanly_when_the_consent_log_is_unreadable(
    tmp_path: Path,
) -> None:
    """``creek process`` refuses, rather than tracebacking, on a broken log.

    A directory where ``consent-log.json`` belongs makes every read of
    the log fail. The gate must translate that into the CLI's refusal
    idiom — exit 1, a message naming the log — and must NOT report the
    source as newly consented, which would overwrite the log.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("Body\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)
    (vault / "00-Creek-Meta" / "Processing-Log" / "consent-log.json").mkdir(
        parents=True
    )

    result = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault), "--yes"],
    )

    assert result.exit_code == 1
    # A clean ``typer.Exit`` reaches CliRunner as SystemExit; an unhandled
    # IsADirectoryError would land here instead and also exit 1.
    assert isinstance(result.exception, SystemExit)
    output = _strip_ansi(result.output)
    # Rich may soft-wrap the long log path, so rejoin lines before matching.
    assert "consent-log.json" in output.replace("\n", "")
    assert "Consent auto-granted" not in " ".join(output.split())


def test_ingest_exits_cleanly_when_the_consent_log_is_unreadable(
    tmp_path: Path,
) -> None:
    """``creek ingest`` shares the gate, so it shares the refusal.

    ``ingest`` never constructs a ``Pipeline``, so a handler wrapped
    around ``pipeline.run`` cannot cover this call site — only one
    inside ``_gate_consent`` does.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("# Hello\n\nFirst note about systems.\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)
    (vault / "00-Creek-Meta" / "Processing-Log" / "consent-log.json").mkdir(
        parents=True
    )

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

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    output = _strip_ansi(result.output)
    assert "consent-log.json" in output.replace("\n", "")
    assert "Consent auto-granted" not in " ".join(output.split())


def test_process_reports_a_consent_log_that_breaks_mid_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log readable at the gate but not at Stage 0 is still a refusal.

    ``creek process`` reads the consent log twice: once in the gate, and
    again in the pipeline's Stage 0 consent check. A log that goes away
    between the two — a disk unmounting, a permissions change — surfaces
    from inside ``pipeline.run`` rather than from the gate, so a
    different handler has to catch it. Without one it exits as a raw
    traceback, which reads like a crash rather than a refusal.
    """
    from creek.consent import ConsentLogUnavailableError, ConsentManager

    src = tmp_path / "src"
    src.mkdir()
    (src / "note.md").write_text("# Hello\n\nFirst note about systems.\n")
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / d).mkdir(parents=True)

    calls = {"n": 0}
    real_check = ConsentManager.check_consent

    def _breaks_after_the_gate(
        self: ConsentManager, source_type: str, source_path: str
    ) -> bool:
        """Answer the gate honestly, then fail every later read.

        Args:
            self: The consent manager under test.
            source_type: Source identifier passed through to the real call.
            source_path: Source path passed through to the real call.

        Returns:
            The real answer, on the first call only.

        Raises:
            ConsentLogUnavailableError: On every call after the first.
        """
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConsentLogUnavailableError(
                vault / "00-Creek-Meta" / "Processing-Log" / "consent-log.json",
                OSError("disk went away"),
            )
        return real_check(self, source_type, source_path)

    monkeypatch.setattr(ConsentManager, "check_consent", _breaks_after_the_gate)

    result = runner.invoke(
        app,
        ["process", "--source", str(src), "--vault", str(vault), "--yes"],
    )

    assert calls["n"] > 1, "the pipeline never re-read the consent log"
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    output = _strip_ansi(result.output).replace("\n", "")
    assert "Consent log unavailable" in output
    assert "disk went away" in output


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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "Generated draft body.",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: _two_phase,
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
    """Test that draft exits 1 with a message when the LLM is unavailable.

    The client is built when the generator knows the tier of the fragments
    its prompt will carry (#1031), so the vault here holds a fragment and the
    draft is seeded from it — a run that composes no prompt at all now
    reaches no provider, which is the same choice ``creek_mcp.tools.draft``
    made in #958 and is why this test seeds rather than running on an empty
    vault.
    """
    from creek.classify.llm import LLMClassifier

    monkeypatch.setattr(LLMClassifier, "available", property(lambda _self: False))
    vault = tmp_path / "vault"
    _seed_test_vault(vault)
    _write_seed_fragment(vault, frag_id="frag-unavailable")
    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--seed-fragment", "frag-unavailable"],
    )
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "Stitched body.",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "Body composed from frag-keep.",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: (
            lambda _t: lambda _p: "Composed from the per-dimension blend."
        ),
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "Draft.",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        cli_module,
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "body",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "Twisted draft body.",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "   ",
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
        "_build_draft_llm_factory",
        lambda *_a, **_k: lambda _t: lambda _p: "   ",
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

    def _fake_builder(config: object, max_tokens: int | None = None) -> object:
        captured["max_tokens"] = max_tokens
        captured["config"] = config
        return lambda _tier: lambda _p: DraftLLMResponse(text="Generated draft body.")

    monkeypatch.setattr(cli_module, "_build_draft_llm_factory", _fake_builder)

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
    """With no flag, the factory uses the passed config's draft.max_tokens."""
    from creek import cli as cli_module
    from creek.classify.llm.providers import AnthropicCompletion
    from creek.config import CreekConfig, DraftConfig
    from creek.models import PrivacyTier

    captured: dict[str, object] = {}

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

    factory = cli_module._build_draft_llm_factory(
        CreekConfig(draft=DraftConfig(max_tokens=2048)),
    )
    factory(PrivacyTier.OPEN)("a prompt")
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


# ---- gdrive --download accounting (#1372) ----


def _flat_output(text: str) -> str:
    """Return *text* ANSI-free with Rich's soft wrapping collapsed to spaces.

    The accounting line the tests below assert on is a single sentence Rich
    is free to wrap; rejoining it means the assertion is about what the
    operator reads, not about where the terminal happened to break it.

    Args:
        text: Raw captured CLI output.

    Returns:
        The single-spaced, escape-free form of *text*.
    """
    return " ".join(_strip_ansi(text).split())


def _listed_drive_file(name: str) -> DriveFile:
    """Build a Drive listing entry named *name*.

    :class:`~creek.ingest.gdrive.DriveFile` is a frozen dataclass with no
    defaults, so every field is supplied; only ``name`` is load-bearing here,
    because it is the one thing an operator can act on.

    Args:
        name: The file name as Drive reports it.

    Returns:
        A fully populated listing entry.
    """
    from datetime import UTC, datetime

    return DriveFile(
        id=f"id-{name}",
        name=name,
        mime_type="application/octet-stream",
        modified_time=datetime(2026, 4, 1, tzinfo=UTC),
        size=2048,
        parent_path="",
    )


class _CannedConnector:
    """A Drive connector stand-in whose ``fetch_to`` returns a fixed result."""

    def __init__(self, result: DownloadResult) -> None:
        """Store the result ``fetch_to`` will hand back."""
        self.result = result

    def fetch_to(self, _staging: Path) -> DownloadResult:
        """Return the canned result without contacting Drive."""
        return self.result


def _install_canned_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: DownloadResult,
) -> None:
    """Point ``creek gdrive --download`` at a connector yielding *result*.

    ``build_drive_connector`` is imported inside the command body
    (cli.py:4641), so the seam is the attribute on :mod:`creek.ingest.gdrive`
    rather than a name bound in :mod:`creek.cli`.

    Args:
        monkeypatch: The test's patcher.
        tmp_path: Somewhere harmless for the config's vault/source roots.
        result: The outcome the download should report.
    """
    from creek.config import CreekConfig
    from creek.ingest.gdrive import GoogleApiDriveClient

    monkeypatch.setattr(
        "creek.cli.load_config",
        lambda *_a, **_k: CreekConfig(vault_path=tmp_path, source_drive=tmp_path),
    )
    monkeypatch.setattr(GoogleApiDriveClient, "is_available", lambda _self: True)
    monkeypatch.setattr(
        "creek.ingest.gdrive.build_drive_connector",
        lambda *_a, **_k: _CannedConnector(result),
    )


class TestGdriveDownloadAccounting:
    """``creek gdrive --download`` must account for every file it listed (#1372).

    The handler printed ``Downloaded N / Skipped M (unchanged) files`` and
    never looked at ``result.errors``, so a three-file listing where two files
    403'd rendered as "Downloaded 1 / Skipped 0" and exited 0 — arithmetic the
    operator cannot check, against a folder only Google can enumerate.
    """

    def test_every_listed_file_is_accounted_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Downloaded + Skipped + Failed covers the listing, and names the failures.

        Both filenames, not just the count: a partial download is only
        actionable if the operator knows which documents did not arrive.
        """
        staging = tmp_path / "stg"
        _install_canned_download(
            monkeypatch,
            tmp_path,
            DownloadResult(
                downloaded=(staging / "alpha.docx",),
                skipped=(),
                errors=(
                    (_listed_drive_file("beta.docx"), RuntimeError("HTTP 403")),
                    (_listed_drive_file("gamma.docx"), RuntimeError("HTTP 500")),
                ),
            ),
        )

        result = runner.invoke(app, ["gdrive", "--download", "--staging", str(staging)])

        flat = _flat_output(result.stdout)
        assert "Downloaded 1 / Skipped 0 (unchanged) / Failed 2" in flat, flat
        assert "beta.docx" in flat, flat
        assert "gamma.docx" in flat, flat
        assert result.exit_code == 0, result.output

    def test_total_failure_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing fetched, nothing already current, and errors — that is a failure.

        A run that produced no files at all exited 0, so a schedule that
        chains ``creek gdrive --download && creek ingest ...`` marched
        straight on to ingest an empty staging directory and reported success.
        """
        staging = tmp_path / "stg"
        _install_canned_download(
            monkeypatch,
            tmp_path,
            DownloadResult(
                downloaded=(),
                skipped=(),
                errors=((_listed_drive_file("beta.docx"), RuntimeError("HTTP 401")),),
            ),
        )

        result = runner.invoke(app, ["gdrive", "--download", "--staging", str(staging)])

        assert result.exit_code == 1, result.output

    def test_a_warm_incremental_tick_with_one_failure_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two files already up to date plus one failure is not a failed run.

        ``errors and not downloaded`` is the predicate that looks natural and
        is wrong: on a warm incremental tick *every* healthy file is skipped
        rather than downloaded, so that test would declare total failure for
        the ordinary steady state — the case that runs every thirty minutes.
        The skip set has to count as work that succeeded.
        """
        staging = tmp_path / "stg"
        _install_canned_download(
            monkeypatch,
            tmp_path,
            DownloadResult(
                downloaded=(),
                skipped=(staging / "alpha.docx", staging / "delta.docx"),
                errors=((_listed_drive_file("beta.docx"), RuntimeError("HTTP 500")),),
            ),
        )

        result = runner.invoke(app, ["gdrive", "--download", "--staging", str(staging)])

        assert result.exit_code == 0, result.output

    def test_attempted_accounts_for_every_listed_file(self, tmp_path: Path) -> None:
        """``attempted`` is downloaded + skipped + errors — the size of the listing.

        The number the printed accounting line is measured against. Kept as a
        property on the result rather than recomputed at each call site, so
        the CLI and any future caller cannot disagree about what "every file"
        means.
        """
        result = DownloadResult(
            downloaded=(tmp_path / "a",),
            skipped=(tmp_path / "b", tmp_path / "c"),
            errors=(
                (_listed_drive_file("d.docx"), RuntimeError("x")),
                (_listed_drive_file("e.docx"), RuntimeError("y")),
                (_listed_drive_file("f.docx"), RuntimeError("z")),
            ),
        )

        assert result.attempted == 6


# ---- --strict must fire when the preflight saw files discover() never did (#1574) ----


_SUBSTACK_POST_HTML = (
    b"<html><body><h1>On Silt</h1><p>Silt is what the creek leaves behind.</p>"
    b"</body></html>"
)
"""One plausible Substack post whose filename carries no leading post id."""

_PLAIN_TEXT = (
    b"# Notes\n\nA plain paragraph with enough words to read as a real body.\n"
)
"""Text that any ingestor willing to read the file will turn into a fragment."""

_BINARY = bytes(range(256)) * 8
"""Bytes no text ingestor will accept, for suffix-filter probes."""

# One plausible file per registered ingestor: the shape an operator would
# reasonably point that ``--type`` at. Nine of the eleven are rejected
# wholesale by ``discover()`` even though the consent preflight counts them;
# ``document`` and ``generic`` accept theirs, which is what keeps the
# invariant below from being satisfiable by "always exit 1".
_ONE_PLAUSIBLE_FILE: dict[str, tuple[str, bytes]] = {
    "chatgpt": ("export.json", b"{}"),
    "claude": ("export.json", b"{}"),
    "code": ("bin.dat", _BINARY),
    "discord": ("channel/messages.json", b"[]"),
    "document": ("notes.txt", _PLAIN_TEXT),
    "generic": ("notes.log", _PLAIN_TEXT),
    "image": ("scan.heic", _BINARY),
    "markdown": ("notes.txt", _PLAIN_TEXT),
    "presentation": ("deck.key", _BINARY),
    "spreadsheet": ("book.numbers", _BINARY),
    "substack": ("posts/on-silt.html", _SUBSTACK_POST_HTML),
}

_INGESTED_COUNT_RE = re.compile(r"Ingested (\d+) fragment\(s\)")


def _seed_substack_export_without_post_ids(tmp_path: Path) -> Path:
    """Write a Substack-shaped export whose post HTML has no leading post id."""
    src = tmp_path / "export"
    (src / "posts").mkdir(parents=True)
    (src / "posts" / "on-silt.html").write_bytes(_SUBSTACK_POST_HTML)
    return src


def _invoke_ingest(*, source_type: str, src: Path, vault: Path, strict: bool) -> object:
    """Run ``creek ingest`` against *src*, optionally under ``--strict``."""
    argv = [
        "ingest",
        "--type",
        source_type,
        "--input",
        str(src),
        "--vault",
        str(vault),
        "-y",
    ]
    if strict:
        argv.append("--strict")
    return runner.invoke(app, argv)


def test_strict_fails_when_preflight_counted_files_but_discover_yielded_none(
    tmp_path: Path,
) -> None:
    """A non-empty source the ingestor rejected wholesale must fail --strict (#1574).

    The preflight counts files with ``rglob("*")`` while ``discover()`` applies
    the ingestor's own filter, so a Substack post missing its leading post id
    is counted by one scanner and invisible to the other. ``--strict`` used to
    return before its raise on ``discovered == 0`` and report success.
    """
    vault = _scaffold_min_vault(tmp_path)
    src = _seed_substack_export_without_post_ids(tmp_path)

    result = _invoke_ingest(source_type="substack", src=src, vault=vault, strict=True)

    assert result.exit_code == 1, result.output
    assert "Ingested 0 fragment(s)" in _flat_output(result.output)


def test_the_rejected_file_and_its_expected_shape_are_named_on_stdout(
    tmp_path: Path,
) -> None:
    """The advisory names the unread file and the shape it should have had (#1574).

    A generic "0 fragments" line leaves the operator with nowhere to go. The
    reject reason lived only in ``logger.debug``, which no CLI run shows.
    """
    vault = _scaffold_min_vault(tmp_path)
    src = _seed_substack_export_without_post_ids(tmp_path)

    result = _invoke_ingest(source_type="substack", src=src, vault=vault, strict=False)

    flat = _flat_output(result.output)
    assert result.exit_code == 0, result.output
    assert "on-silt.html" in flat
    assert "<post_id>.<slug>.html" in flat


def test_every_registered_ingestor_has_an_input_expectation() -> None:
    """The expectation table covers the registry exactly (#1574).

    Guards the parametrised invariant below against silently shrinking: an
    ingestor added without an entry would otherwise get a warning with
    nothing actionable in it.
    """
    from creek.ingest import INGESTOR_INPUT_EXPECTATIONS, INGESTOR_REGISTRY

    assert len(INGESTOR_REGISTRY) == 11
    assert set(INGESTOR_INPUT_EXPECTATIONS) == set(INGESTOR_REGISTRY)
    assert set(_ONE_PLAUSIBLE_FILE) == set(INGESTOR_REGISTRY)


@pytest.mark.parametrize("source_type", sorted(_ONE_PLAUSIBLE_FILE))
def test_no_ingestor_reports_success_when_it_wrote_nothing_from_a_non_empty_source(
    source_type: str,
    tmp_path: Path,
) -> None:
    """Under ``--strict``, exit 0 iff the run actually wrote a fragment (#1574).

    Substack is a sample, not the population: nine of the eleven registered
    ingestors discover nothing from a file an operator would plausibly point
    that ``--type`` at, and every one of them exited 0. The invariant is
    stated over the whole registry so a fix that only patched Substack fails
    here. ``document`` and ``generic`` do write from their fixture, so this
    cannot be satisfied by failing unconditionally.
    """
    vault = _scaffold_min_vault(tmp_path)
    relpath, payload = _ONE_PLAUSIBLE_FILE[source_type]
    src = tmp_path / "in"
    target = src / relpath
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    result = _invoke_ingest(source_type=source_type, src=src, vault=vault, strict=True)

    match = _INGESTED_COUNT_RE.search(_flat_output(result.output))
    assert match is not None, result.output
    wrote = int(match.group(1))
    assert result.exit_code == (0 if wrote else 1), (
        f"{source_type}: wrote {wrote} fragment(s) but exited "
        f"{result.exit_code}\n{result.output}"
    )


def test_a_single_file_source_is_counted_by_the_preflight_scanner(
    tmp_path: Path,
) -> None:
    """``--input <file>`` counts as one file, not zero (#1574).

    ``rglob("*")`` over a file yields nothing, so the consent prompt reported
    "Found: 0 file(s)" for a source the run then read — the same scanner
    disagreement, one directory level down.
    """
    from creek.consent import build_source_summary

    single = tmp_path / "notes.md"
    single.write_bytes(_PLAIN_TEXT)

    summary = build_source_summary(single, [])

    assert summary.file_count == 1
    assert summary.sample_filenames == ["notes.md"]


def test_sync_warns_about_unrecognised_inputs_without_failing_the_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``creek sync`` reports the same reconciliation but never exits (#1574).

    A scheduled tick that raised would both fail the launchd unit and destroy
    the record that the tick ran at all, so sync keeps ``strict=False`` — it
    must still say what it saw.
    """
    from creek.cli import _sync_ingest_source
    from creek.config import CreekConfig, SyncConfig

    monkeypatch.setattr("creek.cli._run_ingest", lambda **_kw: (0, [], 0))
    src = tmp_path / "src"
    journal = src / "personal" / "journal"
    journal.mkdir(parents=True)
    (journal / "notes.rst").write_bytes(_PLAIN_TEXT)
    cfg = CreekConfig(
        vault_path=tmp_path / "v",
        source_drive=src,
        sync=SyncConfig(sources={"journal": True}),
    )

    record = _sync_ingest_source("journal", cfg, tmp_path / "v")

    flat = " ".join(_strip_ansi(capsys.readouterr().out).split())
    assert record.ingested == 0
    assert "notes.rst" in flat
    assert "files ending .md" in flat


def test_the_unread_input_advisory_says_when_it_sampled(tmp_path: Path) -> None:
    """A truncated file list must announce itself as truncated (#1574).

    The consent summary samples at most ten names. Ten names printed with no
    marker read as the whole population, so an operator concludes the other
    files were fine when in fact none of them was read.
    """
    vault = _scaffold_min_vault(tmp_path)
    src = tmp_path / "export"
    (src / "posts").mkdir(parents=True)
    for index in range(14):
        (src / "posts" / f"post-{index:02d}.html").write_bytes(_SUBSTACK_POST_HTML)

    result = _invoke_ingest(source_type="substack", src=src, vault=vault, strict=False)

    flat = _flat_output(result.output)
    assert "saw 14 file(s)" in flat, result.output
    assert "and 4 more" in flat, result.output
