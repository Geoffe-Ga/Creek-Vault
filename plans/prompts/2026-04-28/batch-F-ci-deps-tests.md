# Batch F — CI, dependencies, and test rigour

## Role

You are a release engineer who treats "declared but unenforced" gates as worse than no gate at all. You align local scripts, pre-commit hooks, and CI workflows on a single source of truth. You write end-to-end tests that would have caught the bugs review found.

## Goal

Bring CI, dependencies, and test rigour up to what the project documents. Eliminate `continue-on-error: true` and `|| true` from quality gates; make MyPy strict in CI; reorganise dependencies so `pip install -e .` works without `requirements.txt`; add e2e tests, per-file coverage minimums, property-based tests, and failure-mode fixtures; align pre-commit and CI on the same toolset.

## Context

Independent of every other batch — but the e2e tests added here will be the safety net catching regressions during Batches A–E. **Land the test infrastructure changes (TEST-001, TEST-002) before or alongside Batch A so the e2e tests fail in the way they should during the rebuild.**

`creek-tools/CLAUDE.md` advertises maximum-quality engineering with specific thresholds. CI today has multiple gates that pass even when they fail. `requirements.txt` and `pyproject.toml` disagree about which deps are required vs optional. The 6 integration tests and 0 e2e tests didn't catch the Critical bugs in this review.

**Read these issue files before starting** (in `plans/git-issues/`):
- `CI-001-mypy-not-strict-in-ci.md`
- `CI-002-pylint-and-complexity-checks-non-blocking.md`
- `CI-003-pytest-runs-integration-and-e2e.md`
- `CI-004-pre-commit-vs-ci-drift.md`
- `DEP-001-anthropic-missing-from-pyproject.md`
- `DEP-002-lazy-optional-deps-actually-required.md`
- `DEP-003-pip-audit-cves-in-transitives.md`
- `TEST-001-no-end-to-end-pipeline-tests.md`
- `TEST-002-coverage-aggregate-hides-low-modules.md`
- `TEST-003-mock-tautology-tests.md`
- `TEST-004-failure-mode-fixtures-thin.md`
- `TEST-005-no-property-based-tests.md`
- `STYLE-001-refurb-and-tryceratops-violations.md`
- `STYLE-002-claude-md-skill-and-adr-paths-stale.md`

**Files you will primarily change:**
- `.github/workflows/ci.yml`
- `creek-tools/pyproject.toml` (extras)
- `creek-tools/requirements.txt`, `requirements-dev.txt`
- `creek-tools/scripts/check-all.sh` (and sub-scripts)
- `creek-tools/.pre-commit-config.yaml`
- `creek-tools/CLAUDE.md` (align thresholds and paths)
- `creek-tools/tests/e2e/` (new), `tests/test_properties_*.py` (new), `tests/fixtures/` (extend)

## Output format

A series of focused PRs/commits. Suggested order:

1. **CI gates blocking** — drop `|| true` and `continue-on-error: true` from MyPy, pylint, xenon. Replace inline tool invocations with `./scripts/check-all.sh`. Pylint threshold: 9.0 (or update CLAUDE.md to whatever number you actually enforce).
2. **Dependency reorganisation** — move lazy deps to `[project.optional-dependencies]` extras (`anthropic`, `embeddings`, `ocr`, `documents`, `spreadsheets`, `presentations`, `gdrive`, `all`). Make `import anthropic` lazy inside `creek/classify/llm.py`. Drop or shrink `requirements.txt`.
3. **pip-audit alignment** — bump fixable transitives (`cryptography`, `pyjwt`, `setuptools`, `wheel`); add documented `--ignore-vuln` for the unfixable ones to both CI and `scripts/security.sh`.
4. **Per-file coverage threshold** — 80% per file as a second gate (aggregate stays at 90%). A small CI step reads `coverage.json` and fails if any file falls below.
5. **E2E test scaffolding** — `tests/e2e/` with a shared fixture for "synthetic vault + synthetic source dir" and the canonical e2e tests called out in TEST-001 (markdown round-trip, redaction, idempotency, consent, purge, classify-review). Mark `@pytest.mark.e2e`.
6. **Property-based tests** — `tests/test_properties_id.py`, `test_properties_frontmatter.py`, `test_properties_redaction.py` using Hypothesis. Bound at `@settings(max_examples=200)`.
7. **Failure-mode fixtures** — `tests/fixtures/{corrupt,encoding,injection,scale}/` with at least one example per category. Add corresponding `tests/test_*_failure_modes.py`.
8. **Pre-commit / CI alignment** — pre-commit invokes `./scripts/check-all.sh` (or sub-scripts); CI invokes the same. `refurb`, `tryceratops`, `vulture`, `shellcheck`, `detect-secrets` move into `check-all.sh` so CI runs them too.
9. **Mock-tautology cleanup** — sweep `tests/` for `\.call_count` / `assert mock_*\.called` and either justify with a comment or replace with a behaviour assertion.
10. **Doc alignment (STYLE-002)** — `creek-tools/CLAUDE.md` paths and thresholds match reality. Either create at least one ADR under `docs/architecture/ADR/` or remove the claim.

## Examples

The per-file coverage gate:

```yaml
- name: Verify per-file coverage thresholds
  working-directory: creek-tools
  run: |
    python -c "
    import json, sys
    data = json.load(open('reports/coverage.json'))
    threshold = 80.0
    failed = [
        (f, summary['percent_covered'])
        for f, summary in data['files'].items()
        if summary['percent_covered'] < threshold
    ]
    if failed:
        for f, c in failed:
            print(f'FAIL {c:.2f}% < {threshold}%: {f}')
        sys.exit(1)
    "
```

The hypothesis test for deterministic IDs:

```python
from hypothesis import given, strategies as st
from datetime import datetime, timezone
from creek.ingest.base import generate_fragment_id

@given(source=st.text(min_size=1), ts=st.datetimes(timezones=st.just(timezone.utc)),
       content=st.text(min_size=1))
def test_fragment_id_deterministic(source, ts, content):
    a = generate_fragment_id(source, ts, content)
    b = generate_fragment_id(source, ts, content)
    assert a == b
    assert a.startswith("frag-")
    assert len(a.removeprefix("frag-")) == 12
```

The CI doc-alignment for CLAUDE.md:

```diff
-- **Pylint Score: ≥9.0**
+- **Pylint Score: ≥9.0** (enforced by `pylint creek/ --fail-under=9.0` in CI)
```

## Requirements

- **Use `/stay-green`**: each test added (e2e, property, failure-mode) is Gate 1 — it must fail when the underlying bug exists and pass when it's fixed. Do not write tests that pass against today's broken behaviour.
- **Use `/max-quality-no-shortcuts`**: if a CI gate is "too noisy", fix the noise (refactor / add tests) rather than re-add `|| true`. If a property-based test finds a real edge case, file a sub-issue rather than narrow the input strategy.
- For dependency moves: confirm `pip install -e .` works in a fresh venv, and `pip install -e .[all]` works for the full optional set. CI should install `[all]` to keep coverage of optional code paths.
- Lazy imports for `anthropic`, `pdf2image`, `pytesseract`, `python-docx`, `pdfminer`, `googleapiclient`, `pptx`, `openpyxl`, `sentence_transformers`, `numpy` already exist in places — verify each, and surface a clear "install with `pip install creek-tools[<extra>]`" message when import fails.
- The e2e tests must use real disk I/O against `tmp_path`, not mocks. The whole point is that they would have caught BUG-001 and BUG-008 before review.
- Property-based tests run in <30s in CI (`@settings(max_examples=200, deadline=None)`).
- Failure-mode fixtures must not contain real secrets. Use `AKIAIOSFODNN7EXAMPLE`, the documented Stripe test keys, etc.
- Maintain `mypy --strict` clean across the new test files (relax via the existing `[[tool.mypy.overrides]] module = "tests.*"` but don't widen it).
- `./scripts/check-all.sh` is the single source of truth. After this batch, both pre-commit and CI invoke it (with appropriate `--lite` / `--full` modes if needed).

## Definition of done

`./scripts/check-all.sh` exits 0 locally. CI fails on a deliberately-broken type annotation, a deliberately-introduced cyclomatic complexity violation, and a deliberately-removed test. `pip install -e .` in a fresh venv runs every command that doesn't need optional deps; `pip install -e .[all]` is what CI installs. The 6 e2e tests pass against a clean vault. CLAUDE.md and the docs match reality.
