"""Shared test fixtures for creek-tools.

Provides an auto-use fixture that mocks the sentence-transformer model
loading so tests never download models or require GPU access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_DIMS = 384


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
