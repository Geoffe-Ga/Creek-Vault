"""Deterministic check: name what the compile gate silently refuses over (#1277).

:meth:`creek.classify.privacy_filter.AncestorIndex.chain_tiers` fails **closed**
to ``INTIMATE`` on four distinct vault anomalies, and
``creek_mcp.tools.compile`` reports every one of them through the deliberately
content-free ``_ABOVE_CEILING_REASON``. The refusal is correct and the silence
is correct — the reason string must not leak *which* ancestor offended — but it
leaves an operator holding a vault the gate will not compile and nowhere to
look. This check is the other half of that contract: the same triggers,
surfaced by name, in a report the operator asked for.

**An exact inverse of the gate, or it is worse than nothing.** Any trigger
lint cannot see is a vault that refuses undiagnosably, which is the precise
failure #1277 exists to end. Hence four rules, not the three the issue lists:

===================================  =========  ==========================
finding token                        gate rule  ``privacy_filter.py``
===================================  =========  ==========================
:data:`RULE_DANGLING_PARENT`         (c)        ``:767-770``
:data:`RULE_PARENT_CYCLE`            (d)        ``:771-773``
:data:`RULE_DEEP_BREADCRUMB`         (e)        ``:774-787``
:data:`RULE_DUPLICATE_ID`            (h)        ``:797-804``
===================================  =========  ==========================

Rule (e) is **not** "a root carrying a breadcrumb". The issue phrases it as
``parent_id is None`` plus a non-empty ``structural_path``; the code compares
``len(structural_path)`` against the strict-ancestor depth it could actually
walk. Depth 0 is only the special case an unmodified pipeline could plausibly
produce. An implementation covering depth 0 alone reports "clean" about a
vault the gate is refusing — reproducing exactly the undiagnosable refusal.

Rule (h) is absent from the issue. Two files claiming one content-hashed id
poison the chain with ``INTIMATE`` and produce no operator-visible signal
anywhere. Same fail-closed trigger, same zero visibility; it belongs here.

**Discloses paths and ids, never content.** ``creek state`` appends the lint
report verbatim into a committable document at ``ceiling=intimate`` or broader
(``creek/generate/state.py:1385-1401``), so a finding is a shareable artifact.
Fragment ids are content-hashed and safe. Titles are **not**: an atomized
child's title is derived from its body (``creek/atomize/split.py:251``,
``:278``), and ``structural_path`` entries are ancestor headings, i.e.
document text. Neither is ever printed — breadcrumbs are reported as a
*count*. ``tests/test_lint_new_checks_privacy.py`` asserts that at the
narrowest ceiling which still renders the section.

**Reports, never repairs.** No ``--fix``, no rewrite of ``parent_id``, no
deletion. Which file owns a duplicated id is not derivable from the files
themselves; only a human can adjudicate it.

**Anomalous by construction.** ``creek/atomize/split.py``'s ``_build_children``
is the sole writer of ``structural_path`` and sets ``parent_id`` in the same
``model_copy``, so none of these states is reachable from an unmodified
pipeline. That is why the check needs no allowlist and no severity tiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult
from creek.vault.reader import iter_vault_fragments

RULE_DANGLING_PARENT = "dangling-parent"
RULE_PARENT_CYCLE = "parent-cycle"
RULE_DEEP_BREADCRUMB = "breadcrumb-deeper-than-ancestry"
RULE_DUPLICATE_ID = "duplicate-fragment-id"
"""Stable finding tokens, one per gate trigger.

Each finding carries exactly one, so an operator can map a report line back to
the rule that will refuse the compile and ``creek state``'s verbatim echo
stays greppable.
"""

_FRAGMENTS_SUBDIR: str = "01-Fragments"


@dataclass(frozen=True)
class _Node:
    """One fragment as this check ranks it.

    Attributes:
        path: Vault-relative path of the file, the only locator a finding
            prints.
        parent_id: The declared link up the hierarchy, verbatim.
        resolved_parent: ``parent_id`` when it names a loadable fragment,
            otherwise ``None`` — the walk cannot follow what it cannot load,
            and neither can the gate.
        breadcrumb_len: ``len(structural_path)``. A **count**, never the
            entries: breadcrumb strings are ancestor headings, i.e. document
            text.
    """

    path: Path
    parent_id: str | None
    resolved_parent: str | None
    breadcrumb_len: int


def _collect(vault_path: Path) -> tuple[dict[str, _Node], dict[str, list[Path]]]:
    """Walk ``01-Fragments`` once and return the ancestry graph and id claims.

    Uses :func:`creek.vault.reader.iter_vault_fragments` — the *same* loader
    the compile gate reads the corpus through. A file one side sees and the
    other does not is the bug class these surveys exist to prevent, so the two
    must never disagree about which fragments exist.

    Returns:
        ``(nodes, claims)`` where *nodes* maps a fragment id to its
        :class:`_Node` (first claimant wins, so the graph is well-defined even
        with a duplicate) and *claims* maps every id to every vault-relative
        path claiming it.
    """
    records = [
        (path.relative_to(vault_path), fragment)
        for path, fragment, _body, _meta in iter_vault_fragments(
            vault_path / _FRAGMENTS_SUBDIR,
        )
    ]
    claims: dict[str, list[Path]] = {}
    for relative, fragment in records:
        claims.setdefault(fragment.id, []).append(relative)

    # `claims` doubles as the set of ids that exist, which is what makes a
    # parent_id "resolvable". Deciding that per node during the walk would
    # need the whole corpus anyway, so it is settled once here.
    nodes: dict[str, _Node] = {}
    for relative, fragment in records:
        parent_id = fragment.parent_id
        nodes.setdefault(
            fragment.id,
            _Node(
                path=relative,
                parent_id=parent_id,
                resolved_parent=parent_id if parent_id in claims else None,
                breadcrumb_len=len(fragment.structural_path),
            ),
        )
    return nodes, claims


def _walkable_depth(nodes: dict[str, _Node], leaf_id: str) -> int:
    """Return how many strict ancestors of *leaf_id* the walk could survey.

    Stops at a clean root, at a parent that does not resolve, and at a repeat
    visit — the same three terminations
    :meth:`~creek.classify.privacy_filter.AncestorIndex._ascend` uses, so the
    number this returns is the one rule (e) is applied against on the gate
    side.
    """
    depth = 0
    visited = {leaf_id}
    current = nodes[leaf_id].resolved_parent
    while current is not None and current not in visited:
        visited.add(current)
        depth += 1
        current = nodes[current].resolved_parent
    return depth


def _find_cycles(nodes: dict[str, _Node]) -> list[tuple[str, ...]]:
    """Return each ``parent_id`` cycle once, as a sorted tuple of member ids.

    Reported per cycle rather than per member: every member states the same
    single fact, and a long cycle would otherwise drown the report in
    restatements of it. Members are sorted so the finding is deterministic.
    """
    done: set[str] = set()
    cycles: list[tuple[str, ...]] = []
    for start in nodes:
        if start in done:
            continue
        stack: list[str] = []
        position: dict[str, int] = {}
        current: str | None = start
        while current is not None and current not in done and current not in position:
            position[current] = len(stack)
            stack.append(current)
            current = nodes[current].resolved_parent
        if current is not None and current in position:
            cycles.append(tuple(sorted(stack[position[current] :])))
        done.update(stack)
    return cycles


def _dangling_findings(nodes: dict[str, _Node]) -> list[str]:
    """Report every fragment whose ``parent_id`` names no loadable fragment.

    Worded as the gate means it. :func:`creek.vault.reader.try_load_fragment`
    collapses missing, unreadable, non-``fragment``-typed and schema-invalid
    into one indistinguishable absence, so promising more than "resolves to no
    loadable fragment" would be a distinction the operator could act on
    wrongly.
    """
    return [
        f"- `{node.path}` — {RULE_DANGLING_PARENT}: parent_id `{node.parent_id}` "
        "resolves to no loadable fragment"
        for _frag_id, node in sorted(nodes.items())
        if node.parent_id is not None and node.resolved_parent is None
    ]


def _cycle_findings(nodes: dict[str, _Node]) -> list[str]:
    """Report each ``parent_id`` cycle once, naming every member and its file."""
    findings: list[str] = []
    for cycle in sorted(_find_cycles(nodes)):
        members = ", ".join(f"`{fid}` (`{nodes[fid].path}`)" for fid in cycle)
        findings.append(
            f"- {RULE_PARENT_CYCLE}: parent_id walk revisits itself across "
            f"{len(cycle)} fragment(s): {members}",
        )
    return findings


def _breadcrumb_findings(nodes: dict[str, _Node]) -> list[str]:
    """Report every fragment whose breadcrumb is deeper than its walkable ancestry.

    The depth-0 case gets its own wording: it is the one an unmodified
    pipeline could plausibly produce and the one an operator can actually fix.
    The general case is reported too, and that is the whole point — a
    depth-0-only implementation calls a re-parented fragment clean while the
    gate refuses it.

    Only counts are printed. ``structural_path`` entries are ancestor
    headings lifted out of document text and must never reach the report.
    """
    findings: list[str] = []
    for frag_id, node in sorted(nodes.items()):
        depth = _walkable_depth(nodes, frag_id)
        if node.breadcrumb_len <= depth:
            continue
        detail = (
            f"root fragment carries a {node.breadcrumb_len}-entry structural_path"
            if depth == 0
            else (
                f"{node.breadcrumb_len}-entry structural_path over "
                f"{depth} walkable ancestor(s)"
            )
        )
        findings.append(f"- `{node.path}` — {RULE_DEEP_BREADCRUMB}: {detail}")
    return findings


def _duplicate_findings(claims: dict[str, list[Path]]) -> list[str]:
    """Report every id claimed by more than one file, naming all claimants.

    Naming only one would leave the operator unable to pick the impostor — and
    ids are content-hashed, so a collision is anomalous rather than a
    near-miss.
    """
    return [
        f"- `{frag_id}` — {RULE_DUPLICATE_ID}: claimed by "
        + ", ".join(f"`{path}`" for path in sorted(paths))
        for frag_id, paths in sorted(claims.items())
        if len(paths) > 1
    ]


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report every ancestry anomaly the compile gate fails closed on.

    All four rules are evaluated on one walk and **all** are reported: a check
    that returned at the first offender would let three real problems hide
    behind one, and the operator would fix, re-run, and discover the next —
    once per anomaly.

    Args:
        vault_path: Root of the Obsidian vault.
        since: Accepted for interface symmetry with the other checks and
            ignored — the anomalies this finds are structural, not recent, and
            an mtime window would hide the ones that have been refusing
            compiles the longest.

    Returns:
        A :class:`~creek.lint._result.CheckResult` naming each anomaly by
        vault-relative path and content-hashed id, grouped by rule.
    """
    del since  # interface symmetry only
    nodes, claims = _collect(vault_path)
    findings = [
        *_dangling_findings(nodes),
        *_cycle_findings(nodes),
        *_breadcrumb_findings(nodes),
        *_duplicate_findings(claims),
    ]
    summary = (
        f"{len(findings)} ancestry anomaly(ies) across "
        f"{len(nodes)} fragment(s) — each one fails the compile gate closed"
    )
    return CheckResult(name="ancestry", summary=summary, findings=findings)
