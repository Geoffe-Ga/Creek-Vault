"""GAP-008: KeyboardInterrupt mid-classify must leave the progress JSONL well-formed.

The OPS-001 progress file at
``<vault>/00-Creek-Meta/Processing-Log/llm-progress.jsonl`` is what
makes resume-after-crash a reliable contract. If a ``KeyboardInterrupt``
(SIGINT / Ctrl-C) or any other thrown exception catches the engine
mid-write, the resulting file must still be line-by-line valid JSON —
half-written lines would either corrupt ``json.loads`` on resume or
silently re-classify the partially-written fragment. These exception
paths are what the tests below pin directly.

SIGTERM (laptop sleep / container reclamation) is a different case:
Python does not raise ``KeyboardInterrupt`` — or anything else — into
the interpreter on SIGTERM by default; the process simply terminates.
So the SIGTERM guarantee is not test-pinned. It rests instead on the
OS-level atomicity described below: the per-line write in
``_record_llm_progress`` opens the file in append mode and writes
``{id} + \\n`` as a single ``handle.write`` call. POSIX guarantees
that an O_APPEND write of < ``PIPE_BUF`` bytes (typically 4096) is
atomic across processes, and a per-fragment JSON line is ~30 bytes.
So the file's invariant — every non-empty line parses as JSON —
survives a hard SIGTERM between two write calls by construction, and
the exception-path tests below additionally pin it for interruptions
that *do* raise into Python.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import PropertyMock, patch

import pytest

from creek.classify.classify_engine import (
    LLM_PROGRESS_FILENAME,
    LLMClassifier,
    run_classify,
)
from creek.classify.llm import LLMClassificationResult
from creek.config import CreekConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _force_llm_available() -> object:
    """Bypass the LLM-availability gate (mirrors the resume-test fixture)."""
    with patch.object(
        LLMClassifier,
        "available",
        new_callable=PropertyMock,
        return_value=True,
    ) as patched:
        yield patched


def _llm_low_confidence_config() -> CreekConfig:
    """Force the engine to invoke the LLM on every fragment."""
    config = CreekConfig()
    config.classification.confidence_threshold = 1.0
    return config


def _seed_fragment(vault: Path, *, idx: int) -> None:
    """Write fragment ``idx`` with no rule signal so the LLM path runs."""
    fragment = Fragment(
        id=f"frag-interrupt-{idx:04d}",
        title=f"interrupt test {idx}",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="ambiguous body")


def test_keyboard_interrupt_mid_run_leaves_well_formed_progress_jsonl(
    tmp_path: Path,
) -> None:
    """``KeyboardInterrupt`` after N successful fragments leaves N valid JSONL lines.

    GAP-008 acceptance criterion 1. The user laptop-sleeps mid-run,
    SIGTERM kills the python process between fragments. On resume,
    every line in the progress file must parse as JSON — otherwise the
    next ``creek classify`` invocation either crashes on
    ``json.loads`` or silently re-classifies a partially-written
    fragment.
    """
    vault = tmp_path / "vault"
    for i in range(5):
        _seed_fragment(vault, idx=i)

    call_count = {"n": 0}
    interrupt_after = 3

    def flaky_classify(
        self: LLMClassifier,
        frag: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Stub: succeed N times, then raise KeyboardInterrupt."""
        del self, content
        call_count["n"] += 1
        if call_count["n"] > interrupt_after:
            raise KeyboardInterrupt
        return LLMClassificationResult(fragment=frag, reasoning="")

    with (
        patch.object(
            LLMClassifier,
            "classify_with_reasoning",
            new=flaky_classify,
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    progress_path = vault / "00-Creek-Meta" / "Processing-Log" / LLM_PROGRESS_FILENAME
    assert progress_path.is_file()

    raw = progress_path.read_text(encoding="utf-8")
    # File must end on a newline — never a half-written line.
    assert raw.endswith("\n"), (
        "progress JSONL must not end mid-line; last char was "
        f"{raw[-1]!r}, file ends with: {raw[-20:]!r}"
    )

    lines = [line for line in raw.splitlines() if line]
    assert len(lines) == interrupt_after, (
        f"expected {interrupt_after} progress lines (one per successful "
        f"classification before the interrupt), got {len(lines)}"
    )
    # Every line parses as JSON and contains the expected fragment-ID
    # shape. A torn write would fail this assertion.
    for line in lines:
        entry = json.loads(line)
        assert "id" in entry
        assert isinstance(entry["id"], str)
        assert entry["id"].startswith("frag-interrupt-")


def test_keyboard_interrupt_before_first_fragment_writes_no_progress(
    tmp_path: Path,
) -> None:
    """If the interrupt fires before any LLM call completes, the JSONL stays empty.

    Edge case: Ctrl-C on the very first fragment. The progress directory
    is created up-front (so its creation is not gated on a successful
    classification), but the JSONL itself stays empty.
    """
    vault = tmp_path / "vault"
    _seed_fragment(vault, idx=0)

    def always_interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    with (
        patch.object(
            LLMClassifier,
            "classify_with_reasoning",
            new=always_interrupt,
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    progress_path = vault / "00-Creek-Meta" / "Processing-Log" / LLM_PROGRESS_FILENAME
    # Either the file does not exist (open-on-first-write) or it exists
    # but is empty. Both are valid "nothing succeeded" signals.
    if progress_path.is_file():
        assert progress_path.read_text(encoding="utf-8") == ""
