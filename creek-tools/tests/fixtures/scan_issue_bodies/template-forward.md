## Context
- File(s): `path/to/file.py:120-164`
- Symbol(s): `enclosing_function_name` — verified present at the scan SHA
- Scanned at commit: `<SHA>` — re-verify against HEAD before starting
- **The line numbers and symbol names above are valid ONLY at that scan SHA.**
  They are a claim about one revision, not about HEAD. Line numbers in this
  repo have been observed 100–300 lines stale within weeks, and symbols get
  renamed. Resolve both against the SHA before trusting them, and if a symbol
  does not exist there, treat the citation as wrong rather than hunting for
  the nearest similar name (#1651).
- Evidence: [tool output excerpt — the radon score, the audit finding, the
  coverage gap, the profile, the grep hit with surrounding lines]
- Related: [links to sibling issues from the same scan run, prior PRs]
