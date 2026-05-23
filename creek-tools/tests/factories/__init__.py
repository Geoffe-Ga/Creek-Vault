"""Shared test factories for creek-tools.

Centralising on-disk fixture construction so the canonical shape of a
test artefact lives in one place. Test modules import these helpers
instead of defining their own per-module copies, which used to drift
silently when the underlying schema evolved.
"""
