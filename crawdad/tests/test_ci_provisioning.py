"""The crawdad CI job must provision from ``crawdad/uv.lock``, not PyPI.

Issue #1501. Until this file went green the ``crawdad`` job in
``.github/workflows/ci.yml`` provisioned itself with two lines::

    python -m pip install --upgrade pip setuptools wheel
    pip install -e ".[dev]"

Both are *live* PyPI resolves. Neither reads ``crawdad/uv.lock``, and
neither reads ``[tool.uv].constraint-dependencies`` — pip cannot see
either surface. So the environment CI actually imports is whatever PyPI
resolved on the morning of the run, and the lockfile this project
maintains, relocks, and audits governs nothing that gates a merge. That
is not a theoretical drift: ``tests/test_dependency_pins.py`` records
openai 3.0.0 and its httpx2/httpcore2 stack reaching a green ``main``
build exactly this way (CI run 31740475473), which is why the
``openai<3.0.0`` ceiling had to be written into
``[project].dependencies`` — the only surface a live pip resolve
honours.

The ``--upgrade pip setuptools wheel`` line is the sharper half. It
installs setuptools *unbounded*, which walks straight through the
``setuptools>=83.0.0,<85.0.0`` band in ``[build-system].requires``
(#1258) — and, worse, walks past the guard that band has, because
``test_dependency_pins.py`` inspects ``pyproject.toml`` and nothing
else. A workflow line is invisible to a pyproject walk. So the band can
be correct, its test green, and the runner still building against the
release the advisory is filed for.

The fix is the shape creek-tools' jobs already use — export the lock,
install the export, then install the project itself with ``--no-deps``
— with the temp file scoped to crawdad. What follows is the guard on
that shape. Four design rules govern it, each of which exists because
the obvious version of this file would be worthless:

**Loud preconditions.** The first four tests assert that the workflow
file exists, that it parses to a non-empty ``jobs:`` mapping, that
``crawdad`` is one of those jobs, and that the job runs at least one
command. A guard that passes because it found nothing to inspect is the
standing failure mode on this repository: rename the job, restructure
the steps, or move the workflow, and every "no bad command is present"
assertion below becomes true for the wrong reason. Every filtering
helper here is therefore backed by a non-emptiness assertion —
``_crawdad_commands`` asserts before it returns, so no test downstream
of it can pass on an empty corpus.

**Rules key on install shape, never on the program name.** The trap
case is ``uv pip install --system -e ".[dev]"``. A predicate that asks
"is the program ``pip``?" waves that through, and a later edit — "the
dev tooling is missing, add the extra back" — could re-add a full live
resolve *on top of* the locked install with every other assertion here
still green. ``_requests_extras_editable`` therefore asks only what the
tokens request: an editable flag plus a PEP 508 extras marker. It
answers True for the pip, ``python -m pip``, and ``uv pip`` spellings
alike, and False for ``-e .`` with no extras.

**``uv sync`` is the shape that looks right and does not work.** This
was measured, not assumed: ``uv sync --locked --all-extras`` installs
into ``crawdad/.venv``, while every gate script shells out to *bare*
tool names — ``scripts/lint.sh:15``, ``format.sh:17``,
``typecheck.sh:8``, ``docstrings.sh:8``, ``complexity.sh:8``,
``test.sh:26``. Under a runner-like PATH that yields
``./scripts/lint.sh: line 15: ruff: command not found``. Only a
system-targeted install puts the console scripts where the scripts look
for them, so ``_tools_reach_path`` encodes that distinction and is
tested against a bare ``uv sync`` directly.

**Prose is not a command.** ``_commands_in`` strips ``#`` comments and
joins ``\\`` continuations before tokenising, and one test feeds it a
block that is *nothing but* comments — including a comment that spells
out the very install line we want — to prove no positive assertion here
can be satisfied by a workflow that merely talks about installing from
the lock.

The retired shapes are kept as a literal corpus rather than described,
and the corpus has its own honesty test: it must be non-empty *and*
cover both defect shapes, one classified by
``_requests_extras_editable`` and one by
``_upgrades_build_tools_unbounded``. Deleting an entry to make a future
edit convenient must break a test, not quietly narrow the guard.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml

#: Repository root, resolved from this file: ``tests`` → ``crawdad`` →
#: repo. The workflow is one file shared by every project in the
#: monorepo, so it cannot be reached from ``crawdad/`` alone.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The single CI workflow. Both creek-tools' jobs and crawdad's live
#: here, which is what makes the ``/tmp`` filename collision below a
#: real hazard rather than a stylistic preference.
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Job id of the CrawDad Quality job in ``ci.yml``.
_CRAWDAD_JOB = "crawdad"

#: Substring every crawdad-owned temp path must carry. creek-tools'
#: jobs already export to ``/tmp/locked-requirements.txt``; an unscoped
#: name here would be the same path for a different pinned set.
_CRAWDAD_SCOPE_MARKER = "crawdad"

#: The workflow-scoped env var holding the pinned uv release. Asserted
#: by *name*: the version itself (``0.11.16`` today) lives at
#: ``ci.yml``'s ``env:`` block and is shared by five jobs — four before
#: #1501, plus crawdad — so hardcoding it here would turn every
#: deliberate uv bump into a crawdad test failure in an unrelated file.
_UV_PIN_ENV_VAR = "UV_PINNED_VERSION"

#: The lockfile whose hash must key the dependency cache.
_CRAWDAD_LOCK_HASH_CALL = "hashFiles(crawdad/uv.lock)"

#: The cache path uv actually writes to.
_UV_CACHE_PATH = "~/.cache/uv"

#: Flags that request an editable install, in both spellings pip and uv
#: accept. ``--editable=<target>`` is handled separately, by prefix.
_EDITABLE_FLAGS = frozenset({"-e", "--editable"})

#: Flags naming a requirements file. uv accepts ``--requirements`` as
#: well as pip's ``--requirement``; both are honoured so the guard does
#: not depend on which front end the implementation picks.
_REQUIREMENT_FLAGS = frozenset({"-r", "--requirement", "--requirements"})
_REQUIREMENT_PREFIXES = ("--requirement=", "--requirements=", "-r=")

#: Flags naming an export's output file.
_OUTPUT_FLAGS = frozenset({"-o", "--output-file"})
_OUTPUT_PREFIXES = ("--output-file=", "-o=")

#: Build tooling that must never be installed without a version bound.
#: ``[build-system].requires`` bounds setuptools to ``>=83.0.0,<85.0.0``
#: for PYSEC-2026-3447 (#1258), and a bare ``setuptools`` token on a
#: ``pip install`` line ignores that band entirely.
_BUILD_TOOL_NAMES = frozenset({"setuptools", "wheel"})

#: The exact provisioning lines this issue retires. Kept verbatim
#: rather than paraphrased so the regression is pinned by the text that
#: actually shipped. ``test_the_retired_corpus_covers_both_defect_shapes``
#: keeps the tuple honest: it must stay non-empty and must keep covering
#: both defect shapes, so deleting the setuptools/wheel entry breaks a
#: test instead of silently halving the guard.
_RETIRED_LIVE_INSTALL_COMMANDS = (
    "python -m pip install --upgrade pip setuptools wheel",
    'pip install -e ".[dev]"',
)

#: Every front end that can request an extras-carrying editable install.
#: The third and fourth entries are the trap: a ``uv``-fronted command
#: that is still a full live resolve.
_EXTRAS_EDITABLE_SHAPES = (
    'pip install -e ".[dev]"',
    "python -m pip install -e '.[dev]'",
    'uv pip install --system -e ".[dev]"',
    'uv pip install --system --editable ".[dev,test]"',
)

#: Locked-install shapes that must NOT be mistaken for a live resolve.
#: ``-e .`` with no extras is the project's own install; ``-r`` reads
#: the exported lock.
_LOCKED_INSTALL_SHAPES = (
    "uv pip install --system --no-deps -e .",
    "uv pip install --system -r /tmp/crawdad-locked-requirements.txt",
)

#: The measured trap of rule three: correct-looking, and the gate
#: scripts cannot see anything it installs.
_BARE_UV_SYNC = "uv sync --locked --all-extras"

#: Installs that leave console scripts inside a project virtualenv.
_VENV_ONLY_INSTALL_SHAPES = (
    _BARE_UV_SYNC,
    "uv pip install -r /tmp/crawdad-locked-requirements.txt",
)

#: Installs that put console scripts on the runner's PATH.
_PATH_REACHING_INSTALL_SHAPES = (
    "pip install -e .",
    "python -m pip install --upgrade pip",
    "uv pip install --system -r /tmp/crawdad-locked-requirements.txt",
    "uv pip install --system --no-deps -e .",
)

#: A run block that is entirely commentary — including a comment that
#: spells out a real install command. Feeding this to ``_commands_in``
#: must yield nothing at all.
_COMMENT_ONLY_RUN_BLOCK = """\
# Provision from the lockfile:
#   uv export --locked --all-extras --no-emit-project --no-hashes \\
#     -o /tmp/crawdad-locked-requirements.txt
#   uv pip install --system -r /tmp/crawdad-locked-requirements.txt
# (everything above is prose; this step runs nothing)
"""


def _parsed_workflow() -> object:
    """Return ``ci.yml`` as ``yaml.safe_load`` parsed it, unnarrowed.

    Parsing rather than scanning the raw text is deliberate: several
    steps in this workflow carry comments that quote the exact command
    they run, and a raw-text guard would happily accept a job whose
    provisioning had been commented out.

    Returns:
        Whatever the YAML document parses to. Narrowing is the callers'
        job, so a malformed document produces a named assertion failure
        instead of an attribute error deep in a helper.
    """
    document: object = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    return document


def _jobs() -> dict[str, object]:
    """Return the workflow's ``jobs:`` mapping, keyed by job id.

    Returns:
        One entry per job. Fails the calling test if the document is
        not a mapping or carries no ``jobs:`` mapping — both of which
        would otherwise make every downstream "no bad command" test
        pass by inspecting nothing.
    """
    document = _parsed_workflow()
    assert isinstance(document, dict), (
        f"{_CI_WORKFLOW} did not parse to a YAML mapping (got "
        f"{type(document).__name__}); every provisioning guard in this "
        "file inspects that mapping, so they would all pass vacuously "
        "(#1501)"
    )
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), (
        f"{_CI_WORKFLOW} has no `jobs:` mapping (got "
        f"{type(jobs).__name__}); with nothing to filter, every "
        "assertion below is satisfied by absence rather than by "
        "correctness (#1501)"
    )
    return {str(key): value for key, value in jobs.items()}


def _crawdad_job() -> dict[str, object]:
    """Return the ``crawdad`` job mapping.

    Returns:
        The job's own mapping. Fails the calling test if the job is
        absent or malformed, naming the job ids that *are* present so a
        rename is diagnosable from the failure alone.
    """
    jobs = _jobs()
    assert _CRAWDAD_JOB in jobs, (
        f"{_CI_WORKFLOW} defines no `{_CRAWDAD_JOB}` job; ci.yml holds "
        f"{sorted(jobs)}. If the job was renamed, rename it here too — "
        "otherwise this whole file guards a job that no longer exists "
        "(#1501)"
    )
    job = jobs[_CRAWDAD_JOB]
    assert isinstance(job, dict), (
        f"the `{_CRAWDAD_JOB}` job is not a mapping (got {type(job).__name__}) (#1501)"
    )
    return {str(key): value for key, value in job.items()}


def _crawdad_steps() -> list[object]:
    """Return the ``crawdad`` job's steps, unnarrowed.

    Returns:
        The raw ``steps:`` list. Fails the calling test if the job has
        no step list at all.
    """
    steps = _crawdad_job().get("steps")
    assert isinstance(steps, list), (
        f"the `{_CRAWDAD_JOB}` job has no `steps:` list (got "
        f"{type(steps).__name__}) (#1501)"
    )
    return steps


def _crawdad_run_blocks() -> list[str]:
    """Return every ``run:`` block in the ``crawdad`` job.

    Returns:
        One string per step that runs shell, in workflow order. Steps
        that only ``uses:`` an action contribute nothing here and are
        inspected separately by the cache assertions.
    """
    return [
        str(step["run"])
        for step in _crawdad_steps()
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]


def _crawdad_run_text() -> str:
    """Return the ``crawdad`` job's ``run:`` blocks, concatenated.

    Returns:
        Every run block joined by newlines. Used only where the
        assertion is about a *reference* — an env var name — rather than
        about the shape of a command.
    """
    return "\n".join(_crawdad_run_blocks())


def _commands_in(block: str) -> list[list[str]]:
    """Tokenise one ``run:`` block into the commands a shell would run.

    Backslash continuations are joined first, so a multi-line
    invocation is inspected as the single command it is, and ``#``
    comments are dropped by ``shlex`` itself, which respects quoting
    while doing so. A block of pure commentary therefore yields an
    empty list — the property
    ``test_a_comment_only_run_block_yields_no_commands`` pins, because
    a workflow that merely *documents* installing from the lock must
    never satisfy a positive assertion here.

    Args:
        block: The literal text of one ``run:`` block.

    Returns:
        One token list per non-blank, non-comment logical line.
    """
    joined = block.replace("\\\n", " ")
    commands: list[list[str]] = []
    for line in joined.splitlines():
        tokens = shlex.split(line, comments=True)
        if tokens:
            commands.append(tokens)
    return commands


def _crawdad_commands() -> list[list[str]]:
    """Return every command the ``crawdad`` job runs, tokenised.

    The non-emptiness assertions live *here*, not at each call site, so
    that every filtering test downstream inherits them: a "no forbidden
    shape is present" assertion over an empty corpus is true and
    worthless, and this is the one chokepoint every such test passes
    through.

    Returns:
        One token list per command, across all of the job's run blocks.
    """
    blocks = _crawdad_run_blocks()
    assert blocks, (
        f"the `{_CRAWDAD_JOB}` job has no `run:` steps at all; every "
        "provisioning assertion in this file would pass by finding "
        "nothing (#1501)"
    )
    commands = [command for block in blocks for command in _commands_in(block)]
    assert commands, (
        f"the `{_CRAWDAD_JOB}` job's {len(blocks)} run block(s) contain "
        "no executable commands — only comments and blank lines. Prose "
        "does not provision a runner (#1501)"
    )
    return commands


def _flag_value(
    tokens: list[str], flags: frozenset[str], prefixes: tuple[str, ...]
) -> str | None:
    """Return the value carried by the first matching flag.

    Args:
        tokens: Shell tokens of a single command.
        flags: Spellings that take their value as the next token.
        prefixes: Spellings that carry their value inline, such as
            ``--output-file=``.

    Returns:
        The flag's value, or ``None`` when the command carries no such
        flag. A trailing flag with nothing after it also yields
        ``None`` — an incomplete invocation names no path.
    """
    for index, token in enumerate(tokens):
        if token in flags and index + 1 < len(tokens):
            return tokens[index + 1]
        for prefix in prefixes:
            if token.startswith(prefix):
                return token[len(prefix) :]
    return None


def _requirement_source(tokens: list[str]) -> str | None:
    """Return the requirements file a command installs from, if any.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        The path given to ``-r`` / ``--requirement(s)``, or ``None``.
    """
    return _flag_value(tokens, _REQUIREMENT_FLAGS, _REQUIREMENT_PREFIXES)


def _output_target(tokens: list[str]) -> str | None:
    """Return the file a command writes its output to, if any.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        The path given to ``-o`` / ``--output-file``, or ``None``.
    """
    return _flag_value(tokens, _OUTPUT_FLAGS, _OUTPUT_PREFIXES)


def _is_crawdad_scoped(target: str | None) -> bool:
    """Return whether an export target names crawdad.

    Args:
        target: The path given to the export's ``-o`` flag, or ``None``
            when the command carried none.

    Returns:
        ``True`` only when a path is present and carries ``crawdad``. A
        missing path is not scoped — absence must never read as
        compliance.
    """
    return target is not None and _CRAWDAD_SCOPE_MARKER in target


def _is_install_command(tokens: list[str]) -> bool:
    """Return whether a command installs packages, whatever the front end.

    Keyed on the ``install`` subcommand rather than on the program
    name, so ``pip``, ``python -m pip``, and ``uv pip`` are all seen.
    ``uv sync`` is deliberately *not* an install command by this
    definition: it provisions a project virtualenv, which is a
    different thing with different consequences — see
    ``_tools_reach_path``.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when some token is the ``install`` subcommand.
    """
    return "install" in tokens


def _is_editable_install(tokens: list[str]) -> bool:
    """Return whether a command requests an editable install.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` for ``-e``, ``--editable``, and ``--editable=<target>``
        in any position.
    """
    return any(
        token in _EDITABLE_FLAGS or token.startswith("--editable=") for token in tokens
    )


def _carries_extras_marker(token: str) -> bool:
    """Return whether a token carries a PEP 508 extras marker.

    Args:
        token: One shell token, already unquoted by ``shlex``.

    Returns:
        ``True`` when the token holds a ``[`` ... ``]`` pair, as in
        ``.[dev]`` or ``.[dev,test]``. A bare ``.`` carries none.
    """
    opening = token.find("[")
    return opening != -1 and token.find("]", opening) > opening


def _requests_extras_editable(tokens: list[str]) -> bool:
    """Return whether a command is an extras-carrying editable install.

    This is the front-end-agnostic replacement for "is the program
    ``pip``?". That question is the hole: ``uv pip install --system -e
    ".[dev]"`` answers no and is nonetheless a full live PyPI resolve,
    honouring neither ``uv.lock`` nor
    ``[tool.uv].constraint-dependencies``. Asking about the *shape*
    instead catches every spelling, including ones nobody has written
    yet.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when the command requests an editable install AND some
        token carries an extras marker. ``uv pip install --system
        --no-deps -e .`` and ``uv pip install --system -r <file>`` both
        answer ``False``.
    """
    if not _is_editable_install(tokens):
        return False
    return any(_carries_extras_marker(token) for token in tokens)


def _upgrades_build_tools_unbounded(tokens: list[str]) -> bool:
    """Return whether a command installs setuptools/wheel with no bound.

    The ``--upgrade`` flag is deliberately *not* required. Dropping the
    flag does not make an unbounded ``setuptools`` install safe, and a
    predicate that insisted on it would be defeated by removing four
    characters.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when an install command names ``setuptools`` or
        ``wheel`` as a bare token. ``setuptools>=83.0.0,<85.0.0`` is a
        different token and answers ``False``, which is the point: the
        band declared in ``[build-system].requires`` is what this
        predicate protects (#1258).
    """
    if not _is_install_command(tokens):
        return False
    return any(token in _BUILD_TOOL_NAMES for token in tokens)


def _targets_a_uv_managed_environment(tokens: list[str]) -> bool:
    """Return whether uv is the front end running this command.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when some token is ``uv`` or a path ending in it. uv's
        installer targets the project virtualenv unless told otherwise,
        so knowing the front end is what makes ``--system`` meaningful.
    """
    return any(token.rsplit("/", maxsplit=1)[-1] == "uv" for token in tokens)


def _tools_reach_path(command: list[str]) -> bool:
    """Return whether an install puts console scripts on the runner PATH.

    This distinction was measured, not assumed. ``uv sync --locked
    --all-extras`` is the shape that looks correct and does not work:
    it installs into ``crawdad/.venv``, while every gate script shells
    out to bare tool names — ``scripts/lint.sh:15``,
    ``format.sh:17``, ``typecheck.sh:8``, ``docstrings.sh:8``,
    ``complexity.sh:8``, ``test.sh:26``. Under a runner-like PATH the
    result is ``./scripts/lint.sh: line 15: ruff: command not found``.
    So "installed from the lock" is necessary and not sufficient; the
    install also has to be system-targeted.

    Args:
        command: Shell tokens of a single command.

    Returns:
        ``False`` for ``uv sync`` in any form (it is not an install
        command and it targets a virtualenv), ``False`` for ``uv pip
        install`` without ``--system``, and ``True`` for ``pip
        install`` / ``python -m pip install`` and for ``uv pip install
        --system``.
    """
    if not _is_install_command(command):
        return False
    if _targets_a_uv_managed_environment(command):
        return "--system" in command
    return True


def _is_lockfile_export(tokens: list[str]) -> bool:
    """Return whether a command exports a lockfile to a requirements file.

    Keyed on the ``export`` subcommand plus an output target rather
    than on the program name. A shell ``export FOO=bar`` fails both
    halves: it is the first token, and it names no output file.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` for ``uv export ... -o <path>`` and any equivalent
        spelling.
    """
    if "export" not in tokens or tokens[0] == "export":
        return False
    return _output_target(tokens) is not None


def _lockfile_exports() -> list[list[str]]:
    """Return the ``crawdad`` job's lockfile-export commands.

    Returns:
        One token list per export. May be empty; every caller asserts
        non-emptiness itself, because "no export exists" and "the
        export is correct" must never look the same.
    """
    return [tokens for tokens in _crawdad_commands() if _is_lockfile_export(tokens)]


def _install_commands() -> list[list[str]]:
    """Return the ``crawdad`` job's install commands.

    Returns:
        One token list per install, whatever the front end.
    """
    return [tokens for tokens in _crawdad_commands() if _is_install_command(tokens)]


def _cache_steps() -> list[dict[str, object]]:
    """Return the ``crawdad`` job's ``actions/cache`` steps.

    The cache is configured with ``uses:`` and ``with:``, not with a
    ``run:`` block, so it is unreachable from the command helpers above
    and has to be read from the step mapping directly.

    Returns:
        One mapping per caching step, in workflow order.
    """
    steps: list[dict[str, object]] = []
    for step in _crawdad_steps():
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and uses.split("@")[0] == "actions/cache":
            steps.append({str(key): value for key, value in step.items()})
    return steps


def _step_input(step: dict[str, object], name: str) -> str:
    """Return one ``with:`` input of a step.

    Args:
        step: A step mapping.
        name: The input's key, such as ``path`` or ``key``.

    Returns:
        The input's value as text, or the empty string when the step
        has no ``with:`` mapping or no such key.
    """
    inputs = step.get("with")
    if not isinstance(inputs, dict):
        return ""
    value = inputs.get(name)
    return "" if value is None else str(value)


def _references_lock_hash(key: str) -> bool:
    """Return whether a cache key hashes ``crawdad/uv.lock``.

    Quoting and spacing inside a GitHub expression are free, so the key
    is normalised before matching rather than compared to one literal
    spelling — ``hashFiles('crawdad/uv.lock')`` and
    ``hashFiles("crawdad/uv.lock")`` are the same instruction.

    Args:
        key: The cache step's ``key`` input.

    Returns:
        ``True`` when the key hashes the lockfile.
    """
    normalised = key.replace('"', "").replace("'", "").replace(" ", "")
    return _CRAWDAD_LOCK_HASH_CALL in normalised


def _is_pinned_uv_bootstrap(token: str) -> bool:
    """Return whether a token installs uv at the workflow-pinned version.

    Args:
        token: One shell token, already unquoted by ``shlex``.

    Returns:
        ``True`` for ``uv==${UV_PINNED_VERSION}``. A hardcoded
        ``uv==0.11.16`` answers ``False`` on purpose: the pin is
        workflow-scoped and shared by four jobs precisely so a
        security-driven bump cannot leave one job behind.
    """
    return token.startswith("uv==") and _UV_PIN_ENV_VAR in token


def test_the_ci_workflow_file_exists() -> None:
    """The workflow this file guards must be where we look for it.

    First of the four preconditions. If the path is wrong — a moved
    workflow, a changed directory depth, a test run from an unexpected
    root — then ``_parsed_workflow`` raises and every assertion below
    it becomes noise. The message names the fully resolved path so the
    diagnosis needs no second guess.
    """
    assert _CI_WORKFLOW.is_file(), (
        f"no CI workflow at {_CI_WORKFLOW}; this file resolves it from "
        f"{Path(__file__).resolve()} via parents[2], so either the "
        "workflow moved or the test file did. Every provisioning "
        "assertion here depends on reading it (#1501)"
    )


def test_the_workflow_parses_to_a_non_empty_jobs_mapping() -> None:
    """``ci.yml`` must parse to a ``jobs:`` mapping with jobs in it.

    Second precondition. ``_jobs`` carries the narrowing assertions;
    this test adds the emptiness one, because a document that parses to
    ``jobs: {}`` satisfies every ``isinstance`` check and still gives
    the guard nothing to inspect.
    """
    jobs = _jobs()
    assert jobs, (
        f"{_CI_WORKFLOW} parsed to an empty `jobs:` mapping; a guard "
        "that inspects no jobs passes for the wrong reason (#1501)"
    )


def test_the_workflow_defines_the_crawdad_job() -> None:
    """``ci.yml`` must still define a job with id ``crawdad``.

    Third precondition, and the likeliest one to break: renaming the
    job is a one-line edit that would silently retire this entire file
    if the lookup failed soft. ``_crawdad_job`` names the ids that do
    exist so the rename is obvious from the failure.
    """
    assert _CRAWDAD_JOB in _jobs(), (
        f"`{_CRAWDAD_JOB}` is not among the jobs in {_CI_WORKFLOW}; see "
        "the failure detail from _crawdad_job for the ids that are "
        "(#1501)"
    )


def test_the_crawdad_job_runs_at_least_one_command() -> None:
    """The job's collected ``run:`` commands must be a non-empty list.

    Fourth precondition, and the one that guards the *filtering* rather
    than the lookup. Most tests below assert that a forbidden shape is
    absent; all of them would hold trivially if the extraction returned
    nothing. ``_crawdad_commands`` asserts this itself, so every
    downstream test inherits the guarantee — this test states it
    directly so the failure is attributed here rather than to whichever
    behavioural test happened to run first.
    """
    commands = _crawdad_commands()
    assert commands, (
        f"the `{_CRAWDAD_JOB}` job runs no commands at all, so every "
        "assertion about what it must not install is satisfied by "
        "absence rather than by correctness (#1501)"
    )


def test_extras_editable_predicate_catches_every_front_end() -> None:
    """Every extras-carrying editable install is classified, pip or uv.

    The mandatory trap case is the third entry: ``uv pip install
    --system -e ".[dev]"``. It reads like part of the locked flow and
    is a full live PyPI resolve. A predicate keyed on the program name
    waves it through, which would let a future "the dev tooling is
    missing" edit re-add a live resolve on top of the lock with every
    other test in this file still green.
    """
    assert _EXTRAS_EDITABLE_SHAPES, (
        "the extras-editable corpus is empty, so this test asserts "
        "nothing; emptying a corpus is how a guard disappears behind a "
        "green gate (#1501)"
    )
    for shape in _EXTRAS_EDITABLE_SHAPES:
        tokens = shlex.split(shape)
        assert _requests_extras_editable(tokens), (
            f"_requests_extras_editable({shape!r}) returned False; this "
            "is a live PyPI resolve honouring neither uv.lock nor "
            "[tool.uv].constraint-dependencies, and the predicate must "
            "key on the install SHAPE (editable flag + extras marker), "
            "never on whether the program is named `pip` (#1501)"
        )


def test_extras_editable_predicate_admits_locked_installs() -> None:
    """The locked shapes must not be mistaken for a live resolve.

    The other half of the predicate. A guard that flagged ``-e .`` or
    ``-r <exported lock>`` would make the correct provisioning
    impossible to write, and the usual repair for that is to weaken the
    predicate until nothing fails — including the trap case above.
    """
    assert _LOCKED_INSTALL_SHAPES, (
        "the locked-install corpus is empty, so this test asserts nothing (#1501)"
    )
    for shape in _LOCKED_INSTALL_SHAPES:
        tokens = shlex.split(shape)
        assert not _requests_extras_editable(tokens), (
            f"_requests_extras_editable({shape!r}) returned True; this "
            "is the locked flow itself — an editable install with no "
            "extras, or an install from the exported requirements file "
            "— and flagging it would make the fix unwritable (#1501)"
        )


def test_uv_sync_does_not_put_tools_on_the_runner_path() -> None:
    """A bare ``uv sync`` must be classified as PATH-invisible.

    Measured, not assumed: ``uv sync --locked --all-extras`` installs
    into ``crawdad/.venv``, and every gate script shells out to bare
    tool names (``scripts/lint.sh:15``, ``format.sh:17``,
    ``typecheck.sh:8``, ``docstrings.sh:8``, ``complexity.sh:8``,
    ``test.sh:26``). Under a runner-like PATH the run dies with
    ``./scripts/lint.sh: line 15: ruff: command not found``. ``uv
    sync`` is the tempting one-liner for "install from the lock", so
    the predicate that would accept it is pinned red here.
    """
    assert not _tools_reach_path(shlex.split(_BARE_UV_SYNC)), (
        f"_tools_reach_path({_BARE_UV_SYNC!r}) returned True. It "
        "installs into crawdad/.venv, but scripts/lint.sh:15 and the "
        "other five gate scripts invoke bare tool names, so the job "
        "fails with `ruff: command not found`. Installing from the "
        "lock is necessary and not sufficient — the install must also "
        "be system-targeted (#1501)"
    )


def test_only_system_targeted_installs_put_tools_on_the_path() -> None:
    """``--system`` is what makes a uv install visible to the scripts.

    Both directions are asserted. Without the negative half a
    predicate that simply returned ``True`` would pass the positive
    half and re-admit the ``crawdad/.venv`` failure mode.
    """
    assert _PATH_REACHING_INSTALL_SHAPES and _VENV_ONLY_INSTALL_SHAPES, (
        "a PATH-reachability corpus is empty, so half this test asserts nothing (#1501)"
    )
    for shape in _PATH_REACHING_INSTALL_SHAPES:
        assert _tools_reach_path(shlex.split(shape)), (
            f"_tools_reach_path({shape!r}) returned False; this install "
            "does reach the runner's PATH, and rejecting it would leave "
            "no writable form of the fix (#1501)"
        )
    for shape in _VENV_ONLY_INSTALL_SHAPES:
        assert not _tools_reach_path(shlex.split(shape)), (
            f"_tools_reach_path({shape!r}) returned True; a uv install "
            "without --system lands in crawdad/.venv, where the gate "
            "scripts' bare tool names cannot find it (#1501)"
        )


def test_a_comment_only_run_block_yields_no_commands() -> None:
    """Commentary must never satisfy a positive assertion.

    Every "the job does X" test below finds X by tokenising run
    blocks. If the extractor kept comment text, a step that merely
    *documents* the locked flow — or a step whose provisioning was
    commented out during debugging and never restored — would satisfy
    the export, the ``--locked`` flag, the ``-r`` consumption, and the
    ``--no-deps`` assertions all at once, while installing nothing.
    """
    assert "uv pip install" in _COMMENT_ONLY_RUN_BLOCK, (
        "the comment-only fixture no longer contains a commented-out "
        "install command, so it is no longer a decoy and this test no "
        "longer proves anything (#1501)"
    )
    assert _commands_in(_COMMENT_ONLY_RUN_BLOCK) == [], (
        "_commands_in extracted commands from a block that is entirely "
        f"`#` comments: {_commands_in(_COMMENT_ONLY_RUN_BLOCK)!r}. "
        "Comments must be stripped before tokenising, or prose stands "
        "in for provisioning (#1501)"
    )


def test_the_retired_corpus_covers_both_defect_shapes() -> None:
    """The regression corpus must stay non-empty and cover both defects.

    ``_RETIRED_LIVE_INSTALL_COMMANDS`` records two *different* failures:
    ``pip install -e ".[dev]"`` is a live dependency resolve, and
    ``python -m pip install --upgrade pip setuptools wheel`` installs
    setuptools unbounded, walking past both the
    ``setuptools>=83.0.0,<85.0.0`` band in ``[build-system].requires``
    and the guard on that band in ``tests/test_dependency_pins.py``,
    which walks ``pyproject.toml`` and never sees a workflow.

    Without this test, deleting either entry would narrow the guard by
    half and leave the suite green. With it, the deletion is red.
    """
    assert _RETIRED_LIVE_INSTALL_COMMANDS, (
        "_RETIRED_LIVE_INSTALL_COMMANDS is empty; the regression guard "
        "below now iterates nothing and passes unconditionally (#1501)"
    )
    tokenised = [shlex.split(command) for command in _RETIRED_LIVE_INSTALL_COMMANDS]
    assert any(_requests_extras_editable(tokens) for tokens in tokenised), (
        "no entry in _RETIRED_LIVE_INSTALL_COMMANDS is a live "
        f"extras-editable resolve: {list(_RETIRED_LIVE_INSTALL_COMMANDS)}. "
        "That defect shape is no longer covered by the corpus (#1501)"
    )
    assert any(_upgrades_build_tools_unbounded(tokens) for tokens in tokenised), (
        "no entry in _RETIRED_LIVE_INSTALL_COMMANDS installs setuptools "
        "or wheel unbounded: "
        f"{list(_RETIRED_LIVE_INSTALL_COMMANDS)}. That is the shape that "
        "bypasses the [build-system].requires band AND the pyproject-only "
        "guard in test_dependency_pins.py, so dropping it from the corpus "
        "silently halves this file (#1501)"
    )


def test_no_retired_live_install_shape_survives_in_the_crawdad_job() -> None:
    """Neither retired provisioning line may appear in the job.

    The literal regression check. The shape predicates below generalise
    it, but this one pins the exact text that shipped, so a revert is
    caught by the same assertion that describes what was reverted.
    """
    commands = _crawdad_commands()
    survivors = [
        retired
        for retired in _RETIRED_LIVE_INSTALL_COMMANDS
        if shlex.split(retired) in commands
    ]
    assert not survivors, (
        f"the `{_CRAWDAD_JOB}` job still runs {survivors}. These are "
        "live PyPI resolves: pip reads neither crawdad/uv.lock nor "
        "[tool.uv].constraint-dependencies, so the lockfile this "
        "project maintains and audits governs nothing that gates a "
        "merge. Provision from the lock instead (#1501)"
    )


def test_the_crawdad_job_never_installs_extras_editable() -> None:
    """No command in the job may be an extras-carrying editable install.

    The generalisation of the regression above, and the one that holds
    against the trap case: ``uv pip install --system -e ".[dev]"``
    would look like part of the locked flow and would restore the full
    live resolve on top of it. ``_crawdad_commands`` asserts its own
    non-emptiness, so this cannot pass by finding nothing.
    """
    offenders = [
        tokens for tokens in _crawdad_commands() if _requests_extras_editable(tokens)
    ]
    assert not offenders, (
        f"the `{_CRAWDAD_JOB}` job runs {offenders}; an editable "
        "install carrying extras is a full live PyPI resolve whatever "
        'the front end — `uv pip install --system -e ".[dev]"` is no '
        'safer than `pip install -e ".[dev]"`. Dependencies come from '
        "the exported lock; the project itself installs with --no-deps "
        "and no extras (#1501)"
    )


def test_the_crawdad_job_never_upgrades_build_tools_unbounded() -> None:
    """setuptools and wheel may not be installed without a version bound.

    This is the hole the old ``python -m pip install --upgrade pip
    setuptools wheel`` line opened, and it is worse than an unpinned
    dependency: ``[build-system].requires`` bounds setuptools to
    ``>=83.0.0,<85.0.0`` for PYSEC-2026-3447 / CVE-2026-59890 (#1258),
    and the guard on that band lives in
    ``tests/test_dependency_pins.py``, which walks ``pyproject.toml``
    only. A workflow line is invisible to it. So the band could be
    correct, its test green, and the runner still building against the
    release the advisory names.
    """
    offenders = [
        tokens
        for tokens in _crawdad_commands()
        if _upgrades_build_tools_unbounded(tokens)
    ]
    assert not offenders, (
        f"the `{_CRAWDAD_JOB}` job runs {offenders}, installing "
        "setuptools and/or wheel with no version specifier. That "
        "bypasses the setuptools>=83.0.0,<85.0.0 band in "
        "[build-system].requires (PYSEC-2026-3447) and is invisible to "
        "test_dependency_pins.py, which only walks pyproject.toml. Let "
        "the lock decide the build tooling (#1501, #1258)"
    )


def test_the_crawdad_job_exports_the_lock_with_the_locked_flag() -> None:
    """Every lockfile export must carry ``--locked``.

    ``--locked`` is the freshness gate: if ``pyproject.toml`` and
    ``uv.lock`` have drifted, the export fails with "The lockfile at
    `uv.lock` needs to be updated" and the fix is ``uv lock``, never
    dropping the flag. Without it the job installs a stale lock and
    reports green, which is the same class of lie the live resolve
    told — just quieter. Both halves are asserted: that an export
    exists at all, and that no export omits the flag.
    """
    exports = _lockfile_exports()
    assert exports, (
        f"the `{_CRAWDAD_JOB}` job exports no lockfile; provisioning "
        "must run `uv export --locked ... -o <file>` and install that "
        "file, not resolve live from PyPI (#1501)"
    )
    unlocked = [tokens for tokens in exports if "--locked" not in tokens]
    assert not unlocked, (
        f"these exports omit --locked: {unlocked}. --locked is the "
        "lock-freshness gate — without it a lock that has drifted from "
        "pyproject.toml exports anyway and CI installs a stale pinned "
        "set while reporting green. If it fails, run `uv lock` (#1501)"
    )


def test_the_export_target_is_crawdad_scoped() -> None:
    """The exported requirements file must carry ``crawdad`` in its name.

    creek-tools' ``Install dependencies (locked)`` steps already export
    to ``/tmp/locked-requirements.txt``. Reusing that exact filename
    means the name no longer identifies whose pinned set it holds:
    anything that puts the two flows on one filesystem — a composite
    action, a self-hosted or reused runner, a future single-job merge,
    or a developer following the workflow by hand — installs one
    project's pins believing they are the other's. The failure is
    silent and the fix costs eight characters.
    """
    exports = _lockfile_exports()
    assert exports, (
        f"the `{_CRAWDAD_JOB}` job exports no lockfile, so there is no "
        "export path to scope (#1501)"
    )
    targets = [_output_target(tokens) for tokens in exports]
    unscoped = [target for target in targets if not _is_crawdad_scoped(target)]
    assert not unscoped, (
        f"these export targets are not crawdad-scoped: {unscoped}. "
        "creek-tools already exports to /tmp/locked-requirements.txt in "
        "its `Install dependencies (locked)` steps, so an unscoped name "
        "is the same path holding a different project's pinned set — "
        "use /tmp/crawdad-locked-requirements.txt (#1501)"
    )


def test_the_exported_requirements_file_is_what_gets_installed() -> None:
    """Something must install from the very file the export writes.

    The two halves are asserted against each other rather than against
    two independent literals: an export to
    ``/tmp/crawdad-locked-requirements.txt`` and an install from
    ``/tmp/locked-requirements.txt`` would both match a hardcoded-path
    test written one at a time, and would install creek-tools' pins (or
    nothing at all). The contract is that the path *matches*, whatever
    it is.
    """
    exports = _lockfile_exports()
    assert exports, (
        f"the `{_CRAWDAD_JOB}` job exports no lockfile, so nothing can "
        "install from one (#1501)"
    )
    installs = _install_commands()
    assert installs, (
        f"the `{_CRAWDAD_JOB}` job runs no install commands at all, so "
        "the exported lockfile is written and then ignored (#1501)"
    )
    consumed = {_requirement_source(tokens) for tokens in installs}
    exported = {_output_target(tokens) for tokens in exports}
    unconsumed = sorted(str(target) for target in exported if target not in consumed)
    sources = sorted(str(source) for source in consumed if source is not None)
    assert not unconsumed, (
        f"the job exports {unconsumed} but installs from {sources}; the "
        "export target and the install source must be the same path, or "
        "the job writes one pinned set and installs another (#1501)"
    )


def test_the_dependency_install_reaches_the_runner_path() -> None:
    """The install that provisions the tooling must be system-targeted.

    Positive counterpart to the ``uv sync`` test. Installing from the
    lock is necessary and not sufficient: the gate scripts invoke bare
    tool names (``scripts/lint.sh:15`` and five others), so an install
    into ``crawdad/.venv`` fails the job with ``ruff: command not
    found``. This asserts the property of the command the job actually
    runs, not of a fixture.
    """
    consumers = [
        tokens
        for tokens in _install_commands()
        if _requirement_source(tokens) is not None
    ]
    assert consumers, (
        f"the `{_CRAWDAD_JOB}` job installs from no requirements file; "
        "dependencies must come from the exported lock via -r (#1501)"
    )
    unreachable = [tokens for tokens in consumers if not _tools_reach_path(tokens)]
    assert not unreachable, (
        f"these installs do not reach the runner's PATH: {unreachable}. "
        "A uv install without --system lands in crawdad/.venv, and "
        "scripts/lint.sh:15, format.sh:17, typecheck.sh:8, "
        "docstrings.sh:8, complexity.sh:8 and test.sh:26 all shell out "
        "to bare tool names — the job dies with `ruff: command not "
        "found` (#1501)"
    )


def test_the_project_is_installed_editable_with_no_deps() -> None:
    """crawdad itself installs with ``--no-deps`` and no extras.

    The lock decides the dependency set; the project's own install
    exists only to put ``crawdad`` on ``sys.path`` and its console
    script on the PATH. Without ``--no-deps`` that second install
    re-resolves the whole graph live, undoing the locked install that
    preceded it — the same defect this issue closes, reintroduced one
    line later.
    """
    editables = [
        tokens for tokens in _install_commands() if _is_editable_install(tokens)
    ]
    assert editables, (
        f"the `{_CRAWDAD_JOB}` job never installs the project itself; "
        "`crawdad` and its console script have to be importable for "
        "check-all.sh to run (#1501)"
    )
    resolving = [tokens for tokens in editables if "--no-deps" not in tokens]
    assert not resolving, (
        f"these editable installs omit --no-deps: {resolving}. Without "
        "it the project install re-resolves the entire dependency graph "
        "live from PyPI, discarding the pinned set installed from the "
        "lock one line earlier (#1501)"
    )
    with_extras = [tokens for tokens in editables if _requests_extras_editable(tokens)]
    assert not with_extras, (
        f"these editable installs still carry extras: {with_extras}; "
        "the dev tooling comes from the exported lock, which includes "
        "every extra via --all-extras (#1501)"
    )


def test_the_crawdad_job_bootstraps_the_pinned_uv_version() -> None:
    """uv is installed at the workflow-pinned version, by env var.

    ``UV_PINNED_VERSION`` was hoisted to workflow scope in #1141 so the
    jobs that install uv cannot drift apart on a security-relevant
    version — the pin has to stay on a release with no open advisories,
    since uv itself is audited by pip-audit. A hardcoded version here
    would rejoin that drift and would turn the next deliberate bump
    into a failure in this file.
    """
    assert _UV_PIN_ENV_VAR in _crawdad_run_text(), (
        f"the `{_CRAWDAD_JOB}` job never references ${_UV_PIN_ENV_VAR}; "
        "uv must be bootstrapped at the workflow-scoped pin, not at "
        "whatever the runner resolves and not at a version hardcoded "
        "in this job (#1501, #1141)"
    )
    bootstraps = [
        tokens
        for tokens in _install_commands()
        if any(_is_pinned_uv_bootstrap(token) for token in tokens)
    ]
    assert bootstraps, (
        f"the `{_CRAWDAD_JOB}` job never installs uv as "
        f"`uv==${{{_UV_PIN_ENV_VAR}}}`; the export and the locked "
        "install both need uv on the runner, and it must be the pinned "
        "release the other jobs use (#1501, #1141)"
    )


def test_the_crawdad_job_caches_the_uv_cache_keyed_on_the_lock() -> None:
    """The cache must hold ``~/.cache/uv`` and turn over with the lock.

    Two failures live here. Caching ``~/.cache/pip`` after the flow
    moved to uv caches a directory nothing writes — a cache step that
    costs time and returns nothing. Keying on
    ``hashFiles('crawdad/pyproject.toml')`` is worse: a relock changes
    ``uv.lock`` and routinely leaves ``pyproject.toml`` untouched
    (every ``uv lock --upgrade`` does), so the key would not turn over
    and CI would restore and reuse the pre-relock resolution. The key
    must hash the artefact the install actually reads.
    """
    caches = _cache_steps()
    assert caches, (
        f"the `{_CRAWDAD_JOB}` job has no actions/cache step at all, so "
        "every run re-downloads the pinned set it just proved it could "
        "resolve offline (#1501)"
    )
    paths = [_step_input(step, "path") for step in caches]
    with_uv_path = [
        step for step in caches if _UV_CACHE_PATH in _step_input(step, "path")
    ]
    assert with_uv_path, (
        f"no cache step in the `{_CRAWDAD_JOB}` job caches "
        f"{_UV_CACHE_PATH}; found paths {paths}. The flow installs with "
        "uv, so caching pip's directory caches nothing (#1501)"
    )
    keys = [_step_input(step, "key") for step in with_uv_path]
    assert any(_references_lock_hash(key) for key in keys), (
        f"no uv cache key hashes crawdad/uv.lock; got {keys}. Keying on "
        "crawdad/pyproject.toml is the trap: a relock changes uv.lock "
        "and leaves pyproject.toml untouched, so the key never turns "
        "over and the job restores the pre-relock resolution (#1501)"
    )
