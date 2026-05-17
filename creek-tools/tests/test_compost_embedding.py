"""Tests for ``creek.generate.compost_embedding`` (FEAT-018).

Covers exemplar loading and the cosine-similarity closure factory.
The factory is tested against an in-memory fake ``EmbeddingLinker``
to avoid pulling sentence-transformers into the unit-test runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.generate.compost_embedding import (
    PACKAGED_EXEMPLARS_PATH,
    CompostExemplar,
    load_exemplars,
    make_similarity_fn,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeLinker:
    """Minimal embedding-linker stand-in for similarity-fn tests.

    Returns deterministic vectors based on a substring → coordinate
    table so tests can shape the cosine-similarity landscape without
    importing torch.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[str] = []

    def generate_embedding(self, text: str) -> list[float]:
        """Return the configured vector for whichever key occurs in *text*."""
        self.calls.append(text)
        for key, vector in self._vectors.items():
            if key in text:
                return vector
        return [0.0, 0.0, 0.0]


class TestLoadExemplars:
    """Loading and validating the exemplar YAML."""

    def test_packaged_default_loads(self) -> None:
        """The packaged exemplar set parses without error and is non-empty."""
        exemplars = load_exemplars()
        assert len(exemplars) >= 1
        assert all(isinstance(e, CompostExemplar) for e in exemplars)

    def test_packaged_path_constant_points_inside_package(self) -> None:
        """The package-relative constant resolves to a real file on disk."""
        assert PACKAGED_EXEMPLARS_PATH.exists()
        assert PACKAGED_EXEMPLARS_PATH.name == "compost.yaml"

    def test_custom_path_loads(self, tmp_path: Path) -> None:
        """A user-provided YAML file is parsed and validated."""
        target = tmp_path / "custom.yaml"
        target.write_text(
            "- title: Quiet release\n"
            "  body: I let this one go without drama.\n"
            "  texture: releasing\n"
            "  rationale: Conscious release.\n",
            encoding="utf-8",
        )
        exemplars = load_exemplars(target)
        assert len(exemplars) == 1
        assert exemplars[0].title == "Quiet release"
        assert exemplars[0].texture == "releasing"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A non-existent path raises ``FileNotFoundError``."""
        with pytest.raises(FileNotFoundError):
            load_exemplars(tmp_path / "missing.yaml")

    def test_empty_yaml_rejected(self, tmp_path: Path) -> None:
        """An empty YAML file raises ``ValueError``."""
        target = tmp_path / "empty.yaml"
        target.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="non-empty list"):
            load_exemplars(target)

    def test_non_list_yaml_rejected(self, tmp_path: Path) -> None:
        """A YAML mapping (not list) raises ``ValueError``."""
        target = tmp_path / "wrong-shape.yaml"
        target.write_text("title: not a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="non-empty list"):
            load_exemplars(target)

    def test_missing_key_rejected(self, tmp_path: Path) -> None:
        """An entry missing a required key raises ``ValueError``."""
        target = tmp_path / "incomplete.yaml"
        target.write_text(
            "- title: Only title\n  body: x\n  texture: y\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing required keys"):
            load_exemplars(target)


class TestMakeSimilarityFn:
    """Behaviour of the similarity-closure factory."""

    @staticmethod
    def _exemplar(body: str) -> CompostExemplar:
        return CompostExemplar(
            title="x",
            body=body,
            texture="releasing",
            rationale="r",
        )

    def test_empty_text_returns_zero(self) -> None:
        """Blank input short-circuits to 0.0 without encoding."""
        linker = _FakeLinker({"any": [1.0, 0.0, 0.0]})
        fn = make_similarity_fn([self._exemplar("any")], linker)
        assert fn("") == 0.0
        assert fn("   ") == 0.0
        assert linker.calls == []

    def test_identical_text_returns_one(self) -> None:
        """A fragment matching an exemplar returns cosine 1.0."""
        linker = _FakeLinker({"release": [1.0, 0.0]})
        fn = make_similarity_fn([self._exemplar("release")], linker)
        assert fn("release") == pytest.approx(1.0)

    def test_orthogonal_text_returns_zero(self) -> None:
        """Orthogonal vectors yield 0.0 similarity."""
        linker = _FakeLinker({"release": [1.0, 0.0], "neutral": [0.0, 1.0]})
        fn = make_similarity_fn([self._exemplar("release")], linker)
        assert fn("neutral") == pytest.approx(0.0)

    def test_max_over_exemplars_wins(self) -> None:
        """The maximum similarity across all exemplars is returned."""
        # Fragment vector is closer to the SECOND exemplar; the max-similarity
        # contract requires the function to pick the closer one rather than
        # the iteration-first one.
        linker = _FakeLinker(
            {
                "AAA": [1.0, 0.0],
                "BBB": [0.0, 1.0],
                "fragment-text": [0.1, 1.0],
            },
        )
        exemplars = [self._exemplar("AAA"), self._exemplar("BBB")]
        fn = make_similarity_fn(exemplars, linker)
        score = fn("fragment-text")
        # cos((0.1, 1.0), (0.0, 1.0)) = 1.0 / sqrt(1.01)
        assert score == pytest.approx(1.0 / (1.01**0.5), abs=1e-6)

    def test_exemplar_vectors_cached(self) -> None:
        """Exemplar bodies are encoded only on the first call."""
        linker = _FakeLinker({"any": [1.0]})
        fn = make_similarity_fn([self._exemplar("any")], linker)
        fn("any")
        fn("any")
        # 1 encode for the exemplar (first call) + 2 encodes for the fragment
        assert sum(1 for c in linker.calls if c == "any") >= 2
        # The exemplar must not be re-encoded on each invocation
        exemplar_encodes = [c for c in linker.calls if c == "any"]
        assert len(exemplar_encodes) == 3
