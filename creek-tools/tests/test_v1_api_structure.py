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
  reintroduced it for a vault object would rebuild the existence oracle. Guard
  5 holds the ``httpapi``-scoped half; Guard 7 holds the repo-wide half — a
  pinned allowlist over every module in ``creek_mcp`` that may name the code
  at all, the wire spelling and the ``404`` literal, plus a pin on the sole
  construction site.
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

NOT_FOUND_ROUTING_MODULES: Final[frozenset[str]] = frozenset(
    {
        # The wire vocabulary itself: the ``NOT_FOUND`` member and its rows in
        # ERROR_STATUS, ERROR_MESSAGES and RETRY_POLICY. Not a vault-object
        # path — test_models_module_reads_no_files AST-pins that this module
        # calls no file reader at all.
        "api/models.py",
        # The published document's per-route universal refusal list. A routing
        # miss is reachable on every published path because routing answers
        # before any handler does, so the code belongs on every route.
        "api/openapi.py",
        # The routing layer: the sole construction site, inside _routing_miss,
        # plus the ERROR_STATUS key that registers that handler against
        # Starlette's own router rather than against any handler.
        "httpapi/app.py",
    }
)
"""The only ``creek_mcp`` modules that may name ``NOT_FOUND`` at all (#1098).

Asserted as set **equality**, not containment: a module that stops naming it is
as much a change to this invariant as one that starts. Removing
``api/openapi.py``'s entry would quietly drop the routing miss out of the
published refusal list, and a subset assertion would permit that silently.
"""

NOT_FOUND_WIRE_STRING_MODULES: Final[frozenset[str]] = frozenset({"api/models.py"})
"""The only module that may write the wire spelling ``"not_found"``.

Matched by equality rather than substring: a substring test flags three
modules, two of them only for docstring prose about routing.
"""

NOT_FOUND_MEMBER_LITERAL_MODULES: Final[frozenset[str]] = frozenset()
"""No module may spell the member name as a bare string literal.

``ErrorCode["NOT_FOUND"]`` and ``getattr(ErrorCode, "NOT_FOUND")`` reach the
member without ever writing the attribute the name arm matches. Neither
construct exists in the package today — there is not one ``getattr`` call
anywhere under ``creek_mcp/`` — so this arm is prospective: it forbids the
evasion before anyone writes it.
"""

NOT_FOUND_STATUS_MODULES: Final[frozenset[str]] = frozenset({"api/models.py"})
"""The only module that may carry the integer ``404``.

``ERROR_STATUS`` is where a status is chosen for a code. A ``404`` written
anywhere else is a handler picking a status directly, which is how the
existence oracle comes back without ``NOT_FOUND`` ever being named.
"""

NOT_FOUND_CONSTRUCTION_SITES: Final[frozenset[str]] = frozenset(
    {"httpapi/app.py::_routing_miss"}
)
"""The only ``error_response(ErrorCode.NOT_FOUND, ...)`` site, by ``module::function``.

This is a **static-spelling** pin. Seven ``error_response`` calls across
``httpapi/`` pass a computed code, so no AST sweep can prove at this call site
that ``NOT_FOUND`` is constructed exactly once at runtime. What carries that
half of the invariant is :data:`NOT_FOUND_ROUTING_MODULES`: every one of those
seven resolves through a ``*_refusal_code`` helper living in a module that may
not name ``NOT_FOUND`` at all.
"""

NOT_FOUND_CONSTRUCTION_MODULES: Final[frozenset[str]] = frozenset(
    site.split("::")[0] for site in NOT_FOUND_CONSTRUCTION_SITES
)
"""The module half of :data:`NOT_FOUND_CONSTRUCTION_SITES`, derived not restated.

Asserted separately because the qualified set alone would pass a second call
added at module level, where there is no enclosing function to name.
"""


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


def _int_constants(source: str) -> set[int]:
    """Return every integer literal in *source*.

    ``bool`` is excluded deliberately: it subclasses ``int``, so a bare
    ``True`` would otherwise be indistinguishable from ``1``.

    Args:
        source: Python source text.

    Returns:
        The distinct integer constants.
    """
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }


def _enclosing_function_names(tree: ast.Module) -> dict[int, str]:
    """Map each call node's ``id()`` to the function whose body contains it.

    ``ast.walk`` is breadth-first, so an enclosing function is visited before
    anything nested inside it and the innermost name is the one that survives.

    Args:
        tree: A parsed module.

    Returns:
        ``id(call node) -> enclosing function name``. Calls written at module
        level are absent from the mapping.
    """
    return {
        id(call): node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    }


def _not_found_construction_sites(source: str) -> list[tuple[int, str]]:
    """Return every ``error_response(<owner>.NOT_FOUND, ...)`` site in *source*.

    Both ``error_response(...)`` and ``errors.error_response(...)`` count, for
    the reason :func:`_called_names` documents. A call whose first positional
    argument is not an attribute is **skipped, not crashed on**: seven real
    sites across ``httpapi/`` pass a computed ``*_refusal_code(...)`` result
    or a ``code`` local, and an unguarded ``.attr`` would raise there.

    Args:
        source: Python source text.

    Returns:
        ``(lineno, enclosing function name)`` per site, in walk order. The
        enclosing name is ``""`` for a call written at module level.
    """
    tree = ast.parse(source)
    enclosing = _enclosing_function_names(tree)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            callee = func.attr
        elif isinstance(func, ast.Name):
            callee = func.id
        else:
            continue
        first = node.args[0]
        if callee != "error_response" or not isinstance(first, ast.Attribute):
            continue
        if first.attr == "NOT_FOUND":
            sites.append((node.lineno, enclosing.get(id(node), "")))
    return sites


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
# Guard 5 — NOT_FOUND is a routing code, within httpapi/ (Guard 7 is repo-wide)
# --------------------------------------------------------------------------- #


def test_not_found_is_named_in_exactly_one_httpapi_module() -> None:
    """``ErrorCode.NOT_FOUND`` belongs to routing and to nothing else.

    #846, #970, #972 and #1090 spent five issues collapsing the difference
    between "no such fragment" and "you may not see this fragment": a caller
    who can tell them apart can enumerate the corpus one id at a time without
    reading a byte of it. A ``404`` emitted from a handler over a vault object
    rebuilds that oracle exactly.

    Scoped to ``creek_mcp/httpapi/`` because that is the surface #1074 owns.
    Guard 7 below sweeps all of ``creek_mcp`` and strictly subsumes this check:
    its matcher is owner-agnostic, so it also catches the ``EC.NOT_FOUND``
    spelling this one misses. Both are kept, on the precedent of
    ``test_api_package_imports_no_web_framework`` sitting beside Guard 6.
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


# --------------------------------------------------------------------------- #
# Guard 7 — NOT_FOUND stays inside the routing layer, repo-wide (#1098)
# --------------------------------------------------------------------------- #


def test_the_repo_wide_not_found_name_arm_is_not_vacuous() -> None:
    """Arm (a) is owner-agnostic, which is exactly what Guard 5's matcher is not.

    ``_dotted_attributes`` keys on ``Owner.ATTR``, so ``EC.NOT_FOUND`` and
    ``models.ErrorCode.NOT_FOUND`` both slip past its
    ``"ErrorCode.NOT_FOUND"`` membership test — and five ``httpapi`` modules
    already import ``ErrorCode`` plainly, which puts the aliased spelling one
    edit away. ``_referenced_names`` keys on the attribute alone, so all three
    spellings are caught and a rename of the owner cannot evade it. The last
    assertion is the control: it records what Guard 5's matcher would miss.
    """
    assert "NOT_FOUND" in _referenced_names("x = ErrorCode.NOT_FOUND\n")
    assert "NOT_FOUND" in _referenced_names("x = EC.NOT_FOUND\n")
    assert "NOT_FOUND" in _referenced_names("x = models.ErrorCode.NOT_FOUND\n")
    assert "NOT_FOUND" not in _referenced_names("x = ErrorCode.PRIVACY_REFUSED\n")
    assert "ErrorCode.NOT_FOUND" not in _dotted_attributes("x = EC.NOT_FOUND\n")


def test_the_not_found_wire_string_arm_is_not_vacuous() -> None:
    """Equality, never substring: prose naming ``not_found`` is not a code.

    A substring matcher flags three modules, two of them only for docstring
    prose such as "a ``404 not_found`` is a routing answer". Equality flags
    the one module that mints the member. The last pair covers the bare member
    spelling, which is how ``ErrorCode["NOT_FOUND"]`` would reach the member
    without ever writing the attribute arm (a) matches.
    """
    assert "not_found" in _string_constants('MEMBER = "not_found"\n')
    assert "not_found" not in _string_constants('MEMBER = "privacy_refused"\n')
    assert "not_found" not in _string_constants('"""A 404 not_found is routing."""\n')
    assert "NOT_FOUND" in _string_constants('code = ErrorCode["NOT_FOUND"]\n')
    assert "NOT_FOUND" not in _string_constants('code = ErrorCode["PRIVACY_REFUSED"]\n')


def test_the_404_status_arm_is_not_vacuous() -> None:
    """``bool`` subclasses ``int``, so a bare ``True`` must not read as ``1``.

    Without the ``bool`` exclusion the arm would be keyed on a type that
    ``404`` shares with nothing else here, but the exclusion is what stops a
    future ``status in (404, True)`` shape from reading as two integers.
    """
    assert _int_constants("STATUS = 404\n") == {404}
    assert 404 not in _int_constants("STATUS = 403\n")
    assert _int_constants("FLAG = True\n") == set()


def test_the_not_found_construction_arm_is_not_vacuous() -> None:
    """It finds the call, skips a computed code, and records the function.

    The computed-code case is a regression pin, not a nicety: seven real
    ``error_response`` call sites across ``creek_mcp/httpapi/`` pass a
    ``*_refusal_code(...)`` result or a ``code`` local as the first argument,
    and an unguarded ``.attr`` on that argument raises ``AttributeError`` on
    every one of them — which would stop the guard running at all.
    """
    assert _not_found_construction_sites(
        "error_response(ErrorCode.NOT_FOUND, ctx)\n"
    ) == [(1, "")]
    assert _not_found_construction_sites(
        "errors.error_response(EC.NOT_FOUND, ctx)\n"
    ) == [(1, "")]
    assert (
        _not_found_construction_sites("error_response(ErrorCode.PRIVACY_REFUSED, c)\n")
        == []
    )
    assert _not_found_construction_sites("error_response(code_for(r), ctx)\n") == []
    assert _not_found_construction_sites("error_response(code, ctx)\n") == []
    assert _not_found_construction_sites(
        "async def _routing_miss(request):\n"
        "    return error_response(ErrorCode.NOT_FOUND, ctx)\n"
    ) == [(2, "_routing_miss")]


def test_not_found_is_named_only_in_the_pinned_routing_modules() -> None:
    """The repo-wide half of #1098: three modules may name it, and only three.

    This is the arm that actually carries the invariant. A handler cannot emit
    a code it may not name, and the seven ``error_response`` sites that pass a
    computed code all resolve through ``*_refusal_code`` helpers living in
    modules this allowlist excludes — so the construction pin below is a
    static-spelling pin, and *this* is what makes it hold at runtime.

    Owner-agnostic by construction: ``_referenced_names`` keys on the
    attribute, so ``EC.NOT_FOUND`` and ``models.ErrorCode.NOT_FOUND`` are
    caught alongside ``ErrorCode.NOT_FOUND``. Guard 5 above, which keys on the
    owner, is strictly subsumed by this one and is kept anyway — the same
    scoped-plus-repo-wide pairing ``test_api_package_imports_no_web_framework``
    already has with Guard 6.
    """
    sources = _sources(CREEK_MCP)
    assert sources
    named = {
        path.relative_to(CREEK_MCP).as_posix()
        for path in sources
        if "NOT_FOUND" in _referenced_names(_read(path))
    }
    assert named == NOT_FOUND_ROUTING_MODULES, sorted(named ^ NOT_FOUND_ROUTING_MODULES)


def test_the_not_found_wire_string_appears_only_in_the_vocabulary_module() -> None:
    """The wire spelling is minted once, and the member name is never a string.

    Both halves are equality tests over string literals. The first pins where
    ``"not_found"`` may be written; the second forbids ``ErrorCode["NOT_FOUND"]``
    and ``getattr(ErrorCode, "NOT_FOUND")``, which would otherwise reach the
    member without writing the attribute the arm above matches.
    """
    sources = _sources(CREEK_MCP)
    assert sources
    spelled = {
        path.relative_to(CREEK_MCP).as_posix()
        for path in sources
        if "not_found" in _string_constants(_read(path))
    }
    assert spelled == NOT_FOUND_WIRE_STRING_MODULES, sorted(
        spelled ^ NOT_FOUND_WIRE_STRING_MODULES
    )
    by_member_name = {
        path.relative_to(CREEK_MCP).as_posix()
        for path in sources
        if "NOT_FOUND" in _string_constants(_read(path))
    }
    assert by_member_name == NOT_FOUND_MEMBER_LITERAL_MODULES, sorted(
        by_member_name ^ NOT_FOUND_MEMBER_LITERAL_MODULES
    )


def test_the_404_status_literal_appears_only_in_the_status_table() -> None:
    """A ``404`` written outside ``ERROR_STATUS`` is the oracle without the name.

    ``NOT_FOUND`` is a code, but the leak is the *status*. A handler that
    answered ``404`` directly — never naming the enum member, so the arm above
    stays green — would hand back the same existence signal.
    """
    sources = _sources(CREEK_MCP)
    assert sources
    carrying = {
        path.relative_to(CREEK_MCP).as_posix()
        for path in sources
        if 404 in _int_constants(_read(path))
    }
    assert carrying == NOT_FOUND_STATUS_MODULES, sorted(
        carrying ^ NOT_FOUND_STATUS_MODULES
    )


def test_not_found_is_constructed_once_in_the_routing_miss() -> None:
    """One refusal is built with this code, in the handler for a routing miss.

    Three assertions, because no one of them is sufficient. The module set
    alone passes a second call added at module level in ``app.py``; the
    function set alone passes a second call added inside ``_routing_miss``;
    the count alone passes a call moved to another module's function of the
    same name. Together they pin module, multiplicity and enclosing function.

    A **static-spelling** pin only — see :data:`NOT_FOUND_CONSTRUCTION_SITES`.
    """
    sources = _sources(CREEK_MCP)
    assert sources
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sources:
        sites = _not_found_construction_sites(_read(path))
        if sites:
            found[path.relative_to(CREEK_MCP).as_posix()] = sites
    assert set(found) == NOT_FOUND_CONSTRUCTION_MODULES, found
    assert sum(len(sites) for sites in found.values()) == 1, found
    qualified = {f"{rel}::{name}" for rel, sites in found.items() for _, name in sites}
    assert qualified == NOT_FOUND_CONSTRUCTION_SITES, found
