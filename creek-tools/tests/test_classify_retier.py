"""Detector + narrow remediation for pre-#974 mis-stamped tiers (issue #1106).

#974 stopped ``creek process`` from stamping self-authored confessional
fragments ``personal``. It did nothing for the fragments already written
that way: ``privacy_tier`` is a one-way ratchet, so nothing revisits a
tier that is already concrete, and the only nag Creek has
(:func:`creek.cli._scan_fill_gaps`) counts *untiered* fragments — a
wrongly-``personal`` fragment is not untiered, so it was invisible.

Two halves are covered here.

**Visibility.** ``_scan_fill_gaps`` gains its own ``retierable`` count
and ``creek fill`` its own hint line. The count is deliberately
*disjoint* from ``untiered``: :func:`creek.classify.privacy_pass.needs_tier`
is ``True`` for an explicit ``unclassified``, so on the 35k-fragment demo
vault ``untiered`` already reads 35,330 and a retier count folded into it
could never be seen.

**Remediation.** ``creek classify --retier`` re-derives the tier for
exactly those fragments and writes it through the *existing* narrow
writer (``_backfill_preserved`` → ``_persist_tier_only`` →
``_write_tier_only``), not through ``--force``. That is what keeps
``classification_method`` provenance and manual operator curation intact
while the tier moves.

**The ratchet is the safety property.**
:func:`~creek.classify.privacy_pass.escalate` compares ``_ESCALATION_RANK``
and returns the more restrictive of the two candidates, so a predicate of
``escalate(tier_of(f), classify_tier(f, body)) is not tier_of(f)`` can
only ever fire when the recomputed tier is *stricter*. Both the detector
and the remediation are therefore raise-only by construction, and the
tests below pin that with fragments whose heuristic verdict is *weaker*
than what is on disk.

Every test carries an explicit positive control on the number of
fragments actually walked. The vault walk yields nothing unless
``metadata["type"] == "fragment"``, and a fixture that trips that
silently walks zero fragments while every assertion still passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
from typer.testing import CliRunner

import creek.cli as cli_mod
from creek.classify.classify_engine import run_classify
from creek.classify.privacy import PrivacyClassifier
from creek.classify.privacy_filter import tier_of
from creek.classify.privacy_pass import escalate, needs_retier
from creek.cli import app
from creek.config import CreekConfig
from creek.models import PrivacyTier
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


def _seed(
    vault: Path,
    frag_id: str,
    *,
    tier: str | None,
    platform: str = "journal",
    author: str = "self",
    method: str | None = None,
    body: str = "body\n",
) -> Path:
    """Write one fragment file from a literal frontmatter template.

    Literal, not ``Fragment.model_dump``, for two reasons. ``tier=None``
    has to produce a file whose ``privacy_tier`` key is genuinely
    **absent** (the legacy shape), which a dump cannot express; and the
    mis-stamped shape this issue is about — ``privacy_tier: personal``
    with ``voice_proxy_eligible: true`` on a self-authored journal
    fragment — is one the current model would never emit, so it has to be
    written by hand exactly as the pre-#974 pipeline left it.

    Args:
        vault: Vault root.
        frag_id: Fragment id, also the file stem.
        tier: Tier string to stamp, or ``None`` to omit the key.
        platform: ``source.platform`` value. ``journal`` + ``self`` is
            ``classify_tier``'s second INTIMATE trigger; ``essay`` is its
            OPEN branch, which is how a *weaker* verdict is staged.
        author: ``source.author`` value.
        method: ``classification_method`` to stamp, or ``None`` to omit.
        body: Markdown body, retained for byte-identity assertions.

    Returns:
        The path written.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = f"privacy_tier: {tier}\n" if tier else ""
    # The pre-#974 pipeline wrote the derived flag alongside the tier, so
    # a mis-stamped fragment carries a stale ``true`` here.
    eligible = ""
    if tier is not None:
        flag = "false" if tier == "intimate" else "true"
        eligible = f"voice_proxy_eligible: {flag}\n"
    method_line = f"classification_method: {method}\n" if method else ""
    path = folder / f"{frag_id}.md"
    path.write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "A note"\n'
        f"source:\n  platform: {platform}\n  author: {author}\n"
        f"{tier_line}{eligible}{method_line}---\n{body}",
        encoding="utf-8",
    )
    return path


def _walked(vault: Path) -> int:
    """Return how many fragments the shared vault walk actually yields.

    The mandatory positive control (#1106): ``try_load_fragment`` returns
    ``None`` for any note whose ``type`` is not ``fragment``, so a typo in
    the fixture makes the walk yield nothing and every count assertion
    below pass vacuously at zero.

    Args:
        vault: Vault root.

    Returns:
        The number of loadable fragments under ``01-Fragments``.
    """
    return len(iter_vault_fragments(vault / "01-Fragments"))


# ---- the predicate -------------------------------------------------------


def test_predicate_is_exactly_the_escalate_over_the_two_existing_readers(
    tmp_path: Path,
) -> None:
    """``needs_retier`` is built from ``tier_of`` + ``classify_tier``, nothing new.

    #1079 exists because two tier readers disagreed, so this pins the new
    predicate to the five that already exist rather than letting it grow
    into a sixth opinion: for every tiered fragment its answer must equal
    ``escalate(tier_of(f), classify_tier(f, body)) is not tier_of(f)``,
    computed here from the public helpers directly.
    """
    vault = tmp_path / "vault"
    _seed(vault, "frag-mis", tier="personal")
    _seed(vault, "frag-ok", tier="intimate")
    _seed(vault, "frag-weaker", tier="intimate", platform="essay")
    _seed(vault, "frag-open", tier="open", platform="essay")

    walked = iter_vault_fragments(vault / "01-Fragments")
    assert len(walked) == 4, "positive control: the walk must see every fixture"

    classifier = PrivacyClassifier()
    for _path, fragment, body, raw in walked:
        current = tier_of(fragment)
        candidate = classifier.classify_tier(fragment, content=body)
        expected = escalate(current, candidate) is not current
        assert (
            needs_retier(fragment, body, raw=raw, classifier=classifier) is expected
        ), fragment.id


def test_predicate_declines_an_untiered_fragment(tmp_path: Path) -> None:
    """An untiered fragment belongs to the #876 count, not this one.

    ``tier_of`` reports ``unclassified`` for both untiered shapes, and
    ``_ESCALATION_RANK`` puts that *below* ``open``, so the bare escalate
    predicate is ``True`` for every untiered fragment in the vault.
    Folding those in would make ``retierable`` a near-copy of ``untiered``
    (35,330 on the demo vault) and hide the population this issue is
    about, so ``needs_retier`` excludes them explicitly.
    """
    vault = tmp_path / "vault"
    _seed(vault, "frag-absent", tier=None)
    _seed(vault, "frag-unclassified", tier="unclassified")

    walked = iter_vault_fragments(vault / "01-Fragments")
    assert len(walked) == 2, "positive control: the walk must see every fixture"

    classifier = PrivacyClassifier()
    for _path, fragment, body, raw in walked:
        # The bare escalate would fire on both — that is the trap.
        current = tier_of(fragment)
        candidate = classifier.classify_tier(fragment, content=body)
        assert escalate(current, candidate) is not current
        assert not needs_retier(fragment, body, raw=raw, classifier=classifier)


def test_predicate_declines_when_the_heuristic_is_weaker(tmp_path: Path) -> None:
    """The ratchet: a *weaker* recomputed tier is never a retier candidate.

    A self-authored essay classifies ``open``; on disk it says
    ``intimate``. ``escalate`` returns the more restrictive of the two, so
    the predicate is ``False`` and nothing downstream is ever handed a
    licence to lower the tier. A mutant that swapped ``escalate`` for
    "differs from disk" reddens right here.
    """
    vault = tmp_path / "vault"
    _seed(vault, "frag-weaker", tier="intimate", platform="essay")

    walked = iter_vault_fragments(vault / "01-Fragments")
    assert len(walked) == 1, "positive control: the walk must see every fixture"
    _path, fragment, body, raw = walked[0]

    classifier = PrivacyClassifier()
    assert classifier.classify_tier(fragment, content=body) is PrivacyTier.OPEN
    assert tier_of(fragment) is PrivacyTier.INTIMATE
    assert not needs_retier(fragment, body, raw=raw, classifier=classifier)


# ---- the detector: a separate count and a separate hint ------------------


def test_scan_counts_retierable_separately_from_untiered(tmp_path: Path) -> None:
    """The two gap counts are reported on their own fields, disjointly.

    The vault holds one of each interesting shape. ``untiered`` must see
    only the keyless fragment; ``retierable`` must see only the
    mis-stamped one; the already-correct and the weaker-verdict fragments
    must land in neither.
    """
    vault = tmp_path / "vault"
    _seed(vault, "frag-mis", tier="personal", method="llm")
    _seed(vault, "frag-keyless", tier=None)
    _seed(vault, "frag-ok", tier="intimate")
    _seed(vault, "frag-weaker", tier="intimate", platform="essay")

    assert _walked(vault) == 4, "positive control: the walk must see every fixture"
    scan = cli_mod._scan_fill_gaps(vault)

    assert scan.untiered == 1
    assert scan.retierable == 1
    assert cli_mod._count_retierable_fragments(vault) == 1


def test_count_retierable_is_zero_without_a_fragments_dir(tmp_path: Path) -> None:
    """A vault with no ``01-Fragments`` counts zero rather than exploding."""
    assert cli_mod._count_retierable_fragments(tmp_path / "empty-vault") == 0


def test_fill_hints_the_retierable_count_on_its_own_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``creek fill`` reports the two populations on two distinct lines.

    Three mis-stamped fragments against seven untiered ones, so neither
    number can stand in for the other by coincidence. The hint names the
    remediation command, and — per the config-oracle rule (#846/#848) —
    reports a count only, never a fragment id.
    """
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    for index in range(3):
        _seed(vault, f"frag-mis-{index}", tier="personal", method="llm")
    for index in range(7):
        _seed(vault, f"frag-keyless-{index}", tier=None)

    assert _walked(vault) == 10, "positive control: the walk must see every fixture"
    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    out = capsys.readouterr().out
    retier_lines = [line for line in out.splitlines() if "--retier" in line]
    assert len(retier_lines) == 1, out
    assert "3" in retier_lines[0]
    untiered_lines = [line for line in out.splitlines() if "untiered" in line]
    assert len(untiered_lines) == 1, out
    assert "7" in untiered_lines[0]
    assert "--retier" not in untiered_lines[0]
    for line in retier_lines:
        assert "frag-mis" not in line


def test_fill_is_silent_when_nothing_is_retierable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A vault with no mis-stamped fragment gets no retier nag."""
    from creek.cli import _maybe_upgrade_classification

    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)
    vault = tmp_path / "vault"
    _seed(vault, "frag-ok", tier="intimate")
    _seed(vault, "frag-weaker", tier="intimate", platform="essay")

    assert _walked(vault) == 2, "positive control: the walk must see every fixture"
    _maybe_upgrade_classification(
        vault,
        cli_mod._load_config_for_vault(vault),
        upgrade=False,
    )

    assert "--retier" not in capsys.readouterr().out


def test_retier_hint_failure_never_crashes_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken retier count is swallowed by its own best-effort guard.

    Its own guard, not the untiered hint's: sharing one ``try`` would let
    a single failing count silently suppress the other hint too.
    """
    from creek.cli import _maybe_upgrade_classification

    def _boom(*_a: object) -> int:
        raise OSError("unreadable fragment")

    monkeypatch.setattr(cli_mod, "_count_retierable_fragments", _boom)
    monkeypatch.setattr(cli_mod, "_detect_classify_upgrade", lambda *_a: None)

    # Must not raise.
    _maybe_upgrade_classification(
        tmp_path, cli_mod._load_config_for_vault(tmp_path), upgrade=False
    )


# ---- the remediation -----------------------------------------------------


def _run(vault: Path, *, retier: bool, force: bool = False) -> object:
    """Run the rules-path classify engine over *vault*.

    Args:
        vault: Vault root.
        retier: The new ``--retier`` flag.
        force: The pre-existing ``--force`` flag.

    Returns:
        The run's :class:`~creek.classify.classify_engine.ClassifySummary`.
    """
    return run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=force,
        retier=retier,
    )


def test_without_retier_a_mis_stamped_fragment_is_untouched(tmp_path: Path) -> None:
    """The default path is unchanged — this is the measured RED, pinned.

    A bare ``creek classify`` leaves a preserved fragment's tier exactly
    where it is, which is both the bug this issue reports and the
    operator-override property #1105 deliberately preserved. The new flag
    must not become the default.
    """
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-mis", tier="personal", method="manual")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=False)
    assert summary.total == 1, "positive control: the run must visit the fixture"

    after = frontmatter.load(path)
    assert after.metadata["privacy_tier"] == "personal"
    assert after.metadata["voice_proxy_eligible"] is True
    assert summary.retiered == 0


def test_retier_raises_the_tier_and_the_derived_flag_on_disk(tmp_path: Path) -> None:
    """``--retier`` fixes tier *and* ``voice_proxy_eligible``, asserted on disk.

    The stale ``voice_proxy_eligible: true`` is the half that actually
    feeds voice-proxy generation, so it is read back off the file rather
    than off a return value — the frontmatter is what every later reader
    sees.
    """
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-mis", tier="personal", method="manual")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 1, "positive control: the run must visit the fixture"

    after = frontmatter.load(path)
    assert after.metadata["privacy_tier"] == "intimate"
    assert after.metadata["voice_proxy_eligible"] is False
    assert summary.retiered == 1


def test_retier_preserves_provenance_and_manual_curation(tmp_path: Path) -> None:
    """Provenance and curation survive the retier — that is why it is narrow.

    ``--method rules --force`` would fix the tier too, but it re-stamps
    ``classification_method: llm → rules`` and bypasses the preserved
    short-circuit that honours ``manual``. Driving the retier through the
    existing tier-only writer instead leaves every other key, and the
    body's bytes, exactly as they were.
    """
    vault = tmp_path / "vault"
    manual = _seed(
        vault,
        "frag-manual",
        tier="personal",
        method="manual",
        body="a curated body\n",
    )
    llm = _seed(vault, "frag-llm", tier="personal", method="llm")
    before_manual = manual.read_text(encoding="utf-8")

    assert _walked(vault) == 2, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 2, "positive control: the run must visit both fixtures"
    assert summary.preserved_manual == 1
    assert summary.preserved_llm == 1
    assert summary.retiered == 2

    for path, expected_method in ((manual, "manual"), (llm, "llm")):
        after = frontmatter.load(path)
        assert after.metadata["classification_method"] == expected_method
        assert after.metadata["privacy_tier"] == "intimate"
        assert after.metadata["voice_proxy_eligible"] is False
    # The tier-only writer must not disturb the body.
    assert frontmatter.load(manual).content == "a curated body"
    assert before_manual != manual.read_text(encoding="utf-8")

    # Exactly two keys may move, and this is asserted as a *set difference*
    # rather than key by key: the docs promise "only ``privacy_tier`` and the
    # derived ``voice_proxy_eligible`` change", and a per-key spot check would
    # keep passing if a later edit widened the writer with a third field.
    original = frontmatter.loads(before_manual)
    now = frontmatter.load(manual)
    moved = {
        key
        for key in set(original.metadata) | set(now.metadata)
        if original.metadata.get(key) != now.metadata.get(key)
    }
    assert moved == {"privacy_tier", "voice_proxy_eligible"}


def test_retier_never_lowers_a_tier(tmp_path: Path) -> None:
    """The ratchet, end to end: a weaker verdict changes nothing on disk.

    A self-authored essay stamped ``intimate`` recomputes to ``open``. The
    file must come back byte-identical — a mutant that wrote the
    recomputed tier instead of the escalated one would bury the fragment's
    protection here, and ``privacy_tier`` has no way back.
    """
    vault = tmp_path / "vault"
    path = _seed(
        vault, "frag-weaker", tier="intimate", platform="essay", method="manual"
    )
    before = path.read_text(encoding="utf-8")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 1, "positive control: the run must visit the fixture"

    # Tier first, bytes second: a mutant that genuinely *lowers* the tier
    # must redden on the tier, not merely on an incidental rewrite.
    after = frontmatter.load(path)
    assert after.metadata["privacy_tier"] == "intimate"
    assert after.metadata["voice_proxy_eligible"] is False
    assert summary.retiered == 0
    assert path.read_text(encoding="utf-8") == before


def test_retier_leaves_an_already_correct_fragment_alone(tmp_path: Path) -> None:
    """An already-``intimate`` journal fragment is not rewritten.

    The pass has to be idempotent down to the file's bytes, or the second
    run of a 35k-fragment vault rewrites every file for nothing.
    """
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-ok", tier="intimate", method="llm")
    before = path.read_text(encoding="utf-8")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 1, "positive control: the run must visit the fixture"

    assert path.read_text(encoding="utf-8") == before
    assert summary.retiered == 0


def test_retier_is_idempotent(tmp_path: Path) -> None:
    """A second ``--retier`` run over the same vault reports and writes nothing."""
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-mis", tier="personal", method="llm")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    first = _run(vault, retier=True)
    assert first.retiered == 1
    settled = path.read_text(encoding="utf-8")

    second = _run(vault, retier=True)
    assert second.total == 1, "positive control: the run must visit the fixture"
    assert second.retiered == 0
    assert path.read_text(encoding="utf-8") == settled


def test_retier_does_not_claim_untiered_fragments(tmp_path: Path) -> None:
    """An untiered fragment is tiered by the #876 pass, not counted as retiered.

    Same disjointness the detector enforces, on the write side: the
    ``retiered`` counter must report the population this issue added, not
    re-report work ``privacy_tiers_assigned`` already covers.
    """
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-keyless", tier=None, method="llm")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 1, "positive control: the run must visit the fixture"

    assert frontmatter.load(path).metadata["privacy_tier"] == "intimate"
    assert summary.privacy_tiers_assigned == 1
    assert summary.retiered == 0


def test_retier_flag_is_wired_through_the_cli(tmp_path: Path) -> None:
    """``creek classify --retier`` remediates and reports the count."""
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-mis", tier="personal", method="llm")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    result = runner.invoke(app, ["classify", "--vault", str(vault), "--retier"])

    assert result.exit_code == 0, result.output
    assert frontmatter.load(path).metadata["privacy_tier"] == "intimate"
    assert "re-tiered" in result.output


def test_classify_without_the_flag_reports_zero_retiered(tmp_path: Path) -> None:
    """The summary line is always present, so ``0`` is a readable answer."""
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-mis", tier="personal", method="llm")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    result = runner.invoke(app, ["classify", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert frontmatter.load(path).metadata["privacy_tier"] == "personal"
    assert "0 re-tiered" in result.output


def test_retier_also_remediates_a_fragment_the_short_circuit_does_not_preserve(
    tmp_path: Path,
) -> None:
    """The other engine path: a ``rules``-stamped fragment is re-tiered too.

    ``_record_if_preserved`` short-circuits only ``manual`` and ``llm``
    fragments, so a ``rules``-stamped one runs the full classification
    path and is written by ``_finalise_fragment_write``, not by the narrow
    tier-only writer. The flag has to land on **both** paths or the
    detector would keep reporting a fragment the remediation silently
    walked past — a fix correct on one path and wrong on the other.
    """
    vault = tmp_path / "vault"
    path = _seed(vault, "frag-rules", tier="personal", method="rules")

    assert _walked(vault) == 1, "positive control: the walk must see every fixture"
    summary = _run(vault, retier=True)
    assert summary.total == 1, "positive control: the run must visit the fixture"
    assert summary.preserved_manual == 0
    assert summary.preserved_llm == 0

    after = frontmatter.load(path)
    assert after.metadata["privacy_tier"] == "intimate"
    assert after.metadata["voice_proxy_eligible"] is False
    assert summary.retiered == 1


def test_the_reported_count_equals_what_the_remediation_actually_moves(
    tmp_path: Path,
) -> None:
    """Detector and remediation must never diverge — pinned, not assumed.

    They are two call sites of the same predicate
    (:func:`~creek.classify.privacy_pass.needs_retier` in the scanner,
    :func:`~creek.classify.privacy_pass.outranks_recorded_tier` over the
    cached baseline in the engine), which is exactly the shape that drifts
    later. A hint that promises "N fragments" and a command that moves a
    different number is the whole visibility failure #1106 reports, in a
    new place. Every fragment shape from this module appears here so the
    equality cannot hold by coincidence.
    """
    vault = tmp_path / "vault"
    _seed(vault, "frag-mis-manual", tier="personal", method="manual")
    _seed(vault, "frag-mis-llm", tier="personal", method="llm")
    _seed(vault, "frag-mis-rules", tier="personal", method="rules")
    _seed(vault, "frag-open-mis", tier="open", method="llm")
    _seed(vault, "frag-ok", tier="intimate", method="llm")
    _seed(vault, "frag-weaker", tier="intimate", platform="essay", method="llm")
    _seed(vault, "frag-keyless", tier=None, method="llm")
    _seed(vault, "frag-unclassified", tier="unclassified", method="llm")

    assert _walked(vault) == 8, "positive control: the walk must see every fixture"
    detected = cli_mod._scan_fill_gaps(vault)
    assert detected.retierable == 4
    assert detected.untiered == 2

    summary = _run(vault, retier=True)
    assert summary.total == 8, "positive control: the run must visit every fixture"
    assert summary.retiered == detected.retierable

    # And the vault is now clean by the detector's own reckoning.
    assert cli_mod._scan_fill_gaps(vault).retierable == 0
