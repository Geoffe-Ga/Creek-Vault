"""JWKS-verified, audience-bound agent access tokens (#1963).

:class:`~creek_mcp.agent_tokens.JwtAgentTokenVerifier` accepts exactly the
tokens Adepthood issues for **this** vault and nothing else. Every refusal the
issue names has its own test: another vault's audience (RFC 8707), another
issuer, an algorithm outside ``RS256``/``ES256`` (``none`` and ``HS256``
included), an unknown or missing ``kid``, a bad signature, an expired token
and a not-yet-valid one beyond the 30 s skew, a missing required claim. Every
key is generated in-process and served from a local JWKS fixture
(:mod:`tests.agent_token_support`); nothing dials an issuer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Final

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from creek_mcp.agent_tokens import (
    ACCEPTED_ALGORITHMS,
    AGENT_TOKEN_LOGGER_NAME,
    CLOCK_SKEW_SECONDS,
    JWKS_MIN_REFRESH_SECONDS,
    JWKS_TTL_SECONDS,
    ComposedTokenVerifier,
    HttpJwksFetcher,
    JwksCache,
    _accepted_until,
    build_agent_verifier,
    looks_like_jwt,
    mcp_token_verifier,
)
from creek_mcp.oauth_resource import AUTHORIZATION_SERVER_ENV, RESOURCE_URL_ENV
from creek_mcp.policy import UNGATED, UNGATED_SCOPE, Grant, granted_by_scopes
from creek_mcp.remote_auth import REMOTE_SCOPE, ConsumerTokenVerifier
from tests.agent_token_support import (
    AGENT_SUBJECT,
    EC_KID,
    ISSUER,
    OTHER_VAULT_URL,
    RESOURCE_URL,
    ROGUE_KID,
    FakeClock,
    StaticJwks,
    agent_verifier,
    ec_key,
    jwks,
    mint,
    resource_config,
    rsa_key,
)

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken

_STATIC_TOKEN: Final[str] = "unit-test-static-token-" + "s" * 20
"""A static consumer token. Test literal, not a real credential."""


def _verify(token: str, **kwargs: Any) -> AccessToken | None:
    """Verify *token* against a fresh verifier over the local JWKS.

    Args:
        token: The bearer.
        kwargs: Passed to :func:`tests.agent_token_support.agent_verifier`.

    Returns:
        The verifier's answer.
    """
    return asyncio.run(agent_verifier(**kwargs).verify_token(token))


def _b64(segment: dict[str, Any] | bytes) -> str:
    """Return one base64url JWT segment, unpadded.

    Args:
        segment: A JSON object, or raw bytes.

    Returns:
        The encoded segment.
    """
    raw = segment if isinstance(segment, bytes) else json.dumps(segment).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# --------------------------------------------------------------------------- #
# Accepted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", [rsa_key, ec_key], ids=["RS256", "ES256"])
def test_a_token_for_this_vault_is_accepted(key: Any) -> None:
    """Both accepted algorithms verify, and the token names its consumer.

    Args:
        key: The signing-key factory.
    """
    access = _verify(mint(key()))

    assert access is not None
    assert access.client_id == AGENT_SUBJECT
    assert access.subject == AGENT_SUBJECT
    assert access.resource == RESOURCE_URL
    assert access.scopes == [REMOTE_SCOPE, "vault:read", "vault:desk"]


def test_the_access_token_expires_when_the_jwt_does() -> None:
    """``expires_at`` is the token's own ``exp`` plus the skew, never a fresh TTL.

    The SDK refuses a token past ``expires_at`` with no leeway of its own, so
    the skew rides in it: the transport and the verifier stop accepting the
    token at the same instant.
    """
    exp = int(time.time()) + 123

    access = _verify(mint(exp=exp))

    assert access is not None
    assert access.expires_at == exp + CLOCK_SKEW_SECONDS


def test_a_fractional_expiry_is_accepted_as_whole_seconds() -> None:
    """A float ``exp`` is valid JWT; it must not crash the mapping."""
    exp = time.time() + 123.75

    access = _verify(mint(exp=exp))

    assert access is not None
    assert access.expires_at == int(exp) + CLOCK_SKEW_SECONDS


@pytest.mark.parametrize("exp", [True, "soon", None, [1]])
def test_a_non_numeric_expiry_never_becomes_an_access_token(exp: object) -> None:
    """The mapping refuses what it cannot read, whatever reached it.

    Args:
        exp: A value that is not a number.
    """
    assert _accepted_until(exp) is None


def test_the_scope_claim_becomes_the_grant_set_c3_reads() -> None:
    """``granted_by_scopes`` reads exactly the grants the token carried."""
    access = _verify(mint(scope="vault:journal vault:read"))

    assert access is not None
    assert granted_by_scopes(access.scopes) == {Grant.READ, Grant.JOURNAL}
    # Declaration order, so the scope list is stable however it was spelled.
    assert access.scopes == [REMOTE_SCOPE, "vault:read", "vault:journal"]


def test_a_jwt_can_never_carry_the_ungated_marker() -> None:
    """``creek:ungated`` in a token's scope is dropped, never honoured."""
    access = _verify(mint(scope=f"{UNGATED_SCOPE} {REMOTE_SCOPE} openid"))

    assert access is not None
    assert UNGATED_SCOPE not in access.scopes
    assert granted_by_scopes(access.scopes) != UNGATED
    assert granted_by_scopes(access.scopes) == frozenset()


def test_a_token_with_no_known_grant_lacks_the_remote_scope() -> None:
    """No grant means no ``creek:remote``, which the SDK answers ``403``."""
    for scope in (None, "", "openid profile", UNGATED_SCOPE):
        access = _verify(mint(scope=scope))

        assert access is not None
        assert access.scopes == []


def test_unknown_and_repeated_scopes_are_dropped() -> None:
    """Exact matching only, each grant once."""
    access = _verify(mint(scope="VAULT:READ vault:desk vault:desk vault:everything"))

    assert access is not None
    assert access.scopes == [REMOTE_SCOPE, "vault:desk"]


def test_an_audience_inside_the_skew_is_still_accepted() -> None:
    """A token that expired 10 s ago is inside the 30 s skew."""
    assert CLOCK_SKEW_SECONDS == 30
    expired_recently = int(time.time()) - 10

    assert _verify(mint(exp=expired_recently)) is not None


def test_a_token_not_yet_valid_within_the_skew_is_accepted() -> None:
    """``nbf`` 10 s in the future is inside the skew."""
    assert _verify(mint(nbf=int(time.time()) + 10)) is not None


def test_the_subject_pin_admits_its_own_subject() -> None:
    """A managed vault pinned to its consumer accepts that consumer's token."""
    assert _verify(mint(), expected_subject=AGENT_SUBJECT) is not None


# --------------------------------------------------------------------------- #
# Refused
# --------------------------------------------------------------------------- #


def test_a_token_for_another_vault_is_refused() -> None:
    """RFC 8707: an audience naming another vault is never accepted here."""
    assert _verify(mint(aud=OTHER_VAULT_URL)) is None


@pytest.mark.parametrize(
    "aud",
    [
        f"{RESOURCE_URL}/",
        RESOURCE_URL.upper(),
        RESOURCE_URL.replace("/mcp", "/mcp/x"),
        [RESOURCE_URL],
        [RESOURCE_URL, OTHER_VAULT_URL],
        "",
    ],
)
def test_the_audience_must_equal_the_resource_url_exactly(aud: object) -> None:
    """No normalisation and no list: one string, byte-identical.

    A list naming this vault beside another is a token two resource servers
    would both accept, which is how a token gets passed through.

    Args:
        aud: A near-miss audience.
    """
    assert _verify(mint(aud=aud)) is None


def test_a_token_from_another_issuer_is_refused() -> None:
    """The issuer is pinned verbatim."""
    assert _verify(mint(iss="https://evil.example")) is None
    assert _verify(mint(iss=f"{ISSUER}/")) is None


def test_an_expired_token_is_refused_beyond_the_skew() -> None:
    """Expired 40 s ago is outside the 30 s skew."""
    assert _verify(mint(exp=int(time.time()) - 40)) is None


def test_a_not_yet_valid_token_is_refused_beyond_the_skew() -> None:
    """``nbf`` 60 s ahead is outside the skew."""
    assert _verify(mint(nbf=int(time.time()) + 60)) is None


def test_a_token_issued_in_the_future_is_refused() -> None:
    """``iat`` past the skew is a clock attack or a broken issuer."""
    assert _verify(mint(iat=int(time.time()) + 600)) is None


@pytest.mark.parametrize("claim", ["iss", "sub", "aud", "exp", "iat"])
def test_a_token_missing_a_required_claim_is_refused(claim: str) -> None:
    """Every claim the verifier relies on is required.

    Args:
        claim: The claim dropped.
    """
    assert _verify(mint(**{claim: None})) is None


@pytest.mark.parametrize("sub", ["", "   ", 91])
def test_a_subject_that_names_nobody_is_refused(sub: object) -> None:
    """The consumer identity is stamped on every audit line, so it must exist.

    Args:
        sub: A blank or non-string subject.
    """
    assert _verify(mint(sub=sub)) is None


def test_a_non_string_scope_is_refused() -> None:
    """``scope`` is a space-separated string (RFC 9068), never a list."""
    assert _verify(mint(scope=["vault:read"])) is None


def test_the_subject_pin_refuses_another_subject() -> None:
    """Pinned to its own consumer, a vault refuses any other person's token."""
    assert _verify(mint(sub="acct_other"), expected_subject=AGENT_SUBJECT) is None


def test_an_alg_none_token_is_refused() -> None:
    """``alg: none`` is never accepted, signature or not."""
    header = {"alg": "none", "kid": rsa_key().kid, "typ": "JWT"}
    token = f"{_b64(header)}.{_b64({'iss': ISSUER})}."

    assert _verify(token) is None
    assert _verify(f"{token}c2ln") is None


def test_an_hs256_token_signed_with_the_public_key_is_refused() -> None:
    """The classic confusion: HMAC keyed with a published public key."""
    public_pem = (
        rsa_key()
        .private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    header = {"alg": "HS256", "kid": rsa_key().kid}
    signing_input = f"{_b64(header)}.{_b64({'iss': ISSUER})}".encode()
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    token = f"{signing_input.decode()}.{_b64(signature)}"

    assert _verify(token) is None


def test_a_header_alg_that_disagrees_with_the_key_is_refused() -> None:
    """An ES256 header naming the RSA key's ``kid`` is refused before decoding."""
    token = mint(ec_key(), headers={"kid": rsa_key().kid})

    assert _verify(token) is None


@pytest.mark.parametrize("alg", ["RS512", "ES384", "PS256"])
def test_an_algorithm_outside_the_allowlist_is_refused(alg: str) -> None:
    """Only RS256 and ES256.

    Args:
        alg: Another asymmetric algorithm.
    """
    assert set(ACCEPTED_ALGORITHMS) == {"RS256", "ES256"}
    header = {"alg": alg, "kid": rsa_key().kid}
    token = f"{_b64(header)}.{_b64({'iss': ISSUER})}.{_b64(b'sig')}"

    assert _verify(token) is None


def test_a_token_signed_by_an_unpublished_key_is_refused() -> None:
    """A well-formed token whose ``kid`` the issuer never published."""
    assert _verify(mint(rsa_key(ROGUE_KID))) is None


def test_a_token_whose_signature_does_not_verify_is_refused() -> None:
    """A rogue key reusing a published ``kid`` fails the signature check."""
    forged = mint(rsa_key(ROGUE_KID), headers={"kid": rsa_key().kid})

    assert _verify(forged) is None


def test_a_tampered_payload_is_refused() -> None:
    """Changing one claim after signing breaks the signature."""
    head, _, signature = mint().split(".")
    widened = _b64(
        {
            "iss": ISSUER,
            "sub": AGENT_SUBJECT,
            "aud": RESOURCE_URL,
            "scope": "vault:pipeline",
        }
    )

    assert _verify(f"{head}.{widened}.{signature}") is None


@pytest.mark.parametrize(
    "edit",
    [{"kid": None}, {"kid": ""}, {"kid": 7}, {"crit": ["exp"]}],
    ids=["no-kid", "blank-kid", "integer-kid", "crit"],
)
def test_a_header_without_a_usable_kid_or_with_crit_is_refused(
    edit: dict[str, object],
) -> None:
    """No ``kid`` to select a key by, or an extension this verifier cannot honour.

    Args:
        edit: Header members to replace (``None`` removes one).
    """
    fetcher = StaticJwks()

    assert _verify(_with_header(mint(), **edit), fetcher=fetcher) is None
    assert fetcher.calls == 0


def _with_header(token: str, **edit: object) -> str:
    """Return *token* with its JOSE header edited; the signature goes stale.

    Args:
        token: A minted token.
        edit: Members to set, or (with ``None``) remove.

    Returns:
        The edited token.
    """
    head, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(head + "=="))
    decoded.update(edit)
    kept = {name: value for name, value in decoded.items() if value is not None}
    return f"{_b64(kept)}.{payload}.{signature}"


@pytest.mark.parametrize(
    "token",
    ["", "a.b", "a.b.c.d", "not a jwt", "a..c", "a.b.c!", "x" * 10_000],
)
def test_a_value_that_is_not_jwt_shaped_is_refused(token: str) -> None:
    """Garbage never reaches the key set.

    Args:
        token: A non-JWT value.
    """
    fetcher = StaticJwks()

    assert _verify(token, fetcher=fetcher) is None
    assert fetcher.calls == 0


def test_the_jwt_shape_predicate() -> None:
    """Three non-empty base64url segments, and a static token is never one."""
    assert looks_like_jwt(mint())
    assert not looks_like_jwt(_STATIC_TOKEN)
    assert not looks_like_jwt("a.b")


def test_no_refusal_logs_the_token_or_its_kid(caplog: pytest.LogCaptureFixture) -> None:
    """A refusal is logged by category only: never the token, never a ``kid``."""
    rogue = mint(rsa_key(ROGUE_KID))
    wrong_aud = mint(aud=OTHER_VAULT_URL)

    with caplog.at_level(logging.DEBUG, logger=AGENT_TOKEN_LOGGER_NAME):
        assert _verify(rogue) is None
        assert _verify(wrong_aud) is None

    text = caplog.text
    assert caplog.records
    assert rogue not in text
    assert wrong_aud not in text
    assert ROGUE_KID not in text
    assert OTHER_VAULT_URL not in text
    for part in rogue.split("."):
        assert part not in text


# --------------------------------------------------------------------------- #
# The key set
# --------------------------------------------------------------------------- #


def _cache(fetcher: StaticJwks, clock: FakeClock) -> JwksCache:
    """Return a cache over *fetcher* on *clock*.

    Args:
        fetcher: The key source.
        clock: The monotonic clock.

    Returns:
        The cache.
    """
    return JwksCache(fetcher, clock=clock)


def test_the_cache_defaults_are_the_issue_values() -> None:
    """TTL 300 s; an unknown ``kid`` may refresh at most once a minute."""
    assert JWKS_TTL_SECONDS == 300
    assert JWKS_MIN_REFRESH_SECONDS == 60


def test_keys_are_fetched_once_and_reused_within_the_ttl() -> None:
    """Two verifications inside the TTL cost one fetch."""
    fetcher = StaticJwks()
    clock = FakeClock()
    cache = _cache(fetcher, clock)

    async def drive() -> None:
        assert await cache.key_for(rsa_key().kid) is not None
        clock.advance(JWKS_TTL_SECONDS - 1)
        assert await cache.key_for(EC_KID) is not None

    asyncio.run(drive())

    assert fetcher.calls == 1


def test_keys_are_refetched_after_the_ttl() -> None:
    """Past the TTL the next lookup refreshes."""
    fetcher = StaticJwks()
    clock = FakeClock()
    cache = _cache(fetcher, clock)

    async def drive() -> None:
        await cache.key_for(rsa_key().kid)
        clock.advance(JWKS_TTL_SECONDS)
        await cache.key_for(rsa_key().kid)

    asyncio.run(drive())

    assert fetcher.calls == 2


def test_an_unknown_kid_refreshes_once_then_is_rate_limited() -> None:
    """A new key is picked up early; a flood of random ``kid`` values is not."""
    fetcher = StaticJwks(jwks(rsa_key()))
    clock = FakeClock()
    cache = _cache(fetcher, clock)

    async def drive() -> list[object]:
        seen = [await cache.key_for(rsa_key().kid)]
        clock.advance(JWKS_MIN_REFRESH_SECONDS)
        fetcher.document = jwks(rsa_key(), ec_key())
        seen.append(await cache.key_for(EC_KID))
        for _ in range(5):
            seen.append(await cache.key_for("random-kid"))
        return seen

    seen = asyncio.run(drive())

    assert seen[0] is not None
    assert seen[1] is not None
    assert seen[2:] == [None] * 5
    assert fetcher.calls == 2


def test_concurrent_lookups_share_one_fetch() -> None:
    """A burst of first requests costs the issuer one fetch, not one each."""

    class SlowJwks(StaticJwks):
        async def __call__(self) -> object:
            await asyncio.sleep(0)
            return await super().__call__()

    fetcher = SlowJwks()
    cache = JwksCache(fetcher, clock=FakeClock())

    async def drive() -> list[object]:
        return list(
            await asyncio.gather(*(cache.key_for(rsa_key().kid) for _ in range(5)))
        )

    found = asyncio.run(drive())

    assert all(key is not None for key in found)
    assert fetcher.calls == 1


def test_a_failed_refresh_keeps_the_last_good_keys() -> None:
    """An unreachable issuer does not turn into a vault-wide refusal."""
    fetcher = StaticJwks()
    clock = FakeClock()
    cache = _cache(fetcher, clock)

    async def drive() -> object:
        await cache.key_for(rsa_key().kid)
        clock.advance(JWKS_TTL_SECONDS)
        fetcher.failing = True
        return await cache.key_for(rsa_key().kid)

    assert asyncio.run(drive()) is not None
    assert fetcher.calls == 2


def test_a_first_fetch_failure_refuses_every_token() -> None:
    """No keys yet and no issuer: fail closed."""
    fetcher = StaticJwks()
    fetcher.failing = True

    assert _verify(mint(), fetcher=fetcher) is None


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"keys": "nope"},
        {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": rsa_key().kid}]},
    ],
)
def test_a_malformed_or_symmetric_key_set_yields_no_key(document: object) -> None:
    """A symmetric key can never enter the set, so HS* is unverifiable by design.

    Args:
        document: A bad JWKS document.
    """
    assert _verify(mint(), fetcher=StaticJwks(document)) is None


def test_unusable_entries_are_skipped_and_usable_ones_kept() -> None:
    """One bad entry does not poison the rest of the set."""
    private_entry = {**ec_key().public_jwk, "kid": "has-private", "d": "AAAA"}
    document = {
        "keys": [
            "not-a-dict",
            {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
            {**rsa_key().public_jwk, "use": "enc", "kid": "for-encryption"},
            {**rsa_key().public_jwk, "kid": None},
            {**rsa_key().public_jwk, "alg": "RS512", "kid": "rs512"},
            private_entry,
            rsa_key().public_jwk,
        ]
    }
    cache = JwksCache(StaticJwks(document))

    async def drive() -> list[object]:
        return [
            await cache.key_for(kid)
            for kid in (
                "broken",
                "for-encryption",
                "rs512",
                "has-private",
                rsa_key().kid,
            )
        ]

    found = asyncio.run(drive())

    assert found[:4] == [None] * 4
    assert found[4] is not None


def test_a_kid_published_twice_is_ambiguous_and_refused() -> None:
    """Two keys under one ``kid`` cannot be told apart, so neither is used."""
    document = {
        "keys": [rsa_key().public_jwk, {**ec_key().public_jwk, "kid": rsa_key().kid}]
    }

    assert _verify(mint(), fetcher=StaticJwks(document)) is None


def test_the_http_fetcher_reads_the_key_set() -> None:
    """The production fetcher GETs the URL and decodes JSON."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=jwks())

    fetcher = HttpJwksFetcher(
        f"{ISSUER}/.well-known/jwks.json", transport=httpx.MockTransport(handler)
    )

    assert asyncio.run(fetcher()) == jwks()
    assert str(seen[0].url) == f"{ISSUER}/.well-known/jwks.json"
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        httpx.Response(302, headers={"location": "https://evil.example/jwks"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"{" + b" " * (70 * 1024) + b"}"),
    ],
)
def test_the_http_fetcher_refuses_a_bad_answer(response: httpx.Response) -> None:
    """A non-200, a redirect, non-JSON or an oversized body is a failed fetch.

    Args:
        response: The issuer's answer.
    """
    fetcher = HttpJwksFetcher(
        f"{ISSUER}/.well-known/jwks.json",
        transport=httpx.MockTransport(lambda _request: response),
    )

    with pytest.raises(OSError, match="key set"):
        asyncio.run(fetcher())


def test_the_http_fetcher_reports_a_transport_failure_as_unavailable() -> None:
    """A connection error is the same refusal, with no URL in it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    fetcher = HttpJwksFetcher(
        f"{ISSUER}/.well-known/jwks.json", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(OSError, match="key set") as failed:
        asyncio.run(fetcher())

    assert ISSUER not in str(failed.value)


# --------------------------------------------------------------------------- #
# Composition with the static registry
# --------------------------------------------------------------------------- #


def _composed() -> ComposedTokenVerifier:
    """Return a composed verifier over one static consumer and the JWT fixture.

    Returns:
        The verifier.
    """
    return ComposedTokenVerifier(
        ConsumerTokenVerifier({"adepthood": (_STATIC_TOKEN,)}), agent_verifier()
    )


def test_a_static_token_still_authenticates_through_the_composition() -> None:
    """Self-hosted static bearers keep working beside OAuth."""
    access = asyncio.run(_composed().verify_token(_STATIC_TOKEN))

    assert access is not None
    assert access.client_id == "adepthood"
    assert UNGATED_SCOPE in access.scopes


def test_a_jwt_authenticates_through_the_composition() -> None:
    """A JWT-shaped bearer is verified as a JWT."""
    access = asyncio.run(_composed().verify_token(mint()))

    assert access is not None
    assert access.client_id == AGENT_SUBJECT


def test_a_refused_jwt_is_not_rescued_by_the_static_registry() -> None:
    """A JWT for another vault stays refused."""
    assert asyncio.run(_composed().verify_token(mint(aud=OTHER_VAULT_URL))) is None


def test_the_composition_publishes_only_the_static_consumers() -> None:
    """The ``/v1`` limiter buckets by configured name; JWT callers are not on it."""
    assert _composed().consumers == ("adepthood",)


def test_mcp_token_verifier_is_the_static_one_without_oauth() -> None:
    """No resource server configured: the exact pre-#1963 verifier."""
    static = ConsumerTokenVerifier({"adepthood": (_STATIC_TOKEN,)})

    assert mcp_token_verifier(static, None) is static
    assert isinstance(
        mcp_token_verifier(static, agent_verifier()), ComposedTokenVerifier
    )


def test_build_agent_verifier_reads_the_environment() -> None:
    """The runtime builder reads the two settings, and nothing when unset."""
    assert build_agent_verifier({}) is None
    built = build_agent_verifier(
        {RESOURCE_URL_ENV: RESOURCE_URL, AUTHORIZATION_SERVER_ENV: ISSUER},
        expected_subject=AGENT_SUBJECT,
    )

    assert built is not None
    assert built.config == resource_config()
    assert built.expected_subject == AGENT_SUBJECT


def test_jwt_module_rejects_none_by_default() -> None:
    """Belt and braces: the library itself refuses ``none`` on this path."""
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(
            jwt.encode({"a": 1}, key=None, algorithm="none"),
            options={"verify_signature": True},
            algorithms=list(ACCEPTED_ALGORITHMS),
            key=rsa_key().private_key.public_key(),
        )
