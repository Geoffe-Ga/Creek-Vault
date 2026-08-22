"""On-disk evidence survival across a weighted classify run (issue #1309).

The in-memory merge is covered by ``tests/test_weighted.py``. This module
asserts the half that only a real ``run_classify`` over a real vault can
show: that the restored ``voice.confidence`` actually reaches the written
frontmatter, and that the INTIMATE escalation it triggers is durable across
re-runs.

Why the written file and not the returned ``Fragment``: the write path is
``new_metadata.update(fragment.model_dump(mode="json"))`` with **no**
``exclude_none``, so a nulled confidence really does land on disk as
``confidence: null``. An in-memory assertion cannot see that.

``_RULE_INERT_BODY`` and the explicit precondition assertions were written
here as the visible boundary of a defect that is now closed. When #1309
landed, ``RuleClassifier._build_updates`` still rebuilt ``VoiceClassification``
from scratch under an ``OR`` guard, so for any body its voice matcher fired
on, a persisted ``confidence`` was destroyed *before* the weighted classifier
was ever called and no downstream merge could recover it — which meant these
tests could only prove the weighted path closed **for the fragments the rule
pass left alone**. Issue #1331 fixed that rules-layer twin, so the rule pass
now merges its verdict like this one does and the caveat is retired.

The inert fixture stays, and so do the assertions, for a different and better
reason: these tests are about the *weighted* merge, and a body that fires the
rule matchers would let the rules layer explain a passing result. Keeping the
fixture inert keeps the subject of the measurement unambiguous, and
:func:`_assert_rule_pass_preserves_evidence` now pins a guarantee rather than
hedging a known gap. The general proof that the rules layer preserves evidence
for *any* body lives in ``tests/test_rules_preserves_evidence.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.privacy import PrivacyClassifier
from creek.classify.rules import RuleClassifier
from creek.config import (
    ClassificationConfig,
    CreekConfig,
    LLMConfig,
    LLMRoutingConfig,
)
from creek.models import (
    Authorship,
    Color,
    Confidence,
    Fragment,
    FragmentSource,
    Mode,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_AVAILABILITY: Final[str] = "creek.classify.llm.LLMClassifier._check_availability"
"""Availability hook on the class ``classify_weighted`` builds itself."""

_INVOKE: Final[str] = "creek.classify.llm.LLMClassifier.invoke_prompt"
"""Transport hook on the class ``classify_weighted`` builds itself."""

_FRAGMENT_ID: Final[str] = "frag-1309-on-disk"

_RULE_INERT_BODY: Final[str] = (
    "A plain paragraph about gardening tools and afternoon light."
)
"""A body the rule classifier has no opinion about.

Load-bearing: see the module docstring. Verified by an explicit precondition
assertion in every test below rather than trusted.
"""

_RULE_INERT_TITLE: Final[str] = "A quiet note"
"""A title the rule classifier has no opinion about either.

The rule matchers read the title as well as the body, and that bites. An
earlier draft titled this fragment "Evidence bearing fragment"; the word
"evidence" trips ``_match_voice_register`` into an ``analytical`` verdict.
Before #1331 that fired the ``OR`` guard at ``rules.py:791`` and destroyed the
persisted ``confidence`` before the weighted classifier was reached, turning
these tests red for a reason that had nothing to do with what they measure;
the precondition assertions caught it. The destruction is fixed, but the inert
title is kept so a rule-matcher verdict can never be the thing that explains a
pass here — see the module docstring.
"""

_CONFESSIONAL_CONVICTION_RESPONSE: Final[str] = """\
The author is disclosing something personal and holds it firmly.

```yaml
frequencies:
  - value: F6
    weight: 0.8
voice_registers:
  - value: confessional
    weight: 0.9
confidences:
  - value: conviction
    weight: 0.9
overall_confidence: 0.7
```
"""

_SILENT_ON_VOICE_RESPONSE: Final[str] = """\
Only a frequency reading here.

```yaml
frequencies:
  - value: F6
    weight: 0.8
overall_confidence: 0.7
```
"""


def _respond(payload: str) -> object:
    """Build an ``invoke_prompt`` replacement returning *payload*.

    Args:
        payload: The canned model response.

    Returns:
        A function suitable for ``patch(..., new=...)``.
    """

    def _fake(self: object, prompt: str) -> str:
        del self, prompt
        return payload

    return _fake


def _weighted_config() -> CreekConfig:
    """Build a local-provider config that forces the weighted LLM path.

    ``confidence_threshold=1.0`` is above anything the rule classifier can
    score, so the fragment always reaches the LLM branch; ``ollama`` keeps
    every route local so the run needs no credentials.

    Returns:
        The assembled :class:`CreekConfig`.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="ollama", model="qwen3:8b"),
            intimate=LLMConfig(provider="ollama", model="qwen3:8b"),
        ),
        classification=ClassificationConfig(
            weighted_classification=True,
            confidence_threshold=1.0,
        ),
    )


def _seed_with_evidence(vault: Path) -> Path:
    """Write one fragment already carrying voice and wavelength evidence.

    Args:
        vault: Vault root (created on demand).

    Returns:
        Path to the freshly-written fragment file.
    """
    return write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=_FRAGMENT_ID,
            title=_RULE_INERT_TITLE,
            source=FragmentSource(
                platform=SourcePlatform.ESSAY, author=Authorship.SELF
            ),
            voice=VoiceClassification(
                voice_register=VoiceRegister.ANALYTICAL,
                confidence=Confidence.CONVICTION,
            ),
            wavelength=WavelengthClassification(
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                color=Color.GREEN,
                descriptor="Social Anxiety",
            ),
        ),
        body=_RULE_INERT_BODY,
    )


def _assert_rule_pass_preserves_evidence(fragment: Fragment) -> None:
    """Assert the rules layer leaves this fragment's evidence intact.

    Since #1331 this is a *guarantee* the rule pass makes for any body — it
    merges its verdict instead of rebuilding the sub-models — rather than the
    fixture-dependent precondition it was when #1309 wrote it. It is kept
    because these tests measure the weighted merge specifically: if it ever
    fails, the rules layer has started moving the very axes the assertions
    downstream attribute to ``merge_onto``, and those assertions would be
    measuring the wrong component.

    Args:
        fragment: The seeded fragment, before classification.
    """
    pre = RuleClassifier().classify(fragment, content=_RULE_INERT_BODY)
    assert pre.voice.confidence == Confidence.CONVICTION
    assert pre.wavelength.descriptor == "Social Anxiety"
    assert pre.wavelength.mode == Mode.EXPRESS


def _seeded_fragment() -> Fragment:
    """Return the in-memory twin of the seeded fragment.

    Returns:
        A :class:`Fragment` matching what :func:`_seed_with_evidence` writes.
    """
    return Fragment(
        id=_FRAGMENT_ID,
        title=_RULE_INERT_TITLE,
        source=FragmentSource(platform=SourcePlatform.ESSAY, author=Authorship.SELF),
        voice=VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        ),
        wavelength=WavelengthClassification(
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            color=Color.GREEN,
            descriptor="Social Anxiety",
        ),
    )


def test_persisted_confidence_survives_a_weighted_run_on_disk(
    tmp_path: Path,
) -> None:
    """A frontmatter ``confidence: conviction`` is still there afterwards.

    At HEAD the weighted run replaced ``voice`` wholesale from ``to_legacy``,
    and because the write does no ``exclude_none`` the frontmatter came back
    carrying a literal ``confidence: null``.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    _assert_rule_pass_preserves_evidence(_seeded_fragment())
    vault = tmp_path / "vault"
    md = _seed_with_evidence(vault)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_respond(_SILENT_ON_VOICE_RESPONSE)),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    assert summary.classified == 1
    meta = frontmatter.load(md).metadata
    # The model said nothing about voice or wavelength, so the fragment's
    # own evidence stands — on disk, not merely in memory.
    assert meta["voice"]["confidence"] == "conviction"
    assert meta["voice"]["voice_register"] == "analytical"
    assert meta["wavelength"]["descriptor"] == "Social Anxiety"
    assert meta["wavelength"]["mode"] == "express"
    # What the model DID say is applied.
    assert meta["frequency"]["primary"] == "F6"


def test_weighted_run_escalates_to_intimate_on_disk(tmp_path: Path) -> None:
    """A confessional+conviction verdict reaches ``privacy_tier: intimate``.

    Asserted as an escalation UP from the ESSAY baseline of ``open``, not as
    "the tier did not go down" — every writer of ``privacy_tier`` is already
    escalate-only, so a no-downgrade assertion passes at HEAD and proves
    nothing.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    fragment = _seeded_fragment()
    _assert_rule_pass_preserves_evidence(fragment)
    # The test proves its own baseline: without the model's verdict this
    # fragment is OPEN, so INTIMATE below is a real escalation.
    assert (
        PrivacyClassifier().classify_tier(fragment, content=_RULE_INERT_BODY)
        == PrivacyTier.OPEN
    )

    vault = tmp_path / "vault"
    md = _seed_with_evidence(vault)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_respond(_CONFESSIONAL_CONVICTION_RESPONSE)),
    ):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    assert meta["voice"]["voice_register"] == "confessional"
    assert meta["voice"]["confidence"] == "conviction"
    assert meta["privacy_tier"] == "intimate"


def test_second_weighted_run_keeps_the_tier_and_its_evidence(
    tmp_path: Path,
) -> None:
    """Re-running does not undo the escalation or erase what justifies it.

    ``classify_engine``'s stated invariant is that the escalation "prevents
    every *future* egress" for a fragment. On the weighted path at HEAD it
    prevented none of them: every re-run nulled the confidence again, so the
    fragment routed to the cloud on every run rather than once.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    _assert_rule_pass_preserves_evidence(_seeded_fragment())
    vault = tmp_path / "vault"
    md = _seed_with_evidence(vault)
    config = _weighted_config()

    for _ in range(2):
        with (
            patch(_AVAILABILITY, return_value=True),
            patch(_INVOKE, new=_respond(_CONFESSIONAL_CONVICTION_RESPONSE)),
        ):
            run_classify(
                vault_path=vault,
                config=config,
                method="llm",
                force=True,
            )

    meta = frontmatter.load(md).metadata
    assert meta["privacy_tier"] == "intimate"
    assert meta["voice"]["confidence"] == "conviction"
    assert meta["voice"]["voice_register"] == "confessional"


def test_pre_fix_frontmatter_without_confidences_still_classifies(
    tmp_path: Path,
) -> None:
    """A vault written by the pre-#1309 pipeline still runs (#1309).

    The new axis adds a key to every fragment's ``weighted:`` block, so a
    fragment persisted before the change must still validate on read.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    vault = tmp_path / "vault"
    folder = vault / "01-Fragments" / "Markdown"
    folder.mkdir(parents=True)
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    md = folder / "frag-prefix.md"
    md.write_text(
        "---\ntype: fragment\nid: frag-prefix\ntitle: Pre-fix fragment\n"
        "source:\n  platform: markdown\n"
        "weighted:\n"
        "  frequencies:\n    - value: F3\n      weight: 0.8\n"
        "  voice_registers:\n    - value: analytical\n      weight: 0.5\n"
        "  overall_confidence: 0.7\n"
        f"---\n{_RULE_INERT_BODY}\n",
        encoding="utf-8",
    )

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_respond(_SILENT_ON_VOICE_RESPONSE)),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    assert summary.classified == 1
    meta = frontmatter.load(md).metadata
    assert meta["weighted"]["confidences"] == []


@pytest.mark.parametrize("stance", ["musing", "exploring"])
def test_a_tentative_confessional_is_not_buried_on_disk(
    tmp_path: Path,
    stance: str,
) -> None:
    """A tentative stance must not reach INTIMATE (#1309).

    Under the escalate-only ratchet a manufactured escalation is permanent,
    so over-burying is as much a defect as under-burying.

    Args:
        tmp_path: Pytest-provided scratch directory.
        stance: The non-conviction stance the model reports.
    """
    vault = tmp_path / "vault"
    md = _seed_with_evidence(vault)
    payload = _CONFESSIONAL_CONVICTION_RESPONSE.replace("conviction", stance)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_respond(payload)),
    ):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    # The stance really did round-trip, so this is not passing vacuously
    # because the axis was dropped...
    assert meta["voice"]["confidence"] == stance
    # ...and it did not trigger the burial.
    assert meta["privacy_tier"] != "intimate"
