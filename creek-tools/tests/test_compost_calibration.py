"""Tests for ``creek.generate.compost_calibration`` (FEAT-028).

Covers fixture loading, the two-stage scoring pipeline (embedding gate
+ optional verifier), confusion-matrix arithmetic, JSON sidecar
emission, and the floor-gate assertions. The LLM verifier is mocked
with a deterministic stub keyed by entry id — no live calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.generate.compost_calibration import (
    DEFAULT_FIXTURE_PATH,
    CompostCalibrationEntry,
    CompostCalibrationReport,
    CompostEntryScore,
    CompostFloorBreachError,
    assert_floors,
    load_fixture,
    score_compost,
    write_json_sidecar,
)
from creek.generate.compost_verifier import (
    CompostVerdict,
    CompostVerifierResult,
)

if TYPE_CHECKING:
    from pathlib import Path


class _StubVerifier:
    """Returns a programmed verdict keyed by entry title.

    The compost detector calls ``verify(title=..., body=...)``; we
    look the title up in the response table so each test can shape
    the verifier's behaviour without monkeypatching the LLM client.
    """

    def __init__(
        self,
        responses: dict[str, CompostVerdict],
        *,
        default: CompostVerdict = CompostVerdict.YES,
    ) -> None:
        self._responses = responses
        self._default = default
        self.calls: list[tuple[str, str]] = []

    def verify(self, *, title: str, body: str) -> CompostVerifierResult:
        """Look up the title in the response table, fall back to default."""
        self.calls.append((title, body))
        verdict = self._responses.get(title, self._default)
        return CompostVerifierResult(verdict=verdict, reasoning="stub")


def _entry(
    *,
    entry_id: str,
    title: str = "title",
    body: str = "body",
    expected: bool = True,
) -> CompostCalibrationEntry:
    """Build a minimal entry for table-driven tests."""
    return CompostCalibrationEntry(
        id=entry_id,
        title=title,
        body=body,
        expected=expected,
    )


def _similarity_table(table: dict[str, float], *, default: float = 0.0) -> object:
    """Return a similarity_fn closure that looks ids up by title substring."""

    def fn(text: str) -> float:
        for key, score in table.items():
            if key in text:
                return score
        return default

    return fn


# ---- Fixture loading ----------------------------------------------------


class TestLoadFixture:
    """``load_fixture`` validates the YAML shape and coerces entries."""

    def test_packaged_default_loads(self) -> None:
        """The packaged fixture exists and parses into the expected shape."""
        entries = load_fixture(DEFAULT_FIXTURE_PATH)
        assert len(entries) >= 40
        positives = [e for e in entries if e.expected]
        negatives = [e for e in entries if not e.expected]
        assert len(positives) >= 20, "FEAT-028 requires 20+ positive examples"
        assert len(negatives) >= 20, "FEAT-028 requires 20+ negative examples"

    def test_packaged_default_uses_unique_ids(self) -> None:
        """No duplicate IDs in the packaged fixture."""
        entries = load_fixture(DEFAULT_FIXTURE_PATH)
        ids = [e.id for e in entries]
        assert len(ids) == len(set(ids))

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Absent fixture path raises ``FileNotFoundError`` with the path."""
        target = tmp_path / "does-not-exist.yaml"
        with pytest.raises(FileNotFoundError, match=str(target)):
            load_fixture(target)

    def test_non_list_top_level_raises(self, tmp_path: Path) -> None:
        """A non-list YAML top level produces a ``ValueError``."""
        target = tmp_path / "bad.yaml"
        target.write_text("not_a_list: true\n", encoding="utf-8")
        with pytest.raises(ValueError, match="list at top level"):
            load_fixture(target)

    def test_empty_file_returns_empty_tuple(self, tmp_path: Path) -> None:
        """An empty fixture file is valid and returns an empty tuple."""
        target = tmp_path / "empty.yaml"
        target.write_text("", encoding="utf-8")
        assert load_fixture(target) == ()

    def test_entry_missing_keys_raises(self, tmp_path: Path) -> None:
        """An entry missing ``expected`` surfaces a clear ``ValueError``."""
        target = tmp_path / "incomplete.yaml"
        target.write_text(
            "- id: x\n  title: t\n  body: b\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing keys"):
            load_fixture(target)

    def test_entry_not_a_dict_raises(self, tmp_path: Path) -> None:
        """A non-dict entry surfaces a clear ``ValueError``."""
        target = tmp_path / "scalar.yaml"
        target.write_text("- scalar-not-a-dict\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not a dict"):
            load_fixture(target)

    def test_non_bool_expected_raises(self, tmp_path: Path) -> None:
        """``expected`` must be a bool; strings should fail loud."""
        target = tmp_path / "string-expected.yaml"
        target.write_text(
            "- id: x\n  title: t\n  body: b\n  expected: yes-string\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must be a bool"):
            load_fixture(target)


# ---- Scoring ------------------------------------------------------------


class TestScoreCompost:
    """``score_compost`` runs the two-stage pipeline and tallies the matrix."""

    def test_below_threshold_is_not_flagged(self) -> None:
        """Entries that fail the embedding gate are never flagged."""
        entries = [_entry(entry_id="lo", title="lo-title", expected=True)]
        sim = _similarity_table({"lo-title": 0.1})

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=None,
            embedding_threshold=0.5,
        )

        assert report.true_positives == 0
        assert report.false_negatives == 1
        assert report.embedding_passed == 0
        # The single per-entry record should reflect a non-passing gate.
        assert report.per_entry[0].embedding_passed is False
        assert report.per_entry[0].verifier_verdict is None

    def test_embedding_only_passes_without_verifier(self) -> None:
        """When verifier is None the gate alone accepts above-threshold entries."""
        entries = [_entry(entry_id="hi", title="hi-title", expected=True)]
        sim = _similarity_table({"hi-title": 0.9})

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=None,
            embedding_threshold=0.5,
        )

        assert report.true_positives == 1
        assert report.embedding_passed == 1
        assert report.verified_yes == 0  # verifier was None — nothing to count
        assert report.per_entry[0].flagged is True

    def test_verifier_yes_accepts_as_compost(self) -> None:
        """A ``yes`` verifier verdict produces a flagged candidate."""
        entries = [_entry(entry_id="x", title="x-title", expected=True)]
        sim = _similarity_table({"x-title": 0.7})
        verifier = _StubVerifier({"x-title": CompostVerdict.YES})

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=verifier,
            embedding_threshold=0.6,
        )

        assert report.true_positives == 1
        assert report.verified_yes == 1
        assert report.per_entry[0].flagged is True
        assert report.per_entry[0].landed_in_review is False

    def test_verifier_no_saves_a_false_positive(self) -> None:
        """A ``no`` verdict rescues a false-positive from the matrix."""
        entries = [
            _entry(entry_id="neg", title="neg-title", expected=False),
        ]
        sim = _similarity_table({"neg-title": 0.95})  # gate passes
        verifier = _StubVerifier({"neg-title": CompostVerdict.NO})

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=verifier,
            embedding_threshold=0.6,
        )

        assert report.false_positives == 0
        assert report.true_negatives == 1
        assert report.verified_no == 1
        assert report.per_entry[0].flagged is False

    def test_verifier_ambiguous_routes_to_review_and_still_flags(self) -> None:
        """``ambiguous`` is treated as flagged (matches production tracker)."""
        entries = [_entry(entry_id="amb", title="amb-title", expected=True)]
        sim = _similarity_table({"amb-title": 0.8})
        verifier = _StubVerifier({"amb-title": CompostVerdict.AMBIGUOUS})

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=verifier,
            embedding_threshold=0.6,
        )

        assert report.true_positives == 1
        assert report.verified_ambiguous == 1
        assert report.per_entry[0].landed_in_review is True
        assert report.per_entry[0].flagged is True

    def test_full_confusion_matrix(self) -> None:
        """Mix of TP / FP / TN / FN entries yields the expected tally."""
        entries = [
            _entry(entry_id="tp", title="tp-t", expected=True),
            _entry(entry_id="fn", title="fn-t", expected=True),
            _entry(entry_id="fp", title="fp-t", expected=False),
            _entry(entry_id="tn", title="tn-t", expected=False),
        ]
        sim = _similarity_table(
            {
                "tp-t": 0.9,
                "fn-t": 0.1,  # gate fails — false negative
                "fp-t": 0.9,
                "tn-t": 0.1,
            },
        )
        verifier = _StubVerifier(
            {
                "tp-t": CompostVerdict.YES,
                "fp-t": CompostVerdict.YES,
            },
        )

        report = score_compost(
            entries,
            similarity_fn=sim,  # type: ignore[arg-type]
            verifier=verifier,
            embedding_threshold=0.5,
        )

        assert (
            report.true_positives,
            report.false_positives,
            report.true_negatives,
            report.false_negatives,
        ) == (1, 1, 1, 1)
        assert report.recall == pytest.approx(0.5)
        assert report.precision == pytest.approx(0.5)
        assert report.f1 == pytest.approx(0.5)
        assert report.false_positive_rate == pytest.approx(0.5)

    def test_empty_entries_yield_zero_metrics(self) -> None:
        """No entries → no division by zero; metrics are 0.0."""
        report = score_compost(
            [],
            similarity_fn=_similarity_table({}),  # type: ignore[arg-type]
            verifier=None,
        )
        assert report.entries == 0
        assert report.recall == 0.0
        assert report.precision == 0.0
        assert report.f1 == 0.0
        assert report.false_positive_rate == 0.0

    def test_text_is_title_only_when_body_is_empty(self) -> None:
        """The similarity_fn sees only the title when the body is blank."""
        captured: list[str] = []

        def sim(text: str) -> float:
            captured.append(text)
            return 0.0

        score_compost(
            [_entry(entry_id="t", title="only-title", body="", expected=True)],
            similarity_fn=sim,
            verifier=None,
        )
        assert captured == ["only-title"]


# ---- Report rendering ---------------------------------------------------


class TestReportRendering:
    """The CLI table and JSON sidecar reflect the report contents."""

    def _sample_report(self) -> CompostCalibrationReport:
        return CompostCalibrationReport(
            entries=4,
            true_positives=2,
            false_positives=1,
            true_negatives=0,
            false_negatives=1,
            embedding_passed=3,
            verified_yes=2,
            verified_no=0,
            verified_ambiguous=1,
            per_entry=(
                CompostEntryScore(
                    entry_id="e1",
                    expected=True,
                    similarity=0.91,
                    embedding_passed=True,
                    verifier_verdict=CompostVerdict.YES,
                    flagged=True,
                    landed_in_review=False,
                ),
            ),
        )

    def test_render_table_contains_each_metric(self) -> None:
        """Every metric the operator needs appears in the rendered table."""
        rendered = self._sample_report().render()
        for marker in (
            "Recall:",
            "Precision:",
            "F1:",
            "False-pos. rate:",
            "Embedding gate:",
            "Verifier yes:",
            "Verifier no:",
            "Verifier review:",
        ):
            assert marker in rendered

    def test_json_sidecar_round_trips(self, tmp_path: Path) -> None:
        """The JSON sidecar parses back and matches the report state."""
        target = tmp_path / "subdir" / "report.json"
        report = self._sample_report()

        write_json_sidecar(report, target)

        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed["entries"] == 4
        assert parsed["confusion_matrix"]["true_positives"] == 2
        assert parsed["metrics"]["recall"] == pytest.approx(2 / 3)
        assert parsed["per_stage"]["verified_ambiguous"] == 1
        assert parsed["per_entry"][0]["verifier_verdict"] == "yes"

    def test_json_sidecar_handles_none_verdict(self, tmp_path: Path) -> None:
        """``verifier_verdict=None`` serialises as JSON null, not the string."""
        target = tmp_path / "report.json"
        report = CompostCalibrationReport(
            entries=1,
            true_positives=0,
            false_positives=0,
            true_negatives=0,
            false_negatives=1,
            embedding_passed=0,
            verified_yes=0,
            verified_no=0,
            verified_ambiguous=0,
            per_entry=(
                CompostEntryScore(
                    entry_id="gate-miss",
                    expected=True,
                    similarity=0.1,
                    embedding_passed=False,
                    verifier_verdict=None,
                    flagged=False,
                    landed_in_review=False,
                ),
            ),
        )

        write_json_sidecar(report, target)

        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed["per_entry"][0]["verifier_verdict"] is None


# ---- Floor gating -------------------------------------------------------


class TestAssertFloors:
    """``assert_floors`` raises a single error listing every breach."""

    def _report(
        self, *, tp: int, fp: int, tn: int, fn: int
    ) -> CompostCalibrationReport:
        return CompostCalibrationReport(
            entries=tp + fp + tn + fn,
            true_positives=tp,
            false_positives=fp,
            true_negatives=tn,
            false_negatives=fn,
            embedding_passed=tp + fp,
            verified_yes=tp + fp,
            verified_no=0,
            verified_ambiguous=0,
        )

    def test_passes_when_above_floors(self) -> None:
        """Both metrics above the floors → no exception."""
        report = self._report(tp=8, fp=1, tn=10, fn=2)  # recall 0.8, precision 0.89
        assert_floors(report, floor_recall=0.7, floor_precision=0.7)

    def test_breaches_listed_in_message(self) -> None:
        """A double breach surfaces both metrics in the error string."""
        report = self._report(tp=2, fp=5, tn=5, fn=8)  # recall 0.2, precision 0.29
        with pytest.raises(CompostFloorBreachError) as excinfo:
            assert_floors(report, floor_recall=0.8, floor_precision=0.8)
        message = str(excinfo.value)
        assert "recall" in message
        assert "precision" in message

    def test_none_floor_is_skipped(self) -> None:
        """Passing ``None`` for one floor disables that gate."""
        report = self._report(tp=1, fp=0, tn=10, fn=9)  # recall 0.1, precision 1.0
        # recall is dismal but the recall gate is disabled
        assert_floors(report, floor_recall=None, floor_precision=0.8)
        # The precision gate alone now fires
        with pytest.raises(CompostFloorBreachError):
            assert_floors(report, floor_recall=0.5, floor_precision=None)

    def test_floors_unset_is_a_noop(self) -> None:
        """No floors supplied means no enforcement."""
        report = self._report(tp=0, fp=0, tn=0, fn=10)
        assert_floors(report)


# ---- CLI integration ----------------------------------------------------


class TestCompostCalibrateCLI:
    """End-to-end tests for ``creek compost calibrate`` (FEAT-028)."""

    def _write_tiny_fixture(self, tmp_path: Path) -> Path:
        """Write a two-entry fixture (one positive, one negative)."""
        target = tmp_path / "fx.yaml"
        target.write_text(
            "- id: cc-p\n"
            "  title: Quiet release\n"
            "  body: I am letting this one go. It is not mine anymore.\n"
            "  expected: true\n"
            "- id: cc-n\n"
            "  title: Shipped the migration\n"
            "  body: Two months of work and the migration is live. Done.\n"
            "  expected: false\n",
            encoding="utf-8",
        )
        return target

    def _patch_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        similarities: dict[str, float],
        verifier_responses: dict[str, CompostVerdict] | None = None,
    ) -> None:
        """Monkeypatch the embedding gate + verifier inside the CLI handler."""
        from creek import cli as cli_module
        from creek.generate import compost_embedding, compost_verifier

        def _fake_make_similarity_fn(
            _exemplars: object,
            _linker: object,
        ) -> object:
            def fn(text: str) -> float:
                for key, score in similarities.items():
                    if key in text:
                        return score
                return 0.0

            return fn

        class _FakeLinker:
            def __init__(self, **_kwargs: object) -> None:
                pass

        monkeypatch.setattr(
            "creek.link.embeddings.EmbeddingLinker",
            _FakeLinker,
        )
        monkeypatch.setattr(
            compost_embedding,
            "make_similarity_fn",
            _fake_make_similarity_fn,
        )

        if verifier_responses is None:
            monkeypatch.setattr(
                cli_module,
                "_build_compost_verifier",
                lambda _config: None,
            )
        else:

            class _FakeVerifier:
                def verify(
                    self,
                    *,
                    title: str,
                    body: str,  # signature contract
                ) -> compost_verifier.CompostVerifierResult:
                    verdict = verifier_responses.get(title, CompostVerdict.YES)
                    return compost_verifier.CompostVerifierResult(
                        verdict=verdict,
                        reasoning="stub",
                    )

            monkeypatch.setattr(
                cli_module,
                "_build_compost_verifier",
                lambda _config: _FakeVerifier(),
            )

    def test_calibrate_prints_metrics_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The CLI prints the recall / precision / F1 / FPR block."""
        from typer.testing import CliRunner

        from creek.cli import app

        fixture = self._write_tiny_fixture(tmp_path)
        self._patch_pipeline(
            monkeypatch,
            similarities={"Quiet release": 0.95, "Shipped the migration": 0.1},
        )

        result = CliRunner().invoke(
            app,
            [
                "compost",
                "calibrate",
                "--fixture",
                str(fixture),
                "--no-verifier",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Recall:" in result.output
        assert "Precision:" in result.output
        assert "False-pos. rate:" in result.output
        assert "Embedding gate:" in result.output

    def test_calibrate_writes_json_sidecar(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--json writes a machine-readable sidecar that round-trips."""
        from typer.testing import CliRunner

        from creek.cli import app

        fixture = self._write_tiny_fixture(tmp_path)
        sidecar = tmp_path / "out" / "compost-calibration.json"
        self._patch_pipeline(
            monkeypatch,
            similarities={"Quiet release": 0.95, "Shipped the migration": 0.1},
        )

        result = CliRunner().invoke(
            app,
            [
                "compost",
                "calibrate",
                "--fixture",
                str(fixture),
                "--no-verifier",
                "--json",
                str(sidecar),
            ],
        )

        assert result.exit_code == 0, result.output
        assert sidecar.exists()
        parsed = json.loads(sidecar.read_text(encoding="utf-8"))
        assert parsed["entries"] == 2
        assert parsed["metrics"]["recall"] == pytest.approx(1.0)

    def test_calibrate_exits_nonzero_on_floor_breach(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--floor-recall fails the run when recall is below the floor."""
        from typer.testing import CliRunner

        from creek.cli import app

        fixture = self._write_tiny_fixture(tmp_path)
        # Both entries fall below the embedding gate → recall = 0.
        self._patch_pipeline(
            monkeypatch,
            similarities={"Quiet release": 0.1, "Shipped the migration": 0.1},
        )

        result = CliRunner().invoke(
            app,
            [
                "compost",
                "calibrate",
                "--fixture",
                str(fixture),
                "--no-verifier",
                "--floor-recall",
                "0.8",
            ],
        )

        assert result.exit_code == 1
        assert "recall" in result.output

    def test_missing_fixture_path_exits_with_code_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user-supplied missing fixture path is a configuration error."""
        from typer.testing import CliRunner

        from creek.cli import app

        self._patch_pipeline(monkeypatch, similarities={})

        result = CliRunner().invoke(
            app,
            [
                "compost",
                "calibrate",
                "--fixture",
                str(tmp_path / "missing.yaml"),
            ],
        )

        assert result.exit_code == 2
        assert "not found" in result.output

    def test_build_compost_verifier_falls_back_when_credentials_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No API key → ``_build_compost_verifier`` returns ``None`` (no raise)."""
        from creek import cli as cli_module
        from creek.classify.llm import AnthropicProvider
        from creek.config import load_config

        monkeypatch.delenv(AnthropicProvider.API_KEY_ENV, raising=False)
        monkeypatch.delenv(AnthropicProvider.CONSENT_ENV, raising=False)

        config = load_config()
        assert cli_module._build_compost_verifier(config) is None

    def test_calibrate_routes_to_verifier_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without --no-verifier the stub verifier is consulted and counted."""
        from typer.testing import CliRunner

        from creek.cli import app

        fixture = self._write_tiny_fixture(tmp_path)
        self._patch_pipeline(
            monkeypatch,
            similarities={"Quiet release": 0.95, "Shipped the migration": 0.95},
            verifier_responses={
                "Quiet release": CompostVerdict.YES,
                "Shipped the migration": CompostVerdict.NO,
            },
        )

        result = CliRunner().invoke(
            app,
            [
                "compost",
                "calibrate",
                "--fixture",
                str(fixture),
            ],
        )

        assert result.exit_code == 0, result.output
        # One TP (Quiet release passes both stages), one TN (Shipped saved by NO).
        assert "Verifier yes:     1" in result.output
        assert "Verifier no:      1" in result.output
