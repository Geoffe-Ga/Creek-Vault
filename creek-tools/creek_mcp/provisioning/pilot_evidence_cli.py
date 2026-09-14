"""Non-networking CLI for sanitizing managed-vault pilot evidence (#1806)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from creek_mcp.provisioning.pilot_evidence import (
    PilotEvidenceInput,
    PilotPrerequisiteDocument,
    reduce_pilot_evidence,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Return the deliberately offline evidence command contract."""
    parser = argparse.ArgumentParser(prog="creek-provisioning-pilot-evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    reduce_command = commands.add_parser(
        "reduce",
        help="Validate private local observations and print sanitized evidence.",
    )
    reduce_command.add_argument("--input", type=Path, required=True)
    commands.add_parser("schema", help="Print the versioned sanitized JSON Schema.")
    return parser


def _reduce(path: Path) -> int:
    """Read and sanitize one private local input without echoing failures."""
    try:
        source = PilotEvidenceInput.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        prerequisite = reduce_pilot_evidence(source)
    except (OSError, UnicodeError, ValidationError, ValueError):
        print("pilot evidence input is invalid", file=sys.stderr)
        return 2
    document = PilotPrerequisiteDocument(
        managed_vault_pilot_prerequisite=prerequisite,
    )
    print(document.model_dump_json(indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline reducer or print its public output schema."""
    args = build_parser().parse_args(argv)
    if args.command == "schema":
        print(json.dumps(PilotPrerequisiteDocument.model_json_schema(), indent=2))
        return 0
    return _reduce(args.input)


if __name__ == "__main__":
    sys.exit(main())
