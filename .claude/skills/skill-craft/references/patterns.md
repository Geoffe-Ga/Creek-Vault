# Skill Design Patterns

Patterns that consistently produce effective skills, derived from Anthropic's guide and production experience.

## Pattern 1: Sequential Workflow Orchestration

**Use when**: Users need multi-step processes in a specific order.

```markdown
## Workflow: [Task Name]

### Step 1: [Action]
[What to do, what tool to call, what parameters]

### Step 2: [Action]
[Dependencies on Step 1 output]

### Step 3: [Validation]
[How to verify the workflow succeeded]
```

Key techniques:
- Explicit step ordering with dependencies between steps
- Validation at each stage
- Rollback instructions for failures

## Pattern 2: Iterative Refinement

**Use when**: Output quality improves with iteration.

```markdown
### Initial Draft
1. Generate first version
2. Save to temporary location

### Quality Check
1. Run validation (script or checklist)
2. Identify issues

### Refinement Loop
1. Address each issue
2. Regenerate affected sections
3. Re-validate
4. Repeat until quality threshold met

### Finalization
1. Apply final formatting
2. Generate summary
```

Key techniques:
- Explicit quality criteria (what "done" looks like)
- Validation scripts where possible (deterministic > language instructions)
- Clear stopping conditions

## Pattern 3: Context-Aware Selection

**Use when**: Same goal, different approach depending on context.

```markdown
### Decision Tree
1. Assess context: [what to check]
2. Determine approach:
   - If [condition A]: [approach A]
   - If [condition B]: [approach B]
   - Default: [safe fallback]

### Execute
Based on decision, follow the appropriate path.

### Explain
Tell the user why that approach was chosen.
```

Key techniques:
- Clear decision criteria
- Fallback options
- Transparency about choices

## Pattern 4: Domain-Specific Intelligence

**Use when**: The skill adds specialized knowledge beyond tool access.

```markdown
### Before Action (Domain Check)
1. Gather context
2. Apply domain rules:
   - [Rule 1]
   - [Rule 2]
3. Document decision

### Execute
IF checks passed: proceed
ELSE: flag for review, explain why

### Audit Trail
- Log all decisions
- Record reasoning
```

Key techniques:
- Domain expertise embedded in logic
- Validation before action
- Comprehensive documentation of decisions

## Description Field Patterns

### Good Descriptions (with analysis)

```yaml
# Pattern: [Verb phrase] + [Trigger phrases] + [Capabilities] + [Exclusions]
description: >-
  Analyzes Figma design files and generates developer handoff documentation.
  Use when user uploads .fig files, asks for "design specs", "component
  documentation", or "design-to-code handoff".
  Do NOT use for general design discussion (use design-review skill).
```

Why this works:
- Starts with what it does (analyzes Figma files)
- Includes 4 specific trigger phrases
- Mentions file types (.fig)
- Has exclusions with cross-reference

### Bad Descriptions (with fixes)

| Bad | Problem | Fix |
|-----|---------|-----|
| "Helps with projects" | Too vague, no triggers | "Manages project setup including repository creation, CI configuration, and team onboarding. Use when..." |
| "Creates sophisticated multi-page documentation systems" | No trigger phrases | Add: "Use when user asks to 'generate docs', 'write API reference', or 'create a README'" |
| "Implements the entity model with hierarchical relationships" | Too technical, no user language | Rewrite from user perspective: "Sets up database models for hierarchical data..." |

## Anti-Patterns to Avoid

### 1. The Kitchen Sink Skill
Trying to do everything. Split into focused skills with clear boundaries.

### 2. The Invisible Skill
Vague description that never triggers. Include specific phrases users say.

### 3. The Greedy Skill
Overly broad triggers that fire on unrelated queries. Add "Do NOT use for" exclusions.

### 4. The Monolith
Everything in SKILL.md with no progressive disclosure. Move reference content to `references/`.

### 5. The Island
No exclusions or cross-references. Every skill should acknowledge its neighbors.

### 6. The One-Way Door
Exclusions only go in one direction. If skill A says "don't use for B's domain", skill B should say "don't use for A's domain".
