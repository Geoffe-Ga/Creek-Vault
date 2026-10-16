"""A writer's own provider key, held for one request and never longer (#1873).

ADR-0015 lets a managed vault answer OPEN and PERSONAL generation and
classification with the **writer's** Anthropic or OpenAI key, supplied per
request by the consumer that relays it. This module owns the one in-memory
type that key travels in, and the parse that admits it.

**Why a type and not a ``str``.** A bare string is one f-string, one
``logger.debug("%r", locals())`` or one ``model_dump`` away from a log line, an
audit record or a file. :class:`RequestKey` keeps the value out of every
rendering a Python object has: its ``repr`` and ``str`` name the provider and
say the key is redacted, it refuses to pickle (so no job record, cache or
``copy`` can carry it to disk), and the only way to read the value is the
explicit :meth:`RequestKey.reveal`, which only the provider SDK client
constructor calls.

**Why not :class:`~creek.config.LLMConfig`.** That model is the vault's
writable, YAML-backed configuration and is dumped to disk by design. #761's
``api_key_env`` deliberately holds a variable *name* for the same reason. A
request key rides beside the config, never inside it.

**What it does not decide.** Whether a key may be used at all is
:func:`creek_mcp.model_pin.request_key_admitted`'s question (a managed vault
only), and whether a tier may leave the vault is the
:class:`~creek.classify.llm.router.ModelRouter`'s (INTIMATE never). This module
only parses and carries.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Final, NoReturn

from creek.config import LLMConfig

REQUEST_KEY_PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")
"""The providers a writer may supply a request-scoped key for.

Exactly the two vendors the owner decision (2026-10-16) names. Gemini and the
enclave are registered providers but are not a writer's BYOK choice in
Adepthood, so a header naming them is refused rather than built.
"""

MAX_REQUEST_KEY_LENGTH: Final[int] = 512
"""Longest key accepted, in characters.

Current Anthropic and OpenAI keys are well under 256 characters. The bound
exists so a header cannot carry an arbitrarily large value into memory that is
then handed to an SDK, and it is generous so a longer future key format is not
refused for reasons the writer cannot see.
"""

_KEY_ALPHABET: Final[frozenset[str]] = frozenset(
    string.ascii_letters + string.digits + "-_."
)
"""Characters a provider key may contain.

Both vendors issue keys from this alphabet. Refusing anything else — whitespace,
control characters, quotes, ``:`` — means a value that is plainly not a key
(a pasted sentence, a header-injection attempt) never reaches an SDK.
"""

REDACTED: Final[str] = "<redacted>"
"""What every rendering of a :class:`RequestKey` shows in place of the value."""


class InvalidRequestKeyError(ValueError):
    """The request-key headers were present but did not describe a usable key.

    The message is fixed and names no header value, so it is safe to log and
    safe to turn into a refusal. The offending value is never attached.
    """

    def __init__(self) -> None:
        """Build the error with its one fixed message."""
        super().__init__(
            "request-scoped provider key is malformed: both headers are "
            f"required, the provider must be one of {list(REQUEST_KEY_PROVIDERS)}, "
            "and the key must be a non-empty provider key"
        )


@dataclass(frozen=True, slots=True, eq=False)
class RequestKey:
    """One writer's provider key, for one request.

    Attributes:
        provider: The ``LLMConfig.provider`` the key belongs to, one of
            :data:`REQUEST_KEY_PROVIDERS`.
    """

    provider: str
    _secret: str = field(repr=False)

    def reveal(self) -> str:
        """Return the key value, for the provider SDK client and nothing else.

        Returns:
            The key exactly as the writer supplied it.
        """
        return self._secret

    def stage_config(self) -> LLMConfig:
        """Return the stage configuration this key's provider is built from.

        The provider's own default model, and nothing else: no ``api_base``
        (a vault config cannot redirect a writer's key to a gateway it names),
        and no ``api_key_env`` (the key never comes from the environment).

        Returns:
            A fresh :class:`~creek.config.LLMConfig` naming :attr:`provider`.
        """
        return LLMConfig(provider=self.provider)

    def __repr__(self) -> str:
        """Name the provider and redact the key.

        Returns:
            ``RequestKey(provider='anthropic', key=<redacted>)``.
        """
        return f"RequestKey(provider={self.provider!r}, key={REDACTED})"

    def __str__(self) -> str:
        """Render exactly as :meth:`__repr__` does.

        Returns:
            The redacted rendering.
        """
        return self.__repr__()

    def __reduce__(self) -> NoReturn:
        """Refuse to pickle, so no job record, cache or copy can persist the key.

        Raises:
            TypeError: Always.
        """
        msg = "a request-scoped provider key is never serialised"
        raise TypeError(msg)


def parse_request_key(provider: str | None, key: str | None) -> RequestKey | None:
    """Return the request key two header values describe, or ``None``.

    Args:
        provider: The provider header's value, or ``None`` when absent.
        key: The key header's value, or ``None`` when absent.

    Returns:
        ``None`` when neither header was sent: the request carries no key and
        nothing about it changes.

    Raises:
        InvalidRequestKeyError: When only one header was sent, the provider is
            not one of :data:`REQUEST_KEY_PROVIDERS`, or the key is empty, too
            long, or contains a character no provider key does. The message is
            fixed and carries neither value.
    """
    if provider is key is None:
        return None
    if provider is None or key is None:
        raise InvalidRequestKeyError
    name = provider.strip().lower()
    value = key.strip()
    if name not in REQUEST_KEY_PROVIDERS or not _is_key_shaped(value):
        raise InvalidRequestKeyError
    return RequestKey(provider=name, _secret=value)


def _is_key_shaped(value: str) -> bool:
    """Return whether *value* could be a provider key.

    Args:
        value: The stripped key header value.

    Returns:
        ``True`` for a non-empty value within :data:`MAX_REQUEST_KEY_LENGTH`
        drawn only from :data:`_KEY_ALPHABET`.
    """
    return 0 < len(value) <= MAX_REQUEST_KEY_LENGTH and set(value) <= _KEY_ALPHABET
