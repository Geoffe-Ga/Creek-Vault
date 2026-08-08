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
import pytest
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


def test_draft_help_omits_internal_issue_tags() -> None:
    """``creek draft --help`` advertises its flags without leaking any FEAT tag."""
    result = runner.invoke(app, ["draft", "--help"])
    assert result.exit_code == 0
    plain = _plain(result.output)
    assert "--no-llm" in plain
    assert "--seed-fragment" in plain
    # Internal issue references belong in docstrings/commits, not user --help —
    # guard the whole class (FEAT-040, FEAT-032, …), not just one tag.
    assert "FEAT-" not in plain


@pytest.mark.parametrize(
    "command",
    [
        ["init", "--help"],
        ["process", "--help"],
        ["ingest", "--help"],
        ["redact", "--help"],
        ["classify", "--help"],
        ["link", "--help"],
        ["compile", "--help"],
        ["voice-check", "--help"],
        ["voice-authenticity", "--help"],
        ["report", "--help"],
        ["state", "--help"],
        ["state-budget", "--help"],
        ["lint", "--help"],
        ["review", "--help"],
        ["gdrive", "--help"],
        ["mine", "--help"],
        ["draft", "--help"],
        ["author", "--help"],
        ["save", "--help"],
        ["clean", "--help"],
        ["purge", "--help"],
        ["skills", "--help"],
        ["compost", "--help"],
    ],
)
def test_subcommand_help_omits_internal_issue_tags(command: list[str]) -> None:
    """No subcommand's user-facing --help leaks an internal FEAT-* tag (#445).

    Issue references belong in internal docstrings/commits, not the help a user
    reads. Typer renders both option ``help=`` strings and the command
    docstring into --help, so the guard covers every rendered surface.
    """
    result = runner.invoke(app, command)
    assert result.exit_code == 0, result.output
    assert "FEAT-" not in _plain(result.output)


def test_report_help_mentions_include_tier() -> None:
    """``creek report --help`` advertises the new flag."""
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


def test_skills_help_mentions_include_tier() -> None:
    """``creek skills generate --help`` advertises the privacy-tier flag."""
    result = runner.invoke(app, ["skills", "generate", "--help"])
    assert result.exit_code == 0
    assert "--include-tier" in _plain(result.output)


def test_author_with_include_tier_writes_audit(tmp_path: Path) -> None:
    """``creek author --include-tier intimate`` writes a privacy audit entry (#660)."""
    vault = _make_mining_vault(tmp_path)

    result = runner.invoke(
        app,
        [
            "author",
            "--vault",
            str(vault),
            "--query",
            "what is the thread about",
            "--include-tier",
            "intimate",
        ],
    )
    assert result.exit_code == 0, result.output

    audit_path = vault / PRIVACY_AUDIT_RELPATH
    assert audit_path.exists()
    entries = list(AuditLog(audit_path).read())
    assert any(
        e["command"] == "author" and e["include_tier"] == "intimate" for e in entries
    )


def test_author_default_writes_no_audit(tmp_path: Path) -> None:
    """Default ``creek author`` (OPEN ceiling) writes no privacy audit entry."""
    vault = _make_mining_vault(tmp_path)

    result = runner.invoke(
        app,
        ["author", "--vault", str(vault), "--query", "what is the thread about"],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / PRIVACY_AUDIT_RELPATH).exists()


# ---------------------------------------------------------------------------
# Issue #879 — ``report --type voice`` now writes vault content
# ---------------------------------------------------------------------------
#
# ``report --type voice`` began copying fragment bodies into
# ``07-Voice/Register-Samples/`` in #879, so the ceiling it runs under is
# now visible in the artifact rather than only in a rendered profile. The
# audit polarity that governs it is ``report``'s, not ``mine``'s, and it
# runs backwards from every other surface in this module: on ``report`` an
# ABSENT ``--include-tier`` means UNFILTERED, and the flag NARROWS.


def _make_voice_vault(tmp_path: Path) -> Path:
    """Build a vault with one exemplar-eligible confessional fragment."""
    vault = tmp_path / "vault"
    folder = vault / "01-Fragments" / "Journal"
    folder.mkdir(parents=True)
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    (folder / "frag-voice.md").write_text(
        '---\ntype: fragment\nid: frag-voice\ntitle: "A settled note"\n'
        "source:\n  platform: journal\n  author: self\n"
        "frequency:\n  primary: F5\n"
        "wavelength:\n  phase: rising\n  mode: express\n"
        "voice:\n  voice_register: confessional\n  confidence: conviction\n"
        "privacy_tier: open\n---\nThe creek of thought flows downstream.\n",
        encoding="utf-8",
    )
    return vault


def test_report_voice_with_include_tier_writes_audit(tmp_path: Path) -> None:
    """``report --type voice --include-tier personal`` records ``report.voice``.

    The ``command`` field has to carry the report *type*, not a bare
    ``report``: the entry is invocation-level (``fragment_ids`` is
    intentionally empty, because ``report`` iterates vault content itself
    rather than taking a caller-supplied id list), so the type string is
    the only thing that tells an operator which artifact was elevated.
    """
    vault = _make_voice_vault(tmp_path)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "voice",
            "--vault",
            str(vault),
            "--include-tier",
            "personal",
        ],
    )
    assert result.exit_code == 0, result.output

    audit_path = vault / PRIVACY_AUDIT_RELPATH
    assert audit_path.exists()
    entries = list(AuditLog(audit_path).read())
    assert len(entries) == 1
    assert entries[0]["command"] == "report.voice"
    assert entries[0]["include_tier"] == "personal"
    assert entries[0]["fragment_ids"] == []


def test_report_voice_without_include_tier_writes_no_audit(
    tmp_path: Path,
) -> None:
    """A bare ``report --type voice`` writes no privacy-override entry.

    An absent flag is not an elevation the operator performed, even though
    on ``report`` it is the *widest* scan available (#968). The entry
    records what was typed.
    """
    vault = _make_voice_vault(tmp_path)

    result = runner.invoke(
        app,
        ["report", "--type", "voice", "--vault", str(vault)],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / PRIVACY_AUDIT_RELPATH).exists()


def test_report_voice_include_tier_open_writes_no_audit(tmp_path: Path) -> None:
    """``--include-tier open`` records nothing: ``open`` does not elevate.

    The audit is gated on ``privacy_filter.override_elevates``, which
    returns ``False`` for both ``None`` and ``OPEN`` by construction, so on
    ``report`` — where the flag NARROWS — the widest scan available and the
    narrowest are alike unaudited and indistinguishable in the log.

    Worth pinning because the in-code ``NB (#968)`` note at ``creek/cli.py``
    used to claim the opposite, that a typed ``--include-tier open`` writes
    an entry. That comment is corrected as part of #879; whether the log
    *should* be able to tell "scanned unfiltered" apart from "narrowed to
    open" is a real gap in the audit trail, and is a separate change tracked
    as issue #1218.
    """
    vault = _make_voice_vault(tmp_path)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "voice",
            "--vault",
            str(vault),
            "--include-tier",
            "open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (vault / PRIVACY_AUDIT_RELPATH).exists()
