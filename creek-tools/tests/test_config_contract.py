"""Contract: every ``CreekConfig`` field is consumed, or declared dormant.

A config field can be defined, defaulted, documented, serialised into every
user's vault by ``creek init`` — and never read by a single production code
path. No other gate in this repo notices: ``ruff``, ``mypy --strict``,
``radon``, ``bandit`` and ``interrogate`` are all perfectly happy with a knob
that is wired to nothing. This module is that missing gate (issue #1042).

The field inventory is **derived from the live Pydantic model**, never from a
retyped literal list — a hand-copied list would drift the moment somebody adds
a field, which is the exact defect this contract exists to prevent.

How "read" is decided
---------------------
Every production module under ``creek/`` and ``creek_mcp/`` is parsed with
:mod:`ast` and attribute chains are resolved against the config model tree:

* a parameter, attribute or variable annotated with a config model class roots
  a chain (``def f(cfg: CompostConfig)`` → ``cfg.threshold`` reads
  ``compost.threshold``);
* ``config = load_config(...)`` and ``CreekConfig()`` root a chain at the top
  of the tree;
* a plain assignment from a resolved chain carries the binding along
  (``block = config.compost`` → ``block.threshold``), including assignment onto
  ``self`` inside a class body;
* a function annotated ``-> CreekConfig`` roots a chain at its call sites, so
  reads behind a helper such as ``_load_config_for_vault`` still count;
* a structural ``Protocol`` (``SegmentationConfig``, ``SplitPolicySource``)
  whose members are covered by exactly one config model is treated as that
  model, recovering the reads in modules that deliberately avoid importing the
  settings tree;
* a class that stores a config block on an attribute (``self.config: LLMConfig``)
  is a *carrier*, so ``classifier.config.max_concurrent`` at a distant call site
  resolves;
* ``getattr(config.sources, name)`` with a non-literal name marks the whole
  sub-tree read, because a dynamic lookup really can reach any of its fields.

``creek/config.py`` itself is excluded: it is where the fields are *defined*,
so counting it would let a field's own declaration vouch for it.

Two deliberate over-approximations keep the gate from crying wolf, at the cost
of letting a genuinely dead field hide: a read is attributed to *every* path at
which its model occurs (``LLMConfig.model`` read anywhere marks ``llm.default``
and ``llm.classification`` alike), and a dynamic ``getattr`` marks a whole
sub-tree. A false alarm blocks CI on working code; a miss only leaves a knob
undetected for another pass — so the analysis is tuned to fail quiet.

Adding a config field
---------------------
Consume it in production, or add it to :data:`DORMANT_CONFIG_FIELDS` with a
reason that cites the issue tracking the wire-in. The allowlist is a ratchet:
:func:`test_dormant_allowlist_has_no_stale_entries` fails the moment an
allowlisted field *is* read, so the list can only shrink.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Final, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from creek.config import CreekConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# --------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------

_CLEANING: Final[str] = (
    "declared dormant: the cleaning block reaches no production path; "
    "wire-in tracked by #1041"
)

_AGGREGATION: Final[str] = (
    "declared dormant: creek/atomize/aggregate.py segments on its own "
    "AggregationConfig defaults, so this vault knob never reaches it; "
    "tracked by #1041"
)

_LLM_BATCH_SIZE: Final[str] = (
    "declared dormant: no LLM call site reads LLMConfig.batch_size — the only "
    "batch_size read is EmbeddingsConfig's; tracked by #1041"
)

DORMANT_CONFIG_FIELDS: Final[dict[str, str]] = {
    "timezone": (
        "declared dormant: no production code normalises timestamps through "
        "it yet; wire-in tracked by #1041"
    ),
    "ocr.enabled": "declared dormant: OCR wire-in tracked by #1041",
    "ocr.engine": "declared dormant: OCR wire-in tracked by #1041",
    "ocr.languages": "declared dormant: OCR wire-in tracked by #1041",
    "ocr.min_confidence": "declared dormant: OCR wire-in tracked by #1041",
    "linking.cross_source_aggregation": (
        "declared dormant: cross-source aggregation wire-in tracked by #1041"
    ),
    "linking.burst_similarity_threshold": _AGGREGATION,
    "linking.exchange_max_gap_minutes": _AGGREGATION,
    "linking.session_max_gap_minutes": _AGGREGATION,
    "llm.default.batch_size": _LLM_BATCH_SIZE,
    "llm.classification.batch_size": _LLM_BATCH_SIZE,
    "llm.generation.batch_size": _LLM_BATCH_SIZE,
    "llm.frontend.batch_size": _LLM_BATCH_SIZE,
    "classification.auto_classify_sources": (
        "declared dormant: no classify path consults it; tracked by #1041"
    ),
    "context.mode": "declared dormant: context handling wire-in tracked by #1041",
    "context.quality_penalty": (
        "declared dormant: context handling wire-in tracked by #1041"
    ),
    "cleaning.discord.filter_bot_messages": _CLEANING,
    "cleaning.discord.strip_emoji": _CLEANING,
    "cleaning.discord.filter_commands": _CLEANING,
    "cleaning.discord.min_message_length": _CLEANING,
    "cleaning.chatbot.filter_system_prompts": _CLEANING,
    "cleaning.chatbot.filter_tool_outputs": _CLEANING,
    "cleaning.chatbot.filter_regenerations": _CLEANING,
    "cleaning.chatbot.min_human_turn_length": _CLEANING,
    "cleaning.chatbot.code_block_threshold": _CLEANING,
    "cleaning.chatbot.max_abandoned_turns": _CLEANING,
    "cleaning.markdown.skip_empty_files": _CLEANING,
    "cleaning.markdown.min_body_length": _CLEANING,
    "cleaning.google_drive.deduplicate": _CLEANING,
    "cleaning.google_drive.filter_empty_docs": _CLEANING,
    "cleaning.google_drive.max_collaboration_ratio": _CLEANING,
    "cleaning.validation.min_characters": _CLEANING,
    "cleaning.validation.min_words": _CLEANING,
    "cleaning.validation.max_stop_word_ratio": _CLEANING,
    "cleaning.validation.require_metadata": _CLEANING,
    "cleaning.quality.accept_threshold": _CLEANING,
    "cleaning.quality.skip_threshold": _CLEANING,
    "cleaning.deduplication.strategy": _CLEANING,
    "cleaning.deduplication.similarity_threshold": _CLEANING,
    "cleaning.hygiene.track_orphans": _CLEANING,
    "cleaning.hygiene.staleness_days": _CLEANING,
    "ai_style.citation_network_checks": (
        "declared dormant: citation-network checking is unimplemented; tracked by #1041"
    ),
    "ai_style.category_weights": (
        "declared dormant: the scorer uses its own weights; tracked by #1041"
    ),
    "ai_style.feature_weights": (
        "declared dormant: the scorer uses its own weights; tracked by #1041"
    ),
    "author.synthesis_model": "reserved: #642 per-stage routing supersedes it",
    "author.reflection_model": "reserved: #642 per-stage routing supersedes it",
}
"""Config fields no production path reads, each with the issue tracking it.

Every entry must cite an issue number so the failure message tells the next
engineer where the decision lives. Entries are removed — never edited to say
"still dormant" — when the field is finally consumed.
"""

_ISSUE_REFERENCE: Final[re.Pattern[str]] = re.compile(r"#\d+")

_CREEK_TOOLS_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS: Final[tuple[Path, ...]] = (
    _CREEK_TOOLS_ROOT / "creek",
    _CREEK_TOOLS_ROOT / "creek_mcp",
)
_EXCLUDED_SOURCES: Final[frozenset[Path]] = frozenset(
    {_CREEK_TOOLS_ROOT / "creek" / "config.py"},
)
_CONFIG_LOADERS: Final[frozenset[str]] = frozenset({"load_config"})


# --------------------------------------------------------------------------
# Deriving the field inventory from the live model
# --------------------------------------------------------------------------


def _nested_model(annotation: object) -> type[BaseModel] | None:
    """Return the config model an annotation *directly* holds, else ``None``.

    ``CompostConfig`` and ``LLMConfig | None`` yield the model; a container
    such as ``dict[str, LLMConfig]`` does not, because its members are keyed at
    runtime and have no static dotted path.

    Args:
        annotation: A Pydantic field annotation.

    Returns:
        The nested :class:`~pydantic.BaseModel` subclass, or ``None``.
    """
    if isinstance(annotation, type):
        return annotation if issubclass(annotation, BaseModel) else None
    if get_origin(annotation) not in (Union, UnionType):
        return None
    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _walk_model(
    model: type[BaseModel],
    prefix: tuple[str, ...],
    stack: frozenset[type[BaseModel]],
    leaves: set[tuple[str, ...]],
    occurrences: dict[type[BaseModel], set[tuple[str, ...]]],
) -> None:
    """Record *model*'s leaf paths and where each nested model occurs.

    Args:
        model: The model to walk.
        prefix: Dotted path segments leading to *model*.
        stack: Models already on the current branch (recursion guard).
        leaves: Accumulator of leaf field paths.
        occurrences: Accumulator of model -> paths at which it appears.
    """
    occurrences.setdefault(model, set()).add(prefix)
    for name, info in model.model_fields.items():
        path = (*prefix, name)
        nested = _nested_model(info.annotation)
        if nested is None or nested in stack:
            leaves.add(path)
            continue
        _walk_model(nested, path, stack | {nested}, leaves, occurrences)


def model_leaf_paths(model: type[BaseModel]) -> frozenset[str]:
    """Return every dotted leaf-field path of *model*, derived from the model.

    Args:
        model: The root config model.

    Returns:
        Dotted paths such as ``linking.cross_source_aggregation``.
    """
    leaves: set[tuple[str, ...]] = set()
    _walk_model(model, (), frozenset({model}), leaves, {})
    return frozenset(".".join(path) for path in leaves)


def _model_occurrences(
    model: type[BaseModel],
) -> dict[type[BaseModel], frozenset[tuple[str, ...]]]:
    """Return each nested model class mapped to the paths where it appears.

    Args:
        model: The root config model.

    Returns:
        Model class -> the set of dotted-path tuples reaching it.
    """
    leaves: set[tuple[str, ...]] = set()
    occurrences: dict[type[BaseModel], set[tuple[str, ...]]] = {}
    _walk_model(model, (), frozenset({model}), leaves, occurrences)
    return {cls: frozenset(paths) for cls, paths in occurrences.items()}


# --------------------------------------------------------------------------
# Resolving attribute chains in production source
# --------------------------------------------------------------------------


def _resolve(
    model: type[BaseModel],
    attrs: tuple[str, ...],
) -> tuple[type[BaseModel] | None, tuple[str, ...]]:
    """Walk *attrs* down *model*, stopping at the first non-field attribute.

    Args:
        model: The model the chain is rooted at.
        attrs: Attribute names in access order.

    Returns:
        ``(model reached, attributes consumed)``. The model is ``None`` when
        the last consumed attribute is a scalar leaf.
    """
    current: type[BaseModel] | None = model
    consumed: list[str] = []
    for attr in attrs:
        if current is None or attr not in current.model_fields:
            break
        consumed.append(attr)
        current = _nested_model(current.model_fields[attr].annotation)
    return current, tuple(consumed)


def _attribute_chain(node: ast.Attribute) -> tuple[str, ...] | None:
    """Return a ``Name``-rooted attribute chain, or ``None`` if not one.

    Args:
        node: The outermost attribute node.

    Returns:
        ``("config", "compost", "threshold")`` style tuples, else ``None``.
    """
    attrs: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        attrs.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    attrs.append(current.id)
    return tuple(reversed(attrs))


def _annotation_model(
    node: ast.expr | None,
    classes: dict[str, type[BaseModel]],
) -> type[BaseModel] | None:
    """Return the config model an annotation expression refers to, if any.

    Handles bare names, string (postponed) annotations and wrappers such as
    ``CompostConfig | None``.

    Args:
        node: The annotation expression, or ``None``.
        classes: Config model classes keyed by class name.

    Returns:
        The annotated config model, or ``None``.
    """
    if node is None:
        return None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in classes:
            return classes[sub.id]
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            model = classes.get(sub.value.strip("'\" |"))
            if model is not None:
                return model
    return None


def _arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    """Return every declared argument of a function, in no particular order.

    Args:
        node: The function definition.

    Returns:
        The positional, keyword, ``*args`` and ``**kwargs`` arguments.
    """
    args = node.args
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return [arg for arg in (*every, args.vararg, args.kwarg) if arg is not None]


def _self_attribute(target: ast.expr) -> str | None:
    """Return the attribute name of a ``self.<name>`` target, else ``None``.

    Args:
        target: An assignment target.

    Returns:
        The attribute name, or ``None`` when the target is not ``self.<name>``.
    """
    if not isinstance(target, ast.Attribute):
        return None
    if isinstance(target.value, ast.Name) and target.value.id == "self":
        return target.attr
    return None


def _stored_config_attribute(
    statement: ast.stmt,
    params: dict[str, type[BaseModel]],
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Return the ``self.<attr>`` binding a statement stores, if it stores one.

    Args:
        statement: A statement inside a method body.
        params: The method's config-model-annotated parameters.
        classes: Config model classes keyed by class name.

    Returns:
        A single-entry ``{attribute: model}`` mapping, or an empty mapping.
    """
    target: ast.expr
    model: type[BaseModel] | None
    if isinstance(statement, ast.AnnAssign):
        target, value = statement.target, statement.value
        model = _annotation_model(statement.annotation, classes)
    elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target, value, model = statement.targets[0], statement.value, None
    else:
        return {}
    name = _self_attribute(target)
    if name is None:
        return {}
    if model is None and isinstance(value, ast.Name):
        model = params.get(value.id)
    return {name: model} if model is not None else {}


def _carrier_attributes(
    trees: Iterable[ast.Module],
    classes: dict[str, type[BaseModel]],
) -> dict[str, dict[str, type[BaseModel]]]:
    """Map ordinary classes to the config blocks they hold on an attribute.

    ``LLMClassifier.__init__(self, config: LLMConfig)`` storing ``self.config``
    makes ``classifier.config.max_concurrent`` a real read at a call site far
    from the class. Without this pass such reads are invisible and their fields
    look dormant.

    Args:
        trees: Parsed production modules.
        classes: Config model classes keyed by class name.

    Returns:
        Class name -> ``{attribute: config model}``.
    """
    carriers: dict[str, dict[str, type[BaseModel]]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name in classes:
                continue
            found = _class_config_attributes(node, classes)
            if found:
                carriers.setdefault(node.name, {}).update(found)
    return carriers


def _class_config_attributes(
    node: ast.ClassDef,
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Return the config blocks a class stores on ``self``.

    Args:
        node: The class definition.
        classes: Config model classes keyed by class name.

    Returns:
        Attribute name -> the config model stored there.
    """
    attributes = _declared_config_attributes(node, classes)
    for method in node.body:
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            attributes |= _assigned_config_attributes(method, classes)
    return attributes


def _declared_config_attributes(
    node: ast.ClassDef,
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Return the class-body attributes annotated as a config model.

    Args:
        node: The class definition.
        classes: Config model classes keyed by class name.

    Returns:
        Attribute name -> the config model declared there.
    """
    declared: dict[str, type[BaseModel]] = {}
    for member in node.body:
        if not isinstance(member, ast.AnnAssign):
            continue
        model = _annotation_model(member.annotation, classes)
        if model is not None and isinstance(member.target, ast.Name):
            declared[member.target.id] = model
    return declared


def _assigned_config_attributes(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Return the config blocks a method stores onto ``self``.

    Args:
        method: The method definition.
        classes: Config model classes keyed by class name.

    Returns:
        Attribute name -> the config model stored there.
    """
    params = {
        arg.arg: model
        for arg in _arguments(method)
        if (model := _annotation_model(arg.annotation, classes)) is not None
    }
    stored: dict[str, type[BaseModel]] = {}
    for statement in ast.walk(method):
        if isinstance(statement, ast.stmt):
            stored |= _stored_config_attribute(statement, params, classes)
    return stored


def _protocol_members(node: ast.ClassDef) -> frozenset[str]:
    """Return the public member names a ``Protocol`` class declares.

    Args:
        node: A class definition whose bases include ``Protocol``.

    Returns:
        The declared member names, private and dunder names excluded.
    """
    names: set[str] = set()
    for statement in node.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(statement.name)
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target,
            ast.Name,
        ):
            names.add(statement.target.id)
    return frozenset(name for name in names if not name.startswith("_"))


def _protocol_models(
    trees: Iterable[ast.Module],
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Map structural ``Protocol`` names onto the config model that satisfies them.

    Several modules deliberately declare a ``Protocol`` — ``SegmentationConfig``,
    ``SplitPolicySource`` — instead of importing the settings tree, and the
    production caller then passes a real config block in. Matching a protocol to
    the single config model whose fields cover its members recovers those reads
    without a hand-maintained alias list. A protocol covered by no model, or by
    more than one, is skipped: an ambiguous match would prove nothing.

    Args:
        trees: Parsed production modules.
        classes: Config model classes keyed by class name.

    Returns:
        Protocol name -> the unique config model satisfying it.
    """
    aliases: dict[str, type[BaseModel]] = {}
    models = set(classes.values())
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                match = _protocol_alias(node, models)
                if match is not None:
                    aliases[node.name] = match
    return aliases


def _protocol_alias(
    node: ast.ClassDef,
    models: set[type[BaseModel]],
) -> type[BaseModel] | None:
    """Return the single config model satisfying a ``Protocol`` class def.

    Args:
        node: A class definition, which may or may not be a ``Protocol``.
        models: Every config model in the tree.

    Returns:
        The unique satisfying model, or ``None`` when the class is not a
        protocol, declares nothing, or is satisfied by several models.
    """
    bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
    if "Protocol" not in bases:
        return None
    members = _protocol_members(node)
    if not members:
        return None
    matches = [model for model in models if members <= set(model.model_fields)]
    return matches[0] if len(matches) == 1 else None


def _return_annotation_models(
    trees: Iterable[ast.Module],
    classes: dict[str, type[BaseModel]],
) -> dict[str, type[BaseModel]]:
    """Map function names to the config model they are annotated to return.

    Production code rarely calls ``load_config`` directly; it calls a helper
    such as ``_load_config_for_vault(vault) -> CreekConfig``. Without this map
    every read behind such a helper would look dormant.

    Args:
        trees: Parsed production modules.
        classes: Config model classes keyed by class name.

    Returns:
        Function name -> returned config model.
    """
    returns: dict[str, type[BaseModel]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            model = _annotation_model(node.returns, classes)
            if model is not None:
                returns[node.name] = model
    return returns


class _ReadScanner(ast.NodeVisitor):
    """Collect the config-field paths a single module reads.

    The scanner is deliberately conservative about *rooting* a chain — it needs
    an annotation, a config-model construction, a ``load_config`` call, or an
    assignment from an already-rooted chain — and deliberately generous once
    rooted, so a dynamic ``getattr`` counts as reading the whole sub-tree.
    """

    def __init__(
        self,
        classes: dict[str, type[BaseModel]],
        occurrences: dict[type[BaseModel], frozenset[tuple[str, ...]]],
        returns: dict[str, type[BaseModel]],
        carriers: dict[str, dict[str, type[BaseModel]]],
    ) -> None:
        """Initialise the scanner.

        Args:
            classes: Config model classes keyed by class name.
            occurrences: Model class -> the paths at which it appears.
            returns: Function name -> the config model it is annotated to
                return, so ``config = _load_config_for_vault(v)`` roots a chain.
            carriers: Class name -> the config blocks it stores on ``self``.
        """
        self._classes = classes
        self._occurrences = occurrences
        self._returns = returns
        self._carriers = carriers
        self._scopes: list[dict[tuple[str, ...], type[BaseModel]]] = [{}]
        self._class_scope_depth: list[int] = []
        self.reads: set[tuple[str, ...]] = set()
        self.dynamic_roots: set[tuple[str, ...]] = set()

    # -- scope plumbing ----------------------------------------------------

    def _lookup(
        self,
        chain: tuple[str, ...],
    ) -> tuple[type[BaseModel], tuple[str, ...]] | None:
        """Return the model a chain prefix is bound to, plus the rest.

        Args:
            chain: A ``Name``-rooted attribute chain.

        Returns:
            ``(model, remaining attributes)``, or ``None`` when unbound.
        """
        for size in range(len(chain) - 1, 0, -1):
            key = chain[:size]
            for scope in reversed(self._scopes):
                if key in scope:
                    return scope[key], chain[size:]
        return None

    def _bound_model(self, key: tuple[str, ...]) -> type[BaseModel] | None:
        """Return the model bound to an exact chain key, if any.

        Args:
            key: The chain prefix to look up.

        Returns:
            The bound config model, or ``None``.
        """
        for scope in reversed(self._scopes):
            if key in scope:
                return scope[key]
        return None

    def _bind(self, key: tuple[str, ...], model: type[BaseModel]) -> None:
        """Bind a name (or ``self.attr``) to a config model in the right scope.

        ``self.x = config.compost`` must outlive ``__init__``, so it is stored
        on the enclosing class scope rather than the method scope.

        Args:
            key: The chain prefix being bound.
            model: The config model it holds.
        """
        depth = -1
        if key[0] == "self" and len(key) > 1 and self._class_scope_depth:
            depth = self._class_scope_depth[-1]
        self._scopes[depth][key] = model

    # -- recording ---------------------------------------------------------

    def _record(self, model: type[BaseModel], attrs: tuple[str, ...]) -> None:
        """Record a static read of *attrs* relative to *model*.

        Args:
            model: The model the chain is rooted at.
            attrs: The attribute names accessed on it.
        """
        _, consumed = _resolve(model, attrs)
        if not consumed:
            return
        for base in self._occurrences.get(model, frozenset()):
            self.reads.add(base + consumed)

    def _record_dynamic(self, model: type[BaseModel]) -> None:
        """Record that every field of *model* is reachable by dynamic lookup.

        Args:
            model: The model a non-literal ``getattr`` reads from.
        """
        for base in self._occurrences.get(model, frozenset()):
            self.dynamic_roots.add(base)

    def _expression_model(self, node: ast.expr) -> type[BaseModel] | None:
        """Return the config model an expression evaluates to, if any.

        Args:
            node: The expression to classify.

        Returns:
            The config model, or ``None`` when the expression is not one.
        """
        if isinstance(node, ast.Call):
            return self._call_model(node)
        if isinstance(node, ast.Name):
            return self._bound_model((node.id,))
        if not isinstance(node, ast.Attribute):
            return None
        chain = _attribute_chain(node)
        if chain is None:
            return None
        found = self._lookup(chain)
        if found is None:
            return None
        model, rest = found
        reached, consumed = _resolve(model, rest)
        return reached if consumed == rest else None

    def _call_model(self, node: ast.Call) -> type[BaseModel] | None:
        """Return the config model a call constructs or loads, if any.

        Args:
            node: The call expression.

        Returns:
            The config model produced by the call, or ``None``.
        """
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        if name is None and isinstance(func, ast.Attribute):
            name = func.attr
        if name is None:
            return None
        if name in _CONFIG_LOADERS:
            return CreekConfig
        return self._classes.get(name) or self._returns.get(name)

    def _annotation_model(self, node: ast.expr | None) -> type[BaseModel] | None:
        """Return the config model an annotation refers to, if any.

        Args:
            node: The annotation expression (possibly ``None``).

        Returns:
            The annotated config model, or ``None``.
        """
        return _annotation_model(node, self._classes)

    def _annotation_bindings(
        self,
        root: tuple[str, ...],
        annotation: ast.expr | None,
    ) -> dict[tuple[str, ...], type[BaseModel]]:
        """Return the scope entries an annotated name contributes.

        A config-model annotation binds the name itself; a carrier-class
        annotation binds ``name.attr`` for each config block the class holds.

        Args:
            root: The chain key of the annotated name.
            annotation: Its annotation expression, or ``None``.

        Returns:
            Scope entries to install.
        """
        model = _annotation_model(annotation, self._classes)
        if model is not None:
            return {root: model}
        if annotation is None:
            return {}
        for sub in ast.walk(annotation):
            if isinstance(sub, ast.Name) and sub.id in self._carriers:
                held = self._carriers[sub.id]
                return {(*root, attr): block for attr, block in held.items()}
        return {}

    # -- visitors ----------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Push a class scope, binding ``self`` when the class is a config model.

        Args:
            node: The class definition.
        """
        scope: dict[tuple[str, ...], type[BaseModel]] = {}
        model = self._classes.get(node.name)
        if model is not None:
            scope[("self",)] = model
        for attr, held in self._carriers.get(node.name, {}).items():
            scope[("self", attr)] = held
        self._scopes.append(scope)
        self._class_scope_depth.append(len(self._scopes) - 1)
        self.generic_visit(node)
        self._class_scope_depth.pop()
        self._scopes.pop()

    def _visit_callable(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Push a function scope seeded from annotated parameters.

        Args:
            node: The function definition.
        """
        scope: dict[tuple[str, ...], type[BaseModel]] = {}
        for arg in _arguments(node):
            scope |= self._annotation_bindings((arg.arg,), arg.annotation)
        self._scopes.append(scope)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Scan a function body in its own scope.

        Args:
            node: The function definition.
        """
        self._visit_callable(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Scan an async function body in its own scope.

        Args:
            node: The async function definition.
        """
        self._visit_callable(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Propagate a config-model binding across a plain assignment.

        Args:
            node: The assignment statement.
        """
        self.generic_visit(node)
        model = self._expression_model(node.value)
        if model is None:
            return
        for target in node.targets:
            self._bind_target(target, model)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Bind an annotated assignment target to its config model.

        Args:
            node: The annotated assignment statement.
        """
        self.generic_visit(node)
        root = self._target_chain(node.target)
        if root is not None:
            for key, held in self._annotation_bindings(root, node.annotation).items():
                self._bind(key, held)
        model: type[BaseModel] | None = self._annotation_model(node.annotation)
        if model is None and node.value is not None:
            model = self._expression_model(node.value)
        if model is not None:
            self._bind_target(node.target, model)

    def _target_chain(self, target: ast.expr) -> tuple[str, ...] | None:
        """Return the chain key an assignment target names, if it names one.

        Args:
            target: The assignment target node.

        Returns:
            The chain key, or ``None`` for tuple/subscript targets.
        """
        if isinstance(target, ast.Name):
            return (target.id,)
        if isinstance(target, ast.Attribute):
            return _attribute_chain(target)
        return None

    def _bind_target(self, target: ast.expr, model: type[BaseModel]) -> None:
        """Bind an assignment target to *model* when it is a simple chain.

        Args:
            target: The assignment target node.
            model: The config model being assigned.
        """
        chain = self._target_chain(target)
        if chain is not None:
            self._bind(chain, model)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record a config read when the attribute chain resolves.

        Args:
            node: The attribute access.
        """
        chain = _attribute_chain(node)
        if chain is not None:
            found = self._lookup(chain)
            if found is not None:
                self._record(*found)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Record ``getattr`` lookups against a config model.

        Args:
            node: The call expression.
        """
        self.generic_visit(node)
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "getattr":
            return
        if len(node.args) < 2:
            return
        self._record_getattr(node.args[0], node.args[1])

    def _record_getattr(self, target: ast.expr, name: ast.expr) -> None:
        """Record a ``getattr`` read, dynamic or literal.

        Args:
            target: The object being read from.
            name: The attribute-name expression.
        """
        model = self._expression_model(target)
        if model is None:
            return
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            self._record(model, (name.value,))
            return
        self._record_dynamic(model)


# --------------------------------------------------------------------------
# Running the scan
# --------------------------------------------------------------------------


def read_field_paths(
    model: type[BaseModel],
    sources: Iterable[str],
) -> frozenset[str]:
    """Return the dotted leaf paths of *model* that *sources* read.

    Args:
        model: The root config model.
        sources: Python source texts to analyse.

    Returns:
        The dotted paths of every leaf field reached by the sources.
    """
    occurrences = _model_occurrences(model)
    classes = {cls.__name__: cls for cls in occurrences}
    trees = [ast.parse(source) for source in sources]
    classes |= _protocol_models(trees, classes)
    returns = _return_annotation_models(trees, classes)
    carriers = _carrier_attributes(trees, classes)
    reads: set[tuple[str, ...]] = set()
    dynamic: set[tuple[str, ...]] = set()
    for tree in trees:
        scanner = _ReadScanner(classes, occurrences, returns, carriers)
        scanner.visit(tree)
        reads |= scanner.reads
        dynamic |= scanner.dynamic_roots
    leaves: set[tuple[str, ...]] = set()
    _walk_model(model, (), frozenset({model}), leaves, {})
    hit = {leaf for leaf in leaves if leaf in reads}
    hit |= {leaf for leaf in leaves if any(leaf[: len(d)] == d for d in dynamic)}
    return frozenset(".".join(path) for path in hit)


def _production_files() -> Iterator[Path]:
    """Yield every production Python file the contract scans.

    Yields:
        Paths under ``creek/`` and ``creek_mcp/``, minus ``creek/config.py``.
    """
    for root in _PRODUCTION_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if path not in _EXCLUDED_SOURCES:
                yield path


@lru_cache(maxsize=1)
def _consumed_config_fields() -> frozenset[str]:
    """Return the config leaf paths production code reads.

    Returns:
        Dotted paths read somewhere under ``creek/`` or ``creek_mcp/``.
    """
    sources = [path.read_text(encoding="utf-8") for path in _production_files()]
    return read_field_paths(CreekConfig, sources)


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_config_fields_are_derived_from_the_live_model() -> None:
    """The inventory comes from the model, so it cannot silently drift."""
    paths = model_leaf_paths(CreekConfig)

    assert "linking.cross_source_aggregation" in paths
    assert "cleaning.discord.strip_emoji" in paths
    assert len(paths) > len(CreekConfig.model_fields), (
        "nested config blocks must contribute leaf paths, not one path each"
    )


def test_every_config_field_is_consumed_or_declared_dormant() -> None:
    """No config field may be shipped to users while wired to nothing."""
    unconsumed = sorted(model_leaf_paths(CreekConfig) - _consumed_config_fields())
    undeclared = [path for path in unconsumed if path not in DORMANT_CONFIG_FIELDS]

    assert not undeclared, (
        "These CreekConfig fields are shipped into every vault by `creek init` "
        "but no production code under creek/ or creek_mcp/ reads them: "
        f"{undeclared}. Consume the field, or add it to "
        "DORMANT_CONFIG_FIELDS in tests/test_config_contract.py with a reason "
        "citing the issue that tracks wiring it in."
    )


def test_dormant_allowlist_has_no_stale_entries() -> None:
    """The allowlist ratchets: a field that is read must leave it."""
    consumed = _consumed_config_fields()
    stale = sorted(path for path in DORMANT_CONFIG_FIELDS if path in consumed)

    assert not stale, (
        f"These fields are declared dormant but production code now reads them: "
        f"{stale}. Delete them from DORMANT_CONFIG_FIELDS — the allowlist only "
        "ever shrinks."
    )


def test_dormant_allowlist_names_only_real_fields() -> None:
    """A renamed or deleted field must not leave a fossil in the allowlist."""
    paths = model_leaf_paths(CreekConfig)
    unknown = sorted(path for path in DORMANT_CONFIG_FIELDS if path not in paths)

    assert not unknown, (
        f"DORMANT_CONFIG_FIELDS names fields that no longer exist on "
        f"CreekConfig: {unknown}."
    )


@pytest.mark.parametrize(("path", "reason"), sorted(DORMANT_CONFIG_FIELDS.items()))
def test_dormant_reason_cites_an_issue(path: str, reason: str) -> None:
    """Every dormancy declaration points at where the decision is tracked.

    Args:
        path: The allowlisted config field path.
        reason: The declared reason.
    """
    assert _ISSUE_REFERENCE.search(reason), (
        f"DORMANT_CONFIG_FIELDS[{path!r}] must cite an issue number so the "
        f"next engineer can find the decision; got {reason!r}."
    )


# --------------------------------------------------------------------------
# Guarding the guard
# --------------------------------------------------------------------------


class _Probe(BaseModel):
    """Throwaway model used to prove the detector actually detects."""

    read_directly: int = 0
    """A field the probe sources read through an annotated parameter."""

    read_through_alias: int = 0
    """A field the probe sources read through an assigned local alias."""

    read_dynamically: int = 0
    """A field the probe sources reach only through ``getattr``."""

    never_read: int = 0
    """A field no probe source touches."""


_PROBE_SOURCES: Final[tuple[str, ...]] = (
    "def uses(probe: _Probe) -> int:\n    return probe.read_directly\n",
    "def aliases(probe: _Probe) -> int:\n"
    "    local = probe\n"
    "    return local.read_through_alias\n",
    "def dynamic(probe: _Probe, name: str) -> object:\n"
    "    return getattr(probe, name)\n",
)


def test_detector_reports_an_unread_field_as_unconsumed() -> None:
    """A field nothing reads must not be mistaken for a consumed one."""
    consumed = read_field_paths(_Probe, _PROBE_SOURCES[:2])

    assert "never_read" not in consumed


def test_detector_resolves_direct_and_aliased_reads() -> None:
    """Annotation-rooted and alias-rooted chains both count as reads."""
    consumed = read_field_paths(_Probe, _PROBE_SOURCES[:2])

    assert "read_directly" in consumed
    assert "read_through_alias" in consumed


def test_detector_treats_dynamic_getattr_as_reading_the_subtree() -> None:
    """``getattr(cfg, name)`` really can reach any field, so all count."""
    consumed = read_field_paths(_Probe, _PROBE_SOURCES[2:])

    assert "read_dynamically" in consumed
    assert "never_read" in consumed


def test_detector_finds_nested_reads_in_the_real_model() -> None:
    """The real scan resolves nested chains, not just top-level fields."""
    consumed = _consumed_config_fields()

    assert any("." in path for path in consumed), (
        "the scan resolved no nested config reads at all — the resolver is "
        "broken and every dormancy verdict below it is meaningless"
    )
