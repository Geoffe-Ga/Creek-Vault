# The Creek Ontology: Agent Prompt for Digital Footprint Organization

## Your Mission

You are an agentic coding assistant tasked with building a complete knowledge organization system for a single human's entire digital footprint. The human is a Liminal Trickster Mystic — a software engineer, spiritual practitioner, father, writer, and creator of the APTITUDE framework. Your job is to ingest, transform, classify, interlink, and organize massive amounts of semi-structured and unstructured data into a living Obsidian vault that serves as a second brain, decision-support system, writing voice proxy, and self-actualization engine.

**Everything you build must be oriented toward wholeness, not extraction. Toward emergence, not control. Toward integration, not surveillance.**

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [The Creek Ontology](#2-the-creek-ontology)
3. [Source Data Inventory](#3-source-data-inventory)
4. [Obsidian Vault Architecture](#4-obsidian-vault-architecture)
5. [YAML Frontmatter Schema](#5-yaml-frontmatter-schema)
6. [The APTITUDE Frequency System](#6-the-aptitude-frequency-system)
7. [The Archetypal Wavelength Mapping](#7-the-archetypal-wavelength-mapping)
8. [Semantic Classification Pipeline](#8-semantic-classification-pipeline)
9. [Tooling & Scripts to Build](#9-tooling--scripts-to-build)
10. [Emergence Infrastructure](#10-emergence-infrastructure)
11. [Voice Proxy Generation](#11-voice-proxy-generation)
12. [Decision Support Layer](#12-decision-support-layer)
13. [Ethical Guardrails](#13-ethical-guardrails)
14. [Implementation Plan](#14-implementation-plan)

---

## 1. Design Philosophy

### 1.1 The Creek, Not the Dam

Palantir's Decision Ontology organizes the world into objects, properties, links, and actions — designed for institutions wielding power over populations. This system inverts that. The Creek Ontology organizes one person's inner and outer world into **fragments, resonances, threads, and praxis** — designed for a single consciousness wielding sovereignty over itself.

Where Palantir asks: "What do we know, and how do we act on it?"
The Creek asks: **"What is moving through me, and how do I flow with it?"**

### 1.2 Core Metaphor: The Creek

A creek is a liminal ecosystem. It is:

- **Between** wilderness and civilization (liminal)
- **Playful** in how it routes around obstacles (trickster)
- **Alive** with invisible interdependent processes (mystic)

Data in this system flows like water. It pools in eddies (clusters), carves channels (threads), deposits sediment (patterns), and is never the same creek twice. The ontology must be **descriptive, not prescriptive** — it names what it observes, it doesn't force data into boxes.

### 1.3 Anti-Patterns (Hard Constraints)

Do NOT build anything that:

- Optimizes for productivity metrics, KPIs, or quantified-self gamification
- Frames personal data as an "asset" to be "leveraged"
- Uses language or structures borrowed from corporate intelligence, military ops, or adtech
- Creates hierarchies of worth between data sources (a Discord shitpost may contain more insight than a formal document)
- Surveils patterns for the purpose of behavioral manipulation (even self-manipulation)
- Treats the human as a system to be optimized rather than a being to be understood
- Imposes rigid taxonomies that prevent emergent patterns from surfacing

### 1.4 Pro-Patterns (Guiding Principles)

DO build everything to:

- **Reveal** rather than **prescribe** — surface patterns, don't enforce them
- **Interlink** rather than **silo** — connections between fragments are the primary value
- **Breathe** — leave deliberate room for uncategorized, liminal, in-between content
- **Serve wholeness** — every feature should help the human integrate, not fragment
- **Honor cyclicality** — moods, insights, and creative output follow waves; the system should track and respect this
- **Enable polygnosticism** — contradictory beliefs can coexist; the system never resolves paradox without permission
- **Remain human-readable** — everything is markdown, everything is portable, nothing depends on proprietary tooling

---

## 2. The Creek Ontology

### 2.1 Ontological Primitives

These are the fundamental units of the system, replacing Palantir's Object/Property/Link/Action model:

| Creek Primitive | Description | Palantir Equivalent | Why It's Different |
|---|---|---|---|
| **Fragment** | An atomic unit of meaning extracted from any source. Could be a paragraph, a message, a journal entry, a conversation turn. | Object | Fragments are alive — they gain meaning through connection, not intrinsic properties |
| **Resonance** | A semantic similarity or thematic echo between two or more Fragments. | Link | Resonances are felt, not just logical. They can be intuitive, aesthetic, or synchronistic |
| **Thread** | A narrative or thematic current that runs through multiple Fragments over time. | Property/Tag | Threads are temporal — they have a direction, a beginning, and sometimes an ending |
| **Eddy** | A cluster of Fragments that pool around a topic, project, or recurring concern. | Collection | Eddies form naturally — they are discovered, not created |
| **Praxis** | An actionable insight, practice, or decision derived from observing patterns in Fragments, Resonances, Threads, and Eddies. | Action | Praxis is embodied — it's not just "what to do" but "what practice to deepen" |
| **Wavelength** | A cyclical pattern observed in the human's emotional, creative, or spiritual state over time. | (No equivalent) | The system tracks the human's position on the Archetypal Wavelength |

### 2.2 Relationships Between Primitives

```
Fragment ---resonates_with---> Fragment
Fragment ---belongs_to---> Thread
Fragment ---pools_in---> Eddy
Thread ---generates---> Praxis
Eddy ---reveals---> Wavelength pattern
Praxis ---deepens---> Frequency (APTITUDE stage)
Wavelength ---contextualizes---> all other primitives
```

### 2.3 Metadata Dimensions

Every Fragment carries metadata across these dimensions:

1. **Source** — Where it came from (Claude, ChatGPT, Discord, journal, essay, etc.)
2. **Temporality** — When it was created/modified, normalized to America/Los_Angeles timezone
3. **Frequency** — Which APTITUDE Frequency it most resonates with (can be multiple)
4. **Wavelength Phase** — Where on the six-phase Archetypal Wavelength cycle (Rising → Peaking → Withdrawal → Diminishing → Bottoming Out → Restoration)
5. **Wavelength Mode** — The functional stance: Inhabit, Express, Collaborate, Integrate, or Absorb
6. **Wavelength Orientation** — Do, Feel, or Do/Feel
7. **Wavelength Dosage** — Medicine (healthy) or Toxic (overdose) expression
8. **Wavelength Color** — The Spiral Dynamics frequency-color most active
9. **Wavelength Descriptor** — The specific word from the Mode map (e.g., "Gnosis", "Anxiety", "Power-With")
10. **Emotional Texture** — Free-form tags for the felt quality of the content
11. **Confidence** — How settled vs. exploratory the thought is (from "musing" to "conviction")
12. **Voice Register** — The writing/speaking register (confessional, analytical, playful, prophetic, etc.)
13. **Interlocutor** — Who the human was talking to/with, if applicable
14. **Praxis Potential** — Whether this fragment contains or implies actionable practice

---

## 3. Source Data Inventory

The agent should expect and handle the following data types. Each needs a dedicated ingestion parser.

### 3.1 Chatbot History

| Source | Expected Format | Notes |
|---|---|---|
| Claude (claude.ai) | JSON export, conversation threads | Primary AI interlocutor. Highest volume. Contains code, philosophy, project planning, personal reflection |
| ChatGPT | JSON export, conversation threads | Earlier AI conversations, possibly different tone/topics |
| Other LLM interfaces | Various JSON/text | Any other chatbot exports |

### 3.2 Discord Message History

| Source | Expected Format | Notes |
|---|---|---|
| Creekmason Discord | JSON export (Discord Data Package or bot export) | Community interactions, vulnerable sharing, collaborative meaning-making |
| Recovery Dharma channels | JSON export | Spiritual practice, recovery context |
| Other servers | JSON export | Technical, gaming, miscellaneous |

### 3.3 Personal & Creative Documents

| Source | Expected Format | Notes |
|---|---|---|
| APTITUDE course files | 219+ markdown files, organized by stage | The canonical framework — these are REFERENCE documents, not fragments to reorganize |
| Creekmason Substack essays | HTML/markdown | Published writing — canonical voice samples |
| Journal entries | Various (text, markdown, possibly handwritten/OCR) | Most intimate data — handle with care |
| Code projects & READMEs | Various code files, markdown | Technical thinking, problem-solving patterns |
| Resume versions | DOCX/PDF/markdown | Professional self-presentation over time |
| Notes & scratch files | Various | Often the most valuable — raw, unfiltered thinking |

### 3.4 Google Drive

| Source | Expected Format | Notes |
|---|---|---|
| Google Docs | DOCX export via API | Notes, drafts, shared documents |
| Google Sheets | XLSX export via API | Data, trackers, the Archetypal Wavelength map itself |
| Google Slides | PPTX export via API | Presentations, visual thinking |
| Other Drive files | Various | PDFs, images, etc. stored in Drive |

**Critical safety requirement:** Google Drive ingestion must use a **read-only download-first architecture**:

1. **Single API call to download.** Use the Google Drive API to list and download files to a local staging directory. This download operation must be completely isolated from all other processing. No file modification, no deletion, no in-place editing. The API client should request read-only scopes (`drive.readonly`).
2. **Never write back to Drive.** The pipeline must never call any Drive API method that modifies, moves, or deletes files. Hard-code this constraint — no `update()`, `delete()`, `trash()`, or `copy()` calls.
3. **Stage locally, then ingest.** Downloaded files land in a staging directory (`source_drive/google-drive-export/`). From there, the normal ingestion pipeline processes them. The originals on Drive are never touched.
4. **Parsers needed:** DOCX → markdown (python-docx + markdownify), XLSX → markdown tables or structured fragments (openpyxl/pandas), PPTX → markdown with slide-by-slide content extraction (python-pptx), PDF → text (pdfminer.six).

### 3.5 Other Potential Sources

| Source | Expected Format | Notes |
|---|---|---|
| Email exports | MBOX/EML | If provided |
| Browser bookmarks | HTML/JSON | Interest mapping |
| Social media exports | JSON | If provided |
| Audio transcripts | Text/SRT | Podcast episodes, voice memos |

---

## 4. Obsidian Vault Architecture

### 4.1 Top-Level Folder Structure

```
Creek-Vault/
├── 00-Creek-Meta/                  # System docs, templates, configs
│   ├── Templates/                  # Obsidian templates for each primitive type
│   ├── Scripts/                    # Dataview queries, Templater scripts
│   ├── Ontology/                   # This document and related specs
│   └── Processing-Log/            # Audit trail of what was ingested and how
│
├── 01-Fragments/                   # All ingested content, converted to markdown
│   ├── Conversations/             # Chatbot history, parsed into atomic notes
│   ├── Messages/                  # Discord and other messaging platforms
│   ├── Writing/                   # Essays, posts, creative work
│   ├── Journal/                   # Personal reflection
│   ├── Technical/                 # Code-related thinking, architecture notes
│   └── Unsorted/                  # New ingestions pending classification
│
├── 02-Threads/                     # Narrative currents discovered across fragments
│   ├── Active/                    # Currently developing threads
│   ├── Dormant/                   # Threads that went quiet but may return
│   └── Resolved/                  # Threads that reached natural conclusion
│
├── 03-Eddies/                      # Topic clusters that emerged organically
│
├── 04-Praxis/                      # Actionable insights and practices
│   ├── Daily/                     # Habits, routines, micro-practices
│   ├── Seasonal/                  # Longer-cycle practices and commitments
│   └── Situational/              # Context-dependent decision frameworks
│
├── 05-Wavelength/                  # Cyclical pattern tracking
│   ├── Phase-Maps/               # Temporal maps of wavelength position
│   ├── Mode-Profiles/            # Detailed profiles of each mode
│   └── Observations/             # Raw wavelength-related reflections
│
├── 06-Frequencies/                 # APTITUDE frequency-specific collections
│   ├── F1-Agency/
│   ├── F2-Receptivity/
│   ├── F3-Self-Love-Power/
│   ├── F4-Community-Love/
│   ├── F5-Achievism/
│   ├── F6-Pluralism/
│   ├── F7-Integration/
│   ├── F8-True-Self/
│   ├── F9-Unity/
│   └── F10-Emptiness/
│
├── 07-Voice/                       # Writing voice analysis and proxy materials
│   ├── Register-Samples/         # Exemplar fragments for each voice register
│   ├── Rhetorical-Patterns/      # Identified patterns in argumentation style
│   ├── Lexicon/                  # Distinctive vocabulary, coinages, recurring metaphors
│   └── Drafts/                   # AI-generated drafts linked to source fragments and skills
│
├── 08-Decisions/                   # Decision support layer
│   ├── Active/                    # Decisions currently being navigated
│   ├── Archive/                   # Past decisions and their outcomes
│   └── Frameworks/               # Reusable decision-making approaches
│
├── 09-Reference/                   # Canonical materials (not reorganized, just linked)
│   ├── APTITUDE-Course/           # The 219 course files, preserved as-is
│   ├── Published-Essays/          # Finalized Substack posts
│   └── External-Sources/         # Books, articles, quotes that recur in fragments
│
└── 10-Liminal/                     # THE MOST IMPORTANT FOLDER
    ├── Paradoxes/                 # Contradictions that should not be resolved
    ├── Synchronicities/           # Meaningful coincidences across data
    ├── Unnamed/                   # Patterns that don't fit anywhere yet
    └── Compost/                   # Decomposing ideas that may fertilize future growth
```

### 4.2 Why This Structure

- **Numbered prefixes** keep folders in intentional order in Obsidian's file explorer
- **Fragments** are the raw material; everything else is derived
- **Threads, Eddies, Praxis** are emergent — they get populated by the classification pipeline and by the human over time
- **10-Liminal** exists explicitly to honor the LTM disposition: not everything needs to be categorized, and the uncategorizable is often the most valuable
- **09-Reference** preserves canonical works without fragmenting them

### 4.3 Obsidian Plugin Dependencies

The vault should ship with a recommended plugin list and any necessary configs:

- **Dataview** — For dynamic queries across frontmatter metadata
- **Templater** — For note creation templates
- **Graph Analysis** — For visualizing resonances between fragments
- **Calendar** — For temporal navigation of fragments
- **Kanban** — For managing active Threads and Decisions
- **Tag Wrangler** — For managing the tag taxonomy
- **Periodic Notes** — For daily/weekly/monthly wavelength tracking
- **Obsidian Git** — For version control of the vault

---

## 5. YAML Frontmatter Schema

Every markdown file in the vault gets YAML frontmatter. The schema varies by primitive type.

### 5.1 Fragment Frontmatter

```yaml
---
type: fragment
id: frag-{uuid-short}
title: "{descriptive title generated from content}"
source:
  platform: claude | chatgpt | discord | journal | essay | code | email | other
  original_file: "{path to original file before conversion}"
  original_encoding: "{detected encoding before UTF-8 normalization}"
  conversation_id: "{if applicable}"
  channel: "{discord channel name, if applicable}"
  interlocutor: "{who the human was talking to/with}"
created: "YYYY-MM-DDTHH:MM:SS-08:00"  # Always normalize to America/Los_Angeles
ingested: "YYYY-MM-DDTHH:MM:SS-08:00"
frequency:
  primary: F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | unclassified
  secondary: []  # can resonate with multiple
wavelength:
  phase: rising | peaking | withdrawal | diminishing | bottoming_out | restoration | unclassified
  mode: inhabit | express | collaborate | integrate | absorb | unclassified
  orientation: do | feel | do_feel | unclassified
  dosage: medicine | toxic | ambiguous | unclassified
  color: beige | purple | red | blue | orange | green | yellow | teal | ultraviolet | clear_light | unclassified
  descriptor: "{specific word from the Mode map, e.g. 'Introspectivity', 'Anxiety', 'Gnosis'}"
voice:
  register: confessional | analytical | playful | prophetic | instructional | raw | conversational
  confidence: musing | exploring | forming | settled | conviction
emotional_texture: []  # free-form tags like "grief", "wonder", "frustration", "flow"
threads: []  # wiki-links to Thread notes
eddies: []  # wiki-links to Eddy notes
praxis_potential: none | latent | explicit
tags: []
---
```

### 5.2 Thread Frontmatter

```yaml
---
type: thread
id: thread-{uuid-short}
title: "{thread name}"
status: active | dormant | resolved
first_seen: YYYY-MM-DD
last_seen: YYYY-MM-DD
frequency_affinity: []  # which frequencies this thread touches
fragment_count: 0  # auto-updated by scripts
description: "{1-2 sentence summary of this narrative current}"
tags: []
---
```

### 5.3 Eddy Frontmatter

```yaml
---
type: eddy
id: eddy-{uuid-short}
title: "{cluster name}"
formed: YYYY-MM-DD
fragment_count: 0
threads: []  # threads that flow through this eddy
description: "{what this cluster is about}"
tags: []
---
```

### 5.4 Praxis Frontmatter

```yaml
---
type: praxis
id: praxis-{uuid-short}
title: "{practice or insight name}"
frequency: []
praxis_type: habit | practice | framework | insight | commitment
derived_from: []  # links to fragments, threads, or eddies that generated this
status: proposed | active | integrated | released
review_interval: daily | weekly | monthly | seasonal | as_needed
tags: []
---
```

### 5.5 Decision Frontmatter

```yaml
---
type: decision
id: decision-{uuid-short}
title: "{what is being decided}"
status: sensing | deliberating | committing | enacted | reflecting
opened: YYYY-MM-DD
decided: YYYY-MM-DD  # blank until decided
frequency_context: []
wavelength_phase_at_opening: ""
relevant_threads: []
relevant_praxis: []
options: []  # list of options being considered
criteria: []  # what matters in this decision
outcome: ""  # filled in after reflection
tags: []
---
```

---

## 6. The APTITUDE Frequency System

The 10 Frequencies (formerly Stages) are the primary developmental axis of the ontology. Every Fragment can be tagged with one or more Frequencies based on its thematic content.

### 6.1 Frequency Definitions for Classification

Use these definitions to guide automated and semi-automated classification:

| Frequency | Name | Color | Core Theme | Content Signals |
|---|---|---|---|---|
| **F1** | Agency | Beige | Survival, intentional action, willpower, initiative | Goal-setting, project planning, proactive problem-solving, taking charge, discipline, basic needs |
| **F2** | Receptivity | Purple | Kinship, receptivity to pleasure and Source, intuitive divination, manifestation fruits, surrender, trust | Letting go, accepting help, ancestral bonds, community rituals, shared myths, slowing down, receiving pleasure, divination/tarot/oracle work, noticing manifestation results, openness to intuitive insight |
| **F3** | Self-Love / Power | Red | Self-love as the foundation of healthy power; assertion, individuality, strength, confidence — self-love is what prevents power from becoming power-over | Leadership, boundary-setting, authentic confidence, self-compassion, healing shame, embodying worth, healthy competition, victories, standing up for oneself |
| **F4** | Community Love / Conformity | Blue | Community love — where F3 grounds power in love of self, F4 grounds structure in love of others; devotion, moral grounding, hierarchy, belonging through shared values | Rules, authority, purpose, faith, discipline, service, frustration with rigidity, moral frameworks, showing up for others, institutional belonging |
| **F5** | Achievism | Orange | Innovation, analysis, goal-setting, material success | Theory-building, critical thinking, research, experimentation, status, competition |
| **F6** | Pluralism | Green | Empathy, inclusivity, embodied connection, shadow work | Relationships, community building, vulnerability, sensitivity, performative vs. genuine connection |
| **F7** | Integration | Yellow | Systems thinking, synthesis, holistic understanding | Connecting frameworks, seeing patterns, building personal philosophy, meta-cognition, creative destruction |
| **F8** | True Self / Transcendence | Teal/Turquoise | The True Self — higher self, monad, Holy Guardian Angel, Atman — the aspect of Source incarnated as you; communicating with it yields deep intuition; doing its will is where true free will lies; it cannot fear because it exists outside of time and in truth | Deep intuition, higher-self dialogue, gnosis, channeling, synchronicity, acting from alignment rather than ego, fearlessness rooted in truth, pattern recognition from a transcendent vantage |
| **F9** | Unity | Ultraviolet | Source connection, cosmic harmony, non-dual awareness | Mystical experience, oneness, dissolution of self/other, devotion, bliss, flow with universal will |
| **F10** | Emptiness | Clear Light | Impermanence, no-self, egolessness | Buddhist insights, letting go of attainment, void, groundlessness, humor about it all |

### 6.2 Classification Guidance

- **Multi-tagging is expected.** A fragment about building a meditation habit touches F1 (agency/discipline), F6 (embodiment), and potentially F9 (unity practice).
- **Don't force it.** If a fragment doesn't clearly map to any Frequency, leave it `unclassified`. The 10-Liminal folder exists for a reason.
- **Context matters.** A conversation about code architecture could be F1 (building), F5 (intellectual), or F7 (systems) depending on the human's relationship to it in that moment.
- **Look for developmental arcs.** If early fragments about a topic are F5 (intellectual understanding) and later ones shift to F6 (embodied), that's a Thread worth naming.

---

## 7. The Archetypal Wavelength Mapping

The Archetypal Wavelength is the cyclical pattern beneath all human experience — moods, creativity, spiritual connection, and energy levels rise and fall in recognizable patterns. This is NOT a mood tracker. It's a cyclical pattern recognizer.

### 7.1 Wavelength Phases

The Archetypal Wavelength has **six phases**, not four or five. These map to a narrative of Abundance and Scarcity:

> *Abundance begins to create Indulgence → Abundance peaks → Indulgence creates Scarcity → Scarcity begins to create Resilience → Scarcity peaks → Resilience creates Abundance*

| Phase | Description | Narrative | Content Signals |
|---|---|---|---|
| **Rising** | Energy building, ideas forming, momentum gathering | Abundance begins to create Indulgence | New project starts, enthusiasm, inspiration, increasing creativity and engagement, commitment, social energy |
| **Peaking** | Full expression, maximum creative/spiritual output | Abundance peaks | Prolific output, flow states, belonging, attunement, meditative absorption, peak experience, glory |
| **Withdrawal** | Energy shifts, first cracks appear, turning inward begins | Indulgence creates Scarcity | Self-doubt creeping in, introspection, distraction, anxiety, tensions arising, the come-down |
| **Diminishing** | Active decline, contraction, things falling apart | Scarcity begins to create Resilience | Loss of focus, depression, alienation, hangover, attempts to avoid conflict, radical acceptance easing the landing |
| **Bottoming Out** | Lowest point, maximum contraction, dark night material | Scarcity peaks | Self-loathing, despair, crisis, complete break from flow, mind wandering, fallow state, collapse |
| **Restoration** | Return begins, new energy gathering, re-emergence | Resilience creates Abundance | Recuperation, vulnerability, "waking up," new creative flow, reconciliation, craving that becomes healthy motivation |

**Additional mappings across domains** (from the Archetypal Wavelength map):

| Domain | Rising | Peaking | Withdrawal | Diminishing | Bottoming Out | Restoration |
|---|---|---|---|---|---|---|
| Season | Summer | Summer Solstice | Fall | Winter | Winter Solstice | Spring |
| Mood (bipolar frame) | Mania | Mania | Mania | Depression | Depression | Depression |
| Spaciousness | Expanded | Expanded | Expanded | Contracted | Contracted | Contracted |
| Relation to Others | Belonging | Belonging | Alienation | Alienation | Alienation | Belonging |
| Relation to Self | Esteem | Esteem | Doubt | Doubt | Doubt | Esteem |
| Buddhist Attachment | Attraction | Attraction | Aversion | Aversion | Aversion | Attraction |
| Addiction | Using | Bliss | Come down | Hangover | Depression | Craving |
| Meditation | Redirecting attention | Absorption | Distraction | Forgetting | Mind wandering | "Waking Up" |
| Breath | Inhale end | Hold in | Exhale begin | Exhale end | Hold out | Inhale begin |

### 7.2 Wavelength Modes and Orientations

Modes are NOT emotions or textures — they are **functional stances** the self takes toward experience. Each Mode pairs with an Orientation (Do or Feel) and manifests differently at each of the nine APTITUDE Frequencies (Spiral Dynamics colors). Critically, every Mode has a **Medicine** (right-sized, healthy) dose and a **Toxic** (overdosed, shadow) expression.

**The five Modes and their Orientations:**

| Mode | Orientation | Frequencies | Description |
|---|---|---|---|
| **Inhabit** | Do / Feel | Beige (Do), Purple (Feel) | Being present in the body and in kinship. The Do orientation grounds through survival action; the Feel orientation grounds through emotional/ancestral connection. |
| **Express** | Do / Feel | Red (Do), Blue (Feel) | Putting energy outward. The Do orientation asserts through power and leadership; the Feel orientation expresses through devotion, ambition, and moral structure. |
| **Collaborate** | Do / Feel | Orange (Do), Green (Feel) | Working with others and with reality. The Do orientation collaborates through experimentation and analysis; the Feel orientation collaborates through empathy and belonging. |
| **Integrate** | Do / Feel | Yellow (Do), Teal (Feel) | Weaving parts into wholes. The Do orientation integrates through systemic thinking and creative destruction; the Feel orientation integrates through gnosis and pattern recognition. |
| **Absorb** | Do/Feel | Ultraviolet (Do/Feel) | Dissolving into unified awareness. Do and Feel merge at this level. |

### 7.3 Medicine vs. Toxic Dose — Full Map

**Medicine (Right-Sized) Expressions:**

| Frequency | Mode | Orientation | Rising | Peaking | Withdrawal | Diminishing | Bottoming Out | Restoration |
|---|---|---|---|---|---|---|---|---|
| **Beige** | Inhabit | Do | Commitment | Diligence | Steadiness | Security | Planning | Next Habit |
| **Purple** | Inhabit | Feel | Inspiration | Joy | Introspectivity | Tranquility | Convalescence | Recuperation |
| **Red** | Express | Do | Leading | Power-With | Stepping Back | Self-Acceptance | Following | Assembling |
| **Blue** | Express | Feel | Ambition | Attunement | Discernment | Conviction | Surrender | Catharsis |
| **Orange** | Collaborate | Do | Hypothesize | Experiment | Collect Data | Analyze | Synthesize | Question |
| **Green** | Collaborate | Feel | Connection | Belonging | Retirement | Unwinding | Repose | Vulnerability |
| **Yellow** | Integrate | Do | Rebellion | Anarchy | Organize | Establish | Order | Disintegrate |
| **Teal** | Integrate | Feel | Epiphany | Gnosis | Receptivity | Absorption | Metabolism | Pattern-Seeking |
| **Ultraviolet** | Absorb | Do/Feel | Unification of Mind | Jhana | Metta and Meditative Joy | Sustained Attention | Pleasure | Directed Attention |

**Toxic (Overdose) Expressions:**

| Frequency | Mode | Orientation | Rising | Peaking | Withdrawal | Diminishing | Bottoming Out | Restoration |
|---|---|---|---|---|---|---|---|---|
| **Beige** | Inhabit | Do | Overcommitment | Thriving | Burnout | Grasping | Overwhelm | New Plan |
| **Purple** | Inhabit | Feel | Grandiosity | Ecstasy | Anxiety | Self-Doubt | Self-Loathing | Selfishness |
| **Red** | Express | Do | Dominating | Power-Over | Crumbling | Shame | Subjugation | Revenge |
| **Blue** | Express | Feel | Voraciousness | Leprosy | Self-Medication | Rage | Misery | Self-Repression |
| **Orange** | Collaborate | Do | Assert | Crusade | Overlook Details | Force it | Fail | Presume |
| **Green** | Collaborate | Feel | Oversharing | Megalomania | Social Anxiety | Alienation | Isolation | Bitterness |
| **Yellow** | Integrate | Do | Mischief | Chaos | Discord | Confusion | Bureaucracy | The Aftermath |
| **Teal** | Integrate | Feel | Delusion | Psychosis | Paranoia | Horror | Despair | Belief Salience |
| **Ultraviolet** | Absorb | Do/Feel | Worldly Desire | Bliss Addiction | Agitation Due to Worry or Remorse | Doubt | Aversion | Laziness or Lethargy |

### 7.4 How to Use Modes for Classification

When classifying a Fragment's Wavelength Mode:

1. **Identify the functional stance** — Is the human Inhabiting (grounding), Expressing (projecting outward), Collaborating (working with), Integrating (synthesizing), or Absorbing (dissolving)?
2. **Identify the orientation** — Is the energy directed through Doing or Feeling?
3. **Identify the dosage** — Is this a healthy, medicine-dose expression? Or has it tipped into toxic overdose territory?
4. **Identify the frequency/color** — Which Spiral Dynamics level is most active?
5. **Cross-reference with the phase** — Where on the six-phase cycle is this fragment situated?

This gives you a rich multi-dimensional tag like: `mode: Collaborate/Feel, dosage: medicine, frequency: Green, phase: Withdrawal` → which maps to "Retirement" (a healthy pulling-back from social engagement). The toxic version of the same position would be "Social Anxiety."

**Classification should preserve both the Medicine and Toxic readings when ambiguous.** Many real-world states hover at the boundary — what looks like healthy Introspectivity (Purple Medicine, Withdrawal) from one angle might be Anxiety (Purple Toxic, Withdrawal) from another. Tag both and let the human resolve it during review.

### 7.5 Temporal Wavelength Tracking

The system should attempt to map the human's wavelength position over time by:

1. **Analyzing emotional texture tags and mode classifications** across fragments within time windows
2. **Detecting phase transitions** (e.g., a cluster of Restoration-phase fragments after a period of Bottoming Out)
3. **Tracking Medicine/Toxic dosage trends** — are fragments trending toward toxic overdose at a particular frequency? This is a high-value signal.
4. **Generating periodic wavelength reports** (weekly/monthly) as notes in `05-Wavelength/Phase-Maps/`
5. **Cross-referencing with real-world cycles** — seasons, menstrual cycles if tracked, circadian patterns, meditation practice consistency
6. **Never prescribing** — the wavelength is descriptive. The system does NOT tell the human what phase they "should" be in or when the next transition will come.

### 7.6 Additional Wavelength Cycles (Reference)

The Archetypal Wavelength map documents dozens of isomorphic cycles across domains. The classification system should recognize content that maps to any of these and tag it with the appropriate phase. Key examples beyond the personal:

- **Discord Server Dynamics**: Excitement → Belonging → Tensions → Avoidance → Crisis → Reconciliation
- **Enshittification** (Doctorow): Good to users → Abuse users → Abuse business customers → Die → New platform
- **Subcultural Movements**: Formation → Relevance → Mainstreaming → Fragmentation → Obscurity → Birth of new
- **Cliodynamics** (Turchin): Cooperation → Peak Power → Inequality → Internal Conflict → Crisis → New Configuration
- **Flow State** (Csikszentmihalyi): Rising focus → Full immersion → Interruption → Loss of focus → Complete break → Return to flow
- **External Validation Loop**: Increasing creativity → Basking in glory → Self-doubt about creation → Refreshing Likes/Views → Seeking validation from real relationship → Feeling the vibe again

These provide classification context: if a fragment discusses community dynamics, the system can assess which phase of the Discord Server Dynamics wavelength it maps to.

---

## 8. Semantic Classification Pipeline

### 8.1 Pipeline Overview

```
Raw Data → Ingestion → Redaction → Conversion → Fragmentation → Classification → Linking → Indexing
```

### 8.2 Stage 0: Redaction (Pre-Ingestion Scan)

Before any content enters the vault, it must pass through a redaction scanner. The human's data almost certainly contains passwords, API keys, tokens, SSNs, credit card numbers, and other sensitive material scattered across chat logs, code files, and notes.

**Pattern-based scanning:**

```python
REDACTION_PATTERNS = {
    "api_key": r'(?:api[_-]?key|token|secret)["\s:=]+["\']?([A-Za-z0-9_\-]{20,})["\']?',
    "password": r'(?:password|passwd|pwd)["\s:=]+["\']?(.+?)["\']?(?:\s|$)',
    "ssn": r'\b\d{3}-\d{2}-\d{4}\b',
    "credit_card": r'\b(?:\d{4}[- ]?){3}\d{4}\b',
    "email_password_combo": r'[\w.+-]+@[\w-]+\.[\w.]+\s*[:/]\s*\S+',
    "aws_key": r'(?:AKIA|ASIA)[A-Z0-9]{16}',
    "private_key": r'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----',
    "bearer_token": r'[Bb]earer\s+[A-Za-z0-9_\-\.]+',
    "env_secret": r'(?:export\s+)?[A-Z_]+(?:KEY|SECRET|TOKEN|PASSWORD|PASS|PWD)\s*=\s*\S+',
    "slack_token": r'xox[bporas]-[0-9a-zA-Z-]+',
    "phone_number": r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',
}
```

**Redaction behavior:**

1. **Scan every file** before conversion to markdown.
2. **Flag, don't silently delete.** When a match is found, replace the sensitive content with a redaction marker: `[REDACTED:api_key]`, `[REDACTED:password]`, etc.
3. **Log all redactions** to `00-Creek-Meta/Processing-Log/redactions.json` with: file path, line number, redaction type, and a salted hash of the original value (so duplicates can be detected without storing the secret).
4. **Generate a review queue** of all redacted fragments. Some matches will be false positives (e.g., a long hex string that's actually a git commit hash). The human should review and approve or restore.
5. **Never store raw sensitive data** in the vault, the processing log, or any temporary file. If a fragment contains a password, the password must be redacted before the markdown file is written.
6. **Scan code files with extra care.** `.env` files, config files, and code containing hardcoded credentials are the most likely sources. Scan for common patterns but also for variable names that suggest secrets.

**CLI interface:**

```bash
# Scan without modifying (dry run)
creek redact --scan --source /path/to/data --report

# Redact and ingest
creek redact --apply --source /path/to/data

# Review redactions
creek redact --review --vault /path/to/Creek-Vault
```

### 8.3 Stage 1: Ingestion

For each data source, build a dedicated ingestion script:

```python
# Pseudocode structure for each ingestor
class Ingestor:
    def discover(self, source_path: str) -> list[RawDocument]:
        """Find all files/records in the source"""

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract structured content from raw format"""

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert to clean markdown preserving all formatting"""

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict:
        """Generate initial YAML frontmatter with source metadata"""
```

**Ingestors to build:**

1. `ingest_claude.py` — Parse Claude JSON exports into conversation turns → individual fragment notes
2. `ingest_chatgpt.py` — Parse ChatGPT JSON exports similarly
3. `ingest_discord.py` — Parse Discord data package (messages.json per channel) into fragments, preserving channel context, timestamps, and reply chains
4. `ingest_markdown.py` — Process existing .md files, preserve content, add frontmatter
5. `ingest_documents.py` — Convert DOCX, PDF, HTML, TXT to markdown (use pandoc or python-docx + markdownify; use pdfminer.six for text PDFs)
6. `ingest_images.py` — OCR images (screenshots, photos of handwritten notes) via pytesseract; extract text and create fragments linking back to the original image
7. `ingest_code.py` — Extract READMEs, significant comments, docstrings, and architectural decisions from code repos
8. `ingest_gdrive.py` — Download from Google Drive (read-only API, single download call, local staging) then route to appropriate parser (DOCX/XLSX/PPTX/PDF). XLSX and PPTX require dedicated sub-parsers: use openpyxl to extract sheet data into markdown tables, use python-pptx to extract slide content into sequential markdown sections.
9. `ingest_generic.py` — Fallback for unrecognized formats

**Key ingestion rules:**

- **Normalize encoding.** All files must be converted to UTF-8 before processing. Detect original encoding (chardet), record it in frontmatter as `original_encoding`, and normalize. Discord exports in particular can have emoji and special character encoding issues.
- **Normalize timestamps to America/Los_Angeles.** Parse timestamps from every source format (Discord uses UTC, Claude exports vary, journal entries may have no timezone). Convert all to ISO 8601 with `-08:00` or `-07:00` offset. Record the original timestamp format in the processing log.
- **Preserve timestamps.** Every fragment must retain its original creation time (post-normalization).
- **Preserve attribution.** In conversations, clearly mark which text is the human's and which is the AI's or other party's.
- **Preserve context.** A Discord message that is a reply should include the parent message as context.
- **Preserve formatting.** Code blocks, headers, lists, emphasis — all must survive conversion to markdown.
- **Extract text from images.** Use pytesseract (OCR) for screenshots, photos of handwritten notes, or any image-based content. Store the OCR'd text as a fragment with `source.platform: image_ocr` and link to the original image file.
- **Extract text from PDFs.** Use pdfminer.six for text-based PDFs, pytesseract for scanned PDFs.
- **Generate unique IDs.** Each fragment gets a deterministic ID based on source + timestamp + content hash to prevent duplicates.
- **Maintain a provenance log.** Write a structured provenance entry for every file processed to `00-Creek-Meta/Processing-Log/provenance.json`. Each entry must link the output markdown file back to its exact original file path, byte offset (for chat logs), original encoding, and any transformations applied. This log is the system's audit trail and must be append-only.
- **Log everything.** Additionally, write human-readable ingestion summaries per batch to `00-Creek-Meta/Processing-Log/`.

### 8.4 Stage 2: Fragmentation

Not all source documents map 1:1 to fragments. A long Claude conversation might yield 20+ fragments. A short Discord message might be one fragment or might need to be grouped with surrounding messages for context.

**Fragmentation heuristics:**

- **Chatbot conversations:** Each human turn + AI response = one fragment. If the human's message is a multi-topic request, consider splitting. If a conversation is short (< 5 turns) and focused on a single topic, the whole conversation can be one fragment.
- **Discord messages:** Group by conversational thread (replies + nearby messages from same author within a time window). Isolated messages that stand alone as insights get their own fragment.
- **Essays/posts:** Keep as single fragments unless they're very long (>3000 words), in which case split by section while preserving a parent note that links to all sections.
- **Journal entries:** One fragment per entry unless very long.
- **Code:** Extract the human-readable thinking (READMEs, comments, commit messages, architecture decisions), not the code itself (unless the code IS the insight).

### 8.5 Stage 3: Classification

This is the core intelligence of the pipeline. Use a multi-pass approach:

**Pass 1: Rule-Based Pre-Classification**

```python
# Keyword and pattern matching for obvious signals
FREQUENCY_SIGNALS = {
    "F1": ["goal", "plan", "build", "ship", "discipline", "habit", "survival", "willpower", "resource", "security"],
    "F2": ["surrender", "receive", "allow", "trust", "rest", "kinship", "ancestral", "ritual", "myth", "tribe"],
    "F3": ["power", "assert", "dominat", "strength", "boundar", "compet", "conquer", "victory", "overreach"],
    "F4": ["structur", "moral", "authority", "rule", "hierarch", "faith", "devot", "purpose", "conform"],
    "F5": ["theory", "framework", "analysis", "model", "innovati", "rational", "experiment", "achieve", "status"],
    "F6": ["empath", "inclusi", "shadow", "embod", "felt sense", "vulnerab", "belong", "plural", "connect"],
    "F7": ["system", "synthesiz", "integrat", "meta", "pattern", "holistic", "unique", "yellow"],
    "F8": ["true self", "higher self", "monad", "hga", "atman", "deep intuition", "gnosis", "transcend", "turquoise", "teal", "free will", "alignment", "fearless"],
    "F9": ["oneness", "source", "unity", "cosmic", "divine", "devotion", "yoke", "ultraviolet", "bliss"],
    "F10": ["impermanence", "empty", "void", "no-self", "anatta", "groundless", "death", "egoless", "clear light"],
}

WAVELENGTH_PHASE_SIGNALS = {
    "rising": ["starting", "beginning", "momentum", "excited", "new project", "inspiration", "commitment"],
    "peaking": ["flow", "immersed", "peak", "glory", "abundance", "bliss", "attunement", "belonging"],
    "withdrawal": ["doubt", "anxious", "turning inward", "come down", "distraction", "tension"],
    "diminishing": ["depressed", "declining", "alienat", "hangover", "fading", "self-doubt", "avoidance"],
    "bottoming_out": ["despair", "crisis", "self-loathing", "collapse", "dark night", "fallow", "exhausted"],
    "restoration": ["returning", "waking up", "recuperat", "new energy", "vulnerability", "reconciliation"],
}

MODE_SIGNALS = {
    "inhabit": ["body", "grounding", "survival", "comfort", "somatic", "rest", "home", "safety"],
    "express": ["leading", "asserting", "creating", "performing", "devotion", "ambition", "structure"],
    "collaborate": ["experiment", "team", "research", "community", "connection", "hypothesis"],
    "integrate": ["system", "pattern", "rebellion", "epiphany", "meta-", "synthesis", "gnosis"],
    "absorb": ["meditation", "jhana", "metta", "unification", "absorption", "dissolution"],
}
```

**Pass 2: LLM-Assisted Classification**

For fragments that aren't clearly classified by rules, batch them for LLM classification:

```python
CLASSIFICATION_PROMPT = """
You are classifying fragments of personal writing, conversation, and reflection
for a Liminal Trickster Mystic's knowledge system.

Given the following fragment, provide:
1. Primary APTITUDE Frequency (F1-F10 or unclassified) and its Spiral Dynamics color
2. Secondary Frequencies (list, can be empty)
3. Wavelength Phase (rising/peaking/withdrawal/diminishing/bottoming_out/restoration/unclassified)
4. Wavelength Mode (inhabit/express/collaborate/integrate/absorb/unclassified)
5. Wavelength Orientation (do/feel/do_feel/unclassified)
6. Wavelength Dosage (medicine/toxic/ambiguous/unclassified)
   - Medicine = right-sized, healthy expression
   - Toxic = overdosed, shadow expression
   - Ambiguous = could be read either way
7. Wavelength Descriptor (the specific word from the Mode map that best fits, 
   e.g. "Introspectivity", "Anxiety", "Power-With", "Gnosis", etc.)
8. Voice Register (confessional/analytical/playful/prophetic/instructional/raw/conversational)
9. Confidence Level (musing/exploring/forming/settled/conviction)
10. Emotional Texture (2-5 free-form tags)
11. Praxis Potential (none/latent/explicit)
12. Suggested Thread names (existing or new)
13. A 1-sentence summary suitable as a note title

IMPORTANT: Do not force classifications. "Unclassified" is a valid and 
respected answer. Liminal content that resists categorization is VALUABLE.
Do not resolve paradoxes or contradictions — tag them with 
"paradox" in emotional_texture.
When Medicine vs. Toxic is ambiguous, use "ambiguous" and note both readings.

Fragment:
---
{fragment_content}
---

Respond in valid YAML only.
"""
```

**Pass 3: Human Review Queue**

Generate a review queue of fragments where:
- LLM confidence was low
- Content was flagged as potentially sensitive (journal entries, recovery content)
- Classification was "unclassified" across multiple dimensions
- Contradictions were detected between automated classifications

Store the review queue as a Kanban board in `00-Creek-Meta/Review-Queue.md`.

### 8.6 Stage 4: Linking

After classification, run a linking pass to discover Resonances:

1. **Semantic Similarity** — Use embeddings (sentence-transformers or similar) to find fragments with high cosine similarity that aren't from the same source. These are potential Resonances.
2. **Temporal Proximity + Thematic Overlap** — Fragments from the same time period with overlapping Frequency tags but different sources (e.g., a Discord message and a Claude conversation from the same week about the same topic).
3. **Thread Detection** — Clusters of fragments over time with consistent thematic content form Threads. Use a sliding time window + topic consistency metric.
4. **Eddy Formation** — Dense clusters of interlinked fragments that don't have a clear temporal direction form Eddies.

Generate wiki-links (`[[note-name]]`) between related fragments and add them to the appropriate frontmatter arrays.

### 8.7 Stage 5: Indexing

Generate index notes that serve as entry points:

- **Frequency Index Notes** — One per frequency in `06-Frequencies/`, containing Dataview queries that pull all fragments tagged with that frequency
- **Thread Index** — Master list of all threads with status and fragment counts
- **Eddy Map** — Overview of all clusters
- **Temporal Index** — Year → Month → Week views of fragment creation
- **Voice Register Index** — Examples and analysis for each register
- **Source Index** — What came from where, with statistics

---

## 9. Tooling & Scripts to Build

### 9.1 Project Structure

```
creek-tools/
├── pyproject.toml                  # Project config, use Poetry or uv
├── README.md
├── creek/
│   ├── __init__.py
│   ├── config.py                   # Vault paths, API keys, settings
│   ├── models.py                   # Pydantic models for all primitives
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract Ingestor base class
│   │   ├── claude.py
│   │   ├── chatgpt.py
│   │   ├── discord.py
│   │   ├── markdown.py
│   │   ├── documents.py
│   │   ├── images.py               # OCR via pytesseract
│   │   ├── gdrive.py               # Google Drive read-only download + routing
│   │   ├── spreadsheets.py         # XLSX/CSV → markdown tables (openpyxl/pandas)
│   │   ├── presentations.py        # PPTX → markdown sections (python-pptx)
│   │   ├── code.py
│   │   └── generic.py
│   ├── redact/
│   │   ├── __init__.py
│   │   ├── patterns.py             # Regex patterns for sensitive data detection
│   │   ├── scanner.py              # Scan fragments for sensitive data pre-ingestion
│   │   └── redactor.py             # Replace or flag sensitive data
│   ├── classify/
│   │   ├── __init__.py
│   │   ├── rules.py                # Rule-based pre-classification
│   │   ├── llm.py                  # LLM-assisted classification
│   │   └── review.py               # Review queue generation
│   ├── link/
│   │   ├── __init__.py
│   │   ├── embeddings.py           # Semantic similarity via embeddings
│   │   ├── temporal.py             # Time-based proximity linking
│   │   ├── threads.py              # Thread detection
│   │   └── eddies.py               # Eddy/cluster formation
│   ├── generate/
│   │   ├── __init__.py
│   │   ├── indexes.py              # Generate index notes
│   │   ├── wavelength.py           # Wavelength tracking and reports
│   │   ├── voice.py                # Voice analysis and proxy generation
│   │   └── decisions.py            # Decision support scaffolding
│   ├── vault/
│   │   ├── __init__.py
│   │   ├── writer.py               # Write markdown + frontmatter to vault
│   │   ├── linker.py               # Add/update wiki-links in existing notes
│   │   └── templates.py            # Generate Obsidian templates
│   └── cli.py                      # CLI entry point
├── tests/
│   ├── test_ingest/
│   ├── test_classify/
│   ├── test_link/
│   └── fixtures/                   # Sample data for testing
└── scripts/
    ├── full_pipeline.sh            # Run the entire pipeline
    ├── incremental_ingest.sh       # Ingest new data only
    └── generate_reports.sh         # Generate wavelength/voice reports
```

### 9.2 CLI Interface

```bash
# Full pipeline
creek process --source /path/to/external/drive --vault /path/to/Creek-Vault

# Download from Google Drive (read-only, to local staging)
creek gdrive --download --staging /path/to/external/drive/google-drive-export/

# Redact sensitive data (dry run first, then apply)
creek redact --scan --source /path/to/external/drive --report
creek redact --apply --source /path/to/external/drive
creek redact --review --vault /path/to/Creek-Vault

# Ingest specific source
creek ingest --type claude --input /path/to/claude_export.json --vault /path/to/Creek-Vault

# Classify unclassified fragments
creek classify --vault /path/to/Creek-Vault --method llm --batch-size 50

# Run linking pass
creek link --vault /path/to/Creek-Vault --method embeddings

# Generate reports
creek report --type wavelength --period monthly --vault /path/to/Creek-Vault
creek report --type voice --vault /path/to/Creek-Vault
creek report --type threads --vault /path/to/Creek-Vault

# Generate voice skills
creek skills --generate --vault /path/to/Creek-Vault --output /path/to/creek-skills/

# Mine blog ideas
creek mine --vault /path/to/Creek-Vault --strategy liminal-cross-eddy
creek mine --vault /path/to/Creek-Vault --strategy thread-terminus
creek mine --vault /path/to/Creek-Vault --strategy resonance-chains

# Interactive review
creek review --vault /path/to/Creek-Vault
```

### 9.3 Key Dependencies

```toml
[tool.poetry.dependencies]
python = "^3.11"
pydantic = "^2.0"
pyyaml = "^6.0"
rich = "^13.0"           # Beautiful CLI output
typer = "^0.9"           # CLI framework
python-frontmatter = "^1.0"
markdownify = "^0.11"    # HTML to markdown
python-docx = "^1.0"     # DOCX parsing
python-pptx = "^0.6"     # PPTX slide content extraction
openpyxl = "^3.1"        # XLSX reading and structured extraction
pdfminer-six = "^20221105"  # PDF text extraction (preferred over pypdf for text)
pytesseract = "^0.3"     # OCR for images and scanned PDFs
Pillow = "^10.0"         # Image handling for OCR
chardet = "^5.0"         # Encoding detection for UTF-8 normalization
sentence-transformers = "^2.0"  # Embeddings for semantic linking (runs locally)
scikit-learn = "^1.3"    # Clustering for eddy detection
ollama = "^0.2"          # Local LLM inference (default provider)
anthropic = "^0.30"      # Cloud LLM classification (opt-in only)
google-api-python-client = "^2.0"  # Google Drive API (read-only download)
google-auth-oauthlib = "^1.0"      # OAuth for Drive authentication
tqdm = "^4.0"            # Progress bars for batch processing
pytz = "^2024.1"         # Timezone normalization
```

### 9.4 Configuration

```yaml
# creek_config.yaml
vault_path: "/path/to/Creek-Vault"
source_drive: "/path/to/external/drive"
timezone: "America/Los_Angeles"

llm:
  # DEFAULT: Local-only processing via Ollama. No data leaves your machine.
  # Cloud API is available as an explicit opt-in for higher-quality classification,
  # but sends your fragments to a third-party server. Use with awareness.
  provider: ollama  # ollama (local, default) | anthropic | openai
  model: mistral    # for ollama; or claude-sonnet-4-5-20250929 for anthropic
  ollama_url: "http://localhost:11434"
  batch_size: 50
  max_concurrent: 5
  # To enable cloud classification (opt-in, sends data externally):
  # provider: anthropic
  # model: claude-sonnet-4-5-20250929
  # api_key_env: ANTHROPIC_API_KEY  # read from environment variable, never stored in config

embeddings:
  model: all-MiniLM-L6-v2  # runs locally via sentence-transformers
  similarity_threshold: 0.75  # for resonance detection

ocr:
  enabled: true
  engine: pytesseract
  languages: ["eng"]
  
linking:
  temporal_window_hours: 168  # 1 week window for temporal proximity
  thread_min_fragments: 3     # minimum fragments to form a thread
  eddy_min_fragments: 5       # minimum fragments to form an eddy

classification:
  confidence_threshold: 0.7   # below this, add to review queue
  auto_classify_sources:      # sources safe to auto-classify
    - claude
    - chatgpt
    - discord
  human_review_sources:       # sources that always need human review
    - journal

redaction:
  enabled: true
  dry_run: false              # set true to scan without modifying
  custom_patterns: {}         # add project-specific patterns here
  false_positive_allowlist: [] # hashes of approved "false positive" matches

google_drive:
  credentials_file: "credentials.json"  # OAuth credentials (gitignored)
  token_file: "token.json"              # OAuth token (gitignored)
  scopes: ["https://www.googleapis.com/auth/drive.readonly"]  # READ-ONLY, non-negotiable
  staging_dir: "google-drive-export/"   # download destination within source_drive
  
# Paths within source drive
sources:
  claude: "chatbot-exports/claude/"
  chatgpt: "chatbot-exports/chatgpt/"
  discord: "discord-export/"
  gdrive: "google-drive-export/"
  aptitude: "projects/aptitude/course-files/"
  essays: "writing/substack/"
  journal: "personal/journal/"
  code: "projects/"
```

---

## 10. Emergence Infrastructure

This section defines infrastructure specifically designed to let patterns surface that the ontology doesn't predict.

### 10.1 The Unnamed Folder

`10-Liminal/Unnamed/` is for fragments and patterns that resist all classification. This folder is NOT a failure state — it's the most important research site in the vault.

Generate a weekly `Unnamed Digest` note that:
- Lists all fragments added to Unnamed that week
- Runs embedding similarity against Unnamed fragments to see if they cluster with each other
- Asks (via generated prompt): "What do these have in common that the current ontology can't express?"

### 10.2 Paradox Preservation

When the classification system detects contradictory stances across fragments from the same person:
- Do NOT resolve the contradiction
- Create a note in `10-Liminal/Paradoxes/` that links both fragments
- Tag with `#paradox` and the relevant Frequencies
- Paradoxes are data points about the human's polygnostic nature, not errors to fix

### 10.3 Synchronicity Detection

When the linking pass discovers surprising resonances — fragments from very different sources/times that are semantically near-identical — flag them as potential synchronicities in `10-Liminal/Synchronicities/`.

Criteria for synchronicity flagging:
- Semantic similarity > 0.9
- Source types are different (e.g., a Discord message and a journal entry)
- Created > 30 days apart
- Not obviously about the same project/task (filter out "still working on X" noise)

### 10.4 Compost Tracking

`10-Liminal/Compost/` is for ideas, projects, and threads that died or were abandoned. These are NOT deleted — they decompose into future growth.

When a Thread status changes to `resolved` or when fragments reference abandoned projects, create a compost note that preserves:
- What the idea was
- Why it was abandoned (if known)
- What energy or insight it contained that might still be alive
- Links to any fragments that reference it

### 10.5 Emergent Tag Garden

Maintain a tag taxonomy file at `00-Creek-Meta/Tag-Garden.md` that:
- Lists all tags in use with counts
- Highlights tags that are growing rapidly
- Flags tag clusters that might indicate a new Thread or Eddy
- Runs quarterly to suggest tag consolidation or splitting

---

## 11. Voice Proxy Generation

One of the system's key outputs is a writing voice proxy — enough analyzed material about the human's writing voice that an LLM can faithfully approximate it.

### 11.1 Voice Analysis Pipeline

1. **Collect exemplars** — Pull all fragments with `voice.confidence >= settled` and group by register
2. **Extract patterns:**
   - Sentence structure preferences (length, complexity, fragment usage)
   - Paragraph structure (how ideas develop within a paragraph)
   - Transition patterns (how the human moves between ideas)
   - Metaphor families (creek/water, wave/surf, light/dark, etc.)
   - Rhetorical moves (self-deprecation before insight, paradox construction, etc.)
   - Vocabulary fingerprint (distinctive words, coinages, recurring phrases)
   - Punctuation habits (em-dash usage, ellipsis, parenthetical asides)
3. **Generate register profiles** — For each voice register, produce a profile note with:
   - 5-10 exemplar passages
   - Identified patterns
   - A "voice prompt" that could instruct an LLM to write in this register

### 11.2 Voice Prompt Template

Generate a reusable prompt for each register, stored in `07-Voice/`:

```markdown
# Voice Proxy: {Register Name}

## Prompt for LLM

Write in the voice of a Liminal Trickster Mystic with the following characteristics:

### Structural Patterns
{extracted patterns}

### Metaphor Families
{recurring metaphor domains}

### Rhetorical Moves
{identified moves}

### Sample Passages
{5-10 exemplars}

### Anti-Patterns (DO NOT)
{things this voice never does}
```

### 11.3 Lexicon Generation

Build a living glossary at `07-Voice/Lexicon/` containing:
- **Coined terms** (polygnosticism, Liminal Trickster Mystic, Whole Adept, Liminal Creep, etc.)
- **Recurring metaphors** with the contexts they appear in
- **Distinctive phrases** and their frequency of use
- **Terms borrowed from specific traditions** with the human's particular spin on them

### 11.4 Voice Skill Tree (Claude Code Skills)

This is a primary deliverable of the project. Using Claude Code's skill creation capability, the system should generate a **tree of SKILL.md files** — one for each meaningful organizational unit in the ontology — that teach an LLM how to write in the human's voice when that topic, frequency, phase, or mode is relevant.

**Skill generation passes:**

1. **Frequency Skills** — One SKILL.md per APTITUDE Frequency (F1–F10). Each skill file explains what this frequency sounds like in the human's voice: what metaphors surface, what rhetorical moves appear, what emotional texture is typical, what the Medicine vs. Toxic dosage sounds like in prose. Built from exemplar fragments classified to that frequency.

2. **Wavelength Phase Skills** — One SKILL.md per phase (Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration). Each explains how the human's writing changes across the cycle — the energy, sentence structure, confidence level, and typical topics that arise in each phase.

3. **Mode Skills** — One SKILL.md per Mode/Orientation pair (Inhabit-Do, Inhabit-Feel, Express-Do, Express-Feel, Collaborate-Do, Collaborate-Feel, Integrate-Do, Integrate-Feel, Absorb-Do/Feel). These capture the functional stance of the writing — is the human grounding, projecting, co-creating, synthesizing, or dissolving?

4. **Thread Skills** — For major Threads that emerge from the data, generate SKILL.md files that capture the narrative arc: what this thread is about, how the human's thinking on it has evolved, what voice register and frequency it tends to activate.

5. **Eddy Skills** — For significant Eddies (dense topic clusters), generate SKILL.md files that capture the cluster's gravitational center: what concepts pool here, what contradictions live here, what the human keeps returning to.

6. **Register Skills** — One SKILL.md per voice register (confessional, analytical, playful, prophetic, instructional, raw, conversational) with the full voice prompt template from Section 11.2.

**Skill file structure:**

```
creek-skills/
├── frequencies/
│   ├── F1-agency-beige.SKILL.md
│   ├── F2-receptivity-purple.SKILL.md
│   └── ...
├── phases/
│   ├── rising.SKILL.md
│   ├── peaking.SKILL.md
│   └── ...
├── modes/
│   ├── inhabit-do.SKILL.md
│   ├── inhabit-feel.SKILL.md
│   └── ...
├── threads/
│   ├── {thread-name}.SKILL.md
│   └── ...
├── eddies/
│   ├── {eddy-name}.SKILL.md
│   └── ...
├── registers/
│   ├── confessional.SKILL.md
│   ├── analytical.SKILL.md
│   └── ...
└── meta/
    ├── voice-core.SKILL.md          # The master voice profile
    └── skill-activation-guide.SKILL.md  # How to combine skills
```

Each SKILL.md should follow the Claude Code skill format and include:
- A description of when to activate this skill
- Exemplar passages from the corpus
- Specific writing instructions (sentence structure, metaphor families, rhetorical moves)
- Anti-patterns (what this voice/mode/frequency NEVER sounds like)
- Guidance on combining with other skills (e.g., "When writing in F3/Red + Withdrawal phase, the voice tends to shift from confidence toward vulnerable honesty")

### 11.5 Blog & Essay Mining Pipeline

The skill tree enables a generative writing workflow. The system should support:

**Idea Mining:**

1. **Liminal × Eddy Cross-Pollination** — Systematically cross-reference fragments in `10-Liminal/Unnamed/` and `10-Liminal/Compost/` with established Eddies. Surface combinations where decomposing or uncategorized ideas intersect with active topic clusters. These intersections are high-potential essay seeds.

2. **Thread Terminus Detection** — Identify Threads that have accumulated many fragments but never produced a published essay. These are ideas the human keeps circling without landing — prime candidates for a piece that finally gives the thread a home.

3. **Resonance Chains** — Find chains of 3+ fragments connected by high-similarity Resonances that span different sources (e.g., a Discord rant → a Claude conversation → a journal entry, all touching the same insight from different angles). These chains often contain an essay waiting to be synthesized.

4. **Wavelength-Phase Opportunity Windows** — When the human is in a Rising or Peaking phase, surface high-praxis-potential threads. When in Bottoming Out or Restoration, surface compost and liminal material — the dark-night insights that might become the most powerful writing.

**Idea Presentation (a skill in itself):**

Build a `creek-skills/meta/idea-surfacing.SKILL.md` that teaches Claude Code how to:
- Query the Obsidian vault (via Dataview or direct file access) for mining candidates
- Present 3–5 essay ideas at a time with brief context for each
- Include links to the source fragments so the human can browse before committing
- Never pitch ideas as imperatives — always as invitations
- Respect wavelength phase: don't push high-energy ideas during a trough

**Draft Generation:**

When the human selects an idea and wants a draft:
1. Claude Code activates the relevant skill stack — e.g., for an essay about community alienation, it might load `F4-community-love-blue.SKILL.md` + `F6-pluralism-green.SKILL.md` + `registers/confessional.SKILL.md` + `phases/withdrawal.SKILL.md`
2. It pulls all related fragments as source material
3. It generates a draft that sounds like the human, not like an AI, because the skill tree provides the specific voice instructions
4. The draft is saved to a `07-Voice/Drafts/` folder with frontmatter linking it to the source fragments, activated skills, and target Thread/Eddy

**The human always makes the final call.** The system mines, suggests, and drafts. The human decides what gets published and what goes back to compost.

---

## 12. Decision Support Layer

### 12.1 Decision Ontology

Decisions are treated as living processes, not binary events. The Creek Decision Ontology tracks decisions through five phases:

1. **Sensing** — Something needs to be decided. The system detects decision-relevant fragments (keywords: "should I", "trying to decide", "weighing options", "not sure whether").
2. **Deliberating** — Options are being explored. The system pulls relevant Threads, Praxis notes, and past decisions that might inform this one.
3. **Committing** — A direction is chosen. The system records the decision and the reasoning.
4. **Enacting** — The decision is being lived. The system watches for fragments that reference the decision.
5. **Reflecting** — Looking back. The system prompts reflection on how the decision played out.

### 12.2 Decision Detection

The classification pipeline should flag fragments with decision-relevant content and auto-generate draft Decision notes in `08-Decisions/Active/`.

### 12.3 Decision Context Gathering

When a Decision note is created (manually or auto-detected), generate a `## Context` section that pulls:
- Related Threads (by semantic similarity to the decision topic)
- Related past Decisions (similar themes or stakes)
- Current Wavelength phase (is the human deciding from a Peaking or Bottoming Out phase?)
- Relevant Praxis notes (frameworks or practices that apply)
- Frequency affinity (which developmental stage is this decision most related to?)

### 12.4 Decision Anti-Manipulation Guardrail

The decision layer must NEVER:
- Recommend a specific option
- Weight options by "rationality" over felt sense
- Dismiss emotional or intuitive input as less valid
- Use urgency, scarcity, or loss-aversion framing
- Present decisions as permanent or irreversible unless they truly are

It SHOULD:
- Surface relevant context
- Note the current Wavelength phase as potentially relevant
- Highlight if similar decisions have been made before and what happened
- Respect that some decisions need to be made from the gut, not from analysis

### 12.5 Interventions: Phase × Frequency Practice Map

The Archetypal Wavelength map includes a detailed Interventions dataset — specific practices mapped to the Wavelength Phase and APTITUDE Frequency where they are most effective. This data should be ingested as reference material in `09-Reference/` and cross-linked with the Praxis layer.

**Sample Interventions (from the canonical map):**

| Practice | Frequency/Color | Phase | Notes |
|---|---|---|---|
| Cold Shower | Beige | Rising | Activation energy, basic embodiment |
| Listening to Music | Purple | Bottoming Out | Emotional soothing, ancestral connection |
| Pranayama (Lion's Breath) | Red | Bottoming Out | Energy mobilization through power |
| Metta Meditation | Blue | Rising | Heart-opening, empathy cultivation |
| Pranayama (Wim Hof) | Orange | Rising | Experimental, analytic approach to breathwork |
| Yoga | Green | Rising | Embodied community practice |
| Creative Writing | Green | Rising | Expression through felt sense |
| Samatha Vipassana | Yellow | Peaking | Systematic meditation at peak clarity |
| Magick | Teal | Peaking | Gnosis-work at peak intuition |
| Meditation Retreats | Ultraviolet | Peaking | Deep absorption practice |
| Journaling | Green | Diminishing | Processing through written vulnerability |
| Baby Waterfall Conventions | Teal | Bottoming Out | Pattern-seeking during dark night |

The full Interventions dataset from the spreadsheet should be imported as a structured reference note. When the Wavelength tracking system detects the human is in a particular Phase, Praxis notes should surface relevant Interventions — not as prescriptions, but as reminders of what has been mapped as useful in that territory.

---

## 13. Ethical Guardrails

### 13.1 Data Sovereignty

- All data stays on the local machine / external drive. No cloud sync unless the human explicitly sets it up.
- **Local-first by default.** The LLM classification pipeline defaults to Ollama (local inference). Cloud API (Anthropic, OpenAI) is available as an explicit opt-in, clearly documented as sending fragments to third-party servers. The human must consciously choose this.
- **No external API calls during ingestion.** The ingestion, conversion, and fragmentation stages must work fully offline. Only the LLM classification pass (if cloud provider is opted-in) and optional embedding model downloads require network access.
- No telemetry, no analytics, no usage tracking.
- Embedding models (sentence-transformers) run locally and should be downloaded once and cached.
- The config file must NEVER contain API keys directly. Use environment variables (`api_key_env: ANTHROPIC_API_KEY`).

### 13.2 Privacy Tiers

Some data is more sensitive than others. The system should respect this:

| Tier | Sources | Handling |
|---|---|---|
| **Open** | Published essays, public Discord messages | Can be processed fully automatically |
| **Personal** | Chatbot conversations, private Discord messages | Auto-process but flag sensitive content for review |
| **Intimate** | Journal entries, recovery-related content | Always require human review before classification. Never include in voice proxy generation without explicit consent. |

### 13.3 No Weaponization

The system must not be usable for:
- Building persuasion profiles (even self-persuasion)
- Extracting "leverage" from patterns in the human's data
- Generating manipulative content based on the human's voice
- Reducing the human to a set of predictable patterns

### 13.4 Right to Be Forgotten (by the System)

The human must be able to:
- Delete any fragment permanently
- Remove any fragment from all Threads and Eddies it appears in
- Exclude entire source types from the vault
- Wipe all LLM-generated classifications and re-run from scratch
- Export and then destroy the entire vault

Build a `creek purge` CLI command that handles this cleanly.

### 13.5 Consent Architecture

Before processing any data source for the first time, the CLI should:
1. Show a summary of what was found (file counts, date ranges, apparent content types)
2. Ask for explicit confirmation to proceed
3. Allow exclusion of specific files or date ranges
4. Record the consent in the Processing Log

---

## 14. Implementation Plan

### Phase 1: Foundation (Build the house)

1. Set up the Python project with Poetry/uv
2. Implement Pydantic models for all ontological primitives
3. Build the vault writer (markdown + frontmatter generation)
4. Create Obsidian templates for each primitive type
5. Generate the empty vault folder structure
6. Build the CLI skeleton with Typer
7. Build the redaction scanner and pattern library

### Phase 2: Ingestion (Fill the creek)

8. Build the redaction pipeline → test with synthetic sensitive data → verify no leaks
9. Build ingestors one at a time, starting with the highest-volume source (likely Claude exports)
10. Build the Claude ingestor → test with real data → iterate
11. Build the Discord ingestor → test → iterate
12. Build remaining ingestors (ChatGPT, documents, markdown, code, images/OCR)
13. Build the Google Drive downloader (read-only API, local staging)
14. Build XLSX, PPTX, and PDF sub-parsers for Drive content
15. Process all source data through redaction → ingestion → `01-Fragments/Unsorted/`

### Phase 3: Classification (Name the currents)

16. Implement rule-based pre-classification (frequencies, phases, modes)
17. Implement LLM-assisted classification with the prompt from Section 8.5
18. Run classification on all fragments
19. Generate review queue for uncertain classifications
20. Build Dataview queries for all Frequency index notes

### Phase 4: Linking (Weave the web)

21. Generate embeddings for all fragments
22. Run semantic similarity linking
23. Run temporal proximity linking
24. Implement Thread detection
25. Implement Eddy formation
26. Generate all wiki-links

### Phase 5: Intelligence (Listen to the creek)

27. Build the Voice analysis pipeline
28. Generate voice register profiles and proxy prompts
29. Build the Wavelength tracking system
30. Build the Decision support layer
31. Generate all index notes and reports

### Phase 6: Emergence (Let it breathe)

32. Set up the Unnamed Digest
33. Implement Paradox Preservation
34. Implement Synchronicity Detection
35. Build the Compost system
36. Generate the Tag Garden
37. Create the `creek purge` command
38. Write the vault README with usage guide

### Phase 7: Voice Skills & Generative Writing (Speak)

39. Generate the Voice Skill Tree — one SKILL.md per frequency, phase, mode, register
40. Generate Thread and Eddy skills for major clusters
41. Build the blog idea mining pipeline (liminal×eddy, thread terminus, resonance chains)
42. Build the idea-surfacing skill (vault queries → presented options)
43. Build the draft generation workflow (skill stack activation → draft → review)
44. Test end-to-end: mine an idea → activate skills → generate a draft → human reviews

---

## Final Notes for the Agent

**Remember:** You are building infrastructure for a creek, not a dam. The water (data) knows where it wants to go. Your job is to clear the path, name the features of the landscape, and make it possible for the human to navigate — not to control the flow.

The human who will use this system is a Liminal Trickster Mystic. He lives between worlds. He holds contradictions without needing to resolve them. He finds meaning in the spaces between categories. Build a system worthy of that disposition.

When in doubt, leave room. When something doesn't fit, celebrate it. When a pattern emerges that the ontology can't contain, **expand the ontology**.

The creek is always moving. Build for that.

---

*This document is itself a Fragment. Tag it F7 (Integration/Yellow), mode: Integrate/Do, dosage: medicine, phase: Peaking, register: instructional, confidence: settled.*
