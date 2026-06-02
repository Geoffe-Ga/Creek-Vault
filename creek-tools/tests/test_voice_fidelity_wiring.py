"""Voice-fidelity is wired through the conductor's primary path (#506).

The reflection node's ``check_voice_fidelity`` was implemented and unit-tested
under #471/#473 but stayed *dormant* in :meth:`Conductor.run`: the review call
never received the owner's persisted ``VoiceFingerprint`` or the
``AIStyleConfig``, so the check always short-circuited. These tests pin the
wiring: ``run`` loads the owner's fingerprint + config once, threads them into
``reflection.review``, and surfaces ``voice_fidelity`` findings on the draft —
while degrading gracefully (and identically to before) when no fingerprint has
been built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.agents import default_specialists
from creek.author.conductor import Conductor
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent
from creek.config import AIStyleConfig
from creek.generate.ai_style import (
    FeatureStat,
    VoiceFingerprint,
    save_fingerprint,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from creek.author.models import EvidenceBundle, Medium
    from creek.models import MediumContract


def _seed_fragment(vault: Path, frag_id: str, title: str) -> None:
    """Write a minimal self-authored fragment so the specialists have a corpus."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )


def _persist_fingerprint(vault: Path, fragment_count: int = 5) -> VoiceFingerprint:
    """Persist a non-empty voice fingerprint under *vault* and return it.

    The fingerprint carries an em-dash feature stat so it is a genuine,
    non-empty fingerprint (``fragment_count`` >= 1) that ``check_voice_fidelity``
    will measure against rather than skip.

    Args:
        vault: The vault root to persist under.
        fragment_count: How many fragments the fingerprint claims to rest on.

    Returns:
        The persisted fingerprint.
    """
    fingerprint = VoiceFingerprint(
        features={"em_dash_rate": FeatureStat(rate=4.0, support=fragment_count)},
        fragment_count=fragment_count,
    )
    save_fingerprint(fingerprint, vault, AIStyleConfig())
    return fingerprint


class _FixedVoice:
    """A voice renderer that always returns a chosen body (test double)."""

    last_usage: dict[str, int] | None = None

    def __init__(self, body: str) -> None:
        """Store the body every :meth:`render` will return.

        Args:
            body: The draft body to emit on each render.
        """
        self._body = body

    def render(
        self,
        query: str,
        evidence: EvidenceBundle,
        vault: Path | None = None,
        *,
        medium: Medium | None = None,
        contract: MediumContract | None = None,
    ) -> str:
        """Return the fixed body, ignoring the query and evidence."""
        del query, evidence, vault, medium, contract
        return self._body


def test_run_surfaces_voice_fidelity_finding_via_real_scanner(tmp_path: Path) -> None:
    """A divergent body fires a real ``voice_fidelity`` finding through ``run``.

    The draft carries an unfilled ``2025-xx-xx`` placeholder date — the
    ``placeholder_date`` mechanical tell has a fixed-0 margin and fires on any
    occurrence regardless of fingerprint, so the *real* scanner (not a mock)
    produces the divergence. This proves ``review`` received a non-None
    fingerprint + config and the finding reached the draft.
    """
    _seed_fragment(tmp_path, "frag-a", "Alpha note about q")
    _persist_fingerprint(tmp_path)
    # The deterministic VoiceAgent path would never emit a placeholder date, so
    # a fixed voice double supplies the divergent body the scanner flags.
    divergent = "A claim about q, citing source 2025-xx-xx for the figure."
    conductor = Conductor(
        specialists=default_specialists(),
        voice=_FixedVoice(divergent),
        reflection=ReflectionNode(),
        max_rounds=1,
    )

    draft = conductor.run(medium="research", query="q", vault=tmp_path)

    assert any(f.dimension == "voice_fidelity" for f in draft.findings)


def test_run_dormant_without_built_fingerprint(tmp_path: Path) -> None:
    """No persisted fingerprint → no ``voice_fidelity`` finding and no crash.

    Behaviour is identical to before #506: the empty-fingerprint path returns
    ``None``, so ``check_voice_fidelity`` skips entirely.
    """
    _seed_fragment(tmp_path, "frag-a", "Alpha note about q")
    # Same divergent body — but with NO fingerprint persisted the voice check
    # must stay dormant.
    divergent = "A claim about q, citing source 2025-xx-xx for the figure."
    conductor = Conductor(
        specialists=default_specialists(),
        voice=_FixedVoice(divergent),
        reflection=ReflectionNode(),
        max_rounds=1,
    )

    draft = conductor.run(medium="research", query="q", vault=tmp_path)

    assert all(f.dimension != "voice_fidelity" for f in draft.findings)


def test_load_voice_inputs_returns_fingerprint_when_persisted(tmp_path: Path) -> None:
    """``_load_voice_inputs`` returns ``(config, fingerprint)`` for a real one."""
    persisted = _persist_fingerprint(tmp_path, fragment_count=3)
    conductor = Conductor(
        specialists=[],
        voice=VoiceAgent(),
        reflection=ReflectionNode(),
        max_rounds=1,
    )

    config, fingerprint = conductor._load_voice_inputs(tmp_path)

    assert isinstance(config, AIStyleConfig)
    assert fingerprint is not None
    assert fingerprint.fragment_count == persisted.fragment_count


def test_load_voice_inputs_returns_none_fingerprint_when_absent(tmp_path: Path) -> None:
    """``_load_voice_inputs`` returns ``(config, None)`` when no fingerprint exists."""
    conductor = Conductor(
        specialists=[],
        voice=VoiceAgent(),
        reflection=ReflectionNode(),
        max_rounds=1,
    )

    config, fingerprint = conductor._load_voice_inputs(tmp_path)

    assert isinstance(config, AIStyleConfig)
    assert fingerprint is None


def test_load_voice_inputs_degrades_on_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config that fails Pydantic validation degrades to ``(None, None)``.

    `ValidationError` is not a `ValueError` subclass in Pydantic v2, so the desk
    must name it explicitly; otherwise a malformed `creek_config.yaml` would
    crash the conductor (#506 review).
    """
    config_path = tmp_path / "00-Creek-Meta" / "creek_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # ``max_author_rounds`` is bound [1, 10]; 99 fails validation.
    config_path.write_text("author:\n  max_author_rounds: 99\n", encoding="utf-8")
    # Pin the config path so a leaked CREEK_CONFIG can't shadow this fixture.
    monkeypatch.setenv("CREEK_CONFIG", str(config_path))
    conductor = Conductor(
        specialists=[],
        voice=VoiceAgent(),
        reflection=ReflectionNode(),
        max_rounds=1,
    )

    assert conductor._load_voice_inputs(tmp_path) == (None, None)
