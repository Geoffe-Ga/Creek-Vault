"""``creek draft`` (CLI) must account for compiled thread/eddy sources (#1538).

The CLI twin of #1013. Since #1031 ``creek draft`` keys its LLM by the tier of
the fragments its prompt renders, reducing over the seed's ``source_fragments``
plus the per-dimension slice ids. That survey is **compiled-layer-blind**: the
``## Threads`` and ``## Eddies`` blocks render *compiled-page bodies* —
synthesised text whose sources the seed never names — and neither
:class:`~creek.models.Thread` nor :class:`~creek.models.Eddy` carries a
``privacy_tier``, so those sections cannot be tier-*filtered*, only
tier-*accounted*.

So an ``open`` source fragment plus a thread whose compiled page was
synthesised from ``intimate`` fragments routed the whole prompt ``open`` — to
the cloud ``classification`` stage — while the prompt carried the compiled
distillation of intimate content. Same family as #931: a derived artifact
laundering above-ceiling content past a source-tier computation that cannot
see it.

THE ANTI-VACUITY RULE, inherited from ``tests/test_draft_tier_routing.py``: a
test asserting "intimate content routes local" passes for free on a config
whose stage is already local, and a test asserting a tier proves nothing if
the compiled text never reached the prompt. The CLI test below therefore
(a) pins that the *unrouted* resolution really is the cloud provider, and
(b) pins that the compiled body really is in the prompt. Do not drop either.

The fail-closed half matters just as much: a named thread whose compiled page
is **missing**, or whose page records **no provenance**, is *opaque*, not
*empty*. Reading "no provenance" as "no sources" would reopen the hole for
every page compiled before provenance existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.cli import app
from creek.compile.provenance import ProvenanceEntry
from creek.config import load_config
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.models import CompiledPage, PrivacyTier, Thread
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.draft import _source_routing_tier
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import LLMConfig

runner = CliRunner()

_THREAD_DIR = "02-Threads"
_EDDY_DIR = "03-Eddies"

_OPEN_ID = "frag-open"
_OTHER_OPEN_ID = "frag-open-two"
_INTIMATE_ID = "frag-intimate"
_GHOST_ID = "frag-that-was-deleted"

_THREAD_ID = "thread-confession"
_EDDY_ID = "eddy-confession"

_COMPILED_SENTINEL = "SENTINEL-the-compiled-distillation-of-an-intimate-life"
_THREAD_DESCRIPTION = "SENTINEL-the-thread-frontmatter-description"

_VAULT_LOCAL_MODEL = "vault-local-model"
_VAULT_CLOUD_MODEL = "vault-cloud-model"


def _write_routing_config(vault: Path) -> None:
    """Write a two-stage routing config: local ``default``, cloud ``classification``.

    ``creek draft`` resolves the ``classification`` stage, so a config whose
    classification provider is a cloud one is what makes "routed local" an
    observable event rather than a tautology.

    Args:
        vault: Vault root; the config lands in ``00-Creek-Meta``.
    """
    path = vault / "00-Creek-Meta" / "creek_config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "llm:\n"
        f"  default:\n    provider: ollama\n    model: {_VAULT_LOCAL_MODEL}\n"
        f"  classification:\n"
        f"    provider: anthropic\n    model: {_VAULT_CLOUD_MODEL}\n",
        encoding="utf-8",
    )


def _assert_unrouted_resolution_is_cloud(vault: Path) -> None:
    """Pin that this vault's config resolves ``classification`` to the cloud.

    The anti-vacuity half of the CLI assertion: without it, "routed local"
    would pass for free on a config whose classification stage was local to
    begin with.

    Args:
        vault: Vault whose ``creek_config.yaml`` is under test.
    """
    config = load_config(vault / "00-Creek-Meta" / "creek_config.yaml")
    unrouted = config.model_router.resolve("classification")
    assert unrouted.provider == "anthropic"
    assert unrouted.model == _VAULT_CLOUD_MODEL


def _write_compiled_page(
    vault: Path,
    *,
    reldir: str,
    target_kind: str,
    target_id: str,
    fragment_ids: list[str] | None,
    body: str = _COMPILED_SENTINEL,
) -> None:
    """Write a compiled page under *reldir* whose provenance names *fragment_ids*.

    Args:
        vault: Vault root.
        reldir: ``02-Threads`` or ``03-Eddies``.
        target_kind: ``thread`` or ``eddy``.
        target_id: The id the seed names.
        fragment_ids: Provenance fragment ids, or ``None`` for a page with
            **no** provenance entries at all — the shape that makes a page's
            sources unenumerable.
        body: The synthesised body text the prompt will render.
    """
    provenance = (
        []
        if fragment_ids is None
        else [
            ProvenanceEntry(
                claim_id="c1",
                claim_excerpt="A synthesised claim.",
                fragment_ids=fragment_ids,
                compiled_at=datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC),
                compile_method="llm",
            ),
        ]
    )
    page = CompiledPage(
        target_kind=target_kind,  # type: ignore[arg-type]  # Literal narrowed by caller
        target_id=target_id,
        title=f"Page {target_id}",
        body=body,
        provenance=provenance,
    )
    post = frontmatter.Post(content=body)
    post.metadata.update(page.model_dump(mode="json"))
    path = vault / reldir / f"{target_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _write_thread_note(vault: Path, thread_id: str) -> None:
    """Write a plain ``Thread`` note so the missing-page fallback renders text.

    Without it, a named-but-uncompiled thread would contribute no prompt text
    at all and the fail-closed assertion could be mistaken for a test of the
    empty case.

    Args:
        vault: Vault root.
        thread_id: The thread id the seed names.
    """
    thread = Thread(
        id=thread_id,
        title="A thread with no compiled page",
        description=_THREAD_DESCRIPTION,
    )
    post = frontmatter.Post(content=_THREAD_DESCRIPTION)
    post.metadata.update(thread.model_dump(mode="json"))
    path = vault / _THREAD_DIR / f"{thread_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _seed_vault(tmp_path: Path) -> Path:
    """Scaffold a vault holding one open and one intimate fragment.

    The intimate fragment is deliberately **not** named by any seed below: the
    only way it can reach a prompt is through a compiled page's synthesis of
    it, which is exactly the channel under test.

    Args:
        tmp_path: Pytest tmp dir.

    Returns:
        The vault root, with its routing config already written.
    """
    vault = tmp_path / "vault"
    for sub in ("01-Fragments/Notes", _THREAD_DIR, _EDDY_DIR, "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    for frag_id, tier in (
        (_OPEN_ID, "open"),
        (_OTHER_OPEN_ID, "open"),
        (_INTIMATE_ID, "intimate"),
    ):
        write_raw_fragment_file(
            vault,
            "01-Fragments/Notes",
            frag_id,
            f"Title of {frag_id}",
            body=f"body of {frag_id}",
            privacy_tier=tier,
        )
    _write_routing_config(vault)
    return vault


def _seed(
    *,
    source_fragments: tuple[str, ...] = (_OPEN_ID,),
    threads: tuple[str, ...] = (),
    eddies: tuple[str, ...] = (),
) -> IdeaSeed:
    """Build an :class:`~creek.generate.mining.IdeaSeed` naming the given ids.

    Args:
        source_fragments: Fragment ids the seed names directly.
        threads: Thread ids whose compiled pages the prompt will render.
        eddies: Eddy ids whose compiled pages the prompt will render.

    Returns:
        The seed.
    """
    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="A draft over a compiled thread",
        source_fragments=source_fragments,
        threads=threads,
        eddies=eddies,
        frequency_affinity=(),
        brief_description="A seed whose compiled sections carry hidden sources.",
        score=0.9,
    )


class _TierRecordingFactory:
    """A :data:`~creek.generate.drafts.DraftLLMFactory` recording tiers and prompts."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.tiers: list[PrivacyTier] = []
        self.prompts: list[str] = []

    def __call__(self, tier: PrivacyTier) -> Any:
        """Record *tier* and return a client that records its prompt too."""
        self.tiers.append(tier)

        def _client(prompt: str) -> str:
            """Record *prompt* and return a canned two-paragraph body."""
            self.prompts.append(prompt)
            return "A body the guard can measure.\n\nAnd a second paragraph."

        return _client


def _generator(
    factory: _TierRecordingFactory,
    skills_root: Path,
    *,
    bypass_compiled: bool = False,
) -> DraftGenerator:
    """Build a generator wired to *factory* with the intimate ceiling lifted.

    The ``ALL`` override is what keeps these tests about the *compiled* channel
    rather than about the fragment filter: every named fragment is admitted, so
    any tier the survey reports has to have come from the compiled sections.

    Args:
        factory: The tier-recording factory under test.
        skills_root: Any directory; the skill stack is empty in these tests.
        bypass_compiled: Mirrors ``creek draft --bypass-compiled``.

    Returns:
        The configured :class:`~creek.generate.drafts.DraftGenerator`.
    """
    return DraftGenerator(
        llm_factory=factory,
        skills_root=skills_root,
        privacy_override=PrivacyTierOverride.ALL,
        bypass_compiled=bypass_compiled,
    )


def _bound_tier(
    tmp_path: Path,
    vault: Path,
    idea: IdeaSeed,
) -> tuple[PrivacyTier, str]:
    """Draft *idea* and return the ``(tier, prompt)`` the generator produced.

    Args:
        tmp_path: Pytest tmp dir (supplies a skills root).
        vault: The vault to draft from.
        idea: The seed to draft.

    Returns:
        The single tier the factory was keyed with and the prompt it saw.
    """
    factory = _TierRecordingFactory()
    _generator(factory, tmp_path / "skills").generate_draft(idea, vault_path=vault)
    assert len(factory.tiers) == 1, factory.tiers
    return factory.tiers[0], factory.prompts[0]


class _RecordingClassifier:
    """An ``LLMClassifier`` stand-in recording the config and prompts it saw."""

    built: ClassVar[list[_RecordingClassifier]] = []

    def __init__(self, config: LLMConfig) -> None:
        """Record *config* and register this instance on the class."""
        self.config = config
        self.prompts: list[str] = []
        _RecordingClassifier.built.append(self)

    @property
    def available(self) -> bool:
        """Report the provider as reachable so the draft proceeds."""
        return True

    def invoke_prompt_with_metadata(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Record *prompt* and return a canned two-paragraph completion."""
        del max_tokens
        from creek.classify.llm.completion import Completion

        self.prompts.append(prompt)
        return Completion(
            text="A body the guard can measure.\n\nAnd a second paragraph.",
            stop_reason="end_turn",
        )

    @classmethod
    def providers(cls) -> list[str]:
        """Return the provider of every classifier built, in build order."""
        return [built.config.provider for built in cls.built]

    @classmethod
    def models(cls) -> list[str]:
        """Return the model of every classifier built, in build order."""
        return [built.config.model for built in cls.built]

    @classmethod
    def all_prompts(cls) -> list[str]:
        """Return every prompt every built classifier was sent."""
        return [prompt for built in cls.built for prompt in built.prompts]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClassifier]:
    """Swap ``LLMClassifier`` for the recorder and reset its registry.

    Args:
        monkeypatch: Pytest's patcher.

    Returns:
        The recorder class, for provider/model/prompt assertions.
    """
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv("CREEK_LLM", raising=False)
    _RecordingClassifier.built = []
    monkeypatch.setattr("creek.classify.llm.LLMClassifier", _RecordingClassifier)
    return _RecordingClassifier


def _stub_miner(monkeypatch: pytest.MonkeyPatch, idea: IdeaSeed) -> None:
    """Force the mined path to surface exactly *idea*.

    Args:
        monkeypatch: Pytest's patcher.
        idea: The single seed ``creek draft`` will draft.
    """
    monkeypatch.setattr(
        "creek.generate.mining.IdeaMiner.mine_all",
        lambda _self, _vault, *, current_phase=None: [idea],
    )


def test_cli_draft_routes_local_when_a_named_threads_page_is_intimate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """The flagship regression, on the **default** ``creek draft`` invocation.

    An ``open`` source fragment plus a thread whose compiled page was
    synthesised from an ``intimate`` fragment. No ``--include-tier`` flag: the
    hole is on the path an operator actually takes, and the intimate fragment
    is never *named*, so no fragment filter can see it.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=[_INTIMATE_ID],
    )
    _stub_miner(monkeypatch, _seed(threads=(_THREAD_ID,)))

    # (a) ANTI-VACUITY: with no tier, this config resolves to the CLOUD.
    _assert_unrouted_resolution_is_cloud(vault)

    result = runner.invoke(app, ["draft", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    # (b) the compiled provenance was accounted, so the router forced the
    # vault's own local backend.
    assert recorder.providers() == ["ollama"]
    assert recorder.models() == [_VAULT_LOCAL_MODEL]
    # (c) ANTI-VACUITY: and the compiled distillation really is in the prompt,
    # so this was live egress payload rather than a bookkeeping change.
    prompts = recorder.all_prompts()
    assert prompts, "the draft never reached a provider at all"
    assert any(_COMPILED_SENTINEL in prompt for prompt in prompts)


def test_generator_accounts_a_compiled_eddy_page_too(tmp_path: Path) -> None:
    """``## Eddies`` renders compiled bodies on exactly the same terms.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_EDDY_DIR,
        target_kind="eddy",
        target_id=_EDDY_ID,
        fragment_ids=[_INTIMATE_ID],
    )

    tier, prompt = _bound_tier(tmp_path, vault, _seed(eddies=(_EDDY_ID,)))

    assert tier == PrivacyTier.INTIMATE
    assert _COMPILED_SENTINEL in prompt


def test_generator_keeps_an_all_open_compiled_page_on_the_cloud_stage(
    tmp_path: Path,
) -> None:
    """The over-correction guard: accounting must not force *everything* local.

    A compiled page whose provenance is entirely ``open`` leaves the draft on
    whatever stage the operator configured. Without this row, "return
    ``INTIMATE`` always" would pass every other test in this file.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=[_OTHER_OPEN_ID],
    )

    tier, prompt = _bound_tier(tmp_path, vault, _seed(threads=(_THREAD_ID,)))

    assert tier == PrivacyTier.OPEN
    assert _COMPILED_SENTINEL in prompt


def test_generator_fails_closed_when_a_named_thread_has_no_compiled_page(
    tmp_path: Path,
) -> None:
    """A missing page is **opaque**, not empty — and it still renders text.

    The fallback puts the thread's frontmatter description into the prompt, so
    there is real unaccountable text at stake; ``Thread`` carries no
    ``privacy_tier`` for it either.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_thread_note(vault, _THREAD_ID)

    tier, prompt = _bound_tier(tmp_path, vault, _seed(threads=(_THREAD_ID,)))

    assert tier == PrivacyTier.INTIMATE
    assert _THREAD_DESCRIPTION in prompt


def test_generator_fails_closed_when_the_page_records_no_provenance(
    tmp_path: Path,
) -> None:
    """ "No provenance" is *unknown*, not *no sources*.

    Reading an empty provenance list as "this page has no sources" would
    reopen the hole for every page compiled before provenance was recorded.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=None,
    )

    tier, prompt = _bound_tier(tmp_path, vault, _seed(threads=(_THREAD_ID,)))

    assert tier == PrivacyTier.INTIMATE
    assert _COMPILED_SENTINEL in prompt


def test_generator_fails_closed_when_provenance_names_a_missing_fragment(
    tmp_path: Path,
) -> None:
    """A provenance id that no longer resolves contributes ``INTIMATE``.

    The page's text is in the prompt either way; a deleted or renamed source
    means nothing is *known* about part of it, which is the same evidence
    vacuum a named-but-absent ``source_fragment`` creates.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=[_GHOST_ID],
    )

    tier, _prompt = _bound_tier(tmp_path, vault, _seed(threads=(_THREAD_ID,)))

    assert tier == PrivacyTier.INTIMATE


def test_generator_fails_closed_under_bypass_compiled(tmp_path: Path) -> None:
    """``--bypass-compiled`` renders unenumerable text, so it routes local.

    Bypass skips the compiled index entirely and falls back to the thread's
    frontmatter description — text with no provenance record anywhere. The
    survey has no evidence, so it assumes the worst.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=[_OTHER_OPEN_ID],
    )
    _write_thread_note(vault, f"{_THREAD_ID}-plain")
    factory = _TierRecordingFactory()

    _generator(factory, tmp_path / "skills", bypass_compiled=True).generate_draft(
        _seed(threads=(f"{_THREAD_ID}-plain",)),
        vault_path=vault,
    )

    assert factory.tiers == [PrivacyTier.INTIMATE]
    assert _THREAD_DESCRIPTION in factory.prompts[0]


def test_generator_still_routes_a_thread_less_seed_by_its_fragments(
    tmp_path: Path,
) -> None:
    """A seed naming no thread or eddy is untouched by the new accounting.

    Args:
        tmp_path: Pytest tmp dir.
    """
    vault = _seed_vault(tmp_path)

    tier, _prompt = _bound_tier(tmp_path, vault, _seed())

    assert tier == PrivacyTier.OPEN


_Shape = tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], PrivacyTier]

_EQUIVALENCE_SHAPES: list[_Shape] = [
    ("open-fragment-only", (_OPEN_ID,), (), (), PrivacyTier.OPEN),
    ("intimate-fragment-only", (_INTIMATE_ID,), (), (), PrivacyTier.INTIMATE),
    ("no-sources-at-all", (), (), (), PrivacyTier.OPEN),
    ("open-plus-intimate-thread", (_OPEN_ID,), (_THREAD_ID,), (), PrivacyTier.INTIMATE),
    ("open-plus-open-eddy", (_OPEN_ID,), (), (_EDDY_ID,), PrivacyTier.OPEN),
    (
        "thread-with-no-page",
        (_OPEN_ID,),
        ("thread-uncompiled",),
        (),
        PrivacyTier.INTIMATE,
    ),
    (
        "eddy-with-no-provenance",
        (_OPEN_ID,),
        (),
        ("eddy-unprovenanced",),
        PrivacyTier.INTIMATE,
    ),
    ("compiled-sources-only", (), (_THREAD_ID,), (), PrivacyTier.INTIMATE),
]


@pytest.mark.parametrize(
    ("sources", "threads", "eddies", "expected"),
    [(s, t, e, x) for _name, s, t, e, x in _EQUIVALENCE_SHAPES],
    ids=[name for name, *_rest in _EQUIVALENCE_SHAPES],
)
def test_cli_and_mcp_derive_the_same_tier_shape_for_shape(
    tmp_path: Path,
    sources: tuple[str, ...],
    threads: tuple[str, ...],
    eddies: tuple[str, ...],
    expected: PrivacyTier,
) -> None:
    """The CLI and MCP surveys answer identically across a matrix of shapes.

    Both now reduce over one shared compiled-source survey
    (:func:`creek.generate.compile_routing.compiled_source_ids`); this pins
    that they cannot drift back apart. ``TierCeiling.OPEN`` is the ceiling
    that makes the MCP answer comparable — it contributes ``OPEN`` to the
    reconciliation, which is exactly the floor the ceiling-less CLI starts
    from.

    *expected* is asserted as well as the equality, because equality alone is
    satisfied by both surfaces being wrong in the same direction — "return
    ``INTIMATE`` always" would pass a pure equality matrix on every row.

    Args:
        tmp_path: Pytest tmp dir.
        sources: Fragment ids the seed names.
        threads: Thread ids the seed names.
        eddies: Eddy ids the seed names.
        expected: The tier both surfaces must independently arrive at.
    """
    vault = _seed_vault(tmp_path)
    _write_compiled_page(
        vault,
        reldir=_THREAD_DIR,
        target_kind="thread",
        target_id=_THREAD_ID,
        fragment_ids=[_INTIMATE_ID],
    )
    _write_compiled_page(
        vault,
        reldir=_EDDY_DIR,
        target_kind="eddy",
        target_id=_EDDY_ID,
        fragment_ids=[_OTHER_OPEN_ID],
    )
    _write_compiled_page(
        vault,
        reldir=_EDDY_DIR,
        target_kind="eddy",
        target_id="eddy-unprovenanced",
        fragment_ids=None,
    )
    _write_thread_note(vault, "thread-uncompiled")
    idea = _seed(source_fragments=sources, threads=threads, eddies=eddies)

    cli_tier, _prompt = _bound_tier(tmp_path, vault, idea)
    mcp_tier = _source_routing_tier(vault, idea, TierCeiling.OPEN)

    assert cli_tier == mcp_tier
    assert cli_tier == expected
