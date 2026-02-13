---
name: file-naming-conventions
description: >-
  ISO 8601 date-prefix file naming conventions for documents and plans.
  Use when creating dated documents, plan files, analysis reports,
  meeting notes, or any time-sensitive documentation.
  Do NOT use for source code files, configs, or permanent reference docs.
metadata:
  author: Geoff
  version: 1.0.0
---

# File Naming Conventions

Always prefix dated documents with ISO 8601 format (YYYY-MM-DD) for natural chronological sorting.

## Instructions

### Step 1: Determine if the Document Needs a Date Prefix

**Use date prefixes for**: analysis reports, status reports, plans, meeting notes, decision records, changelogs, progress updates, retrospectives, any time-sensitive documentation.

**Skip date prefixes for**: permanent reference docs (README.md), configuration files, source code, test files, templates, general guides.

### Step 2: Construct the Filename

**Format**: `YYYY-MM-DD_DESCRIPTIVE_NAME.ext`

- Date: 4-digit year, 2-digit month, 2-digit day, separated by hyphens
- Separator: Single underscore between date and description
- Name: ALL_CAPS for major documents, lowercase_with_underscores for supporting docs
- Be descriptive but concise; use common abbreviations (CLI, API, DB)

### Step 3: Handle Special Cases

**Multiple documents same day**: Use descriptive disambiguation:
```
2026-01-26_MUTATION_ANALYSIS_CLI.md
2026-01-26_MUTATION_ANALYSIS_CREDENTIALS.md
```

**Versioned documents**: `YYYY-MM-DD_DOCUMENT_NAME_vX.Y.md`

**Living documents**: Either update the date prefix on major revisions, or skip the date prefix entirely.

## Examples

### Example 1: Plan File
```
2026-01-26_MUTANT_EXTERMINATION_PLAN_CLI.md
2026-01-25_MUTATION_TESTING_STATUS.md
2026-01-20_FEATURE_IMPLEMENTATION_STRATEGY.md
```

### Example 2: Analysis Reports
```
2026-01-26_mutation_analysis_cli.md
2026-01-25_performance_analysis_database.md
2026-01-20_security_audit_report.md
```

### Example 3: Quick Creation
```bash
# Bash alias
alias newplan='touch "$(date +%Y-%m-%d)_PLAN_NAME.md"'

# Python
from datetime import date
filename = f"{date.today().isoformat()}_MY_DOCUMENT.md"
```

## Troubleshooting

### Error: Files don't sort chronologically
- Verify the date comes FIRST in the filename, not at the end
- Use `YYYY-MM-DD` format, never `MM-DD-YYYY` or `DD-MM-YYYY`
- Use hyphens in dates, underscores between date and name

### Error: Naming conflicts on the same day
- Add a descriptive suffix to disambiguate
- Or use sequential numbering: `2026-01-26_01_FIRST.md`, `2026-01-26_02_SECOND.md`
