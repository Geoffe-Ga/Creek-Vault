"""The vault's ``voice_audience_weighting`` reaches the exemplar path (#1313).

The config section shipped with epic #632/#633/#634 and is honoured by the
voice *fingerprint*, but every construction on the *exemplar* path
(``VoiceProfileGenerator``, ``VoiceExemplarCollector``,
``generate_register_samples``) substituted a fresh
:class:`~creek.config.VoiceAudienceWeightingConfig`, so the vault's setting
was inert: ranking applied the 1.5x ``open`` multiplier even with
``enabled: false`` on disk.

Every test here runs from a **config-less working directory with
``CREEK_CONFIG`` unset**. That is not incidental — it is the half of the
suite that separates a real fix from one wired to the bare process-wide
``load_config()``, which resolves ``creek_config.yaml`` against the *current
directory* (``creek/config.py``) and never looks inside the vault. A fix
that satisfies the structural guard while sourcing config from the cwd stays
inert for ``creek report --type voice --vault X`` and would pass a suite that
set ``CREEK_CONFIG``.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from typer.testing import CliRunner

from creek.cli import app
from creek.config import (
    CreekConfig,
    VoiceAudienceWeightingConfig,
    generate_default_config,
)
from creek.generate.voice import DEFAULT_MAX_PER_REGISTER
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.report import report_tool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

runner = CliRunner()

_SAMPLES_SUBPATH = ("07-Voice", "Register-Samples")


def _voice_body(marker: str) -> str:
    """Return a body of ~300 whitespace tokens, inside the length-bonus band.

    ``VoiceExemplarCollector.rank_exemplars`` awards its length bonus for a
    word count in ``[200, 800]``, so every fixture fragment must land there
    for the two candidates to share an identical quality base.
    """
    return f"{marker} " + ("the creek of thought flows gently downstream " * 33)


def _write_voice_fragment(
    vault: Path,
    frag_id: str,
    *,
    tier: str = "personal",
    register: str = "confessional",
    confidence: str = "conviction",
    representativeness: str | None = None,
    body: str | None = None,
) -> Path:
    """Write one exemplar-eligible fragment with hand-built frontmatter.

    A local clone of ``tests/test_cli.py``'s helper rather than an import:
    private test helpers are not shared across modules in this suite (the
    house rule stated at ``tests/test_mcp_report_tier_ceiling.py``).

    ``title`` is set equal to *frag_id* so the ``_Summary.md`` wikilink is the
    unambiguous ``- [[<id>|<id>]]``, which is what the ordering assertions
    read.

    Args:
        vault: Vault root.
        frag_id: Fragment id, file stem and title.
        tier: ``privacy_tier`` value.
        register: ``voice.voice_register`` value.
        confidence: ``voice.confidence`` value.
        representativeness: Optional ``representativeness`` value; omitted
            from the frontmatter entirely when ``None``.
        body: Markdown body; defaults to a length-bonus-eligible body.

    Returns:
        Path of the written fragment file.
    """
    folder = vault / "01-Fragments" / "Journal"
    folder.mkdir(parents=True, exist_ok=True)
    rep_line = (
        f"representativeness: {representativeness}\n"
        if representativeness is not None
        else ""
    )
    target = folder / f"{frag_id}.md"
    target.write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{frag_id}"\n'
        f"source:\n  platform: journal\n  author: self\n"
        f"frequency:\n  primary: F5\n"
        f"wavelength:\n  phase: rising\n  mode: express\n"
        f"voice:\n  voice_register: {register}\n  confidence: {confidence}\n"
        f"privacy_tier: {tier}\n{rep_line}---\n"
        f"{body if body is not None else _voice_body(frag_id)}\n",
        encoding="utf-8",
    )
    return target


def _write_vault_config(
    vault: Path,
    *,
    enabled: bool,
    privacy_tier_authority: dict[str, float] | None = None,
) -> Path:
    """Seed ``<vault>/00-Creek-Meta/creek_config.yaml`` the way ``creek init`` does.

    Dumps the full :class:`~creek.config.CreekConfig` default model — the exact
    block ``generate_default_config`` writes into every scaffolded vault — with
    only the audience-weighting knob edited. Using the full dump rather than a
    minimal fragment is deliberate: it is the shape real vaults carry, and it
    proves the fix reads a realistic file rather than a hand-trimmed one.

    Args:
        vault: Vault root.
        enabled: Value for ``voice_audience_weighting.enabled``.
        privacy_tier_authority: Optional replacement authority map, for the
            custom-multiplier cases.

    Returns:
        Path of the written config file.
    """
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    weighting: dict[str, Any] = data["voice_audience_weighting"]
    weighting["enabled"] = enabled
    if privacy_tier_authority is not None:
        weighting["privacy_tier_authority"] = privacy_tier_authority
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    target = meta / "creek_config.yaml"
    target.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return target


def _weighted_vault(
    tmp_path: Path,
    *,
    enabled: bool,
    name: str = "vault",
    privacy_tier_authority: dict[str, float] | None = None,
) -> Path:
    """Build a scaffolded vault whose own config sets the weighting knob.

    Args:
        tmp_path: Pytest temporary directory.
        enabled: Value for ``voice_audience_weighting.enabled``.
        name: Directory name, so one test can build two vaults.
        privacy_tier_authority: Optional replacement authority map.

    Returns:
        The vault root.
    """
    vault = tmp_path / name
    (vault / "01-Fragments" / "Journal").mkdir(parents=True, exist_ok=True)
    _write_vault_config(
        vault,
        enabled=enabled,
        privacy_tier_authority=privacy_tier_authority,
    )
    return vault


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from a directory holding no ``creek_config.yaml``, ``CREEK_CONFIG`` unset.

    This is the discipline that makes every assertion in this module
    load-bearing: with a cwd config in reach, a fix wired to the bare
    process-wide ``load_config()`` would pass while leaving
    ``creek report --vault X`` inert.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Pytest temporary directory.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir(exist_ok=True)
    monkeypatch.chdir(nowhere)


def _summary_links(vault: Path, register: str = "confessional") -> list[str]:
    """Return the wikilink lines of a register's ``_Summary.md``, in order."""
    summary = vault.joinpath(*_SAMPLES_SUBPATH, register, "_Summary.md")
    return [
        line.strip()
        for line in summary.read_text(encoding="utf-8").splitlines()
        if line.startswith("- [[")
    ]


def _run_report(vault: Path, report_type: str, *args: str) -> None:
    """Invoke ``creek report --type <report_type> --vault <vault>``; assert exit 0."""
    result = runner.invoke(
        app,
        ["report", "--type", report_type, "--vault", str(vault), *args],
    )
    assert result.exit_code == 0, result.output


def test_report_voice_ranks_unweighted_when_vault_config_disables_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled: false`` in the VAULT's config yields an unweighted ranking.

    Two candidates with an identical quality base (conviction 3 + length 1 +
    fully-classified 1 = 5) differ only in ``privacy_tier``. With the
    weighting disabled both stay at 5 and ``rank_exemplars`` breaks the tie on
    ascending id; with it enabled ``b-open`` takes the 1.5x ``open``
    multiplier and leads.

    Before #1313 the collector substituted a fresh config at construction, so
    ``b-open`` led regardless of what the vault's file said.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(tmp_path, enabled=False)
    _write_voice_fragment(vault, "a-personal", tier="personal")
    _write_voice_fragment(vault, "b-open", tier="open")

    _run_report(vault, "voice")

    assert _summary_links(vault) == [
        "- [[a-personal|a-personal]]",
        "- [[b-open|b-open]]",
    ]


def test_report_voice_ranks_weighted_when_vault_config_enables_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enabled half of the two-vault diff: ``b-open`` takes the 1.5x lead.

    Paired with the disabled case above, this is what makes the pair a
    *diff* rather than a single assertion: the two vaults differ in exactly
    one config value and produce opposite orderings, so neither result can be
    explained by the fixture.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(tmp_path, enabled=True)
    _write_voice_fragment(vault, "a-personal", tier="personal")
    _write_voice_fragment(vault, "b-open", tier="open")

    _run_report(vault, "voice")

    assert _summary_links(vault) == [
        "- [[b-open|b-open]]",
        "- [[a-personal|a-personal]]",
    ]


def _seed_cut_corpus(vault: Path) -> None:
    """Seed one OPEN fragment plus a full cap's worth of PERSONAL ones.

    ``z-open`` sorts last by id, so with the weighting **off** every fragment
    ties at base 5 and the ascending-id tiebreak cuts exactly ``z-open``. With
    it **on**, ``z-open``'s 1.5x multiplier lifts it to the front and a
    PERSONAL fragment is cut instead. That makes the top-N *set*, not merely
    its order, depend on the config value.

    Args:
        vault: Vault root to seed.
    """
    for index in range(DEFAULT_MAX_PER_REGISTER):
        _write_voice_fragment(vault, f"p-{index:02d}", tier="personal")
    _write_voice_fragment(vault, "z-open", tier="open")


def _persisted_stems(vault: Path, register: str = "confessional") -> set[str]:
    """Return the persisted sample filenames in *register*, minus the summary."""
    register_dir = vault.joinpath(*_SAMPLES_SUBPATH, register)
    if not register_dir.is_dir():
        return set()
    return {p.name for p in register_dir.glob("*.md") if p.name != "_Summary.md"}


def test_weighting_changes_which_bodies_are_persisted_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob changes top-N SET MEMBERSHIP, not just ordering.

    This is the privacy-relevant half of the fix. A persisted register sample
    is a source fragment's file copied into the vault byte for byte, so which
    fragments survive the cut decides *whose prose is duplicated* into
    ``07-Voice/Register-Samples/`` and seeds drafts. Asserting only on
    ordering would miss that entirely.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_cut_corpus(on_vault)
    _seed_cut_corpus(off_vault)

    _run_report(on_vault, "voice")
    _run_report(off_vault, "voice")

    on_stems = _persisted_stems(on_vault)
    off_stems = _persisted_stems(off_vault)
    assert len(on_stems) == len(off_stems) == DEFAULT_MAX_PER_REGISTER
    assert on_stems != off_stems, (
        "Flipping voice_audience_weighting.enabled left the persisted sample "
        "set identical, so the knob is not reaching the top-N cut."
    )
    assert "z-open.md" in on_stems
    assert "z-open.md" not in off_stems


def test_enabling_the_weighting_prunes_the_sample_that_left_the_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fragment pushed out of the top-N has its verbatim copy DELETED.

    ``generate_register_samples`` both writes and deletes. If activation
    changed the cut but left the displaced fragment's byte-for-byte copy
    behind, the vault would accumulate orphaned above-weight prose that no
    summary references — the worst of both configurations.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(tmp_path, enabled=False)
    _seed_cut_corpus(vault)

    _run_report(vault, "voice")
    assert "z-open.md" not in _persisted_stems(vault)
    displaced = sorted(_persisted_stems(vault))[-1]

    _write_vault_config(vault, enabled=True)
    result = runner.invoke(
        app,
        ["report", "--type", "voice", "--vault", str(vault)],
    )
    assert result.exit_code == 0, result.output

    stems = _persisted_stems(vault)
    assert "z-open.md" in stems
    assert displaced not in stems, (
        f"{displaced} left the top-N but its verbatim copy survived on disk."
    )
    assert "pruned" in result.output.lower()


def _profile_passages(vault: Path, register: str = "confessional") -> list[str]:
    """Return the ``### Sample Passages`` block of a rendered profile.

    Reads the numbered passage lines the profile renderer emits between the
    ``### Sample Passages`` and ``### Anti-Patterns (DO NOT)`` headings.

    Args:
        vault: Vault root.
        register: Voice register whose profile to read.

    Returns:
        The passage lines, in rendered order.
    """
    text = (vault / "07-Voice" / f"{register}-profile.md").read_text(encoding="utf-8")
    body = text.split("### Sample Passages", 1)[1]
    body = body.split("### Anti-Patterns", 1)[0]
    return [line for line in body.splitlines() if line and line[0].isdigit()]


def test_profile_sample_passages_depend_on_the_vault_weighting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``VoiceProfileGenerator`` at the CLI honours the vault's config.

    Independent of the register-samples path: this guards the *profile*
    construction, which the MCP surface also drives. Enough candidates are
    seeded that the selected SET differs, not merely its order — the profile
    cap is below the seeded corpus, so exactly one member of the cohort is
    decided by the config.

    Named rather than merely different: ``z-open`` leads the weighted profile
    and is absent from the unweighted one. A fixture that reordered the
    passages without changing the set could satisfy an inequality but not
    both halves of this.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_cut_corpus(on_vault)
    _seed_cut_corpus(off_vault)

    _run_report(on_vault, "voice")
    _run_report(off_vault, "voice")

    on_passages = _profile_passages(on_vault)
    off_passages = _profile_passages(off_vault)
    assert on_passages != off_passages
    assert on_passages[0].startswith("1. z-open "), on_passages[0]
    assert all("z-open" not in line for line in off_passages)


def test_custom_privacy_tier_authority_inverts_the_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whole config object is threaded, not merely the ``enabled`` boolean.

    The ids are chosen so the expected order is one **no other configuration
    can produce**, which a `personal`-first-by-id fixture could not claim:

    * shipped default (``open: 1.5``) ranks ``a-open`` first on its multiplier;
    * disabled ranks ``a-open`` first on the ascending-id tiebreak;
    * only this custom map — ``open`` demoted to 0.5 — puts ``b-personal``
      first.

    So a fix that forwarded merely a boolean, special-cased ``enabled``, or
    dropped the config entirely fails here, and the test cannot be satisfied
    by accident.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(
        tmp_path,
        enabled=True,
        privacy_tier_authority={
            "open": 0.5,
            "personal": 1.0,
            "unclassified": 0.75,
            "intimate": 0.0,
        },
    )
    _write_voice_fragment(vault, "a-open", tier="open")
    _write_voice_fragment(vault, "b-personal", tier="personal")

    _run_report(vault, "voice")

    assert _summary_links(vault) == [
        "- [[b-personal|b-personal]]",
        "- [[a-open|a-open]]",
    ]


def test_the_vault_config_beats_a_cwd_config_that_names_the_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``--vault``, the VAULT's own config still wins.

    Deliberately does not use :func:`_isolate`: this test needs a cwd config,
    because it pins the one case where the two resolutions genuinely diverge.
    ``creek report`` finds its vault from the cwd file, while the voice
    handlers re-resolve from the *resolved* vault path — so the cwd config
    names the vault and enables the weighting, and the vault's own config
    disables it. The vault's file is the intended winner for vault-scoped
    behaviour.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    vault = _weighted_vault(tmp_path, enabled=False)
    _write_voice_fragment(vault, "a-personal", tier="personal")
    _write_voice_fragment(vault, "b-open", tier="open")

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    outer: dict[str, Any] = CreekConfig().model_dump(mode="json")
    outer["vault_path"] = str(vault)
    outer["voice_audience_weighting"]["enabled"] = True
    (cwd / "creek_config.yaml").write_text(
        yaml.dump(outer, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.chdir(cwd)

    result = runner.invoke(app, ["report", "--type", "voice"])
    assert result.exit_code == 0, result.output

    assert _summary_links(vault) == [
        "- [[a-personal|a-personal]]",
        "- [[b-open|b-open]]",
    ]


_MEMBERSHIP_CEILINGS = ["all", "open"]
assert len(_MEMBERSHIP_CEILINGS) == 2, (
    "An emptied parametrize list would silently skip the privacy cases and "
    "hide them behind a green gate."
)


@pytest.mark.parametrize("ceiling", _MEMBERSHIP_CEILINGS)
def test_weighting_can_never_widen_corpus_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ceiling: str,
) -> None:
    """No authority value can admit an intimate or above-ceiling fragment.

    The knob multiplies a ranking score; membership is decided upstream and
    independently by ``within_ceiling`` and ``_eligible_register``, and
    ``rank_exemplars`` returns ``scored[:max]`` regardless of score. So even a
    deliberately absurd ``intimate: 10.0`` cannot pull an intimate fragment in
    — and under a narrowed ceiling an above-ceiling PERSONAL fragment stays
    out too.

    Asserted at both ceilings because the two gates are separate: the consent
    gate excludes intimate content at every ceiling, while ``--include-tier
    open`` additionally excludes PERSONAL.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
        ceiling: The ``--include-tier`` value under test.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(
        tmp_path,
        enabled=True,
        privacy_tier_authority={
            "open": 1.5,
            "personal": 1.0,
            "unclassified": 0.75,
            "intimate": 10.0,
        },
    )
    _write_voice_fragment(vault, "a-open", tier="open")
    _write_voice_fragment(vault, "b-personal", tier="personal")
    _write_voice_fragment(vault, "c-intimate", tier="intimate")

    _run_report(vault, "voice", "--include-tier", ceiling)

    persisted = _persisted_stems(vault)
    links = " ".join(_summary_links(vault))
    assert "c-intimate.md" not in persisted, (
        "A 10.0 intimate authority pulled an INTIMATE fragment into the "
        "verbatim-copied register samples. The weighting must never override "
        "the consent gate."
    )
    assert "c-intimate" not in links
    if ceiling == "open":
        assert "b-personal.md" not in persisted, (
            "An above-ceiling PERSONAL fragment survived --include-tier open."
        )
        assert "b-personal" not in links


def test_shipped_default_config_is_byte_identical_to_the_code_default(
    tmp_path: Path,
) -> None:
    """Activation is a provable no-op for a vault nobody edited.

    ``generate_default_config`` dumps the full model, so every vault ``creek
    init`` ever created carries a literal ``voice_audience_weighting`` block.
    This asserts that block deserialises to exactly the object the exemplar
    path was already using — which is the whole reason activating the config
    is safe to ship without an opt-in flag.
    """
    target = tmp_path / "creek_config.yaml"
    generate_default_config(target)
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))

    seeded = VoiceAudienceWeightingConfig.model_validate(
        raw["voice_audience_weighting"],
    )
    assert seeded == VoiceAudienceWeightingConfig()


def _voice_tree(vault: Path) -> dict[str, list[str]]:
    """Return every ``07-Voice`` file's content, minus generation timestamps.

    Args:
        vault: Vault root.

    Returns:
        Mapping of relative path to its timestamp-free lines.
    """
    root = vault / "07-Voice"
    out: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        out[str(path.relative_to(root))] = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "generated_at" not in line and "generated_date" not in line
        ]
    return out


def test_untouched_default_config_produces_unchanged_voice_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vault with the shipped default matches one with no config at all.

    "No config at all" is what the exemplar path effectively used before the
    fix — the fabricated ``VoiceAudienceWeightingConfig()``. Byte-identical
    ``07-Voice/`` output between the two (timestamps excluded) is the direct
    evidence that operators who never touched the knob see no change.
    """
    _isolate(monkeypatch, tmp_path)
    seeded = _weighted_vault(tmp_path, enabled=True, name="seeded")
    bare = tmp_path / "bare"
    (bare / "01-Fragments" / "Journal").mkdir(parents=True)
    (bare / "00-Creek-Meta").mkdir(parents=True)
    for vault in (seeded, bare):
        _seed_cut_corpus(vault)

    _run_report(seeded, "voice")
    _run_report(bare, "voice")

    assert _voice_tree(seeded) == _voice_tree(bare)


def test_lexicon_output_is_invariant_to_the_audience_weighting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lexicon is unranked, therefore unweighted — pinned as a tripwire.

    This test exists **instead of** threading ``audience_weighting`` into
    ``generate_lexicon``. That edit would be a no-op no test could fail on:
    ``collect_all_exemplars`` performs no ranking or capping,
    ``extract_patterns`` is called there with no ``weights=``, and
    ``build_lexicon`` reads ``patterns`` only for ``metaphor_families``. See
    the ``Note:`` on :func:`creek.generate.lexicon.generate_lexicon`.

    Unlike a structural-guard exclusion, this goes red the day the lexicon
    starts consuming a weighted metric — at which point the deliberate
    decision it pins needs revisiting rather than silently rotting.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_cut_corpus(on_vault)
    _seed_cut_corpus(off_vault)

    _run_report(on_vault, "lexicon")
    _run_report(off_vault, "lexicon")

    def _lexicon_tree(vault: Path) -> dict[str, list[str]]:
        root = vault / "07-Voice" / "Lexicon"
        return {
            str(p.relative_to(root)): [
                line
                for line in p.read_text(encoding="utf-8").splitlines()
                if "generated_date" not in line and "generated_at" not in line
            ]
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    assert _lexicon_tree(on_vault), "The lexicon report wrote nothing to assert on."
    assert _lexicon_tree(on_vault) == _lexicon_tree(off_vault)


_PARADOX_SENTENCE = " The current holds, and yet the bank runs dry."
"""One paradox construction, and the only rhetorical move in any fixture body.

``_compute_rhetorical_moves`` scores three families, and exactly one member of
one of them — the ``and yet`` alternation of ``_PARADOX_PATTERNS`` — appears
here. Nothing in :func:`_voice_body` matches any self-deprecation, paradox or
callback pattern, so a register's whole tally is
:data:`_MOVES_WITH_THE_OPEN_FRAGMENT` when the fragment carrying this sentence
is in the cohort and :data:`_MOVES_WITHOUT_THE_OPEN_FRAGMENT` when it is not.
One construction rather than many on purpose: the tally is then an exact
expected value rather than a number nobody can check by reading the fixture,
and an exact value is what kills a mutant.
"""

_MOVES_WITH_THE_OPEN_FRAGMENT = {
    "Self-deprecation before insight": 0,
    "Paradox constructions": 1,
    "Callbacks to earlier points": 0,
}
"""The tally when the weighting lifted ``z-open`` into the top-N cohort."""

_MOVES_WITHOUT_THE_OPEN_FRAGMENT = {
    "Self-deprecation before insight": 0,
    "Paradox constructions": 0,
    "Callbacks to earlier points": 0,
}
"""The tally when ``z-open`` fell off the end of the unweighted cut."""

_MOVE_LINE_RE = re.compile(r"^- (?P<label>[^:\n]+): (?P<count>\d+)\.$", re.MULTILINE)
"""Matches one ``- <label>: <n>.`` line of ``_format_rhetorical_moves`` output.

A local clone of the regex in ``tests/test_mcp_report_tier_ceiling.py`` rather
than an import: private test helpers are not shared across modules here.
"""


def _seed_rhetorical_corpus(vault: Path) -> None:
    """Seed a corpus where the OPEN fragment is the only paradox-carrying one.

    ``generate_rhetorical_patterns`` counts rhetorical moves over the *ranked*
    top-N exemplars, and its note renders nothing but three integer counts —
    no fragment-derived text at all. Making the single OPEN fragment the only
    carrier of a paradox marker is therefore what gives that note an observable
    at all: the tally moves if and only if the weighting lifted ``z-open`` into
    the cohort.

    Args:
        vault: Vault root to seed.
    """
    for index in range(DEFAULT_MAX_PER_REGISTER):
        _write_voice_fragment(vault, f"p-{index:02d}", tier="personal")
    _write_voice_fragment(
        vault,
        "z-open",
        tier="open",
        body=_voice_body("z-open") + _PARADOX_SENTENCE,
    )


def _move_counts(vault: Path, register: str = "confessional") -> dict[str, int]:
    """Return a register's ``### Rhetorical Moves`` tally, parsed off disk.

    Parsed rather than diffed as text, and that is load-bearing: the renderer
    stamps ``datetime.now()`` into every note, so two notes ALWAYS differ and a
    bare inequality over the raw file passes unconditionally — it would survive
    a mutation deleting the very keyword these tests exist to guard.

    Args:
        vault: Vault root.
        register: Voice register whose note to read.

    Returns:
        Mapping of move label to its integer count.
    """
    path = vault.joinpath("07-Voice", "Rhetorical-Patterns", f"{register}.md")
    return {
        match.group("label"): int(match.group("count"))
        for match in _MOVE_LINE_RE.finditer(path.read_text(encoding="utf-8"))
    }


def test_cli_rhetorical_patterns_read_the_vault_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``report --type rhetorical-patterns`` honours the vault's weighting.

    A second, independently-droppable ``VoiceProfileGenerator`` construction
    that the issue body never listed. Its output looks like ordinary working
    code at the call site, which is exactly why it needs its own behavioural
    test rather than relying on the profile one.

    Both tallies are pinned by value, in both directions. Asserting only that
    the two differ would also be satisfied by a generator that emptied the
    register on one side, which is an outage rather than a gate.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_rhetorical_corpus(on_vault)
    _seed_rhetorical_corpus(off_vault)

    _run_report(on_vault, "rhetorical-patterns")
    _run_report(off_vault, "rhetorical-patterns")

    assert _move_counts(on_vault) == _MOVES_WITH_THE_OPEN_FRAGMENT
    assert _move_counts(off_vault) == _MOVES_WITHOUT_THE_OPEN_FRAGMENT


def test_mcp_rhetorical_patterns_read_the_vault_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``report_type="rhetorical-patterns"`` tool honours the config too.

    ``creek_mcp/tools/report.py`` builds its own ``VoiceProfileGenerator`` for
    this type, independently of both the CLI handler and its own ``voice``
    branch, so the keyword can be dropped here alone.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_rhetorical_corpus(on_vault)
    _seed_rhetorical_corpus(off_vault)

    _mcp_report(on_vault, "rhetorical-patterns", TierCeiling.ALL)
    _mcp_report(off_vault, "rhetorical-patterns", TierCeiling.ALL)

    assert _move_counts(on_vault) == _MOVES_WITH_THE_OPEN_FRAGMENT
    assert _move_counts(off_vault) == _MOVES_WITHOUT_THE_OPEN_FRAGMENT


def _fingerprint(vault: Path) -> dict[str, Any]:
    """Return the persisted voice fingerprint as a dict."""
    path = vault / "00-Creek-Meta" / "voice-fingerprint.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


_PERSONAL_ZEROED_AUTHORITY = {
    "open": 1.5,
    "personal": 0.0,
    "unclassified": 0.75,
    "intimate": 0.0,
}
"""The shipped tier authorities with ``personal`` zeroed, for the fingerprint.

Needed because the *shipped* map has no reachable zero on that path. Its only
``0.0`` is ``intimate``, and ``_eligible_texts`` drops intimate fragments
before it ever multiplies an authority in — so under the defaults every
eligible fragment keeps a positive weight and ``fragment_count`` is the same
with the weighting on or off. Every other factor bottoms out at ``0.1``
(``platform_authority["journal"]``) or ``0.3``
(``representativeness_authority["reference"]``), never zero.

Zeroing ``personal`` puts a genuine ``if weight > 0.0`` exclusion within
reach, which is what turns the persisted ``fragment_count`` into an exact
integer observable. The alternative — comparing feature *rates* — is not
available on this fixture: every extractor in ``FINGERPRINT_FEATURES`` returns
the same value for every :func:`_voice_body`, so a weighted mean over them
equals the unweighted one and the two artifacts come out byte-identical.
Passing the same map to both vaults keeps ``enabled`` the only difference
between them, and proves the whole config object reaches ``build_fingerprint``
rather than a boolean.
"""


def _seed_fingerprint_corpus(vault: Path) -> None:
    """Seed one OPEN and one PERSONAL fragment for the fingerprint path.

    Both are ``journal``/``self`` and non-intimate, so both clear the
    authorship and privacy filters and the only thing that can separate them is
    the audience authority. Under :data:`_PERSONAL_ZEROED_AUTHORITY` the
    PERSONAL fragment's combined weight is exactly ``0.0``, and on this path —
    unlike the exemplar path — a zero weight is a membership gate rather than a
    de-ranking.

    Args:
        vault: Vault root to seed.
    """
    _write_voice_fragment(vault, "f-open", tier="open")
    _write_voice_fragment(vault, "f-personal", tier="personal")


def _fingerprint_vaults(tmp_path: Path) -> tuple[Path, Path]:
    """Build the ``(weighted, unweighted)`` fingerprint vault pair.

    Both carry :data:`_PERSONAL_ZEROED_AUTHORITY` and the same two fragments;
    they differ in ``voice_audience_weighting.enabled`` and nothing else.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The enabled vault and the disabled vault.
    """
    vaults: list[Path] = []
    for name, enabled in (("on", True), ("off", False)):
        vault = _weighted_vault(
            tmp_path,
            enabled=enabled,
            name=name,
            privacy_tier_authority=_PERSONAL_ZEROED_AUTHORITY,
        )
        _seed_fingerprint_corpus(vault)
        vaults.append(vault)
    return vaults[0], vaults[1]


def test_cli_fingerprint_reads_the_vault_config_not_the_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``report --type fingerprint --vault X`` honours X's own config.

    This site *looked* wired before #1313 — it did read
    ``voice_audience_weighting`` — but through a bare ``load_config()``, which
    resolves against the current directory and never opens the vault's file.
    From a config-less cwd it therefore ran on built-in defaults and silently
    ignored ``--vault``. Only a test that refuses to set ``CREEK_CONFIG`` or
    chdir into the vault can see that.

    The observable is ``fragment_count``, pinned by value on both sides: a
    ``0.0`` authority is a genuine membership gate here (``if weight > 0.0``
    in ``_eligible_texts``), so the weighted run fingerprints one fragment and
    the unweighted run fingerprints both. Asserting the two artifacts merely
    *differ* would be weaker and, on this corpus, wrong — see
    :data:`_PERSONAL_ZEROED_AUTHORITY`.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault, off_vault = _fingerprint_vaults(tmp_path)

    _run_report(on_vault, "fingerprint")
    _run_report(off_vault, "fingerprint")

    assert _fingerprint(on_vault)["fragment_count"] == 1
    assert _fingerprint(off_vault)["fragment_count"] == 2


def _mcp_report(vault: Path, report_type: str, ceiling: TierCeiling) -> None:
    """Drive the MCP ``report`` tool directly and assert it did not refuse."""
    result = report_tool(
        vault_path=vault,
        report_type=report_type,
        privacy_tier_ceiling=ceiling,
        consumer="test",
    )
    assert result.get("status") != "refused", result


def test_mcp_voice_report_reads_the_vault_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``report_type="voice"`` tool honours the vault's weighting.

    Asserted on the PROFILE, deliberately, not on ``Register-Samples/``: the
    MCP voice tool does not write register samples, and that divergence from
    the CLI is a recorded decision (#1204), not an oversight to tidy up here.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault = _weighted_vault(tmp_path, enabled=True, name="on")
    off_vault = _weighted_vault(tmp_path, enabled=False, name="off")
    _seed_cut_corpus(on_vault)
    _seed_cut_corpus(off_vault)

    _mcp_report(on_vault, "voice", TierCeiling.ALL)
    _mcp_report(off_vault, "voice", TierCeiling.ALL)

    on_passages = _profile_passages(on_vault)
    off_passages = _profile_passages(off_vault)
    assert on_passages != off_passages
    assert on_passages[0].startswith("1. z-open "), on_passages[0]
    assert all("z-open" not in line for line in off_passages)
    assert not (on_vault / "07-Voice" / "Register-Samples").exists(), (
        "The MCP voice tool wrote register samples. That it does not is a "
        "recorded decision (#1204); if it changes, this module needs the "
        "membership assertions the CLI half already carries."
    )


def test_mcp_fingerprint_report_reads_the_vault_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``report_type="fingerprint"`` tool honours the vault's weighting.

    Same bare-``load_config()`` defect as the CLI fingerprint handler, on the
    other surface. ``fingerprint`` is tier-blind, so it is served only at the
    ``ALL`` ceiling, and its refusal below that is asserted by
    ``tests/test_mcp_report_tier_ceiling.py`` rather than restated here.
    """
    _isolate(monkeypatch, tmp_path)
    on_vault, off_vault = _fingerprint_vaults(tmp_path)

    _mcp_report(on_vault, "fingerprint", TierCeiling.ALL)
    _mcp_report(off_vault, "fingerprint", TierCeiling.ALL)

    assert _fingerprint(on_vault)["fragment_count"] == 1
    assert _fingerprint(off_vault)["fragment_count"] == 2


_AUTHENTICITY_CASES = [(True, "ON"), (False, "OFF")]
assert len(_AUTHENTICITY_CASES) == 2, (
    "An emptied parametrize list would silently skip both halves of the "
    "voice-authenticity assertion and hide the fix behind a green gate."
)


@pytest.mark.parametrize(("enabled", "expected"), _AUTHENTICITY_CASES)
def test_voice_authenticity_reports_the_vaults_weighting_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    expected: str,
) -> None:
    """``creek voice-authenticity`` reports the vault's setting, not a default.

    ``_probe_audience_mix`` previously constructed a fresh
    ``VoiceAudienceWeightingConfig()`` and reported its ``enabled`` value —
    so the diagnostic printed ``ON`` for every vault in existence, including
    ones that had switched the feature off. A diagnostic reporting its own
    default back as an observation is worse than no diagnostic.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
        enabled: The vault's configured value.
        expected: The literal token the summary line must carry.
    """
    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(tmp_path, enabled=enabled)
    _write_voice_fragment(vault, "a-personal", tier="personal")

    result = runner.invoke(app, ["voice-authenticity", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    # The audience-mix line runs past the 80-column default and Rich soft-wraps
    # it, so whitespace is collapsed before matching. Without this the
    # assertion is really about where the line happened to break, and
    # ``weighting:`` and its value can land on opposite sides of the fold.
    assert f"weighting: {expected}" in " ".join(result.output.split())

    as_json = runner.invoke(
        app,
        ["voice-authenticity", "--vault", str(vault), "--json"],
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["audience_mix"]["weighting_active"] is enabled


def test_fill_voice_step_honours_the_vault_audience_weighting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek fill`` reaches ``_report_voice`` unattended, and it obeys there too.

    ``fill`` is the "make my vault prod-ready" umbrella, and its
    ``report/voice`` step is how most vaults ever get register samples at all
    (#879) — so the vault's config has to reach the exemplar path down that
    route, not only down the one an operator types by hand.

    The plan is built by the production ``_build_fill_steps`` and its own
    ``report/voice`` entry is invoked, rather than driving the whole command.
    ``creek fill``'s first step, ``link/embeddings``, instantiates a
    ``SentenceTransformer``, which reaches for model weights over the network;
    ``fill`` catches the failure and carries on, so the command would still
    *work*, but a unit test that waits on an HTTP timeout is not hermetic.
    Nothing is stubbed in exchange: the callable invoked here is the production
    lambda, calling the production ``_report_voice`` with the production
    arguments, so a weighting dropped on that route still fails this.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.cli import _build_fill_steps, _load_config_for_vault

    _isolate(monkeypatch, tmp_path)
    vault = _weighted_vault(tmp_path, enabled=False)
    _write_voice_fragment(vault, "a-personal", tier="personal")
    _write_voice_fragment(vault, "b-open", tier="open")

    steps = dict(
        _build_fill_steps(vault, _load_config_for_vault(vault), with_compost=False),
    )
    assert "report/voice" in steps
    steps["report/voice"]()

    assert _summary_links(vault) == [
        "- [[a-personal|a-personal]]",
        "- [[b-open|b-open]]",
    ]
