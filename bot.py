import discord
from discord import app_commands
from discord.ext import commands
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

TODOS_PER_PAGE = 10

# Default config
DEFAULT_CONFIG = {
    "todo_channel":           None,
    "todo_roles":             [],
    "todo_stats_message_id":  None,
    "todo_page_message_ids":  [],
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
PRIORITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}

def build_stats_embed(todos: list, archive_count: int) -> discord.Embed:
    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    e = discord.Embed(
        title="AnymeX — TODO Board",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="To Do",         value=str(counts["todo"]),          inline=True)
    e.add_field(name="In Progress",   value=str(counts["in_progress"]),   inline=True)
    e.add_field(name="Review Needed", value=str(counts["review_needed"]), inline=True)
    e.add_field(name="Blocked",       value=str(counts["blocked"]),       inline=True)
    e.add_field(name="Total Done",    value=str(archive_count),           inline=True)
    e.add_field(name="Active",        value=str(len(todos)),              inline=True)
    e.set_footer(text="Last updated")
    return e

def build_todo_card(t: dict) -> tuple[str, str]:
    """Returns (name, value) for an embed field."""
    status   = t.get("status", "todo")
    label    = STATUS_LABELS.get(status, status)
    priority = PRIORITY_LABELS.get(t.get("priority", "medium"), "Medium")
    asgn     = f"<@{t['assigned_to_id']}>" if t.get("assigned_to_id") else "Unassigned"
    added    = f"<@{t['added_by_id']}>"
    name     = f"#{t['id']} — {t['title'][:65]}"
    # Show AI description if present, otherwise show source text snippet
    desc = t.get("ai_description") or ""
    desc_line = f"\n> {desc[:120]}" if desc else ""
    auto_tag  = " ✦ AI title" if t.get("auto_generated") else ""
    value    = (
        f"`{label}`  ·  {priority}{auto_tag}{desc_line}\n"
        f"Assigned: {asgn}  ·  Added by: {added}"
    )
    return name, value

def build_page_embed(todos: list, page: int, total_pages: int) -> discord.Embed:
    e = discord.Embed(
        title=f"Active TODOs — Page {page}/{total_pages}",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for t in todos:
        name, value = build_todo_card(t)
        e.add_field(name=name, value=value, inline=False)
    if not todos:
        e.description = "No active TODOs on this page."
    e.set_footer(text=f"Page {page} of {total_pages}  ·  Last updated")
    return e

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
bot = commands.Bot(command_prefix="ax!", intents=intents, help_command=None)

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

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

async def update_todo_board(guild: discord.Guild, cfg: dict):
    todo_ch_id = cfg.get("todo_channel")
    if not todo_ch_id:
        return
    ch = guild.get_channel(int(todo_ch_id))
    if not ch:
        return

    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read_fresh(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    todos   = todos   or []
    archive = archive or []
    active  = [t for t in todos if t["status"] != "done"]
    pages   = [active[i:i+TODOS_PER_PAGE] for i in range(0, max(len(active), 1), TODOS_PER_PAGE)]
    total_pages = len(pages)
    cfg_dirty   = False

    # Stats message
    stats_embed  = build_stats_embed(active, len(archive))
    stats_msg_id = cfg.get("todo_stats_message_id")
    if stats_msg_id:
        try:
            stats_msg = await ch.fetch_message(int(stats_msg_id))
            await stats_msg.edit(embed=stats_embed)
        except Exception:
            stats_msg = await ch.send(embed=stats_embed)
            cfg["todo_stats_message_id"] = str(stats_msg.id)
            cfg_dirty = True
    else:
        stats_msg = await ch.send(embed=stats_embed)
        cfg["todo_stats_message_id"] = str(stats_msg.id)
        cfg_dirty = True

    # Page messages
    page_ids: list = list(cfg.get("todo_page_message_ids") or [])
    for i, page_todos in enumerate(pages):
        embed = build_page_embed(page_todos, i + 1, total_pages)
        if i < len(page_ids):
            try:
                msg = await ch.fetch_message(int(page_ids[i]))
                await msg.edit(embed=embed)
            except Exception:
                msg = await ch.send(embed=embed)
                page_ids[i] = str(msg.id)
                cfg_dirty = True
        else:
            msg = await ch.send(embed=embed)
            page_ids.append(str(msg.id))
            cfg_dirty = True

    while len(page_ids) > total_pages:
        old_id = page_ids.pop()
        try:
            old_msg = await ch.fetch_message(int(old_id))
            await old_msg.delete()
        except Exception:
            pass
        cfg_dirty = True

    if cfg_dirty or page_ids != cfg.get("todo_page_message_ids"):
        cfg["todo_page_message_ids"] = page_ids
        async with aiohttp.ClientSession() as session:
            cfg2, sha = await gh_read_fresh(session, FILE_CONFIG)
            cfg2 = cfg2 or {}
            cfg2["todo_stats_message_id"] = cfg.get("todo_stats_message_id")
            cfg2["todo_page_message_ids"] = page_ids
            await gh_write(session, FILE_CONFIG, cfg2, sha, "Update todo board message IDs")

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
            todo_id = len(todos) + 1
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
                "added_by_id":          str(interaction.user.id),
                "added_by_name":        str(interaction.user),
                "assigned_to_id":       None,
                "assigned_to_name":     None,
                "created_at":           now_iso(),
                "updated_at":           now_iso(),
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

    # ── TODO channel: message without #addtodo → guide the user ───────────────
    if todo_ch_id and str(message.channel.id) == str(todo_ch_id):
        if content and not content.startswith("/"):
            # Save message details before deleting
            author_mention = message.author.mention
            author_name    = str(message.author)
            jump_link      = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
            msg_text       = message.content or ""
            attachments    = message.attachments[:]

            try:
                await message.delete()
            except Exception:
                pass

            # Copy full message to channel as a log (preserving author, content, attachments, jump link)
            copy_lines = [
                f"📋 **Message from {author_mention}** (copied from TODO channel)",
                f"> {msg_text[:1000]}" if msg_text else "",
                f"[Jump to original]({jump_link})" if jump_link else "",
            ]
            copy_content = "\n".join(l for l in copy_lines if l)

            # Re-attach any files
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

            # Send copied message back in same channel
            if files:
                await message.channel.send(copy_content, files=files, delete_after=30)
            else:
                await message.channel.send(copy_content, delete_after=30)

            # Send usage guide
            await message.channel.send(
                f"{author_mention} This channel is for the TODO board only.\n"
                f"To add a TODO use: `#addtodo Your title here`\n"
                f"Or reply to any message with `#addtodo` and I'll auto-generate a title using AI.\n"
                f"To combine multiple messages: `#addtodo Title --msgs ID1 ID2`",
                delete_after=25,
            )
            return

    await bot.process_commands(message)

# ══════════════════════════════════════════════════════════════════════════════
# TODO COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="todo_assign", description="Assign a TODO to yourself or someone else")
@app_commands.describe(todo_id="TODO number", user="User to assign (leave blank for yourself)")
async def todo_assign(interaction: discord.Interaction, todo_id: int, user: discord.Member = None):
    if not await has_todo_role(interaction):
        await interaction.response.send_message("No permission to assign TODOs.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target = user or interaction.user

    async with aiohttp.ClientSession() as session:
        todos, sha = await gh_read_fresh(session, FILE_TODOS)
        if not todos:
            await interaction.followup.send("No TODOs found.", ephemeral=True)
            return
        todo = next((t for t in todos if t["id"] == todo_id), None)
        if not todo:
            await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
            return
        if todo.get("assigned_to_id") and todo["assigned_to_id"] != str(interaction.user.id):
            if not interaction.user.guild_permissions.administrator:
                await interaction.followup.send(
                    f"TODO #{todo_id} is already assigned to <@{todo['assigned_to_id']}>. Only an admin can reassign it.",
                    ephemeral=True,
                )
                return
        todo["assigned_to_id"]   = str(target.id)
        todo["assigned_to_name"] = str(target)
        todo["updated_at"]       = now_iso()
        if todo["status"] == "todo":
            todo["status"] = "in_progress"
        await gh_write(session, FILE_TODOS, todos, sha, f"TODO #{todo_id} assigned to {target}")

    await interaction.followup.send(
        f"TODO **#{todo_id}** assigned to {target.mention}. Status set to In Progress.",
        ephemeral=True,
    )
    async with aiohttp.ClientSession() as session:
        cfg, _ = await gh_read(session, FILE_CONFIG)
    await update_todo_board(interaction.guild, cfg or {})


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
    await update_todo_board(interaction.guild, cfg or {})


@bot.tree.command(name="todo_list", description="Show all active TODOs")
async def todo_list(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    todos   = todos   or []
    archive = archive or []
    active  = [t for t in todos if t["status"] != "done"]
    if not active:
        await interaction.followup.send(f"No active TODOs. {len(archive)} total completed.")
        return
    pages = [active[i:i+TODOS_PER_PAGE] for i in range(0, len(active), TODOS_PER_PAGE)]
    for i, page in enumerate(pages[:3]):
        await interaction.followup.send(embed=build_page_embed(page, i + 1, len(pages)))


@bot.tree.command(name="todo_mine", description="Show TODOs assigned to you")
async def todo_mine(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos, _ = await gh_read(session, FILE_TODOS)
    todos = todos or []
    mine  = [t for t in todos if t.get("assigned_to_id") == str(interaction.user.id)]
    if not mine:
        await interaction.followup.send("No TODOs assigned to you.", ephemeral=True)
        return
    e = discord.Embed(title="Your TODOs", color=0x5865F2)
    for t in mine:
        name, value = build_todo_card(t)
        e.add_field(name=name, value=value, inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="todo_archive", description="View completed TODOs")
async def todo_archive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    archive = archive or []
    if not archive:
        await interaction.followup.send("No completed TODOs yet.", ephemeral=True)
        return
    recent = list(reversed(archive[-15:]))
    e = discord.Embed(
        title=f"Completed TODOs  ({len(archive)} total)",
        color=0x1D9E75,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for t in recent[:10]:
        done_by = f"<@{t['done_by_id']}>" if t.get("done_by_id") else t.get("done_by_name", "?")
        done_at = t.get("done_at", "")[:10]
        e.add_field(
            name=f"#{t['id']} — {t['title'][:65]}",
            value=f"Done by {done_by}  ·  {done_at}",
            inline=False,
        )
    e.set_footer(text="Showing most recent 10")
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="todo_info", description="View full details of a TODO")
@app_commands.describe(todo_id="TODO number")
async def todo_info(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        todos,   _ = await gh_read(session, FILE_TODOS)
        archive, _ = await gh_read(session, FILE_TODOS_ARCHIVE)
    all_todos = (todos or []) + (archive or [])
    todo = next((t for t in all_todos if t["id"] == todo_id), None)
    if not todo:
        await interaction.followup.send(f"TODO #{todo_id} not found.", ephemeral=True)
        return

    status   = todo.get("status", "todo")
    color    = STATUS_COLORS.get(status, 0x5865F2)
    label    = STATUS_LABELS.get(status, status)
    priority = PRIORITY_LABELS.get(todo.get("priority", "medium"), "Medium")
    assigned = f"<@{todo['assigned_to_id']}>" if todo.get("assigned_to_id") else "Unassigned"

    e = discord.Embed(
        title=f"#{todo['id']} — {todo['title']}",
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    e.add_field(name="Status",   value=label,    inline=True)
    e.add_field(name="Priority", value=priority, inline=True)
    e.add_field(name="Assigned", value=assigned, inline=True)
    e.add_field(name="Added by", value=f"<@{todo['added_by_id']}>",      inline=True)
    e.add_field(name="Created",  value=todo.get("created_at", "")[:10],  inline=True)
    e.add_field(name="Updated",  value=todo.get("updated_at", "")[:10],  inline=True)

    if todo.get("done_by_id"):
        e.add_field(
            name="Completed by",
            value=f"<@{todo['done_by_id']}>  ·  {todo.get('done_at','')[:10]}",
            inline=False,
        )

    # Source message context
    src_text  = todo.get("source_message_text", "")
    src_links = todo.get("source_message_links", []) or ([todo["source_message_link"]] if todo.get("source_message_link") else [])
    src_imgs  = todo.get("source_images", [])
    src_files = todo.get("source_files",  [])

    # AI fields — always show user original message alongside AI output
    ai_title = todo.get("auto_generated")
    ai_desc  = todo.get("ai_description", "")
    if ai_title and ai_desc:
        e.add_field(name="AI summary", value=ai_desc[:300], inline=False)
    if src_text:
        e.add_field(name="Original message (user)", value=src_text[:500], inline=False)

    if src_links:
        links_val = "  ·  ".join(f"[Message {i+1}]({l})" for i, l in enumerate(src_links[:5]))
        e.add_field(name="Source", value=links_val, inline=False)

    if src_imgs:
        e.set_image(url=src_imgs[0])
        if len(src_imgs) > 1:
            extra = "\n".join(f"[Image {i+2}]({u})" for i, u in enumerate(src_imgs[1:4]))
            e.add_field(name="More images", value=extra, inline=False)

    if src_files:
        file_links = "\n".join(f"[{u.split('/')[-1]}]({u})" for u in src_files[:5])
        e.add_field(name="Attachments", value=file_links, inline=False)

    e.set_footer(text=f"TODO #{todo_id}")
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
    todos = todos or []
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

    e = discord.Embed(
        title=f"Filtered TODOs — {filter_str}  ({len(results)} found)",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    for t in results[:15]:
        name, value = build_todo_card(t)
        e.add_field(name=name, value=value, inline=False)
    if len(results) > 15:
        e.set_footer(text=f"Showing first 15 of {len(results)}")
    await interaction.followup.send(embed=e, ephemeral=True)


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
    e.add_field(name="TODO Board",  value=ch_mention(cfg.get("todo_channel")), inline=True)
    roles = cfg.get("todo_roles", [])
    e.add_field(name="TODO Roles",  value=(", ".join(f"<@&{r}>" for r in roles) or "None"), inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)

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
