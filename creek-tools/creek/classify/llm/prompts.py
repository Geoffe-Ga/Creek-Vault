"""Prompt construction for LLM-based fragment classification (FEAT-017).

Holds the canonical two-step classification prompt template, the
input-sanitisation helpers used to neutralise fragment-level injection
vectors, and the prompt-formatting entry point consumed by the
orchestrator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify import few_shot
from creek.generate.indexes import CANONICAL_FREQUENCY_NAMES, FREQUENCY_THEMES
from creek.models import Frequency

if TYPE_CHECKING:
    from creek.config import LLMConfig
    from creek.models import Fragment


def _build_frequency_block() -> str:
    """Render the frequency dimension lines for the classification prompt.

    Pairs each canonical APTITUDE name (per §6.1 of the ontology spec)
    with the matching Core Theme gloss. Sourcing the glosses from
    :data:`creek.generate.indexes.FREQUENCY_THEMES` and the names from
    :data:`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES` keeps the
    prompt header in lockstep with the rest of the toolchain — see
    ONTOLOGY-001 / ONTOLOGY-002 and the
    ``docs/decisions/2026-05-23-frequency-naming.md`` record.

    Returns:
        A newline-joined list of indented ``- F<n>: Name — Gloss`` lines
        ready for substitution into :data:`CLASSIFICATION_PROMPT`.
    """
    lines = [
        (
            f"   - {freq.value}: {CANONICAL_FREQUENCY_NAMES[freq]}"
            f" — {FREQUENCY_THEMES[freq]}"
        )
        for freq in Frequency
        if freq is not Frequency.UNCLASSIFIED
    ]
    return "\n".join(lines)


_FREQUENCY_BLOCK: str = _build_frequency_block()
"""Pre-rendered frequency-dimension lines baked into the prompt template."""


CLASSIFICATION_PROMPT: str = """\
You are a classification assistant for the Creek knowledge organization system.

Given a fragment of content, classify it along the following dimensions:

1. **Frequency** (APTITUDE F1-F10): Which frequency best describes the content?
__FREQUENCY_BLOCK__

2. **Wavelength Phase**: rising, peaking, withdrawal, diminishing, \
bottoming_out, restoration

3. **Engagement Mode**: inhabit, express, collaborate, integrate, absorb

4. **Orientation**: do, feel, do_feel

5. **Dosage**: medicine, toxic, ambiguous

6. **Voice Register**: confessional, analytical, playful, prophetic, \
instructional, raw, conversational

7. **Confidence**: musing, exploring, forming, settled, conviction

A few labelled examples (do not include these in your YAML output):

{examples}

Two-step protocol (FEAT-017):

Step 1 — Reason briefly. Walk through which frequency this most resonates
with and why, then which phase, then which mode, orientation, and dosage,
and finally the voice register. Two to four short sentences total.

Step 2 — Emit your classification as valid YAML inside a fenced
```yaml ... ``` block. The YAML must follow this exact schema (do not
include any other keys; ``confidence_scores`` reports how certain you
are about each gated dimension, on a 0.0-1.0 scale):

```yaml
frequency:
  primary: F3
  secondary: [F5]
wavelength:
  phase: rising
  mode: express
  orientation: do
  dosage: medicine
voice:
  voice_register: analytical
  confidence: forming
confidence_scores:
  mode: 0.7
  orientation: 0.7
  dosage: 0.7
```

If a dimension is genuinely unclear, set ``unclassified`` rather than
guessing. For Mode / Orientation / Dosage we will treat any
``confidence_scores`` entry below {threshold:.2f} as ``unclassified``,
so calibrate honestly.

Fragment title: {title}
Fragment content:
{content}
""".replace("__FREQUENCY_BLOCK__", _FREQUENCY_BLOCK)
"""Prompt template for two-step LLM-based fragment classification (FEAT-017).

Placeholders:

- ``{examples}``: rendered few-shot block from :mod:`creek.classify.few_shot`.
- ``{threshold}``: ``LLMConfig.unclassified_threshold`` (decimal, two
  decimals) shown to the model so honesty about uncertainty is
  rewarded rather than penalised.
- ``{title}``, ``{content}``: the fragment being classified, after
  :func:`_sanitise_for_prompt` neutralises injection vectors.
"""


_MAX_PROMPT_CONTENT_CHARS: int = 8192
"""Cap on fragment content length before injection into the prompt (~8 KiB)."""


_INJECTION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("---", "[FENCE]"),
    ("<!--", "[CMT-OPEN]"),
    ("-->", "[CMT-CLOSE]"),
)
"""Substrings neutralised in fragment title/content before prompt formation.

The first entry replaces YAML document separators, which an attacker
could otherwise use to forge a second document the parser might
accept. The second/third entries replace HTML comment delimiters,
which can hide instructions from a casual reader while still being
visible to the LLM.
"""


def _sanitise_for_prompt(text: str) -> str:
    """Neutralise injection vectors in untrusted text before prompt formatting.

    Replaces YAML-document separators and HTML-comment delimiters with
    inert placeholders, then truncates the result to
    :data:`_MAX_PROMPT_CONTENT_CHARS` so an oversized fragment cannot
    push the model's response over its context window.

    Args:
        text: Raw, attacker-controlled fragment title or content.

    Returns:
        A sanitised, length-capped copy safe to ``str.format`` into the
        classification prompt template.
    """
    cleaned = text
    for needle, placeholder in _INJECTION_REPLACEMENTS:
        cleaned = cleaned.replace(needle, placeholder)
    if len(cleaned) > _MAX_PROMPT_CONTENT_CHARS:
        cleaned = cleaned[:_MAX_PROMPT_CONTENT_CHARS]
    return cleaned


def build_classification_prompt(
    config: LLMConfig,
    fragment: Fragment,
    content: str = "",
) -> str:
    """Build the FEAT-017 two-step classification prompt for a fragment.

    The fragment title and content are attacker-controlled (they
    originate from third-party messages, scraped material, etc.).
    Each is passed through :func:`_sanitise_for_prompt` to neutralise
    YAML-fence and HTML-comment injection and to cap the body length
    before substitution — see SEC-004.

    A deterministic-per-fragment few-shot block is rendered from
    :mod:`creek.classify.few_shot` and substituted into the
    ``{examples}`` placeholder so the model sees worked examples
    before classifying the new fragment. The threshold value is
    shown to the model so it has an incentive to report honest
    per-dimension confidence rather than pad numbers up.

    Args:
        config: LLM configuration carrying the unclassified threshold
            that is shown to the model so it can calibrate its own
            self-reported confidence honestly.
        fragment: The fragment to classify.
        content: Optional content text for the fragment.

    Returns:
        The formatted prompt string.
    """
    safe_title = _sanitise_for_prompt(fragment.title)
    body = content or "(no content provided)"
    safe_content = _sanitise_for_prompt(body)
    samples = few_shot.sample_examples(fragment.id)
    examples_block = few_shot.render_block(samples) or "(no examples bundled)"
    return CLASSIFICATION_PROMPT.format(
        examples=examples_block,
        threshold=config.unclassified_threshold,
        title=safe_title,
        content=safe_content,
    )
