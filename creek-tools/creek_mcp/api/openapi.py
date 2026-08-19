"""The ``/v1`` OpenAPI document, generated from our models (#1074).

The document is a function of :data:`creek_mcp.api.models.CONTRACT_MODELS` and
:data:`creek_mcp.api.routes.ROUTES` and of nothing else. That is the whole
point, and it is the ADR's second reason for declining FastAPI: a framework's
generated OpenAPI document is a function of the framework's own version, so a
routine dependency upgrade would silently rewrite a document a consumer has
pinned.

**Components are the models plus their hoisted definitions.** Each committed
schema in ``docs/contracts/adepthood-v1/schemas/`` carries the definitions it
references inline under its own ``$defs``, which is what makes each file
independently resolvable. An OpenAPI document has one shared
``components/schemas`` namespace instead, so those definitions are hoisted out
and become siblings. Hoisting is not optional: publishing only the sixteen
registered models while rewriting ``#/$defs/X`` to ``#/components/schemas/X``
would emit a document whose references point at names it never defines, which
every standard validator and code generator rejects.

**Why each component is generated per model.** ``model_json_schema()`` is called
once per registered model with :data:`COMPONENT_REF_TEMPLATE`, and the only
difference from the call that produced the committed bytes is that reference
template — a pure string substitution. So "the component equals the committed
schema, ref-rewritten" holds by construction rather than by coincidence, which
is what ``tests/test_v1_api_openapi.py`` reads back off disk to check.

Like the rest of :mod:`creek_mcp.api`, this module imports no web framework.
"""

from __future__ import annotations

import re
from typing import Any, Final

from creek_mcp.api.models import (
    CONTRACT_MODELS,
    ERROR_MESSAGES,
    ERROR_STATUS,
    Capability,
    ErrorCode,
)
from creek_mcp.api.routes import (
    AUTHORIZATION_HEADER,
    CEILING_HEADER,
    CONTRACT_VERSION_HEADER,
    IMPLEMENTED_CAPABILITIES,
    ROUTES,
    RouteSpec,
)
from creek_mcp.contract import CONTRACT_VERSION

OPENAPI_VERSION: Final[str] = "3.1.0"
"""The OpenAPI dialect the document declares.

3.1 rather than 3.0 because 3.1 *is* JSON Schema 2020-12 — the dialect Pydantic
emits — so the component bodies are carried across unchanged instead of being
down-converted, which is what makes them comparable to the committed bytes.
"""

COMPONENT_REF_TEMPLATE: Final[str] = "#/components/schemas/{model}"
"""Where a model reference points inside this document.

Handed straight to ``model_json_schema(ref_template=...)``, so the generator
never rewrites a reference by hand.
"""

DOCUMENT_TITLE: Final[str] = "Creek Adepthood /v1 application API"
"""``info.title``. Names the surface, never the deployment or the vault."""

DOCUMENT_DESCRIPTION: Final[str] = (
    "Authenticated HTTP/JSON access to Creek's Adepthood-facing capabilities. "
    f"Every response this application builds carries Vary: {CEILING_HEADER}, "
    f"{AUTHORIZATION_HEADER} and Cache-Control: no-store — do not enable "
    "caching for /v1 on an intermediary. Every error is the ErrorEnvelope, and "
    "retryability is the static retry-policy.json table keyed on the error "
    "code alone."
)
"""``info.description``. Prose only; it states no fact about any vault.

Interpolated from the header constants rather than spelled out, so the sentence
a consumer reads and the header the server stamps cannot drift — the same
reason :func:`_parameters` publishes the parameter names from constants.

The caching half of it changed with #1129 and is a *description* change only:
no operation, parameter, schema or status moved, so the byte-pinned component
schemas under ``docs/contracts/adepthood-v1/schemas/`` are untouched and the
contract minor does not advance. It documents a response header, which OpenAPI
3.1 has no required place for on a document-wide basis, so prose is where it
goes until the per-response ``headers`` objects are worth generating.
"""

_DEFS_KEY: Final[str] = "$defs"
"""The key Pydantic hoists nested definitions under, and OpenAPI does not."""

_JSON_MEDIA_TYPE: Final[str] = "application/json"
"""The single media type ``/v1`` speaks, in both directions."""

_SUCCESS_STATUS: Final[str] = "200"
"""The one success status any operation documents."""

_PATH_PARAMETER: Final[re.Pattern[str]] = re.compile(r"{(\w+)}")
"""Matches a ``{name}`` placeholder in a route template."""

_ERROR_ENVELOPE_NAME: Final[str] = "ErrorEnvelope"
"""The component every non-``200`` response references."""

_UNIVERSAL_ERROR_CODES: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.UNAUTHENTICATED,
    ErrorCode.NOT_FOUND,
    ErrorCode.INVALID_REQUEST,
    ErrorCode.INTERNAL_ERROR,
    ErrorCode.TEMPORARILY_UNAVAILABLE,
)
"""The refusals reachable on every route, whatever it does.

Authentication, the routing miss, the edge-level ceiling and body-size
refusals, the error boundary and the two shedding limits all sit *above* the
router, so each of these is reachable on every published path. ``404`` is on
the list for the same reason it renders for a method mismatch: routing answers
before any handler does.

:attr:`~creek_mcp.api.models.ErrorCode.UNAVAILABLE` is deliberately absent even
though it shares ``503`` with ``TEMPORARILY_UNAVAILABLE`` — one status may
carry one documented response, and the transient one is the one every route can
actually produce.
"""

_CAPABILITY_ERROR_CODES: Final[dict[Capability, tuple[ErrorCode, ...]]] = {
    Capability.UPLOAD: (ErrorCode.UNSUPPORTED_SOURCE,),
}
"""Refusals only one capability can produce, keyed by that capability.

A table rather than an ``if spec.capability is Capability.UPLOAD`` in
:func:`_error_codes`, because the question "which refusals does this route
document" is data about the route, and the *next* capability-specific code
should be one line here rather than a second branch.

Only ``upload`` has an entry today.
:attr:`~creek_mcp.api.models.ErrorCode.UNSUPPORTED_SOURCE` is emitted by
:func:`creek_mcp.httpapi.upload.upload_refusal_code` and nowhere else, so
documenting it on every route would advertise a ``415`` five endpoints cannot
return — and omitting it from the one that can leaves a generated client with
no branch for the refusal it will meet the first time somebody uploads a
``conversations.json``.
"""

_HEALTH_RESPONSE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"status": {"const": "ok", "type": "string"}},
    "required": ["status"],
}
"""The liveness body, inline because it is not part of the published contract.

``GET /v1/health`` answers one constant object derived from nothing, so it has
no wire model and no fixture. Documenting it inline says exactly that, where a
registered model would imply a shape a consumer should code against.
"""


def _component_ref(model_name: str) -> dict[str, str]:
    """Return a ``$ref`` pointing at the named component.

    Args:
        model_name: The component's name.

    Returns:
        The one-key reference object.
    """
    return {"$ref": COMPONENT_REF_TEMPLATE.format(model=model_name)}


def _components() -> dict[str, Any]:
    """Return every ``components/schemas`` entry: the models plus their ``$defs``.

    Returns:
        Each registered model's schema with its own ``$defs`` stripped, plus
        every definition hoisted out of those ``$defs`` as a sibling. A name
        that appears in both is the same body either way, because both come
        from the same Pydantic model.
    """
    hoisted: dict[str, Any] = {}
    models: dict[str, Any] = {}
    for name, model in CONTRACT_MODELS.items():
        schema = model.model_json_schema(ref_template=COMPONENT_REF_TEMPLATE)
        hoisted.update(schema.get(_DEFS_KEY, {}))
        models[name] = {key: value for key, value in schema.items() if key != _DEFS_KEY}
    return {**hoisted, **models}


def _is_unbuilt(spec: RouteSpec) -> bool:
    """Return whether *spec* names a published-but-unimplemented capability.

    Args:
        spec: The route under consideration.

    Returns:
        ``True`` when the route answers ``501`` today.
    """
    if spec.capability is None:
        return False
    return spec.capability not in IMPLEMENTED_CAPABILITIES


def _error_codes(spec: RouteSpec) -> tuple[ErrorCode, ...]:
    """Return the error codes *spec* can produce, ordered by HTTP status.

    Args:
        spec: The route under consideration.

    Returns:
        The universal refusals, plus ``409``/``403`` for the routes that
        negotiate a contract minor and resolve vault objects, plus whatever
        :data:`_CAPABILITY_ERROR_CODES` reserves for this route's capability,
        plus ``501`` for the routes that are published but not yet built.
        Ordered by status so the document is byte-stable across runs.
    """
    codes = list(_UNIVERSAL_ERROR_CODES)
    if spec.requires_contract_version:
        codes += [ErrorCode.INCOMPATIBLE_VERSION, ErrorCode.PRIVACY_REFUSED]
    if spec.capability is not None:
        codes += _CAPABILITY_ERROR_CODES.get(spec.capability, ())
    if _is_unbuilt(spec):
        codes.append(ErrorCode.UNSUPPORTED_CAPABILITY)
    return tuple(sorted(codes, key=lambda code: ERROR_STATUS[code]))


def _json_content(schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap *schema* in the one media type ``/v1`` speaks.

    Args:
        schema: A schema object or reference.

    Returns:
        The ``content`` mapping for a request or response body.
    """
    return {_JSON_MEDIA_TYPE: {"schema": schema}}


def _success_response(spec: RouteSpec) -> dict[str, Any]:
    """Return the ``200`` response object for *spec*.

    Args:
        spec: The route under consideration.

    Returns:
        A reference to the route's published response model, or the inline
        liveness shape for the one route that has none.
    """
    schema: dict[str, Any] = (
        _HEALTH_RESPONSE_SCHEMA
        if spec.response_model is None
        else _component_ref(spec.response_model.__name__)
    )
    return {"description": spec.summary, "content": _json_content(schema)}


def _error_response(code: ErrorCode) -> dict[str, Any]:
    """Return the response object for one wire error code.

    Args:
        code: The error code being documented.

    Returns:
        The published constant message, and a reference to the single error
        envelope. Never an inline schema — a second error shape is where a
        field that echoes request or vault material eventually appears.
    """
    return {
        "description": ERROR_MESSAGES[code],
        "content": _json_content(_component_ref(_ERROR_ENVELOPE_NAME)),
    }


def _responses(spec: RouteSpec) -> dict[str, Any]:
    """Return every documented response for *spec*, keyed by status string.

    Args:
        spec: The route under consideration.

    Returns:
        The success response plus one entry per reachable refusal.
    """
    responses: dict[str, Any] = {_SUCCESS_STATUS: _success_response(spec)}
    for code in _error_codes(spec):
        responses[str(ERROR_STATUS[code])] = _error_response(code)
    return responses


def _header_parameter(name: str, *, required: bool, description: str) -> dict[str, Any]:
    """Return one header parameter declaration.

    Args:
        name: The header name, exactly as it travels on the wire.
        required: Whether the route refuses a request that omits it.
        description: What the header means to this server.

    Returns:
        The OpenAPI parameter object.
    """
    return {
        "name": name,
        "in": "header",
        "required": required,
        "description": description,
        "schema": {"type": "string"},
    }


def _parameters(spec: RouteSpec) -> list[dict[str, Any]]:
    """Return every parameter *spec* declares.

    A required header that is not in the document is a ``409`` the consumer
    cannot see coming from the contract alone, so the version header is
    published exactly on the routes that refuse without it.

    Args:
        spec: The route under consideration.

    Returns:
        Path parameters first, then the two ``/v1`` headers.
    """
    parameters: list[dict[str, Any]] = [
        {
            "name": name,
            "in": "path",
            "required": True,
            "description": "Consumer-side identifier for this entry.",
            "schema": {"type": "string"},
        }
        for name in _PATH_PARAMETER.findall(spec.path)
    ]
    if spec.requires_contract_version:
        parameters.append(
            _header_parameter(
                CONTRACT_VERSION_HEADER,
                required=True,
                description="Contract major.minor; matched by strict membership.",
            )
        )
    parameters.append(
        _header_parameter(
            CEILING_HEADER,
            required=False,
            description="Declared tier ceiling; absent reads as 'open'.",
        )
    )
    return parameters


def _operation(spec: RouteSpec) -> dict[str, Any]:
    """Return the OpenAPI operation object for *spec*.

    Args:
        spec: The route under consideration.

    Returns:
        The operation, including its request body when the route takes one.
    """
    operation: dict[str, Any] = {
        "operationId": spec.operation_id,
        "summary": spec.summary,
        "parameters": _parameters(spec),
        "responses": _responses(spec),
    }
    if spec.request_model is not None:
        operation["requestBody"] = {
            "required": True,
            "content": _json_content(_component_ref(spec.request_model.__name__)),
        }
    return operation


def _paths() -> dict[str, Any]:
    """Return the document's ``paths`` mapping.

    Returns:
        One path item per route template, each carrying the operations
        :data:`~creek_mcp.api.routes.ROUTES` declares for it.
    """
    paths: dict[str, Any] = {}
    for spec in ROUTES:
        paths.setdefault(spec.path, {})[spec.method.lower()] = _operation(spec)
    return paths


def build_openapi() -> dict[str, Any]:
    """Return the whole ``/v1`` OpenAPI document.

    Generated on demand and never committed: its agreement with the published
    fixture bundle is pinned by tests that read the committed schemas off disk,
    rather than by a second stored artifact that could drift from the first.

    Returns:
        The document, whose ``info.version`` is the runtime
        :data:`creek_mcp.contract.CONTRACT_VERSION` rather than a restated
        literal, so it cannot mis-negotiate against the server that serves it.
    """
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": DOCUMENT_TITLE,
            "version": CONTRACT_VERSION,
            "description": DOCUMENT_DESCRIPTION,
        },
        "paths": _paths(),
        "components": {"schemas": _components()},
    }
