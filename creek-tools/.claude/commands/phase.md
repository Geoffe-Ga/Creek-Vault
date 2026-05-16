---
description: Show the current Archetypal Wavelength phase (subset of `creek state`).
argument-hint: "(no arguments)"
---

# /creek phase

Shorthand for the phase block of the state report. Calls the `creek.state.read` MCP tool and returns just the wavelength snapshot — phase name, confidence, mode, dosage shares, recent transitions.

## What I will do

1. Call `creek.state.read`.
2. Extract the Wavelength snapshot section (per FEAT-007's `## Wavelength snapshot` heading).
3. Display phase, confidence, mode, medicine/toxic dosage share, and any recent phase transitions.

## When to use

- Quick orientation: "what phase am I in right now?".
- Before deciding whether to mine/draft (`Rising`/`Peaking`) or compost/withdraw (`Bottoming Out`/`Diminishing`).

## Related

- `/creek wavelength` — same content, just a different mnemonic.
- `/creek state` — the full report including eddies, threads, suggested questions.
