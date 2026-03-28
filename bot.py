import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
import asyncio
import os
import base64
import json
import datetime
import time

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN")
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY")
PORT          = int(os.environ.get("PORT", 8080))

GROQ_API      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "llama-3.3-70b-versatile"

# Data storage repo
DATA_OWNER  = "Shebyyy"
DATA_REPO   = "anymex-support-db"
DATA_BRANCH = "main"
GITHUB_API  = "https://api.github.com"

# File paths in data repo
FILE_CONFIG        = "config.json"
FILE_TODOS         = "todos.json"
FILE_TODOS_ARCHIVE = "todos_archive.json"
FILE_BOARD_IDS     = "board_ids.json"   # dedicated store for Discord message IDs

TODOS_PER_PAGE = 10

# Default config
DEFAULT_CONFIG = {
    "todo_channel":    None,
    "todo_roles":      [],
    "todo_style":      1,
    "prefix":          "ax!",
    # Activity log
    "log_channel":     None,
    # Reminders
    "reminder_days":   3,
    "reminder_time":   "09:00",
    "reminder_channel": None,   # None = DM assigned user
    # Threads (styles 1-4)
    "thread_channel":  None,
}

# board_ids.json schema — separate file so config.json stays clean
# {
#   "stats_message_id": "discord_msg_id" | null,
#   "style": 1,          <- style used when board was last posted
#   "pages": [           <- one entry per Discord message on the board
#     {
#       "message_id": "discord_msg_id",
#       "thread_id":  "discord_thread_id" | null,  <- for styles 5/6
#       "todo_ids":   [1, 3, 7]   <- which todo IDs live in this message
#     }                              styles 1-4: multiple per message (one page)
#   ]                                styles 5-6: exactly one todo per message
# }
DEFAULT_BOARD_IDS = {
    "stats_message_id": None,
    "style":            None,
    "pages":            [],
}

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache:    dict = {}
_cache_ts: dict = {}
CACHE_TTL = 300  # 5 min

# ══════════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

async def gh_read(session: aiohttp.ClientSession, filepath: str):
    now = time.time()
    if filepath in _cache and now - _cache_ts.get(filepath, 0) < CACHE_TTL:
        return _cache[filepath], None
    url = f"{GITHUB_API}/repos/{DATA_OWNER}/{DATA_REPO}/contents/{filepath}?ref={DATA_BRANCH}"
    async with session.get(url, headers=gh_headers()) as r:
        if r.status == 404:
            return None, None
        data = await r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        parsed = json.loads(content)
        _cache[filepath] = parsed
        _cache_ts[filepath] = now
        return parsed, data["sha"]

async def gh_write(session: aiohttp.ClientSession, filepath: str, data, sha, msg: str):
    _cache.pop(filepath, None)
    payload = {
        "message": msg,
        "content": base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode()).decode(),
        "branch": DATA_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"{GITHUB_API}/repos/{DATA_OWNER}/{DATA_REPO}/contents/{filepath}"
    async with session.put(url, headers=gh_headers(), json=payload) as r:
        return r.status in (200, 201)

async def gh_read_fresh(session: aiohttp.ClientSession, filepath: str):
    _cache.pop(filepath, None)
    return await gh_read(session, filepath)

async def ensure_files():
    async with aiohttp.ClientSession() as session:
        for filepath, default in [
            (FILE_CONFIG,        DEFAULT_CONFIG),
            (FILE_TODOS,         []),
            (FILE_TODOS_ARCHIVE, []),
            (FILE_BOARD_IDS,     DEFAULT_BOARD_IDS),
        ]:
            data, sha = await gh_read(session, filepath)
            if sha is None and data is None:
                await gh_write(session, filepath, default, None, f"init: {filepath}")
                print(f"Created {filepath}")
            else:
                print(f"{filepath} exists")

# ══════════════════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _similarity(a: str, b: str) -> float:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    longer = max(len(a), len(b))
    matches = sum(ca == cb for ca, cb in zip(a, b))
    common  = sum(min(a.count(c), b.count(c)) for c in set(a))
    return (matches + common) / (longer + len(a))

def check_duplicate(todos: list, title: str, source_msg_id: str):
    """
    Returns (kind, existing_todo):
      'message_id' — same source message already a todo
      'exact'      — identical title exists
      'fuzzy'      — similar title (>=80%) exists
      None         — no duplicate
    """
    title_clean = title.lower().strip()
    for t in todos:
        if t.get("status") == "done":
            continue
        if t.get("source_message_id") and t["source_message_id"] == source_msg_id:
            return "message_id", t
        if t["title"].lower().strip() == title_clean:
            return "exact", t
        if _similarity(t["title"], title) >= 0.80:
            return "fuzzy", t
    return None, None

# ══════════════════════════════════════════════════════════════════════════════
# GROQ AI HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def ai_generate_title(message_text: str) -> tuple[str, str]:
    """
    Uses Groq to generate a short TODO title and a clean description.
    Returns (title, description). Falls back to truncated text if AI fails.
    """
    if not GROQ_API_KEY or not message_text.strip():
        fallback = message_text.strip()[:100]
        return fallback, message_text.strip()

    prompt = f"""Generate a short, clear TODO title AND a clean one-sentence description from the message below.

Rules for title:
- Max 10 words
- No punctuation at the end
- Keep it actionable (start with a verb if possible)
- No filler words

Rules for description:
- One sentence, max 150 chars
- Summarise the core issue or task clearly
- Keep the user's intent

Respond ONLY in this exact JSON format with no extra text:
{{"title": "...", "description": "..."}}

Message:
{message_text[:800]}"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.3,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_API, headers=headers, json=payload) as r:
                if r.status != 200:
                    raise Exception(f"Groq status {r.status}")
                data = await r.json()
                raw = data["choices"][0]["message"]["content"].strip()
                # Strip markdown code fences if present
                raw = raw.strip("`").strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
                import json as _json
                parsed = _json.loads(raw)
                title = parsed.get("title", "").strip()[:120]
                desc  = parsed.get("description", "").strip()[:200]
                if not title:
                    raise Exception("Empty title")
                return title, desc
    except Exception as ex:
        print(f"Groq AI error: {ex}")
        fallback = message_text.strip()[:100]
        return fallback, message_text.strip()[:200]

# ══════════════════════════════════════════════════════════════════════════════
# CARD STYLES & EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

STATUS_COLORS = {
    "todo":          0x378ADD,
    "in_progress":   0xBA7517,
    "review_needed": 0x888780,
    "blocked":       0xE24B4A,
    "done":          0x1D9E75,
}
STATUS_LABELS = {
    "todo":          "To Do",
    "in_progress":   "In Progress",
    "review_needed": "Review Needed",
    "blocked":       "Blocked",
    "done":          "Done",
}
PRIORITY_LABELS  = {"low": "Low", "medium": "Medium", "high": "High"}
PRIORITY_ICONS   = {"low": "▽", "medium": "◈", "high": "▲"}
STATUS_ICONS     = {
    "todo":          "○",
    "in_progress":   "◑",
    "review_needed": "◇",
    "blocked":       "✕",
    "done":          "✓",
}

# Progress bar helper (Discord block chars)
def _progress_bar(value: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = round((value / total) * length)
    return "█" * filled + "░" * (length - filled)

# ── Style 1 — Clean (top accent via description rule line) ───────────────────
def _card_style1(t: dict) -> tuple[str, str]:
    status   = t.get("status", "todo")
    label    = STATUS_LABELS.get(status, status)
    priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
    pri_icon = PRIORITY_ICONS.get(t.get("priority", "medium"), "◈")
    asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
    added    = f"<@{t['added_by_id']}>"
    ai_tag   = "  ✦ AI" if t.get("auto_generated") else ""
    desc     = t.get("ai_description") or ""
    desc_line = f"\n> *{desc[:120]}*" if desc else ""
    tags_line = f"\n{_tag_badges(t.get('tags', []))}" if t.get("tags") else ""
    name  = f"#{t['id']} — {t['title'][:65]}"
    value = (
        f"`{label}`  {pri_icon} {priority}{ai_tag}{desc_line}{tags_line}\n"
        f"-# Assigned: {asgn}  ·  Added by: {added}"
    )
    return name, value

def _stats_style1(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="○ To Do",         value=str(counts["todo"]),          inline=True)
    e.add_field(name="◑ In Progress",   value=str(counts["in_progress"]),   inline=True)
    e.add_field(name="◇ Review Needed", value=str(counts["review_needed"]), inline=True)
    e.add_field(name="✕ Blocked",       value=str(counts["blocked"]),       inline=True)
    e.add_field(name="✓ Done",          value=str(archive_count),           inline=True)
    e.add_field(name="Active",          value=str(active_count),            inline=True)
    e.set_footer(text="Style 1 — Clean  ·  Last updated")
    return e

# ── Style 2 — Sidebar (left bar via bold separator trick) ────────────────────
def _card_style2(t: dict) -> tuple[str, str]:
    status   = t.get("status", "todo")
    label    = STATUS_LABELS.get(status, status)
    color_bar = {
        "todo": "🔵", "in_progress": "🟠",
        "review_needed": "⚪", "blocked": "🔴", "done": "🟢",
    }.get(status, "⚪")
    priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
    asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
    added    = f"<@{t['added_by_id']}>"
    ai_tag   = "  ✦ AI" if t.get("auto_generated") else ""
    desc     = t.get("ai_description") or ""
    desc_line = f"\n> *{desc[:120]}*" if desc else ""
    tags_line = f"\n{_tag_badges(t.get('tags', []))}" if t.get("tags") else ""
    name  = f"{color_bar} #{t['id']} — {t['title'][:60]}"
    value = (
        f"**{label}**  ·  {priority}{ai_tag}{desc_line}{tags_line}\n"
        f"-# {asgn}  ·  {added}"
    )
    return name, value

def _stats_style2(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    total = active_count or 1
    bar_todo    = _progress_bar(counts["todo"],          total, 8)
    bar_inprog  = _progress_bar(counts["in_progress"],   total, 8)
    bar_review  = _progress_bar(counts["review_needed"], total, 8)
    bar_blocked = _progress_bar(counts["blocked"],       total, 8)
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.description = (
        f"🔵 **To Do**       `{bar_todo}` {counts['todo']}\n"
        f"🟠 **In Progress** `{bar_inprog}` {counts['in_progress']}\n"
        f"⚪ **Review**      `{bar_review}` {counts['review_needed']}\n"
        f"🔴 **Blocked**     `{bar_blocked}` {counts['blocked']}\n"
        f"\n✓ Done: **{archive_count}**  ·  Active: **{active_count}**"
    )
    e.set_footer(text="Style 2 — Sidebar  ·  Last updated")
    return e

# ── Style 3 — Minimal (clean, no icons, just text) ───────────────────────────
def _card_style3(t: dict) -> tuple[str, str]:
    status   = t.get("status", "todo")
    label    = STATUS_LABELS.get(status, status)
    priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
    asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "—"
    added    = f"<@{t['added_by_id']}>"
    name  = f"#{t['id']}  {t['title'][:70]}"
    value = f"`{label}`  ·  {priority}  ·  {asgn}  ·  {added}"
    return name, value

def _stats_style3(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.description = (
        f"`To Do` {counts['todo']}   "
        f"`In Progress` {counts['in_progress']}   "
        f"`Review` {counts['review_needed']}   "
        f"`Blocked` {counts['blocked']}   "
        f"`Done` {archive_count}"
    )
    e.set_footer(text="Style 3 — Minimal  ·  Last updated")
    return e

# ── Style 4 — Detailed (AI summary + quote block) ────────────────────────────
def _card_style4(t: dict) -> tuple[str, str]:
    status   = t.get("status", "todo")
    label    = STATUS_LABELS.get(status, status)
    st_icon  = STATUS_ICONS.get(status, "○")
    priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
    pri_icon = PRIORITY_ICONS.get(t.get("priority", "medium"), "◈")
    asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
    added    = f"<@{t['added_by_id']}>"
    ai_tag   = "  ✦ AI" if t.get("auto_generated") else ""

    lines = [f"`{st_icon} {label}`  {pri_icon} {priority}{ai_tag}"]

    ai_desc = t.get("ai_description", "")
    if ai_desc:
        lines.append(f"> **Summary:** *{ai_desc[:150]}*")

    src_text = t.get("source_message_text", "")
    if src_text and t.get("auto_generated"):
        snippet = src_text.strip()[:100].replace("\n", " ")
        lines.append(f"> \"{snippet}\"")

    tags = t.get("tags", [])
    if tags:
        lines.append(_tag_badges(tags))

    lines.append(f"-# Assigned: {asgn}  ·  Added by: {added}")
    name  = f"#{t['id']} — {t['title'][:60]}"
    value = "\n".join(lines)
    return name, value

def _stats_style4(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    total     = active_count or 1
    done_pct  = round((archive_count / max(archive_count + active_count, 1)) * 100)
    full_bar  = _progress_bar(archive_count, archive_count + active_count, 12)
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="○ To Do",         value=str(counts["todo"]),          inline=True)
    e.add_field(name="◑ In Progress",   value=str(counts["in_progress"]),   inline=True)
    e.add_field(name="✕ Blocked",       value=str(counts["blocked"]),       inline=True)
    e.add_field(name="◇ Review Needed", value=str(counts["review_needed"]), inline=True)
    e.add_field(name="✓ Done",          value=str(archive_count),           inline=True)
    e.add_field(name="Active",          value=str(active_count),            inline=True)
    e.add_field(
        name="Overall progress",
        value=f"`{full_bar}` {done_pct}% done",
        inline=False,
    )
    e.set_footer(text="Style 4 — Detailed  ·  Last updated")
    return e

# ── Style 5 — FAQ (one embed per TODO, compact, badge-style) ─────────────────
def _embeds_style5(todos: list, page: int, total_pages: int) -> list[discord.Embed]:
    """Returns one embed per TODO, styled like the AnymeX FAQ cards."""
    embeds = []
    if not todos:
        e = discord.Embed(
            title="Active TODOs",
            description="No active TODOs on this page.",
            color=0x5865F2,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        e.set_footer(text=f"Page {page} of {total_pages}  ·  Style 5 — FAQ  ·  Last updated")
        return [e]

    for t in todos:
        status   = t.get("status", "todo")
        color    = STATUS_COLORS.get(status, 0x5865F2)
        label    = STATUS_LABELS.get(status, status)
        st_icon  = STATUS_ICONS.get(status, "○")
        priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
        pri_icon = PRIORITY_ICONS.get(t.get("priority", "medium"), "◈")
        asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
        added    = f"<@{t['added_by_id']}>"
        ai_tag   = "  ✦ AI" if t.get("auto_generated") else ""

        desc_parts = [f"`{st_icon} {label}`  {pri_icon} **{priority}**{ai_tag}"]

        ai_desc = t.get("ai_description", "")
        if ai_desc:
            desc_parts.append(f"> *{ai_desc}*")

        tags = t.get("tags", [])
        if tags:
            desc_parts.append(_tag_badges(tags))

        desc_parts.append(f"-# Assigned: {asgn}  ·  Added by: {added}")

        e = discord.Embed(
            title=f"#{t['id']} — {t['title']}",
            description="\n".join(desc_parts),
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        src_links = t.get("source_message_links", [])
        if src_links:
            e.set_footer(text=f"Page {page}/{total_pages}  ·  Style 5")
        else:
            e.set_footer(text=f"Page {page}/{total_pages}  ·  Style 5")

        embeds.append(e)

    return embeds

def _stats_style5(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    total    = active_count or 1
    full_bar = _progress_bar(archive_count, archive_count + active_count, 12)
    done_pct = round((archive_count / max(archive_count + active_count, 1)) * 100)
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x6A5ACD,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.description = (
        f"`○ To Do` **{counts['todo']}**   "
        f"`◑ In Progress` **{counts['in_progress']}**   "
        f"`◇ Review` **{counts['review_needed']}**   "
        f"`✕ Blocked` **{counts['blocked']}**\n"
        f"\n`{full_bar}` {done_pct}% done  ·  ✓ **{archive_count}** archived  ·  Active: **{active_count}**"
    )
    e.set_footer(text="Style 5 — FAQ  ·  Last updated")
    return e

# ── Style 6 — Full Detailed (one embed per TODO, full text, no truncation) ────
def _embeds_style6(todos: list, page: int, total_pages: int) -> list[discord.Embed]:
    """Returns one rich embed per TODO with full content, no truncation."""
    embeds = []
    if not todos:
        e = discord.Embed(
            title="Active TODOs",
            description="No active TODOs on this page.",
            color=0x5865F2,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        e.set_footer(text=f"Page {page} of {total_pages}  ·  Style 6 — Full Detail  ·  Last updated")
        return [e]

    for t in todos:
        status   = t.get("status", "todo")
        color    = STATUS_COLORS.get(status, 0x5865F2)
        label    = STATUS_LABELS.get(status, status)
        st_icon  = STATUS_ICONS.get(status, "○")
        priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
        pri_icon = PRIORITY_ICONS.get(t.get("priority", "medium"), "◈")
        asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
        added    = f"<@{t['added_by_id']}>"
        ai_tag   = "  ✦ AI" if t.get("auto_generated") else ""

        e = discord.Embed(
            title=f"#{t['id']} — {t['title']}",
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )

        e.add_field(
            name="Status & Priority",
            value=f"`{st_icon} {label}`  {pri_icon} **{priority}**{ai_tag}",
            inline=False,
        )

        tags = t.get("tags", [])
        if tags:
            e.add_field(name="Tags", value=_tag_badges(tags), inline=False)

        ai_desc = t.get("ai_description", "")
        if ai_desc:
            e.add_field(name="Summary", value=f"> *{ai_desc}*", inline=False)

        src_text = t.get("source_message_text", "")
        if src_text:
            # Show full source text, split across fields if needed (Discord field limit = 1024)
            chunks = [src_text[i:i+1000] for i in range(0, len(src_text), 1000)]
            for idx, chunk in enumerate(chunks):
                fname = "Original Message" if idx == 0 else "​"  # zero-width space for continuation
                e.add_field(name=fname, value=f"```\n{chunk}\n```", inline=False)

        e.add_field(name="Assigned to", value=asgn, inline=True)
        e.add_field(name="Added by",    value=added, inline=True)

        created = t.get("created_at", "")
        if created:
            e.add_field(name="Created", value=f"<t:{int(datetime.datetime.fromisoformat(created).timestamp())}:R>", inline=True)

        src_links = t.get("source_message_links", [])
        if src_links:
            links_val = "\n".join(f"[Jump to message]({lnk})" for lnk in src_links[:3])
            e.add_field(name="Source", value=links_val, inline=False)

        src_imgs = t.get("source_images", [])
        if src_imgs:
            e.set_image(url=src_imgs[0])

        e.set_footer(text=f"Page {page}/{total_pages}  ·  Style 6 — Full Detail")
        embeds.append(e)

    return embeds

def _stats_style6(counts: dict, archive_count: int, active_count: int) -> discord.Embed:
    total    = active_count or 1
    full_bar = _progress_bar(archive_count, archive_count + active_count, 12)
    done_pct = round((archive_count / max(archive_count + active_count, 1)) * 100)
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="○ To Do",         value=str(counts["todo"]),          inline=True)
    e.add_field(name="◑ In Progress",   value=str(counts["in_progress"]),   inline=True)
    e.add_field(name="◇ Review",        value=str(counts["review_needed"]), inline=True)
    e.add_field(name="✕ Blocked",       value=str(counts["blocked"]),       inline=True)
    e.add_field(name="✓ Done",          value=str(archive_count),           inline=True)
    e.add_field(name="Active",          value=str(active_count),            inline=True)
    e.add_field(
        name="Overall progress",
        value=f"`{full_bar}` {done_pct}% done",
        inline=False,
    )
    e.set_footer(text="Style 6 — Full Detail  ·  Last updated")
    return e

# ── Dispatchers ──────────────────────────────────────────────────────────────
_CARD_BUILDERS  = {1: _card_style1, 2: _card_style2, 3: _card_style3, 4: _card_style4}
_STATS_BUILDERS = {1: _stats_style1, 2: _stats_style2, 3: _stats_style3, 4: _stats_style4,
                   5: _stats_style5, 6: _stats_style6}
# Styles 5 & 6 use per-todo embed builders instead of _CARD_BUILDERS
_MULTI_EMBED_STYLES = {5: _embeds_style5, 6: _embeds_style6}

def build_todo_card(t: dict, style: int = 1) -> tuple[str, str]:
    fn = _CARD_BUILDERS.get(style, _card_style1)
    return fn(t)

def build_stats_embed(todos: list, archive_count: int, style: int = 1) -> discord.Embed:
    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    fn = _STATS_BUILDERS.get(style, _stats_style1)
    return fn(counts, archive_count, len(todos))

def build_page_embed(todos: list, page: int, total_pages: int, style: int = 1) -> discord.Embed:
    """Single-embed builder for styles 1–4 (fields-based). Used internally."""
    color = 0x5865F2
    e = discord.Embed(
        title=f"Active TODOs — Page {page}/{total_pages}",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for t in todos:
        name, value = build_todo_card(t, style=style)
        e.add_field(name=name, value=value, inline=False)
    if not todos:
        e.description = "No active TODOs on this page."
    e.set_footer(text=f"Page {page} of {total_pages}  ·  Style {style}  ·  Last updated")
    return e

def build_page_embeds(todos: list, page: int, total_pages: int, style: int = 1) -> list[discord.Embed]:
    """Returns a list of embeds for a page.
    Styles 5/6 produce one embed per TODO; styles 1–4 produce a single embed with fields."""
    if style in _MULTI_EMBED_STYLES:
        return _MULTI_EMBED_STYLES[style](todos, page, total_pages)
    return [build_page_embed(todos, page, total_pages, style=style)]

async def send_todo_embeds(send_fn, todos: list, style: int, title: str = "", max_pages: int = 3):
    """Send a list of TODOs respecting the current style.
    send_fn — an async callable that accepts a single discord.Embed, e.g.:
        lambda e: ctx.send(embed=e)
        lambda e: interaction.followup.send(embed=e, ephemeral=True)
    For styles 5/6: sends one embed per TODO with rate-limit delay.
    For styles 1–4: batches into paged field-embeds."""
    if not todos:
        return
    pages = [todos[i:i+TODOS_PER_PAGE] for i in range(0, len(todos), TODOS_PER_PAGE)]
    total_pages = len(pages)

    if style in _MULTI_EMBED_STYLES:
        for page_todos in pages[:max_pages]:
            for e in _MULTI_EMBED_STYLES[style](page_todos, 1, 1):
                await send_fn(e)
                await asyncio.sleep(0.55)
    else:
        for i, page_todos in enumerate(pages[:max_pages]):
            e = build_page_embed(page_todos, i + 1, total_pages, style=style)
            if title and i == 0:
                e.title = title
            await send_fn(e)

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ══════════════════════════════════════════════════════════════════════════════

async def health(request):
    return web.Response(text="AnymeX TODO Bot is running.")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health server on port {PORT}")

# ══════════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
async def _get_prefix(bot, message):
    try:
        async with aiohttp.ClientSession() as session:
            cfg, _ = await gh_read(session, FILE_CONFIG)
        return (cfg or {}).get("prefix", "ax!")
    except Exception:
        return "ax!"

bot = commands.Bot(command_prefix=_get_prefix, intents=intents, help_command=None)

async def has_todo_role_msg(message: discord.Message) -> bool:
    """Permission check for prefix commands (admin or todo role)."""
    if message.author.guild_permissions.administrator:
        return True
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    if not cfg:
        return False
    todo_roles    = cfg.get("todo_roles", [])
    user_role_ids = [str(r.id) for r in message.author.roles]
    return any(rid in user_role_ids for rid in todo_roles)

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# ══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOG
# ══════════════════════════════════════════════════════════════════════════════

TAG_COLORS = {
    "bug":      "🔴",
    "feature":  "🟢",
    "urgent":   "🟠",
    "docs":     "🔵",
    "refactor": "🟣",
    "question": "🟡",
}

def _tag_badges(tags: list) -> str:
    """Return a compact string of tag badges, e.g. `bug` `urgent`"""
    if not tags:
        return ""
    return "  ".join(f"`{t}`" for t in tags)

async def log_activity(guild: discord.Guild, cfg: dict, action: str, todo: dict,
                       user: discord.Member | None, extra: str = ""):
    """Post an activity embed to the configured log channel."""
    log_ch_id = cfg.get("log_channel")
    if not log_ch_id:
        return
    ch = guild.get_channel(int(log_ch_id))
    if not ch:
        return
    status = todo.get("status", "todo")
    color  = STATUS_COLORS.get(status, 0x5865F2)
    title_str = todo.get("title", "")[:60]
    user_str  = user.mention if user else "Unknown"
    e = discord.Embed(
        title=f"📋 TODO #{todo['id']} — {title_str}",
        description=f"**{action}**\n{extra}" if extra else f"**{action}**",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.set_footer(text=f"By {user.display_name if user else 'Unknown'}")
    try:
        await ch.send(embed=e)
    except Exception:
        pass



async def has_todo_role(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    if not cfg:
        return False
    todo_roles   = cfg.get("todo_roles", [])
    user_role_ids = [str(r.id) for r in interaction.user.roles]
    return any(rid in user_role_ids for rid in todo_roles)

# ══════════════════════════════════════════════════════════════════════════════
# TODO BOARD UPDATER
# ══════════════════════════════════════════════════════════════════════════════

def _build_wanted_pages(active: list, archive: list, style: int) -> list[dict]:
    """
    Build the list of "wanted" page slots we need on Discord right now.
    Returns a list of dicts:
      { "todo_ids": [1,3,7], "embed": discord.Embed }

    Styles 1-4  → one message per page-of-10
    Styles 5-6  → one message per individual TODO
    """
    total_todos = max(len(active), 1)
    pages_of_todos = [active[i:i+TODOS_PER_PAGE]
                      for i in range(0, total_todos, TODOS_PER_PAGE)]
    total_pages = len(pages_of_todos)

    wanted = []
    for page_num, page_todos in enumerate(pages_of_todos, start=1):
        embeds = build_page_embeds(page_todos, page_num, total_pages, style=style)
        if style in (5, 6):
            # one embed per todo — zip them together
            for todo, embed in zip(page_todos, embeds):
                wanted.append({"todo_ids": [todo["id"]], "embed": embed})
            # if page_todos is empty build_page_embeds returns a single placeholder
            if not page_todos:
                wanted.append({"todo_ids": [], "embed": embeds[0]})
        else:
            # styles 1-4: one embed for the whole page
            wanted.append({
                "todo_ids": [t["id"] for t in page_todos],
                "embed":    embeds[0],
            })
    return wanted


async def update_todo_board(guild: discord.Guild, cfg: dict):
    """
    Smart board updater:
    - Always fresh-reads config, todos and board_ids
    - Compares what IS on Discord (stored page slots with todo_ids) vs what SHOULD be
    - If style changed  → full wipe + repost
    - If stats missing  → full wipe + repost
    - If a page slot's todo_ids changed (todo added/deleted/reordered onto that page)
        styles 1-4: edit that page message with fresh embed
        styles 5-6: delete removed-todo messages, post new-todo messages, edit changed ones
    - If extra old messages exist (from deleted todos) → delete them
    - Never touches a message whose content hasn't changed
    """
    # Always fresh-read config — never trust the passed-in cfg (could be stale)
    async with aiohttp.ClientSession() as session:
        fresh_cfg, _ = await gh_read_fresh(session, FILE_CONFIG)
    cfg = fresh_cfg or cfg

    todo_ch_id = cfg.get("todo_channel")
    if not todo_ch_id:
        return
    ch = guild.get_channel(int(todo_ch_id))
    if not ch:
        return

    async with aiohttp.ClientSession() as session:
        todos,     _        = await gh_read_fresh(session, FILE_TODOS)
        archive,   _        = await gh_read(session, FILE_TODOS_ARCHIVE)
        board_ids, bid_sha  = await gh_read_fresh(session, FILE_BOARD_IDS)
    todos     = todos    or []
    archive   = archive  or []
    board_ids = board_ids or DEFAULT_BOARD_IDS.copy()

    active = [t for t in todos if t["status"] != "done"]
    style  = int(cfg.get("todo_style", 1))

    stats_embed = build_stats_embed(active, len(archive), style=style)
    wanted      = _build_wanted_pages(active, archive, style)

    # ── Full wipe if style changed or stats message is gone ───────────────────
    stored_style    = board_ids.get("style")
    stats_msg_id    = board_ids.get("stats_message_id")
    need_full_wipe  = False

    if stored_style != style:
        need_full_wipe = True
    elif not stats_msg_id:
        need_full_wipe = True
    else:
        try:
            stats_msg = await ch.fetch_message(int(stats_msg_id))
            await safe_edit(stats_msg, stats_embed)
        except Exception:
            need_full_wipe = True

    if need_full_wipe:
        await _refresh_todo_board(ch, stats_embed, wanted, style=style)
        return

    # ── Build lookup: todo_id → stored page slot ──────────────────────────────
    # stored_pages is the previous snapshot saved in board_ids.json
    stored_pages = board_ids.get("pages") or []          # [{"message_id":..,"todo_ids":[..]}]
    stored_by_todo: dict[int, str] = {}                  # todo_id → message_id
    all_stored_msg_ids: set[str]   = set()
    for slot in stored_pages:
        mid = slot.get("message_id")
        if mid:
            all_stored_msg_ids.add(mid)
            for tid in slot.get("todo_ids", []):
                stored_by_todo[int(tid)] = mid

    # ── Build lookup: todo_id → wanted slot index ─────────────────────────────
    wanted_by_todo: dict[int, int] = {}
    for idx, slot in enumerate(wanted):
        for tid in slot["todo_ids"]:
            wanted_by_todo[int(tid)] = idx

    # ── Figure out which stored message IDs are no longer needed ─────────────
    wanted_todo_ids = set(wanted_by_todo.keys())
    stale_msg_ids   = set()
    for slot in stored_pages:
        slot_tids = {int(t) for t in slot.get("todo_ids", [])}
        # A slot is stale if NONE of its todos are in the current wanted list
        if not slot_tids & wanted_todo_ids:
            mid = slot.get("message_id")
            if mid:
                stale_msg_ids.add(mid)

    # Delete stale messages (completed/deleted todos whose embed is now gone)
    for mid in stale_msg_ids:
        try:
            old_msg = await ch.fetch_message(int(mid))
            await old_msg.delete()
        except Exception:
            pass

    # ── Now process each wanted slot ──────────────────────────────────────────
    new_pages      = []
    need_save      = bool(stale_msg_ids)   # if we deleted anything, save updated IDs

    for slot in wanted:
        slot_tids  = slot["todo_ids"]
        slot_embed = slot["embed"]

        # Find if ALL todos in this slot already share the same stored message
        candidate_msg_ids = {stored_by_todo[tid] for tid in slot_tids if tid in stored_by_todo}
        # Valid reuse: exactly one message covers exactly these todo_ids and nothing extra
        reuse_mid = None
        if len(candidate_msg_ids) == 1:
            cid = next(iter(candidate_msg_ids))
            # Find the stored slot for this message
            for s in stored_pages:
                if s.get("message_id") == cid:
                    stored_tids = {int(t) for t in s.get("todo_ids", [])}
                    if stored_tids == set(slot_tids):
                        reuse_mid = cid
                    break

        if reuse_mid:
            # Same todos, same message → just edit the embed in place
            try:
                existing = await ch.fetch_message(int(reuse_mid))
                await safe_edit(existing, slot_embed)
                # Preserve existing thread_id
                existing_thread_id = None
                for s in stored_pages:
                    if s.get("message_id") == reuse_mid:
                        existing_thread_id = s.get("thread_id")
                        break
                new_pages.append({"message_id": reuse_mid, "thread_id": existing_thread_id, "todo_ids": slot_tids})
            except Exception:
                # Message gone — need a full wipe to be safe
                await _refresh_todo_board(ch, stats_embed, wanted, style=style)
                return
        else:
            # Todo set changed for this slot → post a new message
            need_save = True
            delay = _SEND_DELAY.get(style, 0.0)
            new_msg = await safe_send(ch, slot_embed, delay=delay)
            thread_id = None
            if style in (5, 6) and slot_tids:
                thread_id = await _create_todo_thread(new_msg, slot_tids[0])
            new_pages.append({"message_id": str(new_msg.id), "thread_id": thread_id, "todo_ids": slot_tids})

    # ── Save updated board_ids.json if anything changed ───────────────────────
    if need_save:
        async with aiohttp.ClientSession() as session:
            bid2, sha2 = await gh_read_fresh(session, FILE_BOARD_IDS)
            bid2 = bid2 or DEFAULT_BOARD_IDS.copy()
            bid2["stats_message_id"] = stats_msg_id
            bid2["style"]            = style
            bid2["pages"]            = new_pages
            await gh_write(session, FILE_BOARD_IDS, bid2, sha2, "Update board message IDs")


async def safe_send(ch: discord.TextChannel, embed: discord.Embed,
                    delay: float = 0.0) -> discord.Message:
    """Send one embed, auto-retrying on 429 rate limits. Optional pre-send delay."""
    if delay > 0:
        await asyncio.sleep(delay)
    for attempt in range(5):
        try:
            return await ch.send(embed=embed)
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(getattr(e, "retry_after", None) or 1.0)
                print(f"[rate limit] send throttled — waiting {retry_after:.2f}s (attempt {attempt+1})")
                await asyncio.sleep(retry_after + 0.25)
            else:
                raise
    raise RuntimeError("safe_send: exceeded retry limit")

async def safe_edit(msg: discord.Message, embed: discord.Embed) -> None:
    """Edit one message, auto-retrying on 429 rate limits."""
    for attempt in range(5):
        try:
            await msg.edit(embed=embed)
            return
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = float(getattr(e, "retry_after", None) or 1.0)
                print(f"[rate limit] edit throttled — waiting {retry_after:.2f}s (attempt {attempt+1})")
                await asyncio.sleep(retry_after + 0.25)
            else:
                raise
    raise RuntimeError("safe_edit: exceeded retry limit")

# Delay between sends for styles that post one embed per TODO (5 & 6).
# Keeps us well under Discord's 5 msg/s burst limit.
_SEND_DELAY: dict[int, float] = {5: 0.55, 6: 0.55}

async def _refresh_todo_board(ch: discord.TextChannel, stats_embed: discord.Embed,
                               wanted: list, style: int = 1):
    """
    Wipe the entire todo channel and repost all bot cards cleanly.
    `wanted` is the list produced by _build_wanted_pages():
      [{"todo_ids": [...], "embed": discord.Embed}, ...]
    Saves fresh message IDs to board_ids.json afterwards.
    """
    # Bulk-delete everything in the channel
    try:
        await ch.purge(limit=None, check=lambda m: True)
    except Exception:
        try:
            async for msg in ch.history(limit=200):
                try:
                    await msg.delete()
                except Exception:
                    pass
        except Exception:
            pass

    delay = _SEND_DELAY.get(style, 0.0)

    stats_msg  = await safe_send(ch, stats_embed)
    new_pages  = []
    for slot in wanted:
        msg = await safe_send(ch, slot["embed"], delay=delay)
        thread_id = None
        # Auto-create thread for single-TODO styles 5 & 6
        if style in (5, 6) and slot["todo_ids"]:
            tid = slot["todo_ids"][0]
            thread_id = await _create_todo_thread(msg, tid)
        new_pages.append({"message_id": str(msg.id), "thread_id": thread_id, "todo_ids": slot["todo_ids"]})

    # Save everything to board_ids.json
    async with aiohttp.ClientSession() as session:
        bid, sha = await gh_read_fresh(session, FILE_BOARD_IDS)
        bid = bid or DEFAULT_BOARD_IDS.copy()
        bid["stats_message_id"] = str(stats_msg.id)
        bid["style"]            = style
        bid["pages"]            = new_pages
        await gh_write(session, FILE_BOARD_IDS, bid, sha, "Refreshed todo board IDs")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIRM VIEW  (Yes / No buttons, only triggering user can click)
# ══════════════════════════════════════════════════════════════════════════════

class TodoConfirmView(discord.ui.View):
    def __init__(self, author_id: int, title: str, sources: list[dict], cfg: dict,
                 fuzzy_warn: str = "", auto_generated: bool = False, ai_description: str = ""):
        super().__init__(timeout=60)
        self.author_id      = author_id
        self.title          = title
        self.sources        = sources
        self.cfg            = cfg
        self.fuzzy_warn     = fuzzy_warn
        self.auto_generated = auto_generated
        self.ai_description = ai_description
        self.done           = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who triggered this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, add todo", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.defer()

        async with aiohttp.ClientSession() as session:
            todos, sha = await gh_read_fresh(session, FILE_TODOS)
            todos = todos or []
            all_ids = [t["id"] for t in todos]
            todo_id = (max(all_ids) + 1) if all_ids else 1
            # Merge all source messages
            combined_text  = "\n---\n".join(s["text"]  for s in self.sources if s.get("text"))
            combined_imgs  = [u for s in self.sources for u in s.get("images", [])]
            combined_files = [u for s in self.sources for u in s.get("files",  [])]
            combined_links = [s["link"] for s in self.sources if s.get("link")]
            source_ids     = [s["id"]   for s in self.sources if s.get("id")]

            todo = {
                "id":                   todo_id,
                "title":                self.title[:200],
                "auto_generated":       self.auto_generated,
                "ai_description":       self.ai_description,
                "status":               "todo",
                "priority":             "medium",
                "tags":                 [],
                "added_by_id":          str(interaction.user.id),
                "added_by_name":        str(interaction.user),
                "assigned_to_id":       None,
                "assigned_to_name":     None,
                "created_at":           now_iso(),
                "updated_at":           now_iso(),
                "last_reminded_at":     None,
                "source_message_ids":   source_ids,
                "source_message_links": combined_links,
                "source_message_text":  combined_text[:1000],
                "source_images":        combined_imgs,
                "source_files":         combined_files,
            }
            todos.append(todo)
            await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id}: {self.title[:50]}")

        reply = f"Added as TODO **#{todo_id}**."
        if self.fuzzy_warn:
            reply += f"\n> Similar existing todo: {self.fuzzy_warn}"
        await interaction.edit_original_response(content=reply, view=None)
        async with aiohttp.ClientSession() as _s:
            _cfg2, _ = await gh_read(_s, FILE_CONFIG)
        await log_activity(interaction.guild, _cfg2 or {}, "TODO Added", todo,
                           interaction.guild.get_member(interaction.user.id))
        await update_todo_board(interaction.guild, self.cfg)

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_timeout(self):
        if not self.done:
            # Can't edit without interaction after timeout, silently pass
            pass

# ══════════════════════════════════════════════════════════════════════════════
# CORE TODO CREATION HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _extract_source(msg: discord.Message) -> dict:
    """Pull text, images, files and jump link out of a discord.Message."""
    return {
        "id":     str(msg.id),
        "link":   f"https://discord.com/channels/{msg.guild.id}/{msg.channel.id}/{msg.id}",
        "text":   msg.content[:500] if msg.content else "",
        "images": [a.url for a in msg.attachments if a.content_type and a.content_type.startswith("image")],
        "files":  [a.url for a in msg.attachments if not (a.content_type and a.content_type.startswith("image"))],
    }

async def _resolve_extra_msg_ids(channel: discord.TextChannel, raw_ids: list[str]) -> list[dict]:
    """Fetch extra message IDs provided via --msgs and return their source dicts."""
    sources = []
    for mid in raw_ids:
        mid = mid.strip()
        if not mid.isdigit():
            continue
        try:
            m = await channel.fetch_message(int(mid))
            sources.append(_extract_source(m))
        except Exception:
            pass
    return sources

async def trigger_todo_confirm(
    trigger_msg: discord.Message,
    title: str,
    cfg: dict,
    extra_msg_ids: list[str] = None,
    ref_msg: discord.Message = None,
    auto_generated: bool = False,
    ai_description: str = "",
):
    """
    Show a public confirm prompt (only triggering user can click).
    Gathers all source messages: ref_msg + extra IDs + trigger_msg itself.
    """
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read_fresh(session, FILE_TODOS)
    todos = todos or []

    # Primary source: referenced message > trigger message
    primary = ref_msg or trigger_msg
    sources = [_extract_source(primary)]

    # Add extra messages from --msgs
    if extra_msg_ids:
        extras = await _resolve_extra_msg_ids(trigger_msg.channel, extra_msg_ids)
        sources.extend(extras)

    # Duplicate check against primary source
    dup_kind, dup_todo = check_duplicate(todos, title, str(primary.id))

    if dup_kind == "message_id":
        await trigger_msg.reply(
            f"This message is already tracked as TODO **#{dup_todo['id']}**.",
            mention_author=False,
        )
        return

    if dup_kind == "exact":
        await trigger_msg.reply(
            f"A TODO with this exact title already exists: **#{dup_todo['id']}** — {dup_todo['title'][:80]}",
            mention_author=False,
        )
        return

    fuzzy_warn = ""
    if dup_kind == "fuzzy":
        fuzzy_warn = f"**#{dup_todo['id']}** — {dup_todo['title'][:60]}"

    # Build confirm prompt
    src_summary = f'**"{title}"**'
    if len(sources) > 1:
        src_summary += f" (combining {len(sources)} messages)"
    if fuzzy_warn:
        src_summary += f"\n> Similar todo exists: {fuzzy_warn}"

    view = TodoConfirmView(
        author_id      = trigger_msg.author.id,
        title          = title,
        sources        = sources,
        cfg            = cfg,
        fuzzy_warn     = fuzzy_warn,
        auto_generated = auto_generated,
        ai_description = ai_description,
    )
    # Build confirm message — always show original user message alongside AI output
    confirm_lines = [f"{trigger_msg.author.mention} Add this as a todo?"]
    confirm_lines.append(f"**Title:** {src_summary}")
    if auto_generated:
        confirm_lines.append(f"**AI title** ✦ — generated from your message")
    if ai_description:
        confirm_lines.append(f"**Summary:** {ai_description}")
    # Always show the original message text so user can verify AI understood correctly
    primary_text = (sources[0].get("text") or "").strip()
    if primary_text and auto_generated:
        confirm_lines.append(f"**Your message:** {primary_text[:300]}")
    await trigger_msg.reply(
        "\n".join(confirm_lines),
        view=view,
        mention_author=False,
    )

# ══════════════════════════════════════════════════════════════════════════════
# REASSIGN CONFIRM VIEW  (Yes / No buttons for reassigning an already-assigned TODO)
# ══════════════════════════════════════════════════════════════════════════════

class ReassignConfirmView(discord.ui.View):
    def __init__(self, author_id: int, todo_id: int, target: discord.Member,
                 current_assignee_id: str, todos: list, sha: str, cfg: dict, guild: discord.Guild):
        super().__init__(timeout=60)
        self.author_id          = author_id
        self.todo_id            = todo_id
        self.target             = target
        self.current_assignee_id = current_assignee_id
        self.todos              = todos
        self.sha                = sha
        self.cfg                = cfg
        self.guild              = guild
        self.done               = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who triggered this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes, reassign", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        todo = next((t for t in self.todos if t["id"] == self.todo_id), None)
        if not todo:
            await interaction.response.edit_message(content=f"TODO #{self.todo_id} no longer exists.", view=None)
            return
        todo["assigned_to_id"]   = str(self.target.id)
        todo["assigned_to_name"] = str(self.target)
        todo["updated_at"]       = now_iso()
        if todo["status"] == "todo":
            todo["status"] = "in_progress"
        async with aiohttp.ClientSession() as session:
            await gh_write(session, FILE_TODOS, self.todos, self.sha,
                           f"TODO #{self.todo_id} reassigned to {self.target}")
        await interaction.response.edit_message(
            content=f"✅ TODO **#{self.todo_id}** reassigned to {self.target.mention}.",
            view=None,
        )
        # Notify old assignee
        old_member = self.guild.get_member(int(self.current_assignee_id))
        if old_member and old_member.id != interaction.user.id:
            try:
                await old_member.send(
                    f"You were unassigned from **TODO #{self.todo_id}** by {interaction.user.mention} in **{self.guild.name}**."
                )
            except Exception:
                pass
        async with aiohttp.ClientSession() as _s:
            _cfg2, _ = await gh_read(_s, FILE_CONFIG)
        await log_activity(self.guild, _cfg2 or {}, "Reassigned",
                           todo, interaction.guild.get_member(interaction.user.id),
                           extra=f"→ {self.target.mention}")
        await update_todo_board(self.guild, self.cfg)

    @discord.ui.button(label="No, cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Reassignment cancelled.", view=None)

    async def on_timeout(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# THREAD HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _create_todo_thread(msg: discord.Message, todo_id: int) -> str | None:
    """
    Create a thread on a board card message (styles 5/6).
    Sets invitable=False, adds all members with a TODO role.
    Returns the thread ID as string, or None on failure.
    """
    try:
        thread = await msg.create_thread(
            name=f"TODO #{todo_id}",
            auto_archive_duration=10080,  # 7 days
        )
        try:
            await thread.edit(invitable=False)
        except Exception:
            pass
        async with aiohttp.ClientSession() as session:
            cfg, _ = await gh_read(session, FILE_CONFIG)
        cfg = cfg or {}
        todo_role_ids = [int(r) for r in cfg.get("todo_roles", [])]
        guild = msg.guild
        if guild and todo_role_ids:
            for member in guild.members:
                if any(r.id in todo_role_ids for r in member.roles):
                    try:
                        await thread.add_user(member)
                    except Exception:
                        pass
        return str(thread.id)
    except Exception as e:
        print(f"[thread] Failed to create thread for TODO #{todo_id}: {e}")
        return None


async def _get_or_create_thread_for_todo(guild: discord.Guild, cfg: dict,
                                          todo_id: int, todo_title: str) -> discord.Thread | None:
    """
    For styles 1-4: get/create a dedicated thread in the thread_channel.
    Returns the thread object or None.
    """
    thread_ch_id = cfg.get("thread_channel")
    if not thread_ch_id:
        return None
    ch = guild.get_channel(int(thread_ch_id))
    if not ch:
        return None
    try:
        thread_name = f"TODO #{todo_id} — {todo_title[:40]}"
        active_threads = await guild.active_threads()
        for t in active_threads:
            if t.parent_id == ch.id and f"TODO #{todo_id}" in t.name:
                return t
        msg = await ch.send(f"**TODO #{todo_id}** — {todo_title[:80]}")
        thread = await msg.create_thread(name=thread_name, auto_archive_duration=10080)
        try:
            await thread.edit(invitable=False)
        except Exception:
            pass
        todo_role_ids = [int(r) for r in cfg.get("todo_roles", [])]
        if todo_role_ids:
            for member in guild.members:
                if any(r.id in todo_role_ids for r in member.roles):
                    try:
                        await thread.add_user(member)
                    except Exception:
                        pass
        return thread
    except Exception as e:
        print(f"[thread] Failed to get/create thread for TODO #{todo_id}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-ASSIGN — REACTION  (✋ on board card)
# ══════════════════════════════════════════════════════════════════════════════

class TakeOverView(discord.ui.View):
    """Confirm taking over an already-assigned TODO via ✋ reaction."""
    def __init__(self, reactor: discord.Member, todo: dict, todos: list, sha: str,
                 cfg: dict, guild: discord.Guild):
        super().__init__(timeout=60)
        self.reactor = reactor
        self.todo    = todo
        self.todos   = todos
        self.sha     = sha
        self.cfg     = cfg
        self.guild   = guild
        self.done    = False

    @discord.ui.button(label="Take Over", style=discord.ButtonStyle.danger)
    async def take_over(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.reactor.id:
            await interaction.response.send_message("Not for you.", ephemeral=True); return
        self.done = True
        self.stop()
        old_id = self.todo.get("assigned_to_id")
        self.todo["assigned_to_id"]   = str(self.reactor.id)
        self.todo["assigned_to_name"] = str(self.reactor)
        self.todo["updated_at"]       = now_iso()
        if self.todo["status"] == "todo":
            self.todo["status"] = "in_progress"
        async with aiohttp.ClientSession() as session:
            await gh_write(session, FILE_TODOS, self.todos, self.sha,
                           f"TODO #{self.todo['id']} taken over by {self.reactor}")
        await interaction.response.edit_message(
            content=f"✅ You're now assigned to **TODO #{self.todo['id']}**.", view=None
        )
        if old_id:
            old_member = self.guild.get_member(int(old_id))
            if old_member and old_member.id != self.reactor.id:
                try:
                    await old_member.send(
                        f"You were unassigned from **TODO #{self.todo['id']}** by "
                        f"{self.reactor.mention} in **{self.guild.name}**."
                    )
                except Exception:
                    pass
        async with aiohttp.ClientSession() as _s:
            _cfg2, _ = await gh_read(_s, FILE_CONFIG)
        await log_activity(self.guild, _cfg2 or {}, "Taken Over (✋ reaction)",
                           self.todo, self.reactor, extra=f"→ {self.reactor.mention}")
        await update_todo_board(self.guild, self.cfg)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.reactor.id:
            await interaction.response.send_message("Not for you.", ephemeral=True); return
        self.done = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def on_timeout(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# REMINDER BACKGROUND TASK
# ══════════════════════════════════════════════════════════════════════════════

_reminder_last_fired: str = ""   # "YYYY-MM-DD HH:MM" — prevents double-fire within a day


def _add_minutes(hhmm: str, mins: int) -> str:
    """Add minutes to HH:MM string and return HH:MM."""
    try:
        h, m = map(int, hhmm.split(":"))
        total = h * 60 + m + mins
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"
    except Exception:
        return "99:99"


@tasks.loop(minutes=5)
async def reminder_task():
    """Fire reminders once per day at the configured UTC time."""
    global _reminder_last_fired
    try:
        async with aiohttp.ClientSession() as session:
            cfg, _ = await gh_read(session, FILE_CONFIG)
        cfg = cfg or {}
        reminder_time = cfg.get("reminder_time", "09:00")
        reminder_days = int(cfg.get("reminder_days", 3))
        reminder_ch_id = cfg.get("reminder_channel")

        now_utc  = datetime.datetime.now(datetime.timezone.utc)
        now_hhmm = now_utc.strftime("%H:%M")
        today_key = f"{now_utc.strftime('%Y-%m-%d')} {reminder_time}"

        # Only fire inside the correct 5-minute window, once per day
        if not (reminder_time <= now_hhmm < _add_minutes(reminder_time, 5)):
            return
        if _reminder_last_fired == today_key:
            return
        _reminder_last_fired = today_key

        async with aiohttp.ClientSession() as session:
            todos, _ = await gh_read_fresh(session, FILE_TODOS)
        todos = todos or []

        cutoff   = now_utc - datetime.timedelta(days=reminder_days)
        need_save = False

        for guild in bot.guilds:
            for todo in todos:
                if todo.get("status") not in ("in_progress", "blocked"):
                    continue
                if not todo.get("assigned_to_id"):
                    continue

                # Check last updated time
                updated_str = todo.get("updated_at") or todo.get("created_at", "")
                try:
                    updated_dt = datetime.datetime.fromisoformat(updated_str)
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
                if updated_dt > cutoff:
                    continue

                # Already reminded recently?
                last_reminded = todo.get("last_reminded_at")
                if last_reminded:
                    try:
                        lr_dt = datetime.datetime.fromisoformat(last_reminded)
                        if lr_dt.tzinfo is None:
                            lr_dt = lr_dt.replace(tzinfo=datetime.timezone.utc)
                        if (now_utc - lr_dt).days < reminder_days:
                            continue
                    except Exception:
                        pass

                member = guild.get_member(int(todo["assigned_to_id"]))
                if not member:
                    continue

                msg_text = (
                    f"⏰ **Reminder** — TODO **#{todo['id']}** *{todo['title'][:60]}* "
                    f"has been **{STATUS_LABELS.get(todo['status'], todo['status'])}** "
                    f"for {reminder_days}+ days without an update. {member.mention}"
                )
                try:
                    if reminder_ch_id:
                        ch = guild.get_channel(int(reminder_ch_id))
                        if ch:
                            await ch.send(msg_text)
                    else:
                        await member.send(msg_text)
                except Exception:
                    pass

                todo["last_reminded_at"] = now_iso()
                need_save = True

        if need_save:
            async with aiohttp.ClientSession() as session:
                todos2, sha2 = await gh_read_fresh(session, FILE_TODOS)
                id_map = {t["id"]: t.get("last_reminded_at") for t in todos if t.get("last_reminded_at")}
                for t in (todos2 or []):
                    if t["id"] in id_map:
                        t["last_reminded_at"] = id_map[t["id"]]
                await gh_write(session, FILE_TODOS, todos2 or todos, sha2, "Reminders: update last_reminded_at")

    except Exception as e:
        print(f"[reminder_task] Error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"AnymeX TODO Bot online as {bot.user}")
    await ensure_files()
    if not getattr(bot, "_synced", False):
        try:
            await bot.tree.sync()
            bot._synced = True
            print("Slash commands synced")
        except Exception as e:
            print(f"Sync failed: {e}")
    if not reminder_task.is_running():
        reminder_task.start()
        print("Reminder task started")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """When a member gains a TODO role, add them to all open TODO threads."""
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}
    todo_role_ids = [str(r) for r in cfg.get("todo_roles", [])]
    if not todo_role_ids:
        return

    before_role_ids = {str(r.id) for r in before.roles}
    after_role_ids  = {str(r.id) for r in after.roles}
    newly_added = after_role_ids - before_role_ids

    # Check if any of the newly added roles is a TODO role
    if not any(rid in todo_role_ids for rid in newly_added):
        return

    # Add this member to all currently active threads
    try:
        active_threads = await after.guild.active_threads()
    except Exception:
        return

    todo_ch_id    = cfg.get("todo_channel")
    thread_ch_id  = cfg.get("thread_channel")

    for thread in active_threads:
        parent_id = str(thread.parent_id) if thread.parent_id else None
        # Only touch threads that belong to the TODO board channel or thread channel
        if parent_id not in (todo_ch_id, thread_ch_id):
            continue
        try:
            await thread.add_user(after)
        except Exception:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}

    todo_ch_id = cfg.get("todo_channel")
    content    = message.content.strip()
    TRIGGER    = "#addtodo"

    # ── #addtodo trigger — works anywhere ─────────────────────────────────────
    if TRIGGER in content.lower():
        idx  = content.lower().index(TRIGGER)
        rest = content[idx + len(TRIGGER):].strip()

        # Parse --msgs flag: #addtodo My title --msgs 111 222 333
        extra_ids      = []
        auto_generated = False
        ai_description = ""
        if "--msgs" in rest:
            parts     = rest.split("--msgs", 1)
            title     = parts[0].strip()
            extra_ids = parts[1].strip().split()
        else:
            title = rest

        if not title:
            # No title given — use AI to generate one from the message content
            # Resolve referenced message first so we get its text too
            ref_msg_early = None
            if message.reference and message.reference.message_id:
                try:
                    ref_msg_early = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    pass
            source_text = (ref_msg_early.content if ref_msg_early else message.content) or ""
            source_text = source_text.replace(TRIGGER, "").strip()
            if not source_text:
                await message.reply(
                    "Please include a title or some message content for AI to generate one from:\n"
                    "`#addtodo Your title here`\n"
                    "Or reply to a message with just `#addtodo` to auto-generate the title.",
                    mention_author=False,
                )
                await bot.process_commands(message)
                return
            # Show typing indicator while AI works
            async with message.channel.typing():
                ai_title, ai_desc = await ai_generate_title(source_text)
            title          = ai_title
            auto_generated = True
            ai_description = ai_desc

        # Resolve referenced message if this is a reply
        ref_msg = None
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
            except Exception:
                pass

        # Delete trigger message if in todo channel to keep it clean
        if todo_ch_id and str(message.channel.id) == str(todo_ch_id):
            try:
                await message.delete()
            except Exception:
                pass

        await trigger_todo_confirm(
            message, title, cfg, extra_ids, ref_msg,
            auto_generated=auto_generated,
            ai_description=ai_description,
        )
        return

    # ── TODO channel: delete ALL human messages — only bot cards stay ──────────
    if todo_ch_id and str(message.channel.id) == str(todo_ch_id):
        # Save details before deleting
        author_mention = message.author.mention
        msg_text       = message.content or ""
        attachments    = message.attachments[:]

        try:
            await message.delete()
        except Exception:
            pass

        # Re-attach any files to send as temp notice
        files = []
        for att in attachments[:5]:
            try:
                async with aiohttp.ClientSession() as dl_session:
                    async with dl_session.get(att.url) as resp:
                        if resp.status == 200:
                            file_bytes = await resp.read()
                            files.append(discord.File(
                                __import__("io").BytesIO(file_bytes),
                                filename=att.filename,
                            ))
            except Exception:
                pass

        # If it was a #addtodo command we already handled it above — don't guide again
        if TRIGGER not in content.lower():
            guide_lines = [
                f"{author_mention} This channel is for the TODO board only.",
                f"To add a TODO: `#addtodo Your title here`",
                f"Or reply to any message with `#addtodo` and AI will auto-generate a title.",
                f"To combine multiple messages: `#addtodo Title --msgs ID1 ID2`",
            ]
            if msg_text and not msg_text.startswith(("/", "ax!", "!")):
                guide_lines.insert(1, f"> {msg_text[:300]}")
            if files:
                await message.channel.send("\n".join(guide_lines), files=files, delete_after=30)
            else:
                await message.channel.send("\n".join(guide_lines), delete_after=30)
        return


    # ── todo #N tag — anywhere in any message → reply with info embed ─────────
    import re as _re
    tag_matches = []
    for _tm in _re.finditer(r'(?i)\btodo\s+#(\d+)((?:[\s,\-&+/]|and)*#\d+)*', content):
        tag_matches.append(_tm.group(1))
        extras = _re.findall(r'#(\d+)', _tm.group(0)[len(_tm.group(1))+1:])
        tag_matches.extend(extras)
    if tag_matches:
        # Deduplicate while preserving order
        seen = set()
        unique_ids = []
        for m in tag_matches:
            if m not in seen:
                seen.add(m)
                unique_ids.append(int(m))
        async with aiohttp.ClientSession() as session:
            todos,   _ = await gh_read(session, FILE_TODOS)
            archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
            cfg,     _ = await gh_read(session, FILE_CONFIG)
        all_todos = (todos or []) + (archive or [])
        cfg = cfg or {}
        style = int(cfg.get("todo_style", 1))
        found = []
        not_found = []
        for tid in unique_ids:
            todo = next((t for t in all_todos if t["id"] == tid), None)
            if not todo:
                not_found.append(tid)
            else:
                found.append(todo)
        # Build embeds using the active style
        if found:
            embeds = build_page_embeds(found, 1, 1, style=style)
            delay = _SEND_DELAY.get(style, 0.0)
            first = True
            for e in embeds:
                if first:
                    await message.reply(embed=e, mention_author=False)
                    first = False
                else:
                    await asyncio.sleep(delay)
                    await message.channel.send(embed=e)
        if not_found:
            nf_str = ", ".join(f"#{i}" for i in not_found)
            await message.reply(f"Could not find TODO(s): {nf_str}", mention_author=False)
        await bot.process_commands(message)
        return

    await bot.process_commands(message)

    # ── Auto-assign: scan TODO threads for "I'll handle/fix/take/do this" etc. ─
    if isinstance(message.channel, discord.Thread) and message.channel.parent:
        import re as _re2
        AUTO_PHRASES = [
            r"i'?ll\s+(handle|fix|take\s+care\s+of|do|work\s+on)\s+this",
            r"(taking|on)\s+this",
            r"i'?ll\s+take\s+this",
        ]
        if any(_re2.search(p, content, _re2.IGNORECASE) for p in AUTO_PHRASES):
            # Find which TODO this thread belongs to
            async with aiohttp.ClientSession() as session:
                bid, _ = await gh_read(session, FILE_BOARD_IDS)
                todos2, sha2 = await gh_read_fresh(session, FILE_TODOS)
                cfg2, _ = await gh_read(session, FILE_CONFIG)
            bid   = bid or DEFAULT_BOARD_IDS.copy()
            todos2 = todos2 or []
            cfg2   = cfg2 or {}
            thread_id_str = str(message.channel.id)
            matched_todo = None
            for page in bid.get("pages", []):
                if page.get("thread_id") == thread_id_str and page.get("todo_ids"):
                    tid = page["todo_ids"][0]
                    matched_todo = next((t for t in todos2 if t["id"] == tid), None)
                    break
            if matched_todo and matched_todo.get("status") != "done":
                reactor = message.author

                class _ThreadAssignView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=60)
                        self.done = False

                    @discord.ui.button(label="Yes, assign me", style=discord.ButtonStyle.primary)
                    async def yes_btn(self, intr: discord.Interaction, btn: discord.ui.Button):
                        if intr.user.id != reactor.id:
                            await intr.response.send_message("Not for you.", ephemeral=True); return
                        self.done = True; self.stop()
                        old_id = matched_todo.get("assigned_to_id")
                        matched_todo["assigned_to_id"]   = str(reactor.id)
                        matched_todo["assigned_to_name"] = str(reactor)
                        matched_todo["updated_at"]       = now_iso()
                        if matched_todo["status"] == "todo":
                            matched_todo["status"] = "in_progress"
                        async with aiohttp.ClientSession() as _ss:
                            await gh_write(_ss, FILE_TODOS, todos2, sha2,
                                           f"TODO #{matched_todo['id']} auto-assigned to {reactor}")
                        await intr.response.edit_message(
                            content=f"✅ Assigned **TODO #{matched_todo['id']}** to you.", view=None
                        )
                        if old_id and old_id != str(reactor.id):
                            old_m = message.guild.get_member(int(old_id))
                            if old_m:
                                try:
                                    await old_m.send(
                                        f"You were unassigned from **TODO #{matched_todo['id']}** by "
                                        f"{reactor.mention} in **{message.guild.name}**."
                                    )
                                except Exception:
                                    pass
                        async with aiohttp.ClientSession() as _ss:
                            _cfg3, _ = await gh_read(_ss, FILE_CONFIG)
                        await log_activity(message.guild, _cfg3 or {}, "Auto-assigned (thread)",
                                           matched_todo, reactor, extra=f"→ {reactor.mention}")
                        await update_todo_board(message.guild, cfg2)

                    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
                    async def no_btn(self, intr: discord.Interaction, btn: discord.ui.Button):
                        if intr.user.id != reactor.id:
                            await intr.response.send_message("Not for you.", ephemeral=True); return
                        self.done = True; self.stop()
                        await intr.response.edit_message(content="OK, no assignment made.", view=None)

                    async def on_timeout(self): pass

                await message.reply(
                    f"{reactor.mention} Assign **TODO #{matched_todo['id']}** to you?",
                    view=_ThreadAssignView(),
                    mention_author=False,
                )


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Auto-assign TODO via ✋ reaction on a board card."""
    if str(payload.emoji) != "✋":
        return
    if not payload.guild_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    reactor = guild.get_member(payload.user_id)
    if not reactor or reactor.bot:
        return

    async with aiohttp.ClientSession() as session:
        cfg, _  = await gh_read(session, FILE_CONFIG)
        bid, _  = await gh_read(session, FILE_BOARD_IDS)
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
    cfg   = cfg or {}
    bid   = bid or DEFAULT_BOARD_IDS.copy()
    todos = todos or []

    # Check if this message is a board card
    msg_id_str = str(payload.message_id)
    matched_todo = None
    for page in bid.get("pages", []):
        if page.get("message_id") == msg_id_str and page.get("todo_ids"):
            tid = page["todo_ids"][0]
            matched_todo = next((t for t in todos if t["id"] == tid), None)
            break

    if not matched_todo:
        return  # Not a board card

    # Remove the reaction silently
    try:
        ch = guild.get_channel(payload.channel_id)
        if ch:
            msg = await ch.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, reactor)
    except Exception:
        pass

    if matched_todo.get("status") == "done":
        try:
            await reactor.send(f"TODO #{matched_todo['id']} is already done.")
        except Exception:
            pass
        return

    # Already assigned to self
    if matched_todo.get("assigned_to_id") == str(reactor.id):
        try:
            ch2 = guild.get_channel(payload.channel_id)
            if ch2:
                await ch2.send(f"{reactor.mention} You're already assigned to **TODO #{matched_todo['id']}**.",
                                delete_after=10)
        except Exception:
            pass
        return

    # Already assigned to someone else — show TakeOverView
    if matched_todo.get("assigned_to_id"):
        current_id = matched_todo["assigned_to_id"]
        view = TakeOverView(
            reactor=reactor, todo=matched_todo, todos=todos, sha=sha,
            cfg=cfg, guild=guild
        )
        try:
            ch2 = guild.get_channel(payload.channel_id)
            if ch2:
                await ch2.send(
                    f"{reactor.mention} **TODO #{matched_todo['id']}** is already assigned to <@{current_id}>. "
                    f"Do you want to take it over?",
                    view=view,
                )
        except Exception:
            pass
        return

    # Unassigned — assign directly
    matched_todo["assigned_to_id"]   = str(reactor.id)
    matched_todo["assigned_to_name"] = str(reactor)
    matched_todo["updated_at"]       = now_iso()
    if matched_todo["status"] == "todo":
        matched_todo["status"] = "in_progress"
    async with aiohttp.ClientSession() as session:
        await gh_write(session, FILE_TODOS, todos, sha,
                       f"TODO #{matched_todo['id']} assigned to {reactor} via ✋")
    try:
        ch2 = guild.get_channel(payload.channel_id)
        if ch2:
            await ch2.send(
                f"✅ {reactor.mention} assigned to **TODO #{matched_todo['id']}**.",
                delete_after=15,
            )
    except Exception:
        pass
    await log_activity(guild, cfg, "Assigned (✋ reaction)", matched_todo, reactor,
                       extra=f"→ {reactor.mention}")
    await update_todo_board(guild, cfg)



@bot.tree.command(name="todo_assign", description="Assign a TODO to yourself or someone else")
@app_commands.describe(todo_id="TODO number", user="User to assign (leave blank for yourself)")
async def todo_assign(interaction: discord.Interaction, todo_id: int, user: discord.Member = None):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission to assign TODOs.", ephemeral=True)
        return
    target = user or interaction.user

    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        cfg, _     = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}
    if not todos:
        await interaction.response.send_message("No TODOs found.", ephemeral=True)
        return
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        await interaction.response.send_message(f"TODO #{todo_id} not found.", ephemeral=True)
        return

    # Already assigned to someone else? Show confirmation prompt (non-admin)
    if todo.get("assigned_to_id") and todo["assigned_to_id"] != str(target.id):
        if not interaction.user.guild_permissions.administrator:
            current_id = todo["assigned_to_id"]
            view = ReassignConfirmView(
                author_id           = interaction.user.id,
                todo_id             = todo_id,
                target              = target,
                current_assignee_id = current_id,
                todos               = todos,
                sha                 = sha,
                cfg                 = cfg,
                guild               = interaction.guild,
            )
            await interaction.response.send_message(
                f"⚠️ TODO **#{todo_id}** is already assigned to <@{current_id}>."
                f" Do you want to transfer it to {target.mention}?",
                view=view,
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)
    todo["assigned_to_id"]   = str(target.id)
    todo["assigned_to_name"] = str(target)
    todo["updated_at"]       = now_iso()
    if todo["status"] == "todo":
        todo["status"] = "in_progress"
    async with aiohttp.ClientSession() as session:
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} assigned to {target}")
    await interaction.followup.send(
        f"TODO **#{todo_id}** assigned to {target.mention}. Status set to In Progress.",
        ephemeral=True,
    )
    await log_activity(interaction.guild, cfg, "Assigned", todo,
                       interaction.guild.get_member(interaction.user.id),
                       extra=f"→ {target.mention}")
    await update_todo_board(interaction.guild, cfg)


@bot.tree.command(name="todo_status", description="Update the status of a TODO")
@app_commands.describe(todo_id="TODO number", status="New status")
@app_commands.choices(status=[
    app_commands.Choice(name="To Do",          value="todo"),
    app_commands.Choice(name="In Progress",    value="in_progress"),
    app_commands.Choice(name="Review Needed",  value="review_needed"),
    app_commands.Choice(name="Blocked",        value="blocked"),
    app_commands.Choice(name="Done",           value="done"),
])
async def todo_status(interaction: discord.Interaction, todo_id: int, status: str):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission to update TODOs.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
            return

        todo["updated_at"] = now_iso()

        if status == "done":
            todo["status"]       = "done"
            todo["done_by_id"]   = str(interaction.user.id)
            todo["done_by_name"] = str(interaction.user)
            todo["done_at"]      = now_iso()
            todos = [t for t in todos if t["id"] != todo_id]
            await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} done")
            archive, arch_sha = await gh_read_fresh(session, FILE_TODOS_ARCHIVE)
            archive = archive or []
            archive.append(todo)
            await gh_write(session, FILE_TODOS_ARCHIVE, archive, arch_sha, f"Archive TODO #{todo_id}")
            await interaction.followup.send(f"TODO **#{todo_id}** marked as done and archived.", ephemeral=True)
        else:
            todo["status"] = status
            await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} status -> {status}")
            label = STATUS_LABELS.get(status, status)
            await interaction.followup.send(f"TODO **#{todo_id}** status updated to **{label}**.", ephemeral=True)

    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(interaction.guild, cfg or {}, f"Status → {STATUS_LABELS.get(status, status)}",
                       todo, interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_priority", description="Set priority of a TODO")
@app_commands.describe(todo_id="TODO number", priority="Priority level")
@app_commands.choices(priority=[
    app_commands.Choice(name="Low",    value="low"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="High",   value="high"),
])
async def todo_priority(interaction: discord.Interaction, todo_id: int, priority: str):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
            return
        todo["priority"]   = priority
        todo["updated_at"] = now_iso()
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} priority -> {priority}")

    label = PRIORITY_LABELS.get(priority, priority)
    await interaction.followup.send(f"TODO **#{todo_id}** priority set to **{label}**.", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(interaction.guild, cfg or {}, f"Priority → {label}",
                       todo, interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_delete", description="Delete a TODO you added (or any, if you have todo role/admin)")
@app_commands.describe(todo_id="TODO number")
async def todo_delete(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    is_admin    = interaction.user.guild_permissions.administrator
    has_role    = await has_todo_role(interaction)

    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
            return

        is_author = todo.get("added_by_id") == str(interaction.user.id)
        if not (is_author or has_role or is_admin):
            await interaction.followup.send(
                f"You can only delete TODOs you added. TODO #{todo_id} was added by <@{todo['added_by_id']}>.",
                ephemeral=True,
            )
            return

        todos = [t for t in todos if t["id"] != todo_id]
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} deleted by {interaction.user}")

    await interaction.followup.send(f"TODO **#{todo_id}** deleted.", ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(interaction.guild, cfg or {}, "Deleted", todo,
                       interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_list", description="Show all active TODOs")
async def todo_list(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    todos   = todos   or []
    archive = archive or []
    cfg     = cfg     or {}
    style   = int(cfg.get("todo_style", 1))
    active  = [t for t in todos if t["status"] != "done"]
    if not active:
        await interaction.followup.send(f"No active TODOs. {len(archive)} total completed.")
        return
    await send_todo_embeds(lambda e: interaction.followup.send(embed=e), active, style, title="Active TODOs")


@bot.tree.command(name="todo_mine", description="Show TODOs assigned to you")
async def todo_mine(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
    todos  = todos or []
    cfg    = cfg   or {}
    style  = int(cfg.get("todo_style", 1))
    mine   = [t for t in todos if t.get("assigned_to_id") == str(interaction.user.id)]
    if not mine:
        await interaction.followup.send("No TODOs assigned to you.", ephemeral=True)
        return
    await send_todo_embeds(lambda e: interaction.followup.send(embed=e, ephemeral=True), mine, style, title="Your TODOs")


@bot.tree.command(name="todo_archive", description="View completed TODOs")
async def todo_archive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    archive = archive or []
    cfg     = cfg     or {}
    style   = int(cfg.get("todo_style", 1))
    if not archive:
        await interaction.followup.send("No completed TODOs yet.", ephemeral=True)
        return
    recent = list(reversed(archive[-20:]))
    await send_todo_embeds(lambda e: interaction.followup.send(embed=e, ephemeral=True), recent, style,
                           title=f"Completed TODOs ({len(archive)} total)")


@bot.tree.command(name="todo_info", description="View full details of a TODO")
@app_commands.describe(todo_id="TODO number")
async def todo_info(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    all_todos = (todos or []) + (archive or [])
    todo = next((t for t in all_todos if t["id"] == todo_id), None)
    if not todo:
        await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
        return
    cfg   = cfg or {}
    style = int(cfg.get("todo_style", 1))
    embeds = build_page_embeds([todo], 1, 1, style=style)
    for e in embeds:
        await interaction.followup.send(embed=e, ephemeral=True)

@bot.tree.command(name="todo_unassign", description="Remove assignment from a TODO")
@app_commands.describe(todo_id="TODO number")
async def todo_unassign(interaction: discord.Interaction, todo_id: int):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission to unassign TODOs.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
            return
        if not todo.get("assigned_to_id"):
            await interaction.followup.send(f"TODO #{todo_id} is not assigned to anyone.", ephemeral=True)
            return
        prev = todo["assigned_to_name"]
        todo["assigned_to_id"]   = None
        todo["assigned_to_name"] = None
        todo["updated_at"]       = now_iso()
        if todo["status"] == "in_progress":
            todo["status"] = "todo"
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} unassigned")
    await interaction.followup.send(
        f"TODO **#{todo_id}** unassigned from {prev}. Status reset to To Do.",
        ephemeral=True,
    )
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(interaction.guild, cfg or {}, "Unassigned", todo,
                       interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_filter", description="Filter TODOs by status, priority or assigned user")
@app_commands.describe(
    status="Filter by status",
    priority="Filter by priority",
    user="Filter by assigned user",
)
@app_commands.choices(status=[
    app_commands.Choice(name="To Do",          value="todo"),
    app_commands.Choice(name="In Progress",    value="in_progress"),
    app_commands.Choice(name="Review Needed",  value="review_needed"),
    app_commands.Choice(name="Blocked",        value="blocked"),
])
@app_commands.choices(priority=[
    app_commands.Choice(name="Low",    value="low"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="High",   value="high"),
])
async def todo_filter(
    interaction: discord.Interaction,
    status: str = None,
    priority: str = None,
    user: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
    todos   = todos or []
    cfg     = cfg   or {}
    style   = int(cfg.get("todo_style", 1))
    results = [t for t in todos if t["status"] != "done"]

    if status:
        results = [t for t in results if t["status"] == status]
    if priority:
        results = [t for t in results if t.get("priority") == priority]
    if user:
        results = [t for t in results if t.get("assigned_to_id") == str(user.id)]

    if not results:
        await interaction.followup.send("No TODOs match those filters.", ephemeral=True)
        return

    filters_used = []
    if status:   filters_used.append(STATUS_LABELS.get(status, status))
    if priority: filters_used.append(PRIORITY_LABELS.get(priority, priority))
    if user:     filters_used.append(f"assigned to {user.display_name}")
    filter_str = "  ·  ".join(filters_used) or "All"

    await send_todo_embeds(lambda e: interaction.followup.send(embed=e, ephemeral=True), results[:20], style,
                           title=f"Filtered TODOs — {filter_str} ({len(results)} found)")


# ══════════════════════════════════════════════════════════════════════════════
# TAG COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="todo_tag", description="Add a tag to a TODO")
@app_commands.describe(todo_id="TODO number", tag="Tag to add (e.g. bug, feature, urgent)")
async def todo_tag(interaction: discord.Interaction, todo_id: int, tag: str):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    tag = tag.strip().lower()[:30]
    if not tag:
        await interaction.response.send_message("Tag cannot be empty.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True); return
        tags = todo.setdefault("tags", [])
        if tag in tags:
            await interaction.followup.send(f"Tag `{tag}` already on TODO #{todo_id}.", ephemeral=True); return
        tags.append(tag)
        todo["updated_at"] = now_iso()
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} tag +{tag}")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await interaction.followup.send(f"Added tag `{tag}` to TODO **#{todo_id}**.", ephemeral=True)
    await log_activity(interaction.guild, cfg or {}, f"Tag Added: `{tag}`", todo,
                       interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_untag", description="Remove a tag from a TODO")
@app_commands.describe(todo_id="TODO number", tag="Tag to remove")
async def todo_untag(interaction: discord.Interaction, todo_id: int, tag: str):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    tag = tag.strip().lower()[:30]
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True); return
        tags = todo.get("tags", [])
        if tag not in tags:
            await interaction.followup.send(f"Tag `{tag}` not on TODO #{todo_id}.", ephemeral=True); return
        tags.remove(tag)
        todo["updated_at"] = now_iso()
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} tag -{tag}")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await interaction.followup.send(f"Removed tag `{tag}` from TODO **#{todo_id}**.", ephemeral=True)
    await log_activity(interaction.guild, cfg or {}, f"Tag Removed: `{tag}`", todo,
                       interaction.guild.get_member(interaction.user.id))
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_filter_tag", description="Filter TODOs by tag")
@app_commands.describe(tag="Tag to filter by (e.g. bug, feature, urgent)")
async def todo_filter_tag(interaction: discord.Interaction, tag: str):
    await interaction.response.defer(ephemeral=True)
    tag = tag.strip().lower()
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
    todos   = todos or []
    cfg     = cfg   or {}
    style   = int(cfg.get("todo_style", 1))
    results = [t for t in todos if t.get("status") != "done" and tag in t.get("tags", [])]
    if not results:
        await interaction.followup.send(f"No active TODOs with tag `{tag}`.", ephemeral=True); return
    await send_todo_embeds(lambda e: interaction.followup.send(embed=e, ephemeral=True), results, style,
                           title=f"TODOs tagged `{tag}` ({len(results)} found)")


# ══════════════════════════════════════════════════════════════════════════════
# THREAD COMMAND  (styles 1-4: open/jump to discussion thread)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="todo_thread", description="Open or jump to the discussion thread for a TODO")
@app_commands.describe(todo_id="TODO number")
async def todo_thread(interaction: discord.Interaction, todo_id: int):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
        bid,   _ = await gh_read(session, FILE_BOARD_IDS)
    todos = todos or []
    cfg   = cfg   or {}
    bid   = bid   or DEFAULT_BOARD_IDS.copy()

    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True); return

    style = int(cfg.get("todo_style", 1))

    # Styles 5/6 — thread is on the board card itself
    if style in (5, 6):
        for page in bid.get("pages", []):
            if todo_id in page.get("todo_ids", []) and page.get("thread_id"):
                thread = interaction.guild.get_thread(int(page["thread_id"]))
                if thread:
                    await interaction.followup.send(
                        f"Jump to the thread for **TODO #{todo_id}**: {thread.mention}", ephemeral=True
                    ); return
        await interaction.followup.send(
            f"No thread found for TODO #{todo_id}. Try `/todo_refresh` to rebuild the board.",
            ephemeral=True,
        ); return

    # Styles 1-4 — get or create thread in thread_channel
    thread = await _get_or_create_thread_for_todo(interaction.guild, cfg, todo_id, todo["title"])
    if thread:
        await interaction.followup.send(
            f"Discussion thread for **TODO #{todo_id}**: {thread.mention}", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "No thread channel configured. Ask an admin to run `/setup_thread_channel`.",
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# STYLE COMMAND
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="todo_style", description="Change the TODO board card style (Admin)")
@app_commands.describe(style="Card style to use for the live board")
@app_commands.choices(style=[
    app_commands.Choice(name="Style 1 — Clean (top accent bar)",           value=1),
    app_commands.Choice(name="Style 2 — Sidebar (progress bars)",          value=2),
    app_commands.Choice(name="Style 3 — Minimal (compact, no icons)",      value=3),
    app_commands.Choice(name="Style 4 — Detailed (AI summary + quote)",    value=4),
    app_commands.Choice(name="Style 5 — FAQ (one embed per TODO)",         value=5),
    app_commands.Choice(name="Style 6 — Full Detail (no truncation)",      value=6),
])
@app_commands.default_permissions(administrator=True)
async def todo_style(interaction: discord.Interaction, style: int):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        current = int(cfg.get("todo_style", 1))
        if current == style:
            await interaction.followup.send(
                f"Board is already using Style {style}.", ephemeral=True
            )
            return
        cfg["todo_style"] = style
        await gh_write(session, FILE_CONFIG, cfg, sha, f"TODO board style -> {style}")

    await interaction.followup.send(
        f"Board style changed to **Style {style}**. Rebuilding the board now...",
        ephemeral=True,
    )
    await update_todo_board(interaction.guild, cfg)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup_todo_channel", description="Set the TODO board channel (Admin)")
@app_commands.describe(channel="Channel for the live TODO board")
@app_commands.default_permissions(administrator=True)
async def setup_todo_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["todo_channel"] = str(channel.id)
        await gh_write(session, FILE_CONFIG, cfg, sha, "Setup: todo channel")
    await interaction.followup.send(f"TODO board channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup_todo_roles", description="Add/remove a role that can manage TODOs (Admin)")
@app_commands.describe(role="Role to toggle")
@app_commands.default_permissions(administrator=True)
async def setup_todo_roles(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        roles = cfg.get("todo_roles", [])
        if str(role.id) in roles:
            roles.remove(str(role.id))
            msg = f"Removed {role.mention} from TODO managers."
        else:
            roles.append(str(role.id))
            msg = f"Added {role.mention} as a TODO manager."
        cfg["todo_roles"] = roles
        await gh_write(session, FILE_CONFIG, cfg, sha, "TODO roles updated")
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="setup_log_channel", description="Set the activity log channel (Admin)")
@app_commands.describe(channel="Channel where all activity is logged")
@app_commands.default_permissions(administrator=True)
async def setup_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["log_channel"] = str(channel.id)
        await gh_write(session, FILE_CONFIG, cfg, sha, "Setup: log channel")
    await interaction.followup.send(f"Activity log channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setup_reminder", description="Configure reminder schedule (Admin)")
@app_commands.describe(
    days="Ping assigned user after this many days of no activity (default 3)",
    time="UTC time to send reminders HH:MM (default 09:00)",
    channel="Channel for reminder pings (leave blank to DM the assigned user)",
)
@app_commands.default_permissions(administrator=True)
async def setup_reminder(
    interaction: discord.Interaction,
    days: int = 3,
    time: str = "09:00",
    channel: discord.TextChannel = None,
):
    await interaction.response.defer(ephemeral=True)
    import re as _re
    if not _re.match(r"^\d{2}:\d{2}$", time):
        await interaction.followup.send("Time must be in HH:MM format (UTC), e.g. `09:00`.", ephemeral=True)
        return
    if days < 1 or days > 365:
        await interaction.followup.send("Days must be between 1 and 365.", ephemeral=True)
        return
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["reminder_days"]    = days
        cfg["reminder_time"]    = time
        cfg["reminder_channel"] = str(channel.id) if channel else None
        await gh_write(session, FILE_CONFIG, cfg, sha, "Setup: reminders")
    dest = channel.mention if channel else "DM to assigned user"
    await interaction.followup.send(
        f"Reminders set: ping after **{days} days** of no activity at **{time} UTC** → {dest}.",
        ephemeral=True,
    )


@bot.tree.command(name="setup_thread_channel", description="Set channel for TODO discussion threads (styles 1-4) (Admin)")
@app_commands.describe(channel="Channel where per-TODO discussion threads will be created")
@app_commands.default_permissions(administrator=True)
async def setup_thread_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["thread_channel"] = str(channel.id)
        await gh_write(session, FILE_CONFIG, cfg, sha, "Setup: thread channel")
    await interaction.followup.send(
        f"Thread channel set to {channel.mention}. Use `/todo_thread <id>` to open a discussion.",
        ephemeral=True,
    )


@bot.tree.command(name="config_view", description="View current bot configuration (Admin)")
@app_commands.default_permissions(administrator=True)
async def config_view(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}

    def ch_mention(ch_id):
        if not ch_id:
            return "Not set"
        ch = interaction.guild.get_channel(int(ch_id))
        return ch.mention if ch else f"<#{ch_id}>"

    e = discord.Embed(title="Bot Configuration", color=0x5865F2)
    e.add_field(name="TODO Board",      value=ch_mention(cfg.get("todo_channel")),    inline=True)
    e.add_field(name="Style",           value=str(cfg.get("todo_style", 1)),          inline=True)
    e.add_field(name="Prefix",          value=f"`{cfg.get('prefix', 'ax!')}`",        inline=True)
    e.add_field(name="Activity Log",    value=ch_mention(cfg.get("log_channel")),     inline=True)
    e.add_field(name="Thread Channel",  value=ch_mention(cfg.get("thread_channel")), inline=True)
    reminder_dest = ch_mention(cfg.get("reminder_channel")) if cfg.get("reminder_channel") else "DM to assignee"
    e.add_field(
        name="Reminders",
        value=f"Every **{cfg.get('reminder_days', 3)}** days at **{cfg.get('reminder_time', '09:00')} UTC** → {reminder_dest}",
        inline=False,
    )
    roles = cfg.get("todo_roles", [])
    e.add_field(name="TODO Roles", value=(", ".join(f"<@&{r}>" for r in roles) or "None"), inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)




# ══════════════════════════════════════════════════════════════════════════════
# SETPREFIX — slash + prefix
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setprefix", description="Change the bot command prefix (Admin)")
@app_commands.describe(new_prefix="New prefix to use, e.g. ! or ax!")
@app_commands.default_permissions(administrator=True)
async def setprefix_slash(interaction: discord.Interaction, new_prefix: str):
    await interaction.response.defer(ephemeral=True)
    new_prefix = new_prefix.strip()
    if not new_prefix or len(new_prefix) > 10:
        await interaction.followup.send("Prefix must be 1–10 characters.", ephemeral=True)
        return
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["prefix"] = new_prefix
        await gh_write(session, FILE_CONFIG, cfg, sha, f"Prefix changed to {new_prefix}")
    await interaction.followup.send(f"Prefix updated to `{new_prefix}`.", ephemeral=True)

@bot.command(name="setprefix")
async def setprefix_prefix(ctx, new_prefix: str = None):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Only admins can change the prefix.", delete_after=10)
        return
    if not new_prefix or len(new_prefix.strip()) > 10:
        await ctx.send("Prefix must be 1–10 characters.", delete_after=10)
        return
    new_prefix = new_prefix.strip()
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["prefix"] = new_prefix
        await gh_write(session, FILE_CONFIG, cfg, sha, f"Prefix changed to {new_prefix}")
    await ctx.send(f"Prefix updated to `{new_prefix}`.")

# ══════════════════════════════════════════════════════════════════════════════
# HELP — slash + prefix
# ══════════════════════════════════════════════════════════════════════════════

def _build_help_embed(prefix: str) -> discord.Embed:
    e = discord.Embed(
        title="AnymeX TODO Bot — Help",
        description=(
            f"Use slash commands `/` or prefix `{prefix}` (no underscores).\n"
            f"Prefix commands require **Admin** or **TODO Role**.\n"
            f"Change prefix with `{prefix}setprefix <new>` or `/setprefix`."
        ),
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="​", value="**── TODO Management ──**", inline=False)
    e.add_field(
        name=f"`/todo_style` · `{prefix}todostyle <1-6>`",
        value="Change the card style for the entire TODO board.\n`1` Clean · `2` Sidebar · `3` Minimal · `4` Detailed · `5` FAQ · `6` Full Detail",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_list` · `{prefix}todolist`",
        value="Show all active TODOs (paginated).",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_mine` · `{prefix}todomine`",
        value="Show TODOs assigned to you.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_filter` · `{prefix}todofilter [status] [priority] [user]`",
        value="Filter TODOs by status, priority or assigned user.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_info <id>` · `{prefix}todoinfo <id>`",
        value="View full details of a specific TODO.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_archive` · `{prefix}todoarchive`",
        value="View the 10 most recently completed TODOs.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_assign <id> [user]` · `{prefix}todoassign <id> [user]`",
        value="Assign a TODO to yourself or another user.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_unassign <id>` · `{prefix}todounassign <id>`",
        value="Remove assignment from a TODO.",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_status <id> <status>` · `{prefix}todostatus <id> <status>`",
        value="Update TODO status: `todo` · `in_progress` · `review_needed` · `blocked` · `done`",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_priority <id> <priority>` · `{prefix}todopriority <id> <priority>`",
        value="Set TODO priority: `low` · `medium` · `high`",
        inline=False,
    )
    e.add_field(
        name=f"`/todo_delete <id>` · `{prefix}tododelete <id>`",
        value="Delete a TODO. You can only delete ones you added (admins can delete any).",
        inline=False,
    )
    e.add_field(name="​", value="**── Tags ──**", inline=False)
    e.add_field(
        name="`/todo_tag <id> <tag>`",
        value="Add a tag to a TODO (e.g. `bug`, `feature`, `urgent`).",
        inline=False,
    )
    e.add_field(
        name="`/todo_untag <id> <tag>`",
        value="Remove a tag from a TODO.",
        inline=False,
    )
    e.add_field(
        name="`/todo_filter_tag <tag>`",
        value="Show all active TODOs with a specific tag.",
        inline=False,
    )
    e.add_field(name="​", value="**── Threads & Auto-assign ──**", inline=False)
    e.add_field(
        name="`/todo_thread <id>`",
        value=(
            "Open or jump to the discussion thread for a TODO.\n"
            "Styles 5/6: thread is on the board card itself (auto-created).\n"
            "Styles 1-4: thread is created in the configured thread channel."
        ),
        inline=False,
    )
    e.add_field(
        name="✋ Reaction on board card",
        value=(
            "React ✋ on any board card to assign that TODO to yourself.\n"
            "If already assigned to someone else, you'll be asked to confirm a takeover."
        ),
        inline=False,
    )
    e.add_field(
        name="Thread auto-assign",
        value='Say "I\'ll handle this", "taking this", "on it" etc. in a TODO\'s thread to trigger auto-assign.',
        inline=False,
    )
    e.add_field(name="​", value="**── Adding TODOs ──**", inline=False)
    e.add_field(
        name="`#addtodo <title>`",
        value=(
            "Add a TODO by typing in any channel.\n"
            "Reply to a message + `#addtodo` to auto-generate title with AI.\n"
            "Combine messages: `#addtodo Title --msgs ID1 ID2`"
        ),
        inline=False,
    )
    e.add_field(name="​", value="**── Setup & Config ──**", inline=False)
    e.add_field(
        name=f"`/setup_todo_channel` · `{prefix}setuptodochannel <#channel>`",
        value="Set the channel where the live TODO board is posted. (Admin)",
        inline=False,
    )
    e.add_field(
        name=f"`/setup_todo_roles` · `{prefix}setuptodoroles <@role>`",
        value="Toggle a role's access to manage TODOs. (Admin)",
        inline=False,
    )
    e.add_field(
        name="`/setup_log_channel <#channel>`",
        value="Set the activity log channel — every change posts an embed there. (Admin)",
        inline=False,
    )
    e.add_field(
        name="`/setup_reminder [days] [time] [channel]`",
        value="Configure reminder schedule. Default: 3 days, 09:00 UTC, DM to assignee. (Admin)",
        inline=False,
    )
    e.add_field(
        name="`/setup_thread_channel <#channel>`",
        value="Set the channel for TODO discussion threads (styles 1-4). (Admin)",
        inline=False,
    )
    e.add_field(
        name=f"`/config_view` · `{prefix}configview`",
        value="View current bot config: channels, roles, style, reminders. (Admin)",
        inline=False,
    )
    e.add_field(
        name=f"`/setprefix <prefix>` · `{prefix}setprefix <prefix>`",
        value="Change the bot's command prefix. (Admin)",
        inline=False,
    )
    e.set_footer(text="AnymeX TODO Bot")
    return e

@bot.tree.command(name="help", description="Show all bot commands and usage")
async def help_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    prefix = (cfg or {}).get("prefix", "ax!")
    await interaction.followup.send(embed=_build_help_embed(prefix), ephemeral=True)

@bot.command(name="help")
async def help_prefix(ctx):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("You need the TODO role or admin to use this.", delete_after=10)
        return
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    prefix = (cfg or {}).get("prefix", "ax!")
    await ctx.send(embed=_build_help_embed(prefix))

# ══════════════════════════════════════════════════════════════════════════════
# PREFIX VERSIONS OF ALL TODO COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="todostyle")
async def p_todostyle(ctx, style: int = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if style not in (1, 2, 3, 4, 5, 6):
        await ctx.send(
            "Usage: `todostyle <1-6>`\n"
            "`1` Clean · `2` Sidebar · `3` Minimal · `4` Detailed · `5` FAQ · `6` Full Detail",
            delete_after=15,
        )
        return
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        if int(cfg.get("todo_style", 1)) == style:
            await ctx.send(f"Board is already using Style {style}."); return
        cfg["todo_style"] = style
        await gh_write(session, FILE_CONFIG, cfg, sha, f"TODO board style -> {style}")
    msg = await ctx.send(f"Style changed to **Style {style}**. Rebuilding board...")
    await update_todo_board(ctx.guild, cfg)
    await msg.edit(content=f"Style changed to **Style {style}**. Board updated.")

@bot.command(name="todolist")
async def p_todolist(ctx):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    todos = todos or []; archive = archive or []; cfg = cfg or {}
    style = int(cfg.get("todo_style", 1))
    active = [t for t in todos if t["status"] != "done"]
    if not active:
        await ctx.send(f"No active TODOs. {len(archive)} total completed."); return
    await send_todo_embeds(lambda e: ctx.send(embed=e), active, style, title="Active TODOs")

@bot.command(name="todomine")
async def p_todomine(ctx):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
    todos = todos or []; cfg = cfg or {}
    style = int(cfg.get("todo_style", 1))
    mine = [t for t in todos if t.get("assigned_to_id") == str(ctx.author.id)]
    if not mine:
        await ctx.send("No TODOs assigned to you."); return
    await send_todo_embeds(lambda e: ctx.send(embed=e), mine, style, title="Your TODOs")

@bot.command(name="todofilter")
async def p_todofilter(ctx, *, args: str = ""):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
        cfg,   _ = await gh_read(session, FILE_CONFIG)
    todos = todos or []; cfg = cfg or {}
    style = int(cfg.get("todo_style", 1))
    results = [t for t in todos if t["status"] != "done"]
    for word in args.lower().split():
        if word in STATUS_LABELS:
            results = [t for t in results if t["status"] == word]
        elif word in PRIORITY_LABELS:
            results = [t for t in results if t.get("priority") == word]
    if not results:
        await ctx.send("No TODOs match those filters."); return
    await send_todo_embeds(lambda e: ctx.send(embed=e), results[:20], style, title=f"Filtered TODOs ({len(results)} found)")

@bot.command(name="todoinfo")
async def p_todoinfo(ctx, todo_id: int = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if not todo_id:
        await ctx.send("Usage: `todoinfo <id>`", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    all_todos = (todos or []) + (archive or [])
    todo = next((t for t in all_todos if t["id"] == todo_id), None)
    if not todo:
        await ctx.send(f"TODO #{todo_id} not found."); return
    cfg   = cfg or {}
    style = int(cfg.get("todo_style", 1))
    embeds = build_page_embeds([todo], 1, 1, style=style)
    for e in embeds:
        await ctx.send(embed=e)

@bot.command(name="todoarchive")
async def p_todoarchive(ctx):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
        cfg,     _ = await gh_read(session, FILE_CONFIG)
    archive = archive or []; cfg = cfg or {}
    style = int(cfg.get("todo_style", 1))
    if not archive:
        await ctx.send("No completed TODOs yet."); return
    recent = list(reversed(archive[-20:]))
    await send_todo_embeds(lambda e: ctx.send(embed=e), recent, style, title=f"Completed TODOs ({len(archive)} total)")

@bot.command(name="todoassign")
async def p_todoassign(ctx, todo_id: int = None, user: discord.Member = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if not todo_id:
        await ctx.send("Usage: `todoassign <id> [user]`", delete_after=10); return
    target = user or ctx.author
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        cfg, _     = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}
    if not todos:
        await ctx.send("No TODOs found."); return
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        await ctx.send(f"TODO #{todo_id} not found."); return

    # Already assigned to someone else? Ask for confirmation first
    if todo.get("assigned_to_id") and todo["assigned_to_id"] != str(ctx.author.id):
        if not ctx.author.guild_permissions.administrator:
            view = ReassignConfirmView(
                author_id           = ctx.author.id,
                todo_id             = todo_id,
                target              = target,
                current_assignee_id = todo["assigned_to_id"],
                todos               = todos,
                sha                 = sha,
                cfg                 = cfg,
                guild               = ctx.guild,
            )
            current_id = todo['assigned_to_id']
            await ctx.send(
                f"⚠️ TODO **#{todo_id}** is already assigned to <@{current_id}>."
                f" Do you want to transfer it to {target.mention}?",
                view=view,
            )
            return

    todo["assigned_to_id"]   = str(target.id)
    todo["assigned_to_name"] = str(target)
    todo["updated_at"]       = now_iso()
    if todo["status"] == "todo":
        todo["status"] = "in_progress"
    async with aiohttp.ClientSession() as session:
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} assigned to {target}")
    await ctx.send(f"TODO **#{todo_id}** assigned to {target.mention}.")
    await log_activity(ctx.guild, cfg, "Assigned", todo, ctx.author, extra=f"→ {target.mention}")
    await update_todo_board(ctx.guild, cfg)

@bot.command(name="todounassign")
async def p_todounassign(ctx, todo_id: int = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if not todo_id:
        await ctx.send("Usage: `todounassign <id>`", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await ctx.send(f"TODO #{todo_id} not found."); return
        if not todo.get("assigned_to_id"):
            await ctx.send(f"TODO #{todo_id} is not assigned to anyone."); return
        prev = todo["assigned_to_name"]
        todo["assigned_to_id"]   = None
        todo["assigned_to_name"] = None
        todo["updated_at"]       = now_iso()
        if todo["status"] == "in_progress":
            todo["status"] = "todo"
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} unassigned")
    await ctx.send(f"TODO **#{todo_id}** unassigned from {prev}.")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(ctx.guild, cfg or {}, "Unassigned", todo, ctx.author)
    await update_todo_board(ctx.guild, cfg or {})

@bot.command(name="todostatus")
async def p_todostatus(ctx, todo_id: int = None, status: str = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    valid = ["todo", "in_progress", "review_needed", "blocked", "done"]
    if not todo_id or status not in valid:
        await ctx.send(f"Usage: `todostatus <id> <{'|'.join(valid)}>`", delete_after=15); return
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await ctx.send(f"TODO #{todo_id} not found."); return
        if status == "done":
            todo["done_by_id"]   = str(ctx.author.id)
            todo["done_by_name"] = str(ctx.author)
            todo["done_at"]      = now_iso()
            todos = [t for t in todos if t["id"] != todo_id]
            await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} done")
            archive, arch_sha = await gh_read_fresh(session, FILE_TODOS_ARCHIVE)
            archive = archive or []
            archive.append(todo)
            await gh_write(session, FILE_TODOS_ARCHIVE, archive, arch_sha, f"Archive TODO #{todo_id}")
            await ctx.send(f"TODO **#{todo_id}** marked as done and archived.")
        else:
            todo["status"] = status
            await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} status -> {status}")
            await ctx.send(f"TODO **#{todo_id}** status updated to **{STATUS_LABELS.get(status, status)}**.")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(ctx.guild, cfg or {}, f"Status → {STATUS_LABELS.get(status, status)}", todo, ctx.author)
    await update_todo_board(ctx.guild, cfg or {})

@bot.command(name="todopriority")
async def p_todopriority(ctx, todo_id: int = None, priority: str = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if not todo_id or priority not in ("low", "medium", "high"):
        await ctx.send("Usage: `todopriority <id> <low|medium|high>`", delete_after=15); return
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await ctx.send(f"TODO #{todo_id} not found."); return
        todo["priority"]   = priority
        todo["updated_at"] = now_iso()
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} priority -> {priority}")
    await ctx.send(f"TODO **#{todo_id}** priority set to **{PRIORITY_LABELS.get(priority, priority)}**.")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await log_activity(ctx.guild, cfg or {}, f"Priority → {PRIORITY_LABELS.get(priority, priority)}", todo, ctx.author)
    await update_todo_board(ctx.guild, cfg or {})

@bot.command(name="tododelete")
async def p_tododelete(ctx, todo_id: int = None):
    if not await has_todo_role_msg(ctx.message):
        await ctx.send("No permission.", delete_after=10); return
    if not todo_id:
        await ctx.send("Usage: `tododelete <id>`", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
        if not todo:
            await ctx.send(f"TODO #{todo_id} not found."); return
        is_author = todo.get("added_by_id") == str(ctx.author.id)
        is_admin  = ctx.author.guild_permissions.administrator
        has_role  = await has_todo_role_msg(ctx.message)
        if not (is_author or has_role or is_admin):
            await ctx.send(f"You can only delete TODOs you added."); return
        todos = [t for t in todos if t["id"] != todo_id]
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} deleted by {ctx.author}")
    await ctx.send(f"TODO **#{todo_id}** deleted.")
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await update_todo_board(ctx.guild, cfg or {})

@bot.command(name="setuptodochannel")
async def p_setuptodochannel(ctx, channel: discord.TextChannel = None):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admin only.", delete_after=10); return
    if not channel:
        await ctx.send("Usage: `setuptodochannel <#channel>`", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        cfg["todo_channel"] = str(channel.id)
        await gh_write(session, FILE_CONFIG, cfg, sha, "Setup: todo channel")
    await ctx.send(f"TODO board channel set to {channel.mention}.")

@bot.command(name="setuptodoroles")
async def p_setuptodoroles(ctx, role: discord.Role = None):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admin only.", delete_after=10); return
    if not role:
        await ctx.send("Usage: `setuptodoroles <@role>`", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        cfg, sha = await gh_read_fresh(session, FILE_CONFIG)
        cfg = cfg or DEFAULT_CONFIG.copy()
        roles = cfg.get("todo_roles", [])
        if str(role.id) in roles:
            roles.remove(str(role.id))
            msg = f"Removed {role.mention} from TODO managers."
        else:
            roles.append(str(role.id))
            msg = f"Added {role.mention} as a TODO manager."
        cfg["todo_roles"] = roles
        await gh_write(session, FILE_CONFIG, cfg, sha, "TODO roles updated")
    await ctx.send(msg)

@bot.command(name="configview")
async def p_configview(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admin only.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    cfg = cfg or {}
    def ch_mention(ch_id):
        if not ch_id: return "Not set"
        ch = ctx.guild.get_channel(int(ch_id))
        return ch.mention if ch else f"<#{ch_id}>"
    e = discord.Embed(title="Bot Configuration", color=0x5865F2)
    e.add_field(name="TODO Board",  value=ch_mention(cfg.get("todo_channel")), inline=True)
    e.add_field(name="Style",       value=str(cfg.get("todo_style", 1)),       inline=True)
    e.add_field(name="Prefix",      value=f"`{cfg.get('prefix', 'ax!')}`",     inline=True)
    roles = cfg.get("todo_roles", [])
    e.add_field(name="TODO Roles",  value=(", ".join(f"<@&{r}>" for r in roles) or "None"), inline=False)
    await ctx.send(embed=e)

@bot.command(name="todorefresh")
async def p_todorefresh(ctx):
    """Wipe the todo channel and repost all bot cards cleanly. Admin only."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admin only.", delete_after=10); return
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read_fresh(session, FILE_CONFIG)
    cfg = cfg or {}
    todo_ch_id = cfg.get("todo_channel")
    if not todo_ch_id:
        await ctx.send("No TODO channel configured. Use `setuptodochannel` first.", delete_after=10); return
    ch = ctx.guild.get_channel(int(todo_ch_id))
    if not ch:
        await ctx.send("TODO channel not found.", delete_after=10); return
    if str(ctx.channel.id) != str(todo_ch_id):
        await ctx.send(f"Refreshing {ch.mention}...")
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read_fresh(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    todos   = todos   or []
    archive = archive or []
    active  = [t for t in todos if t["status"] != "done"]
    style   = int(cfg.get("todo_style", 1))
    stats_embed = build_stats_embed(active, len(archive), style=style)
    wanted      = _build_wanted_pages(active, archive, style)
    await _refresh_todo_board(ch, stats_embed, wanted, style=style)
    if str(ctx.channel.id) != str(todo_ch_id):
        await ctx.send(f"✅ {ch.mention} refreshed — all old messages wiped, board reposted.")


@bot.tree.command(name="todo_refresh", description="Wipe the TODO channel and repost everything fresh (admin only)")
async def todo_refresh(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admin only.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read_fresh(session, FILE_CONFIG)
    cfg = cfg or {}
    todo_ch_id = cfg.get("todo_channel")
    if not todo_ch_id:
        await interaction.followup.send("No TODO channel configured.", ephemeral=True); return
    ch = interaction.guild.get_channel(int(todo_ch_id))
    if not ch:
        await interaction.followup.send("TODO channel not found.", ephemeral=True); return
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read_fresh(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    todos   = todos   or []
    archive = archive or []
    active  = [t for t in todos if t["status"] != "done"]
    style   = int(cfg.get("todo_style", 1))
    stats_embed = build_stats_embed(active, len(archive), style=style)
    wanted      = _build_wanted_pages(active, archive, style)
    await _refresh_todo_board(ch, stats_embed, wanted, style=style)
    await interaction.followup.send(f"✅ {ch.mention} refreshed — all messages wiped, board reposted.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    await start_health_server()
    _ph = os.environ.get("PROXY_HOST")
    _pp = os.environ.get("PROXY_PORT")
    _pu = os.environ.get("PROXY_USER")
    _pw = os.environ.get("PROXY_PASS")
    if all([_ph, _pp, _pu, _pw]):
        proxy_url = f"http://{_pu}:{_pw}@{_ph}:{_pp}"
        print(f"Using proxy: {_ph}:{_pp}")
        bot.http.proxy = proxy_url
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
