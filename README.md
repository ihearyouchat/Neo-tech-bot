# Tech-Help Friend — Telegram Bot (MVP)

A Telegram bot with one persona: **Neo**, a friendly tech-help friend who
walks users through IT problems (password resets, app installs, iCloud/Google
account issues, "what tool should I use for X", etc.) — without ever needing
access to their actual accounts or devices.

This version includes:
- Telegram polling (no public server needed yet)
- SQLite for per-user conversation history (one file, zero setup)
- **A real personality** — Neo isn't a generic assistant voice; he's written as
  "the tech friend everyone's had in their life," with some warmth, dry humor,
  and mild self-deprecation baked into the system prompt
- A **shared solutions knowledge base with a real success rate** — Neo tracks
  exactly which solution was offered to which user, and only credits it as
  "confirmed" when that specific user reports it worked. Ranking is based on
  this genuine track record, not just relevance or raw popularity.
- **One-fix-at-a-time troubleshooting, enforced two ways**: a strong,
  explicit system-prompt rule, PLUS a runtime check that scans every reply
  for signs it's listing multiple options — and automatically rewrites it
  down to one fix before the user ever sees it, if it slips
- **A frustration breaker** — if a user hits several failed fixes in a row on
  the same problem, Neo acknowledges it and offers a short dad joke before
  continuing, rather than just grinding on
- **Voice messages** — users can send a Telegram voice note instead of typing.
  It's transcribed directly via AssemblyAI (no conversion step needed), Neo
  shows the user what he heard (so they can catch a mishearing), then
  answers exactly as he would a typed message
- One main Claude API call per message, plus:
  - an occasional "extraction" call when a brand-new fix gets confirmed
  - a rare "corrective rewrite" call, only triggered if a reply is flagged
    as listing multiple options
  - one AssemblyAI transcription call per voice message received

## 1. Get a Telegram bot token

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot` and follow the prompts (choose a name and a username
   ending in `bot`, e.g. `NeoTechHelpBot`).
3. BotFather will give you a token that looks like
   `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`. Copy it.

## 2. Get an Anthropic API key

1. Go to https://console.anthropic.com
2. Create an API key under "API Keys".

## 3. Set up the project

```bash
cd telegram_tech_bot
python -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# then open .env and paste in your two keys
```

## 4. Run it

```bash
python bot.py
```

You should see `Bot starting (polling mode)...` in the terminal. Now open
Telegram, find your bot by its username, and send `/start`.

## How it works, briefly

- Every message is saved to `conversations.db`, per user, in a `messages`
  table.
- The shared `solutions` table tracks, per stored fix: `times_offered` and
  `times_confirmed` — two separate counters, not one blended number.
- A `pending_offers` table records, per user, which specific solution they
  were just given — so when their next message comes in, Neo knows exactly
  what to credit (or not) rather than guessing.
- On each message:
  1. Neo checks whether the user's message resolves a pending offer
     (confirmed it worked / said it didn't / ambiguous) and updates that
     solution's real counters accordingly.
  2. Neo searches the knowledge base for candidates relevant to the new
     message, ranked by smoothed success rate (see below).
  3. Claude generates a reply, instructed to offer only the single top
     candidate.
  4. The reply is scanned for "multiple options" red-flag phrases. If
     flagged, a second Claude call rewrites it down to one fix before it's
     sent.
  5. If the top candidate was used, it's marked "offered" and tracked as
     this user's pending offer, ready to be resolved on their next message.
  6. If the user's message independently sounds like a confirmation (and
     it *wasn't* already tied to a pending stored offer — e.g. a brand-new
     problem with no matching history, or an ad hoc fix Claude improvised),
     a separate step summarizes it into a new knowledge-base entry.

### On the success rate specifically

- **Smoothed, not raw**: the rate used for ranking is
  `(times_confirmed + 1) / (times_offered + 2)`. This "add-one smoothing"
  means a solution with 1 offer and 1 success starts at a modest ~67%, not
  an overconfident 100% — so a handful of lucky/unlucky early results can't
  dominate the ranking before there's enough real data. As more users try a
  solution, the rate converges toward its true effectiveness.
- **Precisely attributed**: because of the `pending_offers` tracking,
  `times_confirmed` only increments when the *specific user who was given
  that fix* confirms it worked — not just "a confirmation phrase appeared
  somewhere nearby." This is the actual fix for the earlier problem where
  "times reused" only measured how often a solution was looked at.
- **Still heuristic at the edges**: detecting "confirmed" vs. "didn't work"
  vs. "ambiguous" from a user's free-text reply is still phrase-matching
  (see `CONFIRMATION_PHRASES` / `FAILURE_PHRASES` in `bot.py`), so unusual
  phrasing can still be misread occasionally. Worth expanding those phrase
  lists as you see real conversations come in.

### On the frustration-breaker specifically

- This is tracked with an actual counter (`user_troubleshoot_state` table), not
  left to Claude to "notice" on its own. Every time a user says a fix "didn't
  work," their `consecutive_failures` count goes up; every time a fix is
  confirmed, it resets to zero.
- When the count hits `FRUSTRATION_THRESHOLD` (3 by default, in `bot.py`), the
  next reply gets a special instruction injected: acknowledge the frustration,
  tell one short dad joke, then continue. The counter resets right after
  triggering, so it takes another 3 failures before it fires again — it won't
  repeat every single message once the threshold is crossed once.
- Same heuristic dependency as the rest of the failure-detection: it relies on
  `FAILURE_PHRASES` picking up the user's wording. Worth tuning that list
  based on real conversations, same as everywhere else it's used.



### On the one-fix-at-a-time enforcement specifically

- This is genuinely two layers now, not just an instruction: (1) an explicit
  system-prompt rule with a clear example of what's allowed (numbered steps
  for one fix) vs. forbidden (multiple alternative fixes), and (2) an
  automatic runtime check + corrective rewrite if a reply slips anyway.
- **This reduces the risk to a low residual, not to zero** — that's an
  honest limit of working with a generative model rather than fixed logic.
  The red-flag phrase list in `MULTI_OPTION_RED_FLAGS` catches common
  patterns but won't catch every possible phrasing of "here are some
  options" — worth expanding that list too as you observe real usage, and
  in the rare case both the original and the rewrite are flagged, the
  system currently logs a warning and sends the reply anyway rather than
  looping indefinitely (retry loops cost money and risk never responding at
  all — a flagged-but-sent reply is the safer failure mode).
- **Privacy**: the extraction prompt explicitly instructs Claude to store
  only the general technical problem and fix, never names or personal
  details. Worth spot-checking the `solutions` table occasionally to confirm
  this holds up in practice.

### On voice messages specifically

- Telegram voice notes arrive as Ogg/Opus audio. **AssemblyAI accepts this
  format directly** — their own docs confirm no pre-conversion is needed, so
  the downloaded voice file is sent straight to their API as-is. (No
  `ffmpeg` dependency needed for this, unlike the OpenAI version this
  replaced.)
- The flow: download the voice file from Telegram → send to AssemblyAI →
  get transcript back → show the user what was heard → run it through the
  exact same pipeline as a typed message (knowledge-base search, one-fix
  rule, frustration tracking, everything — voice messages aren't a separate
  code path once transcribed).
- **Showing "I heard: ..."** before answering is deliberate, not just a nice
  touch: transcription is never 100% accurate, and this gives the user a
  chance to notice a mishearing and correct it (e.g. by typing instead) —
  especially relevant given the audience.
- **Get an AssemblyAI API key**: sign up at assemblyai.com, verify your
  email — the key is shown immediately on your dashboard, no separate
  "create key" step. Comes with ~$50 in free credit, no card required. Add
  it to `.env` as `ASSEMBLYAI_API_KEY`.
- **This adds a new required setting**: the bot will now refuse to start if
  `ASSEMBLYAI_API_KEY` is missing from `.env`, the same way it already does
  for the Telegram and Anthropic keys.
- **Provider history, for context**: this feature went through two prior
  providers before landing here. Speechmatics looked like the best fit on
  accuracy (see earlier research), but their account portal had an
  unresponsive "Create API key" button that couldn't be resolved through
  normal troubleshooting — looked like a bug on their end. OpenAI worked as
  a fallback, but was ruled out for reasons unrelated to the technology
  itself. AssemblyAI ended up simpler than both: no separate key-creation
  step, and no `ffmpeg` conversion needed either. Swapping providers again
  in the future, if ever needed, is a contained change isolated to
  `transcribe_voice()` and this config block, not a rearchitecture — that
  was true for each of these swaps and remains true going forward.


## What's intentionally NOT in this MVP (by design, see conversation history)

- **No proactive messaging** — the bot only replies, never texts first. This
  also means it never needs Telegram-specific proactive-message workarounds.
- **No per-user "facts" memory** (e.g. "this user has an iPhone") — only
  recent conversation history plus the shared solutions knowledge base. Can
  be added later without restructuring the whole project.
- **No deployment / webhook setup yet** — this runs on your own machine via
  polling. Fine for testing; not for having real users depend on it 24/7.
- **Matching is still keyword-based, not semantic.** It compares overlapping
  words between the new question and stored problem summaries/tags, so
  differently-worded versions of the same problem (e.g. "wifi keeps
  dropping" vs. "internet disconnects randomly") won't always match each
  other yet. Upgrade path: embeddings-based similarity search (e.g. Voyage
  AI), storing vectors instead of raw text, ranked by cosine similarity —
  worth doing once you have a few hundred+ stored solutions and start
  noticing missed matches.

## Next steps (in rough order of priority)

1. **Test the persona.** Chat with it, including deliberately saying a fix
   "didn't work" a couple of times, to confirm the ranking and one-at-a-time
   behavior hold up across a multi-turn troubleshooting flow.
2. **Deploy somewhere it runs 24/7.** Options: a small VPS (DigitalOcean,
   Hetzner), or a platform like Railway/Render/Fly.io. You'd run the same
   `bot.py` there — polling mode still works fine on a server, or you can
   switch to a webhook if you want faster response times at scale.
3. **Swap SQLite for Postgres** once you have real concurrent users (SQLite
   is fine for one process but doesn't love high concurrency).
4. **Seed the knowledge base** with fixes you already know (like the Vinted
   hidden-email case) by inserting rows into `solutions` directly, so Neo
   doesn't have to learn them from scratch through live users.
5. **Add lightweight per-user memory** if you want Neo to recall things like
   "what phone/OS this user has" across sessions — separate from the shared
   solutions knowledge base, which stays global across all users.

## Cost note

Each user message triggers one Claude API call, occasionally two (if a new
fix needs to be extracted) or three (if a reply needs correcting). Keep an
eye on your Anthropic usage dashboard once you have real users — cost scales
with number of messages × tokens per message (system prompt + history +
response).
