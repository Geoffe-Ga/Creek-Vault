# ADAPT-006: Multimodal Scope Expansion (Whisper + Vision)

**Verdict:** ADAPT
**Source system:** Graphify
**Affects:** Creek Vault data layer
**Roadmap target:** v0.3 (after compile-then-query is settled)
**Estimated complexity:** M
**Conflicts with non-negotiables?** privacy — adapt carefully (see "Translation")

## What it is

Graphify's Pass 2 uses `faster-whisper` locally to transcribe audio and video (audio track only); Pass 3 uses Claude vision for images. Cited from [`how-it-works.md` v6](https://github.com/safishamsi/graphify/blob/v6/docs/how-it-works.md). The multimodal scope is wider than Creek's today (Creek does OCR via pytesseract for images but has no audio/video pipeline despite the spec listing "Audio transcripts (text/SRT) — podcast episodes, voice memos" as expected source types in §3.5).

## Why it's interesting

Two specific gaps in Creek's current multimodal scope:

1. **Voice memos.** Spec §3.5 lists "Audio transcripts (text/SRT)" as expected source data, but no ingestor exists. Voice memos and podcast episodes are explicitly in scope per the canonical spec.
2. **Diagrams and screenshots beyond OCR.** Creek has OCR for image text, but no semantic understanding of diagrams, flowcharts, whiteboard photos, or screenshots of conversations. Claude vision could provide this.

Both are roadmap items, not v1 deliverables. The lossy-compression risk (Karpathy's lesson) is amplified for multimodal: if a voice memo's transcript loses the affect, the wavelength-phase classification will misread.

## Fit with Creek Vault and/or CrawDad

Two new ingestors and one extension:

1. **`creek ingest --type audio`** — uses local Whisper (`faster-whisper` is the Graphify choice; OpenAI's `whisper` is the alternative). Produces a fragment per audio file with the transcript as body and the audio file linked. Frontmatter includes confidence per segment, language detection, speaker turns if detectable.
2. **`creek ingest --type video`** — extracts the audio track and routes to the audio ingestor. Optionally samples frames for vision-based analysis (deferred — frame-level video understanding is real research and Creek doesn't need it).
3. **`creek ingest --type image` extension** — adds a Claude-vision pass alongside OCR. Vision output goes into a separate frontmatter field (`vision_description`) so it's distinguishable from OCR text. This does call out to Anthropic, so it's gated by privacy tier and opt-in (`--vision`).

For voice memos specifically, the wavelength-phase classification has high signal: the *prosodic* features (pace, pitch, pause length) are wavelength-relevant, but Whisper drops them. A v0.4+ enhancement would extract prosodic features alongside transcripts; v0.3 just gets the words.

## Translation if adapted

Three Creek-specific adaptations:

1. **Local-first, with a hard preference for `faster-whisper` over cloud Whisper APIs.** Voice memos are intimate by default; transcripts ship to OpenAI's Whisper API by mistake is a privacy violation. Use the local model.
2. **Privacy-tier auto-classification of voice memos.** Default tier for an audio-derived fragment is `intimate` until classified otherwise. Force the user to opt in to non-intimate handling. This is stricter than the existing redaction-first policy because voice memos are typically more personal than text exports.
3. **Vision pass is opt-in per ingestor invocation.** `creek ingest --type image --vision` calls Claude vision; without `--vision`, only OCR runs. Same opt-in discipline as the existing Anthropic-classification path.

The multimodal scope is *not* "video frame analysis" — that's a different research project. v0.3 covers audio (Whisper) and vision-on-images (Claude). Frame-level video is DEFER.

## Dependencies

- Depends on: ADOPT-004 (deterministic-first pipeline — Whisper is a Pass 2 addition, not Pass 3).
- Pairs with: ADAPT-004 (MCP server — voice memo ingest may be triggered by CrawDad on Discord uploads).

## Acceptance criteria

- `creek ingest --type audio` exists, uses `faster-whisper` locally, and produces transcripts with frontmatter linking to the source audio file.
- `creek ingest --type video` exists and extracts audio for transcription.
- `creek ingest --type image --vision` runs Claude vision in addition to OCR; without `--vision`, only OCR runs.
- Audio-derived fragments default to `privacy_tier: intimate` until reclassified.
- Network egress during audio/video ingestion is zero (local Whisper only); CI test verifies this.
- A regression test verifies that `--vision` without an Anthropic key fails closed rather than silently skipping.
- The ontology spec §3.5 is updated to reflect that audio is now a first-class source type, not an aspirational one.
