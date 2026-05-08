"""Compile primitive — roll fragments up into compiled-layer pages.

The :mod:`creek.compile` package implements ``creek compile``: the
boundary operation that takes raw fragments from ``01-Fragments/`` and
synthesises them into compiled-layer pages under ``02-Threads/``,
``03-Eddies/``, and ``06-Frequencies/`` (FEAT-003, ADOPT-001).

Per-claim provenance back to source fragment IDs is non-negotiable —
the spec calls lossy compression here a load-bearing risk for voice
fidelity. The :class:`~creek.compile.provenance.ProvenanceEntry` model
encodes the per-claim → fragment-ID mapping that lives in compiled
page frontmatter.

When the LLM detects contradictions across the fragments being
compiled, paradoxes are routed to a side-channel JSONL log under
``00-Creek-Meta/Processing-Log/`` rather than being flattened into the
synthesis page (ontology spec §10.2).

The submodules are imported on demand (``from creek.compile.engine
import ...``) rather than re-exported here — :mod:`creek.compile.engine`
depends on :class:`~creek.models.CompiledPage`, which itself imports
:class:`~creek.compile.provenance.ProvenanceEntry`, so eagerly loading
``engine`` from this ``__init__`` would form a circular import via
:mod:`creek.models`.
"""
