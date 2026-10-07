"""The guard that keeps every benchmark trial on a local model.

The harness exists to size a *local* model envelope, so a cloud provider
answering a trial would both corrupt the measurement and egress synthetic
prompts. :class:`LocalOnlyFactory` therefore refuses twice:

- at construction, a provider name registered as cloud
  (:func:`creek.classify.llm.providers.provider_is_cloud`);
- on every build, a callable that does not positively declare
  ``is_cloud = False``. A missing attribute counts as cloud — fail closed.

The runner accepts nothing but this type, and calls :meth:`preflight` before
it writes a corpus or sends a request, so a refused provider costs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from creek.classify.llm.providers import provider_is_cloud
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from creek_mcp.bench.protocols import LLMCallable, LLMFactory

_CLOUD_REFUSED: Final[str] = "the benchmark refuses a cloud provider"
_PREFLIGHT_MAX_TOKENS: Final[int] = 1
"""Output ceiling for the preflight build; nothing is generated with it."""


class CloudProviderRefusedError(ValueError):
    """A cloud (or undeclared) provider was offered to the harness."""


class LocalOnlyFactory:
    """Wraps a model factory and refuses anything not positively local."""

    def __init__(self, inner: LLMFactory, *, provider_name: str) -> None:
        """Refuse a cloud *provider_name* before anything is built.

        Args:
            inner: The factory producing completion callables.
            provider_name: The provider's registry name (``ollama``, ``fake``).

        Raises:
            CloudProviderRefusedError: When *provider_name* is a cloud provider.
        """
        if provider_is_cloud(provider_name):
            raise CloudProviderRefusedError(_CLOUD_REFUSED)
        self._inner = inner

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        """Build a callable and refuse it unless it declares itself local.

        Raises:
            CloudProviderRefusedError: When the callable is, or may be, cloud.
        """
        llm = self._inner(tier, max_tokens=max_tokens)
        if getattr(llm, "is_cloud", True):
            raise CloudProviderRefusedError(_CLOUD_REFUSED)
        return llm

    def preflight(self) -> None:
        """Build once without calling the model, so a refusal comes first.

        Raises:
            CloudProviderRefusedError: When the built callable is not local.
        """
        self(PrivacyTier.OPEN, max_tokens=_PREFLIGHT_MAX_TOKENS)
