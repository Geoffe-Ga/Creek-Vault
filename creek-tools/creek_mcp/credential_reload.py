"""A static consumer-token set that follows its mounted secret file (#1965).

Adding, rotating or revoking a static consumer token used to need a restart:
the container entry points read the secret file once and served that set for
the life of the process. :class:`ReloadingConsumerTokenVerifier` is the
:class:`~creek_mcp.remote_auth.ConsumerTokenVerifier` they serve instead. It
re-reads its file when the file changes and swaps the parsed set in whole, so
the ``/v1`` bearer gate and the ``/mcp`` mount — which hold this one instance
— both answer from the new set on their next call.

**Polling, not inotify.** The file lives on a Fly secret mount (or a Docker /
Kubernetes one, which swaps a symlink rather than writing in place), and the
simplest primitive that works on all of them is to look again. A request that
arrives :data:`RELOAD_POLL_SECONDS` or more after the last look re-reads the
file — never more often — and compares the SHA-256 of its bytes with the bytes
the set in force came from. Comparing content rather than only the mtime is
deliberate: a rotation swaps one 43-character token for another, so a rewrite
inside the filesystem's timestamp granularity keeps both the size and the
mtime, and the file is a few hundred bytes. A touch that changes nothing is
not a reload.

**Fail closed, keep serving.** The replacement is parsed by the same parser
the boot path uses, so every load-time refusal — a weak token, a consumer
named twice, a shared value, an unreadable grant suffix, and whatever the
caller's parser adds — is a reload refusal. A refused file is named on stderr
once, with the parser's reason, and the previous set stays in force: the vault
is never served half-parsed, and an operator's typo never locks every consumer
out. An unreadable file (a mount mid-swap) is the same: noted once, previous
set kept. No message carries a token value, the file's path, or a byte of its
content; the parser's reasons are the load-time ones, which name consumers and
lengths only.

**Announced as at boot.** A successful reload prints the same rotation-window
and ungated-consumer notices startup prints
(:func:`~creek_mcp.remote_auth.announce_rotation_window`,
:func:`~creek_mcp.remote_auth.announce_ungated_consumers`), on stderr for the
same reason.

**The consumer set is the parser's business.** Some state is built once from
the consumer names at app construction — the per-consumer concurrency buckets,
the journal's single-owner rule, a managed vault's JWT subject pin. An adapter
with such state pins the set in its parser, as the container does
(:meth:`creek_mcp.container_runtime.SingleConsumerSecret.reloading_verifier`
refuses a file naming a different identity); this class only swaps what the
parser accepted.
"""

from __future__ import annotations

import hashlib
import sys
import time
from typing import TYPE_CHECKING, Final

from creek_mcp.remote_auth import (
    ConsumerTokenVerifier,
    announce_rotation_window,
    announce_ungated_consumers,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mcp.server.auth.provider import AccessToken

RELOAD_POLL_SECONDS: Final[float] = 5.0
"""The floor between two reads of the secret file."""

_NOTICE_PREFIX: Final[str] = "creek: consumer credentials"
"""How every reload line on stderr begins, so an operator can grep for it."""

_KEPT: Final[str] = "the previous credential set stays in force"

_NOT_TEXT: Final[str] = "the file is not UTF-8 text"
"""The fixed reason for a decode failure.

Never the codec's own message, which quotes the offending byte — a byte of a
secret.
"""

# Monkeypatchable clock alias, as ``remote_auth._now`` is: a test that drives
# a real entry point moves the poll clock by replacing this.
_monotonic = time.monotonic


def content_digest(content: bytes) -> str:
    """Return the SHA-256 hex digest the reload compares file contents by.

    Args:
        content: The secret file's bytes.

    Returns:
        The digest. It identifies a version of the file; it is never logged.
    """
    return hashlib.sha256(content).hexdigest()


def _say(message: str) -> None:
    """Write one reload line to stderr.

    Args:
        message: A content-free notice.
    """
    print(f"{_NOTICE_PREFIX}: {message}", file=sys.stderr)


class _PollState:
    """When the file was last looked at, and what was seen there.

    Attributes:
        checked_at: The monotonic instant of the last look.
        seen: The digest of the last content read, accepted or refused, or
            ``None`` before the first read when the boot content is unknown.
        unreadable: Whether the last look failed to read the file at all.
    """

    def __init__(self, checked_at: float, seen: str | None) -> None:
        """Record the state as of boot.

        Args:
            checked_at: The boot instant.
            seen: The digest of the boot content, if known.
        """
        self.checked_at = checked_at
        self.seen = seen
        self.unreadable = False


class ReloadingConsumerTokenVerifier(ConsumerTokenVerifier):
    """A :class:`~creek_mcp.remote_auth.ConsumerTokenVerifier` that follows a file."""

    def __init__(
        self,
        source: Path,
        parse: Callable[[str], ConsumerTokenVerifier],
        *,
        initial: ConsumerTokenVerifier,
        baseline: str | None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Serve *initial* and follow *source* from now on.

        Args:
            source: The mounted secret file.
            parse: Turns the file's text into a verifier, raising
                :class:`ValueError` — with a message that names no token — for
                anything the boot path would refuse.
            initial: The verifier the boot path built from *source*.
            baseline: :func:`content_digest` of the exact bytes *initial* was
                parsed from, so an edit landing between the boot read and this
                call is still picked up; ``None`` when unknown, which makes the
                first poll adopt whatever the file then holds.
            clock: A monotonic clock, injectable for tests; defaults to
                :func:`time.monotonic`.
        """
        super().__init__({})
        self._adopt(initial)
        self._source = source
        self._parse = parse
        self._clock = clock if clock is not None else _monotonic
        self._poll = _PollState(self._clock(), baseline)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Pick up a changed file if one is due, then verify against the set in force.

        Args:
            token: The bearer token presented on the request.

        Returns:
            The matching consumer's access token, or ``None``.
        """
        self._follow()
        return await super().verify_token(token)

    def _follow(self) -> None:
        """Re-read the file once the poll interval has passed, and react."""
        now = self._clock()
        if now - self._poll.checked_at < RELOAD_POLL_SECONDS:
            return
        self._poll.checked_at = now
        content = self._read()
        if content is None:
            return
        digest = content_digest(content)
        if digest == self._poll.seen:
            return
        self._poll.seen = digest
        self._reload(content)

    def _read(self) -> bytes | None:
        """Return the file's bytes, or ``None`` after noting it unreadable once.

        Returns:
            The content, or ``None`` when the file cannot be read now.
        """
        try:
            content = self._source.read_bytes()
        except OSError:
            if not self._poll.unreadable:
                self._poll.unreadable = True
                _say(f"the secret file is unreadable; {_KEPT}")
            return None
        self._poll.unreadable = False
        return content

    def _reload(self, content: bytes) -> None:
        """Parse *content* and adopt it, or refuse it and keep the set in force.

        Args:
            content: The file's new bytes.
        """
        try:
            replacement = self._parse(content.decode("utf-8"))
        except UnicodeDecodeError:
            _say(f"reload refused: {_NOT_TEXT}; {_KEPT}")
            return
        except ValueError as refusal:
            _say(f"reload refused: {refusal}; {_KEPT}")
            return
        self._adopt(replacement)
        _say(f"reloaded from the secret file ({len(self.consumers)} consumer(s))")
        announce_rotation_window(self)
        announce_ungated_consumers(self)
