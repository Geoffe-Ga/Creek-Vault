"""Engine for ``creek compile`` (FEAT-003).

The engine orchestrates one compile run:

1. Build a wavelength-aware prompt from the source fragments,
   honouring privacy tiers (intimate / personal contribute title-only).
2. Call the injected :data:`CompileLLM` and parse its JSON response into
   ``claims`` and ``paradoxes``.
3. Materialise a :class:`~creek.models.CompiledPage` carrying one
   :class:`~creek.compile.provenance.ProvenanceEntry` per claim, and
   route paradoxes to the side-channel log (never into the body).
4. On idempotent re-runs, merge new provenance into any entries already
   present on the target page.

The high-level :func:`compile_to_vault` wraps these steps with vault
I/O — fragment loading, target-page write, paradox-log append.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import frontmatter

from creek.audit import AuditLog
from creek.compile.provenance import ProvenanceEntry, merge_provenance
from creek.models import CompiledPage, CompileTargetKind, Fragment, PrivacyTier
from creek.vault.reader import iter_vault_fragments

logger = logging.getLogger(__name__)

CompileLLM = Callable[[str], str]
"""Signature for compile LLM clients: takes a prompt, returns JSON text."""

PARADOX_LOG_RELPATH = Path(
    "00-Creek-Meta/Processing-Log/paradoxes-during-compile.jsonl",
)
"""Side-channel log for LLM-detected paradoxes during compile."""

_TARGET_KINDS: tuple[str, ...] = ("thread", "eddy", "frequency_index")
_TARGET_DIRS: dict[str, Path] = {
    "thread": Path("02-Threads") / "Active",
    "eddy": Path("03-Eddies"),
    "frequency_index": Path("06-Frequencies"),
}
_CLAIM_EXCERPT_LEN: int = 80


@dataclass(frozen=True)
class ParadoxLogEntry:
    """One LLM-detected contradiction routed away from the synthesis page."""

    timestamp: datetime
    target_kind: str
    target_id: str
    description: str
    fragment_ids: list[str]


@dataclass(frozen=True)
class CompileResult:
    """In-memory output of a single :func:`compile_fragments` run."""

    page: CompiledPage
    paradoxes: list[ParadoxLogEntry]


def compile_fragments(
    fragments_with_bodies: Iterable[tuple[Fragment, str]],
    *,
    llm: CompileLLM,
    target_kind: CompileTargetKind,
    target_id: str,
    target_title: str,
    existing_provenance: list[ProvenanceEntry] | None = None,
    compiled_at: datetime | None = None,
) -> CompileResult:
    """Run one compile pass and return the compiled page plus any paradoxes.

    Args:
        fragments_with_bodies: Source fragments paired with their
            converted markdown bodies (the body is what the LLM sees,
            after privacy-tier filtering).
        llm: Injected callable returning a JSON payload with
            ``claims`` and ``paradoxes`` arrays.
        target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable ID of the synthesis target.
        target_title: Human-readable title for the rendered page.
        existing_provenance: Provenance already on the target page, used
            to enforce idempotent re-runs.
        compiled_at: Override the run timestamp; defaults to ``now(UTC)``.

    Returns:
        A :class:`CompileResult` carrying the synthesised
        :class:`CompiledPage` and a list of any paradoxes the LLM
        reported (empty when synthesis succeeded cleanly).

    Raises:
        ValueError: If *target_kind* is not one of the supported
            compiled-page surfaces.
    """
    if target_kind not in _TARGET_KINDS:
        msg = (
            f"Unknown target_kind {target_kind!r}; "
            f"supported: {', '.join(_TARGET_KINDS)}."
        )
        raise ValueError(msg)

    pairs = list(fragments_with_bodies)
    timestamp = compiled_at or datetime.now(tz=UTC)
    prompt = _build_prompt(pairs, target_kind=target_kind, target_title=target_title)
    raw = llm(prompt)
    payload = _parse_llm_payload(raw)

    new_provenance = [
        ProvenanceEntry(
            claim_id=str(claim["id"]),
            claim_excerpt=_excerpt(str(claim.get("text", ""))),
            fragment_ids=_str_list(claim.get("fragment_ids")),
            compiled_at=timestamp,
            compile_method="llm",
        )
        for claim in payload["claims"]
    ]
    provenance = merge_provenance(existing_provenance or [], new_provenance)
    body = _render_body(target_title, payload["claims"])

    page = CompiledPage(
        target_kind=target_kind,
        target_id=target_id,
        title=target_title,
        body=body,
        provenance=provenance,
        compiled_at=timestamp,
        compile_method="llm",
    )
    paradoxes = [
        ParadoxLogEntry(
            timestamp=timestamp,
            target_kind=target_kind,
            target_id=target_id,
            description=str(item.get("description", "")),
            fragment_ids=_str_list(item.get("fragment_ids")),
        )
        for item in payload["paradoxes"]
    ]
    return CompileResult(page=page, paradoxes=paradoxes)


def compile_to_vault(
    *,
    fragment_ids: list[str],
    vault_path: Path,
    target_kind: CompileTargetKind,
    target_id: str,
    target_title: str,
    llm: CompileLLM,
) -> Path:
    """Compile *fragment_ids* into a compiled-layer page on disk.

    Loads the named fragments from ``<vault>/01-Fragments``, runs
    :func:`compile_fragments`, persists the synthesis page to the
    ``target_kind``-specific directory, and appends any paradoxes to
    the side-channel JSONL log.

    Args:
        fragment_ids: IDs of source fragments to roll up.
        vault_path: Vault root.
        target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable ID of the synthesis target.
        target_title: Human-readable title for the page.
        llm: Compile LLM callable returning JSON.

    Returns:
        The path of the written compiled-layer page.

    Raises:
        ValueError: If a requested fragment cannot be found in the vault.
    """
    pairs = _load_fragments_for_compile(vault_path, fragment_ids)
    target_path = _resolve_target_path(vault_path, target_kind, target_id)
    existing = _load_existing_provenance(target_path)
    result = compile_fragments(
        pairs,
        llm=llm,
        target_kind=target_kind,
        target_id=target_id,
        target_title=target_title,
        existing_provenance=existing,
    )
    _write_compiled_page(target_path, result.page)
    if result.paradoxes:
        _append_paradox_log(vault_path, result.paradoxes)
    return target_path


# ---- Internals -----------------------------------------------------------


def _excerpt(text: str) -> str:
    """Return the first :data:`_CLAIM_EXCERPT_LEN` chars of *text*, single-lined."""
    flat = " ".join(text.split())
    return flat[:_CLAIM_EXCERPT_LEN]


def _str_list(raw: object) -> list[str]:
    """Coerce a JSON value into a ``list[str]``, dropping non-string items.

    The LLM payload reaches us as ``dict[str, object]`` because JSON
    types are unconstrained. This helper narrows the value safely so
    mypy strict-mode doesn't reject the comprehension at the call site.
    """
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _fragment_excerpt_for_prompt(fragment: Fragment, body: str) -> str:
    """Honour privacy tiers when handing fragment content to the LLM.

    ``intimate`` fragments contribute title-only — their bodies must
    never reach the LLM (FEAT-003 acceptance criterion). ``personal``
    is also reduced to a title-only summary, matching the default
    contract in :mod:`creek.classify.privacy_filter`.
    """
    title = fragment.title.strip() or fragment.id
    if fragment.privacy_tier == PrivacyTier.INTIMATE:
        return f"[Intimate-tier summary: {title}]"
    if fragment.privacy_tier == PrivacyTier.PERSONAL:
        return f"[Personal-tier summary: {title}]"
    return body


def _build_prompt(
    pairs: list[tuple[Fragment, str]],
    *,
    target_kind: str,
    target_title: str,
) -> str:
    """Assemble the compile prompt for the LLM."""
    sections: list[str] = [
        f"You are compiling a {target_kind} note titled {target_title!r}.",
        "Return JSON with two arrays: 'claims' (each having 'id', 'text', "
        "'fragment_ids') and 'paradoxes' (each having 'description', "
        "'fragment_ids'). Never flatten contradictions into a claim.",
        "",
        "Source fragments:",
    ]
    for fragment, body in pairs:
        excerpt = _fragment_excerpt_for_prompt(fragment, body)
        sections.append(f"- id: {fragment.id}")
        sections.append(f"  title: {fragment.title}")
        sections.append(f"  body: {excerpt}")
    return "\n".join(sections)


def _parse_llm_payload(raw: str) -> dict[str, list[dict[str, object]]]:
    """Parse the LLM response into the canonical ``{claims, paradoxes}`` dict."""
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "Compile LLM returned non-JSON output."
        raise ValueError(msg) from exc
    if not isinstance(decoded, dict):
        msg = "Compile LLM payload must be a JSON object."
        raise ValueError(msg)
    claims = decoded.get("claims") or []
    paradoxes = decoded.get("paradoxes") or []
    if not isinstance(claims, list) or not isinstance(paradoxes, list):
        msg = "Compile LLM payload claims/paradoxes must be arrays."
        raise ValueError(msg)
    return {"claims": list(claims), "paradoxes": list(paradoxes)}


def _render_body(title: str, claims: list[dict[str, object]]) -> str:
    """Render the synthesis body with per-claim provenance footnotes."""
    lines: list[str] = [f"# {title}", ""]
    for claim in claims:
        text = str(claim.get("text", "")).strip()
        claim_id = str(claim.get("id", ""))
        if not text:
            continue
        lines.append(f"{text} [^{claim_id}]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _load_fragments_for_compile(
    vault_path: Path,
    fragment_ids: list[str],
) -> list[tuple[Fragment, str]]:
    """Load the requested fragments from the vault and preserve order."""
    requested = list(fragment_ids)
    by_id: dict[str, tuple[Fragment, str]] = {}
    for _path, fragment, body, _raw in iter_vault_fragments(
        vault_path / "01-Fragments",
    ):
        if fragment.id in requested:
            by_id[fragment.id] = (fragment, body)
    missing = [fid for fid in requested if fid not in by_id]
    if missing:
        msg = f"Fragment(s) not found in vault: {', '.join(missing)}"
        raise ValueError(msg)
    return [by_id[fid] for fid in requested]


def _resolve_target_path(
    vault_path: Path,
    target_kind: CompileTargetKind,
    target_id: str,
) -> Path:
    """Resolve the on-disk path for a compiled-layer target."""
    subdir = _TARGET_DIRS[target_kind]
    target_dir = vault_path / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{target_id}.md"


def _load_existing_provenance(target_path: Path) -> list[ProvenanceEntry]:
    """Read provenance entries from the target page's frontmatter, if any."""
    if not target_path.exists():
        return []
    try:
        post = frontmatter.load(str(target_path))
    except (OSError, ValueError):
        return []
    raw = post.metadata.get("provenance") or []
    if not isinstance(raw, list):
        return []
    return [
        ProvenanceEntry.model_validate(item) for item in raw if isinstance(item, dict)
    ]


def _write_compiled_page(target_path: Path, page: CompiledPage) -> None:
    """Serialise *page* to *target_path* with YAML frontmatter."""
    metadata = page.model_dump(mode="json", exclude={"body"})
    post = frontmatter.Post(content=page.body, **metadata)
    target_path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _append_paradox_log(vault_path: Path, entries: list[ParadoxLogEntry]) -> None:
    """Append paradox entries to the side-channel JSONL log."""
    log_path = vault_path / PARADOX_LOG_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = AuditLog(log_path)
    for entry in entries:
        record = asdict(entry)
        record["timestamp"] = entry.timestamp.isoformat()
        log.append(record)


def _default_llm(_config: object) -> CompileLLM:
    """Return the default cloud LLM client for the CLI entry point.

    Tests monkeypatch this hook to inject a deterministic stub. The
    production path constructs an Anthropic client lazily so the import
    cost (and the API-key check) stays out of the unit-test surface.
    """
    from creek.classify.llm import AnthropicProvider

    provider = AnthropicProvider(_config)  # type: ignore[arg-type]
    return provider.call


CompileMethodLiteral = Literal["rules", "llm", "manual"]
"""Re-export for callers that need to type-annotate compile_method values."""
