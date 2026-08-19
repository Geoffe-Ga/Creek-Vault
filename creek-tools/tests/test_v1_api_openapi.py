"""The OpenAPI document is a function of our models, not of a framework (#1074).

Reason 2 of the ADR's four for declining FastAPI: "the OpenAPI document is
itself a published, pinned artifact. FastAPI's generated OpenAPI document is a
function of FastAPI's own version, so a routine FastAPI upgrade would silently
rewrite the published contract underneath a consumer that has pinned it."
:func:`creek_mcp.api.openapi.build_openapi` therefore generates from
:data:`creek_mcp.api.models.CONTRACT_MODELS` alone.

That claim is only worth anything if something checks it, so the load-bearing
test here compares each component against the **committed bytes** of
``docs/contracts/adepthood-v1/schemas/<name>.schema.json`` — never against a
fresh ``model_json_schema()`` call. Generating the expectation from the same
function under test proves the function agrees with itself; reading the
committed file proves the document agrees with the artifact Adepthood vendors.
It is the same anti-rot discipline as
``test_committed_bundle_equals_a_fresh_build``.

Byte-identity is impossible, and that is expected: Pydantic emits nested models
under a top-level ``$defs`` with ``#/$defs/X`` references, while OpenAPI puts
them under ``components/schemas`` with ``#/components/schemas/X``. So the
comparison runs through :func:`_to_openapi_refs`, one total mechanical
transform — drop ``$defs``, rewrite every ref prefix — with its own non-vacuity
test so it cannot silently degrade into the identity function.

The path set is derived from ``create_app().routes`` rather than from
``ROUTES``. Deriving it from ``ROUTES`` would be tautological: both the
document and the expectation would come from the same table, and a route the
adapter failed to *mount* would still appear documented.

**Why the component set is wider than ``CONTRACT_MODELS``.** The registry holds
the sixteen wire *models*. Each committed schema carries the definitions it
references — the six enums (``Capability``, ``WireTierCeiling``,
``CapabilitiesStatus``, ``JournalAction``, ``NoteKind``, ``ReflectionStatus``)
and the nested models — inline under its own ``$defs``, which is what makes each
schema file independently resolvable. An OpenAPI document has one shared
``components/schemas`` namespace instead, so those definitions are hoisted out
and become siblings.

Hoisting is not optional: dropping ``$defs`` while rewriting ``#/$defs/X`` to
``#/components/schemas/X`` and publishing only the sixteen models would emit a
document whose references point at names it never defines. Every standard
validator and code generator rejects that, so "OpenAPI generation succeeds"
would be true only in the sense that a broken file was produced. The component
set is therefore ``CONTRACT_MODELS`` **plus** the union of the committed
schemas' own ``$defs`` keys — derived from the bundle at test time, never
hard-coded, so a definition added to a model shows up here without anybody
remembering to update a list.

This changes nothing about what the bundle publishes. The sixteen schema files
and the manifest hashes are untouched; hoisting happens only in the OpenAPI
projection, and every hoisted definition is itself checked against the committed
bytes it came from. :func:`test_every_reference_resolves` is the assertion that
actually holds the line — it is the one a dangling ref fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.api.models import (
    CONTRACT_MODELS,
    ERROR_STATUS,
    MAX_EXTERNAL_ID_CHARS,
)
from creek_mcp.api.openapi import (
    BEARER_SCHEME_DESCRIPTION,
    EXTERNAL_ID_PARAM,
    EXTERNAL_ID_PATTERN,
    SECURITY_SCHEME_NAME,
    build_openapi,
)
from creek_mcp.api.routes import AUTHORIZATION_HEADER
from creek_mcp.contract import CONTRACT_VERSION
from creek_mcp.httpapi.journal import admissible_external_id
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    CONTRACT_VERSION_HEADER,
    HEALTH_PATH,
    JOURNAL_TEMPLATE,
    REFLECTIONS_PATH,
    UPLOAD_PATH,
    WHEEL_PATH,
    build_app,
    mounted_method_paths,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SCHEMA_DIR: Final[Path] = REPO_ROOT / "docs" / "contracts" / "adepthood-v1" / "schemas"

MODEL_NAMES: Final[tuple[str, ...]] = tuple(sorted(CONTRACT_MODELS))

ALLOWED_HTTP_STATUSES: Final[frozenset[str]] = frozenset(
    str(status) for status in set(ERROR_STATUS.values()) | {200}
)
"""The published closed status set, as the string keys OpenAPI uses."""

ERROR_ENVELOPE_REF: Final[str] = "#/components/schemas/ErrorEnvelope"

VERSIONED_OPERATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("put", JOURNAL_TEMPLATE),
    ("post", REFLECTIONS_PATH),
    ("get", WHEEL_PATH),
)


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a seeded vault, so the app can be built for route introspection."""
    yield seed_vault(tmp_path)


def _committed_schema(model_name: str) -> dict[str, Any]:
    """Return the committed JSON Schema for *model_name*, parsed from disk.

    Args:
        model_name: The wire model's class name.

    Returns:
        The parsed schema exactly as it is committed.
    """
    path = SCHEMA_DIR / f"{model_name}.schema.json"
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _walk_refs(node: object) -> Iterator[str]:
    """Yield every ``$ref`` string value anywhere inside *node*.

    Recursive rather than a substring scan of the serialised document, so a
    reference nested inside an array item or a ``oneOf`` branch is found too —
    which is where the ones this suite cares about actually live.

    Args:
        node: Any JSON-shaped fragment of the document.

    Yields:
        Each ``$ref`` value encountered, in traversal order.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _walk_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_refs(item)


def _committed_definitions() -> dict[str, Any]:
    """Return every ``$defs`` entry across the committed schemas, by name.

    Derived from the bundle on disk rather than listed here, so a definition
    added to (or dropped from) a model reaches this expectation without anybody
    remembering to edit a constant.

    A name appearing in more than one file must carry the same body in each: the
    schemas are all generated from one set of Pydantic models, so a collision
    with two different bodies would mean the bundle itself is inconsistent, and
    hoisting into a single shared namespace would silently pick a winner. This
    refuses instead of choosing.

    Returns:
        Each definition name mapped to its committed schema body.
    """
    collected: dict[str, Any] = {}
    for model_name in MODEL_NAMES:
        for name, body in _committed_schema(model_name).get("$defs", {}).items():
            assert collected.get(name, body) == body, (
                f"the committed bundle defines {name!r} two different ways; "
                "hoisting them into one components namespace would pick a winner"
            )
            collected[name] = body
    return collected


def _to_openapi_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON Schema into its OpenAPI component form.

    One total, mechanical transform with exactly two steps, so there is no
    room for it to "fix up" a discrepancy the test is supposed to catch:

    1. drop the top-level ``$defs`` — nested models become sibling components;
    2. rewrite every ``#/$defs/X`` reference to ``#/components/schemas/X``.

    Args:
        schema: A parsed Pydantic JSON Schema.

    Returns:
        The equivalent OpenAPI component schema.
    """
    without_defs = {key: value for key, value in schema.items() if key != "$defs"}
    rewritten = json.dumps(without_defs).replace("#/$defs/", "#/components/schemas/")
    parsed: dict[str, Any] = json.loads(rewritten)
    return parsed


def _components() -> dict[str, Any]:
    """Return the document's ``components/schemas`` mapping.

    Returns:
        The component schemas, keyed by name.
    """
    schemas: dict[str, Any] = build_openapi()["components"]["schemas"]
    return schemas


def _paths() -> dict[str, Any]:
    """Return the document's ``paths`` mapping.

    Returns:
        The path items, keyed by path template.
    """
    paths: dict[str, Any] = build_openapi()["paths"]
    return paths


HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
)
"""OpenAPI path-item keys that are operations rather than metadata."""


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    """Return every ``(path, method, operation)`` triple in the document.

    Non-method path-item keys (``summary``, ``parameters``, ``servers``) are
    skipped, so a document that grows shared metadata does not start being
    read as an operation with no ``responses``.

    Returns:
        One entry per documented operation.
    """
    return [
        (path, method, operation)
        for path, item in _paths().items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


# --------------------------------------------------------------------------- #
# It builds at all
# --------------------------------------------------------------------------- #


def test_build_openapi_produces_a_document() -> None:
    """The generator runs and yields the three top-level members we publish."""
    document = build_openapi()
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"]
    assert document["paths"]
    assert document["components"]["schemas"]


def test_the_document_declares_the_runtime_contract_version() -> None:
    """``info.version`` is the runtime constant, not a restated literal.

    A document whose version could drift from the server's is a document a
    consumer would pin and then silently mis-negotiate against.
    """
    assert build_openapi()["info"]["version"] == CONTRACT_VERSION


# --------------------------------------------------------------------------- #
# Components == CONTRACT_MODELS, and each is the committed schema
# --------------------------------------------------------------------------- #


def test_components_are_exactly_the_models_plus_their_committed_definitions() -> None:
    """No extra component, and none missing.

    Equality rather than containment, and both directions matter. A *missing*
    component is a dangling reference. An *extra* one is a shape the bundle
    publishes nothing for, so a consumer generating code from the document
    would get a type the fixtures never validate.

    The expectation is the sixteen registered models plus every name the
    committed schemas hoist out of their own ``$defs`` — read from the bundle,
    not restated — so this cannot be satisfied by inventing a component or by
    quietly dropping one.
    """
    expected = set(CONTRACT_MODELS) | set(_committed_definitions())
    assert set(_components()) == expected


def test_hoisted_definitions_are_not_vacuous() -> None:
    """The hoisted set is non-empty and really does exceed the model registry.

    Without this, the assertion above would still pass if every committed
    schema stopped carrying ``$defs`` — which is exactly the state in which the
    hoisting logic could be deleted and nothing would notice, right up until a
    model gained a nested definition again.
    """
    definitions = _committed_definitions()
    assert definitions
    assert set(definitions) - set(CONTRACT_MODELS)


@pytest.mark.parametrize("definition_name", sorted(_committed_definitions()))
def test_each_hoisted_definition_is_the_committed_definition(
    definition_name: str,
) -> None:
    """A hoisted component equals the committed ``$defs`` body it came from.

    Hoisting is a move, not a re-derivation. If the generator rebuilt these
    from the live enums instead of carrying the published bodies across, an
    enum could gain a member and the document would advertise it while the
    bundle a consumer vendored still did not.

    Args:
        definition_name: The hoisted definition under test.
    """
    expected = _to_openapi_refs(_committed_definitions()[definition_name])
    assert _components()[definition_name] == expected


def test_every_reference_resolves() -> None:
    """No ``$ref`` in the document points at a component that does not exist.

    This is the assertion the whole hoisting arrangement exists to satisfy, and
    the one a partial implementation fails. A document that references
    ``#/components/schemas/Capability`` without defining it is rejected by every
    standard validator and code generator — so "OpenAPI generation succeeds"
    would be true only in the sense that a broken file was written. Checked
    over the entire document, paths included, not just over the components.
    """
    document = build_openapi()
    defined = set(document["components"]["schemas"])
    referenced = {
        value
        for value in _walk_refs(document)
        if value.startswith("#/components/schemas/")
    }
    dangling = {ref for ref in referenced if ref.split("/")[-1] not in defined}
    assert dangling == set()
    assert referenced, "the sweep found no references at all"


@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_each_component_is_the_committed_schema(model_name: str) -> None:
    """Each component equals the committed schema, ref-rewritten.

    The expectation is read from disk on purpose. A fresh
    ``model_json_schema()`` call would make this a test of Pydantic's
    determinism; the committed bytes are what Adepthood actually vendors, and
    a document that has drifted from them is the failure this test exists to
    catch.

    Args:
        model_name: The wire model under test.
    """
    assert _components()[model_name] == _to_openapi_refs(_committed_schema(model_name))


def test_the_ref_transform_actually_rewrites_a_reference() -> None:
    """The transform is not the identity function.

    Every ``==`` above would still pass if ``_to_openapi_refs`` returned its
    argument untouched *and* the generator did the same. Pin that the
    committed schema really does carry ``$defs``-style refs and that the
    transform really does rewrite them.
    """
    committed = _committed_schema("CapabilitiesResponse")
    raw = json.dumps(committed)
    assert "#/$defs/" in raw
    assert "$defs" in committed

    transformed = _to_openapi_refs(committed)
    rendered = json.dumps(transformed)
    assert "$defs" not in transformed
    assert "#/$defs/" not in rendered
    assert "#/components/schemas/" in rendered


def test_the_error_envelope_is_published() -> None:
    """Every error path refs it, so it must exist as a component."""
    assert "ErrorEnvelope" in _components()


# --------------------------------------------------------------------------- #
# Paths == what the app actually mounts
# --------------------------------------------------------------------------- #


def test_documented_paths_equal_the_mounted_routes(vault: Path) -> None:
    """The document describes the server, not the route table it was built from.

    Derived from ``create_app().routes`` rather than from ``ROUTES``: reading
    both sides off the same table would let a route the adapter forgot to
    mount stay documented, which is the one direction of drift a consumer
    cannot detect until it calls the endpoint.

    Args:
        vault: A seeded vault.
    """
    documented = {(method.upper(), path) for path, method, _operation in _operations()}
    assert documented == mounted_method_paths(build_app(vault_path=vault))


def test_every_published_route_is_documented() -> None:
    """All six paths appear, named explicitly so a silent drop is visible."""
    assert set(_paths()) == {
        CAPABILITIES_PATH,
        JOURNAL_TEMPLATE,
        REFLECTIONS_PATH,
        WHEEL_PATH,
        UPLOAD_PATH,
        HEALTH_PATH,
    }


def test_every_operation_declares_a_unique_operation_id() -> None:
    """Client generators key method names on ``operationId``."""
    operation_ids = [
        operation["operationId"] for _path, _method, operation in _operations()
    ]
    assert len(set(operation_ids)) == len(operation_ids)
    assert all(operation_ids)


# --------------------------------------------------------------------------- #
# Responses stay inside the published status set
# --------------------------------------------------------------------------- #


def test_every_documented_status_is_in_the_published_set() -> None:
    """No documented response falls outside ``{200,401,403,404,409,422,500,501,503}``.

    A conforming client maps an out-of-set status to "unreachable" and must
    not synthesize a body, so documenting one — ``405`` above all — would tell
    consumers to write handling for a state that means "the vault is gone".
    """
    documented = {
        status
        for _path, _method, operation in _operations()
        for status in operation["responses"]
    }
    assert documented
    assert documented <= ALLOWED_HTTP_STATUSES


def test_no_operation_documents_a_405() -> None:
    """Stated separately, because ``405`` is the one a router adds for free."""
    assert "405" not in ALLOWED_HTTP_STATUSES
    assert all(
        "405" not in operation["responses"]
        for _path, _method, operation in _operations()
    )


def test_every_non_200_response_is_the_error_envelope() -> None:
    """One error shape across the whole surface, referenced not restated.

    An inline error schema on one route is how a second envelope shape is
    born, and a second shape is a place for a field that echoes request or
    vault material.
    """
    for path, method, operation in _operations():
        for status, response in operation["responses"].items():
            if status == "200":
                continue
            schema = response["content"]["application/json"]["schema"]
            assert schema == {"$ref": ERROR_ENVELOPE_REF}, f"{method} {path} {status}"


def test_every_operation_documents_the_unauthenticated_refusal() -> None:
    """Auth is above the router, so ``401`` is reachable on every path."""
    assert all(
        "401" in operation["responses"] for _path, _method, operation in _operations()
    )


# --------------------------------------------------------------------------- #
# The contract-version parameter
# --------------------------------------------------------------------------- #


def _parameter_names(path: str, method: str) -> set[str]:
    """Return the header/query/path parameter names of one operation.

    Args:
        path: The documented path template.
        method: The lowercase HTTP method.

    Returns:
        The declared parameter names.
    """
    operation = _paths()[path][method]
    return {parameter["name"] for parameter in operation.get("parameters", [])}


def test_capabilities_declares_no_contract_version_parameter() -> None:
    """The negotiation endpoint must not require the thing being negotiated."""
    assert CONTRACT_VERSION_HEADER not in _parameter_names(CAPABILITIES_PATH, "get")


@pytest.mark.parametrize(
    ("method", "path"), VERSIONED_OPERATIONS, ids=["journal", "reflections", "wheel"]
)
def test_the_content_routes_declare_the_contract_version_parameter(
    method: str, path: str
) -> None:
    """The three vault-touching routes publish the header they refuse without.

    A required header that is not in the document is a ``409`` the consumer
    cannot see coming from the contract alone.

    Args:
        method: The lowercase HTTP method.
        path: The documented path template.
    """
    parameters = _paths()[path][method].get("parameters", [])
    declared = {
        parameter["name"]: parameter
        for parameter in parameters
        if parameter["name"] == CONTRACT_VERSION_HEADER
    }
    assert CONTRACT_VERSION_HEADER in declared
    assert declared[CONTRACT_VERSION_HEADER]["in"] == "header"
    assert declared[CONTRACT_VERSION_HEADER]["required"] is True


def test_the_versioned_operations_list_is_not_vacuous() -> None:
    """The parametrize above covers three routes, not zero."""
    assert len(VERSIONED_OPERATIONS) == 3


# --------------------------------------------------------------------------- #
# The bearer requirement (#1371)
# --------------------------------------------------------------------------- #


def _security_schemes() -> dict[str, Any]:
    """Return the document's ``components/securitySchemes`` mapping.

    Returns:
        The declared schemes, keyed by name. Empty when the document declares
        none at all — which is the state #1371 was filed about, and which this
        helper renders as an assertion failure rather than a ``KeyError``.
    """
    schemes: dict[str, Any] = build_openapi()["components"].get("securitySchemes", {})
    return schemes


def test_the_document_declares_the_bearer_scheme_every_route_enforces() -> None:
    """THE INVARIANT — the published document names the credential the server demands.

    :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware` sits above the router,
    so all six published routes are unreachable without a token and
    :func:`~creek_mcp.httpapi.auth.build_verifier` refuses to construct a server
    with no consumers configured. A document that declares no scheme therefore
    says the surface is open while the server refuses everything, and a client
    generated from it has no way to present a credential at all.
    """
    assert _security_schemes()[SECURITY_SCHEME_NAME] == {
        "type": "http",
        "scheme": "bearer",
        "description": BEARER_SCHEME_DESCRIPTION,
    }


def test_the_document_requires_that_scheme_globally() -> None:
    """The requirement is document-level, so it reaches every operation.

    Declared once rather than repeated per operation: five copies of a
    requirement are five places for one of them to be dropped, and OpenAPI
    already resolves an absent operation-level ``security`` against the
    document-level default.
    """
    assert build_openapi()["security"] == [{SECURITY_SCHEME_NAME: []}]


def test_no_operation_publishes_its_own_security_override() -> None:
    """No operation carries an operation-level ``security`` key at all.

    An operation-level ``security: []`` is how OpenAPI spells "this one is
    open". ``GET /v1/health`` is the route somebody would eventually be tempted
    to spell that way, and it is still behind the gate — a probe with no bearer
    is a ``401``, which the suite pins elsewhere.

    Asserted as *absence of the key*, not as ``get(..., default) != []``. The
    earlier form could not fail: with no operation publishing ``security``, the
    default was returned every time and the comparison was always true — a
    forward guard wearing the clothes of evidence. Absence is falsifiable: any
    override, permissive or not, fails here and forces the decision to be made
    deliberately rather than inherited.
    """
    ops = list(_operations())
    assert ops, "no operations found — the walk missed the document"

    overrides = [
        f"{method.upper()} {path}"
        for path, method, operation in ops
        if "security" in operation
    ]
    assert overrides == [], (
        "these operations override the document-level bearer requirement; "
        f"decide each one deliberately: {overrides}"
    )


def test_the_bearer_scheme_is_truthful_about_the_header_the_server_reads() -> None:
    """THE NON-VACUITY TWIN — ``http``/``bearer`` names ``Authorization``, implicitly.

    OpenAPI's ``type: http`` scheme has no place to name a header: it *means*
    the ``Authorization`` header, by RFC 7235. So the declaration above is only
    a true statement about this server while
    :data:`~creek_mcp.api.routes.AUTHORIZATION_HEADER` — the constant
    :mod:`creek_mcp.httpapi.auth` reads and :mod:`creek_mcp.httpapi.errors`
    names in every ``Vary`` — is that header. Move the constant and this scheme
    becomes a lie no other test in this module could see.
    """
    assert AUTHORIZATION_HEADER == "Authorization"


def test_the_bearer_requirement_added_no_component_schema() -> None:
    """Publishing the scheme must not disturb the byte-pinned schema bundle.

    ``components/securitySchemes`` is a different namespace from
    ``components/schemas``; the sixteen committed schema files are what a
    consumer vendors, and a security declaration must not move one of them.
    """
    assert SECURITY_SCHEME_NAME not in _components()
    assert "securitySchemes" not in _components()


# --------------------------------------------------------------------------- #
# The external_id path parameter (#1132)
# --------------------------------------------------------------------------- #


def _path_parameters() -> list[tuple[str, str, dict[str, Any]]]:
    """Return every ``(path, method, parameter)`` triple for an ``in: path`` parameter.

    Returns:
        One entry per declared path parameter across the whole document.
    """
    return [
        (path, method, parameter)
        for path, method, operation in _operations()
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "path"
    ]


def test_every_published_path_parameter_is_bounded() -> None:
    """THE INVARIANT — no path parameter is published as an unbounded string.

    Starlette's default converter is ``[^/]+``, which admits a NUL byte, a
    control character and an id of any length straight through to the handler.
    The server already refuses those —
    :func:`~creek_mcp.httpapi.journal.admissible_external_id` bounds the length
    and demands printable characters — so an unconstrained
    declaration understates the contract, and a consumer that pins this schema
    bundle pins the understatement.
    """
    declared = _path_parameters()
    assert declared, "the document declares no path parameters at all"
    for path, method, parameter in declared:
        schema = parameter["schema"]
        assert "maxLength" in schema, f"{method} {path}: {parameter['name']}"
        assert "pattern" in schema, f"{method} {path}: {parameter['name']}"


def test_the_external_id_bound_is_the_one_both_write_surfaces_share() -> None:
    """The published maximum is :data:`MAX_EXTERNAL_ID_CHARS` itself, not a restatement.

    #1524 made that constant the single canonical bound imported by the journal
    route and the upload model alike. A literal here would be a fourth spelling
    of it, free to drift the day the bound moves.
    """
    (_path, _method, parameter) = next(
        entry for entry in _path_parameters() if entry[2]["name"] == EXTERNAL_ID_PARAM
    )
    assert parameter["schema"]["maxLength"] == MAX_EXTERNAL_ID_CHARS
    assert parameter["schema"]["minLength"] == 1


@pytest.mark.parametrize(
    "external_id",
    [
        "adepthood:doc:2026-08-19",
        "abc",
        "a" * MAX_EXTERNAL_ID_CHARS,
        "..",
        "an id with spaces",
        "émoji-ok-\N{SNOWMAN}",
    ],
    ids=["namespaced", "short", "at-the-bound", "dot-dot", "spaces", "unicode"],
)
def test_the_published_pattern_admits_every_id_the_server_admits(
    external_id: str,
) -> None:
    """THE NON-VACUITY TWIN — the published constraint refuses nothing the server takes.

    A pattern *tighter* than the server is the more dangerous error of the two:
    it makes a generated client refuse, client-side, an id the vault would have
    accepted and already holds — so a consumer's own sync silently stops
    addressing entries it wrote. ``..`` is in the list deliberately: the server
    accepts it (``safe_stem`` digests the raw id rather than pathing on it), so
    the document must not pretend otherwise.

    Args:
        external_id: An id :func:`admissible_external_id` accepts.
    """
    assert admissible_external_id(external_id)
    assert re.fullmatch(EXTERNAL_ID_PATTERN, external_id) is not None


@pytest.mark.parametrize(
    "external_id",
    ["with\x00nul", "with\nnewline", "with\x7fdelete", "with/slash"],
    ids=["nul", "newline", "delete", "slash"],
)
def test_the_published_pattern_refuses_what_it_claims_to_refuse(
    external_id: str,
) -> None:
    """The pattern is not the identity: it rejects the bytes #1132 named.

    Without this, a pattern of ``.*`` would satisfy every assertion above.
    ``/`` is included because Starlette's converter already excludes it, so a
    published pattern that admitted it would document a path the router cannot
    match.

    Args:
        external_id: An id the published pattern must refuse.
    """
    assert re.fullmatch(EXTERNAL_ID_PATTERN, external_id) is None
