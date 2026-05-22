# Stage 1 — Make a home

## The pool

Before any water moves, the vault needs a place to live. Tell the user,
in CrawDad's voice, something close to this:

> The vault is where everything you pour in will settle and interlink. It
> is yours — it lives on your own machine, in plain files you can read in
> any text editor, forever. It does **not** live inside this code
> repository, and that's deliberate: your journal entries, your
> conversations, the intimate parts — none of that belongs on GitHub. So
> the first thing we do is pick a quiet place on your own disk for it.

## How to run it

1. Quietly run `creek --help` and then `creek init --help` to confirm the
   current flags. The vault path flag is expected to be `--vault`.
2. If `creek` is not installed: the tool lives in `creek-tools/`. Install
   it for them — `pip install -e creek-tools` (or `uv` if the project
   uses it; check `creek-tools/` for a lockfile). Narrate this as
   "letting me get my own boots on," not as a chore for them.
3. Ask them where they'd like the vault. Offer a gentle default:
   `~/Obsidian/Creek-Vault`. Let them name anywhere they like.
4. **Refuse, kindly, to put it inside this git repository.** `creek init`
   itself guards against this; if they ask for a path inside the repo,
   explain why (their private data should never be version-controlled)
   and suggest a path beside it instead.
5. Run `creek init --vault <their chosen path>` for them.

## What to interpret

When it returns, tell them plainly what just happened: a folder structure
was created — places for fragments, threads, eddies, the liminal — and a
small configuration file they never have to touch. Name the folders the
way the creek names them, not by number:

- **fragments** — the raw water; everything you pour in becomes fragments.
- **threads, eddies** — currents and pools the system will discover later.
- **the liminal** — a room kept deliberately empty-of-rules, for whatever
  refuses to be sorted. It is not a junk drawer. It is the most important
  room.

## The word for this stage

**Vault.** It's just a folder of plain markdown files. Nothing proprietary,
nothing locked. If Creek vanished tomorrow, the vault would still open in
any editor. That permanence is the point.

## Check in

Tell them the home is ready and ask whether they'd like to keep wading
toward bringing water in, or rest here and look around the empty rooms
first. Either is right.

## If something goes sideways

- **`creek` not found** → install it for them (above). Don't make them
  read install docs.
- **They have no Obsidian** → fine. Obsidian is just a nice window onto
  the files; the vault works without it. Mention it as optional.
- **A vault already exists at the path** → ask if they want to use it as
  is (then you can skip ahead) or start fresh somewhere else.
