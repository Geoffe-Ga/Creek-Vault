---
name: architectural-decisions
description: >-
  Guide explicit, factual trade-off analysis for architectural choices.
  Use when choosing between libraries, patterns, technologies, databases,
  or API designs. Presents 2-4 options with comparison matrices and
  recommendations. Do NOT use for trivial choices, variable naming,
  or code formatting decisions.
metadata:
  author: Geoff
  version: 1.0.0
---

# Architectural Decisions

Never make architectural decisions unilaterally. Always present options with explicit, factual trade-off analysis.

## Instructions

### Step 1: Identify the Decision Point

State clearly what needs to be decided:
- **Decision Required**: Specific choice to be made
- **Context**: Why this decision matters now
- **Impact**: What will be affected

### Step 2: Present Options (2-4 alternatives)

For each option, provide:
- **Name**: Clear, descriptive label
- **Description**: What this approach entails
- **Pros**: Factual advantages (measurable when possible)
- **Cons**: Factual disadvantages (measurable when possible)
- **Implementation Effort**: Low/Medium/High
- **Maintenance Impact**: Long-term considerations

### Step 3: Build a Comparison Matrix

Compare options across key dimensions:
- Performance characteristics
- Development time
- Maintenance burden
- Scalability implications
- Testing complexity
- Learning curve
- Community support

### Step 4: Make a Recommendation

If one option is clearly superior for the context:
- State factual reasoning based on project constraints
- Explain why alternatives are less suitable
- Always let the user make the final decision

### Factual vs Subjective Analysis

**Use factual statements**: "PostgreSQL supports 15K writes/sec", "JWT tokens are ~200 bytes"

**Avoid subjective statements**: "PostgreSQL is better", "JWT is more modern"

## Examples

See `references/examples.md` for three complete worked examples:
- Database choice (PostgreSQL vs MongoDB vs SQLite)
- Authentication strategy (JWT vs Session Cookies vs API Keys)
- Error response format (RFC 7807 vs Simple Object vs Framework Default)

### Example 1: Quick Decision Template

```markdown
## Architectural Decision: [Topic]

**Context**: [Why we need to decide this now]
**Impact**: [What parts of the system are affected]

### Option 1: [Name]
**Pros**: ...
**Cons**: ...
**Implementation**: [Low/Medium/High]

### Option 2: [Name]
**Pros**: ...
**Cons**: ...
**Implementation**: [Low/Medium/High]

### Comparison Matrix
| Criterion | Option 1 | Option 2 |
|-----------|----------|----------|
| Performance | ... | ... |
| Dev Time | ... | ... |

### Recommendation
**Recommended**: Option X
**Rationale**: [Factual reasoning]

**Question for User**: Which option should we proceed with?
```

### Example 2: When NOT to Use This Skill

- Variable naming choices -> just follow project conventions
- Code formatting -> use the linter
- Trivial choices with no trade-offs -> just pick one
- Decisions already made -> follow existing patterns

## Troubleshooting

### Error: Analysis paralysis with too many options
- Limit to 2-4 realistic options
- Eliminate obviously unsuitable options early
- Focus comparison on the 3-4 most important criteria for this project

### Error: Recommendation feels subjective
- Replace opinions with metrics: throughput, latency, LOC, dependency count
- Reference benchmarks or documentation
- State "when this matters" for each pro/con
