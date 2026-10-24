"""Mint Adepthood-shaped agent access tokens against a local JWKS (#1963).

Nothing here dials anywhere: every key pair is generated in-process, the JWKS
is a plain dict handed to the verifier through :class:`StaticJwks`, and every
token is signed locally. The claim set mirrors what adepthood#3533 (A2) issues:
``iss``, ``sub``, ``aud`` (one value), ``scope`` (space-separated grants),
``client_id``, ``iat``, ``exp`` and ``jti``.

This module is deliberately *not* named ``test_*``: pytest's ``python_files``
glob is ``test_*.py``, so it is imported, never collected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from creek_mcp.agent_tokens import JwksCache, JwtAgentTokenVerifier
from creek_mcp.oauth_resource import ResourceServerConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

RESOURCE_URL: Final[str] = "https://v-7q2k.creek.example/mcp"
"""This vault's MCP URL: the audience every accepted token must name."""

OTHER_VAULT_URL: Final[str] = "https://v-other.creek.example/mcp"
"""Another vault's MCP URL, which a token must never be accepted for here."""

ISSUER: Final[str] = "https://api.adepthood.example"
"""The pinned authorization server, spelled as its RFC 8414 metadata spells it."""

AGENT_SUBJECT: Final[str] = "acct_91"
"""The stable consumer identity A2 puts in ``sub``."""

AGENT_CLIENT_ID: Final[str] = "cl_8a"
"""The OAuth client the agent registered as. Not the consumer identity."""

DEFAULT_SCOPE: Final[str] = "vault:read vault:desk"
"""The default profile, as the token's space-separated ``scope`` claim."""

RSA_KID: Final[str] = "rsa-2026-10"
EC_KID: Final[str] = "ec-2026-10"
ROGUE_KID: Final[str] = "rogue-2026-10"

TOKEN_LIFETIME_SECONDS: Final[int] = 600
"""A2's ten-minute access-token lifetime."""

_RSA_KEY_BITS: Final[int] = 2048
_RSA_EXPONENT: Final[int] = 65537


@dataclass(frozen=True, slots=True)
class SigningKey:
    """One test signing key: its ``kid``, algorithm, private key and public JWK."""

    kid: str
    algorithm: str
    private_key: RSAPrivateKey | EllipticCurvePrivateKey
    public_jwk: dict[str, Any]


@cache
def rsa_key(kid: str = RSA_KID) -> SigningKey:
    """Return a cached RS256 signing key, generated once per *kid*.

    Args:
        kid: The key id to publish it under.

    Returns:
        The key, with its public half rendered as a JWK.
    """
    private = rsa.generate_private_key(
        public_exponent=_RSA_EXPONENT, key_size=_RSA_KEY_BITS
    )
    public = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return SigningKey(kid, "RS256", private, {**public, "kid": kid, "use": "sig"})


@cache
def ec_key(kid: str = EC_KID) -> SigningKey:
    """Return a cached ES256 (P-256) signing key, generated once per *kid*.

    Args:
        kid: The key id to publish it under.

    Returns:
        The key, with its public half rendered as a JWK.
    """
    private = ec.generate_private_key(ec.SECP256R1())
    public = ECAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return SigningKey(kid, "ES256", private, {**public, "kid": kid, "use": "sig"})


def jwks(*keys: SigningKey) -> dict[str, Any]:
    """Return a JWKS document publishing the public half of *keys*.

    Args:
        keys: The keys to publish; both test keys when none are named.

    Returns:
        ``{"keys": [...]}``.
    """
    published = keys or (rsa_key(), ec_key())
    return {"keys": [dict(key.public_jwk) for key in published]}


class StaticJwks:
    """A JWKS fetcher that answers from memory and counts how often it was asked."""

    def __init__(self, document: object | None = None) -> None:
        """Serve *document*, or both test keys when none is given.

        Args:
            document: The JWKS document to answer with.
        """
        self.document: object = jwks() if document is None else document
        self.calls = 0
        self.failing = False

    async def __call__(self) -> object:
        """Return the configured document, or fail as an unreachable issuer would.

        Returns:
            The document.

        Raises:
            OSError: While :attr:`failing` is set.
        """
        self.calls += 1
        if self.failing:
            raise OSError("issuer unreachable")
        return self.document


class FakeClock:
    """A monotonic clock the cache tests move by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        """Start at *start* seconds.

        Args:
            start: The initial reading.
        """
        self.now = start

    def __call__(self) -> float:
        """Return the current reading.

        Returns:
            Seconds.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self.now += seconds


def resource_config(resource_url: str = RESOURCE_URL) -> ResourceServerConfig:
    """Return this vault's resource-server configuration.

    Args:
        resource_url: The MCP URL this vault serves.

    Returns:
        The validated configuration, pinned to :data:`ISSUER`.
    """
    return ResourceServerConfig.build(
        resource_url=resource_url, authorization_server=ISSUER
    )


def agent_verifier(
    fetcher: StaticJwks | None = None,
    *,
    expected_subject: str | None = None,
    resource_url: str = RESOURCE_URL,
) -> JwtAgentTokenVerifier:
    """Return a JWT verifier over the local JWKS fixture.

    Args:
        fetcher: The JWKS source, or a fresh :class:`StaticJwks`.
        expected_subject: The ``sub`` this vault is pinned to, if any.
        resource_url: The audience the verifier requires.

    Returns:
        The verifier.
    """
    source = StaticJwks() if fetcher is None else fetcher
    return JwtAgentTokenVerifier(
        resource_config(resource_url),
        keys=JwksCache(source),
        expected_subject=expected_subject,
    )


def claims(**overrides: object) -> dict[str, object]:
    """Return A2's claim set for a valid token, with *overrides* applied.

    A key overridden to ``None`` is removed, so a test can drop a required
    claim by naming it.

    Args:
        overrides: Claims to replace, add or (with ``None``) remove.

    Returns:
        The claims.
    """
    now = int(time.time())
    built: dict[str, object] = {
        "iss": ISSUER,
        "sub": AGENT_SUBJECT,
        "aud": RESOURCE_URL,
        "scope": DEFAULT_SCOPE,
        "client_id": AGENT_CLIENT_ID,
        "iat": now,
        "exp": now + TOKEN_LIFETIME_SECONDS,
        "jti": "jti-test",
    }
    built.update(overrides)
    return {name: value for name, value in built.items() if value is not None}


def mint(
    key: SigningKey | None = None,
    *,
    headers: Mapping[str, object] | None = None,
    **overrides: object,
) -> str:
    """Return a signed access token.

    Args:
        key: The signing key; the RS256 test key by default.
        headers: Extra JOSE header members, laid over ``kid``.
        overrides: Claim overrides, as :func:`claims` takes them.

    Returns:
        The compact JWT.
    """
    signer = rsa_key() if key is None else key
    header = {"kid": signer.kid, **(headers or {})}
    return jwt.encode(
        claims(**overrides),
        signer.private_key,
        algorithm=signer.algorithm,
        headers=header,
    )
