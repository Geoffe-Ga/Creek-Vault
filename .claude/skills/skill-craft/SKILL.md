---
name: skill-craft
description: >-
  Build and validate Claude Code skills to production quality. Use when
  creating new skills, modifying existing SKILL.md files, writing skill
  descriptions, structuring skill directories, or reviewing skill quality.
  Ensures progressive disclosure, trigger optimization, and composability.
  Do NOT use for general documentation or code quality guidance.
metadata:
  author: Geoff
  version: 1.0.0
---

# Skill Craft

Meta-skill for creating world-class Claude Code skills. Every skill this produces must pass the same quality bar it enforces — including itself.

## Instructions

### Step 1: Define Use Cases Before Writing

Identify 2-3 concrete use cases before any code or markdown. For each:
- What does the user want to accomplish?
- What trigger phrases would they use?
- What should the skill NOT handle? (prevents overtriggering)

### Step 2: Structure the Directory

```
skill-name/
├── SKILL.md          # Required — core instructions
├── references/       # Optional — detailed docs loaded on demand
├── scripts/          # Optional — executable validation/tooling
└── assets/           # Optional — templates, icons, fonts
```

Rules:
- Folder name: kebab-case, no spaces, no capitals, no underscores
- File: exactly `SKILL.md` (case-sensitive, not `skill.md` or `SKILL.MD`)
- No `README.md` inside the skill folder

### Step 3: Write the YAML Frontmatter

The frontmatter is the most important part — it determines when Claude loads the skill.

```yaml
---
name: skill-name-in-kebab-case
description: >-
  [What it does]. Use when [specific trigger phrases the user would say].
  [Key capabilities]. Do NOT use for [exclusions with cross-references].
metadata:
  author: Geoff
  version: 1.0.0
---
```

**Frontmatter rules:**
- `name`: kebab-case, must match folder name
- `description`: under 1024 characters, MUST include:
  - What the skill does (1 sentence)
  - When to use it with specific trigger phrases
  - What NOT to use it for (with cross-references to correct skill)
- No XML angle brackets (`<` or `>`) anywhere in frontmatter
- No "claude" or "anthropic" in the name (reserved)

### Step 4: Write the Body with Progressive Disclosure

SKILL.md body structure:

```markdown
# Skill Name

[1-2 sentence overview of the skill's purpose]

## Instructions
### Step 1: [First action]
### Step 2: [Second action]

## Examples
### Example 1: [Common scenario]
### Example 2: [Edge case or alternative]

## Troubleshooting
### Error: [Common problem]
```

**Progressive disclosure levels:**
1. **Frontmatter** (always loaded): Just enough for Claude to know WHEN to use it
2. **SKILL.md body** (loaded when relevant): Full instructions and guidance
3. **references/** (loaded on demand): Detailed docs, language-specific patterns, worked examples

Keep SKILL.md under 5,000 words. Move verbose content to `references/`.

### Step 5: Optimize the Description for Triggering

The description field controls whether the skill activates. Test mentally:

- **Too vague**: "Helps with projects" — won't trigger on anything specific
- **Missing triggers**: "Creates sophisticated multi-page documentation systems" — no user phrases
- **Too technical**: "Implements the entity model with hierarchical relationships" — no user language

**Good pattern**: `[Verb phrase]. Use when [user says X, Y, or Z]. [Capabilities]. Do NOT use for [exclusions].`

### Step 6: Ensure Composability

Skills load simultaneously. Your skill must:
- Work alongside others without assuming it's the only capability
- Have bilateral exclusions with related skills (if skill A excludes B's domain, B should exclude A's)
- Use cross-references in "Do NOT use for" clauses (e.g., "use security skill" not just "use another skill")

### Step 7: Validate Before Shipping

Run the checklist in `references/quality-checklist.md`. Every item must pass.

Quick self-test: Ask "When would you use the [skill-name] skill?" — if the answer is vague, the description needs work.

## Examples

### Example 1: Creating a New Skill from a Simple Prompt

User says: "Create a skill for database migration best practices"

1. Define use cases: schema changes, data migration, rollback procedures
2. Define triggers: "migrate database", "schema change", "add column", "data migration"
3. Define exclusions: "Do NOT use for general SQL queries (use appropriate language docs)"
4. Create `database-migrations/SKILL.md` with frontmatter, instructions, examples, troubleshooting
5. If content exceeds ~200 lines, extract language-specific patterns to `references/`
6. Validate against checklist

### Example 2: Reviewing an Existing Skill

User says: "Review this skill for quality"

1. Check frontmatter: name kebab-case, description has WHAT + WHEN + DO NOT, under 1024 chars, no XML
2. Check body: has Instructions, Examples (2+), Troubleshooting sections
3. Check progressive disclosure: SKILL.md focused, verbose content in references/
4. Check composability: exclusions are bilateral with related skills
5. Check triggering: description includes phrases users would actually say
6. Report issues with specific fixes

### Example 3: Fixing an Overtriggering Skill

Symptom: Skill loads for unrelated queries.

1. Add negative triggers: "Do NOT use for [unrelated domain]"
2. Make description more specific: "Processes PDF legal documents for contract review" not "Processes documents"
3. Clarify scope: "Use specifically for online payment workflows, not for general financial queries"
4. Test with unrelated queries to confirm it no longer triggers

## Troubleshooting

### Error: Skill doesn't trigger when it should
- Description is too generic — add specific trigger phrases users would say
- Missing keywords — include technical terms relevant to the domain
- Debug: Ask Claude "When would you use the [skill-name] skill?" and adjust based on what's missing

### Error: Skill triggers too often
- Description is too broad — add "Do NOT use for" exclusions
- Clarify scope with specific domains, file types, or contexts
- Add negative triggers referencing the correct alternative skill

### Error: Instructions not followed
- Instructions too verbose — keep SKILL.md focused, move detail to references/
- Critical instructions buried — put them at the top, use `## Important` headers
- Ambiguous language — replace "validate things properly" with specific checklists

### Error: Skill content too large / slow responses
- Move detailed reference material to `references/` files
- Keep SKILL.md under 5,000 words
- Link to references instead of inlining content
