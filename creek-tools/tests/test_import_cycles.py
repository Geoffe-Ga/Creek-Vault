"""Regression tests guarding against package-level circular imports.

The Creek package has several deep import chains that cross subsystem
boundaries (``ingest`` → ``models`` → ``classify`` → ``generate`` →
``clean`` → back to ``ingest``). A circular import in that chain only
manifests when a specific module is imported *first* in a fresh
interpreter — once any earlier import populates ``sys.modules``, the
partially-initialised module is already cached and the cycle is
masked. The in-process pytest session imports dozens of modules during
collection, so it cannot reliably catch these.

Each test here therefore spawns a fresh subprocess that imports a
single entry-point module and nothing else, reproducing the
clean-interpreter import order an operator hits when they run, e.g.,
``python -c "import creek.consent"`` or a CLI entry point.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Entry-point modules that each sit at the head of a cross-subsystem
# import chain. Importing any of these first in a clean interpreter
# must not raise — a regression here means a new circular import was
# introduced somewhere along the chain.
_ENTRY_POINT_MODULES = [
    "creek.consent",
    "creek.clean.dedup",
    "creek.ingest.base",
    "creek.generate.state",
    "creek.classify.llm.prompts",
]


@pytest.mark.parametrize("module_name", _ENTRY_POINT_MODULES)
def test_module_imports_cleanly_in_fresh_interpreter(module_name: str) -> None:
    """Importing *module_name* first in a clean interpreter must not raise.

    Runs ``python -c "import <module_name>"`` in a subprocess so the
    import order matches a cold start rather than the warmed-up
    ``sys.modules`` of the pytest session. A non-zero exit code
    (typically an ``ImportError`` from a partially-initialised module)
    means a circular import regressed.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"importing {module_name!r} in a fresh interpreter failed "
        f"(circular import?):\n{result.stderr}"
    )
