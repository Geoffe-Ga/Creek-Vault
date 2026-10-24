"""Per-consumer bearer-token auth for the network MCP transport (#759).

When ``creek-tools-mcp`` is served over the network (streamable-http), every
request must present a bearer token that maps to a known **consumer identity** —
there is no anonymous access. Tokens are configured in the environment only
(never in code or the config file), mirroring the existing
``CREEK_MCP_ELEVATED_TOKEN`` fail-closed / constant-time precedent.

The SDK's :class:`~mcp.server.auth.provider.TokenVerifier` hook does the rest:
when a :class:`ConsumerTokenVerifier` is wired into ``FastMCP``, the SDK installs
``RequireAuthMiddleware`` (401 on a missing/invalid token, 403 on a missing
scope) and exposes the authenticated identity per-call via ``get_access_token()``
— which the server uses to stamp the consumer on the audit log and to apply the
remote tier-ceiling cap.

A consumer holds an **ordered set of currently-valid tokens** (#895), spelled as
a comma-separated list inside its own ``consumer=`` segment
(``adepthood=<old>,<new>``). Every token in the set authenticates as that one
consumer, so a secret is rotated by widening the set, letting the consumer
redeploy, and then narrowing it again: no cutover, and no window left open by
accident, because startup announces every consumer holding more than one token
(:meth:`ConsumerTokenVerifier.rotation_notice`, emitted by
:func:`announce_rotation_window`). The step-by-step runbook lives in the
network-transport section of ``docs/mcp.md``.

Two configurations are refused rather than resolved. A consumer named twice is
an error, because both readings rewrite the operator's intent — overwriting
discards a credential they believe is live, accumulating invents a rotation
window they never asked for — and the comma form says the supported thing
plainly. One token value configured for more than one consumer is an error
because :meth:`ConsumerTokenVerifier.verify_token` attributes a match to the
last consumer it scans, so a shared value would audit calls under the wrong
name.

Verified tokens carry a **finite lifetime** (#837): ``verify_token`` stamps
``expires_at`` at ``now + TTL`` (default 3600s) rather than issuing a
never-expiring credential. ``CREEK_MCP_TOKEN_TTL_SECONDS`` overrides the TTL;
a non-integer or non-positive value falls back to the default rather than
failing open with ``expires_at=None``. That expiry bounds an individually
captured :class:`AccessToken` object; it does not revoke the *configured*
secret, which is what the rotation window above is for.

Configured tokens must clear a **minimum-length floor** (#838): a token in
``CREEK_MCP_CONSUMER_TOKENS`` shorter than 32 characters is refused at load
time so a guessable secret never guards the wire. The floor applies to every
token in a consumer's set and not only the first, because a rotation is exactly
when a fresh secret gets typed in. Generate compliant tokens with
``python -c "import secrets; print(secrets.token_urlsafe(32))"``.

A consumer may also carry a **grant suffix** (#1964) naming which of the four
:class:`~creek_mcp.policy.Grant` profiles its token reaches —
``openclaw=<token>@vault:read+vault:desk``. ``@`` ends the token set and ``+``
separates the grants; the suffix belongs to the whole entry, so every token in
a rotation window shares it. The verifier stamps the grants onto the token's
``scopes``, which is exactly where an OAuth access token (C2) carries them, so
:func:`creek_mcp.policy.granted_by_scopes` is the one place either becomes a
grant set. A consumer with **no** suffix is *ungated*: it keeps the full reach a
consumer token had before #1964, its token carries
:data:`~creek_mcp.policy.UNGATED_SCOPE`, and startup names it as ungated on
stderr (:func:`announce_ungated_consumers`) so the operator can see what is
still open. An unknown grant or an empty suffix is refused at load time, naming
the consumer and the valid grants and never echoing the entry.

Stdio (local CrawDad / Claude Code) is unaffected: no verifier is wired, so
``get_access_token()`` is ``None`` and calls run as before.
"""

from __future__ import annotations

import hmac
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from creek_mcp.policy import DEFAULT_GRANTS, UNGATED_SCOPE, Grant
from creek_mcp.token_policy import require_min_length

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence, Set

CONSUMER_TOKENS_ENV: Final[str] = "CREEK_MCP_CONSUMER_TOKENS"
"""Env var holding ``consumer=token`` entries (tokens never in code).

``;`` separates one consumer from the next; ``,`` inside a single entry lists
the several tokens that consumer may currently present (#895); an optional
``@`` suffix names the consumer's grants, ``+``-separated (#1964).
"""

REMOTE_SCOPE: Final[str] = "creek:remote"
"""The scope every remote consumer token is granted (and the server requires)."""

TOKEN_TTL_ENV: Final[str] = "CREEK_MCP_TOKEN_TTL_SECONDS"
"""Env var overriding the verified-token lifetime in seconds (#837)."""

_REMOTE_TOKEN_TTL_SECONDS: Final[int] = 3600
"""Default lifetime of a verified bearer's ``AccessToken`` (one hour)."""

_CONSUMER_SEPARATOR: Final[str] = ";"
"""Separates one consumer's entry from the next in :data:`CONSUMER_TOKENS_ENV`."""

_TOKEN_SEPARATOR: Final[str] = ","
"""Separates the tokens within one consumer's entry (#895)."""

_NAME_VALUE_SEPARATOR: Final[str] = "="
"""Separates a consumer's name from its token set."""

_GRANT_SUFFIX_SEPARATOR: Final[str] = "@"
"""Ends a consumer's token set and begins its grant suffix (#1964).

The *last* ``@`` in the entry is the separator, so a legacy token that itself
contains ``@`` and carries a suffix still parses. ``secrets.token_urlsafe``
never emits ``@``.
"""

_GRANT_SEPARATOR: Final[str] = "+"
"""Separates the grants within one consumer's grant suffix (#1964)."""

_SETTLED_TOKEN_COUNT: Final[int] = 1
"""How many tokens a consumer holds when it is *not* mid-rotation."""

# Monkeypatchable clock alias: tests pin `_now` to a fixed instant so the
# expires_at arithmetic is exact; production keeps wall-clock time.
_now = time.time

# Non-secret placeholder URLs — this is a self-hosted resource server using bearer
# tokens, not an OAuth authorization-server flow, but AuthSettings requires them.
_ISSUER_URL: Final[str] = "https://creek.invalid/mcp"
_RESOURCE_URL: Final[str] = "https://creek.invalid/mcp"


def _token_ttl_seconds() -> int:
    """Return the verified-token TTL in seconds, defaulting to one hour.

    Reads :data:`TOKEN_TTL_ENV`; a non-integer or non-positive value falls
    back to :data:`_REMOTE_TOKEN_TTL_SECONDS` without raising, so a
    misconfigured environment degrades to the safe finite default instead of
    crashing verification (or, worse, issuing a non-expiring token).

    Returns:
        The token lifetime in seconds (always > 0).
    """
    raw = os.environ.get(TOKEN_TTL_ENV)
    if raw is None:
        return _REMOTE_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return _REMOTE_TOKEN_TTL_SECONDS
    return ttl if ttl > 0 else _REMOTE_TOKEN_TTL_SECONDS


def _validated_tokens(consumer: str, tokens: Sequence[str]) -> tuple[str, ...]:
    """Return *tokens* if every one clears the shared length floor, else raise (#838).

    Delegates to :func:`creek_mcp.token_policy.require_min_length`, which the
    elevated-token gate shares (#907), so both surfaces enforce one number
    with one wording. The floor is applied per token rather than to the set:
    a rotation window is exactly where a *new* secret gets typed in, so a
    check that only saw the incumbent would hold on the token nobody is about
    to start using. The error names the consumer and the observed/required
    lengths and gives the rotation recipe — it never echoes a token.

    Args:
        consumer: The consumer the tokens belong to (already stripped).
        tokens: That consumer's configured tokens (already stripped, non-empty).

    Returns:
        The tokens, unchanged and in order, when every one meets the minimum.

    Raises:
        ValueError: If any token is shorter than
            :data:`creek_mcp.token_policy.MIN_TOKEN_LEN`.
    """
    subject = f"consumer {consumer!r} token"
    return tuple(require_min_length(subject, token) for token in tokens)


def _token_segments(value: str) -> tuple[str, ...]:
    """Split one consumer's configured value into its non-empty tokens.

    Whitespace around a comma is operator formatting and never token material,
    and an empty segment (a doubled or trailing comma) is dropped *here* —
    before the length floor runs — so a stray comma is a typo the parser
    absorbs rather than a zero-character token it reports back at the operator.

    Args:
        value: The right-hand side of one ``consumer=`` entry.

    Returns:
        The configured tokens, stripped, in configured order; empty when the
        entry configures no token at all.
    """
    stripped = (segment.strip() for segment in value.split(_TOKEN_SEPARATOR))
    return tuple(segment for segment in stripped if segment)


@dataclass(frozen=True)
class _Entry:
    """One parsed ``consumer=tokens[@grants]`` entry.

    Attributes:
        consumer: The consumer name, stripped.
        tokens: The configured tokens, stripped, in configured order.
        grants: The suffix's grants, or ``None`` when the entry has no suffix
            (an ungated consumer).
    """

    consumer: str
    tokens: tuple[str, ...]
    grants: frozenset[Grant] | None


def _refuse_bad_grants(consumer: str) -> NoReturn:
    """Refuse a grant suffix that names no grant, or one that is not a grant.

    The message names the consumer and every valid grant, and deliberately
    **not** the offending suffix: the suffix is split off the entry at its last
    ``@``, so for a legacy token that itself contains ``@`` and carries no
    suffix, the "suffix" is the tail of a secret.

    Args:
        consumer: The consumer whose suffix is unreadable.

    Raises:
        ValueError: Always.
    """
    valid = ", ".join(grant.value for grant in Grant)
    msg = (
        f"consumer {consumer!r} has an unreadable grant suffix in "
        f"{CONSUMER_TOKENS_ENV}; after '{_GRANT_SUFFIX_SEPARATOR}' list one or "
        f"more of {valid}, separated by '{_GRANT_SEPARATOR}', or drop the "
        "suffix to leave the consumer ungated"
    )
    raise ValueError(msg)


def _parsed_grants(consumer: str, suffix: str) -> frozenset[Grant]:
    """Return the grants a consumer's suffix names, or refuse it.

    Whitespace around a ``+`` and an empty segment (``++``, a trailing ``+``)
    are operator formatting, absorbed the way :func:`_token_segments` absorbs
    stray commas. Matching is otherwise exact.

    Args:
        consumer: The consumer the suffix belongs to.
        suffix: Everything after the entry's last ``@``.

    Returns:
        The named grants.

    Raises:
        ValueError: Via :func:`_refuse_bad_grants`, when the suffix names no
            grant at all or names something that is not a grant.
    """
    names = [part.strip() for part in suffix.split(_GRANT_SEPARATOR)]
    named = [name for name in names if name]
    valid = {grant.value: grant for grant in Grant}
    if not named or any(name not in valid for name in named):
        _refuse_bad_grants(consumer)
    return frozenset(valid[name] for name in named)


def _parsed_entry(entry: str) -> _Entry | None:
    """Return what one env entry configures, or ``None``.

    ``None`` is the "nothing configured here" answer, and it is deliberately
    silent: a blank entry, a ``name=`` with no token, an ``=orphan`` with no
    name, and the comma spelling of the same thing (``name=,,``) are all
    operator formatting rather than errors — the single-token parser skipped
    them without comment before #895, and the comma form must reach the same
    verdict rather than registering a consumer the verifier then refuses.

    A grant suffix (#1964) is split off at the value's last ``@`` *before*
    the tokens are read, so a suffixed entry authenticates exactly the tokens
    an unsuffixed one would.

    Args:
        entry: One ``;``-delimited entry of :data:`CONSUMER_TOKENS_ENV`.

    Returns:
        The consumer name, its ordered token set and its grants, or ``None``
        when the entry names no consumer or configures no token.

    Raises:
        ValueError: When a configured consumer's grant suffix is unreadable.
    """
    name, _, value = entry.partition(_NAME_VALUE_SEPARATOR)
    consumer = name.strip()
    token_part, separator, suffix = value.rpartition(_GRANT_SUFFIX_SEPARATOR)
    if not separator:
        token_part = suffix
    tokens = _token_segments(token_part)
    if not consumer or not tokens:
        return None
    grants = _parsed_grants(consumer, suffix) if separator else None
    return _Entry(consumer=consumer, tokens=tokens, grants=grants)


def _refuse_repeated_consumer(consumer: str) -> NoReturn:
    """Refuse a consumer name configured more than once (#895).

    Before #895 the second entry silently overwrote the first, throwing away a
    credential the operator believed was live. Now that a consumer can
    legitimately hold several tokens, accumulating instead would be just as
    wrong: it would invent a rotation window nobody asked for. Both readings
    rewrite the intent, so the parser names the supported spelling and stops.

    Args:
        consumer: The repeated consumer name.

    Raises:
        ValueError: Always. The message names the consumer and the comma form,
            and never a token value.
    """
    msg = (
        f"consumer {consumer!r} is configured more than once in "
        f"{CONSUMER_TOKENS_ENV}; name each consumer once and give it a "
        "comma-separated set of currently-valid tokens instead "
        f"({consumer}=<current>,<replacement>)"
    )
    raise ValueError(msg)


def _refuse_shared_token(first: str, second: str) -> NoReturn:
    """Refuse a token value configured in more than one place (#895).

    One rule for two shapes — the same value twice inside one consumer's set,
    and the same value across two consumers — because they have one
    consequence: :meth:`ConsumerTokenVerifier.verify_token` scans every
    configured token without breaking and keeps the *last* match, so a shared
    value resolves to whichever site the scan saw last. Every audit line and
    access line that call produced would then name the wrong consumer. Within
    one set it is additionally a rotation that rotated nothing.

    Args:
        first: The consumer the value was first seen under.
        second: The consumer it was seen under again (possibly *first*).

    Raises:
        ValueError: Always. The message names both consumers and never the
            value they share.
    """
    msg = (
        "the same token value is configured twice — once for "
        f"{first!r} and once for {second!r}; every configured token value must "
        "be unique, because a match is attributed to the last consumer scanned "
        "and a shared value would audit the call under the wrong identity"
    )
    raise ValueError(msg)


def _token_set(consumer: str, configured: Sequence[str]) -> tuple[str, ...]:
    """Return *configured* as a tuple, refusing the two shapes that cannot work.

    Args:
        consumer: The consumer the tokens belong to.
        configured: The value the caller mapped that consumer to.

    Returns:
        The tokens as a tuple, in configured order.

    Raises:
        TypeError: If *configured* is a bare :class:`str`. ``str`` is itself a
            ``Sequence[str]``, so the annotation alone cannot catch one, and
            the value would be read as a single-character token per letter —
            each of which ``hmac.compare_digest`` accepts, degrading one
            43-character secret into an alphabet of valid credentials.
        ValueError: If *configured* is empty. A named consumer holding no
            token can never authenticate, which reads to an operator as an
            auth outage rather than as the typo it is.
    """
    if isinstance(configured, str):
        msg = (
            f"consumer {consumer!r} is configured with a bare string; pass a "
            "sequence of tokens (a 1-tuple for a settled consumer), because a "
            "bare string is itself a sequence of its own characters and would "
            "be read as one single-character token per letter"
        )
        raise TypeError(msg)
    # Materialise before testing emptiness, not after: an empty *iterable*
    # that is not a Sequence (a generator, a spent iterator) is truthy, so the
    # order matters. Reversed, such a value would slip past this refusal and
    # land as an empty tuple — the consumer would authenticate nobody, which
    # reads to an operator as an auth outage rather than the typo it is, and
    # the loud error promised below would never fire.
    resolved = tuple(configured)
    if not resolved:
        msg = (
            f"consumer {consumer!r} is configured with an empty token set, so "
            "it can never authenticate; give it at least one token, or remove "
            "the consumer"
        )
        raise ValueError(msg)
    return resolved


def names_a_consumer(consumer: str | None) -> bool:
    """Return whether *consumer* identifies somebody at all (#1100).

    One predicate, three callers, because a name that identifies nobody can
    enter the system at three different seams and the answer has to be the
    same at each: the configuration
    (:func:`_normalized_token_sets`), a custom verifier wired into the MCP
    transport (:class:`NamedConsumerVerifier`), and a verifier injected into
    the ``/v1`` app (:class:`creek_mcp.httpapi.auth.BearerAuthMiddleware`).

    Whitespace is stripped before the test rather than compared literally.
    ``''``, ``'   '`` and ``'\t'`` are three *distinct* mapping keys and three
    distinct verified identities, all of which render as no identity in an
    audit line — so a rule that only refused the empty string would leave two
    doors open, and per-consumer accounting would give a nameless caller a
    bucket per spelling.

    Args:
        consumer: The candidate identity, or ``None``.

    Returns:
        ``True`` when *consumer* carries at least one non-whitespace
        character.
    """
    return consumer is not None and bool(consumer.strip())


def access_has_expired(access: AccessToken) -> bool:
    """Return whether *access* carries an expiry that has already passed (#1267).

    It lives beside the registry rather than in the adapter, for the reason
    :mod:`creek_mcp.httpapi.auth` opens with (**one registry, one floor**): a
    rule spelled out inside the adapter is a second place for it to drift.
    That is a claim about *where* the rule lives, not about how much is
    shared — :class:`NamedConsumerVerifier` does not call this, and the MCP
    transport still gets its own answer from the SDK. What actually holds the
    two gates together is ``tests/test_admission_parity.py``, which measures
    both against one expected-verdict table.

    Before this existed, :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware`
    never read ``expires_at`` at all while the SDK's
    ``BearerAuthBackend.authenticate`` did, so one credential was ``401`` on
    one network and ``200`` on the other, and the finite lifetime #837 stamps
    on every verified bearer bounded nothing at all on the HTTP surface.

    ``expires_at=None`` means *no expiry* and is admitted, matching the SDK.
    Refusing it would make ``/v1`` stricter than the MCP transport in a case
    that is behaviourally reachable and already pinned: ``_GhostVerifier`` in
    ``tests/test_v1_api_hardening.py`` mints ``None`` through the documented
    ``create_app(verifier=...)`` seam and is asserted to reach a limiter
    ``503``, which only happens past this gate.

    ``expires_at=0`` is read as an expiry in January 1970 and **refused**,
    where the SDK's truthiness guard (``if auth_info.expires_at and
    auth_info.expires_at < int(time.time())``) reads the falsy integer as an
    absent one and serves it. That divergence is deliberate and
    one-directional — ``/v1`` refuses a credential the MCP transport would
    serve, never the reverse — and unreachable in production, because
    :func:`_token_ttl_seconds` is always positive so
    :meth:`ConsumerTokenVerifier.verify_token` cannot mint it. Only an
    injected verifier can. (A *negative* ``expires_at`` is truthy, so both
    surfaces refuse it; the divergence is exactly and only the value ``0``.)

    The two paragraphs above point opposite ways and one rule reconciles them:
    never **admit** what the MCP transport refuses, and only **refuse** what it
    admits where the value is unreachable in production and the SDK's reading
    of it is a falsy-integer accident rather than a decision.

    Args:
        access: A verified access token.

    Returns:
        ``True`` when *access* carries an expiry and that instant has passed.
    """
    return access.expires_at is not None and access.expires_at < int(_now())


def _refuse_unnamed_consumer(consumer: str) -> NoReturn:
    """Refuse a configured consumer whose name identifies nobody (#1100).

    The name is not decoration: it is stamped on every audit line, every
    access line and every ceiling decision the credential produces, and it is
    the key any per-consumer accounting is bucketed by. Admitting a blank one
    writes ``consumer=''`` into the trail that exists to say who acted.

    Args:
        consumer: The offending key, as configured.

    Raises:
        ValueError: Always. The message names the offending key and never a
            token value.
    """
    msg = (
        f"a configured entry names no consumer ({consumer!r}); every consumer "
        "needs a name with at least one non-whitespace character, because the "
        "name is stamped on every audit line, access line and ceiling "
        "decision the credential produces"
    )
    raise ValueError(msg)


def _require_globally_unique(tokens: Mapping[str, tuple[str, ...]]) -> None:
    """Refuse any token value that appears more than once in *tokens*.

    Args:
        tokens: The normalised ``{consumer: (token, ...)}`` configuration.

    Raises:
        ValueError: Via :func:`_refuse_shared_token`, on the first repeat —
            same-consumer and cross-consumer alike, since they are one rule.
    """
    owner: dict[str, str] = {}
    for consumer, configured in tokens.items():
        for token in configured:
            previous = owner.get(token)
            if previous is not None:
                _refuse_shared_token(previous, consumer)
            owner[token] = consumer


def _normalized_token_sets(
    tokens: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Return *tokens* as a validated ``{consumer: (token, ...)}`` mapping.

    The one normalisation both entry points share — the environment parser
    (:func:`load_consumer_tokens`) and the direct constructor
    (:class:`ConsumerTokenVerifier`). Callers that never see the environment
    build verifiers from literal maps (``tests/v1_api_support.py``, the wire
    tests), so the constructor is the narrowest ceiling these invariants can
    sit under; stating them once is what stops the two paths from disagreeing
    about what a valid configuration is.

    The #838 length floor is deliberately *not* enforced here. It is a
    load-time rule about what an operator may put in the environment, and the
    in-process callers above legitimately construct short literals.

    Args:
        tokens: A ``{consumer: sequence-of-tokens}`` mapping.

    Returns:
        The same mapping with every value a tuple.

    Raises:
        TypeError: If any value is a bare :class:`str`.
        ValueError: If a consumer's name identifies nobody (#1100), if a named
            consumer holds no tokens, or if one token value is configured more
            than once.
    """
    for consumer in tokens:
        if not names_a_consumer(consumer):
            _refuse_unnamed_consumer(consumer)
    normalized = {
        consumer: _token_set(consumer, configured)
        for consumer, configured in tokens.items()
    }
    _require_globally_unique(normalized)
    return normalized


def load_consumer_tokens(
    environ: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Parse ``CREEK_MCP_CONSUMER_TOKENS`` into a ``{consumer: tokens}`` map.

    Format: ``adepthood=<token>,<token>;other=<token>``. The ``;`` separates
    consumers; the ``,`` lists the tokens one consumer may currently present
    (#895), so ``adepthood=<old>,<new>`` is an open rotation window and
    ``adepthood=<new>`` has closed it. A single-token configuration is
    unchanged on the wire and lands as a one-element tuple, so an operator who
    has never rotated anything parses exactly as before.

    A grant suffix (``openclaw=<token>@vault:read+vault:desk``, #1964) is
    validated and stripped here, so every caller that only needs the tokens —
    the ``/v1`` app, the container runtime, provisioning — authenticates a
    suffixed entry exactly as an unsuffixed one. Read the grants themselves
    with :func:`load_consumer_grants`.

    Blank entries, entries with no name, and entries with no token — including
    the comma spelling ``name=,,`` — are skipped. Returns an empty dict when
    unset: the caller treats "no tokens configured" as "network mode not
    permitted" (no anonymous access).

    Args:
        environ: Environment mapping (defaults to :data:`os.environ`).

    Returns:
        A ``{consumer: (token, ...)}`` mapping, tokens in configured order.

    Raises:
        ValueError: If a configured token is shorter than
            :data:`creek_mcp.token_policy.MIN_TOKEN_LEN` characters (#838), if
            a consumer is named more than once, if one token value is
            configured more than once, or if a grant suffix is unreadable
            (#1964). Every message names the configuration
            at fault — consumers, lengths, the rotation recipe — and never a
            token value, because a startup error lands in logs, terminals and
            process supervisors.
    """
    return _normalized_token_sets(_load_registry(environ)[0])


def load_consumer_grants(
    environ: Mapping[str, str] | None = None,
) -> dict[str, frozenset[Grant]]:
    """Parse the grant suffixes of ``CREEK_MCP_CONSUMER_TOKENS`` (#1964).

    Reads the same registry :func:`load_consumer_tokens` does, with every one
    of its refusals, so the two can never disagree about whether a
    configuration is valid. Pass the result to :class:`ConsumerTokenVerifier`
    as ``grants=`` alongside the tokens.

    Args:
        environ: Environment mapping (defaults to :data:`os.environ`).

    Returns:
        ``{consumer: grants}`` for every consumer that carries a suffix. A
        consumer with no suffix is absent: it is ungated.

    Raises:
        ValueError: On everything :func:`load_consumer_tokens` refuses, and on
            an unreadable grant suffix.
    """
    return _load_registry(environ)[1]


def _load_registry(
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[Grant]]]:
    """Parse and validate the registry into its token and grant halves.

    Args:
        environ: Environment mapping (defaults to :data:`os.environ`).

    Returns:
        ``({consumer: tokens}, {consumer: grants})``; the second holds only the
        consumers that carry a grant suffix.

    Raises:
        ValueError: On a weak token, a repeated consumer, a token value
            configured twice, or an unreadable grant suffix — the full set, so
            either public loader refuses exactly what the other does.
    """
    raw = (environ if environ is not None else os.environ).get(CONSUMER_TOKENS_ENV, "")
    tokens: dict[str, tuple[str, ...]] = {}
    grants: dict[str, frozenset[Grant]] = {}
    for line in raw.split(_CONSUMER_SEPARATOR):
        entry = _parsed_entry(line)
        if entry is None:
            continue
        if entry.consumer in tokens:
            _refuse_repeated_consumer(entry.consumer)
        tokens[entry.consumer] = _validated_tokens(entry.consumer, entry.tokens)
        if entry.grants is not None:
            grants[entry.consumer] = entry.grants
    _normalized_token_sets(tokens)
    return tokens, grants


class ConsumerTokenVerifier(TokenVerifier):
    """Verifies a bearer token against the configured per-consumer token sets.

    Comparison is constant-time (``hmac.compare_digest``) against every known
    token so a match leaks no timing signal about which consumer — or which
    token of that consumer's set — matched. A verified token yields an
    :class:`AccessToken` whose ``client_id`` is the consumer name and whose
    scopes are :data:`REMOTE_SCOPE` plus the consumer's grants (#1964) — or,
    for a consumer configured with no grants, plus
    :data:`~creek_mcp.policy.UNGATED_SCOPE`.

    A consumer maps to a **sequence** of tokens and never to a bare ``str``.
    The signature is not ``str | Sequence[str]``, because that union is
    unsound: ``str`` already *is* a ``Sequence[str]``, so the union collapses
    and a type checker would wave a bare secret through. :func:`_token_set`
    is the runtime half of the same guard.
    """

    def __init__(
        self,
        tokens: Mapping[str, Sequence[str]],
        grants: Mapping[str, Set[Grant]] | None = None,
    ) -> None:
        """Store the ``{consumer: tokens}`` map, and any grants, to verify against.

        Args:
            tokens: The configured consumers and their ordered token sets. An
                empty mapping is legal and refuses every bearer: "no consumers
                configured" is the callers' *network denied* posture, which is
                a different statement from "this consumer has no tokens".
            grants: ``{consumer: grants}`` for the consumers that are gated,
                normally :func:`load_consumer_grants`. A consumer absent from
                it — every consumer, when this is ``None`` — is ungated and
                keeps its pre-#1964 reach.

        Raises:
            TypeError: If a consumer's value is a bare :class:`str`.
            ValueError: If a named consumer holds no tokens, if one token
                value is configured more than once, or if *grants* names a
                consumer with no tokens or gives one an empty grant set.
        """
        self._tokens = _normalized_token_sets(tokens)
        self._grants = _normalized_grants(self._tokens, grants or {})

    @property
    def consumers(self) -> tuple[str, ...]:
        """Return the configured consumer names, in configured order.

        The public read of what was private state. A limiter that buckets by
        consumer has to build its buckets **eagerly, from this**, and never
        lazily from whatever name arrives: a map that grew on first sight would
        hand a bucket to every distinct identity any future verifier let
        through, turning a limit into an amplifier (#1110). Reaching into
        ``_tokens`` from another module would work and would also make that
        module's correctness depend on this one's internals.

        No token value is reachable through this — only the names, which are
        already stamped on every audit and access line.

        Returns:
            The configured consumer names.
        """
        return tuple(self._tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the consumer's :class:`AccessToken` for a valid token, else ``None``.

        Iterates every configured token of every consumer with constant-time
        compares (never a dict lookup) and **never breaks early**, not even
        when the first token matches: an early exit would make verification
        time depend on where in the configuration the presented token sits,
        which is a positional oracle over the token set. The operands are
        compared as **UTF-8 bytes**, not ``str``: ``compare_digest`` raises
        ``TypeError`` on a non-ASCII ``str`` but compares cleanly on ``bytes``,
        so a non-ASCII bearer is *rejected* (returns ``None``) rather than
        crashing verification (#776).

        Every token in a consumer's set resolves to that same consumer, so an
        open rotation window does not change the caller's identity for its
        duration — a window that did would rewrite every audit line written
        while it was open (#895).

        The returned token expires ``_token_ttl_seconds()`` from now — never
        ``expires_at=None`` — bounding how long any individually captured
        :class:`AccessToken` (e.g. one logged or cached outside this call)
        stays valid (#837). That expiry does not revoke the underlying shared
        secret: the SDK's bearer middleware calls ``verify_token`` fresh on
        every request, so a consumer that keeps presenting a configured value
        is re-verified indefinitely. What revokes it is dropping it from
        :data:`CONSUMER_TOKENS_ENV` and restarting — the closing half of the
        rotation runbook in the network-transport section of ``docs/mcp.md``.

        Args:
            token: The bearer token presented on the request.

        Returns:
            The matching consumer's access token, or ``None`` when no
            configured token matches.
        """
        supplied = token.encode("utf-8")
        matched: str | None = None
        for consumer, known_tokens in self._tokens.items():
            for known in known_tokens:
                if hmac.compare_digest(supplied, known.encode("utf-8")):
                    matched = consumer
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched,
            scopes=[REMOTE_SCOPE, *self._grant_scopes(matched)],
            expires_at=int(_now()) + _token_ttl_seconds(),
        )

    def _grant_scopes(self, consumer: str) -> list[str]:
        """Return the grant scopes *consumer*'s tokens carry (#1964).

        Args:
            consumer: A configured consumer.

        Returns:
            Its grants' values in :class:`~creek_mcp.policy.Grant` declaration
            order, so the scope list is stable; or the ungated marker alone
            when the consumer has no configured grants.
        """
        held = self._grants.get(consumer)
        if held is None:
            return [UNGATED_SCOPE]
        return [grant.value for grant in Grant if grant in held]

    def ungated_notice(self) -> str | None:
        """Return the operator notice naming every ungated consumer, else ``None``.

        An ungated consumer — one configured with no grant suffix — keeps the
        full reach a consumer token had before #1964, writes included, so it is
        named at every start until it is gated, the way an open rotation window
        is. Built from consumer **names** and grant names only, so it cannot
        echo a secret by construction.

        Returns:
            A message naming each ungated consumer and the suffix that would
            give it the default profile, or ``None`` when every consumer is
            gated.
        """
        ungated = [name for name in self._tokens if name not in self._grants]
        if not ungated:
            return None
        default = _GRANT_SEPARATOR.join(
            grant.value for grant in Grant if grant in DEFAULT_GRANTS
        )
        return (
            f"{CONSUMER_TOKENS_ENV}: ungated consumer(s) — {', '.join(ungated)}. "
            "Each reaches every MCP tool a consumer token reached before "
            "per-agent grants, writes included. Gate one by appending a grant "
            f"suffix to its entry, e.g. {ungated[0]}=<token>"
            f"{_GRANT_SUFFIX_SEPARATOR}{default}."
        )

    def rotation_notice(self) -> str | None:
        """Return the operator notice for any open rotation window, else ``None``.

        A window is a consumer holding more than one currently-valid token.
        The steady state is silent on purpose: a notice printed on every start
        is a notice operators stop reading, at which point the one that
        matters — "you left a window open three months ago" — goes unread too.
        A consumer holding exactly one token is therefore not named.

        The message is built from consumer **names and counts only**, so it
        cannot echo a secret by construction. It is printed at startup, which
        means it lands in logs, terminals and process supervisors: the same
        audience, and the same reason, as the #838 refusal message.

        Returns:
            A message naming every consumer mid-rotation and how many tokens
            it holds, or ``None`` when no consumer holds more than one.
        """
        windows = [
            (consumer, len(configured))
            for consumer, configured in self._tokens.items()
            if len(configured) > _SETTLED_TOKEN_COUNT
        ]
        if not windows:
            return None
        held = "; ".join(
            f"{consumer} holds {count} tokens" for consumer, count in windows
        )
        return (
            f"{CONSUMER_TOKENS_ENV}: rotation window open — {held}. "
            "Each of those tokens authenticates as its consumer for as long as "
            "it stays configured; drop the retired secret and restart once the "
            "consumer has redeployed."
        )


class NamedConsumerVerifier(TokenVerifier):
    """Wrap another verifier, refusing any token that names no consumer (#1100).

    :class:`ConsumerTokenVerifier` cannot mint a nameless identity any more —
    :func:`_normalized_token_sets` refuses the configuration outright — but
    :func:`creek_mcp.server.build_server` accepts *any*
    :class:`~mcp.server.auth.provider.TokenVerifier`, and a custom one runs
    none of that validation. This wrapper is the boundary guard for that case,
    and it is deliberately the *only* thing it does: identity, not
    credentials. The inner verifier still decides whether the bearer is valid.

    Mapping "authenticated but unnamed" onto ``None`` rather than raising is
    what makes the refusal a ``401``: the SDK's ``RequireAuthMiddleware``
    answers ``401`` for ``None``, so the call is refused before any tool
    dispatches, before :func:`creek_mcp.policy.effective_consumer` is
    consulted, and before an audit entry can be written under an empty name.
    """

    def __init__(self, inner: TokenVerifier) -> None:
        """Wrap *inner*.

        Args:
            inner: The verifier that actually checks the credential.
        """
        self._inner = inner

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return *inner*'s answer, unless it names nobody.

        Args:
            token: The bearer token presented on the request.

        Returns:
            The inner verifier's :class:`AccessToken` when it names a
            consumer, else ``None`` — the same answer an unknown token gets,
            so a caller cannot tell the two refusals apart.
        """
        access = await self._inner.verify_token(token)
        if access is None or not names_a_consumer(access.client_id):
            return None
        return access


def _normalized_grants(
    tokens: Mapping[str, tuple[str, ...]], grants: Mapping[str, Set[Grant]]
) -> dict[str, frozenset[Grant]]:
    """Return *grants* as frozensets, refusing the shapes that cannot work.

    Args:
        tokens: The verifier's normalised token sets.
        grants: The constructor's ``{consumer: grants}`` mapping.

    Returns:
        The same mapping with every value a :class:`frozenset`.

    Raises:
        ValueError: Via :func:`_refuse_bad_grants` for an empty grant set —
            a consumer that authenticates and can call nothing is a typo, not
            a posture — or when *grants* names a consumer holding no tokens.
    """
    normalized: dict[str, frozenset[Grant]] = {}
    for consumer, held in grants.items():
        if consumer not in tokens:
            msg = (
                f"grants are configured for consumer {consumer!r}, which holds "
                "no token; configure its token or drop its grants"
            )
            raise ValueError(msg)
        if not held:
            _refuse_bad_grants(consumer)
        normalized[consumer] = frozenset(held)
    return normalized


def announce_ungated_consumers(verifier: ConsumerTokenVerifier) -> None:
    """Print *verifier*'s ungated-consumer notice to stderr, when it has one.

    stderr for the reason :func:`announce_rotation_window` gives: stdout on
    the MCP entry point belongs to the stdio transport's JSON-RPC framing.

    Args:
        verifier: The verifier the MCP adapter is about to serve behind.
    """
    notice = verifier.ungated_notice()
    if notice is not None:
        print(notice, file=sys.stderr)


def announce_rotation_window(verifier: ConsumerTokenVerifier) -> None:
    """Print *verifier*'s rotation notice to stderr, when it has one (#895).

    **stderr, never stdout**, on both entry points that call this, for two
    independent reasons: ``creek-tools-mcp``'s stdio transport owns stdout for
    JSON-RPC framing, and ``creek-tools-api --print-openapi`` writes a
    machine-readable document there that consumers pipe into client
    generators. A notice on stdout would corrupt the first and break every
    pipe consuming the second — for exactly the operators who are mid-rotation
    and least able to afford a second broken thing.

    Shared by the two adapters rather than written twice, so that stream
    decision cannot hold on one entry point and lapse on the other.

    Args:
        verifier: The verifier the adapter is about to serve behind.
    """
    notice = verifier.rotation_notice()
    if notice is not None:
        print(notice, file=sys.stderr)


def remote_auth_settings() -> AuthSettings:
    """Return the :class:`AuthSettings` requiring the remote scope on every call."""
    return AuthSettings(
        issuer_url=AnyHttpUrl(_ISSUER_URL),
        resource_server_url=AnyHttpUrl(_RESOURCE_URL),
        required_scopes=[REMOTE_SCOPE],
    )


def secrets_match(supplied: bytes, expected: bytes) -> bool:
    """Compare two already-encoded secret values in constant time."""
    return hmac.compare_digest(supplied, expected)
