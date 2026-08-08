"""Vault-relative config resolution for ``creek compost scan`` (#882 review).

Kept apart from ``tests/test_cli_compost_scan.py`` deliberately: that module
stubs ``_build_compost_similarity_fn`` wholesale via an autouse fixture, which
is right for testing CLI wiring but means the real exemplar-loading path is
never exercised there. These tests run that real path with only the
model-loading pieces replaced.

Two config fields are covered, both of which ``creek compost scan`` is the
first code in the repo to consume:

* ``compost.exemplars_relpath`` is documented "vault-relative", like its
  sibling ``review_queue_relpath``. Resolving it against the process CWD
  instead would crash from an unrelated directory — or, worse, silently load
  whatever file happened to sit at that relative path.
* ``compost.llm_verification`` is the config-level form of ``--no-llm``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from creek.generate.compost_embedding import CompostExemplar

runner = CliRunner()

_EXEMPLARS_RELPATH = "00-Creek-Meta/compost-exemplars.yaml"
"""Where these tests park the vault's custom exemplar set."""

_SENTINEL_TITLE = "vault-local exemplar"
"""Title proving the exemplars came from the vault copy, not the packaged one."""


class _StubLinker:
    """Stand-in for ``EmbeddingLinker`` so no sentence-transformers model loads."""

    def __init__(self, *, config: object) -> None:
        """Accept and ignore the embeddings config."""
        del config


def _write_vault(tmp_path: Path, *, llm_verification: bool = True) -> Path:
    """Build a vault with a custom exemplar set and a matching config.

    Args:
        tmp_path: pytest temporary directory.
        llm_verification: Value written to ``compost.llm_verification``.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for rel in ("01-Fragments", "02-Threads", "00-Creek-Meta", "10-Liminal/Compost"):
        (vault / rel).mkdir(parents=True, exist_ok=True)

    (vault / _EXEMPLARS_RELPATH).write_text(
        yaml.safe_dump(
            [
                {
                    "title": _SENTINEL_TITLE,
                    "body": "I am setting this project down for good.",
                    "texture": "releasing",
                    "rationale": "Explicit release of an ongoing project.",
                },
            ],
        ),
        encoding="utf-8",
    )
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        yaml.safe_dump(
            {
                "vault_path": str(vault),
                "compost": {
                    "exemplars_relpath": _EXEMPLARS_RELPATH,
                    "llm_verification": llm_verification,
                },
            },
        ),
        encoding="utf-8",
    )
    return vault


@pytest.fixture()
def captured_exemplars(monkeypatch: pytest.MonkeyPatch) -> list[CompostExemplar]:
    """Capture the exemplars handed to ``make_similarity_fn``, model-free."""
    captured: list[CompostExemplar] = []

    def _fake_make_similarity_fn(
        exemplars: Sequence[CompostExemplar],
        linker: object,
    ) -> Callable[[str], float]:
        del linker
        captured.extend(exemplars)
        return lambda _text: 0.0

    monkeypatch.setattr("creek.link.embeddings.EmbeddingLinker", _StubLinker)
    monkeypatch.setattr(
        "creek.generate.compost_embedding.make_similarity_fn",
        _fake_make_similarity_fn,
    )
    return captured


def test_exemplars_relpath_resolves_against_the_vault_not_the_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_exemplars: list[CompostExemplar],
) -> None:
    """A vault-relative exemplar path loads regardless of where the scan is run.

    ``CompostConfig.exemplars_relpath`` documents itself as vault-relative, and
    its sibling ``review_queue_relpath`` is joined with the vault root
    everywhere else. Resolving this one against the process CWD makes the
    command's behaviour depend on the operator's shell location: it raises
    ``FileNotFoundError`` from an unrelated directory, or silently loads a
    same-named file that happens to live there.
    """
    vault = _write_vault(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = runner.invoke(
        app,
        ["compost", "scan", "--vault", str(vault), "--no-llm"],
    )

    assert result.exit_code == 0, result.output
    assert [e.title for e in captured_exemplars] == [_SENTINEL_TITLE]


def test_llm_verification_false_in_config_skips_the_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_exemplars: list[CompostExemplar],
) -> None:
    """``compost.llm_verification: false`` is honoured without ``--no-llm``.

    The field documents itself as "when ``False``, the embedding gate alone
    decides acceptance". A ``scan`` that ignored it would build a provider —
    and refuse the run when none is available — despite the operator having
    already declared they want an offline pass.
    """
    vault = _write_vault(tmp_path, llm_verification=False)

    def _explode(_cfg: object) -> object:
        msg = "build_provider must not be called when llm_verification is false"
        raise AssertionError(msg)

    monkeypatch.setattr("creek.classify.llm.build_provider", _explode)

    result = runner.invoke(app, ["compost", "scan", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert captured_exemplars, "the scan never built the embedding gate"
