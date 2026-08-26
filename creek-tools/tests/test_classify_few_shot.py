"""Tests for creek.classify.few_shot (FEAT-017a)."""

from __future__ import annotations

import logging

import pytest

from creek.classify import few_shot
from creek.classify.few_shot import (
    DIMENSIONS,
    EXAMPLES_PER_DIMENSION,
    FewShotExample,
    examples_for,
    render_block,
    sample_examples,
)


class TestExamplesLoad:
    """The bundled YAML fixtures load into FewShotExample tuples."""

    def test_every_dimension_has_at_least_five_examples(self) -> None:
        """FEAT-017 acceptance: 5-10 examples per dimension shipped."""
        for dim in DIMENSIONS:
            pool = examples_for(dim)
            assert len(pool) >= 5, f"{dim} has only {len(pool)} examples"

    def test_each_example_is_immutable_record(self) -> None:
        """Examples expose title, body, label, rationale strings."""
        for dim in DIMENSIONS:
            for ex in examples_for(dim):
                assert isinstance(ex, FewShotExample)
                assert ex.title and ex.body and ex.label and ex.rationale

    def test_unknown_dimension_returns_empty(self) -> None:
        """An unrecognised dimension yields ``()`` rather than raising."""
        assert examples_for("nonsense") == ()


class TestSampleExamples:
    """sample_examples returns a deterministic per-fragment sample."""

    def test_deterministic_for_same_fragment_id(self) -> None:
        """The same fragment ID always picks the same examples."""
        first = sample_examples("frag-abc123")
        second = sample_examples("frag-abc123")
        assert first == second

    def test_differs_across_fragment_ids(self) -> None:
        """Different IDs rotate the example pool (at least one dim changes)."""
        a = sample_examples("frag-aaaaaaa1")
        b = sample_examples("frag-bbbbbbb2")
        # With ≥5 fixtures per dim and 3 picked, collisions are possible per
        # dim but exceedingly unlikely across all five. Require at least one
        # dimension to differ.
        differing = [dim for dim in DIMENSIONS if a[dim] != b[dim]]
        assert differing, "every dimension picked identical examples"

    def test_respects_per_dimension_cap(self) -> None:
        """Each dimension yields at most ``per_dimension`` examples."""
        sample = sample_examples("frag-cap0", per_dimension=2)
        for dim in DIMENSIONS:
            assert len(sample[dim]) <= 2

    def test_default_count_matches_module_constant(self) -> None:
        """The default sample size is EXAMPLES_PER_DIMENSION."""
        sample = sample_examples("frag-default")
        for dim in DIMENSIONS:
            pool_size = len(examples_for(dim))
            assert len(sample[dim]) == min(EXAMPLES_PER_DIMENSION, pool_size)


class TestRenderBlock:
    """render_block produces a prompt-ready string."""

    def test_block_mentions_every_dimension_with_examples(self) -> None:
        """Each dimension that has examples is named in the rendered block."""
        block = render_block(sample_examples("frag-render"))
        for dim in DIMENSIONS:
            if examples_for(dim):
                assert dim.title() in block

    def test_block_includes_labels(self) -> None:
        """Each rendered example shows its canonical label."""
        sample = sample_examples("frag-render")
        block = render_block(sample)
        for chosen in sample.values():
            for ex in chosen:
                assert ex.label in block

    def test_truncates_long_body(self) -> None:
        """A body longer than the cap is truncated with an ellipsis."""
        long_body = "x" * 1000
        sample = {
            "frequency": (
                FewShotExample(
                    title="t",
                    body=long_body,
                    label="F1",
                    rationale="r",
                ),
            ),
        }
        block = render_block(sample)
        assert long_body not in block
        assert "…" in block

    def test_empty_samples_produce_empty_block(self) -> None:
        """All-empty input renders to the empty string, not whitespace."""
        empty = dict.fromkeys(DIMENSIONS, ())
        assert render_block(empty) == ""


class TestLoaderResilience:
    """The loader degrades gracefully when fixtures are malformed."""

    def test_invalid_yaml_entry_is_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Entries missing required keys are dropped silently with a warning."""
        # Clear the lru_cache so our monkeypatched fixture takes effect.
        few_shot._load_all_examples.cache_clear()
        try:
            sentinel = [
                {"title": "ok", "body": "b", "label": "F1", "rationale": "r"},
                {"title": "missing body"},  # invalid
                "not a dict at all",  # invalid
            ]

            class _StubResource:
                def joinpath(self, name: str) -> _StubResource:
                    self.name = name
                    return self

                def read_text(self, encoding: str) -> str:
                    import yaml as _yaml

                    return _yaml.safe_dump(sentinel)

            monkeypatch.setattr(
                few_shot.resources,
                "files",
                lambda _pkg: _StubResource(),
            )
            loaded = few_shot._load_all_examples()
            for dim in DIMENSIONS:
                assert all(isinstance(e, FewShotExample) for e in loaded[dim])
                assert len(loaded[dim]) == 1  # only the well-formed entry
        finally:
            few_shot._load_all_examples.cache_clear()

    def test_missing_fixture_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A FileNotFoundError on a fixture yields an empty tuple, not a crash."""
        few_shot._load_all_examples.cache_clear()
        try:

            class _MissingResource:
                def joinpath(self, name: str) -> _MissingResource:
                    self.name = name
                    return self

                def read_text(self, encoding: str) -> str:
                    raise FileNotFoundError(self.name)

            monkeypatch.setattr(
                few_shot.resources,
                "files",
                lambda _pkg: _MissingResource(),
            )
            loaded = few_shot._load_all_examples()
            for dim in DIMENSIONS:
                assert loaded[dim] == ()
        finally:
            few_shot._load_all_examples.cache_clear()

    def test_non_list_yaml_returns_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A YAML scalar at the top of a fixture is rejected with a warning."""
        few_shot._load_all_examples.cache_clear()
        try:

            class _ScalarResource:
                def joinpath(self, name: str) -> _ScalarResource:
                    self.name = name
                    return self

                def read_text(self, encoding: str) -> str:
                    return "not_a_list: scalar\n"

            monkeypatch.setattr(
                few_shot.resources,
                "files",
                lambda _pkg: _ScalarResource(),
            )
            loaded = few_shot._load_all_examples()
            for dim in DIMENSIONS:
                assert loaded[dim] == ()
        finally:
            few_shot._load_all_examples.cache_clear()

    def test_falsy_non_list_yaml_is_reported_not_silently_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An empty YAML mapping warns rather than passing as "no examples".

        ``yaml.safe_load(raw) or []`` turned every falsy wrong type into
        the empty list before the guard ran, so a malformed fixture was
        indistinguishable from a deliberately empty one (#1004).
        """
        few_shot._load_all_examples.cache_clear()
        try:

            class _EmptyMappingResource:
                def joinpath(self, name: str) -> _EmptyMappingResource:
                    self.name = name
                    return self

                def read_text(self, encoding: str) -> str:
                    return "{}\n"

            monkeypatch.setattr(
                few_shot.resources,
                "files",
                lambda _pkg: _EmptyMappingResource(),
            )
            with caplog.at_level(logging.WARNING, logger=few_shot.__name__):
                loaded = few_shot._load_all_examples()
            for dim in DIMENSIONS:
                assert loaded[dim] == ()
                assert f"{dim}.yaml is not a list" in caplog.text
        finally:
            few_shot._load_all_examples.cache_clear()


class TestCoerceGuard:
    """`_coerce` raises rather than silently producing a half-built example."""

    def test_coerce_rejects_non_dict(self) -> None:
        """The TypeError branch fires when the gate is bypassed."""
        with pytest.raises(TypeError, match="non-dict"):
            few_shot._coerce("not a dict")  # type: ignore[arg-type]


class TestSampleExamplesEmptyPool:
    """`sample_examples` handles an empty fixture pool without crashing."""

    def test_dimension_with_no_examples_yields_empty_tuple(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dimension whose pool is empty is mapped to ``()`` in the sample."""
        stub_pool = dict.fromkeys(DIMENSIONS, ())
        monkeypatch.setattr(few_shot, "_load_all_examples", lambda: stub_pool)
        sample = few_shot.sample_examples("frag-empty00001")
        for dim in DIMENSIONS:
            assert sample[dim] == ()
