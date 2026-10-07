"""Structural check: every Ollama dial goes through the loopback boundary (#1849).

:mod:`creek.classify.llm.local_boundary` owns both halves of an Ollama dial:
the URL (the loopback refusal) and the transport (proxy-free, CA-aware). A
bypass needs either half rebuilt somewhere else, so this module checks the
*data* rather than a helper's name:

1. **No client in an Ollama module.** Outside the boundary, a module that
   names an Ollama API path (``/api/generate``, ``/api/tags`` ...) or reads an
   ``ollama_url`` must not construct or call an HTTP client — ``httpx``,
   ``requests``, ``urllib.request``, ``urllib3``, ``aiohttp`` or
   ``http.client`` — under any import spelling (``import httpx as hx``,
   ``from httpx import Client``). The few non-Ollama dials such a module
   legitimately makes are allowlisted by function, and those functions must
   not touch Ollama themselves.
2. **No URL built from ``ollama_url``.** Outside the boundary an
   ``ollama_url`` may only be passed to a validating or logging sink; it may
   not be concatenated, interpolated, ``.format``-ed or handed to anything
   else that could dial it.
3. **No private URL builder.** Nothing outside the boundary imports
   ``ollama_endpoint``, under any alias.
4. **The boundary's own clients ignore the proxy environment**:
   ``trust_env=False``, no ``**kwargs``, and no ``proxy`` / ``mounts``.

Each rule is proved non-vacuous against planted violations below, one per
bypass shape.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED = (_ROOT / "creek", _ROOT / "creek_mcp")
_BOUNDARY = _ROOT / "creek" / "classify" / "llm" / "local_boundary.py"

_OLLAMA_PATH: Final = re.compile(
    r"/api/(?:generate|tags|show|chat|pull|push|embed|embeddings|ps|version"
    r"|create|copy|delete|blobs)\b"
)
_URL_FIELD: Final = "ollama_url"
_URL_BUILDER: Final = "ollama_endpoint"

_DIALS: Final[dict[str, frozenset[str]]] = {
    "httpx": frozenset(
        {
            "Client",
            "AsyncClient",
            "HTTPTransport",
            "AsyncHTTPTransport",
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "request",
            "stream",
        }
    ),
    "requests": frozenset(
        {
            "Session",
            "session",
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "request",
        }
    ),
    "urllib.request": frozenset(
        {"urlopen", "Request", "build_opener", "OpenerDirector"}
    ),
    "urllib3": frozenset(
        {
            "PoolManager",
            "ProxyManager",
            "HTTPConnectionPool",
            "HTTPSConnectionPool",
            "request",
        }
    ),
    "aiohttp": frozenset({"ClientSession", "request"}),
    "http.client": frozenset({"HTTPConnection", "HTTPSConnection"}),
}
"""HTTP modules and the names in each that construct or call a client."""

_URL_SINKS: Final = frozenset(
    {
        "is_loopback_url",
        "require_local_target",
        "BenchOllamaClient",
        "debug",
        "info",
        "warning",
        "error",
        "exception",
        "critical",
    }
)
"""Calls an ``ollama_url`` may be handed to: validators, the bench client
(itself checked by rule 1) and logging."""

_NON_OLLAMA_DIALS: Final[dict[tuple[str, str], str]] = {
    (
        "creek/classify/llm/providers.py",
        "_fetch_attestation_quote",
    ): "Enclave attestation over attested https; not an Ollama endpoint.",
    (
        "creek/classify/llm/providers.py",
        "call_enclave",
    ): "Enclave generation over attested https; not an Ollama endpoint.",
    (
        "creek_mcp/container_health.py",
        "_v1_is_ready",
    ): "The vault's own /v1/health on the probe host; not an Ollama endpoint.",
}
"""Functions in Ollama-touching modules whose HTTP dial is something else."""

_FORBIDDEN_CLIENT_KEYWORDS: Final = frozenset({"proxy", "proxies", "mounts"})


@dataclass
class _Imports:
    """How a module spells the HTTP modules and the boundary's URL builder."""

    modules: dict[str, str] = field(default_factory=dict)
    names: dict[str, tuple[str, str]] = field(default_factory=dict)
    url_builder_imports: list[int] = field(default_factory=list)


def _collect_imports(tree: ast.Module) -> _Imports:
    """Resolve every import alias in *tree*."""
    found = _Imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None:
                    found.modules[alias.asname] = alias.name
                else:
                    head = alias.name.split(".", 1)[0]
                    found.modules[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                bound = alias.asname or alias.name
                submodule = f"{node.module}.{alias.name}"
                if submodule in _DIALS:
                    found.modules[bound] = submodule
                else:
                    found.names[bound] = (node.module, alias.name)
                if alias.name == _URL_BUILDER:
                    found.url_builder_imports.append(node.lineno)
    return found


def _dotted(node: ast.expr) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _resolved_target(call: ast.Call, imports: _Imports) -> tuple[str, str] | None:
    """Return ``(module, name)`` a call resolves to through import aliases."""
    dotted = _dotted(call.func)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    if not rest:
        return imports.names.get(head)
    if head in imports.modules:
        module, _, name = f"{imports.modules[head]}.{rest}".rpartition(".")
        return module, name
    if head in imports.names:
        module, name = imports.names[head]
        full_module, _, attr = f"{module}.{name}.{rest}".rpartition(".")
        return full_module, attr
    return None


def _is_dial(call: ast.Call, imports: _Imports) -> bool:
    """Return whether *call* constructs or calls an HTTP client."""
    target = _resolved_target(call, imports)
    return target is not None and target[1] in _DIALS.get(target[0], frozenset())


def _docstrings(tree: ast.Module) -> set[int]:
    """Return the ids of every docstring constant in *tree*."""
    owners = [tree] + [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    return {
        id(owner.body[0].value)
        for owner in owners
        if owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
        and isinstance(owner.body[0].value.value, str)
    }


def _ollama_markers(root: ast.AST, docstrings: set[int]) -> list[int]:
    """Return line numbers where *root* names an Ollama path or URL field."""
    lines = []
    for node in ast.walk(root):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _OLLAMA_PATH.search(node.value)
        ) or (isinstance(node, ast.Attribute) and node.attr == _URL_FIELD):
            lines.append(node.lineno)
    return lines


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Map each node's id to its parent node."""
    return {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    """Return the name of the innermost function containing *node*."""
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current.name
        current = parents.get(id(current))
    return None


def _call_name(call: ast.Call) -> str | None:
    """Return the bare or attribute name a call invokes."""
    dotted = _dotted(call.func)
    return None if dotted is None else dotted.rsplit(".", 1)[-1]


def _url_field_misuse(tree: ast.Module, parents: dict[int, ast.AST]) -> list[str]:
    """Rule 2: an ``ollama_url`` reaching anything but a validating sink."""
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == _URL_FIELD):
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.keyword):
            parent = parents.get(id(parent))
        if isinstance(parent, ast.Call) and _call_name(parent) in _URL_SINKS:
            continue
        if isinstance(parent, ast.Call | ast.BinOp | ast.FormattedValue):
            found.append(f"{node.lineno}: ollama_url fed to a URL or call")
    return found


def _module_violations(source: str, relative: str) -> list[str]:
    """Return rule 1-3 violations for a module outside the boundary."""
    tree = ast.parse(source)
    imports = _collect_imports(tree)
    parents = _parents(tree)
    docstrings = _docstrings(tree)
    found = [
        f"{line}: imports the boundary's URL builder"
        for line in imports.url_builder_imports
    ]
    found += _url_field_misuse(tree, parents)
    if not _ollama_markers(tree, docstrings):
        return found
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_dial(node, imports)):
            continue
        function = _enclosing_function(node, parents)
        if (relative, function or "") not in _NON_OLLAMA_DIALS:
            found.append(f"{node.lineno}: HTTP client dial in an Ollama module")
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if (relative, function.name) in _NON_OLLAMA_DIALS and _ollama_markers(
            function, docstrings
        ):
            found.append(f"{function.lineno}: allowlisted dial touches Ollama")
    return found


def _boundary_violations(source: str) -> list[str]:
    """Rule 4: every boundary dial is proxy-free and fully explicit."""
    tree = ast.parse(source)
    imports = _collect_imports(tree)
    dials = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_dial(node, imports)
    ]
    found = [] if dials else ["the boundary constructs no HTTP client"]
    for call in dials:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        trust = keywords.get("trust_env")
        if None in keywords:
            found.append(f"{call.lineno}: **kwargs hides the client's settings")
        if not (isinstance(trust, ast.Constant) and trust.value is False):
            found.append(f"{call.lineno}: client honours the proxy environment")
        found += [
            f"{call.lineno}: explicit {name}= routes through a proxy"
            for name in _FORBIDDEN_CLIENT_KEYWORDS & set(keywords)
        ]
    return found


def _tree_violations() -> list[str]:
    """Run every rule over the real ``creek/`` and ``creek_mcp/`` trees."""
    found = []
    for package in _SCANNED:
        for path in sorted(package.rglob("*.py")):
            relative = str(path.relative_to(_ROOT))
            source = path.read_text(encoding="utf-8")
            violations = (
                _boundary_violations(source)
                if path == _BOUNDARY
                else _module_violations(source, relative)
            )
            found += [f"{relative}:{violation}" for violation in violations]
    return found


def test_every_ollama_dial_goes_through_the_boundary() -> None:
    """The real tree holds no Ollama dial, URL or client outside the boundary."""
    violations = _tree_violations()

    assert not violations, (
        "Ollama dial bypasses local_boundary: "
        + "; ".join(violations)
        + " — dial with ollama_get / ollama_post / ollama_client instead."
    )


def test_the_rules_see_the_real_dial_sites() -> None:
    """Guard the guard: the checker recognises the tree's actual shapes."""
    boundary = ast.parse(_BOUNDARY.read_text(encoding="utf-8"))
    boundary_imports = _collect_imports(boundary)
    assert any(
        isinstance(node, ast.Call) and _is_dial(node, boundary_imports)
        for node in ast.walk(boundary)
    )
    for relative, function in _NON_OLLAMA_DIALS:
        tree = ast.parse((_ROOT / relative).read_text(encoding="utf-8"))
        assert _ollama_markers(tree, _docstrings(tree)), relative
        assert any(
            isinstance(node, ast.FunctionDef) and node.name == function
            for node in ast.walk(tree)
        ), f"{relative}:{function} no longer exists; drop its allowlist entry"


_MARKER = 'TAGS = "/api/tags"\n'


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import httpx\n" + _MARKER + "httpx.get(URL)\n", id="httpx"),
        pytest.param(
            "import httpx as hx\n" + _MARKER + "hx.Client()\n", id="module-alias"
        ),
        pytest.param(
            "from httpx import Client\n" + _MARKER + "Client()\n", id="from-import"
        ),
        pytest.param(
            "from httpx import Client as C\n" + _MARKER + "C()\n",
            id="from-import-alias",
        ),
        pytest.param(
            "import httpx\n" + _MARKER + "httpx.AsyncClient()\n", id="async-client"
        ),
        pytest.param(
            "import requests\ndef f(cfg):\n    requests.post(cfg.ollama_url)\n",
            id="requests",
        ),
        pytest.param(
            "import urllib.request\n" + _MARKER + "urllib.request.urlopen(URL)\n",
            id="urllib",
        ),
        pytest.param(
            "from urllib import request\n" + _MARKER + "request.urlopen(URL)\n",
            id="urllib-from",
        ),
        pytest.param(
            "import http.client\n" + _MARKER + "http.client.HTTPConnection(H)\n",
            id="http-client",
        ),
        pytest.param(
            "def f(cfg):\n    return cfg.ollama_url + '/api/generate'\n",
            id="concatenation",
        ),
        pytest.param(
            "def f(cfg):\n    return '{}/api/tags'.format(cfg.ollama_url)\n",
            id="format",
        ),
        pytest.param(
            "def f(cfg):\n    return f'{cfg.ollama_url}/api/tags'\n", id="f-string"
        ),
        pytest.param(
            "def f(cfg, http):\n    return http.Client(base_url=cfg.ollama_url)\n",
            id="base-url",
        ),
        pytest.param(
            "from creek.classify.llm.local_boundary import ollama_endpoint as ep\n",
            id="builder-alias",
        ),
    ],
)
def test_planted_bypasses_are_caught(source: str) -> None:
    """Each bypass shape outside the boundary is flagged."""
    assert _module_violations(source, "creek/planted.py")


def test_an_allowlisted_dial_that_touches_ollama_is_caught() -> None:
    """An allowlisted non-Ollama dial loses its exemption if it names Ollama."""
    source = (
        "import httpx\n"
        "def _v1_is_ready(settings):\n"
        "    return httpx.get('http://127.0.0.1:11434/api/tags')\n"
    )

    assert _module_violations(source, "creek_mcp/container_health.py")


def test_clean_modules_pass() -> None:
    """Control: Ollama-free dials and sink-only URL use are not flagged."""
    unrelated = "import httpx\nhttpx.get('https://example.test/health')\n"
    sink_only = (
        "from creek.classify.llm.local_boundary import is_loopback_url, ollama_get\n"
        "def f(cfg):\n"
        "    if is_loopback_url(cfg.ollama_url):\n"
        "        return ollama_get(cfg, '/api/tags', timeout=1.0)\n"
        "    return None\n"
    )

    assert _module_violations(unrelated, "creek/planted.py") == []
    assert _module_violations(sink_only, "creek/planted.py") == []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import httpx\nhttpx.Client(timeout=1)\n", id="no-trust-env"),
        pytest.param(
            "import httpx\nhttpx.Client(trust_env=True)\n", id="trust-env-true"
        ),
        pytest.param(
            "import httpx as hx\nhx.Client(trust_env=True)\n", id="alias-trusting"
        ),
        pytest.param(
            "from httpx import Client\nClient(trust_env=False, proxy='http://p')\n",
            id="explicit-proxy",
        ),
        pytest.param(
            "import httpx\nhttpx.Client(trust_env=False, mounts={})\n", id="mounts"
        ),
        pytest.param(
            "import httpx\nhttpx.Client(trust_env=False, **settings)\n", id="kwargs"
        ),
        pytest.param("x = 1\n", id="no-client"),
    ],
)
def test_planted_boundary_clients_are_caught(source: str) -> None:
    """The boundary's own client must be proxy-free and explicit."""
    assert _boundary_violations(source)


def test_a_proxy_free_boundary_client_passes() -> None:
    """Control: an explicit ``trust_env=False`` client is accepted."""
    source = "import httpx\nhttpx.Client(timeout=1, trust_env=False, verify=True)\n"

    assert _boundary_violations(source) == []
