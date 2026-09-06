## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
Cover the degenerate-SDK-response fallbacks in `creek/classify/llm/providers.py`
across all three cloud backends — Anthropic `usage=None`, OpenAI empty
`choices` / non-string `finish_reason` / absent `usage`, Gemini empty
`candidates` / `None` `finish_reason` / absent `usage_metadata` — plus the
missing-API-key `RuntimeError` and the enclave non-dict-quote rejection,
raising the module from 93.32%.

## Context
- File(s): `creek-tools/creek/classify/llm/providers.py` — `_require_env`
  (`:133-134`), `_extract_anthropic_usage` (`:612`),
  `_fetch_attestation_quote` (`:850`), enclave payload options (`:981`,
  `:983`), `_extract_openai_text` (`:1316`), `_map_openai_stop_reason`
  (`:1333`, `:1336`), `_extract_openai_usage` (`:1352`),
  `_extract_gemini_text` (`:1563`), `_map_gemini_stop_reason` (`:1589`,
  `:1592`), `_extract_gemini_usage` (`:1610`).
- Scanned at commit: `c8c5131b4e9afc4e3962d976eaecc8dfb89ba919` — re-verify against HEAD before starting
- Evidence: `./scripts/coverage.sh` reports `creek/classify/llm/providers.py`
  at **93.32%**. Uncovered behavioral lines and branches:

  ```
  lines:    133, 134, 612, 850, 981, 983, 1316, 1333, 1336, 1352, 1563, 1589,
            1592, 1610
  branches: 132->133, 611->612, 849->850, 980->981, 982->983, 1205->1222,
            1315->1316, 1332->1333, 1335->1336, 1351->1352, 1356->1358,
            1358->1360, 1472->1481, 1562->1563, 1569->1567, 1588->1589,
            1591->1592, 1609->1610, 1614->1616, 1616->1618
  ```

  Every extractor's "the SDK gave us nothing usable" arm is unproven. The tests
  in `tests/test_openai_provider.py` / `tests/test_gemini_provider.py` build
  well-formed fakes only, so the fallbacks are dead-on-arrival if an SDK
  version ever returns an empty envelope. At the scanned SHA:

  ```python
  # providers.py:1314-1316 (OpenAI text)   — and mirrored at 1561-1563 (Gemini)
  choices = getattr(response, "choices", None) or []
  if not choices:
      return ""                    # <-- 1316, uncovered

  # providers.py:1331-1336 (OpenAI stop reason)
  if not choices:
      return "end_turn"            # <-- 1333, uncovered
  reason = getattr(choices[0], "finish_reason", None)
  if not isinstance(reason, str):
      return "end_turn"            # <-- 1336, uncovered

  # providers.py:1350-1352 (OpenAI usage) — and 610-612 (Anthropic), 1608-1610 (Gemini)
  usage = getattr(response, "usage", None)
  if usage is None:
      return None                  # <-- 1352, uncovered
  ```

  Also uncovered: the partial-usage branches (`1356->1358`, `1358->1360`,
  `1614->1616`, `1616->1618`) — i.e. a response carrying only
  `prompt_tokens` but no `completion_tokens`, which the docstring at
  `:606-608` explicitly promises "never raises".
- Related: #994 (`[scan:coverage] crawdad/llm/openai.py at 78.57%
  (gemini.py at 83.82%): degenerate-response fallbacks untested`) is the **same
  class of gap in the separate `crawdad/` package** — this issue is
  `creek-tools/creek/classify/llm/providers.py` and is not a duplicate, but
  reuse the fake-response helpers if #994 lands first. See also #1046
  (Anthropic adapter never populates `Completion.usage`). Sibling findings from
  this scan run: #1446, #1447, #1448.

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
One parametrized table covers the three backends symmetrically, since the
extractors are structurally identical:

```python
class _EmptyResponse:
    """An SDK response whose envelope carries no choices/candidates/usage."""
    choices: list[object] = []
    candidates: list[object] = []

@pytest.mark.parametrize(
    ("extract_text", "map_stop", "extract_usage"),
    [
        (_extract_openai_text, _map_openai_stop_reason, _extract_openai_usage),
        (_extract_gemini_text, _map_gemini_stop_reason, _extract_gemini_usage),
    ],
)
def test_degenerate_response_falls_back(extract_text, map_stop, extract_usage):
    """An empty envelope yields "", "end_turn", None — never an exception."""
    resp = _EmptyResponse()
    assert extract_text(resp) == ""            # covers 1316, 1563
    assert map_stop(resp) == "end_turn"        # covers 1333, 1589
    assert extract_usage(resp) is None         # covers 1352, 1610
```

Remaining cases: a choice whose `finish_reason` is an int rather than a str
(1336) and a Gemini candidate whose `finish_reason` is `None` (1592); a usage
object carrying only `prompt_tokens` (assert `{"input_tokens": N}` with no
`output_tokens` key — 1356->1358, 1614->1616); Anthropic `usage=None` (612);
`_require_env` with the var unset and with it set to `"   "` (assert the
`RuntimeError` message names the env var — 133-134); `_fetch_attestation_quote`
returning a JSON list instead of an object (assert `EnclaveAttestationError` —
850); an enclave call with `max_tokens` and with `system` supplied (assert both
land in the posted payload — 981, 983).

## Constraints
- Do not change public API signatures unless the Goal says so
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Scope: this issue only — file follow-up issues for adjacent problems
- These are unit tests over fake response objects: do NOT add tests that
  require a real API key or network, and do not touch the `live` lane
- Assert the exact fallback value (`""`, `"end_turn"`, `None`), not merely that
  the call did not raise
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
