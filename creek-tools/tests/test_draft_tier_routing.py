"""``creek draft`` keys its LLM by the tier of the fragments it drafts from (#1031).

``creek/cli.py`` built the draft client with
``config.model_router.resolve("classification")`` — **no tier** — and
:meth:`~creek.classify.llm.router.ModelRouter._enforce_local_for_intimate`
documents its own behaviour as "non-``Intimate`` tiers (and ``None``) pass
through unchanged". The ``Intimate``-never-cloud chokepoint (#647) was
therefore a no-op on the one draft path that renders whole fragment
*bodies*: ``--include-tier intimate|all`` admits an intimate fragment and
``_render_fragment_section`` puts its title *and* body straight into the
prompt.

It also called the bare ``load_config()`` rather than
``_load_config_for_vault(vault)``, so ``creek draft --vault <v>`` routed on
whichever config the working directory exposed and ignored the vault's own
routing policy. That second defect is why the tests here pin the resolved
**model name** and not only the provider: a vault config that never reached
the router would still produce a local provider — from the built-in defaults
— and an assertion that only read ``provider`` would pass for the wrong
reason.

THE ANTI-VACUITY RULE for everything below, inherited from
``tests/test_compile.py``'s #962 suite: a test asserting "intimate content
routes local" passes for free on a config whose stage is already local.
Every routing test here therefore uses ``classification=anthropic`` (cloud)
with ``default=ollama`` (local) AND asserts explicitly that the *unrouted*
resolution really would have picked the cloud one. Without that line the
test proves nothing; do not drop it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import load_config
from creek.generate.drafts import DraftGenerator
from creek.models import PrivacyTier
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import LLMConfig

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _flatten(text: str) -> str:
    """Return *text* with ANSI escapes stripped and all whitespace collapsed.

    Rich both colours and hard-wraps console output, so a substring assertion
    against a multi-sentence refusal is otherwise a test of the terminal
    width rather than of the message.

    Args:
        text: Raw captured CLI output.

    Returns:
        The single-spaced, escape-free text.
    """
    return " ".join(_ANSI_ESCAPE_RE.sub("", text).split())


_INTIMATE_ID = "frag-intimate"
_INTIMATE_TITLE = "Therapy session with Dana"
_INTIMATE_BODY = "SENTINEL-the-intimate-body-that-must-never-leave-this-machine"
_OPEN_ID = "frag-open"
_OPEN_TITLE = "A public note on rivers"
_OPEN_BODY = "Rivers braid where the gradient slackens; the creek is no different."

_VAULT_LOCAL_MODEL = "vault-local-model"
_VAULT_CLOUD_MODEL = "vault-cloud-model"
_CWD_LOCAL_MODEL = "cwd-local-model"
_CWD_CLOUD_MODEL = "cwd-cloud-model"


def _write_routing_config(
    path: Path,
    *,
    default_model: str,
    classification_model: str,
) -> Path:
    """Write a two-stage routing config: local ``default``, cloud ``classification``.

    Args:
        path: Destination config file (parents are created).
        default_model: Model name for the local ``default`` stage — the
            backend the ``Intimate``-never-cloud rule redirects to.
        classification_model: Model name for the cloud ``classification``
            stage, which is the stage ``creek draft`` resolves.

    Returns:
        The path written, for the anti-vacuity ``load_config`` assertion.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "llm:\n"
        f"  default:\n    provider: ollama\n    model: {default_model}\n"
        f"  classification:\n"
        f"    provider: anthropic\n    model: {classification_model}\n",
        encoding="utf-8",
    )
    return path


class _RecordingClassifier:
    """An ``LLMClassifier`` stand-in recording the config and prompts it saw.

    The provider *name* alone would not show that anything sensitive was at
    stake, so the prompts are kept too: ``_render_fragment_section`` emits
    ``### {id}: {title}`` plus the body, and the body is the payload this
    issue is about.
    """

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


def _seed_vault(tmp_path: Path, *, tier: str = "intimate") -> Path:
    """Scaffold a vault holding one source fragment at *tier*.

    Args:
        tmp_path: Pytest tmp dir.
        tier: ``privacy_tier`` written into the fragment's frontmatter.

    Returns:
        The vault root, with its routing config already written.
    """
    vault = tmp_path / "vault"
    for sub in ("01-Fragments/Notes", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    frag_id, title, body = (
        (_INTIMATE_ID, _INTIMATE_TITLE, _INTIMATE_BODY)
        if tier == "intimate"
        else (_OPEN_ID, _OPEN_TITLE, _OPEN_BODY)
    )
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        frag_id,
        title,
        body=body,
        privacy_tier=tier,
    )
    _write_routing_config(
        vault / "00-Creek-Meta" / "creek_config.yaml",
        default_model=_VAULT_LOCAL_MODEL,
        classification_model=_VAULT_CLOUD_MODEL,
    )
    return vault


def _assert_unrouted_resolution_is_cloud(vault: Path) -> None:
    """Pin that this vault's config resolves ``classification`` to the cloud.

    The anti-vacuity half of every routing assertion below: without it,
    "intimate routed local" would pass for free on a config whose
    classification stage was local to begin with.

    Args:
        vault: Vault whose ``creek_config.yaml`` is under test.
    """
    config = load_config(vault / "00-Creek-Meta" / "creek_config.yaml")
    unrouted = config.model_router.resolve("classification")
    assert unrouted.provider == "anthropic"
    assert unrouted.model == _VAULT_CLOUD_MODEL


def _stub_ontology_detector(monkeypatch: pytest.MonkeyPatch) -> list[LLMConfig]:
    """Stub the outline path's ontology detector, recording its resolved config.

    Args:
        monkeypatch: Pytest's patcher.

    Returns:
        The list the stub appends each resolved :class:`LLMConfig` to, so a
        test can pin that this deliberately tier-less site stayed tier-less.
    """
    from creek import cli as cli_module
    from creek.classify.prompt import PromptOntology

    seen: list[LLMConfig] = []

    def _build(vault: Path | None) -> Any:
        """Return a detector closure over the vault-resolved config."""
        config = cli_module._load_config_for_vault(vault)

        def _detect(prompt: str) -> PromptOntology:
            """Record the config this detection would have used."""
            seen.append(config.model_router.resolve("classification"))
            return PromptOntology(prompt=prompt)

        return _detect

    monkeypatch.setattr(cli_module, "_build_ontology_detector", _build)
    return seen


def _stub_miner(monkeypatch: pytest.MonkeyPatch, frag_id: str, title: str) -> None:
    """Force the mined path to surface exactly one seed over *frag_id*.

    Args:
        monkeypatch: Pytest's patcher.
        frag_id: The fragment the surfaced seed draws on.
        title: Title for the surfaced seed.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    seed = IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title=title,
        source_fragments=(frag_id,),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="A draft whose routing must be keyed by its source.",
        score=0.9,
    )
    monkeypatch.setattr(
        "creek.generate.mining.IdeaMiner.mine_all",
        lambda _self, _vault, *, current_phase=None: [seed],
    )


_OUTLINE_TEXT = "## Therapy\n"
"""A heading-only outline whose seed text is a substring of the intimate title.

``OutlineSection.seed_text`` is ``heading + body``, and
``_topic_matches_fragment`` is a whole-string substring match, so a section
with a body would retrieve nothing and compose as a source-less *bare*
section — which routes ``OPEN`` quite correctly and would make this suite
assert nothing about intimate content.
"""


_ENTRY_PATHS: list[tuple[str, list[str]]] = [
    ("mined", []),
    ("seeded", ["--seed-fragment", _INTIMATE_ID]),
    ("outline", ["--seed-outline-text", _OUTLINE_TEXT]),
]


@pytest.mark.parametrize(
    ("path_name", "extra_argv"), _ENTRY_PATHS, ids=[name for name, _ in _ENTRY_PATHS]
)
def test_cli_draft_routes_an_intimate_source_to_the_local_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
    path_name: str,
    extra_argv: list[str],
) -> None:
    """Every ``creek draft`` entry path keys the client with its sources' tier.

    The flagship regression, run once per entry path (mined seed, ``--seed-*``
    spec, outline section) because all three resolve their sources
    differently — and all three share the single build site the fix moved.

    Assertion (c) is the half that shows what was at stake: the intimate
    *body* is in the prompt, verbatim, so this was live egress payload rather
    than an id or a title.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
        path_name: Entry-path id (unused; names the parametrize row).
        extra_argv: The flags that select this entry path.
    """
    del path_name
    vault = _seed_vault(tmp_path)
    _stub_ontology_detector(monkeypatch)
    _stub_miner(monkeypatch, _INTIMATE_ID, _INTIMATE_TITLE)

    # (a) ANTI-VACUITY: with no tier, this config resolves to the CLOUD.
    _assert_unrouted_resolution_is_cloud(vault)

    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--include-tier", "all", *extra_argv],
    )

    assert result.exit_code == 0, result.output
    # (b) the tier was threaded, so the router redirected to the local default
    # — and to the *vault's* local default, not the built-in one.
    assert recorder.providers() == ["ollama"]
    assert recorder.models() == [_VAULT_LOCAL_MODEL]
    # (c) and what stayed local is the real payload, not a placeholder.
    prompts = recorder.all_prompts()
    assert prompts, "the draft never reached a provider at all"
    assert any(_INTIMATE_BODY in prompt for prompt in prompts)


def test_cli_draft_routes_on_the_vaults_config_not_the_process_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """``--vault``'s config decides the routing, not the working directory's.

    ``_build_draft_llm`` called the bare ``load_config()``, which falls back
    to ``./creek_config.yaml`` — so the provider for a draft over somebody
    else's vault was chosen by whatever directory the operator happened to
    run from, and the function's own docstring claim that the command
    "pre-warms ``CREEK_CONFIG`` resolution" was simply false: nothing sets
    that variable.

    The models, not the providers, are what make this test mean something.
    Both configs route ``classification`` to a cloud provider and ``default``
    locally, so *either* config produces ``ollama`` for an intimate source
    and a provider-only assertion would pass while reading the wrong file.

    (``CREEK_CONFIG`` is not used here: per #322 it deliberately *outranks*
    ``--vault``, so setting it would be asserting the opposite contract.)

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
    """
    vault = _seed_vault(tmp_path)
    _write_routing_config(
        tmp_path / "elsewhere" / "creek_config.yaml",
        default_model=_CWD_LOCAL_MODEL,
        classification_model=_CWD_CLOUD_MODEL,
    )
    monkeypatch.chdir(tmp_path / "elsewhere")

    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-fragment",
            _INTIMATE_ID,
            "--include-tier",
            "all",
        ],
    )

    assert result.exit_code == 0, result.output
    assert recorder.models() == [_VAULT_LOCAL_MODEL]
    assert _CWD_LOCAL_MODEL not in recorder.models()
    assert _CWD_CLOUD_MODEL not in recorder.models()


def test_cli_draft_keeps_open_sources_on_the_configured_cloud_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """An ``open`` source still routes to the stage the operator configured.

    The over-correction guard. Keying by the *ceiling* instead of by the
    content — ``creek_mcp.tier_ceiling`` maps ``ALL -> INTIMATE`` — would
    force every ``--include-tier all`` draft onto the local model and read as
    a passing privacy test while quietly breaking the operator's config. Only
    the intimate tier is gated (#647); nothing else changes.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
    """
    vault = _seed_vault(tmp_path, tier="open")
    _stub_miner(monkeypatch, _OPEN_ID, _OPEN_TITLE)

    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--include-tier", "all"],
    )

    assert result.exit_code == 0, result.output
    assert recorder.providers() == ["anthropic"]
    assert recorder.models() == [_VAULT_CLOUD_MODEL]


def test_cli_draft_refuses_intimate_when_no_local_backend_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """An all-cloud config plus an intimate source refuses, and builds nothing.

    Keying the call by tier *introduces* this failure mode, so it has to read
    as the guarantee working rather than as a crash — the same treatment
    ``creek compile`` gives it (#962). That no classifier was constructed is
    the evidence nothing egressed: the client is the last step before a real
    backend exists, so an empty registry means the router refused first.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
    """
    del monkeypatch
    vault = _seed_vault(tmp_path)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "llm:\n"
        "  default:\n    provider: anthropic\n    model: cloud-default\n"
        "  classification:\n    provider: anthropic\n    model: cloud-stage\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--seed-fragment",
            _INTIMATE_ID,
            "--include-tier",
            "all",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "no local backend to fall back to" in _flatten(result.output)
    assert "Traceback" not in result.output
    assert recorder.built == []


def test_cli_draft_leaves_the_ontology_detector_tier_less(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """``_build_ontology_detector`` keeps resolving with no tier, on purpose.

    It is fed only ``--seed-outline-text`` — the operator's own copy — so no
    classified fragment reaches that provider and there is no tier for the
    router to enforce. Sweeping it up alongside the real fix would send
    outline detection to the local model for no privacy gain, so the
    deliberate no-tier site is pinned here rather than left to a comment.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture (the draft client itself).
    """
    vault = _seed_vault(tmp_path)
    detector_configs = _stub_ontology_detector(monkeypatch)

    result = runner.invoke(
        app,
        [
            "draft",
            "--vault",
            str(vault),
            "--include-tier",
            "all",
            "--seed-outline-text",
            _OUTLINE_TEXT,
        ],
    )

    assert result.exit_code == 0, result.output
    assert detector_configs, "the outline path never detected an ontology"
    assert all(config.provider == "anthropic" for config in detector_configs)
    # ... while the draft client beside it went local on the same run, on the
    # vault's own local model rather than a built-in default.
    assert recorder.providers() == ["ollama"]
    assert recorder.models() == [_VAULT_LOCAL_MODEL]


class _TierRecordingFactory:
    """A :data:`~creek.generate.drafts.DraftLLMFactory` recording every tier."""

    def __init__(self, body: str = "A body.\n\nAnd another paragraph.") -> None:
        """Start with no recorded calls.

        Args:
            body: Text every built client returns.
        """
        self.tiers: list[PrivacyTier] = []
        self.calls: list[tuple[PrivacyTier, str]] = []
        self._body = body

    def __call__(self, tier: PrivacyTier) -> Any:
        """Record *tier* and return a client that records its prompts too."""
        self.tiers.append(tier)

        def _client(prompt: str) -> str:
            """Record the ``(tier, prompt)`` pair and return the canned body."""
            self.calls.append((tier, prompt))
            return self._body

        return _client


def _generator(factory: _TierRecordingFactory, skills_root: Path) -> DraftGenerator:
    """Build a generator wired to *factory* with the intimate ceiling lifted.

    Args:
        factory: The tier-recording factory under test.
        skills_root: Any directory; the skill stack is empty in these tests.

    Returns:
        The configured :class:`DraftGenerator`.
    """
    from creek.classify.privacy_filter import PrivacyTierOverride

    return DraftGenerator(
        llm_factory=factory,
        skills_root=skills_root,
        privacy_override=PrivacyTierOverride.ALL,
    )


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ((("frag-a-int", "intimate"),), PrivacyTier.INTIMATE),
        ((("frag-a-open", "open"),), PrivacyTier.OPEN),
        (
            (("frag-a-open", "open"), ("frag-b-int", "intimate")),
            PrivacyTier.INTIMATE,
        ),
    ],
    ids=["intimate-only", "open-only", "open-first-then-intimate"],
)
def test_generator_keys_the_factory_with_the_max_source_tier(
    tmp_path: Path,
    sources: tuple[tuple[str, str], ...],
    expected: PrivacyTier,
) -> None:
    """The generator reduces its sources' tiers with ``max_source_tier``.

    The mixed row is the one that bites: its ids sort so ``open`` comes first
    in both the seed's order and the vault walk, which means a "tier of the
    first source" implementation returns ``OPEN`` and fails here while a
    ``max`` implementation returns ``INTIMATE``. One intimate fragment among a
    hundred open ones has to pin the whole prompt to the local model.

    Args:
        tmp_path: Pytest tmp dir.
        sources: ``(fragment_id, privacy_tier)`` pairs to seed and draft from.
        expected: The tier the factory must be keyed with.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    for frag_id, tier in sources:
        write_raw_fragment_file(
            vault,
            "01-Fragments/Notes",
            frag_id,
            f"Title of {frag_id}",
            body=f"body of {frag_id}",
            privacy_tier=tier,
        )
    factory = _TierRecordingFactory()
    idea = IdeaSeed(
        strategy=MiningStrategy.RESONANCE_CHAIN,
        title="A draft",
        source_fragments=tuple(frag_id for frag_id, _ in sources),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="Sources of mixed tiers.",
        score=0.5,
    )

    _generator(factory, tmp_path / "skills").generate_draft(idea, vault_path=vault)

    assert factory.tiers == [expected]
    assert None not in factory.tiers


def test_generator_fails_closed_when_named_sources_do_not_resolve(
    tmp_path: Path,
) -> None:
    """Ids that were named but resolved to nothing route ``INTIMATE``.

    "Named but unresolved" is not "nothing named": with no evidence about
    what a prompt carries, the safe assumption is the worst one. The
    reduction is
    :func:`creek.classify.privacy_filter.max_source_tier`'s, shared with
    ``creek.compile`` and the MCP draft tool so the three cannot drift.

    Args:
        tmp_path: Pytest tmp dir.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    factory = _TierRecordingFactory()
    idea = IdeaSeed(
        strategy=MiningStrategy.RESONANCE_CHAIN,
        title="A draft",
        source_fragments=("frag-that-is-not-there",),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="A named source the vault does not hold.",
        score=0.5,
    )

    _generator(factory, tmp_path / "skills").generate_draft(idea, vault_path=vault)

    assert factory.tiers == [PrivacyTier.INTIMATE]


def test_generator_routes_a_source_less_prompt_as_open(tmp_path: Path) -> None:
    """A prompt naming no fragment at all is not forced local.

    An ontology-only seed renders no vault content, so failing it closed
    would push those drafts onto the local model — or refuse them outright on
    an all-cloud config — for no privacy gain whatsoever. The distinction
    against the unresolved case above has to be made before the survey runs,
    because both look like "no tiers found" afterwards.

    Args:
        tmp_path: Pytest tmp dir.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    factory = _TierRecordingFactory()
    idea = IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title="An unexplored corner",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="Built from ontology labels, not from fragments.",
        score=0.5,
    )

    _generator(factory, tmp_path / "skills").generate_draft(idea, vault_path=vault)

    assert factory.tiers == [PrivacyTier.OPEN]


def test_generator_routes_the_outline_stitch_at_the_whole_essays_tier(
    tmp_path: Path,
) -> None:
    """The stitch carries every section's body, so it routes at their maximum.

    The outline path composes one prompt per section and then a stitch over
    the composed bodies. Keying the stitch by whichever section happened to
    compose last would send an essay distilled from intimate sources to the
    cloud on the strength of its final, open section.

    Args:
        tmp_path: Pytest tmp dir.
    """
    from creek.classify.prompt import PromptOntology

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        _INTIMATE_ID,
        _INTIMATE_TITLE,
        body="A session about grief and Dana.",
        privacy_tier="intimate",
    )
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        _OPEN_ID,
        _OPEN_TITLE,
        body="Rivers braid where the gradient slackens.",
        privacy_tier="open",
    )
    factory = _TierRecordingFactory()

    _generator(factory, tmp_path / "skills").generate_outline_draft(
        "## grief\n\n## rivers\n",
        vault_path=vault,
        detect_ontology=lambda prompt: PromptOntology(prompt=prompt),
    )

    assert PrivacyTier.INTIMATE in factory.tiers
    # The stitch is the last call, and it inherits the whole essay's tier.
    assert factory.calls[-1][0] == PrivacyTier.INTIMATE


def test_a_prebuilt_llm_skips_the_tier_survey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm=`` costs no vault survey — its caller already routed the client.

    ``creek_mcp.tools.draft`` derives the tier itself (#958) and every test
    double comes in the same way, so re-deriving it here would be a second
    walk of ``01-Fragments`` on every draft *and* would silently override a
    routing decision that was already taken. The exploding stub is what
    proves the survey is skipped rather than merely ignored.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    def _explode(*_args: object, **_kwargs: object) -> dict[str, PrivacyTier]:
        """Fail loudly if the survey runs for a pre-built client."""
        msg = "resolved_source_tiers must not run when a built llm was supplied"
        raise AssertionError(msg)

    # ``setattr`` on a string target raises if the name is absent, so this
    # also pins that the module still imports the survey it is meant to skip.
    monkeypatch.setattr("creek.generate.drafts.resolved_source_tiers", _explode)
    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        _OPEN_ID,
        _OPEN_TITLE,
        body=_OPEN_BODY,
        privacy_tier="open",
    )
    generator = DraftGenerator(
        llm=lambda _prompt: "A body.\n\nAnd another paragraph.",
        skills_root=tmp_path / "skills",
    )
    idea = IdeaSeed(
        strategy=MiningStrategy.RESONANCE_CHAIN,
        title="A draft",
        source_fragments=(_OPEN_ID,),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="One open source.",
        score=0.5,
    )

    draft = generator.generate_draft(idea, vault_path=vault)

    assert draft.body.startswith("A body.")


def test_an_unbound_generator_routes_intimate(tmp_path: Path) -> None:
    """Before any source is bound, the routing tier is the most restrictive.

    Reading the private attribute is deliberate: this is the fail-closed
    default, and a future path that forgets to bind its sources must egress
    nothing rather than inherit ``None`` and switch the #647 gate off again.

    Args:
        tmp_path: Pytest tmp dir.
    """
    generator = _generator(_TierRecordingFactory(), tmp_path / "skills")

    assert generator._routing_tier is PrivacyTier.INTIMATE


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"llm": lambda _p: "body", "llm_factory": lambda _t: lambda _p: "body"},
    ],
    ids=["neither", "both"],
)
def test_generator_requires_exactly_one_client_seam(
    tmp_path: Path,
    kwargs: dict[str, Any],
) -> None:
    """Neither seam and both seams are errors, not defaults.

    Defaulting either way would be worse than failing: with no client the
    generator cannot draft, and honouring one of two would silently discard
    the caller's routing decision.

    Args:
        tmp_path: Pytest tmp dir.
        kwargs: The client-seam kwargs under test.
    """
    with pytest.raises(ValueError, match="llm"):
        DraftGenerator(skills_root=tmp_path / "skills", **kwargs)


_FILTER_MODES: list[tuple[str, list[str], str, str, bool]] = [
    ("default-filter", [], "anthropic", _VAULT_CLOUD_MODEL, False),
    ("include-tier-all", ["--include-tier", "all"], "ollama", _VAULT_LOCAL_MODEL, True),
]
"""``(id, argv, provider, model, body_in_prompt)`` per ``--include-tier`` mode."""


@pytest.mark.parametrize(
    ("extra_argv", "expected_provider", "expected_model", "body_in_prompt"),
    [row[1:] for row in _FILTER_MODES],
    ids=[row[0] for row in _FILTER_MODES],
)
def test_cli_draft_routes_by_what_the_filter_admitted_not_by_what_was_named(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
    extra_argv: list[str],
    expected_provider: str,
    expected_model: str,
    body_in_prompt: bool,
) -> None:
    """The survey follows the prompt, not the seed's id list.

    Both rows draft from the *same* seed over the *same* intimate fragment;
    only ``--include-tier`` differs. Under the default filter that fragment is
    excluded before the prompt is composed — a mined seed names ids off
    threads and resonances, which are tier-blind — so the prompt carries no
    intimate text and must still reach the stage the operator configured.
    Surveying the named ids instead refuses a draft on a leak that is not
    happening, and the default filter is the *common* invocation.

    The ``include-tier all`` row is the other half of the same pair: the one
    argument that changed admits the body, and the routing changes with it.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
        extra_argv: The ``--include-tier`` flag (or none) under test.
        expected_provider: Provider the built client must carry.
        expected_model: Model the built client must carry.
        body_in_prompt: Whether the intimate body reaches the prompt at all.
    """
    vault = _seed_vault(tmp_path)
    _stub_miner(monkeypatch, _INTIMATE_ID, _INTIMATE_TITLE)

    # ANTI-VACUITY: with no tier, this config resolves to the CLOUD.
    _assert_unrouted_resolution_is_cloud(vault)

    result = runner.invoke(app, ["draft", "--vault", str(vault), *extra_argv])

    assert result.exit_code == 0, result.output
    assert recorder.providers() == [expected_provider]
    assert recorder.models() == [expected_model]
    prompts = recorder.all_prompts()
    assert prompts, "the draft never reached a provider at all"
    # The routing above is only defensible because of what the prompt holds:
    # the cloud row is cloud *because* the filter took the body out.
    assert any(_INTIMATE_BODY in prompt for prompt in prompts) is body_in_prompt


def test_cli_draft_does_not_refuse_when_the_filter_removed_the_intimate_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
) -> None:
    """An all-cloud config plus a *filtered-out* intimate source still drafts.

    The sharp edge of the same defect: on a config with no local stage, a
    survey of the named ids turns "the filter already removed that fragment"
    into ``IntimateRoutingError`` and exit 1 — a refusal citing an egress that
    the privacy filter had prevented three steps earlier, on the default
    invocation. Refusing a draft that leaks nothing is not a safe direction to
    fail in; it is a broken command.

    Contrast ``test_cli_draft_refuses_intimate_when_no_local_backend_exists``,
    which is the same vault and the same config with ``--include-tier all``:
    there the body really is in the prompt and the refusal is correct. The
    pair is the whole distinction.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture.
    """
    vault = _seed_vault(tmp_path)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "llm:\n"
        "  default:\n    provider: anthropic\n    model: cloud-default\n"
        "  classification:\n    provider: anthropic\n    model: cloud-stage\n",
        encoding="utf-8",
    )
    _stub_miner(monkeypatch, _INTIMATE_ID, _INTIMATE_TITLE)

    result = runner.invoke(app, ["draft", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "no local backend to fall back to" not in _flatten(result.output)
    assert recorder.models() == ["cloud-stage"]
    assert not any(_INTIMATE_BODY in prompt for prompt in recorder.all_prompts())


_ABSENT_ID = "frag-that-is-not-there"

_DEFAULT_FILTER_ROWS: list[
    tuple[str, tuple[tuple[str, str | None], ...], tuple[str, ...], PrivacyTier]
] = [
    (
        "filtered-out-only",
        (("frag-int", "intimate"),),
        ("frag-int",),
        PrivacyTier.OPEN,
    ),
    (
        "filtered-out-plus-absent",
        (("frag-int", "intimate"),),
        ("frag-int", _ABSENT_ID),
        PrivacyTier.INTIMATE,
    ),
    (
        "filtered-out-plus-admitted-open",
        (("frag-int", "intimate"), ("frag-op", "open")),
        ("frag-int", "frag-op"),
        PrivacyTier.OPEN,
    ),
    (
        "admitted-personal-summary",
        (("frag-per", "personal"),),
        ("frag-per",),
        PrivacyTier.PERSONAL,
    ),
    (
        "admitted-untiered-fails-closed",
        (("frag-untiered", None),),
        ("frag-untiered",),
        PrivacyTier.INTIMATE,
    ),
]
"""``(id, seeded fragments, ids the seed names, expected tier)`` per row."""


@pytest.mark.parametrize(
    ("seeded", "named", "expected"),
    [row[1:] for row in _DEFAULT_FILTER_ROWS],
    ids=[row[0] for row in _DEFAULT_FILTER_ROWS],
)
def test_generator_surveys_only_what_the_default_filter_admitted(
    tmp_path: Path,
    seeded: tuple[tuple[str, str | None], ...],
    named: tuple[str, ...],
    expected: PrivacyTier,
) -> None:
    """Under the default filter the survey follows the prompt, id by id.

    The three cases a named id falls into must stay three answers:

    * **filtered out** — resolves, but the privacy filter dropped it before
      the prompt was composed, so it contributes nothing (rows 1 and 3);
    * **absent** — resolves to nothing, so nothing is known and it fails
      closed (row 2, which is row 1 plus one unresolvable id and flips the
      whole answer — that is the "do not collapse these two" assertion);
    * **admitted** — its text is in the prompt, so it contributes its own
      tier, whether that is a personal fragment's title-only summary (row 4)
      or an untiered fragment (row 5).

    Row 5 is why the tiers are read from the raw-frontmatter survey and not
    off the admitted map: :class:`~creek.models.Fragment` defaults a *missing*
    ``privacy_tier`` to ``unclassified``, which the filter admits as personal,
    while :func:`~creek.classify.privacy_filter.fragment_tier` calls a missing
    key ``INTIMATE``. Reading ``tier_of`` off the admitted fragment would
    route that body to the cloud.

    Args:
        tmp_path: Pytest tmp dir.
        seeded: ``(fragment_id, privacy_tier or None)`` pairs to write.
        named: The ids the seed declares as sources.
        expected: The tier the factory must be keyed with.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    vault = tmp_path / "vault"
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    for frag_id, tier in seeded:
        write_raw_fragment_file(
            vault,
            "01-Fragments/Notes",
            frag_id,
            f"Title of {frag_id}",
            body=f"body of {frag_id}",
            privacy_tier=tier,
        )
    factory = _TierRecordingFactory()
    generator = DraftGenerator(
        llm_factory=factory,
        skills_root=tmp_path / "skills",
    )
    idea = IdeaSeed(
        strategy=MiningStrategy.RESONANCE_CHAIN,
        title="A draft",
        source_fragments=named,
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="Sources the default filter treats differently.",
        score=0.5,
    )

    generator.generate_draft(idea, vault_path=vault)

    assert factory.tiers == [expected]


_MARKUP_RE = re.compile(r"\[/?[a-z ]+\]")

_REFUSAL_PATHS: list[tuple[str, list[str]]] = [
    ("seeded", ["--seed-fragment", _INTIMATE_ID]),
    ("outline", ["--seed-outline-text", _OUTLINE_TEXT]),
]


@pytest.mark.parametrize(
    "extra_argv",
    [row[1] for row in _REFUSAL_PATHS],
    ids=[row[0] for row in _REFUSAL_PATHS],
)
def test_cli_draft_refusal_prints_no_blank_line_before_exiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[_RecordingClassifier],
    extra_argv: list[str],
) -> None:
    """The routing refusal exits through the handlers, not into them.

    ``typer.Exit`` subclasses :class:`RuntimeError` — check
    ``click.exceptions.Exit.__mro__`` — and both ``_run_seeded_draft`` and
    ``_run_outline_draft`` catch ``RuntimeError`` to turn an empty LLM body
    into a legible exit. So the exit raised *inside* the generator, by the
    tier refusal this PR introduces, was caught by those handlers and
    re-printed as ``console.print(f"[red]{exc}[/red]")`` — and
    ``str(typer.Exit(code=1))`` is the empty string. The operator got a blank
    red line stapled to the refusal, and any future exit raised down there
    would have its code rewritten to 1 on the way out.

    Recording ``console.print`` rather than scraping the rendered output is
    deliberate: Rich hard-wraps, so a blank line in the capture could just be
    a wrap artefact, while a print call whose whole payload is markup cannot.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
        recorder: The classifier recorder fixture (nothing may be built).
        extra_argv: The flags selecting the entry path under test.
    """
    from creek import cli as cli_module

    vault = _seed_vault(tmp_path)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "llm:\n"
        "  default:\n    provider: anthropic\n    model: cloud-default\n"
        "  classification:\n    provider: anthropic\n    model: cloud-stage\n",
        encoding="utf-8",
    )
    _stub_ontology_detector(monkeypatch)
    printed: list[str] = []
    real_print = cli_module.console.print

    def _record(*args: Any, **kwargs: Any) -> None:
        """Forward to Rich after recording the first positional argument."""
        printed.append(str(args[0]) if args else "")
        real_print(*args, **kwargs)

    monkeypatch.setattr(cli_module.console, "print", _record)

    result = runner.invoke(
        app,
        ["draft", "--vault", str(vault), "--include-tier", "all", *extra_argv],
    )

    assert result.exit_code == 1, result.output
    stripped = [_MARKUP_RE.sub("", message).strip() for message in printed]
    assert stripped, "the command printed nothing at all"
    assert any("no local backend to fall back to" in line for line in stripped)
    assert "" not in stripped
    assert recorder.built == []


def _unavailable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every built ``LLMClassifier`` report its backend unreachable.

    Args:
        monkeypatch: Pytest's patcher.
    """
    from creek.classify.llm import LLMClassifier

    monkeypatch.setattr(LLMClassifier, "available", property(lambda _self: False))


def test_cli_draft_with_nothing_to_draft_never_touches_the_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that composes no prompt now exits 0 on an unreachable provider.

    A deliberate behaviour change, pinned rather than left to be discovered:
    the availability check moved from build time into the first prompt, so a
    vault with nothing to surface no longer fails on a backend it was never
    going to call. ``creek_mcp.tools.draft`` made the same choice in #958.
    The cost is that ``creek draft`` no longer doubles as a provider probe.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
    """
    _unavailable_provider(monkeypatch)
    vault = tmp_path / "vault"
    for sub in ("01-Fragments/Notes", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    _write_routing_config(
        vault / "00-Creek-Meta" / "creek_config.yaml",
        default_model=_VAULT_LOCAL_MODEL,
        classification_model=_VAULT_CLOUD_MODEL,
    )

    result = runner.invoke(app, ["draft", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "nothing to draft" in _flatten(result.output)
    assert "LLM provider unavailable" not in _flatten(result.output)


def test_cli_draft_reports_an_unreachable_provider_only_after_the_mine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unreachable-provider refusal now lands *after* the vault work.

    The other face of the same move: when there *is* something to draft, the
    operator pays the whole mine before being told the backend is down, where
    the pre-#1031 build-time check refused immediately. Exit code and message
    are unchanged; the ordering is the tell, so it is what this asserts.

    Args:
        tmp_path: Pytest tmp dir.
        monkeypatch: Pytest's patcher.
    """
    _unavailable_provider(monkeypatch)
    vault = _seed_vault(tmp_path, tier="open")
    _stub_miner(monkeypatch, _OPEN_ID, _OPEN_TITLE)

    result = runner.invoke(app, ["draft", "--vault", str(vault)])

    assert result.exit_code == 1, result.output
    flat = _flatten(result.output)
    assert "LLM provider unavailable" in flat
    assert _OPEN_TITLE in flat, "the mine's idea was never presented"
    assert flat.index(_OPEN_TITLE) < flat.index("LLM provider unavailable")
