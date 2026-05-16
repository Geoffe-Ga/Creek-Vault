"""Index generator for the Creek vault.

Generates Obsidian Dataview-powered index notes for frequencies, threads,
eddies, temporal views, and source statistics. Each generated note contains
YAML frontmatter and Dataview query blocks that dynamically aggregate
vault content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.models import Frequency

if TYPE_CHECKING:
    from pathlib import Path

FREQUENCY_NAMES: dict[Frequency, str] = {
    Frequency.F1: "Agency/Survival",
    Frequency.F2: "Receptivity/Kinship",
    Frequency.F3: "Self-Love/Power",
    Frequency.F4: "Community-Love/Conformity",
    Frequency.F5: "Achievism/Innovation",
    Frequency.F6: "Pluralism/Empathy",
    Frequency.F7: "Integration/Systems",
    Frequency.F8: "True-Self/Transcendence",
    Frequency.F9: "Unity/Cosmic",
    Frequency.F10: "Emptiness/Impermanence",
}
"""Mapping from Frequency enum values to human-readable APTITUDE names."""

FREQUENCY_COLORS: dict[Frequency, str] = {
    Frequency.F1: "beige",
    Frequency.F2: "purple",
    Frequency.F3: "red",
    Frequency.F4: "blue",
    Frequency.F5: "orange",
    Frequency.F6: "green",
    Frequency.F7: "yellow",
    Frequency.F8: "teal",
    Frequency.F9: "ultraviolet",
    Frequency.F10: "clear_light",
}
"""Mapping from Frequency enum values to their ontology color designations."""

FREQUENCY_THEMES: dict[Frequency, str] = {
    Frequency.F1: "Survival, intentional action, willpower, initiative",
    Frequency.F2: (
        "Kinship, receptivity to pleasure and Source,"
        " intuitive divination, surrender, trust"
    ),
    Frequency.F3: (
        "Self-love as the foundation of healthy power;"
        " assertion, individuality, strength, confidence"
    ),
    Frequency.F4: (
        "Community love; devotion, moral grounding,"
        " hierarchy, belonging through shared values"
    ),
    Frequency.F5: "Innovation, analysis, goal-setting, material success",
    Frequency.F6: "Empathy, inclusivity, embodied connection, shadow work",
    Frequency.F7: "Systems thinking, synthesis, holistic understanding",
    Frequency.F8: (
        "Higher self, monad, deep intuition;"
        " communicating with true self yields alignment"
    ),
    Frequency.F9: "Source connection, cosmic harmony, non-dual awareness",
    Frequency.F10: "Impermanence, no-self, egolessness",
}
"""Core thematic descriptions for each frequency from the ontology."""

FREQUENCY_SIGNALS: dict[Frequency, str] = {
    Frequency.F1: (
        "Goal-setting, project planning, proactive problem-solving,"
        " taking charge, discipline, basic needs"
    ),
    Frequency.F2: (
        "Letting go, accepting help, ancestral bonds, community rituals,"
        " shared myths, slowing down, receiving pleasure,"
        " divination/tarot/oracle work"
    ),
    Frequency.F3: (
        "Leadership, boundary-setting, authentic confidence,"
        " self-compassion, healing shame, embodying worth,"
        " healthy competition, victories"
    ),
    Frequency.F4: (
        "Rules, authority, purpose, faith, discipline, service,"
        " frustration with rigidity, moral frameworks,"
        " showing up for others"
    ),
    Frequency.F5: (
        "Theory-building, critical thinking, research,"
        " experimentation, status, competition"
    ),
    Frequency.F6: (
        "Relationships, community building, vulnerability,"
        " sensitivity, performative vs. genuine connection"
    ),
    Frequency.F7: (
        "Connecting frameworks, seeing patterns,"
        " building personal philosophy, meta-cognition,"
        " creative destruction"
    ),
    Frequency.F8: (
        "Deep intuition, higher-self dialogue, gnosis,"
        " channeling, synchronicity,"
        " acting from alignment rather than ego"
    ),
    Frequency.F9: (
        "Mystical experience, oneness,"
        " dissolution of self/other, devotion, bliss,"
        " flow with universal will"
    ),
    Frequency.F10: (
        "Buddhist insights, letting go of attainment,"
        " void, groundlessness, humor about it all"
    ),
}
"""Content classification signals for each frequency."""

_WAVELENGTH_PHASES: list[str] = [
    "Rising",
    "Peaking",
    "Withdrawal",
    "Diminishing",
    "Bottoming Out",
    "Restoration",
]
"""Six wavelength phases used in dosage tables."""

_DOSAGE_MEDICINE: dict[Frequency, list[str]] = {
    Frequency.F1: [
        "Commitment",
        "Diligence",
        "Steadiness",
        "Security",
        "Planning",
        "Next Habit",
    ],
    Frequency.F2: [
        "Inspiration",
        "Joy",
        "Introspectivity",
        "Tranquility",
        "Convalescence",
        "Recuperation",
    ],
    Frequency.F3: [
        "Leading",
        "Power-With",
        "Stepping Back",
        "Self-Acceptance",
        "Following",
        "Assembling",
    ],
    Frequency.F4: [
        "Ambition",
        "Attunement",
        "Discernment",
        "Conviction",
        "Surrender",
        "Catharsis",
    ],
    Frequency.F5: [
        "Hypothesize",
        "Experiment",
        "Collect Data",
        "Analyze",
        "Synthesize",
        "Question",
    ],
    Frequency.F6: [
        "Connection",
        "Belonging",
        "Retirement",
        "Unwinding",
        "Repose",
        "Vulnerability",
    ],
    Frequency.F7: [
        "Rebellion",
        "Anarchy",
        "Organize",
        "Establish",
        "Order",
        "Disintegrate",
    ],
    Frequency.F8: [
        "Epiphany",
        "Gnosis",
        "Receptivity",
        "Absorption",
        "Metabolism",
        "Pattern-Seeking",
    ],
    Frequency.F9: [
        "Unification of Mind",
        "Jhana",
        "Metta and Meditative Joy",
        "Sustained Attention",
        "Pleasure",
        "Directed Attention",
    ],
}
"""Medicine (right-sized) dosage expressions per frequency and phase.

Note: F10 (Clear Light / Emptiness) has no dosage mapping in the ontology.
The dosage framework covers F1-F9 colors (Beige through Ultraviolet).
"""

_DOSAGE_TOXIC: dict[Frequency, list[str]] = {
    Frequency.F1: [
        "Overcommitment",
        "Thriving",
        "Burnout",
        "Grasping",
        "Overwhelm",
        "New Plan",
    ],
    Frequency.F2: [
        "Grandiosity",
        "Ecstasy",
        "Anxiety",
        "Self-Doubt",
        "Self-Loathing",
        "Selfishness",
    ],
    Frequency.F3: [
        "Dominating",
        "Power-Over",
        "Crumbling",
        "Shame",
        "Subjugation",
        "Revenge",
    ],
    Frequency.F4: [
        "Voraciousness",
        "Leprosy",
        "Self-Medication",
        "Rage",
        "Misery",
        "Self-Repression",
    ],
    Frequency.F5: [
        "Assert",
        "Crusade",
        "Overlook Details",
        "Force it",
        "Fail",
        "Presume",
    ],
    Frequency.F6: [
        "Oversharing",
        "Megalomania",
        "Social Anxiety",
        "Alienation",
        "Isolation",
        "Bitterness",
    ],
    Frequency.F7: [
        "Mischief",
        "Chaos",
        "Discord",
        "Confusion",
        "Bureaucracy",
        "The Aftermath",
    ],
    Frequency.F8: [
        "Delusion",
        "Psychosis",
        "Paranoia",
        "Horror",
        "Despair",
        "Belief Salience",
    ],
    Frequency.F9: [
        "Worldly Desire",
        "Bliss Addiction",
        "Agitation Due to Worry or Remorse",
        "Doubt",
        "Aversion",
        "Laziness or Lethargy",
    ],
}
"""Toxic (overdose) dosage expressions per frequency and phase.

Note: F10 (Clear Light / Emptiness) has no dosage mapping in the ontology.
The dosage framework covers F1-F9 colors (Beige through Ultraviolet).
"""

_CROSS_FREQUENCY_LINKS: dict[Frequency, list[str]] = {
    Frequency.F1: ["F3 (Power grounded in agency)", "F5 (Strategy from will)"],
    Frequency.F2: ["F6 (Empathy from kinship)", "F9 (Source from surrender)"],
    Frequency.F3: [
        "F1 (Agency fuels power)",
        "F4 (Self-love grounds community love)",
    ],
    Frequency.F4: [
        "F3 (Power grounded in community)",
        "F6 (Shared values to empathy)",
    ],
    Frequency.F5: ["F1 (Will to achieve)", "F7 (Analysis to integration)"],
    Frequency.F6: [
        "F2 (Kinship to community)",
        "F4 (Belonging through values)",
    ],
    Frequency.F7: [
        "F5 (Analysis to synthesis)",
        "F8 (Integration to transcendence)",
    ],
    Frequency.F8: [
        "F7 (Systems to true self)",
        "F9 (Transcendence to unity)",
    ],
    Frequency.F9: [
        "F8 (True self to cosmic harmony)",
        "F10 (Unity to emptiness)",
    ],
    Frequency.F10: [
        "F9 (Unity dissolves into emptiness)",
        "F2 (Emptiness returns to receptivity)",
    ],
}
"""Developmental links between frequencies for cross-referencing."""


def _build_note(frontmatter: dict[str, str], body: str) -> str:
    """Build a markdown note with YAML frontmatter and body content.

    Args:
        frontmatter: Key-value pairs for the YAML frontmatter block.
        body: The markdown body content after the frontmatter.

    Returns:
        Complete markdown string with ``---`` delimited frontmatter.
    """
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in frontmatter.items())
    lines.extend(("---", "", body))
    return "\n".join(lines)


def _build_frequency_body(freq: Frequency, freq_code: str, name: str) -> str:
    """Build the full markdown body for a frequency index note.

    Assembles all sections: description, Dataview queries (primary,
    secondary, threads), statistics, dosage table, and cross-frequency links.

    Args:
        freq: The Frequency enum member.
        freq_code: Short code like ``"F1"``.
        name: Human-readable frequency name.

    Returns:
        Complete markdown body string.
    """
    parts: list[str] = [
        # Title
        f"# {freq_code} — {name}\n",
        # Description
        "## Description\n",
        f"**Core Theme:** {FREQUENCY_THEMES[freq]}\n",
        f"**Color:** {FREQUENCY_COLORS[freq].replace('_', ' ').title()}\n",
        f"**Signals:** {FREQUENCY_SIGNALS[freq]}\n",
        # Primary fragments query
        "## Fragments\n",
        "```dataview",
        "TABLE frequency.primary, wavelength.phase, created",
        'FROM "01-Fragments"',
        f'WHERE frequency.primary = "{freq_code}"',
        "SORT created DESC",
        "```\n",
        # Secondary frequency query
        "## Secondary Frequency Appearances\n",
        "```dataview",
        "TABLE frequency.primary, frequency.secondary, created",
        'FROM "01-Fragments"',
        f'WHERE contains(frequency.secondary, "{freq_code}")',
        "SORT created DESC",
        "```\n",
        # Threads by frequency affinity
        "## Threads\n",
        "```dataview",
        "TABLE status, first_seen, last_seen, fragment_count",
        'FROM "02-Threads"',
        f'WHERE contains(frequency_affinity, "{freq_code}")',
        "SORT last_seen DESC",
        "```\n",
        # Statistics
        "## Statistics\n",
        "```dataview",
        "TABLE length(rows) AS count",
        'FROM "01-Fragments"',
        f'WHERE frequency.primary = "{freq_code}"',
        'GROUP BY dateformat(created, "yyyy-MM") AS month',
        "SORT month DESC",
        "```\n",
    ]

    # Dosage table (F10 has no dosage mapping in the ontology)
    if freq in _DOSAGE_MEDICINE:
        parts.extend(
            (
                "## Medicine vs. Toxic Dose\n",
                _build_dosage_table(freq),
            )
        )

    # Cross-frequency links
    parts.append("## Cross-Frequency Links\n")
    for link in _CROSS_FREQUENCY_LINKS[freq]:
        parts.append(f"- {link}")
    parts.append("")

    return "\n".join(parts)


def _build_dosage_table(freq: Frequency) -> str:
    """Build a markdown table comparing medicine and toxic dosage expressions.

    Creates a table with wavelength phases as columns and medicine/toxic
    rows for the given frequency.

    Args:
        freq: The Frequency enum member to build the table for.

    Returns:
        Markdown table string with header, medicine row, and toxic row.
    """
    header = "| | " + " | ".join(_WAVELENGTH_PHASES) + " |"
    separator = "| --- " + "| --- " * len(_WAVELENGTH_PHASES) + "|"

    medicine = _DOSAGE_MEDICINE[freq]
    toxic = _DOSAGE_TOXIC[freq]

    med_row = "| **Medicine** | " + " | ".join(medicine) + " |"
    tox_row = "| **Toxic** | " + " | ".join(toxic) + " |"

    return "\n".join([header, separator, med_row, tox_row, ""])


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
        a markdown index note in each one containing ontology descriptions,
        Dataview queries for fragments/threads, dosage comparison tables,
        statistics, and cross-frequency developmental links.

        Returns:
            List of paths to the generated frequency index files.
        """
        freq_dir = self.vault_path / "06-Frequencies"
        generated: list[Path] = []

        for freq, name in FREQUENCY_NAMES.items():
            freq_code = freq.value
            subdir = self._find_frequency_subdir(freq_dir, freq_code)
            if subdir is None:
                continue

            color = FREQUENCY_COLORS[freq]
            frontmatter = {
                "type": "frequency-index",
                "frequency": freq_code,
                "color": color,
                "title": f'"{freq_code} — {name} Index"',
            }

            body = _build_frequency_body(freq, freq_code, name)

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
        generated: list[Path] = self.generate_frequency_indexes()
        generated.extend(
            (
                self.generate_thread_index(),
                self.generate_eddy_map(),
                self.generate_temporal_index(),
                self.generate_source_index(),
            )
        )
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
