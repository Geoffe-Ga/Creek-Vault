"""CLI tests for ``creek compost scan`` (#882).

``creek compost calibrate`` degrades silently when the configured provider is
unavailable — it only prints a score, so embedding-only fallback costs nothing.
``scan`` writes to the vault, so the same fallback would file *unverified*
candidates as canonical compost: exactly the pipeline dishonesty this command
exists to remove. These tests pin the louder contract:

* no provider and no ``--no-llm`` → refuse with a non-zero exit and name the flag;
* ``--no-llm`` → run to completion without ever constructing a provider;
* ``--dry-run`` → print the pre-flight estimate and write nothing;
* a scanned vault feeds ``creek fill --with-compost``'s overview report.

The embedding stack is stubbed throughout: these assert wiring and refusal
behaviour, not detector quality (that is ``creek compost calibrate``'s job).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

import creek.cli as cli_mod
from creek.classify.llm.router import ModelRouter
from creek.cli import app
from creek.generate.compost_verifier import CompostVerdict, CompostVerifierResult
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_CANONICAL_RELDIR = "10-Liminal/Compost"
"""Vault-relative folder confirmed compost notes land in."""

_REVIEW_RELDIR = "10-Liminal/Compost/Review"
"""Vault-relative folder unverified / ambiguous candidates land in."""


class _StubVerifier:
    """Verifier stub returning a fixed verdict for every fragment."""

    def __init__(self, verdict: CompostVerdict = CompostVerdict.YES) -> None:
        """Store the verdict every call will return."""
        self.verdict = verdict

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Return the configured verdict, ignoring *title* and *body*."""
        del title, body
        return CompostVerifierResult(verdict=self.verdict, reasoning="stub")


class _UnavailableProvider:
    """Provider whose prerequisites (key / consent / local host) are unmet."""

    available = False


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Vault holding one high-similarity fragment and the compost folders."""
    for rel in ("01-Fragments", "02-Threads", "00-Creek-Meta", _CANONICAL_RELDIR):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    fragment = Fragment(
        id="frag-scan",
        title="Letting the zine go",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 3, 1, 9, 0, 0),
        ingested=datetime(2026, 3, 1, 9, 0, 0),
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(),
    )
    post = frontmatter.Post(content="I am done carrying this one.")
    post.metadata.update(fragment.model_dump(mode="json"))
    (tmp_path / "01-Fragments" / "frag-scan.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the embedding gate with a deterministic always-high stub.

    The real gate loads a sentence-transformers model; these tests are about
    CLI wiring, so the closure is swapped wholesale.
    """
    monkeypatch.setattr(
        cli_mod,
        "_build_compost_similarity_fn",
        lambda _config, _vault_path: lambda _text: 0.95,
    )


def _notes_in(vault_path: Path, relpath: str) -> list[Path]:
    """Return compost notes under *relpath*, excluding report/index shells."""
    folder = vault_path / relpath
    if not folder.exists():
        return []
    return [p for p in sorted(folder.glob("*.md")) if not p.name.startswith("_")]


# ---- Refusal: never file unverified candidates as canonical compost ----


def test_scan_refuses_when_the_provider_cannot_be_built(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that raises on construction stops the scan, it does not degrade."""

    def _raise(_cfg: object) -> object:
        msg = "ANTHROPIC_API_KEY is not set"
        raise RuntimeError(msg)

    monkeypatch.setattr("creek.classify.llm.build_provider", _raise)

    result = runner.invoke(app, ["compost", "scan", "--vault", str(vault)])

    assert result.exit_code != 0
    assert "--no-llm" in result.output
    assert _notes_in(vault, _CANONICAL_RELDIR) == []


def test_scan_refuses_when_the_provider_reports_prerequisites_unmet(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing key/consent surfaces as ``available is False`` — also a refusal."""
    monkeypatch.setattr(
        "creek.classify.llm.build_provider",
        lambda _cfg: _UnavailableProvider(),
    )

    result = runner.invoke(app, ["compost", "scan", "--vault", str(vault)])

    assert result.exit_code != 0
    assert "--no-llm" in result.output
    assert "configured model not installed" in result.output
    assert _notes_in(vault, _CANONICAL_RELDIR) == []


def test_scan_resolves_the_verifier_with_a_tier_ceiling(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan states a tier when routing, rather than resolving with ``None``.

    ``creek/cli.py``'s ``_build_compost_verifier`` carries a durable caveat:
    a vault-scanning compost path feeds *fragment content* to the provider, so
    unlike the fixture-only calibrate path it must resolve with a tier. A
    ``None`` here would silently inherit whatever the ``generation`` stage
    defaults to, defeating the Intimate-never-cloud gate the router exists to
    enforce. This mirrors the structural check in
    ``tests/test_mcp_report_tier_ceiling.py``.
    """
    seen: list[tuple[str, PrivacyTier | None]] = []
    real_resolve = ModelRouter.resolve

    def _spy(
        self: ModelRouter,
        stage: str,
        tier: PrivacyTier | None = None,
    ) -> object:
        seen.append((stage, tier))
        return real_resolve(self, stage, tier)

    monkeypatch.setattr(ModelRouter, "resolve", _spy)
    monkeypatch.setattr(
        "creek.classify.llm.build_provider",
        lambda _cfg: _UnavailableProvider(),
    )

    runner.invoke(app, ["compost", "scan", "--vault", str(vault)])

    generation_tiers = [tier for stage, tier in seen if stage == "generation"]
    assert generation_tiers, "the scan never resolved the generation stage"
    assert all(tier is not None for tier in generation_tiers)
    assert PrivacyTier.INTIMATE not in generation_tiers


def test_scan_with_no_llm_never_constructs_a_provider(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-llm`` completes offline: no provider, no egress, review-queue only."""

    def _explode(_cfg: object) -> object:
        msg = "build_provider must not be called under --no-llm"
        raise AssertionError(msg)

    monkeypatch.setattr("creek.classify.llm.build_provider", _explode)

    result = runner.invoke(
        app,
        ["compost", "scan", "--vault", str(vault), "--no-llm"],
    )

    assert result.exit_code == 0, result.output
    assert _notes_in(vault, _CANONICAL_RELDIR) == []
    assert len(_notes_in(vault, _REVIEW_RELDIR)) == 1


# ---- Pre-flight estimate ----


def test_scan_dry_run_reports_the_estimate_and_writes_nothing(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` surfaces the counts without touching the vault."""
    monkeypatch.setattr(
        cli_mod,
        "_build_scan_verifier",
        lambda _config: _StubVerifier(),
    )

    result = runner.invoke(
        app,
        ["compost", "scan", "--vault", str(vault), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "1" in result.output
    assert _notes_in(vault, _CANONICAL_RELDIR) == []


def test_dry_run_estimates_without_a_configured_provider(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` quotes the cost even when no provider is configured.

    The estimate exists so an operator can see what a run would cost and bail.
    The moment they most plausibly want that number is *before* they have set
    up credentials — so eagerly building (and refusing on) a verifier the
    dry-run path never calls defeats the feature's whole purpose.

    The count must still be the one a *real* run would incur. Reporting 0 LLM
    calls just because no verifier object was constructed would be a quieter
    bug than the refusal it replaced.
    """

    def _raise(_cfg: object) -> object:
        msg = "ANTHROPIC_API_KEY is not set"
        raise RuntimeError(msg)

    monkeypatch.setattr("creek.classify.llm.build_provider", _raise)

    result = runner.invoke(
        app,
        ["compost", "scan", "--vault", str(vault), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "LLM calls this run: 1" in " ".join(result.output.split())
    assert _notes_in(vault, _CANONICAL_RELDIR) == []


def test_scan_writes_a_canonical_note_for_a_confirmed_candidate(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path: a ``yes`` verdict reaches ``10-Liminal/Compost/``."""
    monkeypatch.setattr(
        cli_mod,
        "_build_scan_verifier",
        lambda _config: _StubVerifier(CompostVerdict.YES),
    )

    result = runner.invoke(app, ["compost", "scan", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    written = _notes_in(vault, _CANONICAL_RELDIR)
    assert len(written) == 1
    assert frontmatter.load(str(written[0])).get("original_fragment") == "[[frag-scan]]"


def test_scan_is_registered_as_a_compost_subcommand() -> None:
    """``creek compost --help`` advertises ``scan`` beside ``calibrate``."""
    result = runner.invoke(app, ["compost", "--help"])

    assert result.exit_code == 0
    assert "scan" in result.output
    assert "calibrate" in result.output


# ---- Integration with the fill report ----


def test_fill_with_compost_report_lists_a_scanned_note(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overview report reflects notes the scan wrote — the FEAT-018 loop closes.

    ``creek fill --with-compost`` was previously a Dataview shell over a
    permanently empty folder. After a scan it has something to report.
    """
    monkeypatch.setattr(
        cli_mod,
        "_build_scan_verifier",
        lambda _config: _StubVerifier(CompostVerdict.YES),
    )
    scan = runner.invoke(app, ["compost", "scan", "--vault", str(vault)])
    assert scan.exit_code == 0, scan.output

    from creek.generate.compost import CompostTracker

    report_path = CompostTracker().generate_compost_report(vault)
    report = report_path.read_text(encoding="utf-8")

    assert "Letting the zine go" in report
    assert "_No compost notes recorded yet._" not in report
