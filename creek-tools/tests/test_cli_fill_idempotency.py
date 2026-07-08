"""Real-vault (un-mocked) idempotency audit for ``creek fill`` (#736).

``test_cli_fill.py`` covers orchestration with every step mocked; this proves the
docstring claim that *"a second ``creek fill`` is a clean no-op"* end-to-end:
build a real scaffolded vault with classified fragments, run the actual pipeline
steps twice, and assert every markdown note is byte-for-byte identical on the
second run. The conftest sentence-transformer mock is deterministic within a
process (seeded by ``hash(text)``), so embeddings are stable across both runs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)
from creek.time import now_la
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_TOP_LEVEL = (
    "00-Creek-Meta",
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
)
_FREQ_SUBDIRS = (
    "F1-Agency",
    "F2-Receptivity",
    "F3-Self-Love-Power",
    "F4-Community-Love",
    "F5-Achievism",
    "F6-Pluralism",
    "F7-Integration",
    "F8-True-Self",
    "F9-Unity",
    "F10-Emptiness",
)


def _scaffold(tmp_path: Path) -> Path:
    """Create a full vault folder tree the fill generators write into."""
    vault = tmp_path / "vault"
    for folder in _TOP_LEVEL:
        (vault / folder).mkdir(parents=True)
    for sub in _FREQ_SUBDIRS:
        (vault / "06-Frequencies" / sub).mkdir()
    return vault


def _seed(vault: Path) -> None:
    """Write classified fragments that populate several fill-managed folders."""
    base = now_la() - timedelta(days=1)
    # Three F5 fragments within the window → a thread + frequency/wavelength data.
    for i in range(3):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-fill00000{i:03d}",
                title=f"Achievism note {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                authored_at=base,
                frequency=FrequencyClassification(primary=Frequency.F5),
                wavelength=WavelengthClassification(phase=Phase.RISING),
            ),
            body=f"Shared achievism theme, entry {i}.",
        )
    # A cross-source, far-dated fragment (synchronicity candidate material).
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-fill000aa1",
            title="a different lens on the same idea",
            source=FragmentSource(platform=SourcePlatform.DISCORD),
            authored_at=datetime(2025, 1, 5, tzinfo=UTC),
            frequency=FrequencyClassification(primary=Frequency.F6),
            wavelength=WavelengthClassification(phase=Phase.WITHDRAWAL),
        ),
        body="Shared achievism theme, a later echo.",
    )


def _markdown_digest(vault: Path) -> dict[str, str]:
    """Map each markdown note's vault-relative path to a sha256 of its bytes."""
    return {
        str(p.relative_to(vault)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(vault.rglob("*.md"))
        if not p.name.startswith(".")
    }


def test_second_creek_fill_is_byte_stable(tmp_path: Path) -> None:
    """A second ``creek fill`` mutates no markdown note (real, un-mocked steps)."""
    vault = _scaffold(tmp_path)
    _seed(vault)

    first = runner.invoke(app, ["fill", "--vault", str(vault)])
    assert first.exit_code == 0, first.output
    snapshot = _markdown_digest(vault)
    assert snapshot, "expected the first fill to populate markdown notes"

    second = runner.invoke(app, ["fill", "--vault", str(vault)])
    assert second.exit_code == 0, second.output
    after = _markdown_digest(vault)

    assert set(after) == set(snapshot), (
        f"second fill added/removed notes: {set(after) ^ set(snapshot)}"
    )
    changed = [path for path, digest in snapshot.items() if after.get(path) != digest]
    assert not changed, f"second fill rewrote notes (not idempotent): {changed}"
