"""The de-slop voice-fidelity guard runs faithfully and loudly.

Pins the contract: a distinct ``voice_distance_target`` drives the rewrite loop
(so mannered drafts below the permissive ceiling are still stripped), every
outcome stamps a machine-readable ``voice_guard_status`` (no silent no-ops),
the real ``save_draft`` path strips planted tells, and the diagnostic surfaces
the recorded status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.config import AIStyleConfig
from creek.generate.ai_style.guard import (
    VOICE_GUARD_STATUS_KEY,
    run_voice_fidelity_guard,
)
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.drafts import Draft, DraftGenerator
from creek.generate.voice_authenticity import build_voice_authenticity_report

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TROPEY = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant and multifaceted nuances. "
    "Furthermore, the landscape stands as a testament to its significance."
)
_PLAIN = "The cat sat by the window. Rain fell on the roof. I made tea and read."


def _fingerprint(*, fragments: int = 20) -> VoiceFingerprint:
    """A non-thin fingerprint whose measured rates are all ~zero."""
    return VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.0, support=fragments)},
        fragment_count=fragments,
    )


def _const_llm(text: str) -> Callable[[str], str]:
    """An LLM stub that returns *text* for any prompt."""
    return lambda _prompt: text


# --- config: voice_distance_target ------------------------------------------


def test_target_defaults_below_ceiling() -> None:
    """The default target is distinct from and below the accepted ceiling."""
    config = AIStyleConfig()
    assert config.voice_distance_target == 0.25
    assert config.voice_distance_target < config.voice_distance_upper


def test_target_clamped_to_ceiling() -> None:
    """A ceiling below the default target clamps the target down to it."""
    config = AIStyleConfig(voice_distance_upper=0.1)
    assert config.voice_distance_target == 0.1


def test_explicit_target_below_ceiling_is_preserved() -> None:
    """An explicit target at or below the ceiling is kept as-is."""
    config = AIStyleConfig(voice_distance_upper=0.4, voice_distance_target=0.3)
    assert config.voice_distance_target == 0.3


# --- guard status -----------------------------------------------------------


def test_status_rewritten() -> None:
    """A rewrite pass records ``rewritten``."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.001),
        llm=_const_llm(_PLAIN),
    )
    assert report.passes == 1
    assert report.status == "rewritten"


def test_status_no_llm() -> None:
    """Measure-only mode records ``measured_only:no_llm``."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.001),
        llm=_const_llm(_PLAIN),
        no_llm=True,
    )
    assert report.status == "measured_only:no_llm"


def test_status_no_rewriter() -> None:
    """A missing rewrite LLM records ``measured_only:no_rewriter``."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.001),
        llm=None,
    )
    assert report.status == "measured_only:no_rewriter"


def test_status_disabled() -> None:
    """A disabled subsystem records ``skipped:disabled``."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(enabled=False),
    )
    assert report.status == "skipped:disabled"


def test_status_below_target() -> None:
    """A clean draft already within target records ``measured_only:below_target``."""
    report = run_voice_fidelity_guard(
        _PLAIN,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(),
        llm=_const_llm(_PLAIN),
    )
    assert report.passes == 0
    assert report.status == "measured_only:below_target"


def test_frontmatter_always_stamps_status() -> None:
    """``to_frontmatter`` always carries the guard status."""
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.001),
        llm=_const_llm(_PLAIN),
    )
    assert report.to_frontmatter()[VOICE_GUARD_STATUS_KEY] == "rewritten"


def test_target_drives_rewrite_below_ceiling() -> None:
    """A draft below the permissive ceiling is still rewritten toward target.

    With a high ceiling but a low target, a mannered draft whose distance sits
    between the two is now stripped — the rewrite loop is gated on the target,
    not the ceiling (the pre-target behaviour would have left it merely measured).
    """
    report = run_voice_fidelity_guard(
        _TROPEY,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(voice_distance_upper=0.9, voice_distance_target=0.01),
        llm=_const_llm(_PLAIN),
    )
    assert report.passes == 1
    assert report.status == "rewritten"
    assert "tapestry" not in report.body


# --- draft-path skip reasons (loud, never silent) ---------------------------


def _generator(
    tmp_path: Path,
    *,
    fingerprint: VoiceFingerprint | None,
    config: AIStyleConfig | None,
) -> DraftGenerator:
    """Build a DraftGenerator with the given voice-guard wiring."""
    return DraftGenerator(
        llm=_const_llm(_PLAIN),
        skills_root=tmp_path / "skills",
        fingerprint=fingerprint,
        ai_style_config=config,
    )


def test_apply_voice_fidelity_skipped_disabled(tmp_path: Path) -> None:
    """A disabled subsystem yields a loud ``skipped:disabled`` status."""
    gen = _generator(
        tmp_path,
        fingerprint=_fingerprint(),
        config=AIStyleConfig(enabled=False),
    )
    _body, fields = gen._apply_voice_fidelity(
        _TROPEY,
        vault_path=tmp_path,
        source_fragments=(),
    )
    assert fields[VOICE_GUARD_STATUS_KEY] == "skipped:disabled"


def test_apply_voice_fidelity_skipped_no_fingerprint(tmp_path: Path) -> None:
    """A missing fingerprint yields a loud ``skipped:no_fingerprint`` status."""
    gen = _generator(tmp_path, fingerprint=None, config=AIStyleConfig())
    _body, fields = gen._apply_voice_fidelity(
        _TROPEY,
        vault_path=tmp_path,
        source_fragments=(),
    )
    assert fields[VOICE_GUARD_STATUS_KEY] == "skipped:no_fingerprint"


def test_apply_voice_fidelity_skipped_thin(tmp_path: Path) -> None:
    """A thin fingerprint is reported, never silently swallowed."""
    gen = _generator(
        tmp_path,
        fingerprint=_fingerprint(fragments=1),
        config=AIStyleConfig(),
    )
    _body, fields = gen._apply_voice_fidelity(
        _TROPEY,
        vault_path=tmp_path,
        source_fragments=(),
    )
    assert fields[VOICE_GUARD_STATUS_KEY] == "skipped:thin_fingerprint"


# --- integration: real save_draft path --------------------------------------


def _draft(body: str) -> Draft:
    """Build a minimal Draft carrying *body*."""
    return Draft(
        title="Tells on parade",
        body=body,
        idea_strategy="thread_terminus",
        source_fragments=(),
        threads=(),
        eddies=(),
        skill_stack=(),
        prompt="prompt",
        generated_date=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )


def test_real_save_draft_strips_planted_tells(tmp_path: Path) -> None:
    """An essay seeded with tells comes out of save_draft with them removed."""
    vault = tmp_path / "vault"
    vault.mkdir()
    gen = DraftGenerator(
        llm=_const_llm(_PLAIN),
        skills_root=tmp_path / "skills",
        fingerprint=_fingerprint(),
        ai_style_config=AIStyleConfig(voice_distance_upper=0.001),
    )

    path = gen.save_draft(_draft(_TROPEY), vault)
    text = path.read_text(encoding="utf-8")

    assert "rich tapestry" not in text
    assert "delve" not in text
    post = frontmatter.loads(text)
    assert post[VOICE_GUARD_STATUS_KEY] == "rewritten"


# --- diagnostic surfacing ---------------------------------------------------


def test_diagnostic_surfaces_guard_status(tmp_path: Path) -> None:
    """The voice-authenticity ``deslop.status`` reflects the recorded status."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    draft = tmp_path / "draft.md"
    post = frontmatter.Post(
        "Body.",
        **{VOICE_GUARD_STATUS_KEY: "rewritten", "voice_distance": 0.12},
    )
    draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    report = build_voice_authenticity_report(vault, draft_path=draft)

    assert report.deslop is not None
    assert report.deslop.attested is True
    assert report.deslop.status == "rewritten"


def test_diagnostic_surfaces_skip_status(tmp_path: Path) -> None:
    """A skipped guard is still attested in the diagnostic, never swallowed."""
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    draft = tmp_path / "draft.md"
    post = frontmatter.Post(
        "Body.", **{VOICE_GUARD_STATUS_KEY: "skipped:thin_fingerprint"}
    )
    draft.write_text(frontmatter.dumps(post), encoding="utf-8")

    deslop = build_voice_authenticity_report(vault, draft_path=draft).deslop

    assert deslop is not None
    assert deslop.attested is True
    assert deslop.status == "skipped:thin_fingerprint"
