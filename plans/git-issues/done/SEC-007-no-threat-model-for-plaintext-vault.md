# SEC-007: No threat model documents the implications of plaintext intimate-tier storage

**Severity:** Medium
**Category:** SEC
**Estimated complexity:** S (≤2h) — documentation
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 2 review

## Files affected
- `creek-tools/docs/` — no threat model file
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §13 — "Ethical Guardrails" lists data sovereignty principles but not a concrete threat model

## Dependencies
None.

## Blockers
None.

## Reproduction
`grep -rn "threat\|adversary\|attacker" creek-tools/docs/ creek-tools/CLAUDE.md` returns nothing. There is no document that says, e.g., "we assume the local filesystem is trusted; we do not protect against malware on the host; we do not encrypt at rest because *X*."

## Analysis

The system stores intimate journal content, recovery-related text, and decision-making fragments as plaintext markdown in `01-Fragments/Journal/` etc. Privacy tiers gate which fragments go *to LLMs* (when working) but do not protect on-disk content. The fragment text is also embedded into a sentence-transformer model and cached locally; the ontology spec calls this out as acceptable, but there's no place a user can read to understand:

- What is the threat model? (Local-only single user? Backed up to a third-party drive sync? Multi-user host?)
- What is and isn't protected? (Confidentiality at rest is not protected. Network egress is gated by the LLM provider config.)
- What controls exist for accidental sync? (`/.gitignore` excludes `creek-skills/`? `.obsidian/`? Backup/restore guidance?)
- What happens if the host is compromised? (OAuth refresh token at `0o600` is the only hardened secret; everything else is plaintext.)

Without this, a user who reads the README sees "local-first by default", interprets that as "private", and may make different assumptions than the implementer.

Confidence: verified.

## Proposed remediation

Add `creek-tools/docs/security/threat-model.md`. Cover:
- Trust boundaries (filesystem, LLM providers, Drive API, embedding model cache)
- Assumed adversaries: nontechnical accidental disclosure (cloud sync, screenshot, shared screen), targeted local malware, third-party LLM logs
- What's protected: API keys via env vars, OAuth token at `0o600`, redaction of well-known secret patterns (with caveats from SEC-002), opt-in cloud LLM
- What's not protected: confidentiality at rest, integrity at rest (the audit log being mutable, see SEC-005), embedding cache reverse-engineering, vault directory permissions
- Recommended hygiene: where to enable disk encryption, gitignore patterns, when to use `creek purge`, when to wipe the embedding cache
- Explicit non-goals: multi-tenant safety, network exposure, DoS resistance

Reference this file from the README and from `docs/redaction.md`, `docs/classification.md`, and `docs/cleaning-and-purge.md`.

## Acceptance criteria

- The threat model is published, dated, versioned.
- README links to it.
- It enumerates each capability the system might be misread as having (encryption at rest, multi-user, etc.) and explicitly disclaims them.
- It cross-references SEC-002 (redaction gaps), SEC-005 (audit log integrity), SEC-006 (privacy-tier enforcement) so a reader can see the limits.

## References
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §13
- OWASP threat-model template
