"""Index generator for the Creek vault.

Generates Obsidian Dataview-powered index notes for frequencies, threads,
eddies, temporal views, and source statistics. Each generated note contains
YAML frontmatter and Dataview query blocks that dynamically aggregate
vault content.
"""

from pathlib import Path

from creek.models import Frequency

FREQUENCY_NAMES: dict[Frequency, str] = {
    Frequency.F1: "Survival/Safety",
    Frequency.F2: "Connection/Belonging",
    Frequency.F3: "Power/Agency",
    Frequency.F4: "Order/Structure",
    Frequency.F5: "Achievement/Strategy",
    Frequency.F6: "Community/Harmony",
    Frequency.F7: "Systems/Integration",
    Frequency.F8: "Holistic/Global",
    Frequency.F9: "Cosmic/Transpersonal",
    Frequency.F10: "Unity/Transcendence",
}
"""Mapping from Frequency enum values to human-readable APTITUDE names."""


def _build_note(frontmatter: dict[str, str], body: str) -> str:
    """Build a markdown note with YAML frontmatter and body content.

    Args:
        frontmatter: Key-value pairs for the YAML frontmatter block.
        body: The markdown body content after the frontmatter.

    Returns:
        Complete markdown string with ``---`` delimited frontmatter.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


class IndexGenerator:
    """Generates Dataview-powered index notes for the Creek vault.

    Creates index notes in the appropriate vault directories, each containing
    Dataview query stubs that dynamically aggregate content from fragments,
    threads, eddies, and other vault primitives.

    Attributes:
        vault_path: Path to the root of the Obsidian vault.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialize the IndexGenerator with a vault path.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.
        """
        self.vault_path = vault_path

    def generate_frequency_indexes(self) -> list[Path]:
        """Generate one index note per frequency in 06-Frequencies/ subdirs.

        Scans existing frequency subdirectories (F1 through F10) and creates
        a markdown index note in each one with a Dataview query that lists
        fragments classified under that frequency.

        Returns:
            List of paths to the generated frequency index files.
        """
        freq_dir = self.vault_path / "06-Frequencies"
        generated: list[Path] = []

        for freq, name in FREQUENCY_NAMES.items():
            freq_code = freq.value  # e.g., "F1"
            # Find matching subdirectory
            subdir = self._find_frequency_subdir(freq_dir, freq_code)
            if subdir is None:
                continue

            frontmatter = {
                "type": "frequency-index",
                "frequency": freq_code,
                "title": f'"{freq_code} — {name} Index"',
            }

            body = f"# {freq_code} — {name}\n\n"
            body += "## Fragments\n\n"
            body += "```dataview\n"
            body += "TABLE frequency.primary, wavelength.phase, created\n"
            body += 'FROM "01-Fragments"\n'
            body += f'WHERE frequency.primary = "{freq_code}"\n'
            body += "SORT created DESC\n"
            body += "```\n\n"
            body += "## Secondary Frequency Appearances\n\n"
            body += "```dataview\n"
            body += "TABLE frequency.primary, frequency.secondary, created\n"
            body += 'FROM "01-Fragments"\n'
            body += f'WHERE contains(frequency.secondary, "{freq_code}")\n'
            body += "SORT created DESC\n"
            body += "```\n"

            note_path = subdir / f"{freq_code}-Index.md"
            note_path.write_text(_build_note(frontmatter, body), encoding="utf-8")
            generated.append(note_path)

        return generated

    def generate_thread_index(self) -> Path:
        """Generate a master thread list in 02-Threads/.

        Creates a markdown note with Dataview queries that list all threads
        grouped by status (active, dormant, resolved).

        Returns:
            Path to the generated thread index file.
        """
        frontmatter = {
            "type": "thread-index",
            "title": '"Thread Index"',
        }

        body = "# Thread Index\n\n"
        body += "## Active Threads\n\n"
        body += "```dataview\n"
        body += "TABLE status, first_seen, last_seen, fragment_count\n"
        body += 'FROM "02-Threads"\n'
        body += 'WHERE type = "thread" AND status = "active"\n'
        body += "SORT last_seen DESC\n"
        body += "```\n\n"
        body += "## Dormant Threads\n\n"
        body += "```dataview\n"
        body += "TABLE status, first_seen, last_seen, fragment_count\n"
        body += 'FROM "02-Threads"\n'
        body += 'WHERE type = "thread" AND status = "dormant"\n'
        body += "SORT last_seen DESC\n"
        body += "```\n\n"
        body += "## Resolved Threads\n\n"
        body += "```dataview\n"
        body += "TABLE status, first_seen, last_seen, fragment_count\n"
        body += 'FROM "02-Threads"\n'
        body += 'WHERE type = "thread" AND status = "resolved"\n'
        body += "SORT last_seen DESC\n"
        body += "```\n"

        note_path = self.vault_path / "02-Threads" / "Thread-Index.md"
        note_path.write_text(_build_note(frontmatter, body), encoding="utf-8")
        return note_path

    def generate_eddy_map(self) -> Path:
        """Generate an eddy overview in 03-Eddies/.

        Creates a markdown note with a Dataview query listing all eddies
        with their thread connections and fragment counts.

        Returns:
            Path to the generated eddy map file.
        """
        frontmatter = {
            "type": "eddy-map",
            "title": '"Eddy Map"',
        }

        body = "# Eddy Map\n\n"
        body += "## All Eddies\n\n"
        body += "```dataview\n"
        body += "TABLE formed, fragment_count, length(threads) AS thread_count\n"
        body += 'FROM "03-Eddies"\n'
        body += 'WHERE type = "eddy"\n'
        body += "SORT fragment_count DESC\n"
        body += "```\n\n"
        body += "## Eddies by Thread Count\n\n"
        body += "```dataview\n"
        body += "TABLE formed, threads, fragment_count\n"
        body += 'FROM "03-Eddies"\n'
        body += 'WHERE type = "eddy"\n'
        body += "SORT length(threads) DESC\n"
        body += "```\n"

        note_path = self.vault_path / "03-Eddies" / "Eddy-Map.md"
        note_path.write_text(_build_note(frontmatter, body), encoding="utf-8")
        return note_path

    def generate_temporal_index(self) -> Path:
        """Generate a year/month/week temporal view in 00-Creek-Meta/.

        Creates a markdown note with Dataview queries that group fragments
        by creation date for temporal navigation.

        Returns:
            Path to the generated temporal index file.
        """
        frontmatter = {
            "type": "temporal-index",
            "title": '"Temporal Index"',
        }

        body = "# Temporal Index\n\n"
        body += "## Recent Fragments (Last 30 Days)\n\n"
        body += "```dataview\n"
        body += "TABLE frequency.primary, wavelength.phase, created\n"
        body += 'FROM "01-Fragments"\n'
        body += "WHERE created >= date(today) - dur(30 days)\n"
        body += "SORT created DESC\n"
        body += "```\n\n"
        body += "## Fragments by Month\n\n"
        body += "```dataview\n"
        body += "TABLE length(rows) AS count\n"
        body += 'FROM "01-Fragments"\n'
        body += 'GROUP BY dateformat(created, "yyyy-MM") AS month\n'
        body += "SORT month DESC\n"
        body += "```\n\n"
        body += "## Fragments by Week\n\n"
        body += "```dataview\n"
        body += "TABLE length(rows) AS count\n"
        body += 'FROM "01-Fragments"\n'
        body += "GROUP BY dateformat(created, \"yyyy-'W'WW\") AS week\n"
        body += "SORT week DESC\n"
        body += "```\n"

        note_path = self.vault_path / "00-Creek-Meta" / "Temporal-Index.md"
        note_path.write_text(_build_note(frontmatter, body), encoding="utf-8")
        return note_path

    def generate_source_index(self) -> Path:
        """Generate a source statistics note in 00-Creek-Meta/.

        Creates a markdown note with Dataview queries that aggregate
        fragment counts by source platform.

        Returns:
            Path to the generated source index file.
        """
        frontmatter = {
            "type": "source-index",
            "title": '"Source Index"',
        }

        body = "# Source Index\n\n"
        body += "## Fragments by Source Platform\n\n"
        body += "```dataview\n"
        body += "TABLE length(rows) AS count\n"
        body += 'FROM "01-Fragments"\n'
        body += "GROUP BY source.platform\n"
        body += "SORT length(rows) DESC\n"
        body += "```\n\n"
        body += "## Recent Ingestions\n\n"
        body += "```dataview\n"
        body += "TABLE source.platform, source.original_file, ingested\n"
        body += 'FROM "01-Fragments"\n'
        body += "SORT ingested DESC\n"
        body += "LIMIT 20\n"
        body += "```\n"

        note_path = self.vault_path / "00-Creek-Meta" / "Source-Index.md"
        note_path.write_text(_build_note(frontmatter, body), encoding="utf-8")
        return note_path

    def generate_all(self) -> list[Path]:
        """Run all index generators and return the combined list of paths.

        Calls each individual generator method and aggregates all generated
        file paths into a single list.

        Returns:
            Combined list of all generated index file paths.
        """
        generated: list[Path] = []
        generated.extend(self.generate_frequency_indexes())
        generated.append(self.generate_thread_index())
        generated.append(self.generate_eddy_map())
        generated.append(self.generate_temporal_index())
        generated.append(self.generate_source_index())
        return generated

    @staticmethod
    def _find_frequency_subdir(freq_dir: Path, freq_code: str) -> Path | None:
        """Find the subdirectory matching a frequency code.

        Scans ``freq_dir`` for a subdirectory whose name starts with
        the given frequency code (e.g., ``F1``).

        Args:
            freq_dir: The 06-Frequencies directory path.
            freq_code: Frequency code to match (e.g., ``"F1"``).

        Returns:
            Path to the matching subdirectory, or ``None`` if not found.
        """
        if not freq_dir.is_dir():
            return None
        for child in sorted(freq_dir.iterdir()):
            if child.is_dir() and child.name.startswith(f"{freq_code}-"):
                return child
        return None
