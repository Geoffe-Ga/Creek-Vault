"""The Writing Desk's round budget comes from the vault's config (#1465).

``creek/author/conductor.py:514`` reads
``max_rounds if max_rounds is not None else AuthorConfig().max_author_rounds``,
so ``run_author`` instantiates a *bare* :class:`~creek.config.AuthorConfig` and
always gets the built-in ``3`` — the vault's ``author.max_author_rounds`` is
never read. An operator who sets ``8`` gets ``3``; an operator who sets ``1``
also gets ``3``. Every other consumer of the desk's config resolves it through
``load_config(resolve_config_path(vault, None))`` (see
``creek/author/agents.py:92-96``); this one path skips that.

These tests pin the resolution in two places: at the conductor boundary (which
integer reaches ``build_default_conductor``) and at the ``creek.author`` MCP
verb, the surface the issue actually names, where ``rounds`` is echoed in the
response envelope. They also pin the *existing* error semantics for a
malformed or unreachable config, so the fix cannot smuggle in a swallowing
``try/except`` that turns a broken config into a silent default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from creek.author.conductor import run_author
from creek_mcp.tools.author import author_tool

if TYPE_CHECKING:
    from pathlib import Path


def _seed_fragment(vault: Path, frag_id: str, title: str) -> None:
    """Write a minimal fragment file so the real specialists have a corpus.

    The tier is stated explicitly because since #876 an untiered fragment
    ranks as PERSONAL and is excluded from the Writing Desk's evidence at
    the default OPEN ceiling — leaving the key off would silently empty
    this corpus and make the assertions below pass vacuously. These tests
    exercise the round budget, not tier policy, so the corpus is ``open``.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"privacy_tier: open\n"
        f"source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )


def _write_config(vault: Path, value: int) -> Path:
    """Write ``<vault>/00-Creek-Meta/creek_config.yaml`` with *value* rounds.

    Returns the path written, so a caller that needs to overwrite it with
    something malformed does not have to re-derive the layout.
    """
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    path = meta / "creek_config.yaml"
    path.write_text(f"author:\n  max_author_rounds: {value}\n", encoding="utf-8")
    return path


# --- 1. the conductor boundary: which integer reaches the conductor ---------


@pytest.mark.parametrize(
    ("config_value", "explicit", "expected"),
    [
        (8, None, 8),
        (1, None, 1),
        (None, None, 3),
        (1, 5, 5),
    ],
    ids=[
        "config-8-is-honoured",
        "config-1-is-honoured",
        "no-config-falls-back-to-3",
        "explicit-argument-wins-over-config",
    ],
)
def test_run_author_resolves_rounds_from_vault_config(
    config_value: int | None,
    explicit: int | None,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_author`` passes the vault-configured round budget to the conductor.

    The full truth table in one place: a configured value is honoured in both
    directions (up to 8 and down to 1), an absent config still yields the
    documented default of 3, and an explicit ``max_rounds`` argument still
    outranks the config. At HEAD the first two rows get 3.

    The chdir is load-bearing for the no-config row: with no ``CREEK_CONFIG``
    and no file in the vault, :func:`creek.config.load_config` falls back to
    ``Path("creek_config.yaml")`` relative to the *cwd*
    (``creek/config.py:2066``), so a stray config in the working directory
    would otherwise decide the answer. Applying it to every row keeps the
    isolation uniform.
    """
    vault = tmp_path
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    if config_value is not None:
        _write_config(vault, config_value)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}
    fake_conductor = MagicMock()
    fake_conductor.run.return_value = MagicMock()

    def _fake_build(*, max_rounds: int, **_kw: object) -> object:
        captured["max_rounds"] = max_rounds
        return fake_conductor

    monkeypatch.setattr("creek.author.conductor.build_default_conductor", _fake_build)
    monkeypatch.setattr("creek.author.conductor._enforce_chat_ceiling", lambda d: d)

    run_author(medium="research", query="q", vault=vault, max_rounds=explicit)

    assert captured["max_rounds"] == expected


# --- 2. the MCP surface the issue names, end to end ------------------------


@pytest.mark.parametrize(
    ("config_value", "expected"),
    [(8, 8), (1, 1)],
    ids=["mcp-honours-8", "mcp-honours-1"],
)
def test_mcp_author_honours_vault_max_author_rounds(
    config_value: int,
    expected: int,
    tmp_path: Path,
) -> None:
    """``creek.author`` reports the round count the vault's config asked for.

    The seeded corpus is load-bearing, not scenery. The fragment *titles*
    carry the jargon ("F6 Pluralism", "Agency") that makes
    ``check_unglossed_jargon`` recur in the drafted body every round, so the
    reflection node keeps returning ``REVISE`` and the conductor spends its
    whole budget — which is what makes ``rounds`` observable at all. On an
    unseeded vault ``creek/author/reflection.py:106`` short-circuits to
    ``ESCALATE`` on attempt 1 (no claims), ``rounds == 1`` for every config
    value, and this assertion becomes vacuous in both directions.
    """
    vault = tmp_path
    _write_config(vault, config_value)
    _seed_fragment(vault, "frag-a", "F6 Pluralism and community")
    _seed_fragment(vault, "frag-b", "Agency and momentum")

    result = author_tool(
        vault_path=vault,
        query="What is F6 Pluralism?",
        medium="research",
        max_rounds=None,
        consumer="test",
    )

    # Status first: an error envelope carries no "rounds" key at all, and a
    # refusal envelope carries one for a run that never drafted — either would
    # otherwise surface as a confusing KeyError rather than the real failure.
    assert result["status"] == "ok"
    # Guards the seeding above: if the fixture ever writes nothing (a renamed
    # subtree, a tier default change), the desk still answers "ok" with an
    # empty evidence bundle and the round assertion goes vacuous.
    assert result["claims"]
    assert result["rounds"] == expected
    # Deliberately no assertion on "verdict": exhausting the budget *is*
    # escalation, so the verdict is ESCALATE for 8, for 1 and for the
    # unfixed 3 alike. A `verdict == "PASS"` assertion could never go green
    # and would not distinguish the fixed code from HEAD either way.


# --- 3/4. the resolver short-circuits, and production really calls it -------


def test_explicit_max_rounds_short_circuits_the_config_read(tmp_path: Path) -> None:
    """An explicit ``max_rounds`` returns before the config is ever read.

    The resolver must early-return on ``max_rounds is not None``, never compute
    both and pick. The eager form — load the config, *then* choose — is
    behaviourally identical on every well-formed vault, so only a vault whose
    config cannot be parsed can tell the two apart.

    This cannot be written at the MCP surface. At HEAD
    ``author_tool(<broken vault>, max_rounds=5)`` already answers
    ``status: "error"``, because the specialists load the same config through
    ``creek/author/agents.py:96`` on every run regardless of what
    ``run_author`` does with the round budget — so the envelope is identical
    whether the resolver short-circuits or not. Calling the named resolver
    directly is the only instrument that fails when someone writes the eager
    form.

    The import is inside the body on purpose: ``_resolve_max_rounds`` does not
    exist at HEAD, and a module-level import of it would fail *collection* of
    this whole file, taking the tests above down with it and destroying their
    RED signal.
    """
    from creek.author.conductor import _resolve_max_rounds

    vault = tmp_path
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "author: [unclosed\n",
        encoding="utf-8",
    )

    assert _resolve_max_rounds(vault, 5) == 5


def test_run_author_delegates_to_the_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_author`` computes its round budget through ``_resolve_max_rounds``.

    The anchor for the test above: that one calls the resolver directly, so a
    later refactor that inlines the resolver back into ``run_author`` would
    leave it passing against an orphaned function while the production path
    silently regressed. This pins the call site — vault and raw argument
    forwarded, exactly once.

    ``raising=False`` is deliberately *not* passed: after the fix the symbol
    exists, and letting this fail loudly at HEAD is the correct RED.
    """
    calls: list[tuple[Path, int | None]] = []

    def _spy(vault: Path, max_rounds: int | None) -> int:
        calls.append((vault, max_rounds))
        return 2

    fake_conductor = MagicMock()
    fake_conductor.run.return_value = MagicMock()

    monkeypatch.setattr("creek.author.conductor._resolve_max_rounds", _spy)
    monkeypatch.setattr(
        "creek.author.conductor.build_default_conductor",
        lambda **_kw: fake_conductor,
    )
    monkeypatch.setattr("creek.author.conductor._enforce_chat_ceiling", lambda d: d)

    run_author(medium="research", query="q", vault=tmp_path, max_rounds=None)

    assert calls == [(tmp_path, None)]


# --- 5. the documented default survives, end to end ------------------------


def test_mcp_author_defaults_to_three_without_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no config anywhere, ``creek.author`` still spends exactly 3 rounds.

    The regression guard on the fix: reading the vault's config must not
    change the answer for the overwhelmingly common case of a vault that never
    configured the desk. The chdir removes the cwd fallback
    (``creek/config.py:2066``) so "no config" really means none.
    """
    vault = tmp_path
    _seed_fragment(vault, "frag-a", "F6 Pluralism and community")
    _seed_fragment(vault, "frag-b", "Agency and momentum")
    monkeypatch.chdir(vault)

    result = author_tool(
        vault_path=vault,
        query="What is F6 Pluralism?",
        medium="research",
        max_rounds=None,
        consumer="test",
    )

    assert result["status"] == "ok"
    assert result["claims"]
    assert result["rounds"] == 3


# --- 6. the existing error semantics, pinned -------------------------------


def _apply_broken_config(
    case: str,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Put *vault* into the named broken-config state for the test below.

    ``stale-env`` points ``CREEK_CONFIG`` at a path that does not exist;
    ``out-of-range`` writes a syntactically valid config whose value violates
    the ``[1, 10]`` bound; ``malformed-yaml`` writes something YAML cannot
    parse at all.
    """
    if case == "stale-env":
        monkeypatch.setenv("CREEK_CONFIG", "/definitely/not/here/creek_config.yaml")
        return
    if case == "out-of-range":
        _write_config(vault, 99)
        return
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text("author: [unclosed\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "expected_in_reason"),
    [
        ("stale-env", "CREEK_CONFIG points to"),
        ("out-of-range", "1 validation error for CreekConfig"),
        # The YAML parser's own message text is a library detail that varies
        # between the C and pure-Python loaders, so this row asserts only the
        # status plus a non-empty reason (checked unconditionally below).
        ("malformed-yaml", ""),
    ],
)
def test_malformed_config_keeps_yielding_an_error_envelope(
    case: str,
    expected_in_reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken config still surfaces as ``status: "error"``, not a default.

    Green at HEAD and green after the fix — this pins behaviour rather than
    demanding new behaviour, and it is here to stop the fix from adding a
    guard.

    The issue's AC #2 ("must not *start* returning ``status: error``") rests on
    a false premise: a stale ``CREEK_CONFIG`` already errors at HEAD, because
    the specialists load the vault's config unguarded on every run
    (``creek/author/agents.py:96``) and the MCP verb converts the exception
    into an envelope (``creek_mcp/tools/author.py:343-346``). So reading the
    config inside ``run_author`` adds no new failure mode, and a defensive
    ``try/except`` around it in ``run_author`` would be unreachable code that
    both the gated vulture check and the per-file coverage gate would flag —
    while quietly converting a typo'd config into a silent fallback to 3, the
    exact bug this issue is about.

    The vault is deliberately unseeded: all three cases raise before retrieval
    ever needs a corpus.

    ``CREEK_CONFIG`` is set on the clean slate the autouse
    ``_isolate_creek_config_env`` fixture in ``tests/conftest.py`` guarantees;
    no per-test ``delenv`` is needed or wanted.
    """
    vault = tmp_path
    _apply_broken_config(case, vault, monkeypatch)

    result = author_tool(
        vault_path=vault,
        query="What is F6 Pluralism?",
        medium="research",
        max_rounds=None,
        consumer="test",
    )

    assert result["status"] == "error"
    reason = result["reason"]
    assert isinstance(reason, str)
    assert reason
    assert expected_in_reason in reason
