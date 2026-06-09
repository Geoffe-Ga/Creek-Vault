"""The provider-neutral contract every synchronous LLM backend satisfies.

Defines :class:`LLMProvider`, the ``Protocol`` (#604) that the existing
Anthropic path — and the OpenAI / Gemini paths to come — implement so the
orchestrator and author desk can route through one structural type. The
protocol is ``runtime_checkable`` so conformance can be asserted in tests;
mypy enforces it statically wherever a provider is bound to this type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from creek.classify.llm.completion import Completion


@runtime_checkable
class LLMProvider(Protocol):
    """A synchronous, provider-neutral text-completion backend.

    Implementations read their API key from the environment (never config),
    expose the resolved model identifier, report whether their prerequisites
    are met, and turn a prompt into a normalized :class:`Completion`.
    """

    is_cloud: bool
    """Whether the backend sends content off-device.

    Drives the cloud-egress warning and consent gate. Declared as a plain
    class attribute on each implementation so the factory can read it without
    instantiating (which, for cloud backends, would require credentials).
    """

    @property
    def model(self) -> str:
        """The resolved model identifier used for completion calls."""

    @property
    def available(self) -> bool:
        """Whether the backend's prerequisites (keys, consent, host) are met."""

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Send *prompt* and return its normalized :class:`Completion`.

        Args:
            prompt: The dynamic, fully-formatted user prompt.
            max_tokens: Maximum tokens to request, or ``None`` for the
                backend's default ceiling.
            system: Optional static system prefix, or ``None``.

        Returns:
            The backend's :class:`Completion`.
        """
