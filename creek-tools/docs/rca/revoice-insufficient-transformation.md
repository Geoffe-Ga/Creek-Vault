# RCA: revoice returned `ok` without applying the owner's voice

## Problem statement

A 22,291-byte essay on Gabor Maté's *Scattered Minds* was revoiced in the
canonical `analytical` register. Because input is capped at 16,000
characters, the client split it at a Markdown heading, made two
`creek.revoice` calls, and stitched the results together. Both calls returned
`status: "ok"`. The result did not sound meaningfully more like the owner:

- the opening was nearly identical to the source;
- most edits were citation removal, punctuation, spelling, compression and
  minor substitutions.

The aggregate diff was nonetheless large, and the downstream client
(Book Essay Forge) treated that difference as a successful voice
transformation.

## Production path, as traced

1. **MCP serialization in.** `creek_mcp/server.py` registers
   `creek.revoice(text, register?, phase?, privacy_tier_ceiling?)`, checks
   the `vault:desk` grant, binds the request-scoped key, and wraps the call
   in the composition-jobs runtime (sync deadline, then a detached job
   handle).
2. **Validation.** `creek_mcp/tools/revoice.py:_invalid` checks non-empty
   text, the 16,000-character limit, and canonical register/phase slugs.
3. **Owner-voice evidence.** *None.* No fragments, samples or exemplars were
   read. The only conditioning was the skill tree.
4. **Register prompt construction.** `creek/author/voice.py:split_revoice_prompt`
   read `voice-core`, `phase-<p>` and `register-<r>`. Native generated skills
   pass through `_source_free_style`, which (correctly, #1986) rebuilds them as
   `render_voice_signature(name)`, a canonical text that is **byte-identical
   for every vault**. For `register-analytical` it says "Write in an analytical
   register. Use precise, qualified claims grounded in evidence…". If
   voice-core was missing, *every* skill was dropped, so a requested register
   could vanish silently.
5. **Preservation constraints.** The ask required meaning, claims,
   qualifications, attribution and biography to be kept. It said nothing about
   headings or citations, and nothing asked for a transformation rather than
   a copyedit.
6. **Model invocation.** One call, with no retry.
7. **Response validation.** Only empty output, `max_tokens` truncation and
   oversized output were rejected.
8. **MCP serialization out.** Any other output was returned as `status: "ok"`,
   with `voice_skills: ["voice-core", "register-analytical"]`, which reads as
   proof that the owner's voice was applied.

## Root cause

Two defects compounded:

1. **No owner-specific signal reached the model.** After #1986 stripped
   exemplars (rightly, for privacy), nothing replaced them. The prompt
   contained only canonical text shared by every vault. Asking for an
   "analytical" rewrite of an already analytical essay produces a
   copyedit, which is exactly the observed output.
2. **No quality gate.** Nothing judged whether the output differed from the
   input in voice, so a copyedit returned `ok`. Clients had to invent their own
   heuristics, and the one used (aggregate diff) cannot tell a
   copyedit-plus-one-generic-paragraph from a revoice.

## Contributing factors

- `voice_skills` named canonical skills, which made generic conditioning look
  owner-specific.
- The prompt did not protect headings and citations, so the model "improved"
  them away, which inflated the diff.
- Client-side splitting meant each call's opening was a section opening, and
  nothing checked any opening.

## Fix

- **Grounding** (`creek/author/revoice_grounding.py`,
  `creek/generate/voice.py:load_owner_voice_samples`). Owner-authored style
  skills, or `07-Voice/Register-Samples/` exemplars, each re-screened
  (self-authored, `voice_weight > 0`, qualifying confidence, tier admitted at
  the declared ceiling), measured into a numeric style profile. With neither,
  the call is refused as `no_owner_voice` before any spend. Requested
  registers and phases always apply, falling back to their canonical
  signature, never dropped.
- **Gate** (`creek/author/revoice_check.py`). Fidelity (headings, citations),
  per-section new wording *and* rebuilt sentences, opening, and style shift
  toward the profile. No raw-diff criterion.
- **One bounded retry** with owner excerpts and fixed feedback; at most two
  model calls inside one job run.
- **Contract 0.30.** A rejected rewrite is `status: "refused"` with closed
  `reason` (`insufficient_transformation`, `fidelity_violation`,
  `no_owner_voice`) and content-free `voice_check`, without the rejected
  text.

## Verification

> **Retired in contract 0.38 (2026-11-26, #2167).** The diagnostics this
> section names (`opening_unchanged`, `section_unchanged`, `copyedit_only`,
> `no_style_shift`) and the `insufficient_transformation` reason no longer
> exist. Revoice is now judged on its end state, the finished rewrite's voice
> rating against the vault's fingerprint, plus the unchanged fidelity check.
> An off-voice rewrite is returned and logged, never refused. See
> [`docs/mcp.md`](../mcp.md#owner-grounding-and-the-end-state-voice-rating-contracts-030-038)
> for the current checks and the exit codes of
> `python -m creek.author.revoice_check`. The steps below are kept as the
> record of how the 0.30 gate was verified.

`tests/fixtures/revoice/` reproduces the failed pair (the real artifacts are
outside the repository, per FEAT-019). The failed After clears a 25%
raw-diff floor (33% new words) yet is refused with `opening_unchanged`,
`section_unchanged`, `copyedit_only`, `no_style_shift` and
`citations_dropped`. A genuinely owner-voiced rewrite passes and halves the
style distance to the owner. The real pair can be checked locally with
`python -m creek.author.revoice_check SOURCE AFTER --vault VAULT`.

## Prevention

- A tool that claims a qualitative outcome ("in the owner's voice") must
  verify it before reporting success, and report failure as a closed code.
- Whenever privacy hardening removes a signal from a prompt, check what
  replaces it, and whether the tool's promise still holds.
