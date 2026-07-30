"""Tests for the vault-driven classification engine.

Covers the dispatch logic in :mod:`creek.classify.classify_engine`,
including manual-decision preservation, ``--force`` behaviour, the
high-confidence skip path, and error handling around bad fragment
files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import PropertyMock, patch

import frontmatter
import pytest

from creek.classify.classify_engine import (
    LLMProviderUnavailableError,
    _describe_llm_unavailability,
    run_classify,
)
from creek.classify.llm import LLMClassificationResult, LLMClassifier
from creek.config import CreekConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _force_llm_available() -> object:
    """Bypass the LLM-availability gate for engine tests by default.

    Most engine tests mock the LLM at the ``classify_with_reasoning``
    boundary and rely on the availability gate being open. Tests that
    exercise the unavailable path (issue #317) re-patch ``available``
    locally to return ``False``.
    """
    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=True,
    ) as patched:
        yield patched


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


# ---- --method llm fails loudly when the provider is unavailable ----


def test_run_classify_llm_unavailable_raises_before_processing(
    tmp_path: Path,
) -> None:
    """``--method llm`` raises when the provider is unavailable.

    Symptom from the bug report: when the Anthropic provider is
    configured but ``CREEK_ANTHROPIC_CONSENT`` is unset, the engine
    iterated over every fragment, the LLM short-circuited to "return
    unchanged", and the summary lied that "Classified N of N"
    succeeded. Worse, the process exited 0 — a shell pipeline reading
    the exit code saw a clean success.

    Fix contract: when the engine is asked for ``--method llm`` and the
    classifier reports ``available is False``, it raises
    :class:`LLMProviderUnavailableError` **before** rewriting any
    fragments. The CLI translates this to a non-zero exit.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-llmunavail01",
        title="placeholder",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ordinary content with no signal keywords at all",
    )
    original_text = file.read_text(encoding="utf-8")

    with (
        patch.object(
            LLMClassifier,
            "available",
            new_callable=PropertyMock,
            return_value=False,
        ),
        pytest.raises(LLMProviderUnavailableError),
    ):
        run_classify(
            vault_path=vault,
            config=CreekConfig(),
            method="llm",
            force=False,
        )

    # Fragments on disk are untouched — no spurious ``classification_method:
    # llm`` stamp gets written when the LLM never actually ran.
    assert file.read_text(encoding="utf-8") == original_text


def test_run_classify_llm_unavailable_message_names_provider(
    tmp_path: Path,
) -> None:
    """The unavailable error mentions the configured provider name.

    A first-time user staring at ``creek classify --method llm`` failing
    needs to know which provider Creek tried to reach so they can fix
    the right environment variable. Pin the provider name into the
    error message.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-llmunavail02",
        title="placeholder",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="content",
    )
    config = CreekConfig()
    config.llm.default.provider = "anthropic"

    with (
        patch.object(
            LLMClassifier,
            "available",
            new_callable=PropertyMock,
            return_value=False,
        ),
        pytest.raises(LLMProviderUnavailableError, match="anthropic"),
    ):
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            force=False,
        )


def test_run_classify_llm_unavailable_skips_progress_dir_writes(
    tmp_path: Path,
) -> None:
    """The progress log file is not created when the LLM never runs.

    A non-zero-exit failure path should leave the vault clean: no
    bookkeeping artefacts that suggest a partial run. The progress
    directory (00-Creek-Meta/Processing-Log) gets created up-front, but
    the ``llm-progress.jsonl`` file inside it should never appear when
    the LLM never executes.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-llmunavail03",
        title="placeholder",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="content",
    )

    with (
        patch.object(
            LLMClassifier,
            "available",
            new_callable=PropertyMock,
            return_value=False,
        ),
        pytest.raises(LLMProviderUnavailableError),
    ):
        run_classify(
            vault_path=vault,
            config=CreekConfig(),
            method="llm",
            force=False,
        )

    progress_file = vault / "00-Creek-Meta" / "Processing-Log" / "llm-progress.jsonl"
    assert not progress_file.exists()


# ---- _describe_llm_unavailability: per-provider remediation hint ----


def test_describe_llm_unavailability_anthropic_mentions_api_key_and_consent() -> None:
    """The Anthropic hint names both env vars the operator must set.

    Pinning both names so a search for either ``ANTHROPIC_API_KEY`` or
    ``CREEK_ANTHROPIC_CONSENT`` in the codebase keeps surfacing the
    user-facing remediation copy. If a future refactor splits these into
    separate sentences the test still passes; if either disappears the
    test fails loudly.
    """
    message = _describe_llm_unavailability("anthropic")
    assert "ANTHROPIC_API_KEY" in message
    assert "CREEK_ANTHROPIC_CONSENT" in message


def test_describe_llm_unavailability_ollama_mentions_daemon_and_url() -> None:
    """The Ollama hint points at the local daemon and the config URL key.

    Ollama failures are local-process failures (daemon not running, wrong
    port), so the remediation has to direct the operator at the daemon
    and the ``llm.url`` setting that controls where Creek looks for it.
    """
    message = _describe_llm_unavailability("ollama")
    assert "Ollama" in message or "ollama" in message
    assert "llm.url" in message


def test_describe_llm_unavailability_unknown_provider_falls_back_to_generic() -> None:
    """Unknown providers get a generic ``creek_config.yaml`` pointer.

    Pins the fallback branch so adding a third provider to
    ``orchestrator.py`` without updating this helper does not silently
    swallow the operator-facing remediation hint — instead they get the
    generic message until the helper is taught about the new provider.
    """
    message = _describe_llm_unavailability("vertex")
    assert "creek_config.yaml" in message
    # The generic branch must NOT leak provider-specific env-var names
    # that would be misleading for the unknown provider.
    assert "ANTHROPIC_API_KEY" not in message
    assert "Ollama" not in message


def test_run_classify_rules_does_not_construct_llm(
    tmp_path: Path,
) -> None:
    """``--method rules`` never constructs the LLM classifier.

    The availability gate must not bleed into the rules-only path —
    a vault without any LLM credentials configured must classify with
    rules cleanly.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-rulesonly001",
        title="rules only",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="power dominance bold rage warrior conquest",
    )

    with patch(
        "creek.classify.classify_engine.LLMClassifier",
    ) as mock_cls:
        summary = run_classify(
            vault_path=vault,
            config=CreekConfig(),
            method="rules",
            force=False,
        )

    mock_cls.assert_not_called()
    assert summary.classified == 1


def _audience_of(path: Path) -> str:
    """Return the persisted ``audience`` axis from a fragment file."""
    return str(frontmatter.load(str(path)).metadata.get("audience"))


def test_run_classify_stamps_audience_axis(tmp_path: Path) -> None:
    """``creek classify`` persists the #634 audience axis to frontmatter."""
    vault = tmp_path / "vault"
    essay = Fragment(
        id="frag-essay00000001",
        title="published essay",
        source=FragmentSource(platform=SourcePlatform.ESSAY),
    )
    essay_path = _write_fragment(
        vault=vault,
        fragment=essay,
        body="# Heading\n\n" + ("a deliberate published sentence. " * 80),
    )
    journal = Fragment(
        id="frag-journal0001",
        title="private musing",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
    )
    journal_path = _write_fragment(
        vault=vault,
        fragment=journal,
        body="today I felt raw and small",
    )

    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert _audience_of(essay_path) == "audience-facing"
    assert _audience_of(journal_path) == "private"


def test_run_classify_audience_axis_is_idempotent(tmp_path: Path) -> None:
    """A second rules pass leaves the audience axis unchanged."""
    vault = tmp_path / "vault"
    essay = Fragment(
        id="frag-essay00000002",
        title="published essay",
        source=FragmentSource(platform=SourcePlatform.ESSAY),
    )
    path = _write_fragment(
        vault=vault,
        fragment=essay,
        body="# Heading\n\n" + ("a deliberate published sentence. " * 80),
    )

    run_classify(vault_path=vault, config=CreekConfig(), method="rules", force=False)
    first = _audience_of(path)
    run_classify(vault_path=vault, config=CreekConfig(), method="rules", force=True)
    assert _audience_of(path) == first == "audience-facing"


# ---- Issue #876: `creek classify` assigns a real privacy tier ---------------
#
# `PrivacyClassifier` shipped fully implemented but with zero production
# callers, so every fragment stayed `privacy_tier: unclassified` after a
# classify run and Intimate-never-cloud routing (#666 / ADR-0003) had nothing
# to act on. These tests pin the wire-up: the tier is assigned BEFORE the
# per-tier router picks a provider, it survives the preserved-method
# short-circuit, it only ever escalates, and it is never persisted as
# `unclassified`.
#
# TEST-DESIGN RULE for every vault-level test below: ``Path.rglob`` does not
# sort — it returns ``os.scandir`` order, which is hash-ordered on APFS. So we
# always build >= 3 fragments with DISTINCT expected tiers, read the results
# back off disk, and assert an explicit ``{fragment_id: tier}`` mapping. Never
# assert on "the first file" and never index into rglob output: a positional
# assertion can false-green against the live bug (this nearly shipped in #847).

_TIER_NEUTRAL_BODY = "a plain note about the walk to the shops and the weather"
"""Body with no rule-classifier voice signal, so the tier heuristic drives.

Deliberately free of confessional keywords ("confess", "shame", "bare", …)
and conviction keywords ("absolutely", "undeniably", "truth is", …) so the
rules pass cannot escalate a fragment to INTIMATE via the
confessional+conviction branch and silently invalidate the expected map.
"""


def _tier_map(vault: Path) -> dict[str, str]:
    """Return ``{fragment_id: privacy_tier}`` read back off disk.

    Reads EVERY markdown file under ``01-Fragments`` (recursively) rather
    than a positional slice of ``rglob`` output, so the assertion is
    independent of filesystem iteration order.
    """
    out: dict[str, str] = {}
    for path in (vault / "01-Fragments").rglob("*.md"):
        meta = frontmatter.load(str(path)).metadata
        if meta.get("type") != "fragment":
            continue
        out[str(meta["id"])] = str(meta.get("privacy_tier", "<absent>"))
    return out


def _seed_tier_corpus(vault: Path) -> None:
    """Seed five fragments whose expected tiers cover the whole ladder.

    One per branch of :meth:`PrivacyClassifier.classify_tier`: journal
    self-authored, recovery keyword, Discord public channel, Discord DM,
    and a published essay.
    """
    from creek.models import Authorship

    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tier-journal",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tier-recovry",
            title="Ninety days",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body="ninety days of sobriety today and the walk felt shorter",
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tier-general",
            title="Build notes",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                author=Authorship.SELF,
                channel="#general",
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tier-dm0001",
            title="Scheduling",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                author=Authorship.SELF,
                channel="dm",
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tier-essay01",
            title="On public transit",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )


_EXPECTED_TIER_MAP = {
    "frag-tier-journal": "intimate",
    "frag-tier-recovry": "intimate",
    "frag-tier-general": "open",
    "frag-tier-dm0001": "personal",
    "frag-tier-essay01": "open",
}
"""The exact id→tier map ``_seed_tier_corpus`` must produce (AC-2)."""


def test_run_classify_assigns_the_exact_privacy_tier_per_fragment(
    tmp_path: Path,
) -> None:
    """``creek classify --method rules`` stamps the right tier on each fragment.

    AC-2 for issue #876. Five fragments, five deliberate outcomes across
    three distinct tiers, asserted as one exact ``{id: tier}`` mapping read
    back off disk. Before the fix every value in this map was
    ``unclassified``.
    """
    vault = tmp_path / "vault"
    _seed_tier_corpus(vault)

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert _tier_map(vault) == _EXPECTED_TIER_MAP
    assert summary.privacy_tiers_assigned == 5


def test_run_classify_never_persists_unclassified_tier(tmp_path: Path) -> None:
    """No file under ``01-Fragments`` retains ``privacy_tier: unclassified``.

    The #934 pin. ``creek_mcp.source_tiers.fragment_tier`` and
    ``creek_mcp.tools.reflect._fragment_tier`` fail closed to INTIMATE only
    when the ``privacy_tier`` key is **absent**; an *explicit*
    ``unclassified`` is admitted at every ceiling. So a vault full of
    explicitly-unclassified fragments is not "safely unknown" — it is the
    whole private corpus exposed at the open tier. The engine's invariant
    is therefore stronger than "eventually classify": it must never write
    ``unclassified`` back to disk at all.

    The corpus deliberately includes one fragment written through the raw
    frontmatter path so its ``privacy_tier`` key is genuinely **missing**,
    not merely defaulted — that file must come out of the run with a real
    tier too.
    """
    from tests.helpers import write_raw_fragment_file

    vault = tmp_path / "vault"
    _seed_tier_corpus(vault)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        "frag-tier-nokey01",
        "No tier key at all",
        body=_TIER_NEUTRAL_BODY,
        platform="journal",
        author="self",
        privacy_tier=None,  # the key must be genuinely ABSENT, not defaulted
    )
    # Precondition: the key really is absent before the run, otherwise this
    # test would silently degrade into a duplicate of the AC-2 mapping test.
    raw_before = frontmatter.load(
        str(vault / "01-Fragments" / "Notes" / "frag-tier-nokey01.md"),
    ).metadata
    assert "privacy_tier" not in raw_before

    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    tiers = _tier_map(vault)
    assert len(tiers) == 6
    assert "unclassified" not in tiers.values()
    assert "<absent>" not in tiers.values()
    assert tiers["frag-tier-nokey01"] == "intimate"


def test_preserved_llm_and_manual_fragments_still_get_a_tier(
    tmp_path: Path,
) -> None:
    """Preserved fragments get a tier without ``--force``, and nothing else moves.

    The 35k demo-vault shape: every fragment already carries
    ``classification_method: llm`` or ``manual``, so the OPS-001 /
    issue-#321 short-circuit skips them and — before this fix — they could
    never acquire a tier at all. The tier pass therefore runs ABOVE that
    short-circuit and persists through a narrow tier-only writer that
    touches ONLY ``privacy_tier`` (and the derived
    ``voice_proxy_eligible``): the classification provenance and the body
    must come out byte-identical, and the preserved counters must not move.
    """
    from creek.models import Authorship

    vault = tmp_path / "vault"
    stamps: dict[str, object] = {
        "classified_at": "2020-01-01T00:00:00-08:00",
        "classification_reasoning": "a trace from the prior paid run",
        "classification_provider": "ollama",
    }
    llm_path = _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-presrv-llm01",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
        method="llm",
        extras=dict(stamps),
    )
    manual_path = _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-presrv-man01",
            title="On public transit",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
        method="manual",
        extras=dict(stamps),
    )
    before = {path: frontmatter.load(str(path)) for path in (llm_path, manual_path)}

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    # The tiers actually landed, distinct per fragment, read back off disk.
    assert _tier_map(vault) == {
        "frag-presrv-llm01": "intimate",
        "frag-presrv-man01": "open",
    }
    assert summary.privacy_tiers_assigned == 2

    # …and the preserved short-circuit still preserved everything else.
    assert summary.preserved_llm == 1
    assert summary.preserved_manual == 1
    for path, original in before.items():
        after = frontmatter.load(str(path))
        assert after.content == original.content, f"body rewritten for {path.name}"
        for key in (
            "classification_method",
            "classified_at",
            "classification_reasoning",
            "classification_provider",
        ):
            assert after.metadata[key] == original.metadata[key], (
                f"{key} rewritten for {path.name}"
            )

    # ``voice_proxy_eligible`` is derived from the tier (BUG-009), so the
    # tier-only write must carry it along or the two fall out of sync.
    assert frontmatter.load(str(llm_path)).metadata["voice_proxy_eligible"] is False
    assert frontmatter.load(str(manual_path)).metadata["voice_proxy_eligible"] is True


def test_router_sees_the_real_tier_on_first_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journal fragment routes LOCAL on its FIRST classify (#666 / ADR-0003).

    The security payload of issue #876. The per-tier router reads
    ``tier_of(fragment)`` to pick a provider; with the fragment still at
    ``unclassified`` on disk, that resolved to "not INTIMATE" and the
    journal entry was shipped to the configured CLOUD provider on the very
    run that was supposed to classify it. The tier pre-pass therefore has
    to run BEFORE ``tier_classifiers.for_tier(...)``, not after.

    Asserted on the recorded ``{fragment_id: provider}`` map (the
    ``_FakeClassifier`` / ``_CALLS`` shape from
    ``tests/test_classify_tier_routing.py``), not on a return value — so a
    regression that leaks Intimate content to the cloud fails here.
    """
    from creek.config import LLMConfig, LLMRoutingConfig
    from creek.models import Authorship

    calls: dict[str, str] = {}

    class _RecordingClassifier:
        """Records which provider was handed which fragment; never calls out."""

        def __init__(self, config: LLMConfig) -> None:
            """Capture the resolved config — its ``provider`` is the assertion."""
            self.config = config

        @property
        def available(self) -> bool:
            """Always reachable; the availability gate is not under test."""
            return True

        def classify_with_reasoning(
            self,
            fragment: Fragment,
            content: str = "",
        ) -> LLMClassificationResult:
            """Record ``id -> provider`` and echo the fragment back unchanged."""
            _ = content
            assert fragment.id not in calls, f"classified {fragment.id} twice"
            calls[fragment.id] = self.config.provider
            return LLMClassificationResult(fragment=fragment, reasoning="")

    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier",
        _RecordingClassifier,
    )

    vault = tmp_path / "vault"
    for frag_id, platform in (
        ("frag-route-journl", SourcePlatform.JOURNAL),
        ("frag-route-essay1", SourcePlatform.ESSAY),
    ):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=frag_id,
                title="A note",
                source=FragmentSource(platform=platform, author=Authorship.SELF),
            ),
            body=_TIER_NEUTRAL_BODY,
        )
    # Precondition: both fragments start at ``unclassified`` on disk, so this
    # really is the FIRST classification, not a re-run over an already-tiered
    # vault (which would pass even with the bug present).
    assert set(_tier_map(vault).values()) == {"unclassified"}

    config = CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="anthropic", model="claude-haiku-4-5"),
        ),
    )
    config.classification.confidence_threshold = 1.0  # force the LLM path

    run_classify(vault_path=vault, config=config, method="llm", force=False)

    assert calls == {
        "frag-route-journl": "ollama",  # Intimate → redirected to local
        "frag-route-essay1": "anthropic",  # non-Intimate → configured cloud
    }


def test_classification_signals_escalate_the_tier(tmp_path: Path) -> None:
    """A confessional+conviction verdict lifts the persisted tier to intimate.

    The pre-pass runs before the classifier, so it can only see the axes
    already on the fragment. When classification then *hardens* the
    signal — a self-authored, non-journal fragment coming back
    ``confessional`` + ``conviction`` — the escalate-only reassess after
    ``audience.classify_and_enforce`` has to raise ``personal`` to
    ``intimate`` before the write, or the harder signal is thrown away.

    Three fragments with three distinct expected tiers, so the assertion
    cannot pass by accidentally escalating everything.
    """
    from creek.models import Authorship, Confidence, VoiceClassification, VoiceRegister

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-escal-conf01",
            title="A hard thing",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-escal-essay1",
            title="On public transit",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-escal-dm0001",
            title="Scheduling",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                author=Authorship.SELF,
                channel="dm",
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )

    confessional = VoiceClassification(
        voice_register=VoiceRegister.CONFESSIONAL,
        confidence=Confidence.CONVICTION,
    )

    def _harden(fragment: Fragment, content: str = "") -> LLMClassificationResult:
        """Return the confessional+conviction verdict for the target fragment."""
        _ = content
        if fragment.id != "frag-escal-conf01":
            return LLMClassificationResult(fragment=fragment, reasoning="")
        return LLMClassificationResult(
            fragment=fragment.model_copy(update={"voice": confessional}),
            reasoning="",
        )

    config = CreekConfig()
    config.classification.confidence_threshold = 1.0  # force the LLM path

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=_harden,
    ):
        run_classify(vault_path=vault, config=config, method="llm", force=False)

    assert _tier_map(vault) == {
        "frag-escal-conf01": "intimate",  # escalated by the LLM's verdict
        "frag-escal-essay1": "open",
        "frag-escal-dm0001": "personal",
    }


@pytest.mark.parametrize("force", [False, True])
def test_an_existing_intimate_tier_is_never_lowered(
    tmp_path: Path,
    force: bool,
) -> None:
    """An essay stamped ``intimate`` stays intimate — even under ``--force``.

    The converse of the escalate test: the heuristic would call these
    essay-platform fragments ``open``, but the tier already on disk is the
    stricter one. Auto-lowering is the one direction that leaks content, so
    both the pre-pass merge and the post-classification reassess must be
    escalate-only. Three fragments with distinct expected tiers keep the
    assertion honest.
    """
    from creek.models import Authorship, PrivacyTier

    vault = tmp_path / "vault"
    for frag_id, tier, platform, channel in (
        ("frag-nolow-intim1", PrivacyTier.INTIMATE, SourcePlatform.ESSAY, None),
        ("frag-nolow-person", PrivacyTier.PERSONAL, SourcePlatform.ESSAY, None),
        ("frag-nolow-open01", PrivacyTier.OPEN, SourcePlatform.ESSAY, None),
    ):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=frag_id,
                title="On public transit",
                source=FragmentSource(
                    platform=platform,
                    author=Authorship.SELF,
                    channel=channel,
                ),
                privacy_tier=tier,
            ),
            body=_TIER_NEUTRAL_BODY,
        )

    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=force,
    )

    assert _tier_map(vault) == {
        "frag-nolow-intim1": "intimate",
        "frag-nolow-person": "personal",
        "frag-nolow-open01": "open",
    }


def test_manual_tier_override_survives_a_non_force_run(tmp_path: Path) -> None:
    """An explicit ``privacy_tier: open`` is untouched by a non-force pass.

    The operator's deliberate decision outranks the heuristic: the
    heuristic would call this journal fragment ``intimate``. Two contrast
    fragments (one untiered journal, one Discord DM) prove the run really
    did do tier work — otherwise a no-op implementation would pass.
    """
    from creek.models import Authorship, PrivacyTier

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-manual-open1",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
            privacy_tier=PrivacyTier.OPEN,
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-manual-jrnl1",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-manual-dm001",
            title="Scheduling",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                author=Authorship.SELF,
                channel="dm",
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert _tier_map(vault) == {
        "frag-manual-open1": "open",  # operator's call, kept
        "frag-manual-jrnl1": "intimate",
        "frag-manual-dm001": "personal",
    }
    assert summary.privacy_tiers_assigned == 2  # the override was NOT re-assigned


def test_second_classify_run_assigns_no_tiers_and_changes_none(
    tmp_path: Path,
) -> None:
    """The tier pass is idempotent: run two reports zero assignments.

    ``classified_at`` is legitimately re-stamped on the non-preserved path,
    so a whole-file byte comparison would be meaningless here; the durable
    contract is that the id→tier mapping is identical and the new counter
    reports zero work on the second pass.
    """
    vault = tmp_path / "vault"
    _seed_tier_corpus(vault)

    first = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )
    after_first = _tier_map(vault)

    second = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert first.privacy_tiers_assigned == 5
    assert second.privacy_tiers_assigned == 0
    assert after_first == _EXPECTED_TIER_MAP
    assert _tier_map(vault) == _EXPECTED_TIER_MAP


def test_reatomized_children_carry_a_concrete_tier(tmp_path: Path) -> None:
    """FEAT-023 child fragments are persisted with a real tier, never unclassified.

    Re-atomization writes brand-new files through the ``VaultWriter``, a
    write path the root fragment never travels. If the children inherit the
    pre-tier root (or are minted fresh), the split silently re-introduces
    ``privacy_tier: unclassified`` rows into a vault the classify run just
    finished cleaning — and the parent here is a journal entry, so those
    rows would be intimate content sitting at the open tier.
    """
    from creek.models import Authorship

    vault = tmp_path / "vault"
    root = Fragment(
        id="frag-reatom-root1",
        title="Morning pages",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
        ),
    )
    _write_fragment(vault=vault, fragment=root, body=_MULTI_PARAGRAPH_BODY)

    config = CreekConfig()
    config.classification.reatomize = True
    config.classification.confidence_threshold = 1.0

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify_with_reasoning",
        side_effect=lambda f, content="": LLMClassificationResult(
            fragment=f,
            reasoning="",
        ),
    ):
        run_classify(vault_path=vault, config=config, method="llm", force=False)

    # Every persisted fragment except the root came from the splitter. Keyed
    # by id (never by rglob position) so the assertion is order-independent.
    tiers = _tier_map(vault)
    children = {fid: tier for fid, tier in tiers.items() if fid != root.id}
    assert children, "FEAT-023 splitter wrote no child fragments to assert on"
    assert "unclassified" not in children.values()
    assert "<absent>" not in children.values()
    assert tiers[root.id] == "intimate"


def test_tier_only_write_failure_lands_on_errors_without_aborting(
    tmp_path: Path,
) -> None:
    """An ``OSError`` from the tier-only writer is recorded, not fatal.

    Contract note for the implementation: this test patches
    ``creek.classify.classify_engine._write_tier_only`` — the narrow writer
    that stamps ONLY ``privacy_tier`` + ``voice_proxy_eligible`` onto a
    preserved fragment. A single unwritable file must not abort a 35k-file
    run, so the failure is appended to ``summary.errors`` and the remaining
    fragments still classify normally.
    """
    from creek.models import Authorship

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tierio-fail1",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
        method="llm",
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-tierio-ok001",
            title="Scheduling",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                author=Authorship.SELF,
                channel="dm",
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )

    with patch(
        "creek.classify.classify_engine._write_tier_only",
        side_effect=OSError("read-only file system"),
    ):
        summary = run_classify(
            vault_path=vault,
            config=CreekConfig(),
            method="rules",
            force=False,
        )

    assert len(summary.errors) == 1
    assert "read-only file system" in summary.errors[0]
    assert "frag-tierio-fail1" in summary.errors[0]
    # The run continued: the other fragment still got its real tier.
    assert _tier_map(vault)["frag-tierio-ok001"] == "personal"


# ---- Praxis-potential pass (issue #877) ------------------------------------
#
# Same discipline as the #876 tier block above: seed fragments with known
# ids, read the whole tree back off disk, and assert an explicit
# ``{fragment_id: praxis_potential}`` mapping. ``rglob`` does not sort, so
# never index into its output — a positional assertion can false-green
# against the live bug (before #877 every fragment in the 35k-fragment demo
# vault read ``praxis_potential: none``, so "the first file says none" was
# trivially true for the *wrong* reason).

_PRAXIS_SIGNAL_BODY = "notes from the week ahead\n- [ ] book the boiler service"
"""A body carrying exactly one strong praxis marker (a task checkbox).

Deliberately free of frequency / phase / voice keywords so the rule
classifier's other axes cannot move and confuse the assertion, and free of
recovery keywords so the #876 tier heuristic stays driven by the platform.
"""


def _praxis_map(vault: Path) -> dict[str, str]:
    """Return ``{fragment_id: praxis_potential}`` read back off disk.

    Reads EVERY markdown file under ``01-Fragments`` (recursively) and keys
    the result by fragment id, so the assertion is independent of
    filesystem iteration order.
    """
    out: dict[str, str] = {}
    for path in (vault / "01-Fragments").rglob("*.md"):
        meta = frontmatter.load(str(path)).metadata
        if meta.get("type") != "fragment":
            continue
        out[str(meta["id"])] = str(meta.get("praxis_potential", "<absent>"))
    return out


def test_run_classify_rules_stamps_praxis_potential(tmp_path: Path) -> None:
    """A ``--method rules`` run writes ``praxis_potential`` to disk (AC-1, #877).

    The bug: ``Fragment.praxis_potential`` defaulted to ``none`` and *no*
    pipeline stage ever set it, so the three consumers gated on
    ``explicit`` (``generate/decisions.py``, ``generate/mining.py``,
    ``generate/compost.py``) were structurally unreachable and
    ``04-Praxis`` / ``08-Decisions`` could never be populated.

    Three fragments covering three distinct outcomes — raised, left alone,
    and already-decided — so the mapping cannot pass by stamping
    everything alike.
    """
    from creek.models import Authorship, PraxisPotential

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-praxis-sig01",
            title="Week notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body=_PRAXIS_SIGNAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-praxis-none1",
            title="Week notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-praxis-keep1",
            title="Week notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
            praxis_potential=PraxisPotential.EXPLICIT,
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    # Precondition: nothing is ``explicit`` by accident of the fixture
    # writer, and the signal-bearing fragment really does start at ``none``.
    assert _praxis_map(vault)["frag-praxis-sig01"] == "none"

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert _praxis_map(vault) == {
        "frag-praxis-sig01": "explicit",  # the checkbox fired
        "frag-praxis-none1": "none",  # a plain note stays none
        "frag-praxis-keep1": "explicit",  # never demoted by a signal-free run
    }
    # Only the genuine raise is counted; re-affirming an existing
    # ``explicit`` is not work the operator needs reported.
    assert summary.praxis_marked == 1


def test_second_run_marks_no_praxis_and_changes_none(tmp_path: Path) -> None:
    """The praxis pass is idempotent: run two reports zero marks.

    ``classified_at`` is legitimately re-stamped on the non-preserved
    path, so a byte comparison would be meaningless; the durable contract
    is that the id→praxis mapping is identical and the counter reports
    zero work the second time.
    """
    from creek.models import Authorship

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-praxis-idem1",
            title="Week notes",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                author=Authorship.SELF,
            ),
        ),
        body=_PRAXIS_SIGNAL_BODY,
    )

    first = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )
    after_first = _praxis_map(vault)

    second = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    assert first.praxis_marked == 1
    assert second.praxis_marked == 0
    assert after_first == {"frag-praxis-idem1": "explicit"}
    assert _praxis_map(vault) == after_first


@pytest.mark.parametrize(
    ("force", "expected_praxis", "expected_marked"),
    [(False, "none", 0), (True, "explicit", 1)],
)
def test_preserved_fragment_gets_praxis_only_under_force(
    tmp_path: Path,
    force: bool,
    expected_praxis: str,
    expected_marked: int,
) -> None:
    """A preserved ``llm``-stamped fragment gains praxis only with ``--force``.

    Deliberate asymmetry with the #876 tier pass. ``_write_tier_only`` is
    the narrow writer used on the OPS-001 resume short-circuit, and it is
    **not** widened to carry praxis: the tier exception exists because an
    untiered fragment is a live privacy hole, whereas a missing praxis
    verdict is a missing feature. Widening that writer would rewrite the
    praxis axis of every already-curated fragment in a mature vault
    without the operator asking.

    The tier still lands on the same run, which is what proves the
    short-circuit was genuinely taken rather than the fragment quietly
    reclassified.
    """
    from creek.models import Authorship

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-praxis-prsv1",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_PRAXIS_SIGNAL_BODY,
        method="llm",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=force,
    )

    assert _praxis_map(vault) == {"frag-praxis-prsv1": expected_praxis}
    assert _tier_map(vault) == {"frag-praxis-prsv1": "intimate"}
    assert summary.praxis_marked == expected_marked
    assert summary.preserved_llm == (0 if force else 1)


def test_praxis_pass_runs_after_the_router_reads_the_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding the praxis pass must not disturb the #876 tier→router ordering.

    The #876 ordering is load-bearing security, not style: the per-tier
    router calls ``tier_of(fragment)`` to honour Intimate-never-cloud
    (#666 / ADR-0003), so the privacy tier has to be assigned in
    ``_prepare_fragment`` — before ``TierClassifiers.for_tier`` — and
    above the resume short-circuit. The praxis pass belongs *after* the
    classifier returns (it reads the same body the classifier saw, and
    must not delay the tier), so this test asserts BOTH facts on one run:
    the recorded ``{fragment_id: provider}`` map is still correct, and the
    praxis verdict still landed.

    It exists so a future agent cannot "simplify" by hoisting the praxis
    pass into ``_prepare_fragment`` and, in the process, sliding the
    privacy pass below the router.
    """
    from creek.config import LLMConfig, LLMRoutingConfig
    from creek.models import Authorship

    calls: dict[str, str] = {}

    class _RecordingClassifier:
        """Records which provider was handed which fragment; never calls out."""

        def __init__(self, config: LLMConfig) -> None:
            """Capture the resolved config — its ``provider`` is the assertion."""
            self.config = config

        @property
        def available(self) -> bool:
            """Always reachable; the availability gate is not under test."""
            return True

        def classify_with_reasoning(
            self,
            fragment: Fragment,
            content: str = "",
        ) -> LLMClassificationResult:
            """Record ``id -> provider`` and echo the fragment back unchanged."""
            _ = content
            assert fragment.id not in calls, f"classified {fragment.id} twice"
            calls[fragment.id] = self.config.provider
            return LLMClassificationResult(fragment=fragment, reasoning="")

    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier",
        _RecordingClassifier,
    )

    vault = tmp_path / "vault"
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-order-journl",
            title="Morning pages",
            source=FragmentSource(
                platform=SourcePlatform.JOURNAL,
                author=Authorship.SELF,
            ),
        ),
        body=_PRAXIS_SIGNAL_BODY,
    )
    _write_fragment(
        vault=vault,
        fragment=Fragment(
            id="frag-order-essay1",
            title="On public transit",
            source=FragmentSource(
                platform=SourcePlatform.ESSAY,
                author=Authorship.SELF,
            ),
        ),
        body=_TIER_NEUTRAL_BODY,
    )
    # Precondition: this really is the FIRST pass over both fragments.
    assert set(_tier_map(vault).values()) == {"unclassified"}
    assert set(_praxis_map(vault).values()) == {"none"}

    config = CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="anthropic", model="claude-haiku-4-5"),
        ),
    )
    config.classification.confidence_threshold = 1.0  # force the LLM path

    run_classify(vault_path=vault, config=config, method="llm", force=False)

    assert calls == {
        "frag-order-journl": "ollama",  # Intimate → redirected to local
        "frag-order-essay1": "anthropic",  # non-Intimate → configured cloud
    }
    assert _praxis_map(vault) == {
        "frag-order-journl": "explicit",
        "frag-order-essay1": "none",
    }
