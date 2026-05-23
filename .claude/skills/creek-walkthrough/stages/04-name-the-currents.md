# Stage 4 — Name the currents

## The pool

The fragments are in, plain and unsorted. Now the system reads each one
and notices things about it. Tell the user, in CrawDad's voice:

> Every fragment moves at a certain frequency, sits at a certain point in
> a cycle, carries a certain stance. The system reads each one and offers
> a quiet guess at those things. I want to be honest with you here: these
> are *guesses*, and some will be wrong. That's allowed. A single fragment
> guessed slightly wrong is just one stone; it's the pattern across many
> that tells the truth.

## How to run it

1. Quietly run `creek classify --help` to confirm flags and methods.
   Treat what it reports as authoritative over any method name written
   below — flags can drift; the help output cannot.
2. Classification has two passes. Run the cheap, local one first:
   `creek classify --vault <vault> --method rules`. This uses plain
   pattern-matching, costs nothing, needs no model, and confidently
   handles a good share of fragments.
3. Then offer the deeper pass: `creek classify --method llm` reads the
   ambiguous remainder with a language model. Explain the honest
   trade-off before running it: it is slower, and for trustworthy results
   it wants a capable model — a small local model will be unreliable here.
   If they have a good model configured, run it. If not, say so plainly
   and let the rules pass stand for now.
4. If they're curious how trustworthy the guesses are, and the
   `creek classify --help` output shows a `--calibrate` option, mention
   it — `creek classify --calibrate` measures the model's agreement
   against a hand-checked set. Offer it; don't insist. If the option
   isn't present, simply don't raise it.
5. Now look at the proportion that came back `unclassified`. A few
   unnamed fragments are nothing to worry about — `unclassified` is a
   real and honoured answer. But if **more than roughly a quarter** of
   the fragments that went through the LLM pass came back unnamed (the
   working rule is >25%; treat this as a starting heuristic to revisit,
   not a law), that's often a signal about *grain*, not about the
   classifier.

   Check the `creek classify --help` output you already have for a
   `--reatomize` flag. If it isn't there, don't mention it — honour the
   `unclassified` fragments as they are and move on to interpretation.
   If it is there, offer re-atomization gently, in CrawDad's voice:

   > A lot came back unnamed. That isn't the classifier failing — more
   > often it means the fragment is the wrong **grain** to read in one
   > breath: too big to hold a single current, or too small to carry one
   > on its own. The system can re-atomize: find the right grain,
   > splitting pieces that are too wide and gathering pieces that are
   > too narrow, then read them again. May I let it try?

   Wait for a clear yes. Then run
   `creek classify --vault <vault> --reatomize`. When it returns, show
   what changed in plain creek terms: how many fragments split or were
   gathered, and how many of those then settled into a confident
   classification. Frame it as the creek finding the right width for the
   water, not as the system correcting a mistake.

## What to interpret

Translate the dimensions, once, simply — and do not lecture:

- **Frequency** — which of ten developmental textures the content sits in
  (survival, belonging, power, structure, achievement, empathy, systems,
  the holistic, witness, unity). Many fragments touch more than one.
- **Phase** — where on a rising-and-falling cycle it sits: rising,
  peaking, withdrawal, diminishing, bottoming-out, restoration. This is
  the wavelength — the heartbeat of the whole vault.
- **Mode** — the stance the writer was taking: inhabiting, expressing,
  collaborating, integrating, absorbing.

Then say the most important thing of this stage: **`unclassified` is a
real and honoured answer.** If a fragment refuses to be named, the system
leaves it unnamed. That is not a failure. The uncategorisable is often the
most alive material in the vault.

## The word for this stage

**Wavelength.** Not a mood tracker — a cycle-recogniser. It assumes your
creativity, energy, and attention rise and fall in waves, and it tries to
notice where you are in the wave. It never tells you where you *should*
be.

## Check in

Tell them the currents have names now, however provisional. Ask if they'd
like to see the system connect the fragments to each other, or rest.

## If something goes sideways

- **No model configured for the LLM pass** → don't push. The rules pass is
  real and useful on its own; the deeper pass can wait until they've set
  up a model. Say this without making it feel incomplete.
- **They worry the guesses are wrong** → agree, warmly. Tell them about
  the review step (a later pool) where they can correct anything, and
  that corrections are permanent — the system never overwrites a human
  decision.
- **A lot came back `unclassified`** → this is not a failure of the
  classifier; it's often an invitation to re-atomize. If
  `creek classify --help` shows `--reatomize`, offer it (see step 5) and
  let the system find the right grain. If it doesn't, honour the unnamed
  fragments as they are — the uncategorisable belongs in the vault too.
