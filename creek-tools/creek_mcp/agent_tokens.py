"""JWKS-verified, audience-bound agent access tokens (#1963).

An agent a person connected through Adepthood presents a short-lived JWT that
Adepthood signed (adepthood#3533). :class:`JwtAgentTokenVerifier` is the
:class:`~mcp.server.auth.provider.TokenVerifier` that decides whether this
vault accepts it, and every rule it applies is a refusal the MCP authorization
specification makes mandatory or that the threat model needs:

* **Signature against the issuer's JWKS**, fetched from the configured
  :attr:`~creek_mcp.oauth_resource.ResourceServerConfig.jwks_url` and cached
  (:class:`JwksCache`: TTL :data:`JWKS_TTL_SECONDS`, refreshed early on an
  unknown ``kid`` at most every :data:`JWKS_MIN_REFRESH_SECONDS`; an issuer
  outage keeps the last good set and is logged once). The key is
  chosen by ``kid`` only — never by a ``jku``, ``x5u`` or embedded ``jwk``
  header — and only RSA and EC P-256 public keys can enter the set, so a
  symmetric key, and therefore every ``HS*`` token, is unverifiable by
  construction.
* **Algorithm allowlist** :data:`ACCEPTED_ALGORITHMS` (``RS256``, ``ES256``),
  and the header's ``alg`` must equal the selected key's own algorithm, so no
  token chooses how its signature is checked. ``none`` never verifies.
* **Issuer pinned** to the configured authorization server, verbatim.
* **Audience equal to this vault's MCP URL exactly** (RFC 8707): one string,
  byte-identical, never a list — a token two resource servers would both accept
  is how a token gets passed through. A token whose audience names another
  vault is refused.
* ``exp``, ``nbf`` and ``iat`` with :data:`CLOCK_SKEW_SECONDS` of skew, and
  every claim the verifier relies on is required (:data:`REQUIRED_CLAIMS`).
* **An optional subject pin.** A managed vault serves one person, and its
  static registry already names that person's consumer identity — the same
  identity Adepthood puts in ``sub``. Pinned to it, the vault refuses any other
  person's token even if one were ever routed to it.
* **Revocation, on every call** (#2005, owner decision R3-2), when a
  :class:`~creek_mcp.agent_revocation.RevocationCache` is configured: after
  every offline rule passes and before any access token is returned, the
  issuer is asked whether the agent's grant still covers the token's
  ``(sub, client_id, iat)``. ``active`` is reused for
  :data:`~creek_mcp.agent_revocation.REVOCATION_CACHE_SECONDS`; no answer
  refuses (fail closed). Without one, startup warns that revocation is not
  enforced and a stopped agent's token works until it expires.

The RFC 9068 ``client_id`` claim is required: a non-blank string of at most
256 characters, with no ASCII control character, exactly the published activity
consumer bound. It becomes the :class:`~mcp.server.auth.provider.AccessToken`'s
``client_id`` verbatim, so each agent has its own audit stamp and Adepthood can
join it directly to ``agent_grant.client_id``, including CIMD URLs. There is no
fallback for old tokens without it; the agent must obtain a fresh issuer token.
``sub`` stays separate as ``AccessToken.subject``: only it may select the
person's vault at the shared edge or satisfy a managed vault's subject pin.
The token's ``scopes`` are
:data:`~creek_mcp.remote_auth.REMOTE_SCOPE` plus the grants named in the
``scope`` claim, read through the same :class:`~creek_mcp.policy.Grant` values
:func:`creek_mcp.policy.granted_by_scopes` matches. The ungated marker is
never passed through from a token, and a token naming no grant this vault knows
carries no ``creek:remote`` either, so the SDK refuses it ``403
insufficient_scope`` before any tool is listed.

**No passthrough.** The token authenticates the call to *this* vault and goes
no further: nothing here, and nothing that reads the request-scoped access
token, forwards it to a provider, the pool or Drive.

**Nothing secret is logged.** A refusal is logged by a fixed category only —
never the token, never a ``kid`` (which an attacker chooses), never an
exception's own message, never another vault's URL.

:class:`ComposedTokenVerifier` puts this beside the static
:class:`~creek_mcp.remote_auth.ConsumerTokenVerifier` on the same endpoint: a
JWT-shaped bearer is tried as a JWT first, and anything else — or a JWT this
verifier refused — gets the static registry's constant-time match, so a
self-hosted operator's static tokens keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Final, Protocol

import httpx
import jwt
from jwt import PyJWK
from mcp.server.auth.provider import AccessToken, TokenVerifier

from creek_mcp.agent_revocation import (
    STATUS_URL_ENV,
    Revocation,
    TokenStatusQuery,
    load_revocation_cache,
)
from creek_mcp.api.models import (
    AGENT_ACTIVITY_CONSUMER_PATTERN,
    AGENT_ACTIVITY_MAX_CONSUMER_CHARS,
)
from creek_mcp.oauth_resource import load_resource_server_config
from creek_mcp.policy import Grant
from creek_mcp.remote_auth import REMOTE_SCOPE, names_a_consumer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from creek_mcp.agent_revocation import RevocationCache
    from creek_mcp.oauth_resource import ResourceServerConfig
    from creek_mcp.remote_auth import ConsumerTokenVerifier

AGENT_TOKEN_LOGGER_NAME: Final[str] = "creek_mcp.agent_tokens"
"""Where a refused token or an unreachable key set is noted, content-free."""

_LOGGER: Final[logging.Logger] = logging.getLogger(AGENT_TOKEN_LOGGER_NAME)

ACCEPTED_ALGORITHMS: Final[frozenset[str]] = frozenset({"RS256", "ES256"})
"""The only signature algorithms a token may use. Never ``none``, never ``HS*``."""

CLOCK_SKEW_SECONDS: Final[int] = 30
"""Leeway applied to ``exp``, ``nbf`` and ``iat``."""

JWKS_TTL_SECONDS: Final[int] = 300
"""How long a fetched key set is used before it is fetched again."""

JWKS_MIN_REFRESH_SECONDS: Final[int] = 60
"""The floor between two fetches, so a flood of unknown ``kid`` values cannot
turn every request into a call to the issuer."""

JWKS_FETCH_TIMEOUT_SECONDS: Final[float] = 5.0
"""How long one key-set fetch may take."""

JWKS_MAX_BYTES: Final[int] = 64 * 1024
"""The largest key-set document read. A real one is a few hundred bytes."""

MAX_TOKEN_LENGTH: Final[int] = 8 * 1024
"""The longest bearer treated as a JWT at all."""

REQUIRED_CLAIMS: Final[tuple[str, ...]] = (
    "iss",
    "sub",
    "aud",
    "exp",
    "iat",
    "client_id",
)
"""Every claim the verifier relies on; a token without one is refused."""

_SCOPE_CLAIM: Final[str] = "scope"
_SUBJECT_CLAIM: Final[str] = "sub"
_CLIENT_ID_CLAIM: Final[str] = "client_id"
_EXPIRY_CLAIM: Final[str] = "exp"
_ISSUER_CLAIM: Final[str] = "iss"
_ISSUED_AT_CLAIM: Final[str] = "iat"
_ALG_HEADER: Final[str] = "alg"
_KID_HEADER: Final[str] = "kid"
_CRIT_HEADER: Final[str] = "crit"
_PUBLIC_KEY_TYPES: Final[frozenset[str]] = frozenset({"RSA", "EC"})
_PRIVATE_MEMBERS: Final[frozenset[str]] = frozenset({"d", "p", "q", "dp", "dq", "qi"})
_SIGNATURE_USE: Final[str] = "sig"
_HTTP_OK: Final[int] = 200
_JWT_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
)
"""Three base64url segments; the third may be empty only for ``alg: none``,
which is shaped like a JWT precisely so that it can be refused as one."""

_REFUSAL_CATEGORIES: Final[tuple[tuple[type[jwt.PyJWTError], str], ...]] = (
    (jwt.ExpiredSignatureError, "expired"),
    (jwt.ImmatureSignatureError, "not yet valid"),
    (jwt.InvalidAudienceError, "audience"),
    (jwt.InvalidIssuerError, "issuer"),
    (jwt.MissingRequiredClaimError, "missing claim"),
    (jwt.InvalidSignatureError, "signature"),
)
"""Fixed log categories, most specific first. Never an exception's message."""

_KEY_SET_UNAVAILABLE: Final[str] = "issuer key set is unavailable"

REVOCATION_NOT_ENFORCED: Final[str] = (
    "agent-token revocation is not enforced: no status endpoint is configured, "
    "so a stopped agent keeps working until its access token expires"
)
"""The startup warning for an OAuth vault or edge with no revocation check."""


class JwksUnavailableError(OSError):
    """The issuer's key set could not be fetched or read. Names no URL."""


class JwksFetcher(Protocol):
    """Return the issuer's current JWKS document, or raise :class:`OSError`."""

    async def __call__(self) -> object:
        """Fetch the document.

        Returns:
            The decoded JSON.
        """


class HttpJwksFetcher:
    """Fetch a JWKS document over HTTPS: no redirects, no credential, bounded."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = JWKS_FETCH_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Bind the key-set URL.

        Args:
            url: The already-validated JWKS URL.
            timeout: Seconds one fetch may take.
            transport: An injected transport, for tests.
        """
        self._url = url
        self._timeout = timeout
        self._transport = transport

    async def __call__(self) -> object:
        """GET the key set and decode it.

        A redirect is refused rather than followed: the URL is pinned
        configuration, and following one would let whoever answers it choose
        the keys this vault trusts.

        Returns:
            The decoded JSON document.

        Raises:
            JwksUnavailableError: On a transport failure, a status other than
                ``200``, a body over :data:`JWKS_MAX_BYTES`, or a body that is
                not JSON.
        """
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
                client.stream("GET", self._url) as response,
            ):
                if response.status_code != _HTTP_OK:
                    raise JwksUnavailableError(_KEY_SET_UNAVAILABLE)
                body = await _bounded_body(response)
        except httpx.HTTPError:
            raise JwksUnavailableError(_KEY_SET_UNAVAILABLE) from None
        try:
            return json.loads(body)
        except ValueError:
            raise JwksUnavailableError(_KEY_SET_UNAVAILABLE) from None


async def _bounded_body(response: httpx.Response) -> bytes:
    """Read *response*'s body, refusing one larger than :data:`JWKS_MAX_BYTES`.

    Args:
        response: A streamed response.

    Returns:
        The body.

    Raises:
        JwksUnavailableError: When the body exceeds the cap.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > JWKS_MAX_BYTES:
            raise JwksUnavailableError(_KEY_SET_UNAVAILABLE)
        chunks.append(chunk)
    return b"".join(chunks)


def _usable_entry(entry: object) -> dict[str, object] | None:
    """Return *entry* when it is a public signing key this vault may trust.

    Args:
        entry: One member of a JWKS ``keys`` array.

    Returns:
        The entry, or ``None`` for anything but a public RSA or EC key with a
        non-empty string ``kid``, marked for signing or unmarked.
    """
    if not isinstance(entry, dict):
        return None
    kid = entry.get(_KID_HEADER)
    if (
        not isinstance(kid, str)
        or not kid
        or entry.get("kty") not in _PUBLIC_KEY_TYPES
        or entry.get("use", _SIGNATURE_USE) != _SIGNATURE_USE
        or _PRIVATE_MEMBERS & entry.keys()
    ):
        return None
    return entry


def _parsed_key(entry: dict[str, object]) -> PyJWK | None:
    """Return *entry* as a verification key, when its algorithm is accepted.

    Args:
        entry: A usable JWKS entry.

    Returns:
        The key, or ``None`` when it does not parse or its algorithm — declared
        or inferred from its curve — is outside :data:`ACCEPTED_ALGORITHMS`.
    """
    try:
        key = PyJWK(entry)
    except jwt.PyJWTError:
        return None
    return key if key.algorithm_name in ACCEPTED_ALGORITHMS else None


def parse_jwks(document: object) -> dict[str, PyJWK]:
    """Return the trusted keys of a JWKS *document*, by ``kid``.

    An unusable entry is skipped, not fatal: one key the vault cannot use must
    not take down the keys it can. A ``kid`` published twice is dropped
    entirely, because a token naming it cannot say which of the two it means.

    Args:
        document: The decoded key-set document.

    Returns:
        ``{kid: key}``.

    Raises:
        JwksUnavailableError: When *document* is not a JWKS object at all.
    """
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise JwksUnavailableError(_KEY_SET_UNAVAILABLE)
    keys: dict[str, PyJWK] = {}
    ambiguous: set[str] = set()
    for raw in document["keys"]:
        entry = _usable_entry(raw)
        key = None if entry is None else _parsed_key(entry)
        if entry is None or key is None:
            continue
        kid = str(entry[_KID_HEADER])
        if kid in keys:
            ambiguous.add(kid)
        keys[kid] = key
    for kid in ambiguous:
        del keys[kid]
    return keys


class JwksCache:
    """The issuer's verification keys, fetched lazily and cached.

    It owns *when* the set is refetched and what happens on failure, and
    nothing else reads the keys, so the issuer rotating its signing key never
    needs a vault restart (#1965). The set is fetched on first use, again once
    it is :data:`JWKS_TTL_SECONDS` old, and early when a token names an
    unknown ``kid`` — never more often than every
    :data:`JWKS_MIN_REFRESH_SECONDS`. A key the issuer adds is therefore
    accepted within a minute of its first use; a key it removes is refused
    after the next *successful* fetch, because a fetch that fails keeps the
    last good set, so an issuer outage does not refuse the tokens it already
    signed. The outage is logged once, when it begins, and its end once, so a
    long one is two lines rather than one per attempt.
    """

    def __init__(
        self,
        fetch: JwksFetcher,
        *,
        ttl_seconds: float = JWKS_TTL_SECONDS,
        min_refresh_seconds: float = JWKS_MIN_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the key source and the refresh policy.

        Args:
            fetch: Where the key set comes from.
            ttl_seconds: How long a fetched set is used.
            min_refresh_seconds: The floor between two fetch attempts.
            clock: A monotonic clock, injectable for tests.
        """
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._failing = False
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> PyJWK | None:
        """Return the key published under *kid*, refreshing if the rules allow.

        Args:
            kid: The token header's key id.

        Returns:
            The key, or ``None`` when the issuer does not publish one.
        """
        if self._refreshing() or self._stale():
            await self._refresh()
        key = self._keys.get(kid)
        if key is None and (self._refreshing() or self._may_attempt()):
            await self._refresh()
            key = self._keys.get(kid)
        return key

    def _refreshing(self) -> bool:
        """Return whether a fetch is in flight right now.

        A lookup that arrives during one waits for it instead of reading the
        key set it is about to replace, so a burst of first requests is not
        refused for want of keys the issuer is already sending.

        Returns:
            ``True`` while another lookup holds the refresh lock.
        """
        return self._lock.locked()

    def _stale(self) -> bool:
        """Return whether the set is past its TTL and a fetch may be attempted.

        Returns:
            ``True`` when no set was ever fetched, or the last one has expired,
            and the attempt floor has passed.
        """
        expired = (
            self._fetched_at is None or self._clock() - self._fetched_at >= self._ttl
        )
        return expired and self._may_attempt()

    def _may_attempt(self) -> bool:
        """Return whether the attempt floor has passed since the last fetch.

        Returns:
            ``True`` when no fetch was attempted yet, or the last one is at
            least :attr:`_min_refresh` seconds old.
        """
        return (
            self._attempted_at is None
            or self._clock() - self._attempted_at >= self._min_refresh
        )

    async def _refresh(self) -> None:
        """Fetch the key set once, keeping the last good one on failure."""
        async with self._lock:
            if not self._may_attempt():
                return
            self._attempted_at = self._clock()
            try:
                keys = parse_jwks(await self._fetch())
            except OSError:
                self._note_failure()
                return
            if self._failing:
                _LOGGER.info("agent-token key set refresh recovered")
            self._failing = False
            self._keys = keys
            self._fetched_at = self._attempted_at

    def _note_failure(self) -> None:
        """Log the first failed fetch of an outage, and only that one.

        Every later attempt in the same outage is silent: an issuer that stays
        down for an hour would otherwise write one warning a minute, which is
        how an operator learns to stop reading them.
        """
        if self._failing:
            return
        self._failing = True
        _LOGGER.warning(
            "agent-token key set refresh failed; keeping %d previously "
            "fetched key(s) until a refresh succeeds",
            len(self._keys),
        )


def looks_like_jwt(token: str) -> bool:
    """Return whether *token* has the compact JWS shape at all.

    Args:
        token: A presented bearer.

    Returns:
        ``True`` for three base64url segments within :data:`MAX_TOKEN_LENGTH`.
        A ``secrets.token_urlsafe`` static token has no ``.`` and is never one.
    """
    return len(token) <= MAX_TOKEN_LENGTH and _JWT_SHAPE.fullmatch(token) is not None


def _refused(category: str) -> AccessToken | None:
    """Note a refusal by category only, and return the refusal itself.

    Typed as the verifier's own answer so every refusal site reads
    ``return _refused(...)``: the note and the answer cannot drift apart.

    Args:
        category: A fixed, content-free label.

    Returns:
        ``None``, always: the refusal.
    """
    _LOGGER.info("agent token refused: %s", category)
    return None


def _category(error: jwt.PyJWTError) -> str:
    """Return the fixed log category for a library refusal.

    Args:
        error: What PyJWT raised.

    Returns:
        A label from :data:`_REFUSAL_CATEGORIES`, or ``"invalid"``.
    """
    for kind, label in _REFUSAL_CATEGORIES:
        if isinstance(error, kind):
            return label
    return "invalid"


def _granted_scopes(claim: object) -> list[str] | None:
    """Return the scopes a token's ``scope`` claim confers here.

    Args:
        claim: The claim, or ``None`` when absent.

    Returns:
        :data:`~creek_mcp.remote_auth.REMOTE_SCOPE` plus each known grant once,
        in :class:`~creek_mcp.policy.Grant` declaration order; ``[]`` when no
        known grant is named; ``None`` when the claim is not a string.
    """
    if claim is None:
        return []
    if not isinstance(claim, str):
        return None
    named = set(claim.split())
    grants = [grant.value for grant in Grant if grant.value in named]
    return [REMOTE_SCOPE, *grants] if grants else []


def _accepted_until(exp: object) -> int | None:
    """Return the last second the SDK may keep accepting a token expiring at *exp*.

    The SDK refuses an access token once ``expires_at`` has passed, with no
    leeway of its own, so the verifier's :data:`CLOCK_SKEW_SECONDS` is carried
    into ``expires_at``: the token stops being accepted at the one instant both
    layers agree on, rather than 30 s earlier at the transport than at the
    verifier.

    Args:
        exp: The verified ``exp`` claim. PyJWT has already checked it is a
            number (an integer, a float, or a numeric string).

    Returns:
        ``exp`` plus the skew, in whole seconds, or ``None`` for a value that
        is not a number — a boolean included.
    """
    if isinstance(exp, bool) or not isinstance(exp, (int, float, str)):
        return None
    try:
        return int(float(exp)) + CLOCK_SKEW_SECONDS
    except ValueError:
        return None


def _agent_client_id(claim: object) -> str | None:
    """Return a claim the activity contract can carry verbatim as its consumer.

    Args:
        claim: The verified RFC 9068 ``client_id`` claim.

    Returns:
        The unchanged identity, or ``None`` for an invalid stamp. In particular,
        there is no truncation or subject fallback that could merge two agents.
    """
    if (
        not isinstance(claim, str)
        or not names_a_consumer(claim)
        or len(claim) > AGENT_ACTIVITY_MAX_CONSUMER_CHARS
        or re.fullmatch(AGENT_ACTIVITY_CONSUMER_PATTERN, claim) is None
    ):
        return None
    return claim


class JwtAgentTokenVerifier(TokenVerifier):
    """Accept exactly the agent tokens the pinned issuer signed for this vault."""

    def __init__(
        self,
        config: ResourceServerConfig,
        *,
        keys: JwksCache | None = None,
        expected_subject: str | None = None,
        revocation: RevocationCache | None = None,
    ) -> None:
        """Bind the resource, the issuer's keys and the optional subject pin.

        Args:
            config: This vault's resource-server configuration.
            keys: The key cache, or ``None`` to fetch from
                :attr:`~creek_mcp.oauth_resource.ResourceServerConfig.jwks_url`.
            expected_subject: The one ``sub`` this vault accepts, or ``None``
                to accept any subject the issuer signed for this audience.
            revocation: The per-call revocation check (#2005, R3-2), or
                ``None`` to verify offline only.
        """
        self._config = config
        self._keys = keys or JwksCache(HttpJwksFetcher(config.jwks_url))
        self._expected_subject = expected_subject
        self._revocation = revocation

    @property
    def config(self) -> ResourceServerConfig:
        """Return the resource this verifier accepts tokens for.

        Returns:
            The configuration, which the metadata and the challenge are built
            from, so the three can never describe different resources.
        """
        return self._config

    @property
    def expected_subject(self) -> str | None:
        """Return the subject pin, if any.

        Returns:
            The pinned ``sub``, or ``None``.
        """
        return self._expected_subject

    @property
    def revocation_enforced(self) -> bool:
        """Return whether every acceptance asks whether the grant was revoked.

        Returns:
            ``True`` when a revocation check is configured.
        """
        return self._revocation is not None

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the caller's access token for a valid agent JWT, else ``None``.

        Args:
            token: The presented bearer.

        Returns:
            The access token, or ``None`` for every refusal — the same answer
            an unknown static token gets, so a caller cannot tell which rule
            it tripped.
        """
        if not looks_like_jwt(token):
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return _refused("malformed")
        alg = header.get(_ALG_HEADER)
        kid = header.get(_KID_HEADER)
        if (
            alg not in ACCEPTED_ALGORITHMS
            or not isinstance(kid, str)
            or not kid
            or _CRIT_HEADER in header
        ):
            return _refused("header")
        key = await self._keys.key_for(kid)
        if key is None or key.algorithm_name != alg:
            return _refused("unknown key")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[key.algorithm_name],
                audience=self._config.resource_url,
                issuer=self._config.authorization_server,
                leeway=CLOCK_SKEW_SECONDS,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    "strict_aud": True,
                    "enforce_minimum_key_length": True,
                },
            )
        except jwt.PyJWTError as error:
            return _refused(_category(error))
        access = self._access_token(token, claims)
        if access is None or self._revocation is None:
            return access
        return await self._unrevoked(access, claims, self._revocation)

    @staticmethod
    async def _unrevoked(
        access: AccessToken,
        claims: Mapping[str, object],
        revocation: RevocationCache,
    ) -> AccessToken | None:
        """Return *access* only while its agent grant still stands (#2005).

        Runs after every offline rule has passed, so only a token this vault
        would otherwise accept is ever asked about, and before any caller sees
        an access token — so nothing is listed, dispatched or replayed for a
        revoked agent. Fail closed: no answer is a refusal.

        Args:
            access: The offline-verified access token.
            claims: Its verified claims.
            revocation: The revocation check.

        Returns:
            *access*, or ``None`` when the grant was revoked, the issuer gave
            no answer, or the claims cannot be put to it.
        """
        query = TokenStatusQuery.from_claims(
            access.subject, access.client_id, claims.get(_ISSUED_AT_CLAIM)
        )
        if query is None:
            return _refused("issued at")
        status = await revocation.status(query, expires_at=access.expires_at)
        if status is not Revocation.ACTIVE:
            return _refused(status.value)
        return access

    def _access_token(
        self, token: str, claims: Mapping[str, object]
    ) -> AccessToken | None:
        """Map verified *claims* to the SDK's access token.

        Args:
            token: The verified bearer.
            claims: Its verified claims.

        Returns:
            The access token, or ``None`` when the subject names nobody or is
            not the pinned one, the client id cannot be shown as an activity
            consumer, the scope claim is not a string, or the expiry is not a number.
        """
        subject = claims.get(_SUBJECT_CLAIM)
        if not isinstance(subject, str) or not names_a_consumer(subject):
            return _refused("subject")
        if self._expected_subject is not None and subject != self._expected_subject:
            return _refused("subject")
        client_id = _agent_client_id(claims.get(_CLIENT_ID_CLAIM))
        if client_id is None:
            return _refused("client id")
        scopes = _granted_scopes(claims.get(_SCOPE_CLAIM))
        if scopes is None:
            return _refused("scope")
        expires_at = _accepted_until(claims.get(_EXPIRY_CLAIM))
        if expires_at is None:
            return _refused("expiry")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self._config.resource_url,
            subject=subject,
            claims={_ISSUER_CLAIM: self._config.authorization_server},
        )


class ComposedTokenVerifier(TokenVerifier):
    """Agent JWTs and static consumer tokens on one endpoint."""

    def __init__(
        self, static: ConsumerTokenVerifier, agent: JwtAgentTokenVerifier
    ) -> None:
        """Compose the two verifiers.

        Args:
            static: The configured static registry — the same instance the
                ``/v1`` bearer gate holds.
            agent: The JWT verifier.
        """
        self._static = static
        self._agent = agent

    @property
    def consumers(self) -> tuple[str, ...]:
        """Return the static registry's consumer names.

        Returns:
            Only the configured names: a JWT caller is a person, not a
            configured consumer, and is never given a ``/v1`` bucket.
        """
        return self._static.consumers

    async def verify_token(self, token: str) -> AccessToken | None:
        """Try a JWT-shaped bearer as a JWT, then the static registry.

        Args:
            token: The presented bearer.

        Returns:
            The first verifier's access token, or ``None``.
        """
        if looks_like_jwt(token):
            access = await self._agent.verify_token(token)
            if access is not None:
                return access
        return await self._static.verify_token(token)


def mcp_token_verifier(
    static: ConsumerTokenVerifier, agent: JwtAgentTokenVerifier | None
) -> TokenVerifier:
    """Return the verifier the MCP surface is served behind.

    Args:
        static: The static registry.
        agent: The JWT verifier, or ``None`` on a bearer-only vault.

    Returns:
        *static* itself when no resource server is configured — exactly the
        pre-#1963 surface — else the composition.
    """
    return static if agent is None else ComposedTokenVerifier(static, agent)


def build_agent_verifier(
    environ: Mapping[str, str] | None = None,
    *,
    expected_subject: str | None = None,
) -> JwtAgentTokenVerifier | None:
    """Return the JWT verifier the environment configures, or ``None``.

    Args:
        environ: The environment, or ``None`` for :data:`os.environ`.
        expected_subject: The subject pin, for a managed vault.

    Returns:
        The verifier, or ``None`` when no resource server is configured.

    Raises:
        ValueError: From
            :func:`~creek_mcp.oauth_resource.load_resource_server_config` or
            :func:`~creek_mcp.agent_revocation.load_revocation_cache`, or when
            a revocation check is configured without a resource server.
    """
    config = load_resource_server_config(environ)
    revocation = load_revocation_cache(environ)
    if config is None:
        if revocation is not None:
            msg = (
                f"{STATUS_URL_ENV} needs a resource server: there are no agent "
                "tokens to check without one"
            )
            raise ValueError(msg)
        return None
    if revocation is None:
        _LOGGER.warning(REVOCATION_NOT_ENFORCED)
    return JwtAgentTokenVerifier(
        config, expected_subject=expected_subject, revocation=revocation
    )
