# REJECT-003: "Screenless" / Voice-First Ambition

**Verdict:** REJECT
**Source system:** AlfredOS (Screenless Dad framing)
**Affects:** CrawDad agent layer
**Roadmap target:** N/A
**Estimated complexity:** N/A
**Conflicts with non-negotiables?** none directly — scope discipline issue

## What it is

AlfredOS's "Screenless Dad" persona ([screenlessdad.com](https://screenlessdad.com/)) markets a "screenless digital butler" — a chat-first, ambient-capture interaction model where the user "doesn't look at app dashboards." Voice is implied through Omi (continuous-audio wearable) and Zoom transcript ingestion. Dontoh's Alfred mentions voice-style understanding on its roadmap.

## Why it's interesting

The user has explicitly framed CrawDad as Discord-first. It's worth recording why "voice-first" or "screenless" aspirations should not be allowed to creep into CrawDad's design, because they sound aspirational and harmless and would not be either.

## Fit with Creek Vault and/or CrawDad

It doesn't. CrawDad's purpose is Discord-mediated conversation — text first, with the textuality being what enables the ambiguity, paradox-tolerance, and reflective tone the LTM register requires. Voice-first interaction would:

- **Force structured output.** Voice agents need to fit into a TTS speaking budget; Discord agents can let prose breathe over multiple paragraphs. The voice-skill tree is built for prose.
- **Lose paradox-tolerance.** Voice interaction is more time-pressed; ambiguous responses sound evasive. Text gives the user time to sit with a paradox-flagging response. Voice doesn't.
- **Add a transcription layer.** Whisper at the bot input would re-introduce the lossy-compression problem at the conversation layer, in addition to it existing at the data layer.
- **Add ambient capture pressure.** "Always listening" is a different consent posture than "responds when you ping it on Discord." The privacy-tier system is built for the latter.
- **Multiply infrastructure.** Voice requires audio I/O on the VPS, latency budgets, possibly real-time streaming. Discord text is a webhook. The deployment topology is out of scope for this analysis but voice-first would make it dramatically more constrained.

The "screenless" framing is also rhetorically wrong. Discord *is* a screen. The honest framing is "no SaaS dashboards" — CrawDad doesn't replace Notion or Airtable, it lives where the user already is. That's fine; it's just text.

## Reasoning if rejected or deferred

The verdict could flip only if:

- The user explicitly wanted a voice-memo capture path (which is in scope as an *ingestion* feature — see ADAPT-006 — but not as an *interaction* feature).
- A clear killer use case for voice-mediated reflection emerged that text couldn't serve. (Spiritual sounding-board work is, if anything, harder to do well in voice than in text — voice favors quick exchange, reflection favors lingering.)

## Dependencies

- Adjacent to: ADAPT-006 (audio ingestion is fine; voice *interaction* is not).

## Acceptance criteria

N/A — this is a rejection. Documented so the "but what if CrawDad responded to voice memos?" question has a recorded answer rather than getting re-asked.
