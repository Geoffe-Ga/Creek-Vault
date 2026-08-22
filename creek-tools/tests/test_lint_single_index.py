"""One :class:`LinkIndex` per ``creek lint`` run, built lazily (#1223).

``broken-links`` reaches ``build_link_index`` through
``creek.clean.hygiene.BrokenLinkScanner.scan`` and ``orphan-compiled`` calls
it directly, so a default run builds the index twice and header-parses every
``*.md`` in the vault twice.

**Why this module does not patch ``frontmatter.load``.** The obvious
instrumentation — count ``frontmatter.load`` calls per file and assert one —
is *vacuous* here: ``build_link_index`` never calls it. ``creek/vault/links.py``
reads header-only through ``_read_header_block`` (a ``path.open`` plus a
readline loop) and ``yaml.safe_load``. ``frontmatter.load`` is the parser for
``compile_routing._load_pages`` and ``OrphanScanner._is_old_enough``, so a
``frontmatter.load`` spy counts walks this issue is not about and passes green
against the unfixed code. The correct probe is
``creek.vault.links.read_header_meta``, which ``_header_names`` resolves
through the module global — so patching it there does take effect.

**Why the build counter is installed on three separate names.**
``build_link_index`` is imported *by name* into ``creek.clean.hygiene`` and
``creek.lint.checks.orphan_compiled`` (and, after the fix, into
``creek.lint.runner``). Patching ``creek.vault.links.build_link_index`` alone
rebinds nothing those modules look at, and the test would pass without
measuring anything.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest

from creek.clean import hygiene as hygiene_module
from creek.clean.hygiene import BrokenLinkScanner, OrphanScanner
from creek.lint import runner as runner_module
from creek.lint.checks import broken_links, orphan_compiled
from creek.lint.checks import orphan_compiled as orphan_module
from creek.lint.runner import LintRunner
from creek.vault import links as links_module

if TYPE_CHECKING:
    from pathlib import Path

    from creek.vault.links import LinkIndex

_BUILD_SITES: tuple[str, ...] = (
    "creek.lint.runner",
    "creek.clean.hygiene",
    "creek.lint.checks.orphan_compiled",
)
"""Every module holding its own ``build_link_index`` binding.

After #1223 the runner owns the single build and the other two must fall to
zero on a lint run — while staying callable standalone.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A small vault with **no compiled directories**.

    Deliberate: ``orphan_compiled._is_generated_index`` calls
    ``read_header_meta`` for every candidate page under a compiled directory,
    which would contaminate the per-file parse counter in
    :class:`TestEachHeaderIsParsedOncePerRun`. With no compiled dirs the only
    ``read_header_meta`` caller in the run is the index build itself, so the
    count is unambiguous.
    """
    root = tmp_path / "vault"
    fragments = root / "01-Fragments"
    fragments.mkdir(parents=True)
    (fragments / "frag-a.md").write_text(
        "---\ntitle: Alpha\naliases:\n  - Alpha Alias\n---\n\nSee [[frag-b]].\n",
        encoding="utf-8",
    )
    (fragments / "frag-b.md").write_text(
        "---\ntitle: Beta\n---\n\nDangling [[nowhere-at-all]].\n",
        encoding="utf-8",
    )
    (fragments / "frag-c.md").write_text(
        "---\ntitle: Gamma\n---\n\nSee [[Alpha Alias]].\n",
        encoding="utf-8",
    )
    return root


def _install_build_counters(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Wrap ``build_link_index`` at every module that binds it.

    Returns:
        A counter keyed by module name, incremented once per build.
    """
    counts: Counter[str] = Counter()
    real = links_module.build_link_index

    for module_name in _BUILD_SITES:
        module = __import__(module_name, fromlist=["build_link_index"])

        def _shim(
            vault_path: Path,
            *,
            _module_name: str = module_name,
        ) -> LinkIndex:
            counts[_module_name] += 1
            return real(vault_path)

        monkeypatch.setattr(module, "build_link_index", _shim)
    return counts


class TestTheRunBuildsOneIndex:
    """A default ``creek lint`` run builds the index exactly once."""

    def test_a_default_run_builds_the_index_once(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Total builds across all three binding sites is 1, and the runner owns it.

        Pre-fix this reads ``{"creek.clean.hygiene": 1,
        "creek.lint.checks.orphan_compiled": 1}`` — two builds, neither from
        the runner.
        """
        counts = _install_build_counters(monkeypatch)

        LintRunner(vault).run(list(runner_module.DETERMINISTIC_CHECKS))

        assert sum(counts.values()) == 1
        assert counts["creek.lint.runner"] == 1
        assert counts["creek.clean.hygiene"] == 0
        assert counts["creek.lint.checks.orphan_compiled"] == 0

    def test_running_only_one_threaded_check_still_builds_once(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One threaded check needs one index, not zero and not two."""
        counts = _install_build_counters(monkeypatch)

        LintRunner(vault).run(["broken-links"])

        assert sum(counts.values()) == 1

    def test_a_run_with_no_threaded_check_builds_nothing(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``creek lint --check tags`` must not pay for an index it never reads.

        Without this the lazy build is untested and an eager one — built in
        ``LintRunner.__init__`` or before the loop — passes every other
        assertion in this module while making the cheapest invocation walk the
        whole vault.
        """
        counts = _install_build_counters(monkeypatch)

        LintRunner(vault).run(["tags"])

        assert sum(counts.values()) == 0


class TestEachHeaderIsParsedOncePerRun:
    """The per-file probe: no page's frontmatter is read twice."""

    def test_every_page_header_is_parsed_exactly_once(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The real anti-duplication guard, independent of the build counter.

        A "fix" that shares one index object but rebuilds its contents, or
        that memoises at the wrong layer, would satisfy the build count and
        fail here. Pre-fix every file is parsed twice.
        """
        calls: Counter[Path] = Counter()
        real = links_module.read_header_meta

        def _spy(path: Path) -> dict[str, object]:
            calls[path] += 1
            return real(path)

        monkeypatch.setattr(links_module, "read_header_meta", _spy)

        LintRunner(vault).run(["broken-links", "orphan-compiled"])

        pages = sorted(vault.rglob("*.md"))
        assert pages, "fixture produced no pages; the assertion below would be vacuous"
        assert dict(calls) == dict.fromkeys(pages, 1)


class TestStandaloneCallabilityIsPreserved:
    """Both scanners and both checks must keep working with no index supplied.

    Not hypothetical: ``HygieneReporter`` drives both scanners for
    ``creek clean``, and tests construct all four directly. The injected index
    is an optional keyword, never a required one.
    """

    def test_broken_link_scanner_builds_its_own_index(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``BrokenLinkScanner().scan(vault)`` still builds exactly one index."""
        counts = _install_build_counters(monkeypatch)

        result = BrokenLinkScanner().scan(vault)

        assert counts["creek.clean.hygiene"] == 1
        assert result.total_broken == 1

    def test_orphan_scanner_builds_its_own_index(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``OrphanScanner().scan(vault)`` still builds exactly one index."""
        counts = _install_build_counters(monkeypatch)

        OrphanScanner().scan(vault)

        assert counts["creek.clean.hygiene"] == 1

    def test_broken_links_check_accepts_an_injected_index(
        self,
        vault: Path,
    ) -> None:
        """Passing an index skips the build and yields identical findings."""
        index = links_module.build_link_index(vault)

        injected = broken_links.run(vault, link_index=index)
        standalone = broken_links.run(vault)

        assert injected.findings == standalone.findings
        assert injected.summary == standalone.summary

    def test_orphan_compiled_check_accepts_an_injected_index(
        self,
        vault: Path,
    ) -> None:
        """Same contract on the second threaded check."""
        index = links_module.build_link_index(vault)

        injected = orphan_compiled.run(vault, link_index=index)
        standalone = orphan_compiled.run(vault)

        assert injected.findings == standalone.findings
        assert injected.summary == standalone.summary

    def test_an_injected_index_is_actually_used_not_ignored(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Injecting an index must suppress the build, not merely be tolerated.

        A signature that accepts ``link_index`` and then rebuilds anyway would
        pass every other test here while delivering none of #1223's saving.
        """
        index = links_module.build_link_index(vault)
        counts = _install_build_counters(monkeypatch)

        broken_links.run(vault, link_index=index)
        orphan_compiled.run(vault, link_index=index)

        assert sum(counts.values()) == 0

    def test_explicit_none_falls_back_to_building_one(self, vault: Path) -> None:
        """``link_index=None`` means "build your own", not "skip the check".

        Pins the ``is not None`` test the scanners must use. ``LinkIndex`` is a
        frozen dataclass with no ``__bool__`` or ``__len__``, so
        ``link_index or build_link_index(...)`` happens to work today — and
        would start silently rebuilding the moment someone adds ``__len__``
        and an empty vault produces a falsy index.
        """
        assert broken_links.run(vault, link_index=None).findings == (
            broken_links.run(vault).findings
        )


class TestCleanSharesOneIndexToo:
    """``creek clean`` carries the same defect, unticketed (hygiene.py:756-758).

    ``HygieneReporter.generate`` calls ``OrphanScanner.scan`` and
    ``BrokenLinkScanner.scan``, each of which builds its own index. The
    CLAUDE.md rule is to sweep the pattern, not the sample.
    """

    def test_a_hygiene_report_builds_the_index_once(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One build for the whole report, not one per scanner."""
        counts = _install_build_counters(monkeypatch)

        hygiene_module.HygieneReporter().generate(vault)

        assert sum(counts.values()) == 1


class TestRegistryDrift:
    """The index-aware table and the check registry must not diverge.

    Without this, a future rename registers two different callables under one
    name and half the runner silently calls the stale one — which looks like a
    passing suite and a check that stopped running.
    """

    def test_every_index_aware_name_is_a_registered_check(self) -> None:
        """``_INDEX_AWARE`` is a subset of ``_REGISTRY``."""
        index_aware: dict[str, Any] = runner_module._INDEX_AWARE
        registry: dict[str, Any] = runner_module._REGISTRY

        assert set(index_aware) <= set(registry)

    def test_index_aware_entries_are_the_same_objects_as_the_registry(self) -> None:
        """Identity, not equality: two equal-looking callables is the failure."""
        index_aware: dict[str, Any] = runner_module._INDEX_AWARE
        registry: dict[str, Any] = runner_module._REGISTRY

        for name, callable_ in index_aware.items():
            assert callable_ is registry[name], name

    def test_both_threaded_checks_are_declared_index_aware(self) -> None:
        """A check that builds an index but is not declared stays duplicated."""
        assert set(runner_module._INDEX_AWARE) == {"broken-links", "orphan-compiled"}


class TestKnownRemainingDuplication:
    """``iter_link_sources`` is still called twice per run — a recorded decision.

    ``hygiene.py:547`` and ``orphan_compiled.py:176`` both enumerate the
    source set, so a default run does four ``rglob`` walks and #1223 removes
    two. Threading the source list is the identical argument with the
    identical safety profile, but #1223's acceptance criterion names the
    index. This test states the residue as a fact so the follow-up is
    discoverable rather than forgotten.
    """

    def test_the_source_enumeration_is_still_duplicated(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Documents the remaining two walks; update it when they are threaded."""
        counts: Counter[str] = Counter()
        real = links_module.iter_link_sources

        for module_name, module in (
            ("hygiene", hygiene_module),
            ("orphan_compiled", orphan_module),
        ):

            def _shim(
                vault_path: Path,
                *,
                _module_name: str = module_name,
            ) -> list[Path]:
                counts[_module_name] += 1
                return real(vault_path)

            monkeypatch.setattr(module, "iter_link_sources", _shim)

        LintRunner(vault).run(["broken-links", "orphan-compiled"])

        assert sum(counts.values()) == 2
