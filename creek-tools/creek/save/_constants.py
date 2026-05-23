"""Shared constants for ``creek save`` (FEAT-009).

A leaf module that imports nothing from the rest of :mod:`creek.save`,
so it can be pulled in from anywhere — notably
:mod:`creek.classify.privacy_filter`, which used to keep a duplicate
``_PRE_SAVE_INTIMATE_STUB_RELPATH`` mirror to avoid a circular import
back into ``creek.save``. The mirror is gone (issue #223): both call
sites now read from this single source of truth.
"""

from __future__ import annotations

from pathlib import Path

INTIMATE_STUB_RELPATH = Path("10-Liminal/Compost/intimate-stubs")
"""Where intimate-tier bodies land — gitignored at the repo level."""
