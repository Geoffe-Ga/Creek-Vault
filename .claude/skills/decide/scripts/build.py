#!/usr/bin/env python3
"""Build an interactive decision-sheet page from a JSON spec.

Usage:
    python3 build.py SHEET.json [--out PAGE.html]

Validates the sheet (see ../references/schema.md), checks every theme token
against a strict allowlist so DESIGN.md-derived values can never inject CSS or
markup, embeds the data safely, and writes the page next to the JSON unless
--out is given. Exits 1 with every problem listed when validation fails.
Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_DIR / "assets" / "template.html"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{3,8}"
    r"|(rgb|rgba|hsl|hsla|oklch|oklab|lab|lch)\([0-9.,%\s/+-]{1,60}\))$"
)
FONT_RE = re.compile(r"^[A-Za-z0-9 ,'\"_-]{1,160}$")
LENGTH_RE = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,2})?(px|rem|em)$")
FONTS_URL_PREFIX = "https://fonts.googleapis.com/css2?"
FONTS_URL_RE = re.compile(
    r"^https://fonts\.googleapis\.com/css2\?[A-Za-z0-9=&:;,@+.%_-]{1,600}$"
)
DEFAULT_FONTS = (
    "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500"
    "&family=Public+Sans:wght@400;500;600&family=Spectral:wght@500;600&display=swap"
)

COLOR_TOKENS = (
    "bg",
    "surface",
    "sunken",
    "fg",
    "muted",
    "line",
    "accent",
    "accent-ink",
    "accent-soft",
    "rec",
    "rec-soft",
    "sev-low",
    "sev-med",
    "sev-high",
)
FONT_TOKENS = {"display": "font-display", "body": "font-body", "mono": "font-mono"}
SEVERITIES = {"low", "medium", "high"}
RESERVED_IDS = {"__defer", "__other"}
MIN_OPTIONS = 2


class SheetError(ValueError):
    """Raised with every validation problem found in a sheet."""

    def __init__(self, problems: list[str]) -> None:
        """Store the problems and build a readable message."""
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


def _check_text(
    obj: dict[str, Any], key: str, where: str, problems: list[str], *, required: bool
) -> None:
    """Append a problem if obj[key] is missing (when required) or not a string."""
    value = obj.get(key)
    if value is None:
        if required:
            problems.append(f"{where}: missing required '{key}'")
        return
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}: '{key}' must be a non-empty string")


def _check_matrix(
    rows: object, row_key: str, option_ids: set[str], where: str, problems: list[str]
) -> None:
    """Check a trade-off or cost matrix: labeled rows, one cell per option."""
    if rows is None:
        return
    if not isinstance(rows, list):
        problems.append(f"{where}: must be a list")
        return
    for i, row in enumerate(rows):
        spot = f"{where}[{i}]"
        if not isinstance(row, dict):
            problems.append(f"{spot}: must be an object")
            continue
        _check_text(row, row_key, spot, problems, required=True)
        values = row.get("values")
        if not isinstance(values, dict) or not values:
            problems.append(f"{spot}: 'values' must map option ids to text")
            continue
        unknown = sorted(set(values) - option_ids)
        if unknown:
            problems.append(f"{spot}: unknown option id(s) {unknown}")


def _check_options(d: dict[str, Any], where: str, problems: list[str]) -> set[str]:
    """Validate a decision's options and return their ids."""
    options = d.get("options")
    if not isinstance(options, list) or len(options) < MIN_OPTIONS:
        problems.append(f"{where}: 'options' needs at least two choices")
        return set()
    ids: set[str] = set()
    for j, opt in enumerate(options):
        spot = f"{where}.options[{j}]"
        if not isinstance(opt, dict):
            problems.append(f"{spot}: must be an object")
            continue
        oid = opt.get("id")
        if not isinstance(oid, str) or not ID_RE.match(oid) or oid in RESERVED_IDS:
            problems.append(f"{spot}: 'id' must be lowercase letters, digits, hyphens")
        elif oid in ids:
            problems.append(f"{spot}: duplicate option id '{oid}'")
        else:
            ids.add(oid)
        _check_text(opt, "label", spot, problems, required=True)
        _check_text(opt, "summary", spot, problems, required=False)
        for key in ("pros", "cons"):
            items = opt.get(key, [])
            if not isinstance(items, list) or not all(
                isinstance(x, str) for x in items
            ):
                problems.append(f"{spot}: '{key}' must be a list of strings")
    return ids


def _check_risks(
    risks: object, option_ids: set[str], where: str, problems: list[str]
) -> None:
    """Check each risk has a severity, a mitigation, and only known option ids."""
    if risks is None:
        return
    if not isinstance(risks, list):
        problems.append(f"{where}.risks: must be a list")
        return
    for k, risk in enumerate(risks):
        spot = f"{where}.risks[{k}]"
        if not isinstance(risk, dict):
            problems.append(f"{spot}: must be an object")
            continue
        _check_text(risk, "risk", spot, problems, required=True)
        if risk.get("severity", "medium") not in SEVERITIES:
            problems.append(f"{spot}: 'severity' must be one of {sorted(SEVERITIES)}")
        unknown = sorted(set(risk.get("options", [])) - option_ids)
        if unknown:
            problems.append(f"{spot}: unknown option id(s) {unknown}")


def _check_examples(examples: object, where: str, problems: list[str]) -> None:
    """Check optional examples: each needs a name, and any URL must be https."""
    if examples is None:
        return
    if not isinstance(examples, list):
        problems.append(f"{where}.examples: must be a list")
        return
    for k, ex in enumerate(examples):
        spot = f"{where}.examples[{k}]"
        if not isinstance(ex, dict):
            problems.append(f"{spot}: must be an object")
            continue
        _check_text(ex, "title", spot, problems, required=True)
        url = ex.get("url")
        if url is not None and (
            not isinstance(url, str) or not url.startswith("https://")
        ):
            problems.append(f"{spot}: 'url' must start with https://")


def _check_decision(d: object, i: int, seen: set[str], problems: list[str]) -> None:
    """Validate one decision and record its id in seen to catch duplicates."""
    where = f"decisions[{i}]"
    if not isinstance(d, dict):
        problems.append(f"{where}: must be an object")
        return
    did = d.get("id")
    if not isinstance(did, str) or not ID_RE.match(did):
        problems.append(f"{where}: 'id' must be lowercase letters, digits, hyphens")
    elif did in seen:
        problems.append(f"{where}: duplicate decision id '{did}'")
    else:
        seen.add(did)
        where = f"decisions[{i}] ({did})"
    for key in ("title", "summary"):
        _check_text(d, key, where, problems, required=True)
    for key in ("question", "recommendation_reason"):
        _check_text(d, key, where, problems, required=False)
    option_ids = _check_options(d, where, problems)
    rec = d.get("recommended")
    if rec is not None and rec not in option_ids:
        problems.append(
            f"{where}: 'recommended' is '{rec}', which is not one of its option ids"
        )
    if rec is not None and not d.get("recommendation_reason"):
        problems.append(f"{where}: 'recommended' needs a 'recommendation_reason'")
    _check_matrix(
        d.get("tradeoffs"), "dimension", option_ids, f"{where}.tradeoffs", problems
    )
    _check_matrix(d.get("costs"), "item", option_ids, f"{where}.costs", problems)
    _check_risks(d.get("risks"), option_ids, where, problems)
    _check_examples(d.get("examples"), where, problems)
    for key, label in (
        ("tradeoffs", "Trade-offs"),
        ("risks", "Risks"),
        ("costs", "Costs"),
        ("examples", "Examples"),
    ):
        if not d.get(key):
            problems.append(f"{where}: '{key}' is empty; every decision needs {label}")


def _check_palettes(theme: dict[str, Any], problems: list[str]) -> None:
    """Check the light and dark palettes hold only known tokens and plain colors."""
    for mode in ("light", "dark"):
        palette = theme.get(mode, {})
        if not isinstance(palette, dict):
            problems.append(f"theme.{mode}: must be an object")
            continue
        for token, value in palette.items():
            if token not in COLOR_TOKENS:
                allowed = ", ".join(COLOR_TOKENS)
                problems.append(
                    f"theme.{mode}: unknown token '{token}' (allowed: {allowed})"
                )
            elif not isinstance(value, str) or not COLOR_RE.match(value.strip()):
                problems.append(
                    f"theme.{mode}.{token}: '{value}' is not a plain CSS color"
                )


def _check_fonts(fonts: object, problems: list[str]) -> None:
    """Check font roles are known and each value is a plain font stack."""
    if not isinstance(fonts, dict):
        problems.append("theme.fonts: must be an object")
        return
    for role, value in fonts.items():
        if role not in FONT_TOKENS:
            problems.append(
                f"theme.fonts: unknown role '{role}' (allowed: display, body, mono)"
            )
        elif not isinstance(value, str) or not FONT_RE.match(value):
            problems.append(f"theme.fonts.{role}: not a plain font stack")


def _check_theme(theme: object, problems: list[str]) -> None:
    """Validate the DESIGN.md theme: plain colors, font stacks, lengths only."""
    if theme is None:
        return
    if not isinstance(theme, dict):
        problems.append("theme: must be an object")
        return
    allowed = {"light", "dark", "fonts", "fontsUrl", "radius", "source"}
    problems.extend(
        f"theme: unknown key '{key}'" for key in sorted(set(theme) - allowed)
    )
    _check_palettes(theme, problems)
    _check_fonts(theme.get("fonts", {}), problems)
    url = theme.get("fontsUrl")
    if url is not None and (not isinstance(url, str) or not FONTS_URL_RE.match(url)):
        problems.append(f"theme.fontsUrl: must be a {FONTS_URL_PREFIX}… stylesheet URL")
    radius = theme.get("radius")
    if radius is not None and (
        not isinstance(radius, str) or not LENGTH_RE.match(radius)
    ):
        problems.append("theme.radius: must be a length like '8px' or '0.5rem'")


def validate(sheet: object) -> None:
    """Raise SheetError listing every problem in the sheet; return None when valid."""
    problems: list[str] = []
    if not isinstance(sheet, dict):
        raise SheetError(["sheet: top level must be a JSON object"])
    sid = sheet.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        problems.append(
            "sheet: 'id' must be lowercase letters, digits, hyphens (it keys saved answers)"
        )
    _check_text(sheet, "title", "sheet", problems, required=True)
    for key in ("prompt", "context", "created"):
        _check_text(sheet, key, "sheet", problems, required=False)
    decisions = sheet.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        problems.append("sheet: 'decisions' must be a non-empty list")
        decisions = []
    seen: set[str] = set()
    for i, d in enumerate(decisions):
        _check_decision(d, i, seen, problems)
    _check_theme(sheet.get("theme"), problems)
    if problems:
        raise SheetError(problems)


def theme_css(theme: dict[str, Any] | None) -> str:
    """Return CSS that overrides template tokens with validated theme values."""
    if not theme:
        return ""

    def block(palette: dict[str, str], extra: str = "") -> str:
        """Render one palette as CSS custom-property declarations."""
        decls = [f"--{token}: {value.strip()};" for token, value in palette.items()]
        return " ".join(decls) + extra

    light = dict(theme.get("light", {}))
    root_extra = "".join(
        f" --{FONT_TOKENS[r]}: {v};" for r, v in theme.get("fonts", {}).items()
    )
    if theme.get("radius"):
        root_extra += f" --radius: {theme['radius']};"
    parts = []
    if light or root_extra:
        parts.append(f":root {{ {block(light, root_extra)} }}")
    dark = dict(theme.get("dark", {}))
    if dark:
        body = block(dark, " color-scheme: dark;")
        dark_query = (
            '@media (prefers-color-scheme: dark) { :root:not([data-theme="light"])'
        )
        parts.extend(
            (
                f"{dark_query} {{ {body} }} }}",
                f':root[data-theme="dark"] {{ {body} }}',
            )
        )
    return "\n".join(parts)


def render(sheet: dict[str, Any], template: str) -> str:
    """Return the finished page for a validated sheet."""
    theme = sheet.get("theme") or {}
    fonts_url = theme.get("fontsUrl", DEFAULT_FONTS)
    fonts = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{html.escape(fonts_url, quote=True)}">'
    )
    data = json.dumps(
        {k: v for k, v in sheet.items() if k != "theme"}, ensure_ascii=False
    )
    # Keep the JSON inert inside <script>: no "</script>", "<!--", or line separators.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    out = template
    for marker, value in (
        ("__DECIDE_TITLE__", html.escape(sheet["title"])),
        ("<!--__DECIDE_FONTS__-->", fonts),
        ("/*__DECIDE_THEME__*/", theme_css(theme)),
        ("__DECIDE_DATA__", data),
    ):
        if out.count(marker) != 1:
            msg = f"template must contain {marker} exactly once"
            raise RuntimeError(msg)
        out = out.replace(marker, value)
    return out


def main(argv: list[str] | None = None) -> int:
    """Validate SHEET.json and write the page; return a process exit code."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sheet", type=Path, help="decision sheet JSON")
    parser.add_argument(
        "--out", type=Path, help="output .html (default: next to the JSON)"
    )
    args = parser.parse_args(argv)
    try:
        sheet = json.loads(args.sheet.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Could not read {args.sheet}: {exc}\n")
        return 1
    try:
        validate(sheet)
    except SheetError as exc:
        sys.stderr.write(f"{args.sheet} has {len(exc.problems)} problem(s):\n{exc}\n")
        return 1
    out = args.out or args.sheet.with_suffix(".html")
    out.write_text(
        render(sheet, TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8"
    )
    sys.stdout.write(f"{out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
