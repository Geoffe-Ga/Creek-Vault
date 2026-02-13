---
name: bug-squashing-methodology
description: >-
  Structured 5-step bug fix process with root cause analysis and TDD.
  Use when fixing bugs, debugging failures, or investigating defects.
  Covers RCA documentation, reproduction, TDD fix cycle, and PR workflow.
  Do NOT use for general feature development (use stay-green), CI
  environment issues (use ci-debugging), or code reviews.
metadata:
  author: Geoff
  version: 1.0.0
---

# Bug Squashing Methodology

Systematic process for fixing bugs: Document, Understand, Fix, Verify. Never skip straight to coding.

## Instructions

### Step 1: Root Cause Analysis (RCA)

Create `RCA_ISSUE_XXX.md` with:
- **Problem Statement**: Error message, reproduction steps
- **Root Cause**: Exact line/logic causing failure
- **Analysis**: Why it happens, what was confused/wrong
- **Impact**: Severity, scope, frequency
- **Contributing Factors**: Why wasn't it caught earlier?
- **Fix Strategy**: Options with recommendation
- **Prevention**: How to avoid similar bugs

### Step 2: File a GitHub Issue

```bash
gh issue create --title "bug(component): Brief description" \
  --body "Reproduction steps, root cause summary, proposed fix" \
  --label "bug"
```

### Step 3: Branch and Fix with TDD

```bash
git checkout -b fix-component-issue-XXX
```

1. **Red**: Write a test that reproduces the bug (should fail)
2. **Green**: Write minimal code to fix the bug (test passes)
3. **Refactor**: Clean up the fix while keeping tests green

### Step 4: Quality Gates

Run both gates before committing:

```bash
# Gate 1: All tests pass
./scripts/test.sh --all

# Gate 2: All quality checks pass
pre-commit run --all-files
```

### Step 5: Commit and PR

Use conventional commit format: `fix(component): brief description (#XXX)`

Include in commit body:
- Problem description (reference RCA)
- Solution description
- Specific file changes
- Testing confirmation (both gates green)

## Examples

### Example 1: Type Validation Bug

**Problem**: API endpoint crashes when receiving single object instead of list.

**RCA**: `src/services/business_logic.py:45` assumes input is always a list. Missing type validation causes runtime error.

**Fix**:
```python
# Red: Write test reproducing the bug
def test_process_handles_single_object():
    result = process_input({"key": "value"})  # Single object, not list
    assert result == [{"key": "value"}]

# Green: Fix the code
def process_input(data):
    if isinstance(data, dict):
        data = [data]
    return [transform(item) for item in data]
```

### Example 2: Off-by-One Boundary Error

**Problem**: Pagination returns duplicate items on page boundaries.

**RCA**: `offset = page * limit` should be `offset = (page - 1) * limit` since pages are 1-indexed.

**Fix**:
```python
# Red: Test that reproduces duplicate
def test_pagination_no_duplicates():
    page1 = get_items(page=1, limit=10)
    page2 = get_items(page=2, limit=10)
    assert not set(page1) & set(page2)  # No overlap

# Green: Fix offset calculation
def get_items(page: int, limit: int):
    offset = (page - 1) * limit  # Fixed: was page * limit
    return db.query(Item).offset(offset).limit(limit).all()
```

## Troubleshooting

### Error: Can't reproduce the bug locally
- Check environment differences (Python version, OS, dependencies)
- Add debug logging at the failure point
- Try running with the exact same data/input as the report

### Error: Fix breaks other tests
- Your fix may have changed shared behavior. Check test assumptions.
- Run the full test suite, not just the new test
- Consider if the other tests were relying on buggy behavior

### Error: RCA seems like overkill for a simple bug
- Spend 10 minutes on RCA even for "obvious" bugs - they're rarely obvious
- The documentation prevents recurrence and builds institutional knowledge
- Skip RCA only for true typos (wrong variable name, missing comma)
