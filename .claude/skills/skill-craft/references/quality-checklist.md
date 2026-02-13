# Skill Quality Checklist

Every item must pass before a skill ships. This checklist is derived from Anthropic's Complete Guide to Building Skills for Claude and validated against production skills.

## Structure

- [ ] Folder named in kebab-case (no spaces, capitals, or underscores)
- [ ] `SKILL.md` file exists with exact spelling (case-sensitive)
- [ ] No `README.md` inside the skill folder
- [ ] `references/` used for verbose content (if SKILL.md > ~200 lines)
- [ ] `scripts/` used for executable validation (if applicable)

## YAML Frontmatter

- [ ] `---` delimiters present (opening and closing)
- [ ] `name` field: kebab-case, matches folder name
- [ ] `name` does not contain "claude" or "anthropic" (reserved)
- [ ] `description` field present and under 1024 characters
- [ ] `description` includes WHAT the skill does
- [ ] `description` includes WHEN to use it (with specific trigger phrases)
- [ ] `description` includes "Do NOT use for" exclusions with cross-references
- [ ] No XML angle brackets (`<` or `>`) anywhere in frontmatter
- [ ] `metadata.author` present
- [ ] `metadata.version` present (semver format)

## Body Structure

- [ ] Starts with `# Skill Name` heading
- [ ] 1-2 sentence overview immediately after heading
- [ ] `## Instructions` section with numbered steps
- [ ] `## Examples` section with at least 2 examples
- [ ] `## Troubleshooting` section with at least 1 error case
- [ ] Instructions are specific and actionable (not "validate things properly")
- [ ] References to bundled files use explicit paths (`references/filename.md`)

## Progressive Disclosure

- [ ] Frontmatter is minimal: just WHAT, WHEN, and DO NOT
- [ ] SKILL.md body contains full instructions but stays under 5,000 words
- [ ] Verbose content (language-specific patterns, worked examples, templates) lives in `references/`
- [ ] SKILL.md links to reference files where appropriate

## Triggering Quality

- [ ] Description includes phrases users would actually say
- [ ] Description is NOT too vague (e.g., "Helps with projects")
- [ ] Description is NOT missing trigger conditions
- [ ] Description is NOT overly technical without user-facing language
- [ ] Negative triggers present for related skill domains
- [ ] Mental test: "When would you use the [skill-name] skill?" produces a clear, specific answer

## Composability

- [ ] Works alongside other skills without assuming exclusivity
- [ ] Exclusions are bilateral (if A excludes B's domain, B excludes A's)
- [ ] Cross-references name the specific alternative skill
- [ ] No overlapping trigger phrases with existing skills (or properly disambiguated)

## Content Quality

- [ ] Instructions use imperative voice ("Do X" not "You should do X")
- [ ] Examples cover both common scenarios and edge cases
- [ ] Troubleshooting addresses real failure modes, not hypothetical ones
- [ ] No over-engineering or unnecessary complexity
- [ ] Error handling guidance included where applicable

## Meta-Recursive Validity

This checklist applies to itself. The skill-craft skill:
- [x] Is in kebab-case folder (`skill-craft/`)
- [x] Has `SKILL.md` with valid frontmatter
- [x] Description includes WHAT + WHEN + DO NOT
- [x] Description under 1024 characters
- [x] Has Instructions, Examples (3), Troubleshooting sections
- [x] Uses `references/` for this checklist
- [x] Has bilateral exclusions with related skills
- [x] No XML angle brackets in frontmatter
