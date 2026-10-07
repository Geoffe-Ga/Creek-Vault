"""A real loopback HTTP stand-in for an Ollama runtime (#1849).

The model-boundary tests need the *real* dial path — ``ollama_endpoint``, the
boundary's client and its transport selection — to run end to end, because a
stubbed ``call_ollama`` or a monkeypatched ``httpx.Client`` would hide exactly
what they check: which host a request actually reaches, and what gets logged
on the way. So this module serves ``/api/tags`` and ``/api/generate`` from a
stdlib server bound to ``127.0.0.1`` on an ephemeral port, and records every
request it receives.

It also hands out a *dead* proxy URL (a loopback port with no listener) for
proxy-bypass tests: a request routed to it fails fast with a connection error
instead of hanging on DNS or a real network.

This module is deliberately not named ``test_*``, so it is imported, never
collected.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import socket
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_LOOPBACK = "127.0.0.1"


@dataclass
class OllamaStub:
    """What the stub serves, and every request it has received."""

    tags: dict[str, Any]
    generation: str = "ready"
    requests: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    url: str = ""


class _Handler(BaseHTTPRequestHandler):
    """Answer the two Ollama endpoints from the bound :class:`OllamaStub`."""

    stub: ClassVar[OllamaStub]

    def _reply(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.stub.requests.append(("GET", self.path, None))
        self._reply(self.stub.tags)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.stub.requests.append(("POST", self.path, body))
        self._reply({"response": self.stub.generation, "done_reason": "stop"})

    def log_message(self, *_args: object) -> None:
        """Stay silent: the tests assert on what the code under test logs."""


def serve_ollama(
    stub: OllamaStub, *, tls: ssl.SSLContext | None = None
) -> Iterator[OllamaStub]:
    """Serve *stub* on loopback for the duration of a fixture.

    Args:
        stub: The inventory and generation text to serve.
        tls: A server-side TLS context; when given the stub speaks HTTPS.

    Yields:
        *stub*, with :attr:`OllamaStub.url` set to the server's base URL.
    """
    handler = type("_BoundHandler", (_Handler,), {"stub": stub})
    server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scheme = "http" if tls is None else "https"
    stub.url = f"{scheme}://{_LOOPBACK}:{server.server_address[1]}"
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def dead_proxy_url() -> str:
    """Return an ``http://`` URL on a loopback port that nothing listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK, 0))
        port = probe.getsockname()[1]
    return f"http://{_LOOPBACK}:{port}"


PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
"""Proxy variables httpx honours under ``trust_env=True``."""

NO_PROXY_ENV_NAMES = ("NO_PROXY", "no_proxy")
"""Variables that would exempt loopback from a proxy, cleared by the tests."""


_CERT_LIFETIME = datetime.timedelta(days=1)


def _certificate(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: tuple[str, ec.EllipticCurvePrivateKey] | None = None,
) -> x509.Certificate:
    """Return a short-lived certificate; self-signed CA when *issuer* is None."""
    now = datetime.datetime.now(datetime.UTC)
    issuer_name, signer = issuer if issuer is not None else (subject, key)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CERT_LIFETIME)
        .not_valid_after(now + _CERT_LIFETIME)
        .add_extension(
            x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True
        )
    )
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
    )
    if issuer is None:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
            critical=False,
        )
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(_LOOPBACK))]
            ),
            critical=False,
        )
    return builder.sign(signer, hashes.SHA256())


def private_ca_tls(directory: Path) -> tuple[Path, ssl.SSLContext]:
    """Mint a private CA and a loopback server certificate it signed.

    Args:
        directory: Where to write the PEM files.

    Returns:
        The CA bundle path (what an operator would put in ``SSL_CERT_FILE``)
        and a server-side TLS context presenting the signed certificate.
    """
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("creek-test-private-ca", ca_key)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _certificate(
        "creek-test-ollama", leaf_key, ("creek-test-private-ca", ca_key)
    )
    ca_path = directory / "private-ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain = directory / "ollama-chain.pem"
    chain.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(chain)
    return ca_path, server
