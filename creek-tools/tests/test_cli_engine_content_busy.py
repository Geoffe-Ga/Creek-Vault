"""Engine-backed CLI commands report a busy vault legibly (#2087).

``creek classify``, ``creek link`` and ``creek compile`` reach engines that
take the vault content-mutation lock themselves; ``creek fill --upgrade`` and
``creek process`` reach the same engines one step removed. Before #2087 each
let the engine's bare ``VaultLockTimeoutError`` escape as a traceback. Each now
prints the one-line busy message and exits 1 — while a lock timeout raised
*under* the engine's hold still propagates as itself, because by then the
engine may already have written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek._fslock import VaultLockTimeoutError
from creek.cli import app
from creek.config import CreekConfig
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON
from tests.helpers import write_fragment_file
from tests.test_mcp_content_mutation import (
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

runner = CliRunner()

_FRAGMENT_ID = "frag-engine-busy-2087"
"""The one fragment each command is pointed at."""

_BUSY_LINE = (
    f"{CONTENT_MUTATION_BUSY_REASON}: another Creek writer holds this vault; "
    "retry once it finishes."
)
"""The whole line the CLI prints for contention, as the operator reads it."""

_INNER_DETAIL = "id-index lock busy"
"""The message of a timeout raised under an engine's hold."""

_ENGINE_BODIES: dict[str, str] = {
    "classify": "creek.classify.classify_engine._run_classify_with_content_lock_held",
    "link": "creek.link.link_engine._run_link_with_content_lock_held",
    "compile": "creek.compile.engine._compile_to_vault_with_content_lock_held",
    "fill": "creek.classify.classify_engine._run_classify_with_content_lock_held",
}
"""Where each command's engine work runs once the lock is held."""


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def _seed_vault(tmp_path: Path) -> Path:
    """Write a vault holding one OPEN fragment and return its root."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id=_FRAGMENT_ID,
        title="Engine busy",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=vault, fragment=fragment, body="synthetic body")
    return vault


def _offer_an_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``creek fill`` find a ``rules`` -> LLM upgrade to apply."""
    monkeypatch.setattr(
        "creek.cli._detect_classify_upgrade", lambda _vault, _config: object()
    )


def _invoke(command: str, vault: Path) -> Result:
    """Run engine-backed *command* against *vault* the way an operator would."""
    args: dict[str, list[str]] = {
        "classify": ["classify", "--method", "rules"],
        "link": ["link", "--method", "temporal"],
        "compile": [
            "compile",
            _FRAGMENT_ID,
            "--target-id",
            "thread-engine-busy-2087",
            "--target-title",
            "Engine busy",
        ],
        "fill": ["fill", "--upgrade"],
    }
    return runner.invoke(app, [*args[command], "--vault", str(vault)])


_COMMANDS = pytest.mark.parametrize("command", ["classify", "link", "compile", "fill"])


@_COMMANDS
def test_a_busy_vault_is_one_line_and_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Contention at the engine's hold is the busy line, never a traceback."""
    vault = _seed_vault(tmp_path)
    before = _fragment_digests(vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    _offer_an_upgrade(monkeypatch)

    with _content_lock_held_elsewhere(vault):
        result = _invoke(command, vault)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert _fragment_digests(vault) == before


@_COMMANDS
def test_a_timeout_under_the_engine_hold_propagates_as_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Only acquisition is busy: an inner timeout is not translated (#2079).

    The engine body may have written before an inner lock timed out, so the
    CLI must not tell the operator the vault was untouched.
    """
    vault = _seed_vault(tmp_path)
    _offer_an_upgrade(monkeypatch)

    def _inner_times_out(**_kwargs: object) -> object:
        """Time out the way a contended inner lock does mid-pass."""
        raise VaultLockTimeoutError(_INNER_DETAIL)

    monkeypatch.setattr(_ENGINE_BODIES[command], _inner_times_out)

    result = _invoke(command, vault)

    assert isinstance(result.exception, VaultLockTimeoutError), result.output
    assert str(result.exception) == _INNER_DETAIL
    assert CONTENT_MUTATION_BUSY_REASON not in _flat(result.output)
    assert _content_lock_is_held(vault) is False


# --------------------------------------------------------------------------- #
# creek process: stage 5 links under the engine's own hold
# --------------------------------------------------------------------------- #


def _invoke_process(vault: Path, source: Path) -> Result:
    """Run ``creek process`` from *source* into *vault*, consent pre-given."""
    return runner.invoke(
        app,
        ["process", "--source", str(source), "--vault", str(vault), "--yes"],
    )


def _stage_5_link(vault: Path) -> None:
    """Run one real link stage the way ``Pipeline._run_linking`` does."""
    run_link(vault_path=vault, config=CreekConfig(), method="temporal", rebuild=False)


def test_creek_process_exits_1_legibly_when_its_link_stage_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage-5 contention is the busy line and exit 1 through the CLI.

    ``Pipeline.run`` is reduced to its link stage so the contention lands on
    the real :func:`~creek.link.link_engine.run_link` hold; stages 2-4 are
    #2086's concern and their writes are not modelled here.
    """
    vault = _seed_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    monkeypatch.setattr(
        "creek.pipeline.Pipeline.run",
        lambda _self, *, source_path, vault_path: _stage_5_link(vault_path),
    )

    with _content_lock_held_elsewhere(vault):
        result = _invoke_process(vault, source)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert "Files scanned" not in result.output


def test_a_timeout_under_creek_process_link_hold_propagates_as_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner timeout in stage 5 is the error it is, not a busy vault."""
    vault = _seed_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()

    def _inner_times_out(**_kwargs: object) -> object:
        """Time out the way a contended inner lock does mid-link."""
        raise VaultLockTimeoutError(_INNER_DETAIL)

    monkeypatch.setattr(_ENGINE_BODIES["link"], _inner_times_out)
    monkeypatch.setattr(
        "creek.pipeline.Pipeline.run",
        lambda _self, *, source_path, vault_path: _stage_5_link(vault_path),
    )

    result = _invoke_process(vault, source)

    assert isinstance(result.exception, VaultLockTimeoutError), result.output
    assert CONTENT_MUTATION_BUSY_REASON not in _flat(result.output)
