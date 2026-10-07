"""Heal fragments already mis-stamped ``llm`` by the #1330 bug (#1357).

#1330/#1358 stopped the engine *writing* the lie. It could not repair what
earlier runs already wrote: a fragment carrying ``classification_method: llm``
for a weighted call that never produced anything. Those fragments are
permanently ``unclassified`` and permanently skipped, because
``_record_if_preserved`` reads an ``llm`` stamp as "already paid for".

The on-disk signature under test is the exact output of the pre-#1358
:func:`~creek.classify.classify_engine._classify_one_weighted`, which assigned
``WeightedFragmentClassification()`` wholesale via ``to_legacy()``:

* ``classification_method: llm``
* a ``weighted:`` block that is **present and entirely vacuous** — every
  dimension list empty, ``emotional_texture`` empty, ``overall_confidence:
  0.0``, ``reasoning: ''``
* ``frequency.primary: unclassified`` with no secondaries, and every
  ``wavelength`` axis ``unclassified`` with ``descriptor: ''``

:func:`_poison` reproduces that write rather than hand-typing frontmatter, so
the fixture cannot drift away from the defect it stands for. The
false-positive controls are the point of the module: T2 classifies a fragment
through a *succeeding* weighted run and asserts the detector stays silent
about the artifact that run wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from unittest.mock import patch

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.constants import (
    CLASSIFICATION_METHOD_KEY,
    CLASSIFICATION_PROVIDER_KEY,
)
from creek.classify.provenance_pass import has_unearned_llm_stamp
from creek.classify.weighted import WeightedFragmentClassification
from creek.config import (
    ClassificationConfig,
    CreekConfig,
    LLMConfig,
    LLMRoutingConfig,
)
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.reader import iter_vault_fragments
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.llm.orchestrator import LLMClassificationResult

_ENGINE_CLASSIFIER: Final[str] = "creek.classify.classify_engine.LLMClassifier"
"""The engine's own classifier reference — swapped for a stub wholesale."""

_AVAILABILITY: Final[str] = "creek.classify.llm.LLMClassifier._check_availability"
"""Availability hook on the real class ``classify_weighted`` builds itself."""

_INVOKE: Final[str] = "creek.classify.llm.LLMClassifier.invoke_prompt"
"""Transport hook on the real class ``classify_weighted`` builds itself."""

F3_BODY: Final[str] = "I feel rage and anger and power and dominance and conflict."
"""A body the rule classifier scores as ``F3``."""

VALID_WEIGHTED_RESPONSE: Final[str] = """The fragment lands at F3 / Red.

```yaml
frequencies:
  - value: F3
    weight: 0.8
phases:
  - value: rising
    weight: 0.7
overall_confidence: 0.7
```
"""
"""A well-formed weighted response, so the run reaches ``merge_onto``."""


# ---- Provider stubs -------------------------------------------------------


class _EngineStub:
    """Stands in for the *engine's* ``LLMClassifier``.

    ``classify_weighted`` builds its own classifier through a function-local
    import, so the engine's up-front availability check has to be satisfied
    separately from the call under test. The single-pick entry point raises so
    a fall-through off the weighted path fails loudly.
    """

    def __init__(self, config: LLMConfig) -> None:
        """Retain the routed config the weighted path re-uses.

        Args:
            config: The per-stage config the ModelRouter resolved.
        """
        self.config = config

    @property
    def available(self) -> bool:
        """Report the engine-side provider as reachable.

        Returns:
            Always ``True``.
        """
        return True

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Fail loudly: a weighted run must never reach the single-pick call.

        Args:
            fragment: Unused; the call is a contract violation.
            content: Unused; the call is a contract violation.

        Returns:
            Never returns.

        Raises:
            AssertionError: Always.
        """
        del fragment, content
        msg = "weighted run must not fall through to classify_with_reasoning"
        raise AssertionError(msg)


def _return_valid_weighted_yaml(self: object, prompt: str) -> str:
    """Return a well-formed weighted classification payload.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        :data:`VALID_WEIGHTED_RESPONSE`.
    """
    del self, prompt
    return VALID_WEIGHTED_RESPONSE


def _raise_transport_error(self: object, prompt: str) -> str:
    """Simulate a provider transport failure.

    Args:
        self: Unused bound instance.
        prompt: Unused prompt text.

    Returns:
        Never returns.

    Raises:
        OSError: Always.
    """
    del self, prompt
    msg = "network down"
    raise OSError(msg)


# ---- Fixtures -------------------------------------------------------------


def _weighted_config() -> CreekConfig:
    """Build a local-provider config that forces the weighted LLM path.

    ``confidence_threshold=1.0`` is above anything the rule classifier can
    score, so every fragment reaches the LLM branch; ``ollama`` keeps both
    routes local so the run never needs credentials.

    Returns:
        The assembled :class:`CreekConfig`.
    """
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="ollama", model="qwen3:8b"),
        ),
        classification=ClassificationConfig(
            weighted_classification=True,
            confidence_threshold=1.0,
        ),
    )


def _poison(vault: Path, fragment_id: str = "frag-0000poisoned") -> Path:
    """Write the exact artifact the pre-#1358 weighted path persisted.

    Reproduces the defective ``model_copy`` — an empty
    :class:`WeightedFragmentClassification` assigned wholesale together with
    its ``to_legacy()`` collapse — rather than hand-typing the frontmatter, so
    the fixture tracks the model rather than a transcription of it.

    Args:
        vault: Vault root (created on demand).
        fragment_id: Id for the seeded fragment.

    Returns:
        Path to the freshly-written poisoned file.
    """
    empty = WeightedFragmentClassification()
    freq, wave, voice = empty.to_legacy()
    fragment = Fragment(
        id=fragment_id,
        title="Poisoned by a failed weighted call",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    ).model_copy(
        update={
            "weighted": empty,
            "frequency": freq,
            "wavelength": wave,
            "voice": voice,
        },
    )
    return write_fragment_file(
        vault=vault,
        fragment=fragment,
        body=F3_BODY,
        method="llm",
        extras={CLASSIFICATION_PROVIDER_KEY: "ollama"},
    )


def _detect(path: Path, vault: Path) -> bool:
    """Load *path* the way every vault consumer does and run the detector.

    Args:
        path: The fragment file to judge.
        vault: Vault root holding it.

    Returns:
        The detector's verdict for that file.

    Raises:
        AssertionError: When *path* is not among the loadable fragments.
    """
    for found, fragment, _body, raw in iter_vault_fragments(vault / "01-Fragments"):
        if found == path:
            return has_unearned_llm_stamp(fragment, raw)
    msg = f"{path} was not loadable as a fragment"
    raise AssertionError(msg)


# ---- T1: the poisoned signature is detected -------------------------------


def test_detector_matches_the_poisoned_on_disk_signature(tmp_path: Path) -> None:
    """The artifact of a failed pre-#1358 weighted call is flagged.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    vault = tmp_path / "vault"
    md = _poison(vault)

    meta = frontmatter.load(md).metadata
    # Pin the fixture against the signature the issue describes, so a model
    # change that stopped producing this shape fails here rather than
    # silently making the detector untested.
    assert meta[CLASSIFICATION_METHOD_KEY] == "llm"
    assert meta["frequency"]["primary"] == "unclassified"
    assert meta["weighted"]["frequencies"] == []
    assert meta["weighted"]["overall_confidence"] == 0.0
    assert meta["weighted"]["reasoning"] == ""

    assert _detect(md, vault) is True


# ---- T2: a genuine LLM classification is never matched ---------------------


def test_detector_ignores_a_genuinely_llm_classified_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fragment written by a *succeeding* weighted run is left alone.

    The false-positive control, run against a real on-disk artifact rather
    than a hand-written one: a remediation that rewrites correct data is
    worse than the bug.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000genuine",
            title="Genuine",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=F3_BODY,
    )

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_return_valid_weighted_yaml),
    ):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )

    meta = frontmatter.load(md).metadata
    assert meta[CLASSIFICATION_METHOD_KEY] == "llm"
    assert _detect(md, vault) is False


@pytest.mark.parametrize(
    ("label", "method", "overrides"),
    [
        # The rules path never writes a weighted block at all.
        ("rules", "rules", {}),
        # Operator curation is untouchable regardless of shape.
        ("manual", "manual", {"weighted": WeightedFragmentClassification()}),
        # The single-pick LLM path leaves `weighted` unset; an unclassified
        # verdict there is a real verdict, not a failed call.
        ("single_pick_llm", "llm", {}),
        # A model that spoke — said "nothing fits", and explained why. Vacuous
        # dimensions, but the reasoning proves a call happened.
        (
            "llm_with_reasoning",
            "llm",
            {"weighted": WeightedFragmentClassification(reasoning="Nothing fits.")},
        ),
        # A model that reported its own confidence but named no dimension.
        (
            "llm_with_confidence",
            "llm",
            {"weighted": WeightedFragmentClassification(overall_confidence=0.4)},
        ),
    ],
)
def test_detector_ignores_every_neighbouring_shape(
    tmp_path: Path,
    label: str,
    method: str,
    overrides: dict[str, object],
) -> None:
    """Only the full conjunction matches; each near-miss stays silent.

    Args:
        tmp_path: Pytest-provided scratch directory.
        label: Human-readable name of the shape under test.
        method: ``classification_method`` to stamp.
        overrides: Fragment field overrides for this shape.
    """
    vault = tmp_path / "vault"
    md = write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=f"frag-0000{label}"[:20],
            title=label,
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ).model_copy(update=overrides),
        body=F3_BODY,
        method=method,
    )

    assert _detect(md, vault) is False


def test_detector_ignores_a_vacuous_block_over_a_real_verdict(
    tmp_path: Path,
) -> None:
    """A surviving frequency verdict rules the fragment out.

    The bug's wholesale ``to_legacy()`` assignment always flattened
    ``frequency`` to ``unclassified``, so a fragment that still names one
    cannot be its victim — whatever its weighted block looks like.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    from creek.models import Frequency, FrequencyClassification

    vault = tmp_path / "vault"
    md = write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000f3kept",
            title="F3 kept",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ).model_copy(
            update={
                "weighted": WeightedFragmentClassification(),
                "frequency": FrequencyClassification(primary=Frequency.F3),
            },
        ),
        body=F3_BODY,
        method="llm",
    )

    assert _detect(md, vault) is False


# ---- T3: an ordinary run heals it -----------------------------------------


def test_ordinary_llm_run_heals_a_poisoned_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek classify --method llm`` (no ``--force``) repairs the lie.

    The whole point of the heal: no vault-wide ``--force``, which would
    re-pay for every already-good fragment.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = _poison(vault)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_return_valid_weighted_yaml),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=False,
        )

    assert summary.healed_unearned_llm == 1
    assert summary.preserved_llm == 0
    assert summary.classified == 1
    meta = frontmatter.load(md).metadata
    assert meta["frequency"]["primary"] == "F3"
    assert meta["weighted"]["frequencies"] == [{"value": "F3", "weight": 0.8}]
    assert meta[CLASSIFICATION_METHOD_KEY] == "llm"


def test_ordinary_llm_run_still_preserves_a_genuine_llm_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OPS-001 resume contract survives the heal.

    A vault holding one poisoned and one genuine fragment must re-pay for
    exactly one of them.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    genuine = write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000genuine",
            title="Genuine",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=F3_BODY,
    )
    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_return_valid_weighted_yaml),
    ):
        run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=True,
        )
    before = frontmatter.load(genuine).metadata
    # Poisoned *after* the genuine fragment is established, so the seeding run
    # cannot heal it and the second run has exactly one of each to tell apart.
    _poison(vault)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_return_valid_weighted_yaml),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=False,
        )

    assert summary.healed_unearned_llm == 1
    assert summary.preserved_llm == 1
    after = frontmatter.load(genuine).metadata
    assert after["classified_at"] == before["classified_at"]


def test_heal_drops_the_fabricated_block_when_the_provider_is_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heal that cannot reach a model still leaves honest frontmatter.

    The fabricated all-zero ``weighted`` block is cleared, not carried over
    onto the ``rules`` stamp — otherwise the heal would trade one false claim
    ("an LLM classified this") for another ("a weighted detection ran").

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to swap the engine's classifier for a stub.
    """
    monkeypatch.setattr(_ENGINE_CLASSIFIER, _EngineStub)
    vault = tmp_path / "vault"
    md = _poison(vault)

    with (
        patch(_AVAILABILITY, return_value=True),
        patch(_INVOKE, new=_raise_transport_error),
    ):
        summary = run_classify(
            vault_path=vault,
            config=_weighted_config(),
            method="llm",
            force=False,
        )

    assert summary.healed_unearned_llm == 1
    meta = frontmatter.load(md).metadata
    assert meta[CLASSIFICATION_METHOD_KEY] == "rules"
    assert CLASSIFICATION_PROVIDER_KEY not in meta
    assert meta.get("weighted") is None
    assert meta["frequency"]["primary"] == "F3"
    # And the vault is now clean: a second run has nothing left to heal.
    assert _detect(md, vault) is False


# ---- T4: the count is visible to an operator ------------------------------


def test_fill_scan_counts_the_poisoned_population(tmp_path: Path) -> None:
    """``_scan_fill_gaps`` reports the mis-stamped fragments on its own field.

    Every other count reads a poisoned fragment as already classified, so a
    number folded into one of them would be invisible.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    import creek.cli as cli_mod

    vault = tmp_path / "vault"
    _poison(vault, "frag-0000poison1")
    _poison(vault, "frag-0000poison2")
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000plain",
            title="Plain",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=F3_BODY,
        method="rules",
    )

    # Positive control: a fixture the walk cannot load would make every
    # count below pass vacuously at zero.
    assert len(iter_vault_fragments(vault / "01-Fragments")) == 3

    scan = cli_mod._scan_fill_gaps(vault)
    assert scan.unearned_llm == 2
    assert cli_mod._count_unearned_llm_fragments(vault) == 2


def test_count_unearned_llm_is_zero_without_a_fragments_dir(tmp_path: Path) -> None:
    """A vault with no ``01-Fragments`` counts zero rather than exploding.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    import creek.cli as cli_mod

    assert cli_mod._count_unearned_llm_fragments(tmp_path / "empty-vault") == 0


def test_fill_hints_the_count_and_names_the_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``creek fill`` prints one line naming the count and the plain re-run.

    The remedy must not be ``--force``: that re-pays for the whole vault.
    And the line must carry no fragment id (#846 / #848).

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to silence the unrelated upgrade offer.
        capsys: Captures the hint output.
    """
    import creek.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    for index in range(3):
        _poison(vault, f"frag-0000poison{index}")

    assert len(iter_vault_fragments(vault / "01-Fragments")) == 3
    cli_mod._maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "classification_method" in line]
    assert len(lines) == 1, out
    hint = " ".join(lines)
    assert "3" in hint
    assert "--force" not in hint.replace("no --force", "")
    assert "frag-0000poison" not in out


def test_fill_is_silent_when_nothing_is_mis_stamped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean vault gets no nag.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Used to silence the unrelated upgrade offer.
        capsys: Captures the hint output.
    """
    import creek.cli as cli_mod

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000clean",
            title="Clean",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=F3_BODY,
        method="rules",
    )

    assert len(iter_vault_fragments(vault / "01-Fragments")) == 1
    cli_mod._maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    assert "classification_method" not in capsys.readouterr().out


def test_unearned_llm_hint_failure_never_crashes_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken count is swallowed by its own best-effort guard.

    Its own guard, not a sibling's: one shared ``try`` would let a single
    failing count silence the other four hints.

    Args:
        tmp_path: Pytest-provided scratch directory.
        monkeypatch: Injects the failing count.
    """
    import creek.cli as cli_mod

    def _boom(*_a: object) -> int:
        raise OSError("unreadable fragment")

    monkeypatch.setattr(cli_mod, "_count_unearned_llm_fragments", _boom)
    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)

    # Must not raise.
    cli_mod._maybe_upgrade_classification(
        tmp_path, cli_mod._load_config_for_vault(tmp_path), upgrade=False
    )


def test_classify_summary_reports_the_heal_even_at_zero(tmp_path: Path) -> None:
    """The CLI summary always carries the healed count.

    Reported at zero so "my vault holds no unearned stamps" is an answer the
    operator can read rather than an absence they have to infer.

    Args:
        tmp_path: Pytest-provided scratch directory.
    """
    from typer.testing import CliRunner

    from creek.cli import app

    vault = tmp_path / "vault"
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-0000clean",
            title="Clean",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=F3_BODY,
        method="rules",
    )

    result = CliRunner().invoke(app, ["classify", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    # Rich soft-wraps the summary at the terminal width, so compare against
    # the line with its wrapping collapsed rather than against raw output.
    unwrapped = " ".join(result.output.split())
    assert "0 unearned llm stamp(s) healed" in unwrapped
