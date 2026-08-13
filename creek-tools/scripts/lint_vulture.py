#!/usr/bin/env python3
"""Dead-code gate: vulture under per-type confidence floors (issue #1395).

Vulture scores every finding with a confidence percentage, and that score
says how likely the symbol is to be *dead* -- not how much we care. The
repo's previous invocations passed ``--min-confidence 80`` uniformly,
which switched the entire dead-symbol tier off in silence: vulture scores
an unused function, method, class, property or attribute at 60%, so
nothing in that tier could ever be reported. The gate was green because
it was blind. Measured on this tree, a brand-new zero-caller function
added to ``creek/`` produced zero findings at 80. ``LEGACY_MIN_CONFIDENCE``
keeps that number on the record so a test can pin the defect.

Dropping straight to ``--min-confidence 60`` is the opposite failure: 287
findings, dominated by the tiers where vulture is honestly guessing. That
gate does not ship either, because a gate nobody can keep at zero gets
turned off within a week. This module is the policy in between -- a floor
*per finding type*, plus a small set of categorical carve-outs -- chosen
so the gate is both capable of failing and quiet enough to live with.

Why the floors sit where they do
--------------------------------
* ``function`` / ``method`` / ``class`` / ``property`` / ``attribute``
  -> 60. That is vulture's own score for the whole dead-symbol tier, and
  it is the tier worth gating: a definition nothing references is either
  dead or reached by one of the mechanisms carved out below. Any floor
  above 60 turns this gate back off.
* ``variable`` / ``import`` -> 90. Under 90 these are dominated by false
  positives: loop targets, tuple unpacking, deliberate ``__init__``
  re-exports, imports kept for their side effects. Vulture scores a
  plainly unreferenced import or variable at 90, so the floor keeps the
  real ones and leaves the guesses out.
* ``unreachable_code`` -> 90. Vulture emits this one at 100 today -- it is
  proven from control flow, not inferred -- so 90 and 100 accept exactly
  the same findings. 90 is chosen anyway because a floor pinned to the one
  score a tool currently produces fails in the direction this whole issue
  is about: if a later vulture scored it 95, a floor of 100 would stop
  reporting the category without saying so. Floors are minimums.

Why the carve-outs are categorical, and why there is no allowlist
-----------------------------------------------------------------
There is deliberately **no allowlist, whitelist or ignore-names parameter
anywhere in this module's API**. A per-symbol allowlist is how a
dead-code gate dies: every finding gets answered with a name added to a
file instead of a deletion, and a release later the allowlist is the only
thing that grows. This gate reaches zero by deleting dead code.

The carve-outs that do exist are *categories*, which is why they are
constants rather than a list of symbols. In each one, something other
than a Python call site is the caller, so "nothing references it" is the
wrong question to be asking:

* ``IGNORE_DECORATORS`` -- Typer commands and MCP tools are invoked
  through their framework's registry, pydantic validators by pydantic,
  and ``@overload`` stubs are erased at runtime by definition.
* ``IMPLICITLY_INVOKED_NAMES`` and dunders -- the language is the caller.
  ``__enter__`` and friends are never named at a call site, and
  ``_missing_`` is called by ``enum`` on a failed lookup.
* ``REFERENCE_ONLY_PATHS`` -- ``tests/`` is scanned so a symbol used only
  by its tests still counts as referenced, but a finding located *inside*
  the tests is not this gate's business.

What this gate cannot see
-------------------------
Five blindnesses, named so a green run is not read as more than it is.
The first four have tracking issues; keep this list true as they close.

1. (#1469) Code kept alive only by its own tests is invisible, because
   ``tests/`` is scanned as a reference source. A module with thorough
   tests and no production caller passes clean. Measured: a
   production-only pass over the same tree reports 67 findings against
   this gate's 0, so roughly 60 symbols have no production caller.
2. (#1470) An orphaned ``@app.command`` or ``@server.tool`` is invisible,
   because registration is the caller and the decorator is carved out. A
   retired CLI command still wired into the app looks alive.
3. (#1471) Unused ``variable`` and ``attribute`` findings below the 90
   floor go unreported, so dead locals and dead ``self.x`` assignments
   inside that uncertain band pass.
4. (#1472) ``crawdad/`` is out of scope entirely -- separate gate,
   separate venv, and this module never scans it.
5. Vulture resolves used names **globally, by bare identifier**, with no
   notion of scope: one reference to the name ``url`` anywhere in the scan
   keeps every ``url`` definition alive. Distinctive names are unaffected,
   but a dead symbol with a common name can be masked by an unrelated
   symbol that merely shares it. Measured instance: the pydantic validator
   ``cls`` parameters report as dead in ``crawdad/`` and not here, purely
   because some unrelated ``@classmethod`` in ``creek/`` uses ``cls`` in
   its body. This is inherent to vulture, not to this policy.

Exit codes: 0 when clean, 3 when any finding survives the policy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from vulture import Vulture

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Scanned together, in one vulture pass: a reference in any of them keeps
# a definition in the others alive.
DEFAULT_SCAN_PATHS: Final[tuple[str, ...]] = ("creek", "creek_mcp", "tests")

# Scanned for their references only; findings located here are dropped.
REFERENCE_ONLY_PATHS: Final[tuple[str, ...]] = ("tests",)

# The uniform threshold this module replaces. Kept as a named constant so
# a test can pin the defect: at 80 the dead-symbol tier is unreachable.
LEGACY_MIN_CONFIDENCE: Final[int] = 80

# Per-type minimum confidence. See the module docstring for why each
# number is what it is; changing one changes what the gate can see.
CONFIDENCE_FLOORS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "variable": 90,
        "import": 90,
        # Vulture only ever emits unreachable_code at 100, so 90 and 100
        # accept exactly the same findings today. 90 is deliberate: a
        # floor pinned to the single score a tool currently produces is
        # brittle in the one direction this issue exists to prevent --
        # if a later vulture scored it 95, a floor of 100 would silently
        # stop reporting the category and the gate would go on saying
        # zero. Floors are minimums, not equality checks.
        "unreachable_code": 90,
        "function": 60,
        "method": 60,
        "class": 60,
        "property": 60,
        "attribute": 60,
    }
)

# Registration is the caller for everything decorated with these, so
# vulture's "no reference" verdict does not apply. The bare and the
# ``@*.`` forms are both listed because the decorator may be reached
# through a module alias (``@typer_app.command`` vs ``@app.command``).
IGNORE_DECORATORS: Final[tuple[str, ...]] = (
    "@app.command",
    "@*.command",
    "@*.callback",
    "@server.tool",
    "@*.tool",
    "@field_validator",
    "@model_validator",
    "@*.field_validator",
    "@*.model_validator",
    "@overload",
    "@typing.overload",
)

# Non-dunder names the language itself invokes. ``_missing_`` is called by
# ``enum`` when a value lookup fails; dunders are handled by shape.
IMPLICITLY_INVOKED_NAMES: Final[frozenset[str]] = frozenset({"_missing_"})

# ``creek-tools/`` -- this file lives in ``creek-tools/scripts/``. Default
# scans resolve against it so the gate reads the same from any cwd.
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Ask vulture for everything it found. The floors are policy and belong
# here, where they can be explained and tested, not in a CLI flag.
_EVERY_CONFIDENCE: Final[int] = 0

# Distinct from 1 (crash) and 2 (bad usage) so a caller can tell "the gate
# ran and found dead code" from "the gate could not run".
_EXIT_FINDINGS_FOUND: Final[int] = 3

_DUNDER_AFFIX: Final[str] = "__"


class UnknownFindingTypeError(RuntimeError):
    """Raised when vulture reports an item type absent from CONFIDENCE_FLOORS."""


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One dead-code finding that survived the policy.

    Ordered by field declaration order, so sorting a collection of these
    groups them by file and then by line -- the order a reader wants.

    Attributes:
        path: Absolute path of the file the symbol is defined in.
        lineno: 1-based line number of the definition.
        typ: Vulture's category, always a key of CONFIDENCE_FLOORS.
        name: The unreferenced symbol's name.
        confidence: Vulture's confidence percentage, 0-100.
    """

    path: Path
    lineno: int
    typ: str
    name: str
    confidence: int

    def __str__(self) -> str:
        """Render the finding in vulture's own one-line report format.

        Returns:
            A ``path:line: unused <type> '<name>' (<n>%)`` line.
        """
        return (
            f"{self.path}:{self.lineno}: "
            f"unused {self.typ} '{self.name}' ({self.confidence}%)"
        )


def _findings_from_items(items: Sequence[Any]) -> list[Finding]:
    """Coerce vulture's untyped items into typed findings.

    This is the module's only untyped boundary. Vulture ships no
    ``py.typed`` marker, so ``Item`` is ``Any`` to mypy; every field is
    therefore coerced explicitly here and nowhere else, which keeps the
    ``Any`` from leaking into the rest of the module.

    Args:
        items: ``vulture.core.Item`` objects, as returned by
            ``Vulture.get_unused_code``.

    Returns:
        One Finding per item, in the order given.
    """
    return [
        Finding(
            path=Path(str(item.filename)).resolve(),
            lineno=int(item.first_lineno),
            typ=str(item.typ),
            name=str(item.name),
            confidence=int(item.confidence),
        )
        for item in items
    ]


def _anchor(root: str) -> Path:
    """Resolve a reference-only root to one absolute directory.

    A relative root is anchored at the project root rather than at the
    cwd, so the gate reads the same however it is invoked. An absolute
    root (what the tests pass for synthetic trees) is used as given.

    Args:
        root: A directory name or path, relative or absolute.

    Returns:
        The resolved absolute path the root denotes.
    """
    candidate = Path(root)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / candidate
    return candidate.resolve()


def _is_under(path: Path, roots: Sequence[str]) -> bool:
    """Report whether `path` lies inside any of `roots`.

    Containment is tested against one resolved absolute directory per
    root, NOT by looking for the root's name among the path's components.
    The difference is a fail-open: segment matching would treat every
    finding in a checkout that merely lives inside some directory called
    ``tests`` (``~/tests/creek-tools/...``) as reference-only, and the
    gate would report zero forever without saying why. A gate that goes
    silently blind depending on where it was cloned is the same defect
    class as the ``--min-confidence 80`` this module replaced.

    Args:
        path: Absolute path of a finding.
        roots: Directory names or paths, relative or absolute.

    Returns:
        True when the path is inside one of the roots.
    """
    return any(path.is_relative_to(_anchor(root)) for root in roots)


def _is_implicitly_invoked(name: str) -> bool:
    """Report whether something other than a call site invokes `name`.

    Args:
        name: The symbol name vulture found no reference to.

    Returns:
        True for dunders and for the names in IMPLICITLY_INVOKED_NAMES.
    """
    is_dunder = name.startswith(_DUNDER_AFFIX) and name.endswith(_DUNDER_AFFIX)
    return is_dunder or name in IMPLICITLY_INVOKED_NAMES


def _floor_for(typ: str) -> int:
    """Return the minimum confidence at which `typ` is reported.

    Args:
        typ: A vulture item type.

    Returns:
        The floor from CONFIDENCE_FLOORS.

    Raises:
        UnknownFindingTypeError: If the type has no floor. A vulture
            upgrade that adds a category must fail loudly here rather
            than pass silently through a default.
    """
    try:
        return CONFIDENCE_FLOORS[typ]
    except KeyError as exc:
        msg = (
            f"vulture reported item type {typ!r}, which has no floor in "
            "CONFIDENCE_FLOORS; add one (with its reason) before this "
            "category can be gated"
        )
        raise UnknownFindingTypeError(msg) from exc


def _survives(finding: Finding, reference_only: Sequence[str]) -> bool:
    """Report whether `finding` is the gate's business.

    Args:
        finding: A coerced vulture finding.
        reference_only: Roots scanned only so their references count.

    Returns:
        True when the finding should be reported.

    Raises:
        UnknownFindingTypeError: If the finding's type has no floor.
    """
    # Scanned for the references it makes, not for the code it holds.
    if _is_under(finding.path, reference_only):
        return False
    # The language calls it; the absence of a call site proves nothing.
    if _is_implicitly_invoked(finding.name):
        return False
    # Below the floor vulture is guessing -- see the module docstring.
    return finding.confidence >= _floor_for(finding.typ)


def _resolve_scan_paths(paths: Sequence[Path | str] | None) -> list[Path]:
    """Return the paths to scan, defaulting to the project's own tree.

    Args:
        paths: Explicit scan paths, or None for DEFAULT_SCAN_PATHS.

    Returns:
        The default paths anchored at the project root (so the default
        works from any cwd), or the caller's paths exactly as given.
    """
    if paths is None:
        return [_PROJECT_ROOT / name for name in DEFAULT_SCAN_PATHS]
    return [Path(path) for path in paths]


def find_dead_code(
    paths: Sequence[Path | str] | None = None,
    *,
    reference_only: Sequence[str] = REFERENCE_ONLY_PATHS,
) -> list[Finding]:
    """Return the policy-filtered dead-code findings, sorted by (path, lineno).

    All paths are scanned in a single vulture pass, because vulture's
    used-name set is global: scanning them separately would report every
    cross-module reference as dead.

    Args:
        paths: Files or directories to scan. None means
            DEFAULT_SCAN_PATHS, resolved against the project root.
        reference_only: Roots whose files are scanned for the references
            they make, but whose own findings are dropped.

    Returns:
        The surviving findings, ordered by file then line.

    Raises:
        UnknownFindingTypeError: If vulture reports a category with no
            floor in CONFIDENCE_FLOORS.
    """
    scanner = Vulture(verbose=False, ignore_decorators=list(IGNORE_DECORATORS))
    scanner.scavenge([str(path) for path in _resolve_scan_paths(paths)])
    items = scanner.get_unused_code(min_confidence=_EVERY_CONFIDENCE)
    hits = _findings_from_items(items)
    return sorted(hit for hit in hits if _survives(hit, reference_only))


def main(argv: Sequence[str] | None = None) -> int:
    """Print findings and return 0 when clean, 3 when any finding survives.

    Args:
        argv: Scan paths, defaulting to the process's own arguments. An
            empty sequence scans DEFAULT_SCAN_PATHS.

    Returns:
        0 when nothing survives the policy, 3 when something does.
    """
    requested = list(sys.argv[1:] if argv is None else argv)
    findings = find_dead_code(requested or None)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"vulture: {len(findings)} dead-code finding(s); "
            "delete the code, do not allowlist it",
            file=sys.stderr,
        )
        return _EXIT_FINDINGS_FOUND
    print("vulture: no dead code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
