"""Shared test fixtures for creek-tools.

Provides auto-use fixtures that:

- mock the sentence-transformer model loading so tests never download
  models or require GPU access, and
- pin the terminal width so Rich/Typer CLI output renders deterministically
  regardless of the ambient terminal or whether stdout is a TTY (GAP-013).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# Pin a wide terminal so Rich/Typer never wrap CLI output mid-word. Rich
# defaults to 80 columns whenever stdout is not a TTY (pipes, fresh
# containers, CI), which wraps substrings the tests assert on (GAP-013).
_TEST_TERMINAL_COLUMNS = "200"
_TEST_TERMINAL_LINES = "50"

_DIMS = 384


@pytest.fixture(autouse=True)
def _pin_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin terminal width/height so Rich CLI output never wraps (GAP-013).

    Rich resolves its render width from the ``COLUMNS`` environment variable
    on every render (not just at ``Console`` construction), so setting it here
    deterministically controls output width for the module-level ``Console``
    in :mod:`creek.cli` and any other Rich/Typer output -- whether stdout is a
    real TTY or a pipe. ``monkeypatch`` restores the prior environment after
    each test.
    """
    monkeypatch.setenv("COLUMNS", _TEST_TERMINAL_COLUMNS)
    monkeypatch.setenv("LINES", _TEST_TERMINAL_LINES)


def _make_mock_model(dims: int = _DIMS) -> MagicMock:
    """Create a mock SentenceTransformer returning deterministic embeddings."""
    model = MagicMock()

    def _encode(
        sentences: str | list[str],
        show_progress_bar: bool = False,
        batch_size: int = 32,
        **kwargs: Any,
    ) -> np.ndarray:
        if isinstance(sentences, str):
            rng = np.random.default_rng(hash(sentences) % 2**32)
            return rng.standard_normal(dims).astype(np.float32)
        rng_batch = [
            np.random.default_rng(hash(s) % 2**32).standard_normal(dims)
            for s in sentences
        ]
        return np.array(rng_batch, dtype=np.float32)

    model.encode = MagicMock(side_effect=_encode)
    return model


@pytest.fixture(autouse=True)
def mock_sentence_transformer() -> Iterator[MagicMock]:
    """Auto-mock sentence-transformer loading to prevent model downloads."""
    mock_model = _make_mock_model()
    with patch(
        "creek.link.embeddings._load_sentence_transformer",
        return_value=mock_model,
    ) as mock_load:
        yield mock_load
