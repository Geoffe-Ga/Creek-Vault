"""End-to-end redaction pipeline test (TEST-001 / BUG-004).

Drops a source file containing a deliberately well-known test secret,
runs the full pipeline, and asserts the secret does not appear in any
file written under the vault. Pinned to the fail-loud behaviour
documented in ``docs/redaction.md``: the pipeline must refuse to ingest
when unresolved redaction matches exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline, RedactionRequiredError

pytestmark = [pytest.mark.e2e]


_SECRET = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret  AWS test example
"""Documented AWS test access key — never a real credential."""


def test_pipeline_does_not_leak_secret_into_vault(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """A well-known fake AWS key must never reach any vault file.

    With the fail-loud redaction gate, the pipeline aborts before
    ingestion when it sees the secret. Either way the invariant holds:
    no file under the vault contains the literal AKIA* token.
    """
    note = synthetic_source / "leaky.md"
    note.write_text(
        f"# Confidential\n\nDo not commit: {_SECRET}\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)
    with pytest.raises(RedactionRequiredError):
        pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)

    for path in synthetic_vault.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert _SECRET not in text, (
            f"AWS test key leaked into vault file {path.relative_to(synthetic_vault)} "
            "(BUG-004 sentinel)"
        )
