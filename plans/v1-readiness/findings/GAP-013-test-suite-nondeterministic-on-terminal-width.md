# GAP-013 — Unit suite is RED on a fresh checkout because CLI-output assertions depend on unpinned terminal width

- **Severity:** High
- **Prod-readiness criterion threatened:** unattended reliability, doc honesty
- **Status:** Open (untracked — no open issue exists; distinct from the prior GAP-007, which closed the gate-list/Bandit/interrogate drift but not test-output width)

## Summary

Two unit tests assert on substrings of rendered Typer/Rich CLI output. When the
console width is ~80 columns — the Rich default whenever stdout is **not a TTY**
(piping to a file, CI, this analysis environment) or the terminal is exactly 80
wide — Rich wraps the output mid-word, so the asserted substrings span a newline
and the matches fail. The suite goes RED. At a wider width the same tests pass.
CI happens to run at a width above the wrap point, so it is green — which means
the suite's pass/fail status is governed by an **ambient, unpinned terminal
width** rather than by the code under test. This directly contradicts
`creek-tools/CLAUDE.md §2.1`, which promises a fresh checkout reproduces CI's
result.

## Evidence (today's code)

- **The two failing assertions:**
  - `tests/test_purge.py:784` — `assert "does not appear to be a Creek vault" in result.output`. At width 80 the output is `… decoy does\nnot appear to be a Creek vault …` ("does" / "not" split by a wrap newline).
  - `tests/test_pipeline.py:496` — `assert "broken.bin" in result.output`. At width 80 the output is `… source/bro\nken.bin …` ("broken.bin" split mid-token).
- **Confirmed width-dependence (measured this pass):**
  - `./scripts/test.sh --coverage` on a fresh `uv sync` checkout → `2 failed, 4585 passed`; aggregate branch coverage 93.66% (so coverage itself is honest, but the suite exits non-zero).
  - `COLUMNS=200 python -m pytest <the two tests>` → `2 passed`.
  - `python -c "import rich.console as c; print(c.Console().width)"` in this env → `80` (because `os.get_terminal_size()` raises and Rich falls back to 80).
- **CI is green at the same HEAD:** all "Code Quality & Testing (3.11/3.12/3.13)" jobs on the latest commit report `conclusion: success`.
- **No width is pinned anywhere:** grep for `COLUMNS` / `width` / `terminal` in `tests/conftest.py`, `.github/workflows/ci.yml`, and the `[tool.pytest.ini_options]` block of `pyproject.toml` returns nothing.
- **The contract that is violated:** `creek-tools/CLAUDE.md §2.1` — "a fresh checkout runs `./scripts/check-all.sh` to the same result CI does on the same commit." It does not.
- **Broader risk surface:** 14 test files assert on `result.output`; any substring that lands across a wrap boundary at some width is latently fragile. Two surfaced today at width 80; others may surface at other widths.

## Why it matters

The project's entire stated culture is "stay green" and "a fresh checkout
matches CI." A new contributor (or the author on a fresh container) who runs
`./scripts/check-all.sh` and, very commonly, redirects its output to a log
(`./scripts/check-all.sh > out.log 2>&1`) makes stdout a non-TTY, gets width
80, and sees a RED suite on untouched `main`. That destroys trust in the
baseline and makes "is my change the thing that broke it?" unanswerable —
exactly the unattended-reliability failure the prior issue #206 work set out to
eliminate.

## Reproduction

```bash
cd creek-tools
uv sync --all-extras
export PATH="$PWD/.venv/bin:$PATH"
./scripts/test.sh --coverage          # -> 2 failed (red), coverage 93.66%
COLUMNS=200 python -m pytest \
  tests/test_purge.py::test_cli_purge_vault_refuses_non_creek_directory \
  tests/test_pipeline.py::TestPipelineErrorSurfacing::test_cli_process_prints_errors \
  -o addopts="" -q                    # -> 2 passed (green)
```

## Acceptance criteria

- A single, repo-wide mechanism pins the console width for CLI tests (e.g. a
  `conftest.py` autouse fixture setting `COLUMNS`/`LINES`, or constructing the
  Typer `CliRunner` with an explicit width), so the suite's result is identical
  whether stdout is a TTY, a pipe, an 80-column terminal, or CI.
- `./scripts/test.sh` and `./scripts/check-all.sh` exit 0 on a fresh checkout
  when their output is redirected to a file.
- A regression test (or the fixture itself) documents the pinned width so the
  assumption is explicit, not implicit.

## Files affected

`tests/conftest.py` (or a new shared CLI-runner fixture), `tests/test_purge.py`,
`tests/test_pipeline.py`, and any of the 14 `result.output`-asserting test files
that prove fragile once audited. Optionally `creek-tools/CLAUDE.md §2.1` if the
contract is reworded rather than made true.

## Dependencies / blockers

None.
