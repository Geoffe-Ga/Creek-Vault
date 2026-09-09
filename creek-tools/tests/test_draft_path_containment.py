"""The draft path inherits the #1373 vault containment guard (#1789).

``creek.vault.reader.iter_vault_fragments`` skips a ``.md`` file under
``01-Fragments/`` that is a symlink resolving OUTSIDE that root (#1373), and
:func:`creek.classify.privacy_filter.source_tiers` — the survey that decides
which provider a draft prompt may reach — walks through it.

Two loaders on the draft path did not: ``creek.generate.mining._load_fragments``
and ``creek.generate.drafts._load_fragments_by_id`` were bespoke
``sorted(root.rglob("*.md"))`` scans. So the component that decides whether
content may be routed never saw the file that got routed, and the direction of
the divergence was the unsafe one: the tier survey **skipped** the planted
file (it contributes no tier) while the miner and the draft loader **read** it
and rendered its body into the LLM prompt.

Every fixture here plants the escaping fragment at ``privacy_tier: open``
deliberately. The leak needs no tier misconfiguration at all — the guard that
would have excluded the file never ran, so the tier it declares is beside the
point. An attacker who can drop one symlink into ``01-Fragments/`` chooses
both the content and the tier it is admitted under.

The skip must stay **silent about the resolved target**: naming what a link
points at turns a safety log into the exfiltration oracle #1087 closed.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.generate.drafts import (
    DraftGenerator,
    DraftLLM,
    _load_fragments_by_id,
)
from creek.generate.mining import (
    IdeaMiner,
    IdeaSeed,
    MiningStrategy,
    _load_fragments,
    _load_mining_snapshot,
)
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    PraxisPotential,
    PrivacyTier,
    SourcePlatform,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_LEAK_SENTINEL = "CANARY-DRAFT-PATH-PLANTED-1789-a91f"
"""Body text of the fragment parked OUTSIDE the vault and symlinked into it."""

_LEAK_ID = "planted-1789"
"""Id of the planted fragment — attacker-chosen, so also a canary."""

_LEAK_TITLE = "CANARY-TITLE-PLANTED-1789-6d02"
"""Title of the planted fragment — reaches an IdeaSeed's ``title`` verbatim."""

_INROOT_SENTINEL = "CONTROL-DRAFT-PATH-INROOT-1789-3b58"
"""Body text of the genuine in-vault fragment every fixture also plants."""

_INROOT_ID = "in-vault-1789"
"""Id of the genuine in-vault fragment."""

_PHASE = Phase.RISING
"""The phase both fragments carry, so the wavelength strategy surfaces them."""


def _fragment(frag_id: str, title: str) -> Fragment:
    """Return a fragment the wavelength mining strategy will surface.

    ``phase`` matches the phase :func:`test_mine_all_seeds_never_name_an_escape`
    mines at and ``praxis_potential`` is ``explicit``, which are jointly the
    gate ``IdeaMiner._mine_wavelength_windows_with_diagnostic`` applies. A
    fragment that failed either would be absent from the seeds for a reason
    that has nothing to do with containment, making the assertion vacuous.

    ``privacy_tier`` is ``OPEN`` on purpose — see the module docstring.

    Args:
        frag_id: Fragment id, also the file stem.
        title: Fragment title.

    Returns:
        The fragment model.
    """
    return Fragment(
        id=frag_id,
        title=title,
        privacy_tier=PrivacyTier.OPEN,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 3, 1, tzinfo=UTC),
        ingested=datetime(2026, 3, 1, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F1),
        wavelength=WavelengthClassification(phase=_PHASE),
        praxis_potential=PraxisPotential.EXPLICIT,
    )


def _write(folder: Path, fragment: Fragment, body: str) -> Path:
    """Write *fragment* into *folder* and return the path.

    Args:
        folder: Destination directory (created if absent).
        fragment: The fragment to persist.
        body: The markdown body below the frontmatter.

    Returns:
        The path written.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fragment.id}.md"
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _plant_draft_path_escape(tmp_path: Path) -> tuple[Path, Path]:
    """Build a vault holding one real fragment and one symlinked-in outsider.

    The planted file carries **valid Creek frontmatter**, so a skip can never
    be credited to a parse failure: if a loader declines it, it declined it on
    containment grounds and nothing else.

    The two preconditions are asserted here rather than described, because a
    symlink fixture that silently degrades into a plain file — a filesystem
    without link support, a copy-on-write ``symlink_to`` — turns every test
    below into a no-op that passes for the wrong reason.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        ``(vault, link)`` — the vault root and the escaping fragment link.
    """
    vault = tmp_path / "vault"
    fragments = vault / "01-Fragments"
    _write(fragments, _fragment(_INROOT_ID, "In vault"), _INROOT_SENTINEL)
    outside = _write(
        tmp_path / "outside",
        _fragment(_LEAK_ID, _LEAK_TITLE),
        _LEAK_SENTINEL,
    )
    link = fragments / "leak.md"
    link.symlink_to(outside)

    assert link.is_symlink(), (
        "the fixture did not create a symlink, so every containment "
        f"assertion below would pass vacuously.\n\nlink={link}"
    )
    resolved = os.path.realpath(link)
    vault_root = os.path.realpath(vault)
    assert not resolved.startswith(vault_root + os.sep), (
        "the planted link resolves INSIDE the vault, so it is not an escape "
        f"and proves nothing.\n\nresolved={resolved}\nvault={vault_root}"
    )
    return vault, link


def _bodies(pairs: list[tuple[Fragment, str]]) -> list[str]:
    """Return just the body strings from ``(fragment, body)`` pairs.

    Args:
        pairs: Loader output.

    Returns:
        The bodies, in loader order.
    """
    return [body for _fragment, body in pairs]


def test_mining_loader_skips_a_fragment_symlinked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """RED. ``mining._load_fragments`` must inherit the #1373 skip.

    Asserted on the body text rather than on a record count: a count assertion
    is satisfied just as happily by a loader that dropped the wrong one.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault, _link = _plant_draft_path_escape(tmp_path)

    bodies = _bodies(_load_fragments(vault / "01-Fragments"))

    assert _INROOT_SENTINEL in bodies, (
        "the genuine in-vault fragment was not loaded, so the absence "
        "assertion below would be satisfied by a loader that read nothing at "
        f"all.\n\nbodies={bodies}"
    )
    assert _LEAK_SENTINEL not in bodies, (
        "a fragment whose file is a symlink out of the vault entered the "
        "mining snapshot. The tier survey behind `creek draft` skips this "
        "same file, so nothing about it was ever ranked — its body reaches "
        f"the LLM prompt untiered.\n\nbodies={bodies}"
    )


def test_mining_snapshot_skips_a_fragment_symlinked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """RED. The skip survives snapshot assembly, which is what the miner reads.

    ``_load_fragments`` is private; ``_load_mining_snapshot`` is what every
    strategy actually consumes. Pinning both means a future refactor that
    moves the walk cannot quietly drop the guard on the way.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault, _link = _plant_draft_path_escape(tmp_path)

    snapshot = _load_mining_snapshot(vault, bypass_compiled=True)

    bodies = _bodies(list(snapshot.fragments))
    assert _INROOT_SENTINEL in bodies, (
        f"the control fragment is missing; the check below is vacuous.\n\n{bodies}"
    )
    assert _LEAK_SENTINEL not in bodies, (
        f"the planted body reached the mining snapshot.\n\n{bodies}"
    )


def test_mine_all_seeds_never_name_an_escape(tmp_path: Path) -> None:
    """RED. No seed may carry the planted fragment's attacker-chosen identity.

    An :class:`~creek.generate.mining.IdeaSeed` renders the fragment's *title*
    and *id* — both supplied by the planted file — and those are the strings a
    ``creek mine`` operator reads and a ``creek draft`` call then names as a
    source. Asserting on the whole seed's repr catches every field at once
    rather than only the two this strategy happens to populate today.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault, _link = _plant_draft_path_escape(tmp_path)
    miner = IdeaMiner(bypass_compiled=True)

    seeds = miner.mine_all(vault, current_phase=_PHASE)

    rendered = [repr(seed) for seed in seeds]
    assert any(_INROOT_ID in text for text in rendered), (
        "the miner surfaced no seed for the genuine in-vault fragment, so "
        "'the planted id is absent' below is satisfied by a miner that "
        f"surfaced nothing.\n\nseeds={rendered}"
    )
    assert not any(_LEAK_ID in text for text in rendered), (
        "a fragment symlinked out of the vault became a mined essay seed. "
        f"Both its id and its title are attacker-chosen.\n\nseeds={rendered}"
    )
    assert not any(_LEAK_TITLE in text for text in rendered), (
        f"the planted fragment's title reached a seed.\n\nseeds={rendered}"
    )


def test_draft_loader_skips_a_fragment_symlinked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """RED. ``drafts._load_fragments_by_id`` must inherit the #1373 skip.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault, _link = _plant_draft_path_escape(tmp_path)

    loaded = _load_fragments_by_id(vault / "01-Fragments")

    assert _INROOT_ID in loaded, (
        f"the control fragment is missing; the check below is vacuous.\n\n{loaded}"
    )
    assert _LEAK_ID not in loaded, (
        "a fragment symlinked out of the vault is available to the draft "
        "composer by id. `_render_fragment_section` puts its title AND body "
        f"straight into the prompt.\n\nloaded={sorted(loaded)}"
    )


def test_the_draft_loaders_never_see_more_than_the_tier_survey(
    tmp_path: Path,
) -> None:
    """RED. The divergence itself, asserted as a subset relation.

    This is the invariant the two bespoke scans broke, stated without
    reference to symlinks: **whatever the draft path can render, the tier
    survey must have been able to rank.** A file the survey cannot see
    contributes no tier, so if a loader can still render it the routing
    decision is made about a corpus that is not the one being sent.

    Pinned as a subset rather than as equality on purpose: the privacy filter
    legitimately removes intimate fragments from the loaders that the survey
    still ranks, so the loaders being *smaller* is correct and expected. Only
    the other direction is a leak.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek.classify.privacy_filter import PrivacyTierOverride, resolved_source_tiers

    vault, _link = _plant_draft_path_escape(tmp_path)

    drafted = set(
        _load_fragments_by_id(
            vault / "01-Fragments",
            privacy_override=PrivacyTierOverride.ALL,
        ),
    )
    mined = {
        fragment.id
        for fragment, _body in _load_fragments(
            vault / "01-Fragments",
            privacy_override=PrivacyTierOverride.ALL,
        )
    }
    surveyed = set(resolved_source_tiers(vault, drafted | mined))

    assert _INROOT_ID in surveyed, (
        f"the survey saw nothing; the subset checks are vacuous.\n\n{surveyed}"
    )
    assert drafted <= surveyed, (
        "the draft loader renders fragments the tier survey cannot rank. "
        "Every id in the difference reaches an LLM prompt with no tier "
        f"behind it.\n\nunranked={sorted(drafted - surveyed)}"
    )
    assert mined <= surveyed, (
        "the mining loader reads fragments the tier survey cannot rank."
        f"\n\nunranked={sorted(mined - surveyed)}"
    )


class _RecordingFactory:
    """A tier-keyed draft LLM factory that records every prompt it is handed.

    The prompt is the payload this issue is about: ``_render_fragment_section``
    emits ``### {id}: {title}`` followed by the body, so a canary in the body
    that appears here has crossed the boundary into whatever provider the
    factory's tier resolves to.
    """

    def __init__(self) -> None:
        """Start with an empty prompt log."""
        self.prompts: list[str] = []
        self.tiers: list[PrivacyTier] = []

    def __call__(self, tier: PrivacyTier) -> DraftLLM:
        """Return the recording client for *tier*.

        Args:
            tier: The routing tier the generator bound before composing.

        Returns:
            A callable that records its prompt and returns a fixed body.
        """
        self.tiers.append(tier)

        def _client(prompt: str) -> str:
            self.prompts.append(prompt)
            return "A drafted body."

        return _client


def test_draft_prompt_never_carries_a_fragment_symlinked_out_of_the_vault(
    tmp_path: Path,
) -> None:
    """RED. The end-to-end statement: the canary must not reach the model.

    The seed names the planted id directly. That is not a contrived step: a
    mined seed names ids off threads and resonances, which are tier-blind, and
    an operator drafting from ``creek mine`` output hands back whatever id the
    miner printed. The recording factory stands in for the provider, so a
    canary in ``factory.prompts`` is a canary that left the machine.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault, _link = _plant_draft_path_escape(tmp_path)
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    factory = _RecordingFactory()
    generator = DraftGenerator(
        llm_factory=factory,
        skills_root=skills_root,
        bypass_compiled=True,
    )
    seed = IdeaSeed(
        strategy=MiningStrategy.WAVELENGTH_WINDOW,
        title="A draft from the planted seed",
        source_fragments=(_INROOT_ID, _LEAK_ID),
        threads=(),
        eddies=(),
        frequency_affinity=(Frequency.F1,),
        brief_description="Drafted from both fragments.",
        score=1.0,
    )

    generator.generate_draft(seed, vault_path=vault)

    assert factory.prompts, "the generator never called the LLM at all."
    composed = "\n".join(factory.prompts)
    assert _INROOT_SENTINEL in composed, (
        "no vault fragment body reached the prompt, so the absence assertion "
        f"below is vacuous.\n\nprompt={composed}"
    )
    assert _LEAK_SENTINEL not in composed, (
        "the body of a fragment symlinked out of the vault was rendered into "
        "the draft prompt. The tier survey never saw this file, so nothing "
        f"about it was ranked before the call.\n\nprompt={composed}"
    )
    assert _LEAK_TITLE not in composed, (
        f"the planted fragment's title reached the prompt.\n\nprompt={composed}"
    )


def test_the_skip_is_logged_without_ever_naming_the_resolved_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The #1087 exfiltration oracle stays closed on the draft path too.

    Two halves, and both matter. An operator whose vault silently loses a
    fragment cannot tell a containment skip from a lost file, so the skip is
    logged at WARNING and names the link **as walked**. But naming what the
    link *resolves to* would let anyone who can read the log — or provoke one —
    learn about paths outside the vault, which is the oracle #1087 closed.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log-capture fixture.
    """
    vault, link = _plant_draft_path_escape(tmp_path)
    outside = str((tmp_path / "outside").resolve())

    with caplog.at_level(logging.DEBUG):
        _load_fragments(vault / "01-Fragments")
        _load_fragments_by_id(vault / "01-Fragments")

    messages = [record.getMessage() for record in caplog.records]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert any(link.name in message for message in warnings), (
        "a loader dropped a fragment and said nothing at WARNING. A silent "
        f"skip in a safety path is its own hazard.\n\nwarnings={warnings}"
    )
    assert not any(outside in message for message in messages), (
        "a log line named the resolved target of the escaping link. That is "
        f"the exfiltration oracle #1087 closed.\n\nmessages={messages}"
    )
    assert not any(_LEAK_SENTINEL in message for message in messages), (
        f"a log line quoted the content it declined to read.\n\n{messages}"
    )


def _vault_with_intra_root_alias(tmp_path: Path) -> Path:
    """Build a vault whose ``01-Fragments`` holds a fragment and an alias to it.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        The ``01-Fragments`` directory.
    """
    fragments = tmp_path / "vault" / "01-Fragments"
    _write(fragments, _fragment(_INROOT_ID, "In vault"), _INROOT_SENTINEL)
    alias = fragments / "alias.md"
    alias.symlink_to(fragments / f"{_INROOT_ID}.md")
    assert alias.is_symlink(), (
        "the alias fixture is not a symlink, so the anchor proves nothing."
    )
    return fragments


def test_the_mining_loader_still_loads_an_intra_root_alias(tmp_path: Path) -> None:
    """NON-VACUITY ANCHOR (passes before and after). Not "skip every symlink".

    Without this, every test above is satisfied by a loader that drops any
    symlinked fragment — which would silently shrink real vaults, since an
    alias beside the file it aliases is an ordinary Obsidian shape. It is the
    draft-path counterpart of
    ``test_iter_vault_fragments_still_loads_an_intra_vault_alias``.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    fragments = _vault_with_intra_root_alias(tmp_path)

    loaded = _load_fragments(fragments)

    assert len(loaded) == 2, (
        "an intra-root alias was dropped. The guard is about the target "
        "escaping the root, not about the link existing; refusing every "
        f"symlink breaks ordinary vaults.\n\nloaded={_bodies(loaded)}"
    )


def test_the_draft_loader_still_loads_an_intra_root_alias(tmp_path: Path) -> None:
    """NON-VACUITY ANCHOR (passes before and after) for the by-id loader.

    The by-id map collapses the alias and the file it aliases onto one key, so
    the assertion is that the id survives at all — a loader refusing every
    symlink would still yield it here (the real file is not a link), which is
    why the mining anchor above counts records instead. Both are kept: this
    one pins that the alias does not somehow *evict* the real entry.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    fragments = _vault_with_intra_root_alias(tmp_path)

    loaded = _load_fragments_by_id(fragments)

    assert _INROOT_ID in loaded, (
        f"the aliased fragment was dropped entirely.\n\n{sorted(loaded)}"
    )
    assert _bodies([loaded[_INROOT_ID]]) == [_INROOT_SENTINEL], (
        f"the alias overwrote the real entry with something else.\n\n{loaded}"
    )


def test_a_link_inside_the_vault_but_outside_the_fragments_root_is_dropped(
    tmp_path: Path,
) -> None:
    """The guard is judged against the fragments ROOT, not the vault. Deliberate.

    A ``01-Fragments`` entry symlinked to a note elsewhere in the same vault --
    ``09-Reference`` here -- resolves inside the vault yet outside the root the
    guard is given, so it is dropped. That is the ACCEPTED NARROWING
    :func:`~creek.vault.reader.iter_vault_fragments` documents, and it is
    precisely what makes these loaders agree with the tier survey: the survey
    drops it too, so admitting it here would recreate the divergence #1789
    closed, pointing the other way.

    Pinned because it is the one shape a future reader is most likely to
    "repair": adversarial review measured it live -- pre-fix both loaders
    returned this id, post-fix neither does -- and nothing else in this module
    would fail if someone widened the guard to the vault root.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    vault = tmp_path / "vault"
    fragments = vault / "01-Fragments"
    reference = vault / "09-Reference"
    _write(fragments, _fragment(_INROOT_ID, "In root"), _INROOT_SENTINEL)
    elsewhere = _write(reference, _fragment("ref-note", "Reference"), "REF-BODY")
    link = fragments / "ref-note.md"
    link.symlink_to(elsewhere)
    assert link.is_symlink(), "the fixture is not a symlink; it proves nothing."
    assert vault in link.resolve().parents, (
        "the fixture must resolve INSIDE the vault -- that is the whole point; "
        "if it escapes the vault it is just another escape test."
    )

    mined = _load_fragments(fragments)
    drafted = _load_fragments_by_id(fragments)

    assert [f.id for f, _ in mined] == [_INROOT_ID], (
        "a link out of 01-Fragments but inside the vault was admitted by the "
        "mining loader. The tier survey drops it, so admitting it here "
        f"reopens the #1789 divergence.\n\nloaded={_bodies(mined)}"
    )
    assert list(drafted) == [_INROOT_ID], (
        "same, for the draft loader.\n\nloaded=" + str(sorted(drafted))
    )
