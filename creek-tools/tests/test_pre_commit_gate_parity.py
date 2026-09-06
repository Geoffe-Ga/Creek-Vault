"""The staged pre-commit gate must agree with the lock-backed quality gate.

Issues #1759 and #1764: several hooks answered materially different questions from
``scripts/check-all.sh``.  The isolated MyPy hook lacked project dependencies;
three advisory scanners widened themselves to paths their canonical gates do
not yet cover; Bandit rejected low-severity findings the canonical gate permits;
and ``check-docstring-first`` rejected the PEP 258 attribute docstrings used
throughout the repository.  A green canonical gate could therefore be followed
by a pre-commit failure with no defect in the diff.

These configuration tests keep pre-commit on the provisioned toolchain, exempt
generated checksum manifests from entropy scanning, and pin the deliberately
narrow scanners to today's documented scope. Issue #965 owns widening that
scope after its existing findings are resolved.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.shell_command_support import PRE_COMMIT_CONFIG

PYPROJECT = PRE_COMMIT_CONFIG.parent / "pyproject.toml"

_CANONICAL_SCANNERS = {
    "refurb": "creek-tools/scripts/lint-refurb.sh",
    "tryceratops": "creek-tools/scripts/lint-tryceratops.sh",
    "interrogate": "creek-tools/scripts/lint-interrogate.sh",
}


def _hooks() -> dict[str, tuple[str, dict[str, Any]]]:
    """Return every configured hook keyed by its unique identifier."""
    config: dict[str, Any] = yaml.safe_load(
        PRE_COMMIT_CONFIG.read_text(encoding="utf-8")
    )
    hooks: dict[str, tuple[str, dict[str, Any]]] = {}
    for repo in config["repos"]:
        for raw_hook in repo.get("hooks", []):
            hook = dict(raw_hook)
            hook_id = str(hook["id"])
            assert hook_id not in hooks, f"duplicate pre-commit hook id {hook_id!r}"
            hooks[hook_id] = str(repo["repo"]), hook
    return hooks


def test_mypy_pre_commit_reuses_the_lockfile_backed_gate() -> None:
    """MyPy must run with project dependencies and canonical strict options."""
    repo, hook = _hooks()["mypy"]

    assert repo == "local"
    assert hook["entry"] == "creek-tools/scripts/typecheck.sh"
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["types"] == ["python"]
    scope = re.compile(str(hook["files"]))
    for path in (
        "creek-tools/creek/example.py",
        "creek-tools/creek_mcp/example.py",
        "creek-tools/tests/test_example.py",
        "creek-tools/scripts/example.py",
    ):
        assert scope.search(path), f"the MyPy hook does not trigger for {path}"


def test_bandit_pre_commit_matches_the_canonical_severity_and_config() -> None:
    """Bandit must ask the same medium-or-higher question in every local gate."""
    repo, hook = _hooks()["bandit"]
    args = [str(arg) for arg in hook["args"]]

    assert repo == "https://github.com/PyCQA/bandit"
    assert args.count("-ll") == 1
    assert args[args.index("-c") + 1] == "creek-tools/pyproject.toml"


def test_attribute_docstrings_are_not_misread_as_module_docstrings() -> None:
    """The incompatible check-docstring-first hook must not return."""
    assert "check-docstring-first" not in _hooks()


def test_generated_contract_hash_manifest_is_not_scanned_as_secrets() -> None:
    """Checksums are exempt, while contract payloads remain secret-scanned."""
    _repo, hook = _hooks()["detect-secrets"]
    excluded = re.compile(str(hook["exclude"]))

    assert excluded.search("creek-tools/.secrets.baseline")
    assert excluded.search("docs/contracts/adepthood-v1/manifest.json")
    assert not excluded.search(
        "docs/contracts/adepthood-v1/schemas/VoiceDraftReadResponse.schema.json"
    )
    assert not excluded.search(
        "docs/contracts/adepthood-v1/examples/voice-drafts/success.json"
    )


def test_lock_backed_hooks_are_not_described_as_independent_installs() -> None:
    """Dependency comments must agree that local hooks reuse the project env."""
    pyproject = PYPROJECT.read_text(encoding="utf-8")

    for stale_claim in (
        ".pre-commit-config.yaml's mirrors-mypy rev",
        ".pre-commit-config.yaml's tryceratops rev",
    ):
        assert stale_claim not in pyproject


@pytest.mark.parametrize(
    ("hook_id", "entry"),
    _CANONICAL_SCANNERS.items(),
)
def test_narrow_scanners_reuse_their_canonical_scope(hook_id: str, entry: str) -> None:
    """Advisory hooks stay on ``creek/`` until #965 deliberately widens them."""
    repo, hook = _hooks()[hook_id]

    assert repo == "local"
    assert hook["entry"] == entry
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["types"] == ["python"]
    scope = re.compile(str(hook["files"]))
    assert scope.search("creek-tools/creek/example.py")
    assert not scope.search("creek-tools/creek_mcp/example.py")
    assert not scope.search("creek-tools/tests/test_example.py")
