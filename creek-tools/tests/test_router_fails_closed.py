"""The INTIMATE gate fails closed on untiered content (#1948).

ADR-0016 Decision 5: INTIMATE content never reaches the pool — or any
provider that is not intimate-eligible — by any route. The router used to
default ``tier`` to ``None`` on every entry point and let ``None`` through, so
the guarantee was only as good as every call site's discipline.

Two changes close it, and this module pins both:

1. ``tier`` is **required** on :meth:`ModelRouter.resolve`,
   :meth:`ModelRouter.resolve_role`, :meth:`ModelRouter.provider_for` and the
   Writing Desk's client factories, so mypy strict rejects an omission. An
   AST audit over ``creek/`` and ``creek_mcp/`` backs that with a test that
   fails on any untiered call into the router.
2. ``None`` stays legal — a Writing Desk run over unclassified evidence has no
   tier to give — but is gated **as INTIMATE**, as is any value that names no
   :class:`~creek.models.PrivacyTier`.

Assertions are content-free: tier values, provider names and exception types.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from pathlib import Path
from typing import Final

import pytest

from creek.author.client import VOICE_DRAFTER_ROLE, AuthorLLMClient
from creek.classify.llm.providers import OllamaProvider
from creek.classify.llm.router import (
    NON_INTIMATE_ROUTE,
    IntimateRoutingError,
    ModelRouter,
    UntieredRoutingError,
    gated_as_intimate,
    recognised_tier,
)
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

_PACKAGE_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
"""``creek-tools/``: the audit walks ``creek/`` and ``creek_mcp/`` under it."""

_AUDITED_PACKAGES: Final[tuple[str, ...]] = ("creek", "creek_mcp")

_ROUTER_METHODS: Final[frozenset[str]] = frozenset(
    {"resolve", "resolve_role", "provider_for"}
)
"""Router entry points; their tier is the second positional or ``tier=``."""

_CLIENT_FACTORIES: Final[frozenset[str]] = frozenset({"for_role", "for_voice_or_none"})
"""Writing Desk factories over the router; their tier is keyword-only."""

_ROUTER_RECEIVER: Final[re.Pattern[str]] = re.compile(r"router$|^keyed$")
"""What a ``.resolve`` receiver looks like when it is a :class:`ModelRouter`.

``resolve`` is also :meth:`pathlib.Path.resolve` and the link indexes', so
the receiver decides. Every router in the tree is bound as ``router``,
``model_router``, ``self._router`` or the request-key ``keyed`` router.
"""

_TIER_POSITION: Final[int] = 1
"""Index of ``tier`` among a router method's positional arguments."""

_MIN_AUDITED_SITES: Final[int] = 20
"""Floor on sites found, so a broken matcher cannot pass by finding none."""

_UNTIERED: Final[tuple[object, ...]] = (None, "secret", 7)
_UNTIERED_IDS: Final[list[str]] = ["none", "unknown-string", "foreign-object"]

_LOOPBACK: Final[str] = "http://127.0.0.1:11434"


def _cloud() -> LLMConfig:
    """Return a stage routed to a readable cloud vendor."""
    return LLMConfig(provider="anthropic")


def _pool() -> LLMConfig:
    """Return a stage routed to the shared inference pool."""
    return LLMConfig(provider="pool", model="creek-test-model:q4")


def _local() -> LLMConfig:
    """Return a stage routed to the loopback Ollama runtime."""
    return LLMConfig(provider="ollama", ollama_url=_LOOPBACK)


def _router(off_device: LLMConfig, default: LLMConfig) -> ModelRouter:
    """Return a router sending every content stage and role to *off_device*."""
    return ModelRouter(
        LLMRoutingConfig(
            default=default,
            classification=off_device,
            generation=off_device,
            writing_desk={VOICE_DRAFTER_ROLE: off_device},
        )
    )


# --- the gate ----------------------------------------------------------------


@pytest.mark.parametrize("off_device", [_cloud(), _pool()], ids=["cloud", "pool"])
@pytest.mark.parametrize("tier", _UNTIERED, ids=_UNTIERED_IDS)
def test_untiered_content_is_redirected_on_every_entry_point(
    tier: object, off_device: LLMConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """``resolve``, ``resolve_role`` and the factory all route untiered local."""
    router = _router(off_device, _local())

    with caplog.at_level(logging.WARNING):
        resolved = router.resolve("generation", tier)
        role = router.resolve_role(VOICE_DRAFTER_ROLE, tier)
        provider = router.provider_for("generation", tier)

    assert resolved.provider == "ollama"
    assert role.provider == "ollama"
    assert isinstance(provider, OllamaProvider)
    assert "Untiered content (gated as Intimate)" in caplog.text


@pytest.mark.parametrize("off_device", [_cloud(), _pool()], ids=["cloud", "pool"])
@pytest.mark.parametrize("tier", _UNTIERED, ids=_UNTIERED_IDS)
def test_untiered_content_refuses_when_no_local_default_exists(
    tier: object, off_device: LLMConfig
) -> None:
    """No local rescue target: a loud refusal every handler already catches."""
    router = _router(off_device, _pool())

    with pytest.raises(UntieredRoutingError) as caught:
        router.resolve("generation", tier)

    assert isinstance(caught.value, IntimateRoutingError)
    assert caught.value.stage_provider == off_device.provider
    assert "Untiered content" in str(caught.value)
    with pytest.raises(UntieredRoutingError):
        router.resolve_role(VOICE_DRAFTER_ROLE, tier)
    with pytest.raises(UntieredRoutingError):
        router.provider_for("generation", tier)


def test_the_intimate_refusal_is_unchanged() -> None:
    """An explicit INTIMATE refusal keeps its exact type and message."""
    router = _router(_pool(), _pool())

    with pytest.raises(IntimateRoutingError) as caught:
        router.resolve("generation", PrivacyTier.INTIMATE)

    assert type(caught.value) is IntimateRoutingError
    assert str(caught.value).startswith("Intimate-tier fragment cannot route to")


def test_the_intimate_redirect_log_is_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit INTIMATE redirect keeps its log wording."""
    router = _router(_cloud(), _local())

    with caplog.at_level(logging.WARNING):
        router.resolve("generation", PrivacyTier.INTIMATE)

    assert "Intimate-tier fragment: redirecting cloud provider" in caplog.text
    assert "Untiered" not in caplog.text


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("open", "anthropic"),
        ("personal", "anthropic"),
        ("unclassified", "anthropic"),
        ("intimate", "ollama"),
    ],
)
def test_a_tier_spelled_as_its_value_is_recognised(tier: str, expected: str) -> None:
    """Fragments store enum *values*; the gate reads them as the tier they name."""
    router = _router(_cloud(), _local())

    assert router.resolve("generation", tier).provider == expected


def test_recognised_tier_and_the_gate_agree() -> None:
    """Only a recognised non-INTIMATE tier escapes the gate."""
    assert recognised_tier(PrivacyTier.OPEN) is PrivacyTier.OPEN
    assert recognised_tier("personal") is PrivacyTier.PERSONAL
    assert recognised_tier("secret") is None
    assert recognised_tier(None) is None
    assert not gated_as_intimate(NON_INTIMATE_ROUTE)
    assert gated_as_intimate(PrivacyTier.INTIMATE)
    assert gated_as_intimate(None)
    assert gated_as_intimate("secret")


def test_the_writing_desk_factory_gates_untiered_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AuthorLLMClient.for_role`` inherits the gate for an untiered run."""
    router = _router(_cloud(), _local())

    built: list[str] = []
    monkeypatch.setattr(
        "creek.author.client.build_provider",
        lambda cfg: built.append(cfg.provider) or OllamaProvider(cfg),
    )

    AuthorLLMClient.for_role(router, VOICE_DRAFTER_ROLE, tier=None)
    with pytest.raises(UntieredRoutingError):
        AuthorLLMClient.for_role(
            _router(_cloud(), _pool()), VOICE_DRAFTER_ROLE, tier=None
        )

    assert built == ["ollama"]


# --- tier is required ----------------------------------------------------------


@pytest.mark.parametrize(
    "entry_point",
    [
        ModelRouter.resolve,
        ModelRouter.resolve_role,
        ModelRouter.provider_for,
        AuthorLLMClient.for_role,
        AuthorLLMClient.for_voice_or_none,
    ],
    ids=lambda fn: fn.__qualname__,
)
def test_tier_has_no_default_on_any_router_entry_point(entry_point: object) -> None:
    """No default means mypy strict reports every call site that omits it."""
    assert callable(entry_point)
    tier = inspect.signature(entry_point).parameters["tier"]

    assert tier.default is inspect.Parameter.empty


# --- the call-site audit -------------------------------------------------------


def _call_name(node: ast.Call) -> tuple[str, str] | None:
    """Return ``(receiver source, method)`` for an attribute call, else ``None``."""
    if not isinstance(node.func, ast.Attribute):
        return None
    return ast.unparse(node.func.value), node.func.attr


def _states_a_tier(node: ast.Call, method: str) -> bool:
    """Return whether the router call *node* passes a tier.

    Args:
        node: The call.
        method: The entry point it names.

    Returns:
        ``True`` for a ``tier=`` keyword, or — on a router method — a second
        positional argument. A ``**`` splat proves nothing, so it fails.
    """
    if any(keyword.arg == "tier" for keyword in node.keywords):
        return True
    positional_tier = method in _ROUTER_METHODS and len(node.args) >= _TIER_POSITION + 1
    return positional_tier and not any(
        isinstance(arg, ast.Starred) for arg in node.args
    )


def _router_calls(source: str) -> list[tuple[int, str, bool]]:
    """Return ``(line, method, states_a_tier)`` for each router call in *source*.

    Args:
        source: Python module source.

    Returns:
        One row per call into a router entry point.
    """
    rows: list[tuple[int, str, bool]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or (named := _call_name(node)) is None:
            continue
        receiver, method = named
        is_router_method = method in _ROUTER_METHODS and bool(
            _ROUTER_RECEIVER.search(receiver)
        )
        if is_router_method or method in _CLIENT_FACTORIES:
            rows.append((node.lineno, method, _states_a_tier(node, method)))
    return rows


def _audited_sources() -> list[Path]:
    """Return every production module the audit walks."""
    return sorted(
        path
        for package in _AUDITED_PACKAGES
        for path in (_PACKAGE_ROOT / package).rglob("*.py")
    )


def test_every_router_call_site_states_a_tier() -> None:
    """Enumerate calls into the router; any that omits a tier fails here."""
    sites: list[str] = []
    untiered: list[str] = []
    for path in _audited_sources():
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        for line, method, tiered in _router_calls(path.read_text(encoding="utf-8")):
            sites.append(relative)
            if not tiered:
                untiered.append(f"{relative}:{line} {method}")

    assert not untiered, f"router calls without a tier: {untiered}"
    assert len(sites) >= _MIN_AUDITED_SITES, sites
    for expected in (
        "creek/cli.py",
        "creek/classify/classify_engine.py",
        "creek/classify/fidelity.py",
        "creek/author/client.py",
        "creek_mcp/server.py",
        "creek_mcp/tools/classify.py",
    ):
        assert expected in sites, f"the audit no longer sees {expected}"


@pytest.mark.parametrize(
    ("snippet", "tiered"),
    [
        ('config.model_router.resolve("generation")', False),
        ('router.resolve_role("voice_drafter")', False),
        ('self._router.provider_for("generation")', False),
        ("AuthorLLMClient.for_role(router, role)", False),
        ("AuthorLLMClient.for_voice_or_none(router, **kwargs)", False),
        ("router.resolve(*args)", False),
        ('keyed.resolve("generation", tier)', True),
        ("router.resolve_role(role, tier=PrivacyTier.OPEN)", True),
        ("AuthorLLMClient.for_role(router, role, tier=tier)", True),
    ],
)
def test_the_audit_catches_an_untiered_call(snippet: str, tiered: bool) -> None:
    """The audit is not vacuous: each violating shape is flagged."""
    assert _router_calls(snippet) == [(1, _call_name_of(snippet), tiered)]


def test_the_audit_ignores_path_resolve() -> None:
    """``Path.resolve`` and link-index lookups are not router calls."""
    source = "root.resolve(strict=False)\nlink_index.resolve(target)\n"

    assert _router_calls(source) == []


def _call_name_of(snippet: str) -> str:
    """Return the method name a one-call *snippet* invokes."""
    node = ast.parse(snippet).body[0]
    assert isinstance(node, ast.Expr)
    assert isinstance(node.value, ast.Call)
    named = _call_name(node.value)
    assert named is not None
    return named[1]
