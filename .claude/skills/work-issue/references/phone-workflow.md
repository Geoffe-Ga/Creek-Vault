# Phone Workflow Guide

Optimizations for running work-issue from a mobile device via Claude Code web or app.

## Minimal Input Required

The skill is designed for single-command invocation:

```
/work-issue 28
```

That's it. The agent handles everything else autonomously.

## What Happens Automatically

1. Issue is read from GitHub (no need to paste the issue text)
2. Branch is created and checked out
3. Codebase is explored for context
4. Tests are written first (TDD)
5. Implementation follows tests
6. Quality gates are run and iterated
7. Self-review is performed
8. PR is created with proper description
9. CI is monitored

## When the Agent Will Ask You

The agent operates autonomously but will ask you when:
- An issue dependency is not satisfied (should it proceed anyway?)
- A design decision has multiple valid approaches (which do you prefer?)
- Self-review found an ethics concern that needs human judgment
- CI keeps failing after 3 fix attempts

## Status Updates

The agent uses the TodoWrite tool to show progress. You'll see:
- Current step (e.g., "Writing tests for EmbeddingLinker")
- Completed steps with checkmarks
- Any blockers or questions

## Batch Workflow (Multiple Issues)

To work on multiple issues in parallel, launch separate agents:

```
/work-issue 28    # Agent 1: Embedding generation
/work-issue 30    # Agent 2: Temporal linking (different files, no conflict)
/work-issue 79    # Agent 3: Discord filter (different files, no conflict)
```

Only batch issues from the same sub-group in the execution plan (see batch analysis).
Issues in different sub-groups touch different files and are safe to parallelize.

## Reviewing from Phone

After the PR is created:
1. You'll get the PR URL
2. CI runs automatically
3. Claude Code Review runs automatically
4. If both are green: merge from GitHub mobile app
5. If changes requested: the agent can address them if still running

## Recovery

If the session disconnects:
1. The branch and any commits are preserved on the remote
2. Start a new session and say: "Continue work on issue #28 on branch feat/28-embedding-generation"
3. The agent will pick up where it left off
