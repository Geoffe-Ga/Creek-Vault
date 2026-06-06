"""Tests for the voice-fidelity guard + revision loop (FEAT-040.9).

The guard sanitizes a composed draft, measures its distance from the user's
voice fingerprint, and — when over the configured ceiling — runs a bounded
rewrite *toward* that voice. These tests pin the contract the acceptance
criteria name: a tropey body is rewritten lower, ``no_llm`` measures without
a rewrite, a distance-raising rewrite is discarded, a grounding-dropping
rewrite is rejected, the pass count is capped, and the report stamps clean
frontmatter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import AIStyleConfig
from creek.generate.ai_style.guard import (
    VoiceFidelityReport,
    build_voice_fidelity_frontmatter,
    build_voice_rewrite_prompt,
    run_voice_fidelity_guard,
)
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.scanner import scan

if TYPE_CHECKING:
    from collections.abc import Callable

# A body dense with registry tells: transition openers, AI vocabulary,
# didactic disclaimers, significance puffery.
_TROPEY = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant and multifaceted nuances. "
    "Furthermore, the landscape stands as a testament to its significance."
)
# Plain prose with no tells.
_PLAIN = "The cat sat by the window. Rain fell on the roof. I made tea and read."


def _fingerprint(*, fragments: int = 20) -> VoiceFingerprint:
    """A non-thin fingerprint whose measured rates are all ~zero.

    With no AI-ish features in the user's own writing, any tell the draft
    over-uses diverges from the user — exactly the case the guard exists for.
    """
    return VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.0, support=fragments)},
        fragment_count=fragments,
    )


def _eager_config(**overrides: object) -> AIStyleConfig:
    """A config that wants to rewrite almost any divergence."""
    base: dict[str, object] = {"voice_distance_upper": 0.001, "max_revision_passes": 1}
    base.update(overrides)
    return AIStyleConfig(**base)


def _counting_llm(*returns: str) -> tuple[Callable[[str], str], dict[str, int]]:
    """Return an LLM stub yielding *returns* in order, plus a call counter."""
    seq = iter(returns)
    calls = {"n": 0}

    def _call(_prompt: str) -> str:
        calls["n"] += 1
        return next(seq)

    return _call, calls


def test_no_llm_measures_without_rewrite() -> None:
    """``no_llm`` sanitizes and measures but never calls the rewrite LLM."""
    llm, calls = _counting_llm(_PLAIN)
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=_eager_config(),
        llm=llm,
        no_llm=True,
    )
    assert calls["n"] == 0
    assert report.passes == 0
    assert report.voice_distance > 0.0


def test_rewrite_lowers_distance() -> None:
    """A tropey body is rewritten once toward voice and re-measured lower."""
    fingerprint = _fingerprint()
    config = _eager_config()
    baseline = run_voice_fidelity_guard(
        _TROPEY, fingerprint=fingerprint, config=config, no_llm=True
    )
    llm, calls = _counting_llm(_PLAIN)
    report = run_voice_fidelity_guard(
        _TROPEY, fingerprint=fingerprint, config=config, llm=llm
    )
    assert calls["n"] == 1
    assert report.passes == 1
    assert report.voice_distance < baseline.voice_distance
    assert "tapestry" not in report.body


def test_distance_raising_rewrite_discarded() -> None:
    """A rewrite that raises distance is discarded; the original is kept."""
    fingerprint = _fingerprint()
    config = _eager_config()
    baseline = run_voice_fidelity_guard(
        _TROPEY, fingerprint=fingerprint, config=config, no_llm=True
    )
    # Fires many more features than the baseline, so it scans strictly higher.
    worse = (
        _TROPEY + " It is not only vibrant but also profound. As of my last "
        "knowledge update, it serves as a testament. In summary, the red, white, "
        "and blue significance is important to note. Not only does it delve, but "
        "it also boasts a tapestry."
    )
    llm, _ = _counting_llm(worse)
    report = run_voice_fidelity_guard(
        _TROPEY, fingerprint=fingerprint, config=config, llm=llm
    )
    # The lower-distance (original sanitized) version wins.
    assert report.voice_distance == baseline.voice_distance
    assert "As of my last" not in report.body


def test_grounding_dropping_rewrite_rejected() -> None:
    """A lower-distance rewrite that fails the grounding check is rejected."""
    fingerprint = _fingerprint()
    config = _eager_config()
    baseline = run_voice_fidelity_guard(
        _TROPEY, fingerprint=fingerprint, config=config, no_llm=True
    )
    llm, _ = _counting_llm(_PLAIN)
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=fingerprint,
        config=config,
        llm=llm,
        grounding_check=lambda _body: False,
    )
    # Even though _PLAIN scores lower, dropping grounding vetoes it.
    assert report.voice_distance == baseline.voice_distance
    assert "tapestry" in report.body


def test_grounding_passing_rewrite_accepted() -> None:
    """A lower-distance rewrite that passes the grounding check is accepted."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=_eager_config(),
        llm=lambda _p: _PLAIN,
        grounding_check=lambda _body: True,
    )
    assert report.passes == 1
    assert "tapestry" not in report.body


def test_revision_passes_capped() -> None:
    """The loop never exceeds ``max_revision_passes`` even if still divergent."""
    # Each rewrite improves but never reaches zero distance, and the ceiling
    # is 0 so the loop always "wants" another pass — the cap must stop it.
    llm, calls = _counting_llm(
        "Additionally a. Additionally b.",
        "Additionally a.",
        _PLAIN,
    )
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.0, max_revision_passes=2),
        llm=llm,
    )
    assert calls["n"] == 2
    assert report.passes == 2


def test_disabled_config_returns_body_unchanged() -> None:
    """When the subsystem is disabled the guard is a no-op."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(enabled=False),
        llm=lambda _p: _PLAIN,
    )
    assert report.body == _TROPEY
    assert report.voice_distance == 0.0
    assert report.findings == ()
    assert report.passes == 0


def test_under_ceiling_skips_rewrite() -> None:
    """A draft already within the ceiling is not rewritten."""
    llm, calls = _counting_llm(_PLAIN)
    report = run_voice_fidelity_guard(
        _PLAIN,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.35, max_revision_passes=2),
        llm=llm,
    )
    assert calls["n"] == 0
    assert report.passes == 0


def test_summary_line_wording() -> None:
    """The stderr summary names the status and the measured distance."""
    report = VoiceFidelityReport(
        body="x",
        voice_distance=0.22,
        findings=(),
        thin_fingerprint=False,
        passes=1,
        status="rewritten",
    )
    assert report.summary_line() == (
        "voice-fidelity: rewritten — distance 0.22 "
        "(0 residual divergences) — see frontmatter"
    )


def test_frontmatter_stamps_distance_and_findings() -> None:
    """The report serialises voice_distance and per-finding entries."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=_eager_config(),
        no_llm=True,
    )
    payload = report.to_frontmatter()
    assert "voice_distance" in payload
    assert isinstance(payload["voice_distance"], float)
    findings = payload.get("voice_findings")
    assert isinstance(findings, list)
    assert findings  # the tropey body produced at least one finding
    entry = findings[0]
    assert {"tell_id", "feature_key", "direction", "message"} <= set(entry)


def test_build_frontmatter_skips_empty() -> None:
    """A clean run with no distance/findings stamps nothing misleading."""
    assert build_voice_fidelity_frontmatter(voice_distance=None, findings=()) == {}


def test_rewrite_prompt_orders_targets_divergences_ask_draft() -> None:
    """The rewrite prompt carries targets, divergences, ask, and draft in order."""
    report = scan(_TROPEY, fingerprint=_fingerprint(), config=AIStyleConfig())
    assert report.findings  # the tropey body must surface divergences to repair
    preamble = "## Voice targets\nuses em-dashes freely"
    prompt = build_voice_rewrite_prompt(_TROPEY, report, style_preamble=preamble)
    assert preamble in prompt
    assert "## Voice divergences to repair" in prompt
    assert "## Ask" in prompt
    assert f"## Draft\n{_TROPEY}" in prompt
    # The rewrite must move toward voice without fabricating or dropping content.
    assert "Do not invent facts" in prompt
    assert (
        prompt.index(preamble)
        < prompt.index("## Voice divergences to repair")
        < prompt.index("## Ask")
        < prompt.index("## Draft")
    )


def test_rewrite_prompt_omits_empty_blocks() -> None:
    """With no preamble and no findings, only the ask + draft remain."""
    report = scan(_PLAIN, fingerprint=_fingerprint(), config=AIStyleConfig())
    assert not report.findings
    prompt = build_voice_rewrite_prompt(_PLAIN, report)
    assert "## Voice targets" not in prompt
    assert "## Voice divergences to repair" not in prompt
    assert prompt.startswith("## Ask")
    assert f"## Draft\n{_PLAIN}" in prompt
