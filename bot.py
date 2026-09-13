"""
Neo — Tech-Help Friend, Telegram bot powered by Claude.

Architecture:
- Telegram polling (no public server needed for local dev/testing)
- SQLite for:
    - per-user conversation history
    - a SHARED "solutions" knowledge base, built from real conversations,
      and reused across all users, ranked by a genuine success rate
    - a "pending offers" tracker, so Neo knows exactly which solution a user
      is currently trying, and can correctly credit (or not) its success rate
- Claude calls involved:
    1. The main reply (always)
    2. A lightweight "extraction" call (only when a NEW, not-yet-known fix
       gets confirmed), which turns that exchange into a reusable,
       anonymized knowledge-base entry
    3. An occasional "corrective rewrite" call — a safety net that catches
       and fixes any reply that accidentally lists multiple solutions at
       once, before the user ever sees it

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your keys
    python bot.py
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

DB_PATH = "conversations.db"
MAX_HISTORY_MESSAGES = 20       # per-user chat history sent as context
MAX_CANDIDATE_SOLUTIONS = 5     # how many ranked past solutions to offer as candidates
FRUSTRATION_THRESHOLD = 3       # consecutive failed fixes before offering a breather

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Rough heuristics for detecting how a user responded to a fix they were given
CONFIRMATION_PHRASES = [
    "thanks", "thank you", "worked", "works now", "fixed", "solved",
    "perfect", "awesome", "that did it", "all good now", "problem solved",
    "got it working", "it's working", "its working", "resolved",
]

FAILURE_PHRASES = [
    "didn't work", "did not work", "doesn't work", "does not work",
    "no luck", "still not working", "still broken", "not fixed",
    "still having the issue", "still happening", "nope", "no change",
    "still the same", "still broken", "that didn't fix it", "still an issue",
]

# Phrases that signal a reply is listing multiple alternatives instead of one
MULTI_OPTION_RED_FLAGS = [
    "option 1", "option 2", "here are a few", "a few things you can try",
    "few options", "couple of options", "some things to try",
    "you could try any", "alternatively,", "another option",
    "another possibility", "other possibilities", "possible causes",
    "possible fixes", "few possible", "try one of the following",
    "a couple of things", "several things you can try", "here are some options",
    "there are a few reasons", "a number of reasons", "could be a few things",
]

# Basic stopwords so keyword search isn't dominated by "the", "is", "my", etc.
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "my", "i", "to", "it",
    "on", "in", "of", "and", "or", "for", "with", "this", "that", "how",
    "do", "can", "you", "me", "please", "help", "not", "when", "why",
}

# ---------------------------------------------------------------------------
# Persona
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are Neo — a tech-help friend that people chat with on Telegram. You're not \
a corporate support-desk voice; you're the guy who's always been "the tech \
person" in every friend group and family you've ever been part of — the one \
who fixed his parents' printer, then his aunt's laptop, then somehow became \
the unofficial IT department for everyone he knows. You genuinely like this \
stuff. A stubborn bug is satisfying to crack, not a chore.

Personality:
- Warm, casual, patient — never makes anyone feel dumb for asking something \
  "obvious." You've heard every version of every question before.
- A little dry, understated humor sometimes — not a comedian, just occasionally \
  lets a light, easy line through. Never forced.
- Mildly self-deprecating about being "that friend everyone calls when \
  something breaks" — it's part of your charm, not a complaint.
- Quietly satisfied when a fix works — a small "nice, knew that'd do it" \
  energy, not over the top.
- Talk like a real person texting, not a manual. Plain language first; \
  explain jargon only if unavoidable, and briefly.

HARD RULE — read carefully, this is not a stylistic preference:
- Give exactly ONE fix per message. Never present multiple possible causes or \
  a menu of solutions to choose from. Real tech support doesn't hand someone \
  ten bullet points and hope one sticks — it tries the single most likely fix, \
  watches whether it worked, and only then moves to the next one. You work the \
  same way, one attempt at a time, like a human would (a human can't try five \
  fixes simultaneously either).
- It IS fine to give numbered, sequential STEPS for how to carry out that one \
  fix (e.g. "1. Open Settings 2. Tap Accounts 3. Tap Remove"). That is not the \
  same thing as listing multiple alternative fixes, and is encouraged for \
  clarity. What's forbidden is presenting more than one distinct possible \
  cause/fix as options.
- If "candidate solutions" are listed below, they're already ordered from most \
  likely to least likely (based on real track record with other users). Offer \
  ONLY the top one.
- After giving a fix, ask the user to try it and tell you if it worked.
- If they come back and say it didn't work, move to the NEXT candidate — never \
  repeat one already given in this conversation. Use the conversation history \
  to track what's already been tried.
- If you run out of candidates (or none were given), fall back to your own \
  best judgment about the single most likely cause — still only one at a time.

- Ask a clarifying question if you genuinely need one detail to help (device \
  type, OS, app name) — but don't interrogate; assume the most common case if \
  it's a reasonable guess.

Other boundaries:
- You do NOT have access to the user's actual devices, accounts, or passwords. \
  You talk them through steps, like a help-desk agent would over the phone — \
  you never claim to perform an action yourself (e.g. never say "I've reset \
  your password").
- Menus and steps in apps/OSes change over time. If you're not confident an \
  instruction is current, say so plainly rather than stating it with false \
  certainty.
- If someone asks for something outside tech help (venting about their day, \
  something emotional, unrelated topics), respond kindly and briefly, but \
  gently note that tech stuff is where you're most useful, without being cold \
  about it.
{solutions_block}{frustration_block}
"""

FRUSTRATION_BREAK_TEMPLATE = """
The user has just tried {n} suggested fixes in a row on this problem without \
success. Before offering the next fix, take a short beat: acknowledge, warmly \
and genuinely (not in a corporate-apology way), that this has been frustrating. \
Then tell ONE short, original, wholesome "dad joke" — the corny, groan-worthy, \
pun-based kind — as a quick breather. Keep it brief: one joke, not a routine. \
Then gently continue: offer the next fix, or ask if they'd rather take a short \
break first.
"""

SOLUTIONS_BLOCK_TEMPLATE = """
Candidate solutions from past conversations with OTHER users who had a similar \
problem, ordered from most likely to least likely to be the right fix (based \
on real confirmed success rate, not just relevance). Offer ONLY the first one \
below. Only move to the next if the user says the first one didn't work.

{entries}
"""

SOLUTION_ENTRY_TEMPLATE = (
    "- Problem: {problem_summary}\n"
    "  Fix: {solution_summary}\n"
    "  Track record: confirmed effective {times_confirmed} out of {times_offered} "
    "time(s) it's been offered before"
)

EXTRACTION_SYSTEM_PROMPT = """\
You analyze a short tech-support conversation snippet between a helper and a \
user. Decide whether it contains a SPECIFIC technical problem that was given \
a CONCRETE fix, which the user then confirmed worked.

If yes, respond with ONLY a JSON object, no other text, in this exact shape:
{"problem_summary": "...", "solution_summary": "...", "tags": "comma,separated,keywords"}

Rules:
- problem_summary and solution_summary must describe the GENERAL technical \
  issue and fix only — never include the user's name, account details, or \
  any other personal information.
- Keep both summaries concise (1-2 sentences each).
- tags should be 3-6 short lowercase keywords useful for future search \
  matching (e.g. "wifi,router,dns,connection").
- If the snippet does NOT contain a clearly confirmed fix to a specific \
  technical problem, respond with exactly: null
"""

CORRECTIVE_REWRITE_SYSTEM_PROMPT = """\
You are reviewing a draft reply from a tech-support persona named Neo. The \
draft below breaks a hard rule: it lists multiple possible causes or \
solutions instead of presenting exactly ONE fix to try right now.

Rewrite it so it presents only the single most likely fix (pick the best one \
if several were listed), phrased warmly and directly. Numbered steps are \
fine ONLY if they are sequential steps of that one fix, not separate \
alternative solutions. Respond with ONLY the rewritten reply text, nothing \
else — no preamble, no explanation of what you changed.
"""

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_summary TEXT NOT NULL,
            solution_summary TEXT NOT NULL,
            tags TEXT NOT NULL,
            times_offered INTEGER NOT NULL DEFAULT 0,
            times_confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_offers (
            user_id TEXT PRIMARY KEY,
            solution_id INTEGER NOT NULL,
            offered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_troubleshoot_state (
            user_id TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(user_id: str, role: str, content: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, role, content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_recent_history(user_id: str, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT role, content FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]


def get_recent_plaintext(user_id: str, limit: int = 6) -> str:
    """A plain-text transcript of the last few turns, for the extraction step."""
    history = get_recent_history(user_id, limit=limit)
    lines = []
    for msg in history:
        speaker = "User" if msg["role"] == "user" else "Neo"
        lines.append(f"{speaker}: {msg['content']}")
    return "\n".join(lines)


def save_solution(problem_summary: str, solution_summary: str, tags: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO solutions
            (problem_summary, solution_summary, tags, times_offered, times_confirmed, created_at)
        VALUES (?, ?, ?, 0, 0, ?)
        """,
        (problem_summary, solution_summary, tags, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info("Stored new solution: %s", problem_summary)


def increment_offered(solution_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE solutions SET times_offered = times_offered + 1 WHERE id = ?",
        (solution_id,),
    )
    conn.commit()
    conn.close()


def increment_confirmed(solution_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE solutions SET times_confirmed = times_confirmed + 1 WHERE id = ?",
        (solution_id,),
    )
    conn.commit()
    conn.close()


def set_pending_offer(user_id: str, solution_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO pending_offers (user_id, solution_id, offered_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            solution_id = excluded.solution_id,
            offered_at = excluded.offered_at
        """,
        (user_id, solution_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_pending_offer(user_id: str) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT solution_id FROM pending_offers WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def clear_pending_offer(user_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM pending_offers WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_consecutive_failures(user_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT consecutive_failures FROM user_troubleshoot_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def set_consecutive_failures(user_id: str, count: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO user_troubleshoot_state (user_id, consecutive_failures)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET consecutive_failures = excluded.consecutive_failures
        """,
        (user_id, count),
    )
    conn.commit()
    conn.close()


def resolve_pending_offer(user_id: str, message_text: str) -> tuple[str, bool]:
    """
    Checks the user's latest message against any solution they were just
    offered, and updates its real success rate accordingly. Also tracks
    consecutive failed attempts, to trigger a frustration-breaker.

    Returns (status, trigger_frustration_break) where status is one of:
      "confirmed"  — a pending offer existed and the user confirmed it worked
      "failed"     — a pending offer existed and the user said it didn't work
      "unresolved" — a pending offer existed but the message was ambiguous
      "none"       — there was no pending offer for this user
    """
    solution_id = get_pending_offer(user_id)
    if solution_id is None:
        return "none", False

    lowered = message_text.lower()

    if any(phrase in lowered for phrase in CONFIRMATION_PHRASES):
        increment_confirmed(solution_id)
        clear_pending_offer(user_id)
        set_consecutive_failures(user_id, 0)
        return "confirmed", False

    if any(phrase in lowered for phrase in FAILURE_PHRASES):
        clear_pending_offer(user_id)
        new_count = get_consecutive_failures(user_id) + 1
        if new_count >= FRUSTRATION_THRESHOLD:
            set_consecutive_failures(user_id, 0)  # reset so the next batch counts fresh
            return "failed", True
        set_consecutive_failures(user_id, new_count)
        return "failed", False

    return "unresolved", False


def _tokenize(text: str) -> set[str]:
    words = "".join(c.lower() if c.isalnum() else " " for c in text).split()
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def _smoothed_success_rate(times_offered: int, times_confirmed: int) -> float:
    # Add-one (Laplace) smoothing: a brand-new, untested solution starts at a
    # neutral 0.5 rather than 0.0, and the estimate firms up as more data
    # comes in, rather than letting one lucky/unlucky early result dominate.
    return (times_confirmed + 1) / (times_offered + 2)


def search_solutions(query_text: str, limit: int = MAX_CANDIDATE_SOLUTIONS) -> list[dict]:
    """
    Keyword-overlap search over the shared solutions table, ranked by REAL
    confirmed success rate (not just relevance or raw popularity) — this is
    the "Neo already knows the fix that actually works most often" ranking.

    Matching itself is still naive keyword overlap; the upgrade path is
    embeddings-based semantic search (see README) once the knowledge base
    grows and phrasing varies more than exact word overlap can catch.
    """
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return []

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, problem_summary, solution_summary, tags, times_offered, times_confirmed "
        "FROM solutions"
    ).fetchall()
    conn.close()

    scored = []
    for row_id, problem_summary, solution_summary, tags, times_offered, times_confirmed in rows:
        entry_tokens = _tokenize(problem_summary) | _tokenize(tags)
        overlap = len(query_tokens & entry_tokens)
        if overlap > 0:
            rate = _smoothed_success_rate(times_offered, times_confirmed)
            scored.append(
                (rate, overlap, row_id, problem_summary, solution_summary,
                 times_offered, times_confirmed)
            )

    # Primary sort: real success rate. Secondary: keyword relevance.
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = scored[:limit]

    return [
        {
            "id": row_id,
            "problem_summary": p,
            "solution_summary": s,
            "times_offered": to,
            "times_confirmed": tc,
        }
        for _, _, row_id, p, s, to, tc in top
    ]


# ---------------------------------------------------------------------------
# Claude calls
# ---------------------------------------------------------------------------


def build_system_prompt(relevant_solutions: list[dict], trigger_frustration_break: bool = False) -> str:
    if not relevant_solutions:
        solutions_block = ""
    else:
        entries = "\n".join(
            SOLUTION_ENTRY_TEMPLATE.format(
                problem_summary=s["problem_summary"],
                solution_summary=s["solution_summary"],
                times_confirmed=s["times_confirmed"],
                times_offered=s["times_offered"],
            )
            for s in relevant_solutions
        )
        solutions_block = SOLUTIONS_BLOCK_TEMPLATE.format(entries=entries)

    frustration_block = (
        FRUSTRATION_BREAK_TEMPLATE.format(n=FRUSTRATION_THRESHOLD)
        if trigger_frustration_break
        else ""
    )

    return SYSTEM_PROMPT_TEMPLATE.format(
        solutions_block=solutions_block, frustration_block=frustration_block
    )


def looks_like_multiple_options(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in MULTI_OPTION_RED_FLAGS)


def enforce_single_solution(draft_reply: str) -> str:
    """Safety net: rewrites a reply that slipped into listing multiple
    options, so the user never sees a rule-breaking response."""
    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=CORRECTIVE_REWRITE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": draft_reply}],
        )
        rewritten = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return rewritten or draft_reply
    except Exception:
        logger.exception("Corrective rewrite call failed; sending original draft")
        return draft_reply


def get_claude_reply(user_id: str, user_message: str) -> tuple[str, str]:
    """Returns (reply_text, offer_resolution)."""
    offer_resolution, trigger_frustration_break = resolve_pending_offer(user_id, user_message)

    save_message(user_id, "user", user_message)
    history = get_recent_history(user_id)
    relevant_solutions = search_solutions(user_message)
    system_prompt = build_system_prompt(relevant_solutions, trigger_frustration_break)

    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            system=system_prompt,
            messages=history,
        )
        reply_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not reply_text:
            reply_text = "Hmm, I didn't get a proper answer that time — mind trying again?"
    except Exception:
        logger.exception("Claude API call failed")
        reply_text = (
            "Sorry — I'm having trouble thinking right now. "
            "Give it a moment and try again?"
        )
        save_message(user_id, "assistant", reply_text)
        return reply_text, offer_resolution

    # Safety net: catch and correct any reply that slipped into listing
    # multiple options, before it ever reaches the user.
    if looks_like_multiple_options(reply_text):
        logger.warning("Detected multi-option reply for user %s; rewriting", user_id)
        reply_text = enforce_single_solution(reply_text)
        if looks_like_multiple_options(reply_text):
            logger.warning(
                "Reply still flagged as multi-option after one corrective "
                "rewrite for user %s; sending as-is to avoid retry loops",
                user_id,
            )

    save_message(user_id, "assistant", reply_text)

    # The persona is instructed to only ever offer the top-ranked candidate,
    # so that's the one we track as "offered" for success-rate purposes.
    if relevant_solutions:
        top = relevant_solutions[0]
        increment_offered(top["id"])
        set_pending_offer(user_id, top["id"])

    return reply_text, offer_resolution


def maybe_extract_solution(user_id: str, latest_user_message: str) -> None:
    """If the latest message sounds like confirmation a fix worked, ask Claude
    to summarize the resolved problem into a reusable knowledge-base entry.

    Only called when the confirmation is NOT already tied to an existing
    stored solution (see handle_message) — this avoids storing near-duplicate
    entries every time a known fix gets reused and reconfirmed.
    """
    lowered = latest_user_message.lower()
    if not any(phrase in lowered for phrase in CONFIRMATION_PHRASES):
        return

    transcript = get_recent_plaintext(user_id, limit=6)

    try:
        response = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=300,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
        raw_text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()

        if raw_text == "null" or not raw_text:
            return

        data = json.loads(raw_text)
        problem_summary = data.get("problem_summary", "").strip()
        solution_summary = data.get("solution_summary", "").strip()
        tags = data.get("tags", "").strip()

        if problem_summary and solution_summary:
            save_solution(problem_summary, solution_summary, tags)

    except json.JSONDecodeError:
        logger.warning("Extraction call returned non-JSON, skipping: %s", raw_text)
    except Exception:
        logger.exception("Extraction call failed")


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey, I'm Neo 👋 Your go-to for tech headaches — locked accounts, "
        "confusing settings, app problems, \"what tool should I use for X\", "
        "you name it.\n\nWhat's going on?"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    reply_text, offer_resolution = get_claude_reply(user_id, user_text)
    await update.message.reply_text(reply_text)

    # Only look for a brand-new fix to store if this confirmation wasn't
    # already tied to (and credited to) an existing stored solution.
    if offer_resolution != "confirmed":
        maybe_extract_solution(user_id, user_text)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting (polling mode)...")
    app.run_polling()


if __name__ == "__main__":
    main()
