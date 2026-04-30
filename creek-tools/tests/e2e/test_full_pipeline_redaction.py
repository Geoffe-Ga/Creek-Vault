"""End-to-end redaction pipeline test (TEST-001 / BUG-004).

Drops a source file containing a deliberately well-known test secret,
runs the full pipeline, and asserts the secret does not appear in any
file written under the vault. Catches the BUG-004 regression where the
pipeline scans for redactions but never applies them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.xfail(
        reason=(
            "BUG-004: pipeline scans for redactions but does not apply "
            "them. strict=True so when the redactor is wired into the "
            "ingestion path the test XPASSES, fails CI, and forces "
            "removal of this xfail marker."
        ),
        strict=True,
    ),
]


_SECRET = "AKIAIOSFODNN7EXAMPLE"
"""Documented AWS test access key — never a real credential."""


def test_pipeline_does_not_leak_secret_into_vault(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """A well-known fake AWS key must not survive to any vault file.

    Reads every file in the resulting vault and asserts the secret token
    is absent. This is a hard "no leaks" invariant: even if redaction
    only rewrites *some* files, none of them should contain the literal
    AKIA* token.
    """
    note = synthetic_source / "leaky.md"
    note.write_text(
        f"# Confidential\n\nDo not commit: {_SECRET}\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)
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
