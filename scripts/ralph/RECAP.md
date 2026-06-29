# Ralph Discord Recap

Whenever a PR merges, this posts a clean Discord embed in two blocks —
`————THIS PR————` (the just-merged PR: its unlock headline, link, footprint,
review iterations, review time, and tick length) and `————THE LOOP————` (the
rolling/all-time figures: busiest day, PRs merged, lines of code, merge rate,
review iterations, review time, tick length, and backlog remaining). Within each
block, multi-window stats run smallest window first (24h → 7d → all-time).

A "Ralph tick loop" is the autonomous loop driven by `/loop /ralph-tick` (see
`.claude/commands/ralph-tick.md`): each tick opens a PR, iterates against the
Claude reviewer's `Verdict:` comment until LGTM, merges, and moves to the next
backlog issue. This tool turns that merge history into a recap.

Adapted from the `discord-ralph-recap` skill in
[`Geoffe-Ga/well-worn-tools` PR #14](https://github.com/Geoffe-Ga/well-worn-tools/pull/14).

## Files

```
scripts/ralph/
├── recap.py             # I/O shell: fetch GitHub data, generate headline, post embed
├── stats.py             # pure, unit-tested statistics math
├── test_recap_stats.py  # tests for stats.py + the backlog filter
└── RECAP.md             # this file
.github/workflows/
├── ralph-recap.yml        # fires on every merged PR → posts the recap
└── ralph-recap-tests.yml  # runs the unit tests on changes under scripts/ralph/
```

## What you need

- `DISCORD_BOT_TOKEN` (repo **secret**) — a Discord bot token. The bot must be
  in the server with **View Channel** + **Send Messages** on the target channel.
- `RALPH_CHANNEL_ID` (repo **secret** or **variable**) — the target channel's
  numeric ID (Developer Mode → right-click channel → Copy Channel ID). The
  workflow reads `secrets` first and falls back to `vars`, so either store works.
- A GitHub token — in Actions the built-in `GITHUB_TOKEN` is enough
  (`contents: read`, `pull-requests: read`, `issues: read`).
- `ANTHROPIC_API_KEY` (repo **secret**, optional) — enables the Claude-written
  ten-word headline. Without it the headline falls back to a cleaned PR title
  and nothing else changes.

## Setup

The workflow is keyed on `pull_request: types: [closed]`, gated to merges with
`if: github.event.pull_request.merged == true`. (`closed` fires on both merge
and plain close; the guard keeps it to real merges. A `push: branches: [main]`
trigger would wrongly fire on hotfixes and reverts too.)

Wire the secrets once (`RALPH_CHANNEL_ID` can be a `gh variable set` instead —
the workflow accepts either):

```bash
gh secret set DISCORD_BOT_TOKEN
gh secret set RALPH_CHANNEL_ID    # or: gh variable set RALPH_CHANNEL_ID --body "1234..."
gh secret set ANTHROPIC_API_KEY   # optional, for the headline
```

The next merged PR triggers the first recap. Each post is self-contained — no
state is stored between runs; every recap is recomputed from the live history.

## Dry-run / one-off from the terminal

`--dry-run` prints the embed JSON instead of posting it:

```bash
DISCORD_BOT_TOKEN=unused RALPH_CHANNEL_ID=unused \
GITHUB_TOKEN="$(gh auth token)" \
python scripts/ralph/recap.py --repo Geoffe-Ga/adepthood --dry-run
```

Drop `--dry-run` (with a real `DISCORD_BOT_TOKEN` / `RALPH_CHANNEL_ID`) to post
once manually — handy for backfilling after enabling the bot mid-campaign.

## Metric definitions

All math is pure and unit-tested in `stats.py`; all I/O is in `recap.py`.

The headline **PRs merged** total is all-time; the activity and quality stats
below cover a trailing **7-day window** (`RECENT_WINDOW_DAYS`) so they move with
recent work instead of being diluted by a frozen lifetime average. The window is
fetched via the search API (`is:pr is:merged merged:>=<date>`), capped at
`--max-prs` (default 200) as a safety bound on a burst day.

- **PRs merged** — "in 24h" and "in 7d" counts from the window, then the true
  cumulative all-time count from the search API (`total_count`), which is
  independent of any per-run fetch cap (smallest window first).
- **LoC** — lines-of-code churn as `+additions / -deletions` over the trailing
  24h and 7d windows (summed from each window PR's detail call), then the
  **full-repo net** from GitHub's code-frequency stats API
  (`/stats/code_frequency`, additions − deletions across all history). The stats
  endpoint warms asynchronously and answers 202 on a cold cache, so the full-repo
  figure retries a few times and falls back to "—" if still unavailable.
- **Merge rate** — rolling windows: merges in the last 24h as **per-hour**, and
  merges in the last 7 days as **per-day**. Fixed-width windows move every recap
  and decay toward zero when the loop idles. The 7-day per-day figure (steadier
  than the 24h one) is what the backlog ETA is built on.
- **Review iterations before LGTM** (7d) — per merged PR in the window, issue
  comments are read oldest-first and a comment counts as a verdict only if it
  contains `VERDICT` (case-insensitive), matching `iteration-trigger.yml`. The
  count is the number of non-LGTM verdicts preceding the first LGTM. PRs that
  never reached an LGTM verdict are excluded. The embed shows avg rounds,
  first-try-clean %, worst, and the sample size `n`.
- **Time to merge · opened → merged** (7d) — `merged_at - created_at` per PR:
  the review/merge window. Clamped to zero so clock skew can't go negative.
  Median, fastest, slowest. (A PR's first-commit timestamp is deliberately not
  used: for single-commit squash PRs the commit is authored seconds before the
  PR opens, so "first commit → merge" collapses to this same window.)
- **Tick cadence · merge → merge** (7d) — the gap between consecutive merges
  (`stats.merge_intervals_hours`). For a sequential loop this is the closest
  proxy for how long each tick actually took end to end (pick → code → review →
  merge), since commit timestamps only mark when work was committed, not begun.
  Median, fastest, slowest; reads "not enough merges in the window yet" below two
  merges.
- **Backlog remaining and ETA** — `open_items / per_day` as days and a date.
  When the rate is zero the ETA reads "unknown (stalled)"; an empty backlog
  reads "backlog clear".
- **Busiest day** (7d) — the UTC calendar day in the window with the most merges.

The embed groups fields into two labelled blocks so a single merge's numbers are
never confused with the dataset-wide ones. The `————THIS PR————` block holds:

- **Unlock** — the ten-word "what this merge unlocked" headline (see below).
- **Link** — the PR number, title, and URL.
- **Footprint** — additions/deletions/changed-files for the just-merged PR (the
  list endpoint omits diff stats, so it's fetched via the single-PR detail
  endpoint).
- **Review iterations** — this PR's own rounds before its first LGTM verdict
  (`0` reads "clean first try"; a PR merged without an LGTM verdict says so).
- **Time for review** — this PR's own `opened → merged` window.
- **Tick length** — the gap from the previous merge, the per-PR analogue of the
  tick cadence (`first tracked merge` when there is no prior merge in the window).

- **The ten-word headline** — `generate_headline` asks `claude-opus-4-8`
  (effort `low`) for a plain-language headline of what the latest merge
  unlocked, and degrades to the cleaned PR title on any SDK/API absence or
  error, so the recap never fails on the headline.

## adepthood-specific tuning

The **backlog count is the metric most worth checking**, and this port already
adapts it. `count_open_backlog` counts open issues (PRs excluded) minus any
issue bearing one of the picker's exclude labels — epics, `blocked`,
`needs-spec`, `do-not-auto-merge`, etc. — so the ETA reflects what
`scripts/ralph/pick-next.sh` would actually pick up, not every open card. The
exclude set defaults to the same list as the picker and honors the same
`RALPH_EXCLUDE_LABELS` env var; set it (space-separated) to override, or to an
empty string to count every open issue.

If the reviewer ever phrases verdicts differently from the `Verdict:` line that
`claude-code-review.yml` posts, update `normalize_verdict` in `stats.py`.

## Tests

```bash
python -m pytest scripts/ralph -q
```

CI runs the same suite via `.github/workflows/ralph-recap-tests.yml` on any
change under `scripts/ralph/`.

## Troubleshooting

- **Discord 401 Unauthorized** — wrong/revoked token. Use the **bot** token
  (Bot tab), and don't prefix it with `Bot ` (the script adds that).
- **Discord 403 Forbidden** — the bot isn't in the server, or lacks View
  Channel / Send Messages on `RALPH_CHANNEL_ID`.
- **Discord 404 Not Found** — `RALPH_CHANNEL_ID` is wrong. It's an all-digits
  snowflake (Copy Channel ID), not the channel name.
- **Headline is just the PR title** — `ANTHROPIC_API_KEY` is unset or the SDK
  isn't installed (the workflow installs it).
- **"no LGTM verdicts found yet"** — no merged PR has a Claude review comment
  containing a `Verdict` line yet.
- **ETA "unknown (stalled)"** — no merges in the window, so no rate to
  extrapolate; resolves once merges resume.
