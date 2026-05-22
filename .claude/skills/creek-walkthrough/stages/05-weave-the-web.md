# Stage 5 — Weave the web

## The pool

Named fragments are still solitary stones until they're connected. Tell
the user, in CrawDad's voice:

> Here is where the vault stops being a pile and becomes a creek. The
> system looks for fragments that *resonate* — that say something kindred,
> even when they share no words. A journal entry from last winter and a
> chat message from this spring might be reaching for the same thing. The
> system finds those reachings and draws the lines.

## How to run it

1. Quietly run `creek link --help` to confirm the methods.
2. Linking has three passes; run them in turn and narrate each:
   - `creek link --vault <vault> --method embeddings` — finds
     **resonances**: fragments that are semantically close.
   - `creek link --method temporal` — finds **threads**: a topic
     carried across time, oldest to newest.
   - `creek link --method eddies` — finds **eddies**: tight clusters
     where many fragments pool around one concern.

## What to interpret

When the passes finish, give them the shape of the web in plain numbers —
how many resonances were drawn, how many threads, how many eddies, the
longest thread, the largest eddy. Then explain the three kindly:

- **Resonance** — a felt kinship between two fragments. Lines of
  similarity, drawn even across years and across very different sources.
- **Thread** — a narrative current: the same theme returning over time,
  with a direction and sometimes an ending.
- **Eddy** — a pool: a dense knot of fragments circling one topic, with no
  particular direction. Discovered, not created.

If the linking surfaced any **synchronicities** — strikingly similar
fragments from very different sources, far apart in time — point at one or
two. These are often the moment the user feels the vault knows something
they didn't consciously hold. Let that be quiet and a little uncanny; don't
oversell it.

## The word for this stage

**Resonance.** Creek's word for connection. Not a logical link, not a
citation — a kinship the embeddings can feel between two pieces of writing.
The connections are the value here, more than the fragments themselves.

## Check in

Tell them the web is woven. Ask whether they'd like to see the system pool
all of this into something readable, or rest.

## If something goes sideways

- **Embeddings need a model download** → the first run fetches a small
  local model and caches it. Narrate the one-time wait calmly; nothing
  leaves their machine.
- **Very few links found** → normal with a small vault. More water means
  more web. Reassure; don't treat it as a defect.
- **It's slow on a large vault** → say so honestly, give a rough sense of
  the wait, and offer to let it run while you both rest.
