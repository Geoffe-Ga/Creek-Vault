"""New lint checks must not carry fragment content into a shareable artifact.

``creek state``'s ``## Lint summary`` appends the lint report **verbatim**
(``creek/generate/state.py:1385-1401``). #969 admits that section at
``ceiling=intimate`` or broader — and the CLI's default ceiling is ``all``, so
a plain ``creek state`` includes it. Whatever the checks added by #1277 and
#883 print therefore lands, unedited, in a committable document.

**Everything asserted here is asserted at the NARROWEST admitting ceiling**,
``PrivacyTierOverride.INTIMATE``, not at the permissive default. Testing at
``ALL`` would prove only that the loosest configuration leaks nothing, while
the interesting claim is that the *strictest* configuration that still shows
the section shows nothing it should not.

What counts as content, and why the obvious mistake is easy:

* **Fragment bodies** — never read by either check. ``root-hygiene`` measures
  emptiness with ``stat().st_size``; ``ancestry`` reads frontmatter only.
* **Fragment titles** — content too, which is the non-obvious half. An
  atomized child's title is *derived from its body*:
  ``creek/atomize/split.py:251`` calls ``_derive_title_from_content``, whose
  ``:278`` takes ``content.strip().splitlines()[0]``. Printing
  ``fragment.title`` in a finding therefore prints the first line of the body.
* **``structural_path`` entries** — ancestor headings the splitter
  accumulated, i.e. document text.
* **Absolute paths** — the #969 defect ``creek.lint.checks.broken_links.run``
  documents at length in its vault-relative paragraph: an absolute source put
  ``/Users/<operator>/...`` into the artifact and falsified the standing "no
  path is absolute" claim in ``docs/generation.md``.

Permitted, and asserted positively so the checks are not merely silent:
vault-relative paths and content-hashed fragment ids
(``creek.ingest.base.generate_fragment_id``).

Live precedent that this is not theoretical: #1506 — artifacts written with
no ``privacy_tier`` at all. And the review queue itself already prints
``**{fragment.title}**`` for ``INTIMATE`` fragments — see
``creek.classify.review.ReviewQueueGenerator._format_fragment_entry`` — into a
file carrying no tier. Relocating it under
``00-Creek-Meta/Processing-Log/`` adds no new exposure — ``latest_lint_report``
reads only ``lint-*.md`` — so that is a follow-up issue, not this PR's scope,
and :meth:`TestReviewQueueExposureIsUnchanged` pins the "no new exposure"
half so the claim is checked rather than asserted in prose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate.state import StateReportGenerator
from creek.lint.runner import LintRunner
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.lint._result import CheckResult

SENTINEL = "XYZZYSECRET"
"""A string that appears only in places a finding must never quote.

Planted in every fragment body, every fragment title and every
``structural_path`` heading of the fixture below. One token rather than
several so a single ``in`` test covers all three content channels.
"""

NEW_CHECKS: tuple[str, ...] = ("ancestry", "root-hygiene")
"""The checks #1277 and #883 add. Both are surveyed by every test here."""

NARROWEST_ADMITTING_CEILING = PrivacyTierOverride.INTIMATE
"""The strictest ceiling at which ``creek state`` still renders the Lint summary.

``section_lint_summary`` gates on
``tier_within_override(PrivacyTier.INTIMATE, override)``, so ``OPEN`` and
``PERSONAL`` drop the section entirely and would make every assertion below
vacuously true.
"""


def _check_entry_point(name: str) -> Callable[[Path], CheckResult]:
    """Return one of the new checks' entry points, by registry name.

    Imported per-test so a missing module fails each behaviour individually
    instead of collapsing this file into a single collection error. The
    return type names the callable rather than the module: a ``ModuleType``
    attribute types as ``Any``, which would erase from type checking exactly
    the ``CheckResult`` assertions that pin the no-body-text guarantee.
    """
    if name == "ancestry":
        from creek.lint.checks.ancestry import run as ancestry_run

        return ancestry_run
    from creek.lint.checks.root_hygiene import run as root_hygiene_run

    return root_hygiene_run


def _run(name: str, vault: Path) -> CheckResult:
    """Run the named new check against *vault*."""
    return _check_entry_point(name)(vault)


def _write_fragment(
    vault: Path,
    frag_id: str,
    *,
    parent_id: str | None = None,
    structural_path: list[str] | None = None,
    filename: str | None = None,
) -> Path:
    """Write an ``INTIMATE`` fragment whose every text field carries the sentinel."""
    fragment = Fragment(
        id=frag_id,
        title=f"{SENTINEL} in the title of {frag_id}",
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
        ),
        created=datetime(2026, 5, 1, tzinfo=UTC),
        privacy_tier=PrivacyTier.INTIMATE,
        parent_id=parent_id,
        structural_path=structural_path or [],
    )
    path = write_fragment_file(
        vault=vault,
        fragment=fragment,
        body=f"A private paragraph mentioning {SENTINEL} explicitly.",
    )
    if filename is not None:
        renamed = path.with_name(filename)
        path.rename(renamed)
        return renamed
    return path


@pytest.fixture
def leaky_vault(tmp_path: Path) -> Path:
    """A vault that trips every new-check rule, saturated with the sentinel.

    Anomalies planted: a dangling parent, a two-node cycle, a root carrying a
    breadcrumb, a duplicate id, a stray root file, a stray root directory and
    a zero-byte markdown file. Every fragment is ``INTIMATE`` and every title,
    body and breadcrumb entry contains :data:`SENTINEL`.
    """
    root = tmp_path / "vault"
    for name in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (root / name).mkdir(parents=True)

    _write_fragment(root, "frag-dangling", parent_id="frag-ghost")
    _write_fragment(root, "frag-cyc-a", parent_id="frag-cyc-b")
    _write_fragment(root, "frag-cyc-b", parent_id="frag-cyc-a")
    _write_fragment(
        root,
        "frag-deep",
        structural_path=[f"{SENTINEL} chapter heading", f"{SENTINEL} section"],
    )
    _write_fragment(root, "frag-dup", filename="frag-dup-shadow.md")
    _write_fragment(root, "frag-dup")

    (root / "stray-note.md").write_text(
        f"A note whose BODY mentions {SENTINEL}.\n",
        encoding="utf-8",
    )
    (root / "Scratch").mkdir()
    (root / "02-Threads" / "empty.md").touch()
    return root


def _all_text(result: CheckResult) -> str:
    """Return every operator-visible string a check emits, joined."""
    return "\n".join([result.name, result.summary, *result.findings])


class TestFindingsCarryNoContent:
    """The core assertion, per check, at finding granularity."""

    @pytest.mark.parametrize("check_name", NEW_CHECKS)
    def test_the_check_produces_findings_at_all(
        self,
        leaky_vault: Path,
        check_name: str,
    ) -> None:
        """The anti-vacuity guard, and it must run first.

        Every sentinel assertion below is an *absence* test, and an absence
        test over an empty list passes for the wrong reason. If this fails,
        the fixture stopped tripping the rules and none of the privacy
        assertions mean anything.
        """
        assert _run(check_name, leaky_vault).findings

    @pytest.mark.parametrize("check_name", NEW_CHECKS)
    def test_no_finding_quotes_body_title_or_breadcrumb(
        self,
        leaky_vault: Path,
        check_name: str,
    ) -> None:
        """The sentinel appears in no finding and in no summary.

        Catches the obvious mistake — printing ``fragment.title`` to make a
        finding friendlier — which quietly prints the first line of the body
        for every atomized fragment in the vault.
        """
        result = _run(check_name, leaky_vault)

        assert SENTINEL not in _all_text(result)

    @pytest.mark.parametrize("check_name", NEW_CHECKS)
    def test_no_finding_contains_an_absolute_path(
        self,
        leaky_vault: Path,
        check_name: str,
    ) -> None:
        """Vault-relative only — the #969 defect, restated for the new checks."""
        result = _run(check_name, leaky_vault)

        assert str(leaky_vault) not in _all_text(result)
        for finding in result.findings:
            assert not finding.lstrip("- `").startswith("/"), finding

    def test_ancestry_findings_name_vault_relative_paths_and_ids(
        self,
        leaky_vault: Path,
    ) -> None:
        """The positive half: silence is not the same as usefulness.

        A check that emitted only ``"4 anomalies"`` would pass every absence
        assertion above and still leave the operator exactly where #1277 found
        them. Findings must name the file and the content-hashed id.
        """
        result = _run("ancestry", leaky_vault)
        joined = "\n".join(result.findings)

        assert "01-Fragments/Notes/frag-dangling.md" in joined
        assert "frag-ghost" in joined

    def test_root_hygiene_findings_name_vault_relative_paths(
        self,
        leaky_vault: Path,
    ) -> None:
        """Same positive half for #883.

        The fixture deliberately keeps the sentinel out of every *filename*
        and confines it to file *contents*. Paths are the one thing these
        findings must print, so a sentinel in a filename would make the
        absence assertions unsatisfiable and force whoever hit them to weaken
        the privacy test rather than the fixture.
        """
        result = _run("root-hygiene", leaky_vault)
        joined = "\n".join(result.findings)

        assert "02-Threads/empty.md" in joined
        assert "stray-note.md" in joined
        assert "Scratch" in joined
        assert str(leaky_vault) not in joined


class TestTheRenderedReportCarriesNoContent:
    """The artifact that actually reaches disk, not just the dataclass."""

    def test_the_lint_report_markdown_is_clean(self, leaky_vault: Path) -> None:
        """``LintReport.render()`` over both new checks contains no sentinel."""
        report = LintRunner(leaky_vault).run(list(NEW_CHECKS))

        rendered = report.render()

        assert rendered.strip(), "empty report would make the assertion vacuous"
        assert SENTINEL not in rendered
        assert str(leaky_vault) not in rendered

    def test_the_written_report_file_is_clean(self, leaky_vault: Path) -> None:
        """The bytes on disk — what ``creek state`` later reads back verbatim."""
        runner = LintRunner(leaky_vault)

        target = runner.write(runner.run(list(NEW_CHECKS)))

        text = target.read_text(encoding="utf-8")
        assert SENTINEL not in text
        assert str(leaky_vault) not in text


class TestStateReportAtTheNarrowestAdmittingCeiling:
    """``creek state`` at ``ceiling=intimate`` — the strictest ceiling that shows it."""

    def test_the_lint_summary_is_actually_rendered_at_this_ceiling(
        self,
        leaky_vault: Path,
    ) -> None:
        """Anti-vacuity again: prove the section is present before proving it clean.

        At ``OPEN`` or ``PERSONAL`` the section collapses to a placeholder, so
        a sentinel assertion there would pass regardless of what the checks
        print.
        """
        runner = LintRunner(leaky_vault)
        runner.write(runner.run(list(NEW_CHECKS)))

        section = StateReportGenerator(
            leaky_vault,
            override=NARROWEST_ADMITTING_CEILING,
        ).section_lint_summary()

        assert "ancestry" in section
        assert "root-hygiene" in section

    def test_the_lint_summary_carries_no_fragment_content(
        self,
        leaky_vault: Path,
    ) -> None:
        """The whole point of Hazard 2, asserted where it actually bites."""
        runner = LintRunner(leaky_vault)
        runner.write(runner.run(list(NEW_CHECKS)))

        section = StateReportGenerator(
            leaky_vault,
            override=NARROWEST_ADMITTING_CEILING,
        ).section_lint_summary()

        assert SENTINEL not in section
        assert str(leaky_vault) not in section

    def test_a_default_full_run_is_equally_clean(self, leaky_vault: Path) -> None:
        """The new checks must not leak through the default invocation either.

        Repo history says this is the recurring shape of the defect: every
        test passes an explicit flag, the default invocation goes uncovered,
        and a doc asserts it unconditionally. Here the explicit flag is
        ``run(list(NEW_CHECKS))``; this test uses ``run()`` with nothing.
        """
        runner = LintRunner(leaky_vault)
        report = runner.run()
        runner.write(report)

        section = StateReportGenerator(
            leaky_vault,
            override=NARROWEST_ADMITTING_CEILING,
        ).section_lint_summary()

        assert {*NEW_CHECKS} <= {result.name for result in report.results}
        assert SENTINEL not in section


class TestReviewQueueExposureIsUnchanged:
    """#883's relocation must not put the queue on ``creek state``'s read path.

    The queue does print ``**{fragment.title}**`` for ``INTIMATE`` fragments
    into a file with no ``privacy_tier`` — the #1506 defect class, and a real
    follow-up. What this PR must not do is *worsen* it by moving that file
    into a directory ``creek state`` echoes verbatim.
    """

    def test_a_review_queue_is_not_read_back_into_the_state_report(
        self,
        leaky_vault: Path,
    ) -> None:
        """``latest_lint_report`` globs ``lint-*.md`` only, so the queue is invisible.

        Pins the exact reason the relocation is safe. If someone later widens
        that glob, this fails and says why.
        """
        from creek.classify.review import ReviewQueueGenerator

        queue = ReviewQueueGenerator().generate_queue([], leaky_vault)
        queue.write_text(
            f"# Classification Review Queue\n\n- [ ] **{SENTINEL}** (`frag-x`)\n",
            encoding="utf-8",
        )
        runner = LintRunner(leaky_vault)
        runner.write(runner.run(list(NEW_CHECKS)))

        section = StateReportGenerator(
            leaky_vault,
            override=NARROWEST_ADMITTING_CEILING,
        ).section_lint_summary()

        assert SENTINEL not in section
