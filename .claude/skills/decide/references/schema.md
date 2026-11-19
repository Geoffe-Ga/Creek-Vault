# Decision Sheet Schema

`scripts/build.py` validates every field below and lists every problem it finds. Keep ids to lowercase letters, digits, and hyphens.

## Top level

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Stable slug, e.g. `2026-11-07-trust-model`. It keys the viewer's saved answers, so **keep it the same when you rebuild the same sheet** and change it for a new sheet. |
| `title` | yes | The page name: a short noun phrase ("Billing Launch Calls"), not a sentence. |
| `prompt` | no | The user's request, close to verbatim. Shown as "Prompt: …". |
| `context` | no | 1–3 sentences on what is already settled and what this sheet decides. |
| `created` | no | ISO date. |
| `decisions` | yes | 1–8 decisions, upstream decisions first. |
| `theme` | no | Brand tokens from DESIGN.md (see below). Omit when there is no DESIGN.md. |

## Decision

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Unique within the sheet. |
| `title` | yes | Short name of the fork ("Trust model"). |
| `question` | no | The choice as one question ("Where does membership data live?"). |
| `summary` | yes | What is being decided, why it matters now, and what it blocks. 2–4 sentences. |
| `options` | yes | 2–4 choices. The page adds "Something else" and "Decide later" itself; don't include them. |
| `multi` | no | `true` when several options can be chosen together. Default: pick one. |
| `recommended` | no | An option id. Include only when you have a real lean. |
| `recommendation_reason` | with `recommended` | One or two sentences saying why. |
| `tradeoffs` | yes | Matrix rows: `{ "dimension": "…", "values": { "<option id>": "…" } }`. Use 3–6 dimensions and fill a cell for every option. |
| `costs` | yes | Matrix rows: `{ "item": "…", "values": { … } }`. Cover build time, running cost, complexity, and reversibility. |
| `risks` | yes | `{ "risk", "severity": "low"\|"medium"\|"high", "mitigation"?, "options"?: [ids] }`. Omit `options` when a risk applies to every choice. |
| `examples` | yes | Real precedents: `{ "title", "detail"?, "url"? }`. A `url` must be `https://` and must be one you know exists. Don't invent links. |

## Option

| Field | Required | Notes |
|---|---|---|
| `id` | yes | Unique within the decision. Can't be `__defer` or `__other`. |
| `label` | yes | 1–4 words. |
| `summary` | no | One sentence on what choosing it means. |
| `pros` / `cons` | no | Short strings, at most 3 of each. |

## Theme (from DESIGN.md)

Each token is optional, and anything you leave out keeps the template default. Values must be plain CSS colors (`#hex`, `rgb()`, `hsl()`, `oklch()`, …). Nothing else gets through.

```json
"theme": {
  "source": "DESIGN.md",
  "light": { "bg": "#…", "surface": "#…", "sunken": "#…", "fg": "#…", "muted": "#…", "line": "#…",
             "accent": "#…", "accent-ink": "#…", "accent-soft": "#…", "rec": "#…", "rec-soft": "#…",
             "sev-low": "#…", "sev-med": "#…", "sev-high": "#…" },
  "dark":  { "…same tokens…": "#…" },
  "fonts": { "display": "\"Brand Serif\", Georgia, serif", "body": "\"Brand Sans\", system-ui, sans-serif", "mono": "ui-monospace, monospace" },
  "fontsUrl": "https://fonts.googleapis.com/css2?family=…&display=swap",
  "radius": "8px"
}
```

Token roles: `bg` page ground · `surface` cards · `sunken` table heads and wells · `fg` text · `muted` secondary text · `line` borders · `accent` primary action and the selected state · `accent-ink` text on the accent · `accent-soft` the selected-option fill · `rec`/`rec-soft` the "Claude's lean" marker · `sev-*` risk severity.

Fonts must come from Google Fonts (the only font host artifacts allow) or be system stacks. Always end a stack with a generic fallback. When DESIGN.md defines only a light palette, derive a dark one that keeps the same accent hue with enough contrast. Don't leave dark mode on the defaults while light mode is branded.

## Minimal example

```json
{
  "id": "2026-11-07-session-store",
  "title": "Session Store Choice",
  "prompt": "how should we store sessions?",
  "decisions": [{
    "id": "session-store",
    "title": "Session store",
    "summary": "Sessions need to survive restarts before the beta. This blocks the auth work.",
    "options": [
      { "id": "postgres", "label": "Postgres table", "summary": "Reuse the existing database." },
      { "id": "redis", "label": "Redis", "summary": "Add a cache tier with TTLs." }
    ],
    "recommended": "postgres",
    "recommendation_reason": "One fewer service to run, and traffic is low.",
    "tradeoffs": [{ "dimension": "Ops burden", "values": { "postgres": "None new", "redis": "New service" } }],
    "costs": [{ "item": "Build time", "values": { "postgres": "½ day", "redis": "1–2 days" } }],
    "risks": [{ "risk": "Table bloat from expired rows", "severity": "low", "mitigation": "Nightly purge job", "options": ["postgres"] }],
    "examples": [{ "title": "Django database sessions", "detail": "The default session backend stores sessions in a DB table." }]
  }]
}
```
