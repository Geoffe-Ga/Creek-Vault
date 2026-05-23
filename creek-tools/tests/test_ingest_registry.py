"""Pin tests for the ``INGESTOR_REGISTRY`` size and identity (INC-012).

The README claims a specific count of registered ingestors. This file
pins both the count and the names so that adding or removing an
ingestor lights up red until the README paragraph is updated alongside
the registry.
"""

from __future__ import annotations

from creek.ingest import INGESTOR_REGISTRY

_EXPECTED_INGESTOR_NAMES: frozenset[str] = frozenset(
    {
        "chatgpt",
        "claude",
        "code",
        "discord",
        "document",
        "generic",
        "image",
        "markdown",
        "presentation",
        "spreadsheet",
        "substack",
    },
)


def test_ingestor_registry_size_matches_readme() -> None:
    """Pin the registry size to the README's "11 source ingestors" claim."""
    # Update both this assertion and the README paragraph in
    # ``creek-tools/README.md`` ("Source platforms") together.
    assert len(INGESTOR_REGISTRY) == 11


def test_ingestor_registry_names_match_readme() -> None:
    """Pin the exact set of registered ingestor names.

    `gdrive` is intentionally absent (it is a downloader, not an
    ``Ingestor``) and `other` has no parser.
    """
    assert set(INGESTOR_REGISTRY.keys()) == _EXPECTED_INGESTOR_NAMES
