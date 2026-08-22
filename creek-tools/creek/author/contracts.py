"""Loader for medium contracts (``<medium>.MEDIUM.md``) (FEAT-041 #459).

A medium contract is a small skill file — YAML frontmatter declaring the
structure, specialist weights, citation norm, default privacy tier, and
reflection rubric, plus a budgeted markdown body of human guidance. The
Conductor loads one to drive an authoring run. Contracts deploy to
``<vault>/00-Creek-Meta/Skills/mediums/`` from the canonical templates; the
loader reads the deployed copy when present and falls back to the template so
the desk runs on a freshly scaffolded vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.models import MediumContract
from creek.scaffold import SKILLS_TEMPLATE_DIR
from creek.vault.reader import load_post_or_raise

if TYPE_CHECKING:
    from pathlib import Path

#: Canonical medium-contract templates shipped with the package.
MEDIUMS_TEMPLATE_DIR = SKILLS_TEMPLATE_DIR / "mediums"

#: Deployed location of medium contracts within a vault.
_VAULT_MEDIUMS_SUBDIR = ("00-Creek-Meta", "Skills", "mediums")

#: Soft ceiling on a ``chat`` reply length, in *characters* (FEAT-041 §5).
#: Distinct from the contract's ``≤1500 tokens`` template budget (an upstream
#: generation budget): this is a post-generation gate on the reply that reaches
#: the caller — ~2000 chars ≈ 500 tokens at 4 chars/token.
CHAT_MAX_CHARS: int = 2000

#: Reflection-rubric dimensions every medium contract must weight (SPEC §8).
REQUIRED_RUBRIC_KEYS: frozenset[str] = frozenset(
    {"ontological_accuracy", "citation_completeness", "voice_fidelity"}
)

#: Tolerance for a weight map summing to ~1.0.
_WEIGHT_SUM_TOLERANCE: float = 0.01


class ContractConformanceError(ValueError):
    """Raised when a :class:`~creek.models.MediumContract` is malformed."""


def _check_weight_map(name: str, weights: dict[str, float]) -> list[str]:
    """Return conformance problems for a weight map (range + sum sanity).

    Args:
        name: The field name, for error messages.
        weights: The weight map to validate.

    Returns:
        A list of human-readable problems (empty when conformant).
    """
    if not weights:
        return [f"{name} is empty"]
    problems: list[str] = []
    if any(not 0.0 <= weight <= 1.0 for weight in weights.values()):
        problems.append(f"{name} has values outside [0, 1]")
    total = sum(weights.values())
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        problems.append(f"{name} weights sum to {total:.3f}, expected ~1.0")
    return problems


def assert_contract_conformant(contract: MediumContract) -> None:
    """Validate that *contract* is well-formed, raising if not.

    Checks (SPEC §5/§8): a non-empty structure, specialist weights and a
    reflection rubric that each sum to ~1.0 with values in ``[0, 1]``, and the
    presence of every :data:`REQUIRED_RUBRIC_KEYS` dimension. Reusable by every
    medium contract, current and future.

    Args:
        contract: The contract to validate.

    Raises:
        ContractConformanceError: When the contract violates any invariant.
    """
    problems: list[str] = []
    if not contract.structure:
        problems.append("structure is empty")
    problems.extend(
        _check_weight_map("specialist_weights", contract.specialist_weights)
    )
    problems.extend(_check_weight_map("reflection_rubric", contract.reflection_rubric))
    missing = REQUIRED_RUBRIC_KEYS - set(contract.reflection_rubric)
    if missing:
        problems.append(f"reflection_rubric missing keys: {sorted(missing)}")
    if problems:
        joined = "; ".join(problems)
        msg = f"Contract {contract.medium!r} is non-conformant: {joined}"
        raise ContractConformanceError(msg)


def _resolve_contract_path(medium: str, vault: Path) -> Path | None:
    """Return the contract path for *medium*, preferring the vault copy.

    Args:
        medium: The medium slug.
        vault: The vault root.

    Returns:
        The deployed contract path, the canonical template path, or ``None``
        when neither exists.
    """
    deployed = vault.joinpath(*_VAULT_MEDIUMS_SUBDIR, f"{medium}.MEDIUM.md")
    if deployed.is_file():
        return deployed
    template = MEDIUMS_TEMPLATE_DIR / f"{medium}.MEDIUM.md"
    if template.is_file():
        return template
    return None


def load_medium_contract(medium: str, vault: Path) -> MediumContract:
    """Load and validate the contract for *medium*.

    Args:
        medium: The medium slug (e.g. ``research``).
        vault: The vault to look in (falls back to the canonical template).

    Returns:
        The parsed :class:`~creek.models.MediumContract`.

    Raises:
        FileNotFoundError: When no contract exists for *medium*.
    """
    path = _resolve_contract_path(medium, vault)
    if path is None:
        msg = f"No medium contract found for {medium!r}."
        raise FileNotFoundError(msg)
    post = load_post_or_raise(path)
    data: dict[str, object] = post.metadata.copy()
    data.setdefault("medium", medium)
    return MediumContract.model_validate(data)
