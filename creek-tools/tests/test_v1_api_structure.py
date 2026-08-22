"""Structural guards: ``/v1`` may not grow a second copy of anything (#1074).

Every behavioural test in this suite proves the HTTP adapter *currently*
answers correctly. None of them can prove it will keep doing so for the right
reason. A second token registry, a second admission gate, a second loopback
predicate or a second error envelope would each pass the behavioural suite on
the day it was written and then drift — and drift is the failure this whole
epic exists to prevent: the six-divergence table in the ADR is what happens
when two surfaces implement the same rule twice.

So these are AST sweeps over the sources themselves, and they answer questions
no runtime test can:

* Is there **exactly one** call to :func:`creek_mcp.policy.admitted_ceiling`?
  Two gates are two places to disagree, and the lenient one wins.
* Is there **exactly one** ``CallerIdentity(`` site, and does any of them say
  ``is_remote=False``? That literal is the #1073 bug spelled out — a ``/v1``
  caller asserted local is a caller the ``personal`` cap does not apply to.
* Does ``creek_mcp/httpapi/`` import ``hmac`` or mention the token env var?
  Either is the beginning of a second registry with its own length floor.
* Is ``ErrorEnvelope`` constructed anywhere but ``errors.py``? A second
  construction site is where a fourth field eventually gets added.
* Is ``ErrorCode.NOT_FOUND`` used outside the routing layer? #846/#970/#972/
  #1090 spent five issues collapsing exactly that distinction; a handler that
  reintroduced it for a vault object would rebuild the existence oracle. The
  repo-wide version of this guard — an allowlist over every construction site
  in ``creek_mcp`` — is tracked in **#1098**; this module holds the
  ``httpapi``-scoped half that #1074 can enforce today.
* Does ``fastapi`` appear anywhere in ``creek_mcp/``? The ADR rejected it for
  four stated reasons and, until now, nothing enforced that.

**Every guard here is paired with a non-vacuity twin.** A reflection guard
fails *open* when its discovery stops matching: the sweep finds nothing, every
``not in`` holds, and the test stays green while the invariant rots. So each
helper is also fed a source string that *does* contain the forbidden construct,
and pinned to flag it. That pattern is borrowed verbatim from
``tests/test_mcp_policy.py``'s group 8.

``creek/`` never importing ``creek_mcp`` (#1032) is deliberately **not**
duplicated here: ``test_creek_package_never_imports_creek_mcp`` in
``tests/test_adepthood_contract_models.py`` already owns it, and a second copy
would be exactly the duplication this module exists to forbid.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CREEK_MCP: Final[Path] = REPO_ROOT / "creek-tools" / "creek_mcp"
HTTPAPI: Final[Path] = CREEK_MCP / "httpapi"
API: Final[Path] = CREEK_MCP / "api"

FORBIDDEN_FRAMEWORKS: Final[frozenset[str]] = frozenset({"fastapi"})
"""Rejected in the ADR for four reasons; nothing enforced it before #1074."""


# --------------------------------------------------------------------------- #
# AST helpers — each with a twin below
# --------------------------------------------------------------------------- #


def _sources(root: Path) -> list[Path]:
    """Return every Python source under *root*, sorted.

    Args:
        root: A package directory.

    Returns:
        The sorted source paths, or ``[]`` when *root* does not exist.
    """
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.py"))


def _read(path: Path) -> str:
    """Return *path*'s text.

    Args:
        path: The source file.

    Returns:
        The decoded source.
    """
    return path.read_text(encoding="utf-8")


def _imported_roots(source: str) -> set[str]:
    """Return the top-level module names *source* imports, by AST alone.

    Reading the source rather than the imported module's ``__dict__`` is
    deliberate: a lazily-imported module inside a function body is still an
    import, and this finds it.

    Args:
        source: Python source text.

    Returns:
        The distinct top-level roots.
    """
    tree = ast.parse(source)
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    roots.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    return roots


def _imported_module_paths(source: str) -> set[str]:
    """Return the fully dotted module paths *source* imports.

    Args:
        source: Python source text.

    Returns:
        Every dotted path named by an ``import`` or ``from ... import ...``.
    """
    tree = ast.parse(source)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            paths.add(node.module)
            paths.update(f"{node.module}.{alias.name}" for alias in node.names)
    return paths


def _called_names(source: str) -> list[str]:
    """Return the callee name of every call expression in *source*.

    ``foo()`` yields ``"foo"``; ``bar.foo()`` also yields ``"foo"``, so both
    spellings of one construction site are counted the same way.

    Args:
        source: Python source text.

    Returns:
        One entry per call, in walk order.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.append(node.func.attr)
    return names


def _referenced_names(source: str) -> set[str]:
    """Return every bare name and attribute *source* mentions.

    Args:
        source: Python source text.

    Returns:
        The union of ``Name.id`` and ``Attribute.attr``.
    """
    referenced: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    return referenced


def _dotted_attributes(source: str) -> set[str]:
    """Return ``Owner.ATTR``-style references in *source*.

    Args:
        source: Python source text.

    Returns:
        Dotted pairs such as ``"TierCeiling.INTIMATE"``.
    """
    dotted: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            dotted.add(f"{node.value.id}.{node.attr}")
    return dotted


def _string_constants(source: str) -> set[str]:
    """Return every string literal in *source*, docstrings included.

    Args:
        source: Python source text.

    Returns:
        The distinct string constants.
    """
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _keyword_literals(source: str, keyword: str) -> set[object]:
    """Return the literal values passed as *keyword* anywhere in *source*.

    Args:
        source: Python source text.
        keyword: The keyword-argument name to collect.

    Returns:
        The constant values supplied for it. Non-literal expressions are
        skipped, which is safe here: the guard asks whether a *literal*
        ``False`` was written, and a computed value is a different question.
    """
    values: set[object] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.keywords:
            if arg.arg == keyword and isinstance(arg.value, ast.Constant):
                values.add(arg.value.value)
    return values


def _defined_function_names(source: str) -> set[str]:
    """Return every function and coroutine defined in *source*.

    Args:
        source: Python source text.

    Returns:
        The defined function names.
    """
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _count_across_httpapi(callee: str) -> int:
    """Return how many times *callee* is called across ``httpapi/``.

    Args:
        callee: The callee name to count.

    Returns:
        The total call count.
    """
    return sum(_called_names(_read(path)).count(callee) for path in _sources(HTTPAPI))


# --------------------------------------------------------------------------- #
# The sweeps have something to sweep
# --------------------------------------------------------------------------- #


def test_the_httpapi_package_has_sources_to_walk() -> None:
    """Guards the whole module: an empty sweep satisfies every ``not in``."""
    names = {path.name for path in _sources(HTTPAPI)}
    assert names >= {
        "__init__.py",
        "app.py",
        "auth.py",
        "capabilities.py",
        "cli.py",
        "errors.py",
        "handlers.py",
        "logging.py",
    }


def test_the_middleware_subpackage_has_sources_to_walk() -> None:
    """The middleware modules exist under the package the sweeps walk."""
    names = {path.name for path in _sources(HTTPAPI / "middleware")}
    assert names >= {"access_log.py", "boundary.py", "ceiling.py", "limits.py"}


def test_the_framework_free_api_modules_exist() -> None:
    """``routes.py`` and ``openapi.py`` live in the framework-free half."""
    names = {path.name for path in _sources(API)}
    assert {"routes.py", "openapi.py"} <= names


# --------------------------------------------------------------------------- #
# Guard 1 — no second token registry
# --------------------------------------------------------------------------- #


def test_httpapi_reaches_tokens_only_through_remote_auth() -> None:
    """The HTTP adapter borrows the MCP registry; it does not build one.

    ``hmac`` is the tell: a module that compares tokens itself has imported
    it. The env-var literal is the other tell — a module that reads
    ``CREEK_MCP_CONSUMER_TOKENS`` directly has parsed the registry itself,
    and will eventually parse it differently.
    """
    sources = _sources(HTTPAPI)
    assert sources
    for path in sources:
        source = _read(path)
        assert "hmac" not in _imported_roots(source), path
        assert "CREEK_MCP_CONSUMER_TOKENS" not in _string_constants(source), path
    assert any(
        "creek_mcp.remote_auth" in _imported_module_paths(_read(path))
        for path in sources
    )


def test_the_token_registry_guard_is_not_vacuous() -> None:
    """Both halves of the guard above detect what they are looking for."""
    assert _imported_roots("import hmac\n") == {"hmac"}
    assert "hmac" in _imported_roots("from hmac import compare_digest\n")
    assert "CREEK_MCP_CONSUMER_TOKENS" in _string_constants(
        'RAW = os.environ["CREEK_MCP_CONSUMER_TOKENS"]\n'
    )
    assert "creek_mcp.remote_auth" in _imported_module_paths(
        "from creek_mcp.remote_auth import ConsumerTokenVerifier\n"
    )


# --------------------------------------------------------------------------- #
# Guard 2 — no second admission gate
# --------------------------------------------------------------------------- #


def test_there_is_exactly_one_admission_call() -> None:
    """One request, one gate, one call site.

    A second call site is a second gate, and the two would eventually be
    reached under different conditions — at which point the effective policy
    is whichever one the request happens to hit.
    """
    assert _count_across_httpapi("admitted_ceiling") == 1


def test_the_admission_call_lives_in_the_ceiling_middleware() -> None:
    """And it lives above the router, where the ADR puts it."""
    ceiling = HTTPAPI / "middleware" / "ceiling.py"
    assert _called_names(_read(ceiling)).count("admitted_ceiling") == 1


def test_httpapi_never_names_the_admitted_ceiling_set_or_the_barred_tiers() -> None:
    """The adapter does not re-derive the cap from its ingredients.

    Referencing ``REMOTE_ADMITTED_CEILINGS``, ``TierCeiling.INTIMATE`` or
    ``TierCeiling.ALL`` means the adapter is reasoning about the membership
    test itself rather than asking policy for a verdict — the same shape as
    reimplementing the gate, one step subtler.
    """
    for path in _sources(HTTPAPI):
        source = _read(path)
        assert "REMOTE_ADMITTED_CEILINGS" not in _referenced_names(source), path
        dotted = _dotted_attributes(source)
        assert "TierCeiling.INTIMATE" not in dotted, path
        assert "TierCeiling.ALL" not in dotted, path


def test_there_is_exactly_one_caller_identity_construction_site() -> None:
    """Remoteness is asserted once, by the authentication middleware.

    Two sites means two answers to "which side of the network is this?", and
    only one of them is checked by the tests.
    """
    assert _count_across_httpapi("CallerIdentity") == 1


def test_no_caller_identity_is_constructed_as_local() -> None:
    """``is_remote=False`` is the #1073 bug written out longhand.

    ``/v1`` is remote by construction. A single ``is_remote=False`` anywhere
    in this package would lift the ``personal`` cap for whatever path reached
    it, and ``intimate`` would become readable over HTTP.
    """
    for path in _sources(HTTPAPI):
        assert False not in _keyword_literals(_read(path), "is_remote"), path


def test_the_admission_guards_are_not_vacuous() -> None:
    """Each of the four checks above detects its forbidden construct."""
    assert _called_names("admitted_ceiling(identity, raw)\n") == ["admitted_ceiling"]
    assert _called_names("policy.admitted_ceiling(identity, raw)\n") == [
        "admitted_ceiling"
    ]
    assert "REMOTE_ADMITTED_CEILINGS" in _referenced_names(
        "if member in REMOTE_ADMITTED_CEILINGS:\n    pass\n"
    )
    assert "TierCeiling.INTIMATE" in _dotted_attributes("x = TierCeiling.INTIMATE\n")
    assert "TierCeiling.ALL" in _dotted_attributes("y = TierCeiling.ALL\n")
    assert _called_names("CallerIdentity(consumer=c, is_remote=True)\n") == [
        "CallerIdentity"
    ]
    assert False in _keyword_literals(
        "CallerIdentity(consumer=None, is_remote=False)\n", "is_remote"
    )
    assert False not in _keyword_literals(
        "CallerIdentity(consumer=c, is_remote=True)\n", "is_remote"
    )


# --------------------------------------------------------------------------- #
# Guard 3 — no second transport-posture gate
# --------------------------------------------------------------------------- #


def test_httpapi_defines_no_loopback_predicate() -> None:
    """The adapter classifies no host itself.

    ``ipaddress`` is the import a hand-rolled loopback check needs, and a
    function whose name mentions loopback is the check itself. Either would
    be a second posture gate free to disagree with ``creek_mcp.server``'s
    about, say, ``127.0.0.5``.
    """
    for path in _sources(HTTPAPI):
        source = _read(path)
        assert "ipaddress" not in _imported_roots(source), path
        offenders = [
            name for name in _defined_function_names(source) if "loopback" in name
        ]
        assert offenders == [], path


def test_httpapi_imports_the_shared_transport_posture() -> None:
    """It calls the extracted module rather than re-deriving the rule."""
    assert any(
        "creek_mcp.transport_posture" in _imported_module_paths(_read(path))
        for path in _sources(HTTPAPI)
    )


def test_the_transport_posture_guard_is_not_vacuous() -> None:
    """The loopback sweep detects both spellings of a hand-rolled check."""
    assert _imported_roots("import ipaddress\n") == {"ipaddress"}
    assert "is_loopback" in _defined_function_names(
        "def is_loopback(host: str) -> bool:\n    return False\n"
    )
    assert "_is_loopback" in _defined_function_names(
        "async def _is_loopback(host):\n    return False\n"
    )
    assert "creek_mcp.transport_posture" in _imported_module_paths(
        "from creek_mcp.transport_posture import is_loopback\n"
    )


# --------------------------------------------------------------------------- #
# Guard 4 — no second error envelope
# --------------------------------------------------------------------------- #


def test_the_error_envelope_has_exactly_one_construction_site() -> None:
    """One place builds an error body, so one place can be audited.

    ``ErrorEnvelope`` is ``extra="forbid"`` with three fields, but that only
    constrains the *shape*. What keeps the contents honest is that every
    refusal is built from the ``ERROR_MESSAGES`` table at one site; a second
    site is where a caller-derived ``message`` eventually appears.
    """
    assert _count_across_httpapi("ErrorEnvelope") == 1


def test_the_error_envelope_is_built_in_the_errors_module() -> None:
    """And that site is ``errors.py``, not a handler."""
    errors = HTTPAPI / "errors.py"
    assert _called_names(_read(errors)).count("ErrorEnvelope") == 1


def test_the_error_envelope_guard_is_not_vacuous() -> None:
    """The counter detects both call spellings."""
    assert _called_names("ErrorEnvelope(code=c, message=m, request_id=r)\n") == [
        "ErrorEnvelope"
    ]
    assert _called_names("models.ErrorEnvelope(code=c)\n") == ["ErrorEnvelope"]


# --------------------------------------------------------------------------- #
# Guard 5 — NOT_FOUND is a routing code (the httpapi half of #1098)
# --------------------------------------------------------------------------- #


def test_not_found_is_named_in_exactly_one_httpapi_module() -> None:
    """``ErrorCode.NOT_FOUND`` belongs to routing and to nothing else.

    #846, #970, #972 and #1090 spent five issues collapsing the difference
    between "no such fragment" and "you may not see this fragment": a caller
    who can tell them apart can enumerate the corpus one id at a time without
    reading a byte of it. A ``404`` emitted from a handler over a vault object
    rebuilds that oracle exactly.

    Scoped to ``creek_mcp/httpapi/`` because that is the surface #1074 owns.
    The repo-wide sweep — every ``NOT_FOUND`` construction site in
    ``creek_mcp``, checked against a pinned routing-layer allowlist — is
    **#1098**.
    """
    offenders = [
        path.relative_to(CREEK_MCP).as_posix()
        for path in _sources(HTTPAPI)
        if "ErrorCode.NOT_FOUND" in _dotted_attributes(_read(path))
    ]
    assert len(offenders) == 1, offenders


def test_the_not_found_guard_is_not_vacuous() -> None:
    """The sweep detects a ``NOT_FOUND`` reference when one is present."""
    assert "ErrorCode.NOT_FOUND" in _dotted_attributes(
        "raise Refusal(ErrorCode.NOT_FOUND)\n"
    )
    assert "ErrorCode.NOT_FOUND" not in _dotted_attributes(
        "raise Refusal(ErrorCode.PRIVACY_REFUSED)\n"
    )


# --------------------------------------------------------------------------- #
# Guard 6 — FastAPI appears nowhere in creek_mcp/
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_FRAMEWORKS))
def test_no_module_in_creek_mcp_imports_fastapi(forbidden: str) -> None:
    """The ADR rejected FastAPI for four reasons; this is what enforces it.

    Its Starlette pin range would have to stay compatible with the MCP SDK's
    forever; its generated OpenAPI document is a function of its own version
    rather than of our models; its default ``422`` handler echoes the
    offending input, which the no-echo invariant forbids outright; and the
    route table is five endpoints, so the convenience never clears the bar.
    ``test_api_package_imports_no_web_framework`` covers ``creek_mcp/api/``
    only — this is the whole package.

    Args:
        forbidden: The framework root that must not be imported.
    """
    sources = _sources(CREEK_MCP)
    assert sources
    offenders = [
        path.relative_to(CREEK_MCP).as_posix()
        for path in sources
        if forbidden in _imported_roots(_read(path))
    ]
    assert offenders == []


def test_the_framework_guard_is_not_vacuous() -> None:
    """The import sweep detects both spellings of a FastAPI import."""
    assert "fastapi" in _imported_roots("import fastapi\n")
    assert "fastapi" in _imported_roots("from fastapi import FastAPI\n")


def test_starlette_is_confined_to_the_adapter() -> None:
    """Only ``creek_mcp/httpapi/`` and ``creek_mcp/server.py`` may see Starlette.

    The framework-free half of the surface — ``creek_mcp/api/`` — is what
    lets the wire vocabulary and the published document outlive whatever
    serves them, and ``creek_mcp/policy.py``'s transport-neutrality claim
    depends on the same separation.
    """
    offenders = [
        path.relative_to(CREEK_MCP).as_posix()
        for path in _sources(CREEK_MCP)
        if "starlette" in _imported_roots(_read(path))
        and not path.is_relative_to(HTTPAPI)
        and path.name != "server.py"
    ]
    assert offenders == []
