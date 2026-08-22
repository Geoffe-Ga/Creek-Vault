"""``creek lint`` findings must not move when the link index is shared (#1223).

#1223 is pure de-duplication of work: ``broken-links`` and ``orphan-compiled``
each call ``build_link_index`` today, so a default run walks and header-parses
the whole vault twice. Threading one index through both is meant to change
*nothing* an operator sees.

**Every test in this module passes before the threading change and must pass
after it.** "No finding changed" is #1223's acceptance criterion, not a side
effect, so it is asserted rather than assumed.

Two independent layers, because either alone has a blind spot:

* :class:`TestSharedIndexMatchesStandalone` compares the runner's results
  against each check called standalone. Today both paths build their own
  index and the comparison is trivially true; after the change the runner
  shares one index and the standalone calls do not, so the equality becomes a
  real cross-check. It is self-maintaining — no literal to update — but it
  cannot notice a change applied identically to both paths.
* :class:`TestFindingsMatchTheCommittedBaseline` pins the exact strings,
  captured by running the pre-change code against the fixture below. That
  catches a change to the check *logic* which the first layer would wave
  through.

The fixture is deliberately **insensitive to #1224**, which ships alongside
this work and legitimately *can* move orphan-compiled verdicts: no name here
is both a foreign page's declared alias and another page's filename stem, so
the resolution ladder cannot reorder anything. That keeps a failure of this
module unambiguous — it means the threading changed behaviour, not that the
precedence rule did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.lint.checks import broken_links, orphan_compiled
from creek.lint.runner import LintRunner

if TYPE_CHECKING:
    from pathlib import Path

_THREADED_CHECKS: tuple[str, ...] = ("broken-links", "orphan-compiled")

_BASELINE: dict[str, tuple[str, tuple[str, ...]]] = {
    "broken-links": (
        "2 broken link(s) across 6 file(s)",
        (
            "- `01-Fragments/frag-alpha.md` → `[[nowhere-at-all]]`",
            "- `01-Fragments/frag-beta.md` → `[[Also Missing]]`",
        ),
    ),
    "orphan-compiled": (
        "1 orphan compiled page(s)",
        ("- `03-Eddies/Lonely Eddy.md` (suggestion: review; never auto-deleted)",),
    ),
}
"""``{check_name: (summary, findings)}`` measured against the pre-#1223 code.

Regenerated only when a deliberate behaviour change is being made, and never
to make a refactor go green.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault exercising every path both threaded checks take.

    Covers: a declared alias, a case-folded wiki-link, two genuinely dangling
    links, one orphan compiled page, and two generated index notes that must
    stay *excluded* from the orphan verdict.
    """
    root = tmp_path / "vault"

    def _page(relpath: str, text: str) -> None:
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    _page(
        "01-Fragments/frag-alpha.md",
        "---\ntitle: Alpha note\naliases:\n  - Alpha Alias\n---\n\n"
        "Links to [[Beta Thread]] and [[nowhere-at-all]].\n",
    )
    _page(
        "01-Fragments/frag-beta.md",
        "---\ntitle: Beta note\n---\n\n"
        "Case-folded link to [[ALPHA ALIAS]] and a dangling [[Also Missing]].\n",
    )
    _page(
        "02-Threads/Beta Thread.md",
        "---\ntype: thread\nid: thread-beta\ntitle: Beta Thread\n---\n\nA current.\n",
    )
    _page(
        "03-Eddies/Lonely Eddy.md",
        "---\ntype: eddy\nid: eddy-lonely\ntitle: Lonely Eddy\n---\n\n"
        "Nobody links here.\n",
    )
    _page(
        "02-Threads/Thread-Index.md",
        "---\ntype: thread-index\ntitle: Thread Index\n---\n\n"
        "Dataview query, never linked.\n",
    )
    _page(
        "06-Frequencies/F1-Index.md",
        "---\ntype: frequency-index\ntitle: F1 Index\n---\n\n"
        "Dataview query, never linked.\n",
    )
    return root


class TestFindingsMatchTheCommittedBaseline:
    """The exact strings, pinned against pre-change output."""

    @pytest.mark.parametrize("check_name", _THREADED_CHECKS)
    def test_check_output_is_unchanged(self, vault: Path, check_name: str) -> None:
        """Summary and findings both match the committed baseline verbatim."""
        expected_summary, expected_findings = _BASELINE[check_name]

        (result,) = LintRunner(vault).run([check_name]).results

        assert result.name == check_name
        assert result.summary == expected_summary
        assert tuple(result.findings) == expected_findings

    def test_a_full_default_run_still_contains_both_checks(
        self,
        vault: Path,
    ) -> None:
        """Guards against a threaded check quietly dropping out of the default set.

        A refactor that removed ``orphan-compiled`` from the registry would
        make every finding assertion above vacuously true by never running it.
        """
        names = [result.name for result in LintRunner(vault).run().results]

        assert set(_THREADED_CHECKS) <= set(names)


class TestSharedIndexMatchesStandalone:
    """The runner's results must equal each check called on its own."""

    def test_broken_links_matches_its_standalone_call(self, vault: Path) -> None:
        """``broken_links.run(vault)`` with no injected index agrees exactly."""
        (threaded,) = LintRunner(vault).run(["broken-links"]).results
        standalone = broken_links.run(vault)

        assert threaded.summary == standalone.summary
        assert threaded.findings == standalone.findings

    def test_orphan_compiled_matches_its_standalone_call(self, vault: Path) -> None:
        """``orphan_compiled.run(vault)`` with no injected index agrees exactly."""
        (threaded,) = LintRunner(vault).run(["orphan-compiled"]).results
        standalone = orphan_compiled.run(vault)

        assert threaded.summary == standalone.summary
        assert threaded.findings == standalone.findings

    def test_running_both_together_matches_running_each_alone(
        self,
        vault: Path,
    ) -> None:
        """Sharing one index across two checks must not couple their verdicts.

        The specific hazard: a shared mutable index that one check consumes or
        filters would leave the second check reading a different vault than it
        would have built for itself.
        """
        together = {
            r.name: (r.summary, r.findings)
            for r in LintRunner(vault)
            .run(
                list(_THREADED_CHECKS),
            )
            .results
        }
        separately = {
            name: (r.summary, r.findings)
            for name in _THREADED_CHECKS
            for r in LintRunner(vault).run([name]).results
        }

        assert together == separately

    def test_two_successive_runs_report_identically(self, vault: Path) -> None:
        """A shared index must not leak state between runs.

        ``iter_link_sources`` withholds ``00-Creek-Meta/Processing-Log`` for
        exactly this reason (#1344): lint quoting its own output back inflated
        the broken-link count on each successive run. A cached or module-level
        index would reintroduce a run-to-run dependency by another route.
        """
        first = [
            (r.name, r.summary, r.findings) for r in LintRunner(vault).run().results
        ]
        second = [
            (r.name, r.summary, r.findings) for r in LintRunner(vault).run().results
        ]

        assert first == second
