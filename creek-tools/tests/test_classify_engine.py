"""Tests for the vault-driven classification engine.

Covers the dispatch logic in :mod:`creek.classify.classify_engine`,
including manual-decision preservation, ``--force`` behaviour, the
high-confidence skip path, and error handling around bad fragment
files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import frontmatter

from creek.classify.classify_engine import run_classify
from creek.classify.llm import LLMClassificationResult
from creek.config import CreekConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


def test_run_classify_returns_zeroes_when_no_fragments_dir(tmp_path: Path) -> None:
    """Missing ``01-Fragments`` returns a zeroed summary, not an error."""
    summary = run_classify(
        vault_path=tmp_path / "vault",
        config=CreekConfig(),
        method="rules",
        force=False,
    )
    assert summary.total == 0
    assert summary.classified == 0


def test_run_classify_rules_updates_fragments(tmp_path: Path) -> None:
    """Rule classification stamps ``classification_method: rules``."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-aaaaaaaaaaaa",
        title="Power and dominance",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert summary.classified == 1
    assert summary.total == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"


def test_classified_at_is_la_tz_aware(tmp_path: Path) -> None:
    """The ``classified_at`` frontmatter timestamp is LA-tz-aware (BUG-002).

    A naive implementation would call ``datetime.now()`` and silently
    record host-tz timestamps that fail to compare against the LA-tz
    timestamps elsewhere in the pipeline. Pin the contract.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-classifiedat",
        title="Power and dominance",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    reloaded = frontmatter.load(str(file))
    classified_at = reloaded["classified_at"]
    assert isinstance(classified_at, str)
    parsed = datetime.fromisoformat(classified_at)
    assert parsed.tzinfo is not None
    la_offset = ZoneInfo("America/Los_Angeles").utcoffset(parsed)
    assert parsed.utcoffset() == la_offset


def test_run_classify_preserves_manual_without_force(tmp_path: Path) -> None:
    """Manual decisions survive a ``--method rules`` pass."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-manual000000",
        title="Hand-tagged",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="body",
        method="manual",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert summary.preserved_manual == 1
    assert summary.classified == 0
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"


def test_run_classify_force_overwrites_manual(tmp_path: Path) -> None:
    """``--force`` rewrites manual fragments."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-manual000001",
        title="Force me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="body",
        method="manual",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=True,
    )

    assert summary.classified == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"


def test_classify_summary_errors_is_an_immutable_tuple(tmp_path: Path) -> None:
    """``ClassifySummary`` is genuinely immutable, not just frozen.

    A ``frozen=True`` dataclass with a ``list`` field would let
    callers mutate the list in place — defeating the purpose of the
    annotation. Pin the contract: ``summary.errors`` is a tuple, and
    appending to it raises.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-immutable001",
        title="Immutable",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert isinstance(summary.errors, tuple)
    import pytest as _pytest

    with _pytest.raises(AttributeError):
        # Tuples have no ``append``; the call is the regression pin.
        summary.errors.append("oops")  # type: ignore[attr-defined]


def test_run_classify_skips_non_fragment_files(tmp_path: Path) -> None:
    """Files that parse as YAML but aren't fragments are silently skipped.

    The classify engine treats a missing/wrong ``type`` field as "not a
    Creek fragment, leave it alone" — these are not errors, they're
    arbitrary markdown that happens to share the directory. Such
    files must not appear in ``summary.total`` either, otherwise the
    "Classified N of M" CLI message becomes misleading.
    """
    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)
    bad = fragments_dir / "bad.md"
    bad.write_text("---\nnot: valid\n---\nplain", encoding="utf-8")

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )
    assert summary.total == 0
    assert summary.classified == 0
    assert summary.errors == ()


def test_run_classify_unreadable_file_records_error(tmp_path: Path) -> None:
    """Files that fail to load surface on ``summary.errors``.

    Simulates an OSError at write time by patching ``_write_fragment``
    so that the file gets through validation and into the rewrite path,
    where the engine's ``except OSError`` branch records the failure.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-iofail000000",
        title="Power dominance bold rage warrior conquest",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    with patch(
        "creek.classify.classify_engine._write_fragment",
        side_effect=OSError("disk full"),
    ):
        summary = run_classify(
            vault_path=vault,
            config=CreekConfig(),
            method="rules",
            force=False,
        )

    assert summary.total == 1
    assert summary.classified == 0
    assert len(summary.errors) == 1
    assert "disk full" in summary.errors[0]


def test_run_classify_llm_skips_high_confidence(tmp_path: Path) -> None:
    """High-confidence rule result is persisted with method='rules'.

    When ``--method llm`` and the rule classifier produces a
    confident answer, the LLM is short-circuited but the rule's
    classification IS written back to disk — otherwise the operator's
    work would be silently discarded and the fragment would re-enter
    the review queue on every run.
    """
    import frontmatter

    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-skip00000000",
        title="Confident already",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    config = CreekConfig()
    config.classification.confidence_threshold = 0.0

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
    ) as mock_llm:
        summary = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    mock_llm.assert_not_called()
    assert summary.skipped_high_confidence >= 1
    assert summary.classified == summary.skipped_high_confidence

    # The rule-classified fragment must be persisted with the truthful
    # provenance stamp, not the user's CLI choice.
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"
    assert "classified_at" in reloaded.metadata


def test_run_classify_total_only_counts_creek_fragments(tmp_path: Path) -> None:
    """``total`` excludes arbitrary markdown notes that share the directory.

    A vault with 2 Creek fragments and 3 unrelated Obsidian notes
    must report ``total == 2`` — not 5. Counting non-fragments would
    surface as a misleading "Classified 2 of 5" CLI message.
    """
    vault = tmp_path / "vault"
    for i in range(2):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=f"frag-realfrag00{i:03d}",
                title=f"Real fragment {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="Power dominance bold rage warrior conquest",
        )

    notes_dir = vault / "01-Fragments" / "Notes"
    for i in range(3):
        note = notes_dir / f"unrelated-{i}.md"
        # Plain Obsidian note — no ``type: fragment`` frontmatter.
        note.write_text(f"# Unrelated {i}\n\nJust a note.\n", encoding="utf-8")

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert summary.total == 2
    assert summary.classified == 2


def test_run_classify_llm_invoked_for_low_confidence(tmp_path: Path) -> None:
    """The LLM classifier runs when rules leave the fragment unclassified."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-llmcall00000",
        title="not enough signal",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ordinary content with no signal keywords at all",
    )

    config = CreekConfig()

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=lambda f, content="": LLMClassificationResult(
            fragment=f,
            reasoning="",
        ),
    ) as mock_llm:
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    mock_llm.assert_called_once()


# ---- FEAT-017a: classification_reasoning tier-routing ----


_LONG_REASONING: str = (
    "I worked through this in detail. "
    "The frequency is clearly F3 because the operator is exercising "
    "authority over a decision. "
    + ("the same point repeated until we hit the cap. " * 20)
)


def _run_with_reasoning(
    vault: Path,
    fragment: Fragment,
    body: str,
    reasoning: str,
) -> None:
    """Seed *fragment* into *vault* and run the engine with a canned reasoning trace."""
    _write_fragment(vault=vault, fragment=fragment, body=body)
    config = CreekConfig()
    config.classification.confidence_threshold = 1.0  # force LLM path
    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=lambda f, content="": LLMClassificationResult(
            fragment=f,
            reasoning=reasoning,
        ),
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )


def test_open_tier_truncates_reasoning_into_frontmatter(tmp_path: Path) -> None:
    """An ``open``-tier fragment carries a truncated trace in its frontmatter."""
    import importlib

    from creek.models import PrivacyTier

    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-opentier001",
        title="open",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=PrivacyTier.OPEN,
    )
    _run_with_reasoning(vault, frag, body="open content", reasoning=_LONG_REASONING)

    constants = importlib.import_module("creek.classify.constants")
    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-opentier001.md"),
    )
    stored = reloaded[constants.CLASSIFICATION_REASONING_KEY]
    assert isinstance(stored, str)
    assert stored.endswith("…")
    assert len(stored) == constants.CLASSIFICATION_REASONING_MAX_CHARS


def test_personal_tier_truncates_reasoning_into_frontmatter(tmp_path: Path) -> None:
    """A ``personal``-tier fragment also gets the truncated trace in frontmatter."""
    import importlib

    from creek.models import PrivacyTier

    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-personal001",
        title="personal",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=PrivacyTier.PERSONAL,
    )
    _run_with_reasoning(vault, frag, body="personal content", reasoning=_LONG_REASONING)

    constants = importlib.import_module("creek.classify.constants")
    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-personal001.md"),
    )
    stored = reloaded[constants.CLASSIFICATION_REASONING_KEY]
    assert isinstance(stored, str)
    assert stored
    assert len(stored) <= constants.CLASSIFICATION_REASONING_MAX_CHARS


def test_intimate_tier_writes_full_trace_to_log_not_frontmatter(
    tmp_path: Path,
) -> None:
    """An ``intimate``-tier fragment's full trace lives in the log file only."""
    import json as _json

    from creek.classify.constants import (
        CLASSIFICATION_REASONING_KEY,
        CLASSIFY_TRACE_LOG_FILENAME,
    )
    from creek.models import PrivacyTier

    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-intimate001",
        title="intimate",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=PrivacyTier.INTIMATE,
    )
    _run_with_reasoning(vault, frag, body="intimate content", reasoning=_LONG_REASONING)

    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-intimate001.md"),
    )
    # Frontmatter MUST NOT carry the reasoning for intimate-tier fragments.
    assert CLASSIFICATION_REASONING_KEY not in reloaded.metadata

    # Full trace lives in the gitignorable trace log.
    log_path = vault / "00-Creek-Meta" / "Processing-Log" / CLASSIFY_TRACE_LOG_FILENAME
    assert log_path.exists()
    records = [
        _json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(records) == 1
    assert records[0]["id"] == frag.id
    assert records[0]["tier"] == "intimate"
    assert records[0]["reasoning"] == _LONG_REASONING


def test_short_reasoning_is_persisted_verbatim(tmp_path: Path) -> None:
    """A trace shorter than the cap is stored without truncation marker."""
    from creek.classify.constants import CLASSIFICATION_REASONING_KEY
    from creek.models import PrivacyTier

    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-shortreas01",
        title="short",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        privacy_tier=PrivacyTier.OPEN,
    )
    short = "Brief reasoning."
    _run_with_reasoning(vault, frag, body="content", reasoning=short)

    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-shortreas01.md"),
    )
    assert reloaded[CLASSIFICATION_REASONING_KEY] == short


def test_rules_method_does_not_persist_reasoning(tmp_path: Path) -> None:
    """``--method rules`` runs never emit a classification_reasoning field."""
    from creek.classify.constants import CLASSIFICATION_REASONING_KEY

    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-rulesreas01",
        title="rules-only",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=frag,
        body="power dominance bold rage warrior",
    )
    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )
    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-rulesreas01.md"),
    )
    assert CLASSIFICATION_REASONING_KEY not in reloaded.metadata


# ---- Issue #321: preserved counter splits manual vs prior-LLM ----


def test_preserved_manual_counts_only_genuinely_manual_fragments(
    tmp_path: Path,
) -> None:
    """``preserved_manual`` reflects only ``classification_method: manual``.

    Issue #321: prior to the fix, a fragment stamped by a partial LLM
    run inflated ``preserved_manual``, causing the CLI to report "N
    manual preserved" even though no human had ever touched the file.
    Pin the contract: the field name must match the data.
    """
    vault = tmp_path / "vault"
    manual_frag = Fragment(
        id="frag-trulymanual0",
        title="Hand-tagged",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=manual_frag,
        body="body",
        method="manual",
    )
    llm_frag = Fragment(
        id="frag-priorllm0001",
        title="Prior LLM run",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=llm_frag,
        body="body",
        method="llm",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert summary.preserved_manual == 1
    assert summary.preserved_llm == 1
    assert summary.classified == 0


def test_preserved_llm_counts_only_prior_llm_runs(tmp_path: Path) -> None:
    """``preserved_llm`` counts fragments preserved by the OPS-001 resume path.

    A fragment carrying ``classification_method: llm`` from a previous
    (possibly partial) run must be tallied separately from manual
    decisions so the CLI message can distinguish the two.
    """
    vault = tmp_path / "vault"
    frag = Fragment(
        id="frag-priorllm0002",
        title="Prior LLM only",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=frag,
        body="body",
        method="llm",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert summary.preserved_manual == 0
    assert summary.preserved_llm == 1
    assert summary.classified == 0


# ---- Issue #319: wavelength round-trips end-to-end through --method llm ----


_WAVELENGTH_FULL_YAML: str = """\
frequency:
  primary: F3
  secondary: []
wavelength:
  phase: peaking
  mode: express
  orientation: do
  dosage: medicine
  color: red
  descriptor: Power-With
voice:
  voice_register: prophetic
  confidence: settled
confidence_scores:
  mode: 0.9
  orientation: 0.9
  dosage: 0.9
"""
"""Reference LLM response covering every Wavelength sub-field.

Lives next to its consumer so a regression in the prompt/parser
contract is visible alongside the round-trip assertion that catches
it, rather than buried in a fixtures directory.
"""


def test_run_classify_llm_persists_all_wavelength_fields(tmp_path: Path) -> None:
    """Issue #319: wavelength.color and wavelength.descriptor reach disk.

    Reproduces the user-visible bug end-to-end with a mocked LLM
    response: a fragment classified via ``--method llm`` must persist
    ``color`` and ``descriptor`` to its frontmatter, not just the
    historical four-field subset (phase / mode / orientation / dosage).
    Asserts on the on-disk YAML — not the in-memory ``Fragment`` —
    because that is what the user inspects after the run.
    """
    from creek.classify.llm import LLMClassifier

    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-wavefull001",
        title="exuberant declaration",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ordinary content with no rule-classifier signal keywords",
    )

    config = CreekConfig()
    config.classification.confidence_threshold = 1.0  # force LLM path

    with (
        patch.object(LLMClassifier, "_check_availability", return_value=True),
        patch.object(LLMClassifier, "_invoke_llm", return_value=_WAVELENGTH_FULL_YAML),
    ):
        summary = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    assert summary.classified == 1
    reloaded = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-wavefull001.md"),
    )
    wavelength = reloaded["wavelength"]
    assert isinstance(wavelength, dict)
    assert wavelength["phase"] == "peaking"
    assert wavelength["mode"] == "express"
    assert wavelength["orientation"] == "do"
    assert wavelength["dosage"] == "medicine"
    assert wavelength["color"] == "red"
    assert wavelength["descriptor"] == "Power-With"


# ---- Issue #318: YAML ``classification.reatomize: true`` is honored --------


_MULTI_PARAGRAPH_BODY: str = (
    "First paragraph carries some loose musing about ordinary subject matter.\n"
    "\n"
    "Second paragraph shifts register entirely and pulls in another thread.\n"
    "\n"
    "Third paragraph closes with yet another distinct slice of content."
)
"""Body wide enough that the FEAT-021 splitter produces multiple children.

The ``document``-level splitter cascades to paragraph chunks when no
headings exist, so three blank-line-separated paragraphs guarantee
three child fragments are produced.
"""


def test_run_classify_honors_yaml_reatomize_default(tmp_path: Path) -> None:
    """``classification.reatomize: true`` fires FEAT-023 without ``--reatomize``.

    Regression test for issue #318: setting the YAML default for
    ``classification.reatomize`` previously had no effect — only the
    ``--reatomize`` CLI flag fired the splitter. The wire-up lives in
    the engine, so YAML and CLI now reach the same code path.
    """
    vault = tmp_path / "vault"
    parent = Fragment(
        id="frag-yamlreatm01",
        title="document needing zoom-in",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=parent, body=_MULTI_PARAGRAPH_BODY)

    config = CreekConfig()
    # The smoking gun: only the YAML default is on. No CLI flag involved.
    config.classification.reatomize = True
    # Force the LLM path even on the (already short) rule result so the
    # mocked LLM low-confidence return value drives the orchestrator.
    config.classification.confidence_threshold = 1.0

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=lambda f, content="": LLMClassificationResult(
            fragment=f,
            reasoning="",
        ),
    ):
        summary = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    # The root fragment is still counted as classified.
    assert summary.total == 1
    assert summary.classified == 1

    # The splitter produced child fragment files with the parent ID set.
    notes_dir = vault / "01-Fragments" / "Notes"
    children = [
        frontmatter.load(str(p))
        for p in notes_dir.glob("*.md")
        if p.name != "frag-yamlreatm01.md"
    ]
    assert children, "FEAT-023 splitter did not write any child fragments"
    assert all(child.metadata.get("parent_id") == parent.id for child in children)


def test_run_classify_skips_reatomize_when_yaml_disables_it(tmp_path: Path) -> None:
    """The default (``reatomize: false``) keeps the engine on the single-pass path."""
    vault = tmp_path / "vault"
    parent = Fragment(
        id="frag-noreatm0001",
        title="document staying whole",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=parent, body=_MULTI_PARAGRAPH_BODY)

    config = CreekConfig()
    # Defaults: reatomize is False. Force LLM path; mock low-confidence return.
    config.classification.confidence_threshold = 1.0

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=lambda f, content="": LLMClassificationResult(
            fragment=f,
            reasoning="",
        ),
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    notes_dir = vault / "01-Fragments" / "Notes"
    # No new files: the single original fragment file is the only output.
    assert sorted(p.name for p in notes_dir.glob("*.md")) == [
        "frag-noreatm0001.md",
    ]


def test_run_classify_reatomize_invokes_orchestrator(tmp_path: Path) -> None:
    """When YAML enables reatomize, ``classify_reatomize`` is invoked, not bypassed.

    This pins the wire-up at the call-site level so a future refactor
    that satisfies the "children on disk" assertion via a different
    code path still has to go through the FEAT-023 orchestrator (or
    explicitly reroute this test).
    """
    vault = tmp_path / "vault"
    parent = Fragment(
        id="frag-orchcall001",
        title="check orchestrator",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=parent, body=_MULTI_PARAGRAPH_BODY)

    config = CreekConfig()
    config.classification.reatomize = True
    config.classification.confidence_threshold = 1.0

    with (
        patch(
            "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
            side_effect=lambda f, content="": LLMClassificationResult(
                fragment=f,
                reasoning="",
            ),
        ),
        patch(
            "creek.classify.classify_engine.classify_reatomize",
            wraps=__import__(
                "creek.classify.reatomize",
                fromlist=["classify_reatomize"],
            ).classify_reatomize,
        ) as spy,
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )

    spy.assert_called()
