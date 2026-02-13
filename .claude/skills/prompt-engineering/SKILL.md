---
name: prompt-engineering
description: >-
  Transform vague requests into effective 6-component prompts.
  Use when crafting prompts for AI agents, writing plan files,
  delegating tasks, or when Claude's responses miss the mark.
  Covers role, goal, context, format, examples, and constraints.
  Do NOT use for direct code implementation or testing.
metadata:
  author: Geoff
  version: 1.0.0
---

# Prompt Engineering

Transform vague requests into effective, actionable prompts using the 6-component framework.

## Instructions

### Step 1: Identify Missing Components

Check the prompt against the 6-component checklist:
1. **Role or Persona** - Who the AI should be
2. **Goal / Task Statement** - Exactly what you want done
3. **Context or References** - Key data the model needs
4. **Format or Output Requirements** - How you want the answer
5. **Examples or Demonstrations** - Show, don't just tell
6. **Constraints / Additional Instructions** - Boundaries that improve quality

Missing 3+ items? Rewrite the prompt.

### Step 2: Rewrite with All Components

Transform the vague request into a structured prompt with all 6 components present. Be concise but complete.

### Step 3: Apply to Plan Files

All files in `plan/` should follow this structure:
```markdown
## Role
You are a [specific role with relevant expertise].

## Goal
[Specific, measurable task with clear success criteria]

## Context
- Current state, problem, constraints, file references

## Output Format
[Specify structure: numbered steps, code blocks, etc.]

## Examples
[Concrete examples of what you want]

## Requirements
- [Specific constraints]
```

## Examples

### Example 1: Code Review Request

**Ineffective**: "Review this code."

**Effective**:
```
Role: Senior Python engineer focused on code quality.

Task: Review the FastAPI endpoint in src/api/users.py for:
- Security vulnerabilities
- Performance issues
- Code style violations

Context: User registration endpoint handling ~1000 requests/day.

Format: Numbered list with issue description, severity
(Critical/Medium/Low), line number, and recommended fix.

Example:
1. SQL Injection Risk (Critical)
   Line 45: Direct string interpolation in query
   Fix: Use parameterized queries

Constraints:
- Only flag issues with confidence > 80%
- Skip minor style issues if functionality is correct
```

### Example 2: Bug Fix Plan

**Ineffective**: "Help me fix the login bug."

**Effective**:
```
Role: Backend debugging specialist with FastAPI expertise.

Task: Create root cause analysis and fix plan for login failures.

Context:
- Error: "401 Unauthorized" on POST /auth/login
- Happens intermittently (30% of requests)
- Started after deploying commit abc123
- JWT token validation in src/auth/jwt.py

Format: Markdown with Root Cause, Reproduction Steps,
Fix Strategy, and Testing Plan sections.

Constraints:
- Must include specific file:line references
- Fix must maintain backward compatibility
- Include regression test cases
```

## Troubleshooting

### Error: AI responses are too vague or miss the mark
- Add concrete examples showing exactly what good output looks like
- Specify the output format explicitly (table, numbered list, code block)
- Add constraints to narrow the scope

### Error: Prompt is getting too long
- Move reference data to separate files and point to them
- Use "See Also" references instead of inlining everything
- Focus on the 3 most important components for your use case
