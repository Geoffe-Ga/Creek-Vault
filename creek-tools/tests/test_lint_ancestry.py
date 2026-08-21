"""``creek lint --check ancestry``: name what the compile gate refuses over (#1277).

``creek.classify.privacy_filter.AncestorIndex.chain_tiers`` fails **closed** to
``INTIMATE`` on four distinct vault anomalies, and ``creek_mcp.tools.compile``
reports every one of them through the deliberately content-free
``_ABOVE_CEILING_REASON``. The refusal is correct and the silence is correct —
the reason string must not leak which ancestor offended — but it leaves an
operator with a vault the gate will not compile and nowhere to look. This
check is the other half of that contract: the same four triggers, surfaced
by name, in a report the operator asked for.

**The check must be an exact inverse of the gate.** Any trigger lint cannot
see is a vault that refuses undiagnosably, which is the precise failure #1277
exists to end. Hence four rules, not the three the issue lists:

===================================  =========  ==========================
rule                                 gate rule  privacy_filter.py
===================================  =========  ==========================
``dangling-parent``                  (c)        :767-770
``parent-cycle``                     (d)        :771-773
``breadcrumb-deeper-than-ancestry``  (e)        :774-787
``duplicate-fragment-id``            (h)        :797-804
===================================  =========  ==========================

Two traps this module pins deliberately:

* **Rule (e) is not "root with a breadcrumb".** The issue phrases it as
  ``parent_id is None`` plus a non-empty ``structural_path``; the code
  compares ``len(structural_path)`` against the strict-ancestor depth it
  could actually walk. The depth-0 case is only the special case an
  unmodified pipeline could plausibly produce. An implementation covering
  only depth-0 reports "clean" about a vault the gate is refusing —
  reproducing exactly the undiagnosable refusal. See
  :meth:`TestBreadcrumbDeeperThanAncestry.test_a_reparented_fragment_is_reported`.
* **Rule (h) is absent from the issue.** Two files claiming one content-hashed
  id poison the chain with ``INTIMATE`` and no operator-visible signal
  anywhere. Same fail-closed trigger, same zero visibility; it belongs here.

Every fixture is **anomalous by construction**: ``creek/atomize/split.py``
:func:`_build_children` is the sole writer of ``structural_path`` and sets
``parent_id`` in the same ``model_copy``. None of these states is reachable
from an unmodified pipeline, which is why the check needs no allowlist and no
severity tiers — and why the fixtures are hand-written on disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.classify.privacy_filter import build_ancestor_index
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.vault.reader import iter_vault_fragments
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.lint._result import CheckResult

RULE_DANGLING_PARENT = "dangling-parent"
RULE_PARENT_CYCLE = "parent-cycle"
RULE_DEEP_BREADCRUMB = "breadcrumb-deeper-than-ancestry"
RULE_DUPLICATE_ID = "duplicate-fragment-id"
"""Stable finding tokens, one per gate trigger.

Findings are grouped by these so an operator can map a report line back to the
rule that will refuse the compile, and so ``creek state``'s verbatim echo
stays greppable.
"""

ALL_RULES: tuple[str, ...] = (
    RULE_DANGLING_PARENT,
    RULE_PARENT_CYCLE,
    RULE_DEEP_BREADCRUMB,
    RULE_DUPLICATE_ID,
)


def _ancestry() -> Callable[[Path], CheckResult]:
    """Return the ancestry check entry point (added by the fix for #1277).

    Imported inside the tests rather than at module scope so a missing module
    fails each test individually, instead of collapsing the file into one
    collection error that hides how many behaviours are unimplemented. The
    return type names the callable rather than the module: a ``ModuleType``
    attribute types as ``Any``, which would erase every ``CheckResult``
    assertion in this file from type checking.
    """
    from creek.lint.checks.ancestry import run

    return run


def _run(vault: Path) -> CheckResult:
    """Run the ancestry check against *vault*."""
    return _ancestry()(vault)


def _write(
    vault: Path,
    frag_id: str,
    *,
    parent_id: str | None = None,
    structural_path: list[str] | None = None,
    tier: PrivacyTier = PrivacyTier.OPEN,
    title: str | None = None,
    body: str = "Body text.",
    filename: str | None = None,
) -> Path:
    """Write one fragment file, anomalies and all.

    Args:
        vault: Vault root.
        frag_id: Fragment id to stamp in the frontmatter.
        parent_id: Optional link up the hierarchy.
        structural_path: Optional persisted breadcrumb.
        tier: Declared privacy tier.
        title: Fragment title; defaults to ``Title <frag_id>``.
        body: Markdown body.
        filename: Optional filename override, so two files can claim one id.
            ``write_fragment_file`` always writes ``<id>.md``, so a second
            claimant would overwrite the first. Callers building a duplicate
            therefore write the *renamed* file first, which moves it out of
            the way, and the canonical ``<id>.md`` second — see
            :func:`_write_duplicate_pair`.

    Returns:
        Path to the written file.
    """
    fragment = Fragment(
        id=frag_id,
        title=title if title is not None else f"Title {frag_id}",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
        ),
        created=datetime(2026, 5, 1, tzinfo=UTC),
        privacy_tier=tier,
        parent_id=parent_id,
        structural_path=structural_path or [],
    )
    path = write_fragment_file(vault=vault, fragment=fragment, body=body)
    if filename is not None:
        renamed = path.with_name(filename)
        path.rename(renamed)
        return renamed
    return path


def _write_duplicate_pair(vault: Path, frag_id: str) -> tuple[Path, Path]:
    """Write two files both claiming *frag_id*, and return them sorted.

    Order matters: the shadow is written and renamed away first, so the
    canonical ``<frag_id>.md`` written second does not clobber it. Getting
    this backwards leaves one file on disk and a fixture that silently tests
    nothing — verified against
    :func:`creek.classify.privacy_filter.build_ancestor_index`, which reports
    ``INTIMATE`` only when both files survive.
    """
    shadow = _write(
        vault,
        frag_id,
        body="Second claimant.",
        filename=f"{frag_id}-shadow.md",
    )
    canonical = _write(vault, frag_id, body="First claimant.")
    return canonical, shadow


def _gate_says_intimate(vault: Path, leaf_id: str) -> bool:
    """Return whether the compile gate fails closed on *leaf_id*.

    The parity oracle: reads the corpus through the same loader the gate uses
    (``iter_vault_fragments``) and asks ``AncestorIndex`` directly.
    """
    records = [
        (fragment, raw)
        for _path, fragment, _body, raw in iter_vault_fragments(vault / "01-Fragments")
    ]
    tiers = build_ancestor_index(records).chain_tiers([leaf_id])
    return PrivacyTier.INTIMATE in tiers


def _findings_for(result: CheckResult, rule: str) -> list[str]:
    """Return the findings carrying *rule*'s token."""
    return [line for line in result.findings if rule in line]


class TestDanglingParent:
    """Rule (c): a ``parent_id`` naming no loadable fragment."""

    def test_a_dangling_parent_is_reported(self, tmp_path: Path) -> None:
        """The child names a parent that is not in the vault at all."""
        path = _write(tmp_path, "frag-child", parent_id="frag-ghost")

        result = _run(tmp_path)

        hits = _findings_for(result, RULE_DANGLING_PARENT)
        assert len(hits) == 1
        assert str(path.relative_to(tmp_path)) in hits[0]
        assert "frag-ghost" in hits[0]

    def test_a_resolvable_parent_is_not_reported(self, tmp_path: Path) -> None:
        """The negative control: a real parent produces no dangling finding."""
        _write(tmp_path, "frag-parent")
        _write(tmp_path, "frag-child", parent_id="frag-parent")

        assert _findings_for(_run(tmp_path), RULE_DANGLING_PARENT) == []

    def test_an_unloadable_parent_counts_as_dangling(self, tmp_path: Path) -> None:
        """ "Present but unloadable" and "absent" are one indistinguishable state.

        ``try_load_fragment`` collapses missing, unreadable, non-``fragment``
        typed and schema-invalid into a single absence, and the gate cannot
        tell them apart either. Wording that promised more than that would be
        a lie the operator could act on wrongly.
        """
        broken = tmp_path / "01-Fragments" / "Notes" / "frag-parent.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("---\ntype: note\nid: frag-parent\n---\n\nNot a fragment.\n")
        _write(tmp_path, "frag-child", parent_id="frag-parent")

        assert len(_findings_for(_run(tmp_path), RULE_DANGLING_PARENT)) == 1


class TestParentCycle:
    """Rule (d): ``parent_id`` walking revisits a node."""

    def test_a_two_node_cycle_is_reported_once(self, tmp_path: Path) -> None:
        """Report per cycle, not per member — two members, one finding.

        Keyed by the minimum id in the cycle so the choice is deterministic.
        One finding per member would make a long cycle drown the report in
        restatements of a single fact.
        """
        _write(tmp_path, "frag-a", parent_id="frag-b")
        _write(tmp_path, "frag-b", parent_id="frag-a")

        hits = _findings_for(_run(tmp_path), RULE_PARENT_CYCLE)

        assert len(hits) == 1
        assert "frag-a" in hits[0]
        assert "frag-b" in hits[0]

    def test_a_three_node_cycle_is_reported_once(self, tmp_path: Path) -> None:
        """Cycle length does not multiply the finding count."""
        _write(tmp_path, "frag-a", parent_id="frag-b")
        _write(tmp_path, "frag-b", parent_id="frag-c")
        _write(tmp_path, "frag-c", parent_id="frag-a")

        assert len(_findings_for(_run(tmp_path), RULE_PARENT_CYCLE)) == 1

    def test_a_self_parent_is_a_cycle(self, tmp_path: Path) -> None:
        """The degenerate one-node cycle still fails the gate, so still reports."""
        _write(tmp_path, "frag-a", parent_id="frag-a")

        assert len(_findings_for(_run(tmp_path), RULE_PARENT_CYCLE)) == 1

    def test_two_separate_cycles_are_two_findings(self, tmp_path: Path) -> None:
        """Per-cycle, not per-vault: distinct cycles are distinct operator tasks."""
        _write(tmp_path, "frag-a", parent_id="frag-b")
        _write(tmp_path, "frag-b", parent_id="frag-a")
        _write(tmp_path, "frag-y", parent_id="frag-z")
        _write(tmp_path, "frag-z", parent_id="frag-y")

        assert len(_findings_for(_run(tmp_path), RULE_PARENT_CYCLE)) == 2


class TestBreadcrumbDeeperThanAncestry:
    """Rule (e): ``len(structural_path)`` exceeds the walkable ancestor depth."""

    def test_a_root_carrying_a_breadcrumb_is_reported(self, tmp_path: Path) -> None:
        """The depth-0 case — no parent, two breadcrumb entries.

        The one an unmodified pipeline could plausibly produce and the one an
        operator can actually fix, so it earns its own wording.
        """
        path = _write(
            tmp_path,
            "frag-orphan",
            parent_id=None,
            structural_path=["Chapter", "Section"],
        )

        hits = _findings_for(_run(tmp_path), RULE_DEEP_BREADCRUMB)

        assert len(hits) == 1
        assert str(path.relative_to(tmp_path)) in hits[0]

    def test_a_reparented_fragment_is_reported(self, tmp_path: Path) -> None:
        """THE TRAP. Depth 1 of walkable ancestry, a 3-entry breadcrumb.

        A depth-0-only implementation calls this vault clean while the compile
        gate refuses it. That is the exact undiagnosable refusal #1277 exists
        to end, so this test is the one that decides whether the check is an
        inverse of the gate or merely resembles one.
        """
        _write(tmp_path, "frag-root")
        path = _write(
            tmp_path,
            "frag-deep",
            parent_id="frag-root",
            structural_path=["Chapter", "Section", "Subsection"],
        )

        hits = _findings_for(_run(tmp_path), RULE_DEEP_BREADCRUMB)

        assert len(hits) == 1
        assert str(path.relative_to(tmp_path)) in hits[0]

    def test_a_breadcrumb_matching_the_ancestry_is_not_reported(
        self,
        tmp_path: Path,
    ) -> None:
        """Negative control: depth 2 of ancestry, 2 breadcrumb entries."""
        _write(tmp_path, "frag-root")
        _write(tmp_path, "frag-mid", parent_id="frag-root", structural_path=["Chapter"])
        _write(
            tmp_path,
            "frag-leaf",
            parent_id="frag-mid",
            structural_path=["Chapter", "Section"],
        )

        assert _findings_for(_run(tmp_path), RULE_DEEP_BREADCRUMB) == []

    def test_a_shallower_breadcrumb_is_not_reported(self, tmp_path: Path) -> None:
        """Only *excess* breadcrumb depth fails the gate; a shorter one is fine."""
        _write(tmp_path, "frag-root")
        _write(tmp_path, "frag-mid", parent_id="frag-root")
        _write(tmp_path, "frag-leaf", parent_id="frag-mid", structural_path=["Chapter"])

        assert _findings_for(_run(tmp_path), RULE_DEEP_BREADCRUMB) == []


class TestDuplicateFragmentId:
    """Rule (h): one content-hashed id claimed by more than one file."""

    def test_both_claiming_paths_are_reported(self, tmp_path: Path) -> None:
        """Naming only one file leaves the operator unable to pick the impostor."""
        first, second = _write_duplicate_pair(tmp_path, "frag-dup")

        hits = _findings_for(_run(tmp_path), RULE_DUPLICATE_ID)

        assert len(hits) == 1
        joined = hits[0]
        assert str(first.relative_to(tmp_path)) in joined
        assert str(second.relative_to(tmp_path)) in joined

    def test_distinct_ids_are_not_reported(self, tmp_path: Path) -> None:
        """Negative control."""
        _write(tmp_path, "frag-a")
        _write(tmp_path, "frag-b")

        assert _findings_for(_run(tmp_path), RULE_DUPLICATE_ID) == []


class TestCleanVaultAndReportShape:
    """A well-formed hierarchy is silent, and the check never mutates."""

    def test_a_clean_hierarchical_vault_reports_nothing(self, tmp_path: Path) -> None:
        """Parent plus two children, breadcrumbs consistent: zero findings."""
        _write(tmp_path, "frag-root")
        _write(
            tmp_path,
            "frag-child-1",
            parent_id="frag-root",
            structural_path=["Chapter"],
        )
        _write(
            tmp_path,
            "frag-child-2",
            parent_id="frag-root",
            structural_path=["Chapter"],
        )

        result = _run(tmp_path)

        assert result.findings == []
        assert result.name == "ancestry"

    def test_an_empty_vault_reports_nothing(self, tmp_path: Path) -> None:
        """A vault with no ``01-Fragments`` yields a clean result, not an error."""
        assert _run(tmp_path).findings == []

    def test_every_rule_is_reported_never_short_circuited(
        self,
        tmp_path: Path,
    ) -> None:
        """All four anomalies in one vault produce all four rules.

        A walk that returned at the first offender would let three real
        problems hide behind one, and the operator would fix, re-run, and
        discover the next — once per anomaly.
        """
        _write(tmp_path, "frag-dangling", parent_id="frag-ghost")
        _write(tmp_path, "frag-cyc-a", parent_id="frag-cyc-b")
        _write(tmp_path, "frag-cyc-b", parent_id="frag-cyc-a")
        _write(tmp_path, "frag-deep", structural_path=["Chapter", "Section"])
        _write_duplicate_pair(tmp_path, "frag-dup")

        result = _run(tmp_path)

        for rule in ALL_RULES:
            assert _findings_for(result, rule), rule

    def test_the_check_never_mutates_the_vault(self, tmp_path: Path) -> None:
        """Reports, never repairs: no ``--fix``, no rewrite, no deletion.

        Every anomaly here is one only a human can adjudicate — which file
        owns a duplicated id is not derivable from the files themselves.
        """
        _write(tmp_path, "frag-dangling", parent_id="frag-ghost")
        _write(tmp_path, "frag-cyc-a", parent_id="frag-cyc-b")
        _write(tmp_path, "frag-cyc-b", parent_id="frag-cyc-a")
        before = {
            path: path.read_bytes()
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

        _run(tmp_path)

        after = {
            path: path.read_bytes()
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }
        assert after == before

    def test_the_check_is_registered_and_runs_by_default(self) -> None:
        """``creek lint`` with no flags must include it, or #1277 is unfixed.

        A check an operator has to know to ask for cannot answer the question
        "why is the gate refusing my vault", which is asked precisely by
        someone who does not know.
        """
        from creek.lint import runner as runner_module

        assert "ancestry" in runner_module.DETERMINISTIC_CHECKS
        assert "ancestry" in runner_module._REGISTRY


class TestGateParity:
    """Every fixture lint reports must be one the gate actually refuses.

    The inverse property, asserted directly rather than by inspection: if lint
    flags a vault the gate compiles happily the check is noise, and if the
    gate refuses a vault lint calls clean the operator is back to a
    content-free refusal with nowhere to look.
    """

    @pytest.mark.parametrize(
        ("rule", "builder"),
        [
            (
                RULE_DANGLING_PARENT,
                lambda v: [_write(v, "frag-leaf", parent_id="frag-ghost")],
            ),
            (
                RULE_PARENT_CYCLE,
                lambda v: [
                    _write(v, "frag-leaf", parent_id="frag-b"),
                    _write(v, "frag-b", parent_id="frag-leaf"),
                ],
            ),
            (
                RULE_DEEP_BREADCRUMB,
                lambda v: [
                    _write(v, "frag-leaf", structural_path=["Chapter", "Section"]),
                ],
            ),
            (
                RULE_DUPLICATE_ID,
                lambda v: list(_write_duplicate_pair(v, "frag-leaf")),
            ),
        ],
        ids=list(ALL_RULES),
    )
    def test_a_reported_vault_is_one_the_gate_fails_closed_on(
        self,
        tmp_path: Path,
        rule: str,
        builder: object,
    ) -> None:
        """Lint reports it *and* ``chain_tiers`` contributes ``INTIMATE``."""
        assert callable(builder)
        builder(tmp_path)

        assert _findings_for(_run(tmp_path), rule), rule
        assert _gate_says_intimate(tmp_path, "frag-leaf") is True

    def test_a_clean_vault_is_one_the_gate_admits(self, tmp_path: Path) -> None:
        """The other direction: silence from lint means silence from the gate."""
        _write(tmp_path, "frag-root")
        _write(
            tmp_path,
            "frag-leaf",
            parent_id="frag-root",
            structural_path=["Chapter"],
        )

        assert _run(tmp_path).findings == []
        assert _gate_says_intimate(tmp_path, "frag-leaf") is False
