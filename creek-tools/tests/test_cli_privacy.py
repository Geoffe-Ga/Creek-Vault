"""Tests for the ``--include-tier`` CLI flag and audit-trail wiring.

The flag must be available on every generation surface (``mine``,
``draft``, ``report``, ``skills``) and must produce an audit-log entry
whenever the operator elevates inclusion above the default tier.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
from typer.testing import CliRunner

from creek.audit import AuditLog
from creek.classify.privacy_filter import PRIVACY_AUDIT_RELPATH
from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

# Click 8 + Rich emit ANSI styling around dashes when stdout is detected
# as a terminal (CI runners differ from local CliRunner here), which
# breaks naive substring searches like ``"--include-tier" in output``
# because the dashes are split across reset codes. Strip ANSI before
# asserting on help text.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def _plain(text: str) -> str:
    """Return *text* with ANSI escape sequences stripped."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Vault fixture
# ---------------------------------------------------------------------------


def _make_mining_vault(tmp_path: Path) -> Path:
    """Build a vault with an intimate journal fragment and a thread terminus.

    The thread terminus seed surfaces under all strategies regardless of
    the privacy override, so we can verify the seed list contents based
    on whether the intimate fragment is included.
    """
    vault = tmp_path / "vault"
    for sub in (
        "00-Creek-Meta/audit",
        "01-Fragments/Journal",
        "02-Threads/Active",
        "03-Eddies",
        "10-Liminal/Unnamed",
    ):
        (vault / sub).mkdir(parents=True)

    intimate_id = "frag-intimate"
    intimate_path = vault / "01-Fragments" / "Journal" / "intimate.md"
    intimate_meta: dict[str, object] = {
        "id": intimate_id,
        "title": "Recovery reflections",
        "type": "fragment",
        "source": {"platform": "journal", "author": "self", "original_file": "j.md"},
        "created": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        "frequency": {"primary": "F3", "secondary": []},
        "wavelength": {
            "phase": "rising",
            "mode": "inhabit",
            "orientation": "do",
            "dosage": "medicine",
            "color": "orange",
            "descriptor": "bright",
        },
        "voice": {"voice_register": "confessional", "confidence": "conviction"},
        "privacy_tier": "intimate",
    }
    intimate_path.write_text(
        frontmatter.dumps(
            frontmatter.Post(content="intimate body", **intimate_meta),
        ),
        encoding="utf-8",
    )

    thread_path = vault / "02-Threads" / "Active" / "Waves.md"
    thread_meta: dict[str, object] = {
        "id": "thread-waves",
        "title": "Waves",
        "type": "thread",
        "status": "active",
        "fragment_count": 50,
        "frequency_affinity": ["F3"],
    }
    thread_path.write_text(
        frontmatter.dumps(frontmatter.Post(content="", **thread_meta)),
        encoding="utf-8",
    )

    return vault


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_mine_default_excludes_intimate_no_audit(tmp_path: Path) -> None:
    """Default ``creek mine`` excludes intimate and writes no audit entry."""
    vault = _make_mining_vault(tmp_path)

    result = runner.invoke(app, ["mine", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert "Recovery reflections" not in result.output

    audit_path = vault / PRIVACY_AUDIT_RELPATH
    assert not audit_path.exists()


def test_mine_with_include_tier_writes_audit(tmp_path: Path) -> None:
    """``--include-tier intimate`` writes a privacy audit entry."""
    vault = _make_mining_vault(tmp_path)

    result = runner.invoke(
        app,
        [
            "mine",
            "--vault",
            str(vault),
            "--include-tier",
            "intimate",
        ],
    )
    assert result.exit_code == 0, result.output

    audit_path = vault / PRIVACY_AUDIT_RELPATH
    assert audit_path.exists()
    entries = list(AuditLog(audit_path).read())
    assert len(entries) == 1
    assert entries[0]["command"] == "mine"
    assert entries[0]["include_tier"] == "intimate"


def test_mine_rejects_unknown_include_tier(tmp_path: Path) -> None:
    """A bogus ``--include-tier`` value exits with code 2."""
    vault = _make_mining_vault(tmp_path)

    result = runner.invoke(
        app,
        ["mine", "--vault", str(vault), "--include-tier", "leaky"],
    )
    assert result.exit_code == 2
    assert "include-tier" in result.output


def test_mine_help_mentions_include_tier() -> None:
    """``creek mine --help`` advertises the new flag."""
    result = runner.invoke(app, ["mine", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


def test_draft_help_mentions_include_tier() -> None:
    """``creek draft --help`` advertises the new flag."""
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


def test_report_help_mentions_include_tier() -> None:
    """``creek report --help`` advertises the new flag."""
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


def test_skills_help_mentions_include_tier() -> None:
    """``creek skills --help`` advertises the new flag."""
    result = runner.invoke(app, ["skills", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)
