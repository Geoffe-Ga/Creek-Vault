"""Creek vault subpackage — write and link ontological primitives."""

from creek.vault.linker import BatchLinkResult, VaultLinker
from creek.vault.writer import VaultWriter

__all__ = ["BatchLinkResult", "VaultLinker", "VaultWriter"]
