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
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from creek._fsio import atomic_write_text
from creek._schema import coerce_optional_list
from creek.audit import AuditLog
from creek.care.guardrail import CARE_POLICY
from creek.classify.privacy_filter import (
    AncestorIndex,
    build_ancestor_index,
    fragment_tier,
    max_source_tier,
)
from creek.compile.provenance import ProvenanceEntry, merge_provenance
from creek.hierarchy import (
    LevelPolicy,
    select_by_policy,
    source_levels,
    structural_path_context,
)
from creek.models import CompiledPage, CompileTargetKind, Fragment, PrivacyTier
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS, iter_vault_fragments

if TYPE_CHECKING:
    from creek.config import LLMConfig

logger = logging.getLogger(__name__)

CompileLLM = Callable[[str], str]
"""Signature for compile LLM clients: takes a prompt, returns JSON text."""

CompileLLMFactory = Callable[[PrivacyTier], CompileLLM]
"""Tier-keyed builder for a :data:`CompileLLM`.

:func:`compile_to_vault` takes one of these rather than a ready-built
client, and the key is the point: only the engine has seen the fragments,
so only the engine can say how sensitive the call is. Its callers hold an
id list and nothing more. A production factory closes over
:class:`creek.classify.llm.router.ModelRouter` and resolves the
``generation`` stage *with* the tier, which is what puts every compile
through the ``Intimate``-never-cloud chokepoint (#647/#962); handed a
pre-built client instead, the caller resolves with no tier and
``ModelRouter._enforce_local_for_intimate`` degrades to a no-op — the
invariant is then documented and unenforced, which is precisely the bug
#962 fixes.

The engine deliberately knows nothing about ``ModelRouter``, tier
ceilings, or the MCP layer: it derives a :class:`~creek.models.PrivacyTier`
and hands it over. Whatever the factory decides — including raising
:class:`~creek.classify.llm.router.IntimateRoutingError` when there is no
local backend to fall back to — propagates to the caller untouched.
"""

PARADOX_LOG_RELPATH = Path(
    "00-Creek-Meta/Processing-Log/paradoxes-during-compile.jsonl",
)
"""Side-channel log for LLM-detected paradoxes during compile."""

TARGET_KINDS: tuple[str, ...] = ("thread", "eddy", "frequency_index")
"""Public list of compiled-page surfaces.

Re-used by :mod:`creek.cli` for ``--target-kind`` validation so a new
kind added here automatically tightens the CLI gate too — no parallel
constant to drift.
"""

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
    level_policy: LevelPolicy = "leaves",
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
            to enforce idempotent re-runs. Only the entries this run
            reproduces are carried forward — see :func:`_build_provenance`.
        compiled_at: Override the run timestamp; defaults to ``now(UTC)``.
        level_policy: FEAT-025 level policy applied to the input pairs.
            ``"leaves"`` (default) keeps the most-atomic representation
            and surfaces parents as ``structural_path`` context. ``"all"``
            reproduces the pre-FEAT-025 path. ``"documents"`` keeps only
            document- and session-level rows.

    Returns:
        A :class:`CompileResult` carrying the synthesised
        :class:`CompiledPage` and a list of any paradoxes the LLM
        reported (empty when synthesis succeeded cleanly).

    Raises:
        ValueError: If *target_kind* is not one of the supported
            compiled-page surfaces, or if the LLM payload's ``claims``
            or ``paradoxes`` are not arrays of JSON objects.
    """
    _validate_target_kind(target_kind)
    pairs = list(fragments_with_bodies)
    by_id: dict[str, Fragment] = {fragment.id: fragment for fragment, _ in pairs}
    selected_fragments, selected_pairs = _apply_level_policy(pairs, level_policy)

    timestamp = compiled_at or datetime.now(tz=UTC)
    prompt = _build_prompt(
        selected_pairs,
        target_kind=target_kind,
        target_title=target_title,
        by_id=by_id,
    )
    payload = _parse_llm_payload(llm(prompt))
    valid_claims = _filter_valid_claims(payload["claims"])
    provenance = _build_provenance(
        existing_provenance or [],
        valid_claims,
        timestamp,
    )
    page = CompiledPage(
        target_kind=target_kind,
        target_id=target_id,
        title=target_title,
        body=_render_body(target_title, valid_claims),
        provenance=provenance,
        compiled_at=timestamp,
        compile_method="llm",
        level_policy=level_policy,
        source_levels=source_levels(selected_fragments),
    )
    paradoxes = _payload_to_paradox_entries(
        payload["paradoxes"],
        target_kind=target_kind,
        target_id=target_id,
        timestamp=timestamp,
    )
    return CompileResult(page=page, paradoxes=paradoxes)


def _validate_target_kind(target_kind: str) -> None:
    """Reject any *target_kind* outside the published surface list."""
    if target_kind not in TARGET_KINDS:
        msg = (
            f"Unknown target_kind {target_kind!r}; "
            f"supported: {', '.join(TARGET_KINDS)}."
        )
        raise ValueError(msg)


def _apply_level_policy(
    pairs: list[tuple[Fragment, str]],
    policy: LevelPolicy,
) -> tuple[list[Fragment], list[tuple[Fragment, str]]]:
    """Filter (fragment, body) pairs by *policy* and return the surviving subset.

    Returns the selected fragments (for ``source_levels`` recording)
    alongside the matching (fragment, body) pairs so the prompt builder
    keeps body parity with the policy's choice of leaves.
    """
    fragments_only = [fragment for fragment, _ in pairs]
    selected_fragments = select_by_policy(fragments_only, policy)
    selected_ids = {fragment.id for fragment in selected_fragments}
    selected_pairs = [pair for pair in pairs if pair[0].id in selected_ids]
    return selected_fragments, selected_pairs


def _filter_valid_claims(
    claims: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop malformed claims (missing/empty id) before they reach the page body.

    Otherwise ``_render_body`` would emit a broken Markdown footnote
    ``[^]`` that downstream readers can't resolve.
    """
    return [c for c in claims if str(c.get("id", "")).strip()]


def _claims_to_provenance(
    claims: list[dict[str, object]],
    timestamp: datetime,
) -> list[ProvenanceEntry]:
    """Build per-claim provenance entries for an LLM compile run."""
    return [
        ProvenanceEntry(
            claim_id=str(claim["id"]),
            claim_excerpt=_excerpt(str(claim.get("text", ""))),
            fragment_ids=_str_list(claim.get("fragment_ids")),
            compiled_at=timestamp,
            compile_method="llm",
        )
        for claim in claims
    ]


def _build_provenance(
    existing: list[ProvenanceEntry],
    claims: list[dict[str, object]],
    timestamp: datetime,
) -> list[ProvenanceEntry]:
    """Merge *existing* provenance into this run's, dropping orphaned entries.

    Provenance may never outlive the body it describes. Compile
    regenerates the whole body from the claims it was just handed, so an
    existing entry this run did not re-emit records sources for a claim
    the new body no longer contains — the state that let an empty
    compile publish a title-only page still asserting the previous run's
    fragments. Entries for claims this run *did* make keep merging
    unchanged, so idempotent re-runs still accumulate ``fragment_ids``.

    Args:
        existing: Provenance already on the target page.
        claims: The current run's claims, post-:func:`_filter_valid_claims`.
        timestamp: The current run's compile timestamp.

    Returns:
        Merged entries, one per claim in *claims*, each stamped with
        *timestamp*.
    """
    current = _claims_to_provenance(claims, timestamp)
    claim_ids = {entry.claim_id for entry in current}
    retained = [entry for entry in existing if entry.claim_id in claim_ids]
    return merge_provenance(retained, current)


def _payload_to_paradox_entries(
    items: list[dict[str, object]],
    *,
    target_kind: str,
    target_id: str,
    timestamp: datetime,
) -> list[ParadoxLogEntry]:
    """Map LLM-reported paradox dicts into structured log entries."""
    return [
        ParadoxLogEntry(
            timestamp=timestamp,
            target_kind=target_kind,
            target_id=target_id,
            description=str(item.get("description", "")),
            fragment_ids=_str_list(item.get("fragment_ids")),
        )
        for item in items
    ]


def _routing_tier_for(
    loaded: list[tuple[Fragment, str, dict[str, object]]],
    ancestry: list[PrivacyTier],
) -> PrivacyTier:
    """Return the tier this compile's LLM call must be keyed with.

    The reduction spans **every loaded fragment**, and is taken *before*
    any level policy is applied. That ordering is the trap a future
    "optimisation" would fall into: :func:`compile_fragments` runs
    :func:`_apply_level_policy`, which under the default ``"leaves"``
    drops a parent whose child is also in the set — but the parent is not
    thereby absent from the prompt. :func:`_build_prompt` passes a
    ``by_id`` lookup spanning *all* pairs to
    :func:`creek.hierarchy.structural_path_context`, which walks
    ``parent_id`` and emits the dropped parents' **titles** as the
    surviving child's ``structural_path:`` breadcrumb. Reducing over the
    policy-selected subset would therefore under-route: an intimate
    parent's title would egress on a call keyed by its open child.

    **The *ancestry* term closes the other half of the same channel
    (#931).** The walk above only reaches ancestors the caller *named*, so
    it never saw the branch that fires when they did not:
    ``structural_path_context`` prefers the leaf's own **persisted**
    ``structural_path``, a list of ancestor headings the splitter baked in
    at ingest, and that branch needs no ancestor fragment in memory at all.
    Name one ``open`` child of an ``intimate`` document and pre-#931 the
    call routed ``OPEN`` — a cloud client built with ``structural_path:
    <the intimate document's heading>`` sitting in the prompt. Folding
    :meth:`creek.classify.privacy_filter.AncestorIndex.chain_tiers` into the
    reduction escalates the routing tier instead of redacting the
    breadcrumb: the ``creek compile`` operator owns the vault, so the
    orienting value of the ancestry is theirs to keep — it just may not
    leave the machine. (The MCP wrapper makes the opposite and equally
    correct choice for *its* caller, refusing the whole call; see
    ``creek_mcp.tools.compile``.)

    **Two accepted costs of ranking by walking ``parent_id``.** First it
    *over-covers*: it ranks the root document, whose title the persisted
    breadcrumb never carries (the splitter appends only headings). An
    intimate root therefore forces its descendants' compiles local even
    though the prompt shows only section headings — accepted, because the
    fallback branch of ``structural_path_context`` *does* render the root's
    title, and the two branches must be gated the same way. Second, an
    ancestry link that cannot be surveyed — dangling (``creek
    ingest``'s resplit unlinks a merged parent), cyclic, or a breadcrumb
    deeper than the walk can reach — ranks ``INTIMATE``, so a whole subtree
    becomes uncompilable at cloud tiers and the operator sees only
    ``IntimateRoutingError``. That is the right privacy answer and a poor
    diagnostic; ``creek lint`` surfacing those states is #1277.

    Sensitivity is read fragment-by-fragment through
    :func:`creek.classify.privacy_filter.fragment_tier`, which is why the
    loader threads each file's *raw* frontmatter this far. A fragment with
    no ``privacy_tier`` key at all is indistinguishable from an explicit
    ``unclassified`` once :class:`~creek.models.Fragment` has applied its
    default, and the two must route differently: nobody has vouched for
    the former, so it fails closed to ``INTIMATE``.

    Args:
        loaded: Every ``(fragment, body, raw)`` triple the compile loaded,
            pre-policy and in requested order.
        ancestry: The tiers of the requested fragments' ancestors, from
            :meth:`creek.classify.privacy_filter.AncestorIndex.chain_tiers`.
            Empty when nothing requested resolved — which cannot narrow the
            result, because the reduction below then has nothing to reduce
            and fails closed anyway.

    Returns:
        The most sensitive tier among *loaded* and *ancestry*, or
        :attr:`~creek.models.PrivacyTier.INTIMATE` when both are empty
        — an empty compile carries no evidence about what it would send,
        and :func:`creek.classify.privacy_filter.max_source_tier` owns
        that fail-closed default so every caller of it agrees.
    """
    return max_source_tier(
        [*(fragment_tier(f, raw) for f, _body, raw in loaded), *ancestry],
    )


def compile_to_vault(
    *,
    fragment_ids: list[str],
    vault_path: Path,
    target_kind: CompileTargetKind,
    target_id: str,
    target_title: str,
    llm_factory: CompileLLMFactory,
    level_policy: LevelPolicy = "leaves",
) -> Path:
    """Compile *fragment_ids* into a compiled-layer page on disk.

    Loads the named fragments from ``<vault>/01-Fragments``, builds the
    LLM client for their combined privacy tier, runs
    :func:`compile_fragments`, persists the synthesis page to the
    ``target_kind``-specific directory, and appends any paradoxes to
    the side-channel JSONL log.

    The step order is load-bearing, not incidental. The factory is invoked
    **after** the load — so an unresolvable id gets the ``ValueError``
    below rather than whatever the factory would have raised — and
    **before** :func:`_resolve_target_path`, which unconditionally creates
    the target directory. A compile refused on privacy grounds must leave
    zero state on disk, not an empty ``02-Threads/Active``.

    Args:
        fragment_ids: IDs of source fragments to roll up.
        vault_path: Vault root.
        target_kind: ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable ID of the synthesis target.
        target_title: Human-readable title for the page.
        llm_factory: Tier-keyed builder for the compile LLM client. It is
            called exactly once, with the tier :func:`_routing_tier_for`
            derives from every loaded fragment — the engine is the only
            layer that has seen them, so it is the only layer that can
            supply it. See :data:`CompileLLMFactory`.
        level_policy: FEAT-025 level policy applied to the input
            fragments. Passed through to :func:`compile_fragments`;
            defaults to ``"leaves"``. It deliberately does not narrow the
            routing tier; see :func:`_routing_tier_for`.

    Returns:
        The path of the written compiled-layer page.

    Raises:
        ValueError: If a requested fragment cannot be found in the vault,
            or if *target_id* escapes the compiled-layer directory.
        creek.classify.llm.router.IntimateRoutingError: Raised by the
            production *llm_factory* when the sources route ``INTIMATE``
            and no local backend exists to fall back to. It subclasses
            ``RuntimeError``, so ``creek_mcp.tools.compile``'s existing
            ``except (ValueError, RuntimeError)`` keeps converting it into
            a structured refusal; the CLI catches it by name so a privacy
            refusal reads as a refusal rather than a traceback. Anything
            else *llm_factory* raises — an unreachable provider, a missing
            credential — propagates untouched for the same reason: the
            engine takes no view on how a client is built.
    """
    pairs_with_raw, ancestors = _load_fragments_for_compile(vault_path, fragment_ids)
    tier = _routing_tier_for(pairs_with_raw, ancestors.chain_tiers(fragment_ids))
    llm = llm_factory(tier)
    target_path = _resolve_target_path(vault_path, target_kind, target_id)
    existing = _load_existing_provenance(target_path)
    result = compile_fragments(
        [(fragment, body) for fragment, body, _raw in pairs_with_raw],
        llm=llm,
        target_kind=target_kind,
        target_id=target_id,
        target_title=target_title,
        existing_provenance=existing,
        level_policy=level_policy,
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
    by_id: dict[str, Fragment] | None = None,
) -> str:
    """Assemble the compile prompt for the LLM.

    Surfaces each fragment's ``structural_path`` breadcrumb (FEAT-025)
    as a separate field so the LLM treats parent context as orientation
    rather than duplicate claim material.
    """
    lookup: dict[str, Fragment] = by_id or {f.id: f for f, _ in pairs}
    sections: list[str] = [
        CARE_POLICY,
        f"You are compiling a {target_kind} note titled {target_title!r}.",
        "Return JSON with two arrays: 'claims' (each having 'id', 'text', "
        "'fragment_ids') and 'paradoxes' (each having 'description', "
        "'fragment_ids'). Never flatten contradictions into a claim.",
        "Respond with raw JSON only. No prose, no preamble, no Markdown code fences.",
        "",
        "Source fragments:",
    ]
    for fragment, body in pairs:
        excerpt = _fragment_excerpt_for_prompt(fragment, body)
        breadcrumb = structural_path_context(fragment, lookup)
        sections.extend(
            (
                f"- id: {fragment.id}",
                f"  title: {fragment.title}",
            )
        )
        if breadcrumb:
            sections.append(f"  structural_path: {' > '.join(breadcrumb)}")
        sections.append(f"  body: {excerpt}")
    return "\n".join(sections)


_JSON_FENCE_RE = re.compile(
    r"```(?:json|JSON)?[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```",
    re.DOTALL,
)
"""First fenced code block in a Markdown-ish response (group 1 = inner text).

Tolerates an optional ``json`` language tag and surrounding whitespace.
Non-greedy so it stops at the first closing fence even when the LLM
emits multiple blocks.
"""


def _strip_fenced_block(raw: str) -> str | None:
    """Return the first fenced code block's inner text, or ``None`` if absent."""
    match = _JSON_FENCE_RE.search(raw)
    if match is None:
        return None
    return match.group(1).strip()


def _extract_first_json_object(raw: str) -> str | None:
    """Return the substring of the first balanced JSON object in *raw*.

    Uses :class:`json.JSONDecoder.raw_decode` from the first ``{``, so
    nested braces and braces inside JSON strings don't confuse the
    scan. Returns ``None`` if no opener is present or if the decoder
    cannot consume a complete object from that point.
    """
    start = raw.find("{")
    if start == -1:
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(raw, start)
    except json.JSONDecodeError:
        return None
    return raw[start:end]


def _json_candidates(raw: str) -> list[str]:
    """Build an ordered list of JSON decoding candidates from *raw*.

    The raw response comes first (the happy path: a well-behaved LLM
    returned bare JSON), then a fenced-code-block strip, then a
    first-balanced-object extraction. Duplicates are skipped so callers
    don't pay for parsing the same string twice.
    """
    candidates = [raw]
    fenced = _strip_fenced_block(raw)
    if fenced is not None and fenced not in candidates:
        candidates.append(fenced)
    extracted = _extract_first_json_object(raw)
    if extracted is not None and extracted not in candidates:
        candidates.append(extracted)
    return candidates


def _decode_json_tolerant(raw: str) -> object:
    """Decode JSON from an LLM response, tolerating fences and preambles.

    Claude 4.x routinely wraps structured responses in ``` ```json … ```
    `` ` fences or prefaces them with a short preamble (INC-007). The
    strict :func:`json.loads` path rejects both; this helper retries
    against successively more aggressive cleanups before giving up.
    """
    last_error: json.JSONDecodeError | None = None
    for candidate in _json_candidates(raw):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    msg = "Compile LLM returned non-JSON output."
    raise ValueError(msg) from last_error


def _parse_llm_payload(raw: str) -> dict[str, list[dict[str, object]]]:
    """Parse the LLM response into the canonical ``{claims, paradoxes}`` dict."""
    decoded = _decode_json_tolerant(raw)
    if not isinstance(decoded, dict):
        msg = "Compile LLM payload must be a JSON object."
        raise ValueError(msg)  # noqa: TRY004  # ValueError unifies all LLM-payload schema errors with the JSONDecodeError branch above.
    claims = _require_array(decoded.get("claims"))
    paradoxes = _require_array(decoded.get("paradoxes"))
    return {
        "claims": _require_object_array(claims),
        "paradoxes": _require_object_array(paradoxes),
    }


def _require_array(value: object) -> list[object]:
    """Return *value* as a list, tolerating only an absent key or ``null``.

    Args:
        value: The raw ``claims`` / ``paradoxes`` value from the payload.

    Returns:
        The list *value* denotes; ``[]`` when it is absent or ``null``.

    Raises:
        ValueError: When *value* is any other non-list — the falsy ones
            (``0``, ``""``, ``false``, ``{}``) included, which the old
            ``or []`` coerced away before the guard could see them.
    """
    items = coerce_optional_list(value)
    if items is None:
        msg = "Compile LLM payload claims/paradoxes must be arrays."
        raise ValueError(msg)
    return items


def _require_object_array(items: list[object]) -> list[dict[str, object]]:
    """Return *items* once every element is a JSON object.

    Rejects the array whole rather than dropping the bad elements: a
    salvaged payload writes a hollowed-out body over a good page, which
    is worse than a failure the operator can retry.

    Args:
        items: An already-validated list of raw payload elements.

    Returns:
        The same elements, typed as JSON objects.

    Raises:
        ValueError: When any element is not a JSON object.
    """
    objects: list[dict[str, object]] = [
        item for item in items if isinstance(item, dict)
    ]
    if len(objects) != len(items):
        msg = "Compile LLM payload claims/paradoxes must contain JSON objects."
        raise ValueError(msg)
    return objects


def _render_body(title: str, claims: list[dict[str, object]]) -> str:
    """Render the synthesis body with per-claim provenance footnotes."""
    lines: list[str] = [f"# {title}", ""]
    for claim in claims:
        text = str(claim.get("text", "")).strip()
        claim_id = str(claim.get("id", ""))
        if not text:
            continue
        lines.extend((f"{text} [^{claim_id}]", ""))
    return "\n".join(lines).rstrip() + "\n"


def _load_fragments_for_compile(
    vault_path: Path,
    fragment_ids: list[str],
) -> tuple[list[tuple[Fragment, str, dict[str, object]]], AncestorIndex]:
    """Load the requested fragments from the vault and preserve order.

    Keeps the ``raw`` frontmatter :func:`creek.vault.reader.iter_vault_fragments`
    already yields instead of discarding it. Within the compile engine this
    is the only place a *missing* ``privacy_tier`` key is still
    distinguishable from an explicit ``unclassified`` —
    :class:`~creek.models.Fragment` has defaulted the two together by the
    time the model is built — and :func:`_routing_tier_for` needs that
    distinction to fail closed. Scoped to this engine deliberately:
    :func:`creek_mcp.tools.reflect._fragment_tier` reaches the same
    distinction independently, off its own raw ``post.metadata`` read, and
    that duplication is pre-existing and unresolved (#847). The two must
    keep agreeing; nothing but review currently makes them.

    It also returns the whole-corpus
    :class:`~creek.classify.privacy_filter.AncestorIndex` (#931), built from
    the records of *this* walk rather than fetched by a second one. The
    ancestors the prompt's ``structural_path:`` breadcrumb renders are
    usually not among the requested ids, so ranking them needs the corpus —
    but at the 35k-fragment bar a second ``rglob``-plus-parse pass doubles
    the pre-LLM wall clock, and a privacy fix has no business shipping that
    regression. ``tests/test_compile.py``'s
    ``test_compile_to_vault_walks_the_vault_exactly_once`` pins the single
    walk so a future lane cannot quietly reintroduce the second one.

    Args:
        vault_path: Vault root; fragments are read from ``01-Fragments``.
        fragment_ids: The ids to load, in the order the caller wants them.

    Returns:
        A ``(triples, ancestor_index)`` pair: one ``(fragment, body, raw)``
        triple per requested id in requested order, and the ancestry index
        spanning every fragment in the corpus.

    Raises:
        ValueError: If any requested id has no matching fragment.
    """
    requested = fragment_ids.copy()
    records = iter_vault_fragments(vault_path / "01-Fragments")
    by_id: dict[str, tuple[Fragment, str, dict[str, object]]] = {
        fragment.id: (fragment, body, raw)
        for _path, fragment, body, raw in records
        if fragment.id in requested
    }
    ancestors = build_ancestor_index(
        (fragment, raw) for _path, fragment, _body, raw in records
    )
    missing = [fid for fid in requested if fid not in by_id]
    if missing:
        msg = f"Fragment(s) not found in vault: {', '.join(missing)}"
        raise ValueError(msg)
    return [by_id[fid] for fid in requested], ancestors


def _resolve_target_path(
    vault_path: Path,
    target_kind: CompileTargetKind,
    target_id: str,
) -> Path:
    """Resolve the on-disk path for a compiled-layer target.

    Raises:
        ValueError: If *target_id* resolves to a path outside the
            compiled-layer subdirectory (e.g. ``"../escape"``). The
            CLI passes ``target_id`` straight through from operator
            input, so a one-line guard here prevents accidental
            traversal regardless of upstream validation.
    """
    subdir = _TARGET_DIRS[target_kind]
    target_dir = vault_path / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = (target_dir / f"{target_id}.md").resolve()
    if candidate.parent != target_dir.resolve():
        msg = f"target_id {target_id!r} escapes the compiled-layer directory"
        raise ValueError(msg)
    return candidate


def default_llm(config: LLMConfig) -> CompileLLM:
    """Return the generation LLM client for the CLI entry point.

    Tests monkeypatch this hook to inject a deterministic stub. The production
    path builds the provider lazily via the factory (#646) so the generation
    stage honors the configured provider (Anthropic, Ollama, …) rather than
    being hard-wired to Anthropic; the import cost (and any API-key check) stays
    out of the unit-test surface.
    """
    from creek.classify.llm import build_provider

    provider = build_provider(config)

    def _complete(prompt: str) -> str:
        # ``complete`` is the provider-neutral protocol method (every provider
        # implements it); ``.call`` is Anthropic-only, so routing to Ollama/etc.
        # must go through ``complete`` to stay backend-agnostic (#646).
        return provider.complete(prompt).text

    return _complete


def _load_existing_provenance(target_path: Path) -> list[ProvenanceEntry]:
    """Read provenance entries from the target page's frontmatter, if any."""
    if not target_path.exists():
        return []
    try:
        post = frontmatter.load(str(target_path))
    except FRONTMATTER_LOAD_ERRORS:
        return []
    raw = post.metadata.get("provenance") or []
    if not isinstance(raw, list):
        return []
    return [
        ProvenanceEntry.model_validate(item) for item in raw if isinstance(item, dict)
    ]


def _write_compiled_page(target_path: Path, page: CompiledPage) -> None:
    """Serialise *page* to *target_path* with YAML frontmatter.

    Commits through :func:`creek._fsio.atomic_write_text` rather than
    ``Path.write_text``, which truncates the target before it writes: an
    interrupted re-compile would otherwise destroy the previously
    compiled page and leave a stub under its name. The staged commit
    also narrows the page to ``mkstemp``'s owner-only 0o600, matching
    what :mod:`creek.vault.writer` already produces for vault notes.
    """
    metadata = page.model_dump(mode="json", exclude={"body"})
    post = frontmatter.Post(content=page.body, **metadata)
    atomic_write_text(target_path, frontmatter.dumps(post))


def _append_paradox_log(vault_path: Path, entries: list[ParadoxLogEntry]) -> None:
    """Append paradox entries to the side-channel JSONL log."""
    log_path = vault_path / PARADOX_LOG_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = AuditLog(log_path)
    for entry in entries:
        record = asdict(entry)
        record["timestamp"] = entry.timestamp.isoformat()
        log.append(record)
