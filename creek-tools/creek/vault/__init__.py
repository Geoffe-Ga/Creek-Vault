"""Creek vault subpackage — read/write ontological primitives in an Obsidian vault."""

from creek.vault.authors import load_author_manifest
from creek.vault.reader import iter_vault_fragments, try_load_fragment
from creek.vault.writer import VaultWriter

__all__ = [
    "VaultWriter",
    "iter_vault_fragments",
    "load_author_manifest",
    "try_load_fragment",
]
