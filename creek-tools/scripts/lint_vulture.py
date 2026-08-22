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

One policy, two subprojects
---------------------------
The monorepo ships two Python trees with two virtualenvs, two lockfiles
and two CI jobs. They share exactly one dead-code policy, and they share
it by *executing the same file*: :data:`CREEK_TOOLS` and :data:`CRAWDAD`
are two :class:`Scope` instances declared side by side below, and
``crawdad/scripts/lint-vulture.sh`` runs this module rather than a copy
of it (issue #1472). A copy is how four call sites end up disagreeing
about a threshold, which is the drift #1395's single-wrapper design
exists to prevent; declaring both scopes here means a floor change or a
new carve-out cannot land in one subproject and not the other.

Scanning ``crawdad/`` needs no crawdad import: vulture is pure
``ast.parse`` and never executes what it reads, so the only thing the two
environments must each provide is a ``vulture`` distribution.

Every path in a :class:`Scope` is resolved against that scope's own
``root``. The alternative -- relative names anchored at one module-level
project root -- fails **open** rather than loud: a caller asking for
``reference_only=("crawdad/tests",)`` from crawdad's own tree would have
had it resolved to ``creek-tools/crawdad/tests``, a directory that does
not exist, and every finding inside the real test tree would have been
reported instead of dropped. Measured at the time: 12 findings under the
relative form against 11 under the absolute one. :func:`find_dead_code`
therefore refuses a relative reference-only root outright.

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
constants and shapes rather than lists of symbols. In each one, something
other than a Python call site is the caller, so "nothing references it"
is the wrong question to be asking:

* ``IGNORE_DECORATORS`` -- Typer commands and MCP tools are invoked
  through their framework's registry, pydantic validators by pydantic,
  and ``@overload`` stubs are erased at runtime by definition.
* ``IMPLICITLY_INVOKED_NAMES`` and dunders -- the language is the caller.
  ``__enter__`` and friends are never named at a call site, and
  ``_missing_`` is called by ``enum`` on a failed lookup.
* ``IMPLICITLY_BOUND_PARAMETERS`` -- ``cls`` and ``self`` are bound by the
  interpreter at call time whether the body reads them or not. Vulture
  reports an unread one as an unused *variable* at 100%, far above the
  90 floor, and ``ignore_decorators`` cannot reach it: that option
  suppresses the finding on the decorated *function*, never on its
  parameters. Every one of crawdad's eight pydantic ``@field_validator``
  / ``@model_validator`` classmethods reported this way. These names are
  not suppressed anywhere else: a ``cls`` parameter is not something a
  reviewer can be asked to justify.
* ``if TYPE_CHECKING:`` imports -- vulture parses the AST and never
  evaluates a string annotation, so a symbol imported for typing and used
  only inside ``cast("list[MessageParam]", ...)`` or a quoted annotation
  looks unreferenced. Deleting it breaks ``mypy --strict``, i.e. two
  gates in the same ``check-all.sh`` would contradict each other. Nothing
  is ceded by dropping the category: ruff's ``F401`` is selected in both
  subprojects, it *does* understand string annotations, and it owns
  unused imports.
* ``Scope.reference_only`` -- a subproject's ``tests/`` tree is scanned so
  a symbol used only by its tests still counts as referenced, but a
  finding located *inside* the tests is not this gate's business.

What this gate cannot see
-------------------------
Four blindnesses, named so a green run is not read as more than it is.
The first three have tracking issues; keep this list true as they close.

1. (#1469) Code kept alive only by its own tests is invisible, because
   each scope's ``tests/`` tree is scanned as a reference source. A module
   with thorough tests and no production caller passes clean. Measured on
   ``creek-tools`` at the time of writing: a production-only pass over
   ``creek/`` + ``creek_mcp/`` reports **61** findings against this gate's
   0 (39 methods, 11 functions, 5 properties, 3 classes, 2 attributes, 1
   variable), so roughly that many symbols have no production caller.
2. (#1470) An orphaned ``@app.command`` or ``@server.tool`` is invisible
   *to this module*, because registration is the caller and the decorator
   is carved out. That hole is now covered from the other side rather
   than here: ``tests/test_wiring_contract.py`` walks the live Typer and
   MCP registries and fails when a registered surface is named by no file
   under its ``DOC_ROOTS``. What remains invisible is narrower -- a
   surface that is still documented and still registered, but that
   nothing downstream calls.
3. (#1471) Unused ``variable`` and ``import`` findings below the 90 floor
   go unreported, so a dead local inside that uncertain band passes.
   (``attribute`` is *not* in this band -- its floor is 60.) Measured:
   dropping both floors to 60 adds **41** findings on this tree, every
   one of them a ``variable`` at exactly 60% and none an ``import``. The
   population is dominated by class-body declarations a runtime reads by
   name rather than by call -- ``StrEnum`` members, pydantic ``Field(...)``
   declarations, dataclass field annotations -- so the fix is a
   shape-based carve-out for those, not a bare floor change.
4. Vulture resolves used names **globally, by bare identifier**, with no
   notion of scope: one reference to the name ``url`` anywhere in the scan
   keeps every ``url`` definition alive. Distinctive names are unaffected,
   but a dead symbol with a common name can be masked by an unrelated
   symbol that merely shares it. This is inherent to vulture, not to this
   policy -- and it is what hid the ``cls`` and ``TYPE_CHECKING`` carve-out
   gaps above until ``crawdad`` was brought into scope, since ``creek/``
   happens to contain unrelated uses of both names.

Exit codes: 0 when clean, 2 on a usage error, 3 when any finding
survives the policy.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from vulture import Vulture

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

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

# Bound by the interpreter at call time, not by an assignment anyone
# chose. ``ignore_decorators`` cannot reach these -- it suppresses the
# finding on the decorated function, never on its parameters -- so a
# pydantic validator that does not read ``cls`` reports as an unused
# variable at 100%. See the carve-out section of the module docstring.
IMPLICITLY_BOUND_PARAMETERS: Final[frozenset[str]] = frozenset({"cls", "self"})

# The guard whose body holds imports that exist only for annotations.
_TYPE_CHECKING_NAME: Final[str] = "TYPE_CHECKING"

# ``creek-tools/`` -- this file lives in ``creek-tools/scripts/``.
_CREEK_TOOLS_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# The monorepo root, which is what makes a sibling subproject addressable.
_REPO_ROOT: Final[Path] = _CREEK_TOOLS_ROOT.parent

# Ask vulture for everything it found. The floors are policy and belong
# here, where they can be explained and tested, not in a CLI flag.
_EVERY_CONFIDENCE: Final[int] = 0

# Distinct from 1 (crash) and 2 (bad usage) so a caller can tell "the gate
# ran and found dead code" from "the gate could not run".
_EXIT_FINDINGS_FOUND: Final[int] = 3

# A command line this module cannot act on -- notably a positional path,
# which a gate call site must never be able to pass.
_EXIT_USAGE: Final[int] = 2

_DUNDER_AFFIX: Final[str] = "__"

_SCOPE_FLAG: Final[str] = "--scope"

_IMPORT_TYPE: Final[str] = "import"

_VARIABLE_TYPE: Final[str] = "variable"


class UnknownFindingTypeError(RuntimeError):
    """Raised when vulture reports an item type absent from CONFIDENCE_FLOORS."""


class RelativeReferenceRootError(ValueError):
    """Raised when a reference-only root is not an absolute directory."""


class UsageError(ValueError):
    """Raised for a command line the gate cannot act on."""


@dataclass(frozen=True, slots=True)
class Scope:
    """One subproject's dead-code scan surface.

    Every path is relative to :attr:`root` and joined here, never anchored
    at some module-level default. That is what stops a scope declared for
    one subproject from silently resolving inside another -- the fail-open
    described in the module docstring.

    Attributes:
        name: The ``--scope`` value that selects this scope.
        root: Absolute directory the names below are resolved against.
        scan: Directory names scanned together in one vulture pass.
        reference_only: Names scanned for the references they make, whose
            own findings are dropped.
    """

    name: str
    root: Path
    scan: tuple[str, ...]
    reference_only: tuple[str, ...]

    @property
    def scan_paths(self) -> tuple[Path, ...]:
        """Return the absolute directories to scan.

        Returns:
            One resolved path per name in :attr:`scan`.
        """
        return tuple((self.root / name).resolve() for name in self.scan)

    @property
    def reference_only_paths(self) -> tuple[Path, ...]:
        """Return the absolute directories scanned for references only.

        Returns:
            One resolved path per name in :attr:`reference_only`.
        """
        return tuple((self.root / name).resolve() for name in self.reference_only)


CREEK_TOOLS: Final[Scope] = Scope(
    name="creek-tools",
    root=_CREEK_TOOLS_ROOT,
    scan=("creek", "creek_mcp", "tests"),
    reference_only=("tests",),
)

CRAWDAD: Final[Scope] = Scope(
    name="crawdad",
    root=_REPO_ROOT / "crawdad",
    scan=("crawdad", "tests"),
    reference_only=("tests",),
)

# Every scope the gate can be pointed at, keyed by its ``--scope`` value.
SCOPES: Final[Mapping[str, Scope]] = MappingProxyType(
    {scope.name: scope for scope in (CREEK_TOOLS, CRAWDAD)}
)


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


def _validated_roots(reference_only: Sequence[Path]) -> tuple[Path, ...]:
    """Resolve reference-only roots, refusing a relative one.

    A relative root has no single correct anchor once more than one
    subproject shares this policy, and guessing one fails *open*: the
    guess resolves to a directory that does not exist, nothing matches it,
    and every finding inside the real tree is reported as though the root
    had never been declared. Refusing is the only reading that cannot go
    quietly wrong.

    Args:
        reference_only: Roots whose findings are to be dropped.

    Returns:
        The same roots, resolved.

    Raises:
        RelativeReferenceRootError: If any root is relative.
    """
    for root in reference_only:
        if not root.is_absolute():
            msg = (
                f"reference-only root {str(root)!r} is relative. It would be "
                "anchored at a guess, match nothing, and silently report the "
                "findings it was meant to drop. Pass Scope.reference_only_paths "
                "or another absolute path."
            )
            raise RelativeReferenceRootError(msg)
    return tuple(root.resolve() for root in reference_only)


def _is_under(path: Path, roots: Sequence[Path]) -> bool:
    """Report whether `path` lies inside any of `roots`.

    Containment is tested against resolved absolute directories, NOT by
    looking for a root's name among the path's components. The difference
    is a fail-open: segment matching would treat every finding in a
    checkout that merely lives inside some directory called ``tests``
    (``~/tests/creek-tools/...``) as reference-only, and the gate would
    report zero forever without saying why. A gate that goes silently
    blind depending on where it was cloned is the same defect class as the
    ``--min-confidence 80`` this module replaced.

    Args:
        path: Absolute path of a finding.
        roots: Resolved absolute directories.

    Returns:
        True when the path is inside one of the roots.
    """
    return any(path.is_relative_to(root) for root in roots)


def _is_implicitly_invoked(name: str) -> bool:
    """Report whether something other than a call site invokes `name`.

    Args:
        name: The symbol name vulture found no reference to.

    Returns:
        True for dunders and for the names in IMPLICITLY_INVOKED_NAMES.
    """
    is_dunder = name.startswith(_DUNDER_AFFIX) and name.endswith(_DUNDER_AFFIX)
    return is_dunder or name in IMPLICITLY_INVOKED_NAMES


def _tests_type_checking(test: ast.expr) -> bool:
    """Report whether `test` is the ``TYPE_CHECKING`` guard.

    Both spellings count: the bare ``TYPE_CHECKING`` imported from
    ``typing``, and the qualified ``typing.TYPE_CHECKING``.

    Args:
        test: The condition expression of an ``if`` statement.

    Returns:
        True when the condition names ``TYPE_CHECKING``.
    """
    return any(
        (isinstance(node, ast.Name) and node.id == _TYPE_CHECKING_NAME)
        or (isinstance(node, ast.Attribute) and node.attr == _TYPE_CHECKING_NAME)
        for node in ast.walk(test)
    )


def _type_checking_lines(path: Path) -> frozenset[int]:
    """Return every line number inside an ``if TYPE_CHECKING:`` body.

    Only the ``if`` body is collected. An ``else`` branch is ordinary
    runtime code and its imports are ordinary imports.

    Args:
        path: A Python source file.

    Returns:
        The covered line numbers; empty when the file cannot be parsed,
        which leaves the finding reported rather than swallowed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return frozenset()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _tests_type_checking(node.test):
            for statement in node.body:
                end = statement.end_lineno or statement.lineno
                lines.update(range(statement.lineno, end + 1))
    return frozenset(lines)


def _drop_type_checking_imports(findings: Iterable[Finding]) -> list[Finding]:
    """Drop import findings bound inside an ``if TYPE_CHECKING:`` block.

    Vulture never evaluates a string annotation, so an import used only by
    ``cast("list[X]", ...)`` or a quoted annotation looks unreferenced;
    deleting it would break ``mypy --strict``. Ruff's ``F401`` owns this
    category and does understand those use sites.

    Args:
        findings: Every finding vulture produced, in any order.

    Returns:
        The findings with that one category removed.
    """
    covered: dict[Path, frozenset[int]] = {}
    kept: list[Finding] = []
    for finding in findings:
        if finding.typ != _IMPORT_TYPE:
            kept.append(finding)
            continue
        if finding.path not in covered:
            covered[finding.path] = _type_checking_lines(finding.path)
        if finding.lineno not in covered[finding.path]:
            kept.append(finding)
    return kept


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


def _survives(finding: Finding, reference_only: Sequence[Path]) -> bool:
    """Report whether `finding` is the gate's business.

    Args:
        finding: A coerced vulture finding.
        reference_only: Resolved roots scanned only for their references.

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
    # The interpreter binds it; nobody chose to write it down.
    if finding.typ == _VARIABLE_TYPE and finding.name in IMPLICITLY_BOUND_PARAMETERS:
        return False
    # Below the floor vulture is guessing -- see the module docstring.
    return finding.confidence >= _floor_for(finding.typ)


def find_dead_code(
    paths: Sequence[Path | str],
    *,
    reference_only: Sequence[Path] = (),
) -> list[Finding]:
    """Return the policy-filtered dead-code findings, sorted by (path, lineno).

    All paths are scanned in a single vulture pass, because vulture's
    used-name set is global: scanning them separately would report every
    cross-module reference as dead.

    This is the ad-hoc entry point -- triage passes (a production-only
    sweep for #1469) and the tests use it. Gates call :func:`scan_scope`,
    which cannot be narrowed.

    Args:
        paths: Files or directories to scan.
        reference_only: **Absolute** roots whose files are scanned for the
            references they make, but whose own findings are dropped.

    Returns:
        The surviving findings, ordered by file then line.

    Raises:
        RelativeReferenceRootError: If a reference-only root is relative.
        UnknownFindingTypeError: If vulture reports a category with no
            floor in CONFIDENCE_FLOORS.
    """
    roots = _validated_roots(reference_only)
    scanner = Vulture(verbose=False, ignore_decorators=list(IGNORE_DECORATORS))
    scanner.scavenge([str(path) for path in paths])
    items = scanner.get_unused_code(min_confidence=_EVERY_CONFIDENCE)
    hits = _drop_type_checking_imports(_findings_from_items(items))
    return sorted(hit for hit in hits if _survives(hit, roots))


def scan_scope(scope: Scope) -> list[Finding]:
    """Run the gate over one whole subproject.

    Args:
        scope: The subproject to scan.

    Returns:
        The surviving findings, ordered by file then line.
    """
    return find_dead_code(
        scope.scan_paths,
        reference_only=scope.reference_only_paths,
    )


def _scope_from_argv(argv: Sequence[str]) -> Scope:
    """Resolve the scope a command line selects.

    Args:
        argv: Arguments after the program name.

    Returns:
        The named scope, or :data:`CREEK_TOOLS` when none is named.

    Raises:
        UsageError: For anything else -- notably a positional path. The
            scan surface is policy, not an argument: a gate call site that
            could pass a path could narrow itself into a green run.
    """
    if not argv:
        return CREEK_TOOLS
    if len(argv) == 2 and argv[0] == _SCOPE_FLAG and argv[1] in SCOPES:
        return SCOPES[argv[1]]
    choices = "|".join(SCOPES)
    msg = (
        f"usage: lint_vulture [{_SCOPE_FLAG} {{{choices}}}]\n"
        "The scan surface is policy, not an argument: every gate call site "
        "scans one whole named scope, so none of them can narrow it into a "
        f"green run. Got {list(argv)!r}."
    )
    raise UsageError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    """Print findings and return 0 when clean, 3 when any finding survives.

    Args:
        argv: Command-line arguments, defaulting to the process's own.
            Empty selects :data:`CREEK_TOOLS`.

    Returns:
        0 when nothing survives the policy, 2 on a usage error, 3 when
        something does.
    """
    requested = list(sys.argv[1:] if argv is None else argv)
    try:
        scope = _scope_from_argv(requested)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return _EXIT_USAGE
    findings = scan_scope(scope)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"vulture: {len(findings)} dead-code finding(s) in {scope.name}; "
            "delete the code, do not allowlist it",
            file=sys.stderr,
        )
        return _EXIT_FINDINGS_FOUND
    print(f"vulture: no dead code in {scope.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
