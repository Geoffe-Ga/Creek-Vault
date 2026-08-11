"""Tests for the ``creek voice-authenticity`` diagnostic skeleton (FEAT #552).

These cover the read-only :mod:`creek.generate.voice_authenticity` probes and
the Typer command surface. The skeleton only *measures* current behaviour, so
the assertions pin the report *shape* (mix counts reconcile to the eligible
corpus, leak fraction stays in ``[0, 1]``, ``--draft`` populates the de-slop
sub-score) rather than any fixed numbers.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import frontmatter
import pytest
import yaml
from typer.testing import CliRunner

from creek.cli import app
from creek.generate.voice_authenticity import (
    build_voice_authenticity_report,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Return *text* with ANSI escape sequences removed."""
    return _ANSI_ESCAPE_RE.sub("", text)


_WEIGHTING_CASES = [True, False]
assert len(_WEIGHTING_CASES) == 2, (
    "Both halves are required: a lone True case passes whether or not the "
    "probe reads the vault's config, which is how #1313 hid for so long."
)


def _set_vault_weighting(vault: Path, *, enabled: bool) -> None:
    """Write ``<vault>/00-Creek-Meta/creek_config.yaml`` with the knob set.

    Before #1313 ``_probe_audience_mix`` constructed its own
    ``VoiceAudienceWeightingConfig()`` and reported that default, so a probe on
    a config-less tmp vault answered ``True`` whether or not it read anything.
    Seeding a real config — and asserting both values — is what makes the
    ``weighting_active`` assertions below able to fail.

    Args:
        vault: Vault root.
        enabled: Value for ``voice_audience_weighting.enabled``.
    """
    from creek.config import CreekConfig

    data = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["voice_audience_weighting"]["enabled"] = enabled
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text(
        yaml.dump(data, sort_keys=False),
        encoding="utf-8",
    )


def _write_fragment(
    fragments_dir: Path,
    name: str,
    *,
    platform: str,
    author: str = "self",
    privacy_tier: str = "open",
) -> None:
    """Write one fragment markdown file into *fragments_dir*.

    Args:
        fragments_dir: The vault's ``01-Fragments`` directory.
        name: File stem and fragment id/title.
        platform: ``source.platform`` value (e.g. ``"markdown"``, ``"claude"``).
        author: ``source.author`` value; defaults to ``"self"``.
        privacy_tier: The fragment's ``privacy_tier``; defaults to ``"open"``.
    """
    post = frontmatter.Post(
        f"Body text for {name}.",
        type="fragment",
        id=name,
        title=name,
        source={"platform": platform, "author": author},
        privacy_tier=privacy_tier,
    )
    (fragments_dir / f"{name}.md").write_text(
        frontmatter.dumps(post),
        encoding="utf-8",
    )


def _scaffold_mixed_vault(vault: Path) -> Path:
    """Create a vault whose eligible corpus mixes native and AI-chat ingests.

    Eligible (self-authored, non-intimate):
      - two native ``markdown`` fragments (one OPEN, one PERSONAL),
      - one ``claude`` ingest (OPEN) and one ``chatgpt`` ingest (PERSONAL),
      - one UNCLASSIFIED native fragment.
    Excluded:
      - one INTIMATE self fragment (privacy excludes it),
      - one AI-authored ``claude`` fragment (authorship excludes it).

    Returns:
        The ``01-Fragments`` directory that was populated.
    """
    fragments_dir = vault / "01-Fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    _write_fragment(fragments_dir, "native_open", platform="markdown")
    _write_fragment(
        fragments_dir,
        "native_personal",
        platform="markdown",
        privacy_tier="personal",
    )
    _write_fragment(fragments_dir, "claude_open", platform="claude")
    _write_fragment(
        fragments_dir,
        "chatgpt_personal",
        platform="chatgpt",
        privacy_tier="personal",
    )
    _write_fragment(
        fragments_dir,
        "native_unclassified",
        platform="markdown",
        privacy_tier="unclassified",
    )
    _write_fragment(
        fragments_dir,
        "intimate_excluded",
        platform="markdown",
        privacy_tier="intimate",
    )
    _write_fragment(
        fragments_dir,
        "ai_authored_excluded",
        platform="claude",
        author="ai",
    )
    return fragments_dir


def test_report_shape_reconciles_to_eligible_corpus(tmp_path: Path) -> None:
    """Mix counts sum to the eligible total and the leak fraction is bounded."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    report = build_voice_authenticity_report(vault, draft_path=None)

    # Five eligible fragments; the intimate and AI-authored ones are excluded.
    assert report.audience_mix.total == 5
    assert report.audience_mix.total == report.ai_corpus_leak.eligible_total
    assert sum(report.audience_mix.by_tier.values()) == report.audience_mix.total
    assert 0.0 <= report.ai_corpus_leak.fraction <= 1.0


@pytest.mark.parametrize("enabled", _WEIGHTING_CASES)
def test_audience_mix_buckets_eligible_by_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> None:
    """Eligible fragments bucket by tier; INTIMATE stays zero (excluded).

    Parametrized over the weighting knob because ``weighting_active`` must
    track the **vault's** setting rather than a fabricated default (#1313).
    The tier buckets are invariant to it — the weighting ranks, it does not
    gate — so asserting them under both values also pins that.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Used to unset ``CREEK_CONFIG``, which outranks the
            vault's own file in ``resolve_config_path`` — with it set in the
            environment the seeded config would never be read and the
            ``False`` row would report ``True`` for the wrong reason.
        enabled: The vault's configured weighting value.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    # And run from a directory holding no creek_config.yaml. Without this the
    # False row discriminates against a bare-load_config() regression only
    # because the ambient cwd happens to have no config in it — true today,
    # enforced by nothing.
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    _set_vault_weighting(vault, enabled=enabled)

    mix = build_voice_authenticity_report(vault, draft_path=None).audience_mix

    assert mix.by_tier["open"] == 2
    assert mix.by_tier["personal"] == 2
    assert mix.by_tier["unclassified"] == 1
    assert mix.by_tier["intimate"] == 0
    # Reports the vault's own setting, not the probe's built-in default.
    assert mix.weighting_active is enabled


def test_ai_corpus_leak_counts_chat_platforms(tmp_path: Path) -> None:
    """Only eligible claude/chatgpt ingests count toward the leak."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    leak = build_voice_authenticity_report(vault, draft_path=None).ai_corpus_leak

    # claude_open + chatgpt_personal are eligible AI-chat ingests.
    assert leak.leaked == 2
    assert leak.eligible_total == 5
    assert leak.fraction == pytest.approx(2 / 5)


def test_empty_vault_is_well_formed(tmp_path: Path) -> None:
    """A vault with no fragments yields a zeroed, division-safe report."""
    vault = tmp_path / "vault"
    vault.mkdir()

    report = build_voice_authenticity_report(vault, draft_path=None)

    assert report.audience_mix.total == 0
    assert report.ai_corpus_leak.eligible_total == 0
    assert report.ai_corpus_leak.fraction == 0.0
    assert report.deslop is None


def test_deslop_none_without_draft(tmp_path: Path) -> None:
    """Without ``--draft`` the de-slop sub-score is absent."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    report = build_voice_authenticity_report(vault, draft_path=None)

    assert report.deslop is None


def test_deslop_attested_when_voice_distance_present(tmp_path: Path) -> None:
    """A draft carrying voice-fidelity frontmatter reports attestation."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    draft = tmp_path / "draft.md"
    post = frontmatter.Post(
        "Drafted essay body.",
        voice_distance=0.4213,
        voice_findings=[{"tell_id": "t1"}, {"tell_id": "t2"}],
    )
    draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    deslop = build_voice_authenticity_report(vault, draft_path=draft).deslop

    assert deslop is not None
    assert deslop.attested is True
    assert deslop.voice_distance == pytest.approx(0.4213)
    assert deslop.residual_findings == 2
    assert deslop.status  # a non-empty (stubbed) reason string


def test_deslop_unattested_when_no_voice_distance(tmp_path: Path) -> None:
    """A draft without voice-fidelity frontmatter reports no attestation."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    draft = tmp_path / "plain_draft.md"
    draft.write_text(
        frontmatter.dumps(frontmatter.Post("Just a body, no guard ran.")),
        encoding="utf-8",
    )

    deslop = build_voice_authenticity_report(vault, draft_path=draft).deslop

    assert deslop is not None
    assert deslop.attested is False
    assert deslop.voice_distance is None
    assert deslop.residual_findings == 0
    assert deslop.status


@pytest.mark.parametrize("enabled", _WEIGHTING_CASES)
def test_to_json_is_stable_and_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> None:
    """``to_json`` emits a documented, parseable schema with all sub-scores.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Used to unset ``CREEK_CONFIG`` — see
            :func:`test_audience_mix_buckets_eligible_by_tier` for why the
            ``False`` row is otherwise satisfiable for the wrong reason.
        enabled: The vault's configured weighting value, which the JSON
            payload must carry through faithfully rather than defaulting
            (#1313).
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    # And run from a directory holding no creek_config.yaml. Without this the
    # False row discriminates against a bare-load_config() regression only
    # because the ambient cwd happens to have no config in it — true today,
    # enforced by nothing.
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    _set_vault_weighting(vault, enabled=enabled)

    report = build_voice_authenticity_report(vault, draft_path=None)
    payload = json.loads(report.to_json())

    assert payload["eligible_total"] == 5
    assert payload["audience_mix"]["weighting_active"] is enabled
    assert payload["audience_mix"]["by_tier"]["open"] == 2
    assert payload["audience_mix"]["total"] == 5
    assert payload["ai_corpus_leak"]["leaked"] == 2
    assert 0.0 <= payload["ai_corpus_leak"]["fraction"] <= 1.0
    assert payload["deslop"] is None


def test_summary_line_reports_all_three_subscores(tmp_path: Path) -> None:
    """The human summary names the corpus size, mix, leak, and de-slop."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    summary = build_voice_authenticity_report(vault, draft_path=None).summary_line()

    assert "5 eligible" in summary
    assert "audience mix" in summary
    assert "AI-corpus leak" in summary
    assert "de-slop" in summary
    assert "(no --draft given)" in summary


def test_summary_line_renders_attested_deslop(tmp_path: Path) -> None:
    """With an attested draft the summary names the distance and findings."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    draft = tmp_path / "draft.md"
    post = frontmatter.Post(
        "Body.",
        voice_distance=0.51,
        voice_findings=[{"tell_id": "t1"}],
    )
    draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    summary = build_voice_authenticity_report(
        vault,
        draft_path=draft,
    ).summary_line()

    assert "voice_distance 0.5100" in summary
    assert "1 residual finding(s)" in summary


def test_summary_line_renders_unattested_deslop(tmp_path: Path) -> None:
    """With an unattested draft the summary reports no attestation."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    draft = tmp_path / "plain.md"
    draft.write_text(
        frontmatter.dumps(frontmatter.Post("Body, no guard.")),
        encoding="utf-8",
    )

    summary = build_voice_authenticity_report(
        vault,
        draft_path=draft,
    ).summary_line()

    assert "no attestation" in summary


# --- CLI smoke tests --------------------------------------------------------


def test_cli_text_output(tmp_path: Path) -> None:
    """The command runs end-to-end and prints all three sub-scores."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    result = runner.invoke(
        app,
        ["voice-authenticity", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    plain = _strip_ansi(result.output)
    assert "audience mix" in plain
    assert "AI-corpus leak" in plain
    assert "de-slop" in plain


def test_cli_json_output(tmp_path: Path) -> None:
    """``--json`` emits a parseable object instead of the text summary."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    result = runner.invoke(
        app,
        ["voice-authenticity", "--vault", str(vault), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(_strip_ansi(result.output))
    assert payload["eligible_total"] == 5
    assert payload["deslop"] is None


def test_cli_draft_populates_deslop(tmp_path: Path) -> None:
    """Passing ``--draft`` populates the de-slop sub-score in JSON."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)
    draft = tmp_path / "draft.md"
    post = frontmatter.Post("Body.", voice_distance=0.3)
    draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "voice-authenticity",
            "--vault",
            str(vault),
            "--draft",
            str(draft),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(_strip_ansi(result.output))
    assert payload["deslop"]["attested"] is True
    assert payload["deslop"]["voice_distance"] == pytest.approx(0.3)


def test_cli_missing_draft_exits_two(tmp_path: Path) -> None:
    """A ``--draft`` path that does not exist exits with code 2."""
    vault = tmp_path / "vault"
    _scaffold_mixed_vault(vault)

    result = runner.invoke(
        app,
        [
            "voice-authenticity",
            "--vault",
            str(vault),
            "--draft",
            str(tmp_path / "nope.md"),
        ],
    )

    assert result.exit_code == 2
