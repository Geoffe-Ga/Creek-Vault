"""``bench eval --corpus-vault``: the owner-run real-voice check (#1875).

The owner judges candidate models against his own vault, on his own machine.
Nothing here ever touches a real vault: every vault is a synthetic fixture
written under ``tmp_path``, every fragment body is invented, and every body and
fragment id carries a canary string so a test can prove where text did — and
did not — travel.

Four things are pinned:

- **Admission.** Only ``open`` and ``personal`` fragments that are
  voice-proxy eligible (self-authored, not AI-quarantined) reach the prompt.
  INTIMATE, explicitly untiered, tier-less, AI-authored and
  ``voice_weight: 0`` fragments do not — asserted on the prompt the injected
  provider actually receives.
- **Refusal.** The mode needs ``--mode live``, the ownership acknowledgement,
  a Creek vault, both private output paths, a hermetic judge, and no ``CI``
  variable. Each refusal happens before a provider is built.
- **Outputs.** The ``--out`` ranking is content-free (no fragment text, no
  fragment id, no candidate text); the candidate texts go to a ``0600`` review
  file under opaque labels, and the labels' key to a separate ``0600`` file.
- **Determinism.** One seed samples one corpus; the sample is bounded in both
  entry count and words.
"""

from __future__ import annotations

import argparse
import json
import socket
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.completion import Completion
from creek.classify.llm.router import IntimateRoutingError
from creek.config import LLMConfig
from creek.models import PrivacyTier
from creek_mcp.bench import cli
from creek_mcp.bench.eval import command
from creek_mcp.bench.eval.blinding import Candidate, blind, identity_map
from creek_mcp.bench.eval.candidates import FakeCandidateSource, routed_source
from creek_mcp.bench.eval.command import add_eval_arguments, plan_from_args
from creek_mcp.bench.eval.review import render_review, unblind_key, write_private
from creek_mcp.bench.eval.rubric import Basis
from creek_mcp.bench.eval.runner import EvalPlanError
from creek_mcp.bench.eval.vault_corpus import (
    VAULT_MAX_ENTRIES,
    VAULT_MAX_ENTRY_WORDS,
    VAULT_MAX_TOTAL_WORDS,
    VaultCorpusError,
    load_vault_corpus,
)
from creek_mcp.bench.eval.voice_corpus import MIN_ENTRIES

if TYPE_CHECKING:
    from pathlib import Path

_MODELS = "gpt-oss-120b,glm-5.3"
"""Example catalog model tags. Named, never dialled."""

_KEY_ENV = "CREEK_BENCH_EVAL_TEST_KEY_1875"
"""A variable name no test ever sets in the real environment."""

_CANDIDATE_CANARY = "CANDIDATECANARYQX"
"""Carried by every stub candidate, so the artifact can be searched for it."""

_FRAGMENT_CANARY = "FRAGCANARY"
"""Prefix of every fragment body's canary token."""

_ID_CANARY = "idcanary"
"""Carried by every fragment id, so an id leaking into the artifact shows."""

_OPEN = 12
_PERSONAL = 3


def _front_matter(
    ident: str,
    *,
    tier: str | None,
    author: str = "self",
    voice_weight: float | None = None,
) -> str:
    """Render model-valid fragment front matter as JSON-valued YAML."""
    metadata: dict[str, object] = {
        "type": "fragment",
        "id": ident,
        "title": ident,
        "created": "2026-01-01T00:00:00+00:00",
        "ingested": "2026-01-01T00:00:00+00:00",
        "source": {"platform": "journal", "author": author},
        "frequency": {"primary": "F1", "secondary": []},
        "eddies": [],
    }
    if tier is not None:
        metadata["privacy_tier"] = tier
    if voice_weight is not None:
        metadata["voice_weight"] = voice_weight
    return "\n".join(f"{key}: {json.dumps(value)}" for key, value in metadata.items())


def _write(
    vault: Path,
    label: str,
    index: int,
    *,
    tier: str | None,
    author: str = "self",
    voice_weight: float | None = None,
) -> None:
    """Write one fragment whose id and body both carry a canary."""
    ident = f"{_ID_CANARY}-{label}-{index:02d}"
    body = (
        f"I walked to the {label} gate. {_FRAGMENT_CANARY}{label.upper()}{index} "
        f"was written on the post. I went home and slept."
    )
    header = _front_matter(ident, tier=tier, author=author, voice_weight=voice_weight)
    notes = vault / "01-Fragments" / "Notes"
    (notes / f"{ident}.md").write_text(f"---\n{header}\n---\n{body}\n", "utf-8")


def _vault(tmp_path: Path) -> Path:
    """Build a synthetic Creek vault holding every tier and authorship shape."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    for index in range(_OPEN):
        _write(vault, "open", index, tier="open")
    for index in range(_PERSONAL):
        _write(vault, "personal", index, tier="personal")
    for index in range(2):
        _write(vault, "intimate", index, tier="intimate")
    _write(vault, "untiered", 0, tier="unclassified")
    _write(vault, "tierless", 0, tier=None)
    _write(vault, "aiauthored", 0, tier="open", author="ai")
    _write(vault, "quarantined", 0, tier="open", voice_weight=0.0)
    return vault


_EXCLUDED_BY_TIER = 4
"""Two intimate, one explicitly untiered, one with no tier at all."""

_EXCLUDED_BY_AUTHORSHIP = 2
"""One AI-authored, one ``voice_weight: 0`` (AI-quarantined)."""

_EXCLUDED_LABELS = ("INTIMATE", "UNTIERED", "TIERLESS", "AIAUTHORED", "QUARANTINED")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A fresh synthetic vault."""
    return _vault(tmp_path)


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything opens a socket or builds a real provider."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the corpus-vault eval reached for the network")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(command, "build_provider", _refuse)
    monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)


@dataclass
class RecordingProvider:
    """A provider double: records every prompt, answers in-voice, no socket."""

    model: str
    available: bool = True
    is_cloud: bool = True
    prompts: list[str] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Record *prompt* and answer with a canary-bearing fake reflection."""
        del max_tokens, system
        self.prompts.append(prompt)
        text = FakeCandidateSource(self.model).generate(prompt)
        return Completion(text=f"{text} {_CANDIDATE_CANARY} {self.model}")


@dataclass
class Factory:
    """Builds one :class:`RecordingProvider` per model and remembers them."""

    built: dict[str, RecordingProvider] = field(default_factory=dict)

    def __call__(self, config: LLMConfig) -> RecordingProvider:
        """Return the provider for *config*'s model."""
        tag = config.model or ""
        return self.built.setdefault(tag, RecordingProvider(tag))


def _argv(tmp_path: Path, vault: Path, *extra: str) -> list[str]:
    """Return a complete, valid corpus-vault argv, plus *extra*."""
    return [
        "--models",
        _MODELS,
        "--seed",
        "17",
        "--mode",
        "live",
        "--provider",
        "openai",
        "--api-base",
        "http://127.0.0.1:9/v1",
        "--api-key-env",
        _KEY_ENV,
        "--corpus-vault",
        str(vault),
        "--i-own-this-vault",
        "--review-out",
        str(tmp_path / "review.md"),
        "--unblind-out",
        str(tmp_path / "unblind.json"),
        "--out",
        str(tmp_path / "eval.json"),
        *extra,
    ]


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse ``eval`` flags with the registration :func:`cli.main` uses."""
    parser = argparse.ArgumentParser()
    add_eval_arguments(parser)
    return parser.parse_args(argv)


def _run(tmp_path: Path, vault: Path, *extra: str) -> tuple[str, Factory]:
    """Run the whole command through injected credentials and a stub factory."""
    factory = Factory()
    line = command.run_eval_command(
        _parse(_argv(tmp_path, vault, *extra)),
        environ={_KEY_ENV: "x"},
        provider_factory=factory,
    )
    return line, factory


@pytest.mark.usefixtures("sealed")
def test_only_open_and_personal_self_authored_prose_reaches_the_prompt(
    tmp_path: Path, vault: Path
) -> None:
    """INTIMATE, untiered, tier-less, AI-authored and quarantined never ship."""
    _, factory = _run(tmp_path, vault)
    prompts = [
        prompt for provider in factory.built.values() for prompt in provider.prompts
    ]
    assert len(prompts) == len(_MODELS.split(","))
    for prompt in prompts:
        for index in range(_OPEN):
            assert f"{_FRAGMENT_CANARY}OPEN{index}" in prompt
        for index in range(_PERSONAL):
            assert f"{_FRAGMENT_CANARY}PERSONAL{index}" in prompt
        for label in _EXCLUDED_LABELS:
            assert f"{_FRAGMENT_CANARY}{label}" not in prompt
        assert _ID_CANARY not in prompt


@pytest.mark.usefixtures("sealed")
def test_the_ranking_artifact_is_content_free(tmp_path: Path, vault: Path) -> None:
    """``--out`` keeps scores, verdicts, tags and counts — and no text at all."""
    _run(tmp_path, vault)
    raw = (tmp_path / "eval.json").read_text(encoding="utf-8")
    assert _FRAGMENT_CANARY not in raw
    assert _CANDIDATE_CANARY not in raw
    assert _ID_CANARY not in raw
    assert "walked" not in raw
    payload = json.loads(raw)
    assert payload["corpus_source"] == "vault"
    assert "generation_prompt" not in payload
    assert payload["corpus_entries"] == _OPEN + _PERSONAL
    assert payload["vault_counts"]["admitted"] == _OPEN + _PERSONAL
    assert payload["vault_counts"]["excluded_by_tier"] == _EXCLUDED_BY_TIER
    assert payload["vault_counts"]["excluded_by_authorship"] == _EXCLUDED_BY_AUTHORSHIP
    for row in payload["rows"]:
        assert row["model_tag"] in _MODELS.split(",")
        assert row["dimensions"]["voice_match"]["basis"] == Basis.JUDGE.value
        assert "requests" not in row["record"]
        assert row["record"]["judge_requests"] == 1


@pytest.mark.usefixtures("sealed")
def test_the_review_file_is_private_and_blinded(tmp_path: Path, vault: Path) -> None:
    """Candidate text goes to a 0600 file under opaque labels, keyed separately."""
    _run(tmp_path, vault)
    review_path, key_path = tmp_path / "review.md", tmp_path / "unblind.json"
    for path in (review_path, key_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    review = review_path.read_text(encoding="utf-8")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert review.count(_CANDIDATE_CANARY) == len(_MODELS.split(","))
    assert sorted(key["labels"].values()) == sorted(_MODELS.split(","))
    for label in key["labels"]:
        assert f"## Candidate {label}" in review
    for line in review.splitlines():
        if line.startswith("## Candidate"):
            assert not any(tag in line for tag in _MODELS.split(","))


def test_the_review_labels_are_the_existing_blinding(vault: Path) -> None:
    """Labels and their key come from ``blind`` / ``identity_map`` at the seed."""
    candidates = (Candidate("m-a", "text a"), Candidate("m-b", "text b"))
    corpus = load_vault_corpus(vault, seed=3)
    review = render_review(candidates, seed=3, vault=corpus)
    for blinded in blind(candidates, seed=3):
        assert f"## Candidate {blinded.candidate_id}" in review
    assert unblind_key(candidates, seed=3)["labels"] == identity_map(candidates, seed=3)


@pytest.mark.usefixtures("sealed")
@pytest.mark.parametrize(
    ("drop", "add", "reason"),
    [
        pytest.param("--i-own-this-vault", (), "--i-own-this-vault", id="no-ack"),
        pytest.param("--review-out", (), "--review-out", id="no-review-out"),
        pytest.param("--unblind-out", (), "--unblind-out", id="no-unblind-out"),
        pytest.param(
            None,
            ("--judge", "routed", "--judge-provider", "openai"),
            "--judge",
            id="routed-judge",
        ),
    ],
)
def test_an_incomplete_opt_in_is_refused(
    tmp_path: Path,
    vault: Path,
    capsys: pytest.CaptureFixture[str],
    drop: str | None,
    add: tuple[str, ...],
    reason: str,
) -> None:
    """Every part of the opt-in is required, and a cloud judge is never one."""
    argv = _argv(tmp_path, vault, *add)
    if drop is not None:
        position = argv.index(drop)
        width = 1 if drop.startswith("--i-own") else 2
        del argv[position : position + width]
    assert cli.main(["eval", *argv]) == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert err.startswith("refused: ")
    assert reason in err
    assert not (tmp_path / "eval.json").exists()
    assert not (tmp_path / "review.md").exists()


@pytest.mark.usefixtures("sealed")
def test_a_fake_run_over_a_real_vault_is_refused(
    tmp_path: Path, vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode fake`` over a real vault is pointless, so it is refused."""
    argv = [
        "eval",
        "--models",
        _MODELS,
        "--corpus-vault",
        str(vault),
        "--i-own-this-vault",
        "--review-out",
        str(tmp_path / "review.md"),
        "--unblind-out",
        str(tmp_path / "unblind.json"),
        "--out",
        str(tmp_path / "eval.json"),
    ]
    assert cli.main(argv) == cli.EXIT_REFUSED
    assert "--mode live" in capsys.readouterr().err
    assert not (tmp_path / "eval.json").exists()


@pytest.mark.usefixtures("sealed")
def test_a_run_under_ci_is_refused(
    tmp_path: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CI never reads a real vault, however complete the opt-in is."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(_KEY_ENV, "x")
    assert cli.main(["eval", *_argv(tmp_path, vault)]) == cli.EXIT_REFUSED
    assert "CI" in capsys.readouterr().err
    assert not (tmp_path / "eval.json").exists()


@pytest.mark.usefixtures("sealed")
def test_a_path_that_is_not_a_creek_vault_is_refused(tmp_path: Path) -> None:
    """A directory without the Creek marker is refused before anything is read."""
    plain = tmp_path / "plain"
    (plain / "01-Fragments").mkdir(parents=True)
    with pytest.raises(EvalPlanError, match="Creek vault"):
        plan_from_args(
            _parse(_argv(tmp_path, plain)),
            environ={_KEY_ENV: "x"},
            provider_factory=Factory(),
        )


@pytest.mark.usefixtures("sealed")
def test_an_existing_review_file_is_never_overwritten(
    tmp_path: Path, vault: Path
) -> None:
    """The private outputs are created fresh, refused before any spend."""
    (tmp_path / "review.md").write_text("earlier review", encoding="utf-8")
    factory = Factory()
    with pytest.raises(EvalPlanError, match="--review-out"):
        plan_from_args(
            _parse(_argv(tmp_path, vault)),
            environ={_KEY_ENV: "x"},
            provider_factory=factory,
        )
    assert factory.built == {}
    assert (tmp_path / "review.md").read_text(encoding="utf-8") == "earlier review"


@pytest.mark.usefixtures("sealed")
def test_the_private_outputs_must_be_distinct(tmp_path: Path, vault: Path) -> None:
    """The review file and the key cannot be the same file, or the artifact."""
    argv = _argv(tmp_path, vault)
    argv[argv.index("--unblind-out") + 1] = str(tmp_path / "review.md")
    with pytest.raises(EvalPlanError, match="distinct"):
        plan_from_args(
            _parse(argv), environ={_KEY_ENV: "x"}, provider_factory=Factory()
        )


@pytest.mark.usefixtures("sealed")
@pytest.mark.parametrize(
    "flag",
    [
        ("--i-own-this-vault",),
        ("--review-out", "r.md"),
        ("--unblind-out", "k.json"),
    ],
)
def test_corpus_vault_flags_without_the_vault_are_refused(
    tmp_path: Path, flag: tuple[str, ...]
) -> None:
    """A flag that looks applied but is not is refused rather than ignored."""
    args = _parse(["--models", _MODELS, "--out", str(tmp_path / "e.json"), *flag])
    with pytest.raises(EvalPlanError, match="--corpus-vault"):
        plan_from_args(args, environ={}, provider_factory=Factory())


def test_the_live_source_resolves_at_the_corpus_tier() -> None:
    """The router's chokepoint sees the real tier, not a hard-coded ``open``."""
    plan = plan_from_args(
        _parse(
            [
                "--models",
                "glm-5.3",
                "--mode",
                "live",
                "--provider",
                "openai",
                "--api-key-env",
                _KEY_ENV,
                "--out",
                "unused.json",
            ]
        ),
        environ={_KEY_ENV: "x"},
        provider_factory=Factory(),
    )
    assert list(plan.sources) == ["glm-5.3"]
    config = LLMConfig(provider="openai", model="glm-5.3", api_key_env=_KEY_ENV)
    with pytest.raises(IntimateRoutingError):
        routed_source(
            config, max_tokens=10, provider_factory=Factory(), tier=PrivacyTier.INTIMATE
        )


def test_the_vault_corpus_records_its_tier(vault: Path) -> None:
    """A corpus holding personal prose routes as personal."""
    corpus = load_vault_corpus(vault, seed=17)
    assert corpus.tier is PrivacyTier.PERSONAL


def test_one_seed_samples_one_corpus(vault: Path) -> None:
    """The bounded sample is a pure function of the vault and the seed."""
    first = load_vault_corpus(vault, seed=17, max_entries=MIN_ENTRIES)
    again = load_vault_corpus(vault, seed=17, max_entries=MIN_ENTRIES)
    assert first == again
    assert first.corpus.entry_count == MIN_ENTRIES
    assert first.counts.sampled == MIN_ENTRIES
    assert first.counts.admitted == _OPEN + _PERSONAL
    seeds = {
        load_vault_corpus(vault, seed=seed, max_entries=MIN_ENTRIES).source_ids
        for seed in range(8)
    }
    assert len(seeds) > 1


def test_the_sample_is_bounded_in_words(tmp_path: Path) -> None:
    """A long entry is truncated, and the whole sample stays under the cap."""
    vault = tmp_path / "vault"
    notes = vault / "01-Fragments" / "Notes"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    notes.mkdir(parents=True)
    long_body = " ".join(["word"] * (VAULT_MAX_ENTRY_WORDS * 3))
    for index in range(30):
        header = _front_matter(f"long-{index:02d}", tier="open")
        (notes / f"long-{index:02d}.md").write_text(
            f"---\n{header}\n---\n{long_body}\n", "utf-8"
        )
    corpus = load_vault_corpus(vault, seed=1)
    entries = corpus.corpus.voice_sample.split("\n\n")
    assert all(len(entry.split()) <= VAULT_MAX_ENTRY_WORDS for entry in entries)
    assert corpus.counts.words <= VAULT_MAX_TOTAL_WORDS
    assert len(corpus.corpus.voice_sample.split()) == corpus.counts.words
    assert corpus.corpus.entry_count >= MIN_ENTRIES


def test_too_few_eligible_fragments_is_refused(tmp_path: Path) -> None:
    """A vault too thin to carry a voice is refused, naming counts only."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments" / "Notes").mkdir(parents=True)
    for index in range(MIN_ENTRIES - 1):
        _write(vault, "open", index, tier="open")
    with pytest.raises(VaultCorpusError) as caught:
        load_vault_corpus(vault, seed=1)
    assert _FRAGMENT_CANARY not in str(caught.value)
    assert _ID_CANARY not in str(caught.value)


def test_loading_the_vault_writes_nothing_to_it(vault: Path) -> None:
    """The owner's vault is read, never modified."""
    before = {
        path: path.read_bytes() for path in sorted(vault.rglob("*")) if path.is_file()
    }
    load_vault_corpus(vault, seed=17)
    after = {
        path: path.read_bytes() for path in sorted(vault.rglob("*")) if path.is_file()
    }
    assert before == after


@pytest.mark.parametrize("cap", [MIN_ENTRIES - 1, VAULT_MAX_ENTRIES + 1])
def test_a_sample_cap_outside_the_range_is_refused(vault: Path, cap: int) -> None:
    """``--entries`` bounds the vault sample inside #1875's 10-20 range."""
    with pytest.raises(VaultCorpusError, match="entries"):
        load_vault_corpus(vault, seed=1, max_entries=cap)


def test_the_loader_refuses_a_directory_that_is_not_a_vault(tmp_path: Path) -> None:
    """The loader checks the Creek marker itself, not only the command."""
    with pytest.raises(VaultCorpusError, match="Creek vault"):
        load_vault_corpus(tmp_path, seed=1)


@pytest.mark.usefixtures("sealed")
def test_a_dangling_symlink_at_a_private_output_is_refused(
    tmp_path: Path, vault: Path
) -> None:
    """A planted link is refused up front, not followed when writing."""
    (tmp_path / "unblind.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(EvalPlanError, match="--unblind-out"):
        plan_from_args(
            _parse(_argv(tmp_path, vault)),
            environ={_KEY_ENV: "x"},
            provider_factory=Factory(),
        )
    assert not (tmp_path / "elsewhere.json").exists()


def test_write_private_never_overwrites(tmp_path: Path) -> None:
    """The write itself is exclusive, so a race cannot clobber a file."""
    target = tmp_path / "review.md"
    write_private(target, "first")
    with pytest.raises(FileExistsError):
        write_private(target, "second")
    assert target.read_text(encoding="utf-8") == "first"
