"""Static check: no Claude model-ID literals live outside ``config.py``.

FEAT-014 §pre-decided choices §27 mandates this: model IDs move, so the
constant ``DEFAULT_ROUTER_MODEL`` in ``config.py`` is the contract.
Other modules must read it (or accept a model argument resolved from
it), never reference the literal string.

The check is a simple grep so it remains fast and trivially debuggable
when it fires.
"""

from __future__ import annotations

import re
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "crawdad"
_ALLOWED_FILE = "config.py"
_MODEL_PATTERN = re.compile(r"claude-(haiku|sonnet|opus)-\d", re.IGNORECASE)


def test_no_model_id_literals_outside_config() -> None:
    """No ``crawdad/*.py`` (except ``config.py``) contains a model ID string."""
    offenders: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        if path.name == _ALLOWED_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        match = _MODEL_PATTERN.search(text)
        if match:
            offenders.append(f"{path.relative_to(_PACKAGE_ROOT)}: {match.group(0)!r}")
    assert not offenders, (
        "model-ID literals found outside config.py: "
        + ", ".join(offenders)
        + " — read DEFAULT_ROUTER_MODEL from config.py instead."
    )
