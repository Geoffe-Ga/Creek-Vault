"""Fragmentation engine for splitting and grouping content fragments.

Sits between ingestion and classification in the Creek pipeline.
Splits long documents at heading or paragraph boundaries and groups
short related fragments by source proximity.
"""

import itertools
import re

from pydantic import BaseModel, Field

from creek.ingest.base import ParsedFragment, generate_fragment_id


class FragmentationConfig(BaseModel):
    """Configuration for fragmentation thresholds.

    Attributes:
        max_words: Split documents exceeding this word count.
        min_words: Group fragments below this word count.
        section_split_level: Heading level for section splitting (2 = ``##``).
        group_time_window_minutes: Maximum gap for grouping fragments.
    """

    max_words: int = Field(default=3000, ge=1)
    min_words: int = Field(default=50, ge=1)
    section_split_level: int = Field(default=2, ge=1, le=6)
    group_time_window_minutes: int = Field(default=5, ge=1)


def _word_count(text: str) -> int:
    """Count words in text.

    Args:
        text: Input string.

    Returns:
        Number of whitespace-delimited tokens.
    """
    return len(text.split())


def _heading_pattern(level: int) -> re.Pattern[str]:
    """Build a regex that matches a markdown heading at the given level.

    Args:
        level: Heading level (1-6).

    Returns:
        Compiled regex matching the heading marker at the start of a line.
    """
    return re.compile(rf"^{'#' * level}\s+", re.MULTILINE)


class FragmentationEngine:
    """Splits long documents and groups short fragments.

    Uses configurable thresholds for word-count-based splitting
    and source-proximity-based grouping.

    Attributes:
        config: The active fragmentation configuration.
    """

    def __init__(self, config: FragmentationConfig | None = None) -> None:
        """Initialise with optional configuration.

        Args:
            config: Thresholds for splitting and grouping.
                Defaults to ``FragmentationConfig()`` if not provided.
        """
        self.config = config or FragmentationConfig()

    def fragment(
        self,
        parsed: list[ParsedFragment],
    ) -> list[ParsedFragment]:
        """Apply fragmentation rules to a list of parsed fragments.

        Long fragments are split; consecutive short fragments from
        the same source are grouped.

        Args:
            parsed: Input fragments from ingestion.

        Returns:
            Processed list of fragments.
        """
        if not parsed:
            return []

        result: list[ParsedFragment] = []
        for frag in parsed:
            if self.should_split(frag):
                result.extend(self._split(frag))
            else:
                result.append(frag)

        return self._group_pass(result)

    def should_split(self, fragment: ParsedFragment) -> bool:
        """Check if a fragment exceeds the word count threshold.

        Args:
            fragment: Fragment to evaluate.

        Returns:
            True if the fragment should be split.
        """
        return _word_count(fragment.content) > self.config.max_words

    def split_by_sections(
        self,
        fragment: ParsedFragment,
    ) -> list[ParsedFragment]:
        """Split at heading boundaries.

        Splits content at markdown headings of the configured level
        (default ``##``).  If no headings are found the fragment is
        returned unchanged.

        Args:
            fragment: Fragment to split.

        Returns:
            List of child fragments (or single-element list if no split).
        """
        pattern = _heading_pattern(self.config.section_split_level)
        level = self.config.section_split_level
        prefix = "#" * level + " "
        parts = pattern.split(fragment.content)
        # re.split keeps text between matches; filter empties.
        # The first element is text before the first heading (no marker).
        # Subsequent elements had their heading marker consumed by split,
        # so we re-prepend it.
        sections: list[str] = []
        for idx, part in enumerate(parts):
            stripped = part.strip()
            if not stripped:
                continue
            if idx > 0:
                stripped = prefix + stripped
            sections.append(stripped)

        if len(sections) <= 1:
            return [fragment]

        return self._make_children(fragment, sections)

    def split_by_length(
        self,
        fragment: ParsedFragment,
        max_words: int | None = None,
    ) -> list[ParsedFragment]:
        """Split at paragraph boundaries when exceeding max length.

        Paragraphs are delimited by double newlines.  Adjacent
        paragraphs are accumulated until the word budget is reached,
        then a new chunk is started.

        Args:
            fragment: Fragment to split.
            max_words: Override for ``config.max_words``.

        Returns:
            List of child fragments.
        """
        limit = max_words if max_words is not None else self.config.max_words
        paragraphs = fragment.content.split("\n\n")

        chunks: list[str] = []
        current: list[str] = []
        current_words = 0

        for para in paragraphs:
            pw = _word_count(para)
            if current and current_words + pw > limit:
                chunks.append("\n\n".join(current))
                current = [para]
                current_words = pw
            else:
                current.append(para)
                current_words += pw

        if current:
            chunks.append("\n\n".join(current))

        if len(chunks) <= 1:
            return [fragment]

        return self._make_children(fragment, chunks)

    def should_group(
        self,
        fragments: list[ParsedFragment],
    ) -> bool:
        """Check if consecutive short fragments should be merged.

        Fragments are eligible for grouping when they all come from
        the same source, every fragment is below ``min_words``, and
        every consecutive pair falls within ``group_time_window_minutes``.

        Args:
            fragments: Candidate fragments to evaluate.

        Returns:
            True if the fragments should be grouped.
        """
        if len(fragments) < 2:
            return False

        sources = {f.source_path for f in fragments}
        if len(sources) > 1:
            return False

        if not all(_word_count(f.content) < self.config.min_words for f in fragments):
            return False

        max_gap = self.config.group_time_window_minutes * 60
        return all(
            abs((b.timestamp - a.timestamp).total_seconds()) <= max_gap
            for a, b in itertools.pairwise(fragments)
        )

    def group_fragments(
        self,
        fragments: list[ParsedFragment],
    ) -> ParsedFragment:
        """Merge related short fragments into one.

        Content is joined with double newlines.  The earliest
        timestamp is preserved and metadata dicts are merged in
        order with ``dict.update()``, so later fragments win on
        key collisions (last-wins semantics).

        Args:
            fragments: Fragments to merge (must be non-empty).

        Returns:
            A single merged fragment.
        """
        combined_content = "\n\n".join(f.content for f in fragments)
        earliest = min(f.timestamp for f in fragments)
        merged_meta: dict[str, object] = {}
        for frag in fragments:
            merged_meta.update(frag.metadata)

        return ParsedFragment(
            content=combined_content,
            metadata=merged_meta,
            source_path=fragments[0].source_path,
            timestamp=earliest,
        )

    # ---- Private helpers ----

    def _split(self, fragment: ParsedFragment) -> list[ParsedFragment]:
        """Split a fragment using sections first, then length.

        Args:
            fragment: Fragment to split.

        Returns:
            List of child fragments.
        """
        sections = self.split_by_sections(fragment)
        if len(sections) > 1:
            return sections
        return self.split_by_length(fragment)

    def _make_children(
        self,
        parent: ParsedFragment,
        sections: list[str],
    ) -> list[ParsedFragment]:
        """Create child fragments from split sections.

        Each child inherits the parent's metadata and receives
        a ``parent_id`` entry.

        Args:
            parent: The original fragment being split.
            sections: Content strings for each child.

        Returns:
            List of child fragments with parent_id metadata.
        """
        parent_id = generate_fragment_id(
            parent.source_path,
            parent.timestamp,
            parent.content,
        )
        children: list[ParsedFragment] = []
        for section in sections:
            child_meta = parent.metadata.copy()
            child_meta["parent_id"] = parent_id
            children.append(
                ParsedFragment(
                    content=section,
                    metadata=child_meta,
                    source_path=parent.source_path,
                    timestamp=parent.timestamp,
                ),
            )
        return children

    def _group_pass(
        self,
        fragments: list[ParsedFragment],
    ) -> list[ParsedFragment]:
        """Run a grouping pass over the fragment list.

        Accumulates consecutive short fragments from the same source
        and merges them when the group is complete.

        Args:
            fragments: Input fragments (already split).

        Returns:
            Fragments with short runs grouped.
        """
        result: list[ParsedFragment] = []
        group: list[ParsedFragment] = []

        for frag in fragments:
            if self._can_extend_group(group, frag):
                group.append(frag)
            else:
                result.extend(self._flush_group(group))
                group = [frag]

        result.extend(self._flush_group(group))
        return result

    def _can_extend_group(
        self,
        group: list[ParsedFragment],
        frag: ParsedFragment,
    ) -> bool:
        """Check if a fragment can extend the current group.

        A fragment can extend when it is short, shares the same source,
        and falls within the configured time window of the last fragment.

        Args:
            group: Current accumulator of short fragments.
            frag: Candidate fragment.

        Returns:
            True if the fragment is short, same source, and within time window.
        """
        if not group:
            return _word_count(frag.content) < self.config.min_words

        time_delta = abs(
            (frag.timestamp - group[-1].timestamp).total_seconds(),
        )
        max_gap = self.config.group_time_window_minutes * 60

        return (
            frag.source_path == group[0].source_path
            and _word_count(frag.content) < self.config.min_words
            and time_delta <= max_gap
        )

    def _flush_group(
        self,
        group: list[ParsedFragment],
    ) -> list[ParsedFragment]:
        """Flush a group, merging if eligible.

        Args:
            group: Accumulated fragments.

        Returns:
            Merged fragment as single-element list, or the group as-is.
        """
        if not group:
            return []
        if self.should_group(group):
            return [self.group_fragments(group)]
        return group
