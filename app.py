"""
AnymeX TODO Dashboard — Flask Web App
Discord OAuth2 + GitHub DB (same repo as bot)
"""

import os
import base64
import json
import time
import datetime
import secrets
import asyncio
import aiohttp
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (
    Flask, render_template, redirect, request,
    session, url_for, jsonify, abort
)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# Discord OAuth2
DISCORD_CLIENT_ID     = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI  = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DISCORD_GUILD_ID      = os.environ.get("DISCORD_GUILD_ID")   # your server ID

DISCORD_API   = "https://discord.com/api/v10"
DISCORD_OAUTH = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_SCOPE = "identify guilds.members.read"

# Discord Bot Token (for reading forum channels — reuse the same token from bot.py)
DISCORD_BOT_TOKEN          = os.environ.get("DISCORD_TOKEN")
DISCORD_BUGS_CHANNEL_ID    = os.environ.get("DISCORD_BUGS_CHANNEL_ID")
DISCORD_SUGGESTIONS_CHANNEL_ID = os.environ.get("DISCORD_SUGGESTIONS_CHANNEL_ID")

# GitHub DB (same as bot)
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN")
DATA_OWNER    = os.environ.get("DATA_OWNER", "Shebyyy")
DATA_REPO     = os.environ.get("DATA_REPO",  "anymex-support-db")
DATA_BRANCH   = os.environ.get("DATA_BRANCH", "main")
GITHUB_API    = "https://api.github.com"

FILE_CONFIG        = "config.json"
FILE_TODOS         = "todos.json"
FILE_TODOS_ARCHIVE = "todos_archive.json"
FILE_FORUM_LINKS   = "forum_links.json"
FILE_ACTIVITY_LOG  = "activity_log.json"
FILE_MEMBERS       = "members.json"       # guild member snapshot synced from Discord
FILE_TODO_MEMBERS  = "todo_members.json"  # only is_todo_role=true members (small, always readable)

# ── New feature files ────────────────────────────────────────────────────────
FILE_COMMENTS       = "todo_comments.json"       # #1  Comments on TODOs
FILE_SAVED_FILTERS  = "saved_filters.json"       # #3  Saved board filters per user
FILE_GITHUB_ISSUES  = "github_issues.json"       # #14 GitHub issues mirror
FILE_ISSUE_LINKS    = "github_issue_links.json"  # #14 GitHub issue → TODO links
FILE_RELEASES       = "releases.json"            # #22 Release notes
FILE_BOT_HEALTH     = "bot_health.json"          # #25 Bot health monitor

# GitHub repo to mirror issues from (AnymeX main repo)
ANYMEX_OWNER = os.environ.get("ANYMEX_OWNER", "RyanYuuki")
ANYMEX_REPO  = os.environ.get("ANYMEX_REPO",  "AnymeX")

GITHUB_ISSUES_SYNC_TTL = 10 * 60  # 10 min

# Bot notification (triggers Discord board refresh after site changes)
BOT_NOTIFY_URL  = os.environ.get("BOT_NOTIFY_URL")   # e.g. http://localhost:8081/notify
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")  # same value set in bot.py

DEFAULT_CONFIG = {
    "todo_channel": None, "todo_roles": [], "todo_style": 1,
    "prefix": "ax!", "log_channel": None,
    "reminder_days": 3, "reminder_time": "09:00",
    "reminder_channel": None, "thread_channel": None,
}

STATUS_COLORS = {
    "todo": "#378ADD", "in_progress": "#BA7517",
    "review_needed": "#888780", "blocked": "#E24B4A", "done": "#1D9E75",
}
STATUS_LABELS = {
    "todo": "To Do", "in_progress": "In Progress",
    "review_needed": "Review Needed", "blocked": "Blocked", "done": "Done",
}
STATUS_ICONS = {
    "todo": "○", "in_progress": "◑",
    "review_needed": "◇", "blocked": "✕", "done": "✓",
}
PRIORITY_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}
PRIORITY_ICONS  = {"low": "▽", "medium": "◈", "high": "▲"}
TAG_COLORS = {
    "bug": "#E24B4A", "feature": "#1D9E75", "urgent": "#BA7517",
    "docs": "#378ADD", "refactor": "#9B59B6", "question": "#F1C40F",
}

CACHE_TTL = 60  # 1 min cache for web
_cache: dict = {}
_cache_ts: dict = {}

# ══════════════════════════════════════════════════════════════════════════════
# GITHUB HELPERS (sync wrappers using requests)
# ══════════════════════════════════════════════════════════════════════════════

import requests as req

# Proxy config (optional — set env vars to enable)
_PROXY_HOST = os.environ.get('WEB_PROXY_HOST')
_PROXY_PORT = os.environ.get('WEB_PROXY_PORT')
_PROXY_USER = os.environ.get('WEB_PROXY_USER')
_PROXY_PASS = os.environ.get('WEB_PROXY_PASS')

def get_proxies():
    if _PROXY_HOST and _PROXY_PORT:
        auth = f"{_PROXY_USER}:{_PROXY_PASS}@" if _PROXY_USER else ""
        proxy_url = f"http://{auth}{_PROXY_HOST}:{_PROXY_PORT}"
        return {"http": proxy_url, "https": proxy_url}
    return None

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_read(filepath: str, force=False):
    now = time.time()
    if not force and filepath in _cache and now - _cache_ts.get(filepath, 0) < CACHE_TTL:
        return _cache[filepath], None
    try:
        url = f"{GITHUB_API}/repos/{DATA_OWNER}/{DATA_REPO}/contents/{filepath}?ref={DATA_BRANCH}"
        r = req.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 404:
            return None, None
        if not r.ok:
            print(f"[gh_read] GitHub error {r.status_code} for {filepath}")
            return _cache.get(filepath), None
        data = r.json()
        if "content" not in data:
            print(f"[gh_read] Unexpected response for {filepath}: {list(data.keys())}")
            return _cache.get(filepath), None
        content = base64.b64decode(data["content"]).decode("utf-8")
        parsed = json.loads(content)
        _cache[filepath] = parsed
        _cache_ts[filepath] = now
        return parsed, data["sha"]
    except Exception as e:
        print(f"[gh_read] Exception reading {filepath}: {e}")
        return _cache.get(filepath), None

def gh_write(filepath: str, data, sha, msg: str):
    _cache.pop(filepath, None)
    payload = {
        "message": msg,
        "content": base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode()).decode(),
        "branch": DATA_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    url = f"{GITHUB_API}/repos/{DATA_OWNER}/{DATA_REPO}/contents/{filepath}"
    try:
        r = req.put(url, headers=gh_headers(), json=payload, timeout=15)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"[gh_write] Exception writing {filepath}: {e}")
        return False


FILE_SESSIONS = "sessions.json"
SESSION_TTL_DAYS = 30

def session_read_all():
    data, sha = gh_read(FILE_SESSIONS, force=True)
    return (data or {}), sha

def session_save(sid, payload):
    sessions, sha = session_read_all()
    sessions[sid] = {
        **payload,
        "expires_at": (datetime.datetime.utcnow() + datetime.timedelta(days=SESSION_TTL_DAYS)).isoformat()
    }
    # Clean expired sessions
    now = datetime.datetime.utcnow().isoformat()
    sessions = {k: v for k, v in sessions.items() if v.get("expires_at", "") > now}
    gh_write(FILE_SESSIONS, sessions, sha, f"Session: save {sid[:8]}")

def session_get(sid):
    sessions, _ = session_read_all()
    s = sessions.get(sid)
    if not s:
        return None
    if s.get("expires_at", "") < datetime.datetime.utcnow().isoformat():
        return None
    return s

def session_delete(sid):
    sessions, sha = session_read_all()
    if sid in sessions:
        del sessions[sid]
        gh_write(FILE_SESSIONS, sessions, sha, f"Session: delete {sid[:8]}")

# ══════════════════════════════════════════════════════════════════════════════
# DISCORD OAUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def discord_get(endpoint, token):
    r = req.get(f"{DISCORD_API}{endpoint}", headers={"Authorization": f"Bearer {token}"}, proxies=get_proxies())
    return r.json() if r.ok else None

# ── Discord Bot API helpers (for forum channel access) ──────────────────────

def bot_get(endpoint):
    """Call Discord API using the Bot Token (not user OAuth)."""
    if not DISCORD_BOT_TOKEN:
        return None
    r = req.get(
        f"{DISCORD_API}{endpoint}",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        proxies=get_proxies(),
        timeout=10,
    )
    return r.json() if r.ok else None

def notify_bot_board():
    """
    Fire-and-forget POST to the bot's /notify endpoint so the Discord
    TODO board refreshes immediately after any site change.
    Silently fails if BOT_NOTIFY_URL is not configured.
    """
    if not BOT_NOTIFY_URL:
        return
    try:
        req.post(
            BOT_NOTIFY_URL,
            headers={"X-Internal-Secret": INTERNAL_SECRET},
            timeout=3,
        )
    except Exception:
        pass  # non-critical — board will self-correct on next bot action


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOG — mirrors bot.py log_activity but called from web dashboard
# Posts an embed to the configured log_channel for every action on the site
# ══════════════════════════════════════════════════════════════════════════════

STATUS_COLORS_INT = {
    "todo":          0x378ADD,
    "in_progress":   0xBA7517,
    "review_needed": 0x888780,
    "blocked":       0xE24B4A,
    "done":          0x1D9E75,
}

def _web_log_activity(action: str, todo: dict, user: dict, extra: str = ""):
    """
    Post an activity embed to the Discord log channel via the bot token.
    Runs in a background thread so it never blocks the API response.

    action  — human label e.g. "TODO Created", "Status → In Progress"
    todo    — the todo dict (must have at least id, title, status)
    user    — the session user dict (Discord user object)
    extra   — optional extra line shown below the action
    """
    if not DISCORD_BOT_TOKEN:
        return

    cfg, _ = gh_read(FILE_CONFIG)
    cfg = cfg or {}
    log_ch_id = cfg.get("log_channel")
    if not log_ch_id:
        return

    status    = todo.get("status", "todo")
    color     = STATUS_COLORS_INT.get(status, 0x5865F2)
    title_str = todo.get("title", "")[:60]
    user_name = (user.get("global_name") or user.get("username") or "Web User") if user else "Web Dashboard"
    user_id   = str(user.get("id", "")) if user else ""
    user_str  = f"<@{user_id}>" if user_id else user_name

    desc = f"**{action}**"
    if extra:
        desc += "\n" + extra

    embed = {
        "title":       f"📋 TODO #{todo.get('id', '?')} — {title_str}",
        "description": desc,
        "color":       color,
        "footer":      {"text": f"By {user_name} (web dashboard)"},
        "timestamp":   datetime.datetime.utcnow().isoformat() + "Z",
    }

    payload = {"embeds": [embed]}

    def _post():
        try:
            req.post(
                f"https://discord.com/api/v10/channels/{log_ch_id}/messages",
                headers={
                    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=5,
                proxies=get_proxies(),
            )
        except Exception as e:
            print(f"[web_log] Failed to post activity: {e}")

    _threading.Thread(target=_post, daemon=True).start()
    # Also persist to activity_log.json for the /activity page
    _persist_activity(action, todo, user if user else {}, extra)


def _persist_activity(action: str, todo: dict, user: dict, extra: str = ""):
    """Append an entry to activity_log.json (last 200 entries, newest first)."""
    def _write():
        try:
            log, sha = gh_read(FILE_ACTIVITY_LOG, force=True)
            log = log or []
            entry = {
                "ts":        datetime.datetime.utcnow().isoformat() + "Z",
                "action":    action,
                "extra":     extra,
                "todo_id":   todo.get("id"),
                "todo_title": todo.get("title", "")[:80],
                "todo_status": todo.get("status", "todo"),
                "user_id":   str(user.get("id", "")) if user else "",
                "user_name": (user.get("global_name") or user.get("username") or "Web User") if user else "System",
                "user_avatar": _avatar_url(user) if user else "",
            }
            log.insert(0, entry)
            log = log[:200]   # keep last 200 entries
            gh_write(FILE_ACTIVITY_LOG, log, sha, f"Activity: {action[:40]}")
        except Exception as e:
            print(f"[activity_log] Failed: {e}")
    _threading.Thread(target=_write, daemon=True).start()

def _avatar_url(user_obj):
    """Build a Discord CDN avatar URL from a user object."""
    if not user_obj:
        return "https://cdn.discordapp.com/embed/avatars/0.png"
    uid = user_obj.get("id", "0")
    av  = user_obj.get("avatar")
    if av:
        return f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=64"
    # Default avatar index based on discriminator or user id
    disc = int(user_obj.get("discriminator", "0") or "0")
    idx  = (disc % 5) if disc else (int(uid) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"

def _fmt_discord_ts(ts_str):
    """Format a Discord ISO timestamp to a readable string."""
    if not ts_str:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(ts_str.rstrip("Z").split("+")[0])
        return dt.strftime("%b %d, %Y")
    except Exception:
        return ts_str

# ══════════════════════════════════════════════════════════════════════════════
# FORUM — GitHub DB storage + smart Discord sync
# ══════════════════════════════════════════════════════════════════════════════
#
# Storage layout in forum_posts.json:
# {
#   "bugs": {
#     "last_synced": "2026-03-29T10:00:00",
#     "channel_id": "123456",
#     "posts": [
#       {
#         "id": "111",
#         "title": "App crashes on login",
#         "content": "OP description text",
#         "author_id": "999",
#         "author_name": "sheby",
#         "author_avatar": "https://cdn.discordapp.com/...",
#         "created_at": "2026-03-01T10:00:00",
#         "created_at_fmt": "Mar 01, 2026",
#         "message_count": 5,
#         "is_locked": false,
#         "is_archived": false,
#         "status_label": "Open",
#         "tags": ["bug", "urgent"],
#         "linked_todo_id": null,
#         "messages": [
#           {
#             "author_name": "john",
#             "author_avatar": "https://...",
#             "content": "I have this too",
#             "created_at": "2026-03-01T11:00:00",
#             "attachments": [
#               { "filename": "screen.png", "content_type": "image/png",
#                 "width": 1280, "height": 720 }
#             ]
#           }
#         ],
#         "attachments": [...]   ← OP attachments (no URLs — refreshed on open)
#       }
#     ]
#   },
#   "suggestions": { ... }
# }
#
# NOTE: Attachment URLs are NOT stored — Discord CDN links expire.
#       When a thread is opened, we call Discord once to get fresh URLs.
# ══════════════════════════════════════════════════════════════════════════════

FILE_FORUM_POSTS_BUGS        = "forum_posts_bugs.json"
FILE_FORUM_POSTS_SUGGESTIONS = "forum_posts_suggestions.json"

def _forum_file(forum_type):
    """Return the correct file path for a given forum type."""
    if forum_type == "suggestions":
        return FILE_FORUM_POSTS_SUGGESTIONS
    return FILE_FORUM_POSTS_BUGS
FORUM_SYNC_TTL   = 5 * 60  # only hit Discord API again after 5 minutes

import threading as _threading

def _resolve_tags(applied_tag_ids, available_tags):
    """Turn a list of tag IDs into human-readable tag name strings."""
    out = []
    for tid in (applied_tag_ids or []):
        info = available_tags.get(str(tid)) or available_tags.get(tid)
        if info:
            emoji = info.get("emoji") or ""
            name  = info.get("name", str(tid))
            out.append(f"{emoji} {name}".strip() if emoji else name)
        else:
            out.append(str(tid))
    return out

def _thread_status(thread_meta):
    is_locked   = thread_meta.get("locked", False)
    is_archived = thread_meta.get("archived", False)
    if is_locked:
        label = "Locked"
    elif is_archived:
        label = "Archived"
    else:
        label = "Open"
    return is_locked, is_archived, label

def _parse_attachments(raw_attachments, include_url=False):
    """
    Parse Discord attachment objects.
    include_url=False  → strip the URL (for storage, since CDN links expire)
    include_url=True   → keep the URL (for live thread detail responses)
    """
    out = []
    for a in (raw_attachments or []):
        entry = {
            "filename":     a.get("filename", "file"),
            "content_type": a.get("content_type", ""),
            "width":        a.get("width"),
            "height":       a.get("height"),
            "size":         a.get("size"),
        }
        if include_url:
            entry["url"] = a.get("url", "")
            entry["proxy_url"] = a.get("proxy_url", "")
        out.append(entry)
    return out

import re as _re
import html as _html

def _render_discord_content(raw_content, msg_obj, extra_channel_map=None):
    """
    Convert raw Discord markdown into rendered HTML.

    Resolves (using data already present in the message object — zero extra API calls):
      <@id> / <@!id>   -> @DisplayName  (from msg mentions array)
      <#id>            -> #channel-name (from mention_channels + extra_channel_map)
      <@&id>           -> @role
      <:name:id>       -> PNG emoji img
      <a:name:id>      -> GIF emoji img (animated — detected via separate regex)

    extra_channel_map: optional dict {channel_id: name} built once per sync run
    so channel mentions always resolve correctly even if not in mention_channels.
    """
    if not raw_content:
        return ""

    content = raw_content

    # Build user lookup from mentions array (free — part of every message)
    user_map = {}
    for u in (msg_obj.get("mentions") or []):
        uid  = str(u.get("id", ""))
        name = u.get("global_name") or u.get("username") or uid
        if uid:
            user_map[uid] = name

    # Build channel lookup — mention_channels from message + extra from sync
    channel_map = dict(extra_channel_map or {})
    for ch in (msg_obj.get("mention_channels") or []):
        cid  = str(ch.get("id", ""))
        name = ch.get("name") or cid
        if cid:
            channel_map[cid] = name

    # 1. User mentions  <@id>  <@!id>
    def _user(m):
        uid  = m.group(1)
        name = user_map.get(uid, uid)
        return f'<span class="discord-mention discord-mention-user">@{_html.escape(name)}</span>'
    content = _re.sub(r"<@!?(\d+)>", _user, content)

    # 2. Channel mentions  <#id>
    def _channel(m):
        cid  = m.group(1)
        name = channel_map.get(cid, cid)
        return f'<span class="discord-mention discord-mention-channel">#{_html.escape(name)}</span>'
    content = _re.sub(r"<#(\d+)>", _channel, content)

    # 3. Role mentions  <@&id>  — role names not in message payload
    content = _re.sub(
        r"<@&(\d+)>",
        lambda m: '<span class="discord-mention discord-mention-role">@role</span>',
        content,
    )

    # 4. Animated custom emoji  <a:name:id>  — must match BEFORE static emoji
    def _anim_emoji(m):
        name = m.group(1)
        eid  = m.group(2)
        url  = f"https://cdn.discordapp.com/emojis/{eid}.gif?size=24"
        return (
            f'<img class="discord-emoji" src="{url}" '
            f'alt=":{_html.escape(name)}:" title=":{_html.escape(name)}:" '
            f'style="width:22px;height:22px;vertical-align:middle;display:inline;">'
        )
    content = _re.sub(r"<a:([^:]+):(\d+)>", _anim_emoji, content)

    # 5. Static custom emoji  <:name:id>
    def _static_emoji(m):
        name = m.group(1)
        eid  = m.group(2)
        url  = f"https://cdn.discordapp.com/emojis/{eid}.png?size=24"
        return (
            f'<img class="discord-emoji" src="{url}" '
            f'alt=":{_html.escape(name)}:" title=":{_html.escape(name)}:" '
            f'style="width:22px;height:22px;vertical-align:middle;display:inline;">'
        )
    content = _re.sub(r"<:([^:]+):(\d+)>", _static_emoji, content)

    return content


def _fetch_thread_messages(thread_id, include_urls=False, extra_channel_map=None):
    """
    Fetch all messages for a thread from Discord.
    Returns (op_content_raw, op_content_html, op_author, op_attachments, messages_list).

    Stores BOTH raw (content_raw) and rendered HTML (content) so:
    - DB keeps original text — clean, re-renderable
    - Templates get ready-to-display HTML with | safe
    - If renderer is fixed later, just re-sync to get updated HTML
    """
    msgs_raw = bot_get(f"/channels/{thread_id}/messages?limit=100")
    if not msgs_raw:
        return "", "", {}, [], []
    msgs_raw = list(reversed(msgs_raw))  # Discord returns newest-first

    op_content_raw  = ""
    op_content_html = ""
    op_author       = {}
    op_attachments  = []
    messages        = []

    for i, m in enumerate(msgs_raw):
        author      = m.get("author") or {}
        author_name = author.get("global_name") or author.get("username") or "Unknown"
        avatar_url  = _avatar_url(author)
        raw         = m.get("content", "")
        rendered    = _render_discord_content(raw, m, extra_channel_map=extra_channel_map)
        ts          = m.get("timestamp", "")
        attachments = _parse_attachments(m.get("attachments"), include_url=include_urls)

        if i == 0:
            op_content_raw  = raw
            op_content_html = rendered
            op_author       = {"name": author_name, "avatar": avatar_url, "id": author.get("id")}
            op_attachments  = attachments
        else:
            messages.append({
                "author_name":   author_name,
                "author_avatar": avatar_url,
                "content":       rendered,   # HTML — use | safe in template
                "content_raw":   raw,        # original Discord markdown — kept for reference
                "created_at":    ts,
                "attachments":   attachments,
            })

    return op_content_raw, op_content_html, op_author, op_attachments, messages

def _sync_forum_to_db(channel_id, forum_type):
    """
    Sync a Discord forum channel into its own JSON file on GitHub.
    - New posts → fully fetched (messages + attachments metadata stored, no URLs)
    - Existing posts with changed message_count → messages re-fetched
    - Unchanged posts → untouched
    - Only writes to GitHub if something actually changed
    Called in a background thread so page loads don't block.
    Always wrapped in try/except so a Discord API error never crashes the page.
    """
    try:
        _sync_forum_to_db_inner(channel_id, forum_type)
    except Exception as e:
        print(f"[forum_sync] Error syncing {forum_type}: {e}")


def _sync_forum_to_db_inner(channel_id, forum_type):
    if not channel_id or not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID:
        return

    # Load current DB state — each forum type has its own file
    forum_file = _forum_file(forum_type)
    db, db_sha = gh_read(forum_file, force=True)
    db = db or {}
    section = db.get(forum_type) or {"last_synced": "", "channel_id": channel_id, "posts": []}
    stored_posts = {str(p["id"]): p for p in (section.get("posts") or [])}

    # Fetch forum channel for tag definitions
    forum_channel = bot_get(f"/channels/{channel_id}")
    if not forum_channel:
        return
    available_tags = {
        t["id"]: {"name": t.get("name", t["id"]), "emoji": t.get("emoji_name") or ""}
        for t in (forum_channel.get("available_tags") or [])
    }

    # Build channel name map ONCE per sync run so <#channel_id> always resolves correctly
    # This is 1 extra API call per sync, not per message
    extra_channel_map = {}
    guild_channels = bot_get(f"/guilds/{DISCORD_GUILD_ID}/channels") or []
    for ch in guild_channels:
        cid  = str(ch.get("id", ""))
        name = ch.get("name", cid)
        if cid:
            extra_channel_map[cid] = name

    # Fetch active threads (guild-scoped, filter to this channel)
    active_data = bot_get(f"/guilds/{DISCORD_GUILD_ID}/threads/active")
    active_threads = [
        t for t in ((active_data or {}).get("threads") or [])
        if str(t.get("parent_id")) == str(channel_id)
    ]
    # Fetch archived threads
    archived_data = bot_get(f"/channels/{channel_id}/threads/archived/public?limit=50")
    archived_threads = ((archived_data or {}).get("threads") or [])

    all_threads = active_threads + archived_threads
    changed = False

    for t in all_threads:
        tid          = str(t.get("id"))
        thread_meta  = t.get("thread_metadata") or {}
        is_locked, is_archived, status_label = _thread_status(thread_meta)
        ts_raw       = thread_meta.get("create_timestamp", "")
        new_msg_count = t.get("message_count", 0)
        resolved_tags = _resolve_tags(t.get("applied_tags"), available_tags)

        existing = stored_posts.get(tid)

        if existing is None:
            # Brand new post — fetch full messages
            raw, html_content, op_author, op_attach, messages = _fetch_thread_messages(tid, include_urls=False, extra_channel_map=extra_channel_map)
            stored_posts[tid] = {
                "id":             tid,
                "title":          t.get("name", "Untitled"),
                "content":        html_content,
                "content_raw":    raw,
                "preview":        raw[:120] if raw else "",
                "author_id":      t.get("owner_id"),
                "author_name":    op_author.get("name", "Member"),
                "author_avatar":  op_author.get("avatar", "https://cdn.discordapp.com/embed/avatars/0.png"),
                "created_at":     ts_raw,
                "created_at_fmt": _fmt_discord_ts(ts_raw),
                "message_count":  new_msg_count,
                "is_locked":      is_locked,
                "is_archived":    is_archived,
                "status_label":   status_label,
                "tags":           resolved_tags,
                "linked_todo_id": None,
                "attachments":    op_attach,
                "messages":       messages,
            }
            changed = True

        else:
            # Existing post — check what changed
            post_changed = False

            # New replies came in
            if new_msg_count != existing.get("message_count", 0):
                raw, html_content, op_author, op_attach, messages = _fetch_thread_messages(tid, include_urls=False, extra_channel_map=extra_channel_map)
                existing["content"]       = html_content
                existing["content_raw"]   = raw
                existing["preview"]       = raw[:120] if raw else ""
                existing["author_name"]   = op_author.get("name", existing["author_name"])
                existing["author_avatar"] = op_author.get("avatar", existing["author_avatar"])
                existing["message_count"] = new_msg_count
                existing["attachments"]   = op_attach
                existing["messages"]      = messages
                post_changed = True

            # Metadata updates (title rename, tag change, lock/archive)
            if existing.get("title") != t.get("name", "Untitled"):
                existing["title"] = t.get("name", "Untitled")
                post_changed = True
            if existing.get("status_label") != status_label:
                existing["status_label"] = status_label
                existing["is_locked"]    = is_locked
                existing["is_archived"]  = is_archived
                post_changed = True
            if existing.get("tags") != resolved_tags:
                existing["tags"] = resolved_tags
                post_changed = True

            if post_changed:
                stored_posts[tid] = existing
                changed = True

    if not changed:
        # Still update last_synced timestamp even if nothing changed
        section["last_synced"] = datetime.datetime.utcnow().isoformat()
        db[forum_type] = section
        gh_write(forum_file, db, db_sha, f"Forum: sync {forum_type} (no changes)")
        return

    # Merge forum_links into posts
    links, _ = gh_read(FILE_FORUM_LINKS)
    links = links or {}
    for tid, post in stored_posts.items():
        post["linked_todo_id"] = links.get(tid)

    section["last_synced"] = datetime.datetime.utcnow().isoformat()
    section["channel_id"]  = channel_id
    section["posts"]       = list(stored_posts.values())
    db[forum_type] = section
    gh_write(forum_file, db, db_sha, f"Forum: sync {forum_type} ({len(all_threads)} posts)")


def fetch_forum_posts(channel_id, forum_type):
    """
    Return posts from GitHub DB (always fast — never blocks).

    - Fresh DB data (< 5 min old) → return immediately, no sync
    - Stale or never synced → kick off background sync, return whatever we have now
      (first-ever load returns [] instantly; page reloads after a few seconds to pick up data)
    """
    if not channel_id:
        return [], "Channel ID not configured"

    db, _ = gh_read(_forum_file(forum_type))
    db = db or {}
    section = db.get(forum_type) or {}
    posts   = section.get("posts") or []

    last_synced  = section.get("last_synced", "")
    needs_sync   = True

    if last_synced:
        try:
            age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_synced)).total_seconds()
            needs_sync = age > FORUM_SYNC_TTL
        except Exception:
            needs_sync = True

    # Always non-blocking — sync runs in background regardless of whether posts exist
    if DISCORD_BOT_TOKEN and needs_sync:
        t = _threading.Thread(target=_sync_forum_to_db, args=(channel_id, forum_type), daemon=True)
        t.start()

    # Enrich linked_todo_id from forum_links
    links, _ = gh_read(FILE_FORUM_LINKS)
    links = links or {}
    for p in posts:
        p["linked_todo_id"] = links.get(str(p["id"]), p.get("linked_todo_id"))

    return posts, None


def fetch_forum_thread_detail(thread_id, forum_type="bugs"):
    """
    Return full thread detail.
    - Metadata + stored messages come from GitHub DB (instant).
    - Attachment URLs are refreshed live from Discord (CDN links expire).
    """
    if not DISCORD_BOT_TOKEN:
        return None, "Bot token not configured"

    links, _ = gh_read(FILE_FORUM_LINKS)
    links = links or {}

    # Load stored post from DB — from the correct split file
    db, _ = gh_read(_forum_file(forum_type))
    db = db or {}
    stored_post = None
    for p in (db.get(forum_type, {}).get("posts") or []):
        if str(p["id"]) == str(thread_id):
            stored_post = dict(p)
            break

    # Build channel map for mention resolution
    extra_channel_map = {}
    if DISCORD_GUILD_ID:
        guild_channels = bot_get(f"/guilds/{DISCORD_GUILD_ID}/channels") or []
        for ch in guild_channels:
            cid  = str(ch.get("id", ""))
            name = ch.get("name", cid)
            if cid:
                extra_channel_map[cid] = name

    # Refresh attachment URLs from Discord (CDN links expire, so we always fetch fresh)
    fresh_raw, fresh_content, fresh_op_author, fresh_op_attach, fresh_messages = _fetch_thread_messages(
        thread_id, include_urls=True, extra_channel_map=extra_channel_map
    )

    if stored_post:
        # Use stored metadata, but inject fresh attachment URLs + any new messages
        stored_msgs = stored_post.get("messages") or []
        for i, msg in enumerate(fresh_messages):
            if i < len(stored_msgs):
                stored_msgs[i]["attachments"] = msg["attachments"]  # refresh URLs only
            else:
                stored_msgs.append(msg)  # new message since last sync
        stored_post["messages"]    = stored_msgs
        stored_post["attachments"] = fresh_op_attach  # refresh OP attachment URLs
        stored_post["linked_todo_id"] = links.get(str(thread_id), stored_post.get("linked_todo_id"))
        return stored_post, None

    # Post not in DB yet (first open before sync ran) — build from live data
    thread = bot_get(f"/channels/{thread_id}")
    if not thread:
        return None, "Thread not found"

    thread_meta = thread.get("thread_metadata") or {}
    is_locked, is_archived, status_label = _thread_status(thread_meta)
    ts_raw = thread_meta.get("create_timestamp", "")

    # Resolve tags
    parent_id = thread.get("parent_id")
    tag_names = []
    if parent_id:
        parent = bot_get(f"/channels/{parent_id}")
        if parent:
            available_tags = {
                t["id"]: {"name": t.get("name", t["id"]), "emoji": t.get("emoji_name") or ""}
                for t in (parent.get("available_tags") or [])
            }
            tag_names = _resolve_tags(thread.get("applied_tags"), available_tags)

    return {
        "id":            str(thread_id),
        "title":         thread.get("name", "Untitled"),
        "content":       fresh_content,
        "content_raw":   fresh_raw,
        "author_name":   fresh_op_author.get("name", "Member"),
        "author_avatar": fresh_op_author.get("avatar", "https://cdn.discordapp.com/embed/avatars/0.png"),
        "created_at":    ts_raw,
        "created_at_fmt": _fmt_discord_ts(ts_raw),
        "messages":      fresh_messages,
        "attachments":   fresh_op_attach,
        "tags":          tag_names,
        "status_label":  status_label,
        "is_locked":     is_locked,
        "is_archived":   is_archived,
        "linked_todo_id":   links.get(str(thread_id)),
        "linked_todo_info": _get_todo_info(links.get(str(thread_id))),
    }, None

def _get_todo_info(todo_id):
    """Return a full todo snapshot for display, or None."""
    if not todo_id:
        return None
    todos, _   = gh_read(FILE_TODOS)
    todo = next((t for t in (todos or []) if t["id"] == int(todo_id)), None)
    if not todo:
        archive, _ = gh_read(FILE_TODOS_ARCHIVE)
        todo = next((t for t in (archive or []) if t["id"] == int(todo_id)), None)
    if not todo:
        return None
    return {
        "id":             todo["id"],
        "title":          todo.get("title", ""),
        "status":         todo.get("status", "todo"),
        "priority":       todo.get("priority", "medium"),
        "tags":           todo.get("tags", []),
        "ai_description": todo.get("ai_description", ""),
    }

def get_access_token(code):
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    try:
        r = req.post(DISCORD_TOKEN_URL, data=data, timeout=10, proxies=get_proxies())
        print("TOKEN RESPONSE STATUS:", r.status_code)
        print("TOKEN RESPONSE BODY:", r.text)
        return r.json() if r.ok else r.json()
    except Exception as e:
        print("TOKEN REQUEST EXCEPTION:", str(e))
        return {"error": type(e).__name__, "error_description": str(e)}

def get_guild_member(token, guild_id):
    r = req.get(
        f"{DISCORD_API}/users/@me/guilds/{guild_id}/member",
        headers={"Authorization": f"Bearer {token}"},
        proxies=get_proxies()
    )
    return r.json() if r.ok else None

def resolve_access_level(member, cfg):
    """
    Returns: 'owner', 'admin', 'manager', 'member', 'public'
    """
    if not member:
        return "public"
    perms = int(member.get("permissions", 0))
    is_admin = bool(perms & 0x8)  # ADMINISTRATOR bit
    is_owner = member.get("is_pending") is False and is_admin  # rough check

    todo_roles = cfg.get("todo_roles", [])
    user_roles = member.get("roles", [])

    if is_admin:
        return "admin"
    if any(r in user_roles for r in todo_roles):
        return "manager"
    return "member"

# ══════════════════════════════════════════════════════════════════════════════
# AUTH DECORATORS
# ══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in get_session():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def manager_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        s = get_session()
        if "user" not in s:
            return redirect(url_for("login"))
        if s.get("access_level") not in ("manager", "admin", "owner"):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        s = get_session()
        if "user" not in s:
            return redirect(url_for("login"))
        if s.get("access_level") not in ("admin", "owner"):
            abort(403)
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def enrich_todo(t):
    """Add display helpers to a todo dict."""
    t = dict(t)
    status = t.get("status", "todo")
    t["status_label"]  = STATUS_LABELS.get(status, status)
    t["status_icon"]   = STATUS_ICONS.get(status, "○")
    t["status_color"]  = STATUS_COLORS.get(status, "#5865F2")
    pri = t.get("priority", "medium")
    t["priority_label"] = PRIORITY_LABELS.get(pri, pri)
    t["priority_icon"]  = PRIORITY_ICONS.get(pri, "◈")
    t["tag_colors"]     = {tag: TAG_COLORS.get(tag, "#5865F2") for tag in t.get("tags", [])}
    # Format dates
    for field in ("created_at", "updated_at"):
        val = t.get(field)
        if val:
            try:
                dt = datetime.datetime.fromisoformat(val)
                t[f"{field}_fmt"] = dt.strftime("%b %d, %Y")
                t[f"{field}_ts"]  = int(dt.timestamp())
            except Exception:
                t[f"{field}_fmt"] = val
    # Due date enrichment
    due = t.get("due_date")
    if due:
        try:
            due_dt = datetime.datetime.fromisoformat(due)
            t["due_date_fmt"] = due_dt.strftime("%b %d, %Y")
            now = datetime.datetime.utcnow()
            delta = (due_dt - now).days
            if status not in ("done",):
                if delta < 0:
                    t["due_overdue"] = True
                    t["due_urgency"] = "overdue"
                    t["due_label"]   = f"Overdue by {abs(delta)}d"
                elif delta == 0:
                    t["due_urgency"] = "today"
                    t["due_label"]   = "Due today"
                elif delta <= 2:
                    t["due_urgency"] = "soon"
                    t["due_label"]   = f"Due in {delta}d"
                else:
                    t["due_urgency"] = "normal"
                    t["due_label"]   = t["due_date_fmt"]
            else:
                t["due_urgency"] = "done"
                t["due_label"]   = t["due_date_fmt"]
        except Exception:
            t["due_date_fmt"] = due
    return t

@app.context_processor
def inject_nav_counts():
    """Inject my_task_count into every template for the nav badge."""
    try:
        sess = get_session()
        user = sess.get("user")
        if not user:
            return {"my_task_count": 0}
        uid = str(user.get("id", ""))
        todos, _ = gh_read(FILE_TODOS)
        count = len([t for t in (todos or [])
                     if t.get("assigned_to_id") == uid and t.get("status") != "done"])
        return {"my_task_count": count}
    except Exception:
        return {"my_task_count": 0}


app.jinja_env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    STATUS_COLORS=STATUS_COLORS,
    STATUS_ICONS=STATUS_ICONS,
    PRIORITY_LABELS=PRIORITY_LABELS,
    PRIORITY_ICONS=PRIORITY_ICONS,
    TAG_COLORS=TAG_COLORS,
    now=datetime.datetime.utcnow,
    enumerate=enumerate,
)

def _patch_forum_post_todo_link(thread_id, forum_type, todo_id, todo_info):
    """
    Immediately update a single post's linked_todo_id and linked_todo snapshot
    in forum_posts.json — no full sync needed.

    todo_id=None / todo_info=None → clears the link (used on TODO delete).
    todo_info should be: { id, title, status, priority }
    """
    try:
        db, db_sha = gh_read(_forum_file(forum_type), force=True)
        db = db or {}
        section = db.get(forum_type)
        if not section:
            return  # forum not synced yet, nothing to patch
        posts = section.get("posts") or []
        patched = False
        for post in posts:
            if str(post.get("id")) == str(thread_id):
                post["linked_todo_id"]   = todo_id
                post["linked_todo_info"] = todo_info  # full snapshot for UI display
                patched = True
                break
        if patched:
            section["posts"] = posts
            db[forum_type]   = section
            action = f"TODO #{todo_id}" if todo_id else "unlinked"
            gh_write(_forum_file(forum_type), db, db_sha,
                     f"Web: Patch forum post {thread_id} → {action}")
    except Exception:
        pass  # non-critical — background sync will fix it next round


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — AUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/login")
def login():
    state = secrets.token_hex(16)
    sid = secrets.token_hex(32)
    session_save(sid, {"oauth_state": state})
    resp = redirect(DISCORD_OAUTH + (
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={DISCORD_SCOPE.replace(' ', '%20')}"
        f"&state={state}"
    ))
    resp.set_cookie("sid", sid, max_age=600, httponly=True, samesite="Lax")
    return resp

@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return redirect(url_for("index") + "?error=discord_denied")

    sid = request.cookies.get("sid")
    sid_data = session_get(sid) if sid else None
    if not sid_data:
        return redirect(url_for("index") + "?error=state_mismatch")

    state = request.args.get("state")
    if state != sid_data.get("oauth_state"):
        return redirect(url_for("index") + "?error=state_mismatch")

    code = request.args.get("code")
    token_data = get_access_token(code)
    if not token_data or "access_token" not in token_data:
        import urllib.parse
        raw = urllib.parse.quote(str(token_data))
        return redirect(url_for("index") + f"?error=token_fail&raw={raw}")

    access_token = token_data["access_token"]
    user = discord_get("/users/@me", access_token)
    if not user:
        return redirect(url_for("index") + "?error=user_fail")

    member = None
    if DISCORD_GUILD_ID:
        member = get_guild_member(access_token, DISCORD_GUILD_ID)

    cfg, _ = gh_read(FILE_CONFIG)
    cfg = cfg or DEFAULT_CONFIG.copy()
    access_level = resolve_access_level(member, cfg)

    # Save full session to GitHub
    new_sid = secrets.token_hex(32)
    session_save(new_sid, {
        "user": user,
        "access_token": access_token,
        "access_level": access_level,
        "member": member,
    })

    # Keep members.json fresh for anyone who logs in
    if member:
        _upsert_member(user, member)

    resp = redirect(url_for("dashboard"))
    resp.set_cookie("sid", new_sid, max_age=60*60*24*30, httponly=True, samesite="Lax")
    return resp

def get_session():
    sid = request.cookies.get("sid")
    if not sid:
        return {}
    return session_get(sid) or {}

@app.route("/logout")
def logout():
    sid = request.cookies.get("sid")
    if sid:
        session_delete(sid)
    resp = redirect(url_for("index"))
    resp.delete_cookie("sid")
    return resp

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — PAGES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    todos   = [enrich_todo(t) for t in (todos or []) if t.get("status") != "done"]
    archive = archive or []

    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    counts["done"] = len(archive)

    return render_template("index.html",
        todos=todos, counts=counts, archive_count=len(archive),
        user=get_session().get("user"), access_level=get_session().get("access_level", "public"),
    )

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_session()["user"]
    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    todos   = todos or []
    archive = archive or []

    uid = str(user["id"])
    my_todos     = [enrich_todo(t) for t in todos if t.get("assigned_to_id") == uid and t.get("status") != "done"]
    added_todos  = [enrich_todo(t) for t in todos if t.get("added_by_id") == uid]
    active_todos = [enrich_todo(t) for t in todos if t.get("status") != "done"]

    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    counts["done"] = len(archive)

    return render_template("dashboard.html",
        user=user, access_level=get_session().get("access_level"),
        my_todos=my_todos, added_todos=added_todos,
        active_todos=active_todos, counts=counts,
        archive_count=len(archive),
    )

@app.route("/board")
def board():
    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    cfg, _     = gh_read(FILE_CONFIG)
    todos   = [enrich_todo(t) for t in (todos or []) if t.get("status") != "done"]
    archive = archive or []
    cfg     = cfg or DEFAULT_CONFIG.copy()

    # Filtering
    status_filter   = request.args.get("status", "all")
    priority_filter = request.args.get("priority", "all")
    tag_filter      = request.args.get("tag", "all")
    assignee_filter = request.args.get("assignee", "all")   # "all" | "me" | "unassigned"
    due_filter      = request.args.get("due", "all")         # "all" | "overdue" | "soon"
    search          = request.args.get("q", "").lower().strip()

    sess = get_session()
    current_uid = str(sess.get("user", {}).get("id", "")) if sess.get("user") else ""

    filtered = todos
    if status_filter != "all":
        filtered = [t for t in filtered if t["status"] == status_filter]
    if priority_filter != "all":
        filtered = [t for t in filtered if t.get("priority") == priority_filter]
    if tag_filter != "all":
        filtered = [t for t in filtered if tag_filter in t.get("tags", [])]
    if assignee_filter == "me" and current_uid:
        filtered = [t for t in filtered if str(t.get("assigned_to_id", "")) == current_uid]
    elif assignee_filter == "unassigned":
        filtered = [t for t in filtered if not t.get("assigned_to_id")]
    elif assignee_filter not in ("all", "me", "unassigned") and assignee_filter:
        # specific user ID passed
        filtered = [t for t in filtered if str(t.get("assigned_to_id", "")) == assignee_filter]
    if due_filter == "overdue":
        filtered = [t for t in filtered if t.get("due_urgency") == "overdue"]
    elif due_filter == "soon":
        filtered = [t for t in filtered if t.get("due_urgency") in ("overdue", "today", "soon")]
    if search:
        filtered = [t for t in filtered if
                    search in t["title"].lower()
                    or search in (t.get("ai_description") or "").lower()
                    or search in (t.get("assigned_to_name") or "").lower()
                    or any(search in tag.lower() for tag in t.get("tags", []))]

    all_tags = sorted(set(tag for t in todos for tag in t.get("tags", [])))

    # Load todo-role members for the assignee filter dropdown
    members_db = gh_read(FILE_TODO_MEMBERS)[0] or {}
    assignable_members = sorted(
        members_db.get("members") or [],
        key=lambda m: m.get("display_name", "").lower()
    )

    return render_template("board.html",
        todos=filtered, all_todos=todos, archive_count=len(archive),
        cfg=cfg, all_tags=all_tags,
        assignable_members=assignable_members,
        status_filter=status_filter, priority_filter=priority_filter,
        tag_filter=tag_filter, assignee_filter=assignee_filter,
        due_filter=due_filter, search=search,
        user=sess.get("user"), access_level=sess.get("access_level", "public"),
        current_user_id=current_uid,
    )

@app.route("/analytics")
@login_required
def analytics():
    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    todos   = todos or []
    archive = archive or []
    all_todos = todos + archive

    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    counts["done"] = len(archive)

    # Top contributors
    from collections import Counter
    added_by = Counter(t.get("added_by_id") for t in all_todos if t.get("added_by_id"))
    assigned = Counter(t.get("assigned_to_id") for t in all_todos if t.get("assigned_to_id"))

    # Tag distribution
    tag_counts = Counter(tag for t in all_todos for tag in t.get("tags", []))

    # Priority distribution
    pri_counts = Counter(t.get("priority", "medium") for t in todos if t.get("status") != "done")

    # Recent activity (last 10 completed)
    recent_done = sorted(archive, key=lambda t: t.get("updated_at", ""), reverse=True)[:10]
    recent_done = [enrich_todo(t) for t in recent_done]

    # AI vs manual
    ai_count     = len([t for t in all_todos if t.get("auto_generated")])
    manual_count = len(all_todos) - ai_count

    return render_template("analytics.html",
        user=get_session()["user"], access_level=get_session().get("access_level"),
        counts=counts, total=len(all_todos), archive_count=len(archive),
        tag_counts=dict(tag_counts), pri_counts=dict(pri_counts),
        ai_count=ai_count, manual_count=manual_count,
        recent_done=recent_done,
        added_by=dict(added_by.most_common(5)),
        assigned=dict(assigned.most_common(5)),
    )

@app.route("/settings")
@admin_required
def settings():
    cfg, _ = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    return render_template("settings.html",
        user=get_session()["user"], access_level=get_session().get("access_level"),
        cfg=cfg,
    )

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — API (JSON endpoints for JS)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todos")
def api_todos():
    include_archive = request.args.get("include_archive", "0") == "1"
    todos, _   = gh_read(FILE_TODOS)
    todos      = [enrich_todo(t) for t in (todos or []) if t.get("status") != "done"]
    if include_archive:
        archive, _ = gh_read(FILE_TODOS_ARCHIVE)
        archived   = [enrich_todo(t) for t in (archive or [])]
        todos      = todos + archived
    return jsonify(todos)

@app.route("/api/todo/<int:todo_id>", methods=["GET"])
def api_todo_get(todo_id):
    todos, _ = gh_read(FILE_TODOS)
    todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404
    return jsonify(enrich_todo(todo))

@app.route("/api/todo", methods=["POST"])
def api_todo_create():
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    next_id = max((t["id"] for t in todos), default=0) + 1
    user = get_session().get("user", {})

    new_todo = {
        "id": next_id,
        "title": title[:120],
        "ai_description": data.get("description", ""),
        "status": "todo",
        "priority": data.get("priority", "medium"),
        "tags": data.get("tags", []),
        "assigned_to_id": data.get("assigned_to_id"),
        "due_date": data.get("due_date") or None,
        "added_by_id": str(user.get("id", "")),
        "added_by": user.get("username", "web"),
        "created_at": datetime.datetime.utcnow().isoformat(),
        "updated_at": datetime.datetime.utcnow().isoformat(),
        "auto_generated": False,
        "source": "web_dashboard",
    }

    # If created from a forum thread, record the link
    linked_thread_id = data.get("linked_forum_thread_id")
    linked_type      = data.get("linked_forum_type")    # 'bug' or 'suggestion'
    if linked_thread_id:
        new_todo["linked_forum_thread_id"] = str(linked_thread_id)
        new_todo["linked_forum_type"]      = linked_type or "unknown"
        # 1. Persist thread → todo ID mapping in forum_links.json
        links, links_sha = gh_read(FILE_FORUM_LINKS, force=True)
        links = links or {}
        links[str(linked_thread_id)] = next_id
        gh_write(FILE_FORUM_LINKS, links, links_sha,
                 f"Web: Link forum thread {linked_thread_id} → TODO #{next_id}")
        # 2. Immediately patch forum_posts.json so the badge shows up right away
        #    without waiting for the next background sync
        _patch_forum_post_todo_link(
            thread_id  = str(linked_thread_id),
            forum_type = linked_type or "bugs",
            todo_id    = next_id,
            todo_info  = {
                "id":       next_id,
                "title":    new_todo["title"],
                "status":   new_todo["status"],
                "priority": new_todo["priority"],
            },
        )
    todos.append(new_todo)
    ok = gh_write(FILE_TODOS, todos, sha, f"Web: Add TODO #{next_id} by {user.get('username','?')}")
    if ok:
        notify_bot_board()
        extra = f"Linked to {linked_type} forum post" if linked_thread_id else ""
        _web_log_activity("TODO Created", new_todo, user, extra=extra)
        return jsonify(enrich_todo(new_todo)), 201
    return jsonify({"error": "GitHub write failed"}), 500

@app.route("/api/todo/<int:todo_id>", methods=["PATCH"])
def api_todo_update(todo_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404

    data = request.json or {}
    allowed = ("status", "priority", "tags", "assigned_to_id", "assigned_to_name", "title", "ai_description", "due_date")

    # Build a human-readable action label from what actually changed
    changes = []
    for field in allowed:
        if field in data and data[field] != todo.get(field):
            if field == "status":
                changes.append(f"Status → {STATUS_LABELS.get(data[field], data[field])}")
            elif field == "priority":
                changes.append(f"Priority → {PRIORITY_LABELS.get(data[field], data[field])}")
            elif field == "assigned_to_id":
                name = data.get("assigned_to_name") or data[field] or "Unassigned"
                changes.append(f"Assigned → {name}")
            elif field == "title":
                changes.append("Title updated")
            elif field == "tags":
                changes.append("Tags updated")
            elif field == "ai_description":
                changes.append("Description updated")
            elif field == "due_date":
                if data[field]:
                    changes.append(f"Due date → {data[field][:10]}")
                else:
                    changes.append("Due date cleared")
        if field in data:
            todo[field] = data[field]

    action_label = "  ·  ".join(changes) if changes else "TODO Updated"
    todo["updated_at"] = datetime.datetime.utcnow().isoformat()

    # Archive if done
    if todo.get("status") == "done":
        todos = [t for t in todos if t["id"] != todo_id]
        archive, arch_sha = gh_read(FILE_TODOS_ARCHIVE, force=True)
        archive = archive or []
        archive.append(todo)
        gh_write(FILE_TODOS_ARCHIVE, archive, arch_sha, f"Web: Archive TODO #{todo_id}")

    gh_write(FILE_TODOS, todos, sha, f"Web: Update TODO #{todo_id}")

    # If this todo is linked to a forum post, push updated info back immediately
    linked_thread_id = todo.get("linked_forum_thread_id")
    linked_type      = todo.get("linked_forum_type", "bugs")
    if linked_thread_id:
        _patch_forum_post_todo_link(
            thread_id  = str(linked_thread_id),
            forum_type = linked_type,
            todo_id    = todo_id,
            todo_info  = {
                "id":       todo_id,
                "title":    todo.get("title", ""),
                "status":   todo.get("status", "todo"),
                "priority": todo.get("priority", "medium"),
            },
        )

    notify_bot_board()
    web_user = get_session().get("user", {})
    _web_log_activity(action_label, todo, web_user)
    return jsonify(enrich_todo(todo))

@app.route("/api/todo/<int:todo_id>", methods=["DELETE"])
def api_todo_delete(todo_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    # Grab todo before deleting so we can unlink from forum post
    todo = next((t for t in todos if t["id"] == todo_id), None)
    original = len(todos)
    todos = [t for t in todos if t["id"] != todo_id]
    if len(todos) == original:
        return jsonify({"error": "Not found"}), 404
    gh_write(FILE_TODOS, todos, sha, f"Web: Delete TODO #{todo_id}")

    # Clear the link from the forum post if one existed
    if todo:
        linked_thread_id = todo.get("linked_forum_thread_id")
        linked_type      = todo.get("linked_forum_type", "bugs")
        if linked_thread_id:
            # Remove from forum_links.json
            links, links_sha = gh_read(FILE_FORUM_LINKS, force=True)
            links = links or {}
            links.pop(str(linked_thread_id), None)
            gh_write(FILE_FORUM_LINKS, links, links_sha,
                     f"Web: Unlink forum thread {linked_thread_id} (TODO #{todo_id} deleted)")
            # Clear from forum_posts.json
            _patch_forum_post_todo_link(
                thread_id  = str(linked_thread_id),
                forum_type = linked_type,
                todo_id    = None,   # None = clear the link
                todo_info  = None,
            )

    notify_bot_board()
    if todo:
        web_user = get_session().get("user", {})
        _web_log_activity("TODO Deleted", todo, web_user)
    return jsonify({"ok": True})

@app.route("/api/forum/<forum_type>/link", methods=["POST"])
def api_forum_link_todo(forum_type):
    """
    Link an existing TODO to a forum post (or unlink by passing todo_id=null).
    Body: { thread_id, todo_id }   (todo_id can be null to unlink)
    """
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    data      = request.json or {}
    thread_id = str(data.get("thread_id", "")).strip()
    todo_id   = data.get("todo_id")  # int or null

    if not thread_id:
        return jsonify({"error": "thread_id required"}), 400

    # Validate the todo exists if linking (not unlinking)
    todo_info = None
    if todo_id is not None:
        todos, _ = gh_read(FILE_TODOS)
        todos    = todos or []
        todo     = next((t for t in todos if t["id"] == int(todo_id)), None)
        if not todo:
            # Check archive too
            archive, _ = gh_read(FILE_TODOS_ARCHIVE)
            todo = next((t for t in (archive or []) if t["id"] == int(todo_id)), None)
        if not todo:
            return jsonify({"error": f"TODO #{todo_id} not found"}), 404
        todo_id   = int(todo_id)
        todo_info = {
            "id":       todo_id,
            "title":    todo.get("title", ""),
            "status":   todo.get("status", "todo"),
            "priority": todo.get("priority", "medium"),
        }
        # Also write back linked_forum_thread_id onto the todo itself
        todos_rw, todos_sha = gh_read(FILE_TODOS, force=True)
        todos_rw = todos_rw or []
        for t in todos_rw:
            if t["id"] == todo_id:
                t["linked_forum_thread_id"] = thread_id
                t["linked_forum_type"]      = forum_type
                t["updated_at"]             = datetime.datetime.utcnow().isoformat()
                break
        gh_write(FILE_TODOS, todos_rw, todos_sha,
                 f"Web: Link TODO #{todo_id} → forum {forum_type} thread {thread_id}")

    # Update forum_links.json
    links, links_sha = gh_read(FILE_FORUM_LINKS, force=True)
    links = links or {}
    if todo_id is not None:
        links[thread_id] = todo_id
    else:
        links.pop(thread_id, None)
    gh_write(FILE_FORUM_LINKS, links, links_sha,
             f"Web: {'Link' if todo_id else 'Unlink'} forum thread {thread_id}")

    # Patch forum_posts.json immediately
    _patch_forum_post_todo_link(
        thread_id  = thread_id,
        forum_type = forum_type,
        todo_id    = todo_id,
        todo_info  = todo_info,
    )

    notify_bot_board()
    # Log the link/unlink action
    if todo_info:
        web_user = get_session().get("user", {})
        link_todo_obj = {"id": todo_id, "title": todo_info.get("title",""), "status": todo_info.get("status","todo")}
        action = f"Linked to {forum_type} forum post #{thread_id}"
        _web_log_activity(action, link_todo_obj, web_user)
    elif todo_id is None:
        web_user = get_session().get("user", {})
        _web_log_activity(f"Unlinked from {forum_type} forum post #{thread_id}", {"id": "?", "title": "Unknown", "status": "todo"}, web_user)
    return jsonify({"ok": True, "linked_todo_id": todo_id, "linked_todo_info": todo_info})

@app.route("/api/config", methods=["POST"])
def api_config_save():
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    allowed = ("prefix", "reminder_days", "reminder_time", "todo_style")
    changes = []
    for k in allowed:
        if k in data:
            if data[k] != cfg.get(k):
                changes.append(f"{k} → {data[k]}")
            cfg[k] = data[k]
    ok = gh_write(FILE_CONFIG, cfg, sha, "Web: Config updated")
    if ok and changes:
        web_user = get_session().get("user", {})
        fake_todo = {"id": "—", "title": "Bot Config", "status": "todo"}
        _web_log_activity("⚙️ Settings Updated", fake_todo, web_user, extra="  ·  ".join(changes))
    return jsonify({"ok": ok})

@app.route("/api/me")
def api_me():
    return jsonify({
        "user": get_session().get("user"),
        "access_level": get_session().get("access_level", "public"),
        "member": get_session().get("member"),
    })

# ══════════════════════════════════════════════════════════════════════════════
# MEMBER DB  —  members.json stored on GitHub
#
# Schema of members.json:
# {
#   "last_synced": "2026-04-01T10:00:00",
#   "guild_id": "123456789",
#   "roles": {                          ← all guild roles for display
#     "role_id": { "name": "Dev Team", "color": "#e74c3c", "position": 5 }
#   },
#   "members": {
#     "user_id": {
#       "id":           "123",
#       "username":     "sheby",
#       "global_name":  "Sheby",
#       "nick":         "Sheby Dev",           ← server nickname
#       "display_name": "Sheby Dev",           ← nick > global_name > username
#       "avatar_url":   "https://cdn.discordapp.com/...",
#       "roles":        ["role_id_1", "role_id_2"],
#       "role_names":   ["Dev Team", "Moderator"],
#       "is_admin":     false,
#       "is_todo_role": true,                  ← has at least one configured todo role
#       "joined_at":    "2024-01-15T08:30:00",
#       "synced_at":    "2026-04-01T10:00:00"
#     }
#   }
# }
#
# Sync is triggered:
#   - Background thread on first /api/members/search call if DB is stale/empty
#   - POST /api/members/sync  (admin only — manual force)
#   - Automatically after login (updates the logged-in user's own record)
# ══════════════════════════════════════════════════════════════════════════════

MEMBERS_SYNC_TTL = 30 * 60   # re-sync from Discord every 30 minutes
_members_syncing = False      # simple flag to avoid parallel syncs


def _build_avatar_url(uid: str, avatar_hash: str | None) -> str:
    if avatar_hash:
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=128"
    idx = (int(uid) >> 22) % 6 if uid and uid.isdigit() else 0
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _sync_members_to_db():
    """
    Full guild member sync → writes to members.json on GitHub.
    - Fetches all guild roles first (for name/color lookup)
    - Paginates /guilds/{id}/members (1000/page) until done
    - Stores rich member records with role names, todo-role flag, etc.
    - Runs in a background thread; never blocks a request.
    """
    global _members_syncing
    if _members_syncing:
        return
    _members_syncing = True
    try:
        _sync_members_to_db_inner()
    except Exception as e:
        print(f"[member_sync] Error: {e}")
    finally:
        _members_syncing = False


def _sync_members_to_db_inner():
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID:
        return

    # 1. Fetch all guild roles for name/color lookup
    raw_roles = bot_get(f"/guilds/{DISCORD_GUILD_ID}/roles") or []
    roles_map = {}
    for r in raw_roles:
        rid = str(r.get("id", ""))
        if rid:
            # Discord stores color as an int; convert to hex string
            color_int = r.get("color", 0)
            color_hex = f"#{color_int:06x}" if color_int else None
            roles_map[rid] = {
                "name":     r.get("name", rid),
                "color":    color_hex,
                "position": r.get("position", 0),
                "hoist":    r.get("hoist", False),   # shown separately in member list
            }

    # 2. Load current config so we know which roles are "todo roles"
    cfg, _ = gh_read(FILE_CONFIG)
    todo_roles_set = set(str(r) for r in (cfg or {}).get("todo_roles", []))

    # 3. Paginate through all guild members
    raw_members = []
    after = 0
    while True:
        page = bot_get(f"/guilds/{DISCORD_GUILD_ID}/members?limit=1000&after={after}")
        if not page:
            break
        raw_members.extend(page)
        if len(page) < 1000:
            break
        after = max(int((m.get("user") or {}).get("id", "0")) for m in page)

    # 4. Build the members dict
    now_iso = datetime.datetime.utcnow().isoformat()
    members_out = {}
    for m in raw_members:
        user = m.get("user") or {}
        if user.get("bot"):
            continue
        uid         = str(user.get("id", ""))
        if not uid:
            continue
        username    = user.get("username", "")
        global_name = user.get("global_name") or ""
        nick        = m.get("nick") or ""
        display_name = nick or global_name or username
        avatar_url  = _build_avatar_url(uid, user.get("avatar"))
        member_role_ids = [str(r) for r in (m.get("roles") or [])]
        role_names  = [roles_map[r]["name"] for r in member_role_ids if r in roles_map]
        perms       = int(m.get("permissions", 0) or 0)
        is_admin    = bool(perms & 0x8)
        is_todo     = is_admin or bool(todo_roles_set and set(member_role_ids) & todo_roles_set)

        members_out[uid] = {
            "id":           uid,
            "username":     username,
            "global_name":  global_name,
            "nick":         nick,
            "display_name": display_name,
            "avatar_url":   avatar_url,
            "roles":        member_role_ids,      # list of role ID strings
            "role_names":   role_names,           # human names for display
            "is_admin":     is_admin,
            "is_todo_role": is_todo,
            "joined_at":    m.get("joined_at", ""),
            "synced_at":    now_iso,
        }

    # 5. Write to GitHub
    db = {
        "last_synced": now_iso,
        "guild_id":    DISCORD_GUILD_ID,
        "roles":       roles_map,
        "members":     members_out,
    }
    existing, sha = gh_read(FILE_MEMBERS, force=True)
    gh_write(FILE_MEMBERS, db, sha, f"Members: sync {len(members_out)} members")

    # Also write a small todo_members.json with only todo-role members
    todo_only = [m for m in members_out.values() if m.get("is_todo_role")]
    todo_db = {
        "last_synced": now_iso,
        "guild_id":    DISCORD_GUILD_ID,
        "members":     todo_only,
        "total":       len(members_out),
    }
    existing_todo, sha_todo = gh_read(FILE_TODO_MEMBERS, force=True)
    gh_write(FILE_TODO_MEMBERS, todo_db, sha_todo, f"Members: sync {len(todo_only)} todo members")
    print(f"[member_sync] Synced {len(members_out)} members, {len(todo_only)} todo-role, {len(roles_map)} roles")


def _upsert_member(user_obj: dict, member_obj: dict):
    """
    Update a single member's record in members.json right after they log in.
    Keeps the DB fresh for active users without waiting for the next full sync.
    Runs in a background thread.
    """
    def _write():
        try:
            uid = str(user_obj.get("id", ""))
            if not uid:
                return
            cfg, _ = gh_read(FILE_CONFIG)
            todo_roles_set = set(str(r) for r in (cfg or {}).get("todo_roles", []))

            db, sha = gh_read(FILE_MEMBERS, force=True)
            db = db or {"last_synced": "", "guild_id": DISCORD_GUILD_ID,
                        "roles": {}, "members": {}}
            roles_map  = db.get("roles") or {}
            members    = db.get("members") or {}

            username    = user_obj.get("username", "")
            global_name = user_obj.get("global_name") or ""
            nick        = (member_obj or {}).get("nick") or ""
            display_name = nick or global_name or username
            avatar_url  = _build_avatar_url(uid, user_obj.get("avatar"))
            member_role_ids = [str(r) for r in ((member_obj or {}).get("roles") or [])]
            role_names  = [roles_map[r]["name"] for r in member_role_ids if r in roles_map]
            perms       = int((member_obj or {}).get("permissions", 0) or 0)
            is_admin    = bool(perms & 0x8)
            is_todo     = is_admin or bool(todo_roles_set and set(member_role_ids) & todo_roles_set)

            members[uid] = {
                "id":           uid,
                "username":     username,
                "global_name":  global_name,
                "nick":         nick,
                "display_name": display_name,
                "avatar_url":   avatar_url,
                "roles":        member_role_ids,
                "role_names":   role_names,
                "is_admin":     is_admin,
                "is_todo_role": is_todo,
                "joined_at":    (member_obj or {}).get("joined_at", ""),
                "synced_at":    datetime.datetime.utcnow().isoformat(),
            }
            db["members"] = members
            gh_write(FILE_MEMBERS, db, sha, f"Members: upsert {username}")

            # Keep todo_members.json in sync too
            if is_todo:
                todo_db, todo_sha = gh_read(FILE_TODO_MEMBERS, force=True)
                todo_db = todo_db or {"last_synced": "", "guild_id": DISCORD_GUILD_ID, "members": [], "total": 0}
                todo_list = todo_db.get("members") or []
                # Update or add this member
                todo_list = [m for m in todo_list if m.get("id") != uid]
                todo_list.append(members[uid])
                todo_db["members"] = todo_list
                todo_db["last_synced"] = datetime.datetime.utcnow().isoformat()
                gh_write(FILE_TODO_MEMBERS, todo_db, todo_sha, f"Members: upsert todo {username}")
        except Exception as e:
            print(f"[member_upsert] Failed: {e}")
    _threading.Thread(target=_write, daemon=True).start()


def _get_todo_members_db() -> dict:
    """
    Return the todo_members DB (small file, always readable).
    Triggers a background sync if stale or missing.
    """
    db, _ = gh_read(FILE_TODO_MEMBERS)
    db = db or {}
    last_synced = db.get("last_synced", "")
    needs_sync = True
    if last_synced:
        try:
            age = (datetime.datetime.utcnow() -
                   datetime.datetime.fromisoformat(last_synced)).total_seconds()
            needs_sync = age > MEMBERS_SYNC_TTL
        except Exception:
            needs_sync = True
    if needs_sync and DISCORD_BOT_TOKEN and not _members_syncing:
        _threading.Thread(target=_sync_members_to_db, daemon=True).start()
    return db


@app.route("/api/members/search")
def api_members_search():
    """
    Search todo-role members from todo_members.json (small file, always readable).
    Zero Discord API calls — purely a local JSON lookup.

    ?q=<query>  — matches username, global_name, nick (display_name)
    Access: manager, admin, owner only.
    """
    sess = get_session()
    if sess.get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify([])

    db = _get_todo_members_db()
    members = db.get("members") or []

    if not members:
        return jsonify({"syncing": True, "results": []}), 202

    results = []
    for m in members:
        searchable = f"{m.get('username','')} {m.get('global_name','')} {m.get('nick','')}".lower()
        if q not in searchable:
            continue
        results.append({
            "id":           m["id"],
            "username":     m["username"],
            "display_name": m["display_name"],
            "avatar_url":   m["avatar_url"],
            "role_names":   m.get("role_names", []),
            "is_admin":     m.get("is_admin", False),
            "is_manager":   True,
        })
        if len(results) >= 15:
            break

    return jsonify(results)


@app.route("/api/members")
def api_members_list():
    """
    Return all todo-role members from todo_members.json (small file, always readable).
    Used by the settings page to display the full assignable team.
    Access: manager, admin, owner only.
    """
    sess = get_session()
    if sess.get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    try:
        db = _get_todo_members_db()
        todo_members = db.get("members") or []
        todo_members = sorted(todo_members, key=lambda m: m.get("display_name", "").lower())

        return jsonify({
            "members":     todo_members,
            "last_synced": db.get("last_synced", ""),
            "total":       db.get("total", 0),
        })
    except Exception as e:
        print(f"[api_members_list] Error: {e}")
        return jsonify({"error": "Failed to load members — GitHub may be temporarily unavailable. Try again in a moment."}), 503


@app.route("/api/members/sync", methods=["POST"])
def api_members_sync():
    """Force a full member sync from Discord. Admin only."""
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID:
        return jsonify({"error": "Bot token or guild ID not configured"}), 503
    _threading.Thread(target=_sync_members_to_db, daemon=True).start()
    return jsonify({"ok": True, "message": "Member sync started in background"})


@app.route("/api/todo/<int:todo_id>/assign", methods=["POST"])
def api_todo_assign(todo_id):
    """
    Self-assign / unassign endpoint for users with the TODO role (manager).
    Body: { action: "assign" | "unassign" }
    Managers can only assign/unassign THEMSELVES.
    Admins/owners can pass assignee_id to assign anyone.
    """
    sess  = get_session()
    level = sess.get("access_level")
    if level not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    data   = request.json or {}
    action = data.get("action", "assign")   # "assign" or "unassign"
    me     = sess.get("user", {})
    me_id  = str(me.get("id", ""))

    # Managers can only assign themselves; admins/owners can assign anyone
    if level == "manager":
        assignee_id   = me_id
        assignee_name = me.get("global_name") or me.get("username") or "Unknown"
    else:
        assignee_id   = str(data.get("assignee_id", me_id))
        assignee_name = data.get("assignee_name") or assignee_id

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404

    if action == "unassign":
        old_name = todo.get("assigned_to_name") or todo.get("assigned_to_id") or "Unassigned"
        todo["assigned_to_id"]   = None
        todo["assigned_to_name"] = None
        action_label = f"Unassigned (was {old_name})"
    else:
        todo["assigned_to_id"]   = assignee_id
        todo["assigned_to_name"] = assignee_name
        action_label = f"Assigned → {assignee_name}"

    todo["updated_at"] = datetime.datetime.utcnow().isoformat()

    gh_write(FILE_TODOS, todos, sha,
             f"Web: {action_label} for TODO #{todo_id} by {me.get('username','?')}")
    notify_bot_board()
    _web_log_activity(action_label, todo, me)
    return jsonify({"ok": True, "todo": enrich_todo(todo)})

@app.route("/api/forum/sync", methods=["POST"])
def api_forum_sync():
    """Force an immediate sync of both forum channels. Admin only."""
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    if DISCORD_BUGS_CHANNEL_ID:
        t1 = _threading.Thread(target=_sync_forum_to_db, args=(DISCORD_BUGS_CHANNEL_ID, "bugs"), daemon=True)
        t1.start()
    if DISCORD_SUGGESTIONS_CHANNEL_ID:
        t2 = _threading.Thread(target=_sync_forum_to_db, args=(DISCORD_SUGGESTIONS_CHANNEL_ID, "suggestions"), daemon=True)
        t2.start()
    return jsonify({"ok": True, "message": "Sync started in background"})

# ── Forum pages ──────────────────────────────────────────────────────────────

@app.route("/bugs")
def bugs_page():
    posts, error = [], None
    channel_id_configured = bool(DISCORD_BUGS_CHANNEL_ID)
    channel_name = "bugs"
    syncing = False
    if channel_id_configured:
        posts, error = fetch_forum_posts(DISCORD_BUGS_CHANNEL_ID, "bugs")
        syncing = (not posts and not error)
    all_tags = sorted(set(tag for p in posts for tag in (p.get("tags") or [])))
    return render_template("bugs.html",
        posts=posts, error=error,
        channel_name=channel_name,
        channel_id_configured=channel_id_configured,
        syncing=syncing,
        all_tags=all_tags,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

@app.route("/suggestions")
def suggestions_page():
    posts, error = [], None
    channel_id_configured = bool(DISCORD_SUGGESTIONS_CHANNEL_ID)
    channel_name = "suggestions"
    syncing = False
    if channel_id_configured:
        posts, error = fetch_forum_posts(DISCORD_SUGGESTIONS_CHANNEL_ID, "suggestions")
        syncing = (not posts and not error)
    all_tags = sorted(set(tag for p in posts for tag in (p.get("tags") or [])))
    return render_template("suggestions.html",
        posts=posts, error=error,
        channel_name=channel_name,
        channel_id_configured=channel_id_configured,
        syncing=syncing,
        all_tags=all_tags,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

# ── Forum API endpoints ───────────────────────────────────────────────────────

@app.route("/api/forum/bugs/<thread_id>")
def api_forum_bug_thread(thread_id):
    detail, error = fetch_forum_thread_detail(thread_id, forum_type="bugs")
    if error:
        return jsonify({"error": error}), 500
    return jsonify(detail)

@app.route("/api/forum/suggestions/<thread_id>")
def api_forum_suggestion_thread(thread_id):
    detail, error = fetch_forum_thread_detail(thread_id, forum_type="suggestions")
    if error:
        return jsonify({"error": error}), 500
    return jsonify(detail)


@app.route("/activity")
@login_required
def activity():
    log, _ = gh_read(FILE_ACTIVITY_LOG)
    log    = log or []
    # Enrich each entry with display helpers
    enriched = []
    for e in log[:100]:
        e = dict(e)
        status = e.get("todo_status", "todo")
        e["status_color"] = STATUS_COLORS.get(status, "#5865F2")
        e["status_label"] = STATUS_LABELS.get(status, status)
        # Human-friendly timestamp
        try:
            dt = datetime.datetime.fromisoformat(e["ts"].rstrip("Z"))
            now = datetime.datetime.utcnow()
            delta = int((now - dt).total_seconds())
            if delta < 60:
                e["ts_fmt"] = "just now"
            elif delta < 3600:
                e["ts_fmt"] = f"{delta // 60}m ago"
            elif delta < 86400:
                e["ts_fmt"] = f"{delta // 3600}h ago"
            else:
                e["ts_fmt"] = dt.strftime("%b %d")
        except Exception:
            e["ts_fmt"] = e.get("ts", "")[:10]
        enriched.append(e)
    return render_template("activity.html",
        log=enriched,
        user=get_session()["user"],
        access_level=get_session().get("access_level"),
    )


@app.route("/api/todos/bulk", methods=["POST"])
def api_todos_bulk():
    """
    Bulk update a set of TODOs.
    Body: { ids: [int, ...], patch: { status?, assigned_to_id?, assigned_to_name? } }
    Access: manager, admin, owner.
    """
    sess  = get_session()
    level = sess.get("access_level")
    if level not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    data  = request.json or {}
    ids   = [int(i) for i in (data.get("ids") or [])]
    patch = data.get("patch") or {}
    if not ids:
        return jsonify({"error": "No IDs provided"}), 400

    # Managers can only self-assign in bulk
    me = sess.get("user", {})
    if level == "manager" and "assigned_to_id" in patch:
        patch["assigned_to_id"]   = str(me.get("id", ""))
        patch["assigned_to_name"] = me.get("global_name") or me.get("username") or "Unknown"

    allowed_patch = ("status", "priority", "assigned_to_id", "assigned_to_name")
    patch = {k: v for k, v in patch.items() if k in allowed_patch}
    if not patch:
        return jsonify({"error": "Nothing to update"}), 400

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []

    archive_new = []
    updated_ids = []
    changes_desc = "  ·  ".join(
        f"{k.replace('_',' ')} → {v}" for k, v in patch.items()
        if k not in ("assigned_to_name",)
    )

    for t in todos:
        if t["id"] in ids:
            for k, v in patch.items():
                t[k] = v
            t["updated_at"] = datetime.datetime.utcnow().isoformat()
            updated_ids.append(t["id"])

    # Archive any newly-done ones
    if patch.get("status") == "done":
        archive, arch_sha = gh_read(FILE_TODOS_ARCHIVE, force=True)
        archive = archive or []
        done_todos = [t for t in todos if t["id"] in ids]
        todos     = [t for t in todos if t["id"] not in ids]
        archive.extend(done_todos)
        gh_write(FILE_TODOS_ARCHIVE, archive, arch_sha,
                 f"Web: Bulk archive {len(done_todos)} TODOs")

    gh_write(FILE_TODOS, todos, sha,
             f"Web: Bulk update {len(updated_ids)} TODOs by {me.get('username','?')}")
    notify_bot_board()

    # Log one summary entry
    fake_todo = {"id": f"{len(updated_ids)} items", "title": f"Bulk: {changes_desc}", "status": patch.get("status","todo")}
    _web_log_activity(f"Bulk Update ({len(updated_ids)} TODOs)", fake_todo, me, extra=changes_desc)

    return jsonify({"ok": True, "updated": updated_ids})

# ══════════════════════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
        message="You don't have permission to view this page.",
        user=get_session().get("user"), access_level=get_session().get("access_level", "public")), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404,
        message="Page not found.",
        user=get_session().get("user"), access_level=get_session().get("access_level", "public")), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500,
        message="Something went wrong on our end. Please try again in a moment.",
        user=get_session().get("user"), access_level=get_session().get("access_level", "public")), 500

# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #1 — TODO COMMENTS
# Schema: { "todo_id": [{ id, user_id, user_name, user_avatar, text, ts }] }
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todo/<int:todo_id>/comments", methods=["GET"])
def api_comments_get(todo_id):
    comments, _ = gh_read(FILE_COMMENTS)
    comments = comments or {}
    return jsonify(comments.get(str(todo_id), []))

@app.route("/api/todo/<int:todo_id>/comments", methods=["POST"])
def api_comments_post(todo_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    data = request.json or {}
    text = (data.get("text") or "").strip()[:1000]
    if not text:
        return jsonify({"error": "Text required"}), 400

    comments, sha = gh_read(FILE_COMMENTS, force=True)
    comments = comments or {}
    key = str(todo_id)
    thread = comments.get(key, [])
    user = sess["user"]
    new_comment = {
        "id":          secrets.token_hex(8),
        "user_id":     str(user.get("id", "")),
        "user_name":   user.get("global_name") or user.get("username") or "Member",
        "user_avatar": _avatar_url(user),
        "text":        text,
        "ts":          datetime.datetime.utcnow().isoformat() + "Z",
    }
    thread.append(new_comment)
    comments[key] = thread
    ok = gh_write(FILE_COMMENTS, comments, sha, f"Comment on TODO #{todo_id} by {user.get('username','?')}")
    if ok:
        # Log to activity
        todos, _ = gh_read(FILE_TODOS)
        todo = next((t for t in (todos or []) if t["id"] == todo_id), {"id": todo_id, "title": "Unknown", "status": "todo"})
        _web_log_activity("💬 Comment Added", todo, user, extra=text[:60])
    return jsonify(new_comment), 201

@app.route("/api/todo/<int:todo_id>/comments/<comment_id>", methods=["DELETE"])
def api_comments_delete(todo_id, comment_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    user = sess["user"]
    level = sess.get("access_level", "public")

    comments, sha = gh_read(FILE_COMMENTS, force=True)
    comments = comments or {}
    key = str(todo_id)
    thread = comments.get(key, [])
    comment = next((c for c in thread if c["id"] == comment_id), None)
    if not comment:
        return jsonify({"error": "Not found"}), 404
    # Only author or manager+ can delete
    if comment["user_id"] != str(user.get("id")) and level not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    thread = [c for c in thread if c["id"] != comment_id]
    comments[key] = thread
    gh_write(FILE_COMMENTS, comments, sha, f"Delete comment on TODO #{todo_id}")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #2 — TODO WATCHERS
# Stored as "watchers": ["user_id", ...] on each todo in todos.json
# Bot DMs watchers when status changes (handled in bot.py via notify endpoint)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todo/<int:todo_id>/watch", methods=["POST"])
def api_todo_watch(todo_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    user = sess["user"]
    uid = str(user.get("id", ""))

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404

    action = request.json.get("action", "watch") if request.json else "watch"
    watchers = todo.get("watchers", [])
    if action == "unwatch":
        watchers = [w for w in watchers if w != uid]
        msg = "unwatched"
    else:
        if uid not in watchers:
            watchers.append(uid)
        msg = "watching"
    todo["watchers"] = watchers
    gh_write(FILE_TODOS, todos, sha, f"TODO #{todo_id} {msg} by {user.get('username','?')}")
    return jsonify({"ok": True, "action": msg, "watchers": watchers})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #3 — SAVED FILTERS
# Schema: { "user_id": [{ id, name, params: {status,priority,tag,assignee,due,q} }] }
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/filters", methods=["GET"])
def api_filters_get():
    sess = get_session()
    if "user" not in sess:
        return jsonify([])
    uid = str(sess["user"].get("id", ""))
    saved, _ = gh_read(FILE_SAVED_FILTERS)
    return jsonify((saved or {}).get(uid, []))

@app.route("/api/filters", methods=["POST"])
def api_filters_save():
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    user = sess["user"]
    uid = str(user.get("id", ""))
    data = request.json or {}
    name = (data.get("name") or "").strip()[:50]
    params = data.get("params") or {}
    if not name:
        return jsonify({"error": "Name required"}), 400

    saved, sha = gh_read(FILE_SAVED_FILTERS, force=True)
    saved = saved or {}
    user_filters = saved.get(uid, [])
    new_filter = {
        "id":     secrets.token_hex(6),
        "name":   name,
        "params": params,
        "ts":     datetime.datetime.utcnow().isoformat() + "Z",
    }
    user_filters.append(new_filter)
    user_filters = user_filters[-20:]  # max 20 per user
    saved[uid] = user_filters
    gh_write(FILE_SAVED_FILTERS, saved, sha, f"Saved filter '{name}' for {user.get('username','?')}")
    return jsonify(new_filter), 201

@app.route("/api/filters/<filter_id>", methods=["DELETE"])
def api_filters_delete(filter_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    uid = str(sess["user"].get("id", ""))
    saved, sha = gh_read(FILE_SAVED_FILTERS, force=True)
    saved = saved or {}
    user_filters = [f for f in saved.get(uid, []) if f["id"] != filter_id]
    saved[uid] = user_filters
    gh_write(FILE_SAVED_FILTERS, saved, sha, f"Delete filter {filter_id}")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #4 — TODO TEMPLATES (stored in config.json under "templates")
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/templates", methods=["GET"])
def api_templates_get():
    cfg, _ = gh_read(FILE_CONFIG)
    return jsonify((cfg or {}).get("templates", []))

@app.route("/api/templates", methods=["POST"])
def api_templates_save():
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    name = (data.get("name") or "").strip()[:50]
    if not name:
        return jsonify({"error": "Name required"}), 400
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    templates = cfg.get("templates", [])
    new_tpl = {
        "id":          secrets.token_hex(6),
        "name":        name,
        "title":       data.get("title", ""),
        "description": data.get("description", ""),
        "priority":    data.get("priority", "medium"),
        "tags":        data.get("tags", []),
    }
    templates.append(new_tpl)
    cfg["templates"] = templates
    gh_write(FILE_CONFIG, cfg, sha, f"Add template '{name}'")
    return jsonify(new_tpl), 201

@app.route("/api/templates/<tpl_id>", methods=["DELETE"])
def api_templates_delete(tpl_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    cfg["templates"] = [t for t in cfg.get("templates", []) if t["id"] != tpl_id]
    gh_write(FILE_CONFIG, cfg, sha, f"Delete template {tpl_id}")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #5 — WEEKLY DIGEST (stored: config.json "last_digest_at")
# POST /api/digest/send  — admin triggers manual digest; auto via bot
# GET  /api/digest/preview — returns this week's completion stats
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/digest/preview", methods=["GET"])
@login_required
def api_digest_preview():
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    archive = archive or []
    now = datetime.datetime.utcnow()
    week_ago = (now - datetime.timedelta(days=7)).isoformat()
    this_week = [t for t in archive if (t.get("done_at") or t.get("updated_at", "")) >= week_ago]
    from collections import Counter
    completers = Counter(t.get("done_by_id") for t in this_week if t.get("done_by_id"))
    return jsonify({
        "week_completed": len(this_week),
        "total_archive":  len(archive),
        "top_completers": dict(completers.most_common(5)),
        "items":          [{"id": t["id"], "title": t.get("title",""), "done_at": t.get("done_at","")} for t in this_week[:20]],
    })


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #6 — TIME TRACKING
# Stored as "time_logs": [{ user_id, start, end, minutes }] on each TODO in todos.json
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todo/<int:todo_id>/time", methods=["POST"])
def api_time_log(todo_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    if sess.get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    minutes = int(data.get("minutes", 0))
    if minutes < 1 or minutes > 1440:
        return jsonify({"error": "Minutes must be 1–1440"}), 400
    user = sess["user"]

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        # Check archive
        archive, arch_sha = gh_read(FILE_TODOS_ARCHIVE, force=True)
        todo = next((t for t in (archive or []) if t["id"] == todo_id), None)
        if not todo:
            return jsonify({"error": "Not found"}), 404
        logs = todo.get("time_logs", [])
        logs.append({
            "user_id":   str(user.get("id","")),
            "user_name": user.get("global_name") or user.get("username","?"),
            "start":     data.get("start", datetime.datetime.utcnow().isoformat() + "Z"),
            "end":       datetime.datetime.utcnow().isoformat() + "Z",
            "minutes":   minutes,
        })
        todo["time_logs"] = logs
        gh_write(FILE_TODOS_ARCHIVE, archive, arch_sha, f"Time log TODO #{todo_id}")
        return jsonify({"ok": True, "total_minutes": sum(l.get("minutes",0) for l in logs)})

    logs = todo.get("time_logs", [])
    logs.append({
        "user_id":   str(user.get("id","")),
        "user_name": user.get("global_name") or user.get("username","?"),
        "start":     data.get("start", datetime.datetime.utcnow().isoformat() + "Z"),
        "end":       datetime.datetime.utcnow().isoformat() + "Z",
        "minutes":   minutes,
    })
    todo["time_logs"] = logs
    gh_write(FILE_TODOS, todos, sha, f"Time log TODO #{todo_id}")
    return jsonify({"ok": True, "total_minutes": sum(l.get("minutes",0) for l in logs)})

@app.route("/api/todo/<int:todo_id>/time", methods=["GET"])
def api_time_get(todo_id):
    todos, _ = gh_read(FILE_TODOS)
    todo = next((t for t in (todos or []) if t["id"] == todo_id), None)
    if not todo:
        archive, _ = gh_read(FILE_TODOS_ARCHIVE)
        todo = next((t for t in (archive or []) if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404
    logs = todo.get("time_logs", [])
    return jsonify({"logs": logs, "total_minutes": sum(l.get("minutes",0) for l in logs)})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #7 — BURNDOWN / VELOCITY  (derived from archive timestamps)
# GET /api/analytics/burndown?weeks=8
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/analytics/burndown")
@login_required
def api_burndown():
    weeks = min(int(request.args.get("weeks", 8)), 26)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    archive = archive or []
    now = datetime.datetime.utcnow()
    result = []
    for i in range(weeks - 1, -1, -1):
        week_start = now - datetime.timedelta(weeks=i+1)
        week_end   = now - datetime.timedelta(weeks=i)
        count = sum(
            1 for t in archive
            if week_start.isoformat() <= (t.get("done_at") or t.get("updated_at","")) < week_end.isoformat()
        )
        result.append({
            "week":  week_end.strftime("%b %d"),
            "count": count,
        })
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #9 — FORUM UPVOTES
# "upvotes": {"user_id": true} stored in forum_posts_bugs/suggestions.json
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/forum/<forum_type>/<thread_id>/upvote", methods=["POST"])
def api_forum_upvote(forum_type, thread_id):
    sess = get_session()
    if "user" not in sess:
        return jsonify({"error": "Login required"}), 401
    uid = str(sess["user"].get("id", ""))
    db, sha = gh_read(_forum_file(forum_type), force=True)
    db = db or {}
    section = db.get(forum_type, {})
    posts = section.get("posts", [])
    for post in posts:
        if str(post.get("id")) == str(thread_id):
            upvotes = post.get("upvotes", {})
            if uid in upvotes:
                del upvotes[uid]
                action = "removed"
            else:
                upvotes[uid] = True
                action = "added"
            post["upvotes"] = upvotes
            section["posts"] = posts
            db[forum_type] = section
            gh_write(_forum_file(forum_type), db, sha, f"Upvote {action} on {forum_type} {thread_id}")
            return jsonify({"ok": True, "action": action, "count": len(upvotes)})
    return jsonify({"error": "Post not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #10 — CHANGELOG PAGE
# GET /changelog  — public page showing done TODOs grouped by week
# Admin can flag todos as public_changelog=true via PATCH /api/todo/<id>
# (public_changelog field already supported by the existing PATCH handler via allowed fields)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/changelog")
def changelog():
    # Also include releases
    releases, _ = gh_read(FILE_RELEASES)
    releases = sorted((releases or []), key=lambda r: r.get("date", ""), reverse=True)

    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    archive = archive or []
    public_todos = [enrich_todo(t) for t in archive if t.get("public_changelog")]

    # Group by week
    from collections import defaultdict
    weeks = defaultdict(list)
    for t in public_todos:
        done_at = t.get("done_at") or t.get("updated_at", "")
        try:
            dt = datetime.datetime.fromisoformat(done_at[:10])
            # Monday of that week
            monday = dt - datetime.timedelta(days=dt.weekday())
            key = monday.strftime("%Y-%m-%d")
        except Exception:
            key = "older"
        weeks[key].append(t)

    sorted_weeks = sorted(weeks.items(), reverse=True)
    return render_template("changelog.html",
        releases=releases,
        weeks=sorted_weeks,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #11 — ROLE-BASED TAG RESTRICTIONS
# config.json: "tag_permissions": { "urgent": ["admin"], "blocked": ["admin","manager"] }
# ══════════════════════════════════════════════════════════════════════════════

def _check_tag_permission(tag: str, access_level: str) -> bool:
    cfg, _ = gh_read(FILE_CONFIG)
    tag_perms = (cfg or {}).get("tag_permissions", {})
    allowed_roles = tag_perms.get(tag)
    if not allowed_roles:
        return True  # no restriction
    return access_level in allowed_roles

@app.route("/api/tag-permissions", methods=["GET"])
@admin_required
def api_tag_permissions_get():
    cfg, _ = gh_read(FILE_CONFIG)
    return jsonify((cfg or {}).get("tag_permissions", {}))

@app.route("/api/tag-permissions", methods=["POST"])
@admin_required
def api_tag_permissions_save():
    data = request.json or {}
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    cfg["tag_permissions"] = data
    gh_write(FILE_CONFIG, cfg, sha, "Update tag permissions")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #12 — GITHUB ISSUE IMPORT
# Sync open issues from RyanYuuki/AnymeX into github_issues.json
# Stored: [{ number, title, body, labels, state, assignees, created_at,
#            comment_count, html_url, linked_todo_id }]
# ══════════════════════════════════════════════════════════════════════════════

def _sync_github_issues():
    """Fetch open issues from ANYMEX_OWNER/ANYMEX_REPO and save to github_issues.json."""
    try:
        headers = gh_headers()  # reuse same GitHub token
        url = f"https://api.github.com/repos/{ANYMEX_OWNER}/{ANYMEX_REPO}/issues?state=open&per_page=100&sort=created&direction=desc"
        r = req.get(url, headers=headers, timeout=15)
        if not r.ok:
            print(f"[gh_issues] GitHub API error {r.status_code}")
            return
        raw_issues = r.json()

        # Load existing links
        links, _ = gh_read(FILE_ISSUE_LINKS)
        links = links or {}

        issues_out = []
        for issue in raw_issues:
            if issue.get("pull_request"):
                continue  # skip PRs
            num = issue.get("number")
            issues_out.append({
                "number":       num,
                "title":        issue.get("title", ""),
                "body":         (issue.get("body") or "")[:500],
                "labels":       [l.get("name","") for l in issue.get("labels", [])],
                "state":        issue.get("state", "open"),
                "assignees":    [a.get("login","") for a in issue.get("assignees", [])],
                "created_at":   issue.get("created_at", ""),
                "comment_count": issue.get("comments", 0),
                "html_url":     issue.get("html_url", ""),
                "linked_todo_id": links.get(str(num)),
                "platform":     _detect_platform(issue.get("title","") + " " + (issue.get("body") or "")),
            })

        db, sha = gh_read(FILE_GITHUB_ISSUES, force=True)
        new_db = {
            "last_synced": datetime.datetime.utcnow().isoformat(),
            "repo":        f"{ANYMEX_OWNER}/{ANYMEX_REPO}",
            "issues":      issues_out,
        }
        gh_write(FILE_GITHUB_ISSUES, new_db, sha, f"GitHub issues sync: {len(issues_out)} issues")
        print(f"[gh_issues] Synced {len(issues_out)} issues")
    except Exception as e:
        print(f"[gh_issues] Error: {e}")

def _detect_platform(text: str) -> str:
    """Detect platform from issue text."""
    text_lower = text.lower()
    for p in ("android", "ios", "windows", "linux", "macos", "desktop", "mobile"):
        if p in text_lower:
            return p
    return ""

def _get_github_issues(force=False):
    """Return github issues from DB, triggering background sync if stale."""
    db, _ = gh_read(FILE_GITHUB_ISSUES)
    db = db or {}
    last_synced = db.get("last_synced", "")
    needs_sync = True
    if last_synced and not force:
        try:
            age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_synced)).total_seconds()
            needs_sync = age > GITHUB_ISSUES_SYNC_TTL
        except Exception:
            needs_sync = True
    if needs_sync:
        _threading.Thread(target=_sync_github_issues, daemon=True).start()
    return db.get("issues", []), db.get("last_synced", "")

@app.route("/issues")
def issues_page():
    issues, last_synced = _get_github_issues()
    # Enrich with linked todo info
    links, _ = gh_read(FILE_ISSUE_LINKS)
    links = links or {}
    for issue in issues:
        issue["linked_todo_id"] = links.get(str(issue.get("number")), issue.get("linked_todo_id"))

    label_filter    = request.args.get("label", "all")
    platform_filter = request.args.get("platform", "all")
    search          = request.args.get("q", "").lower().strip()

    filtered = issues
    if label_filter != "all":
        filtered = [i for i in filtered if label_filter in i.get("labels", [])]
    if platform_filter != "all":
        filtered = [i for i in filtered if i.get("platform") == platform_filter]
    if search:
        filtered = [i for i in filtered if search in i.get("title","").lower() or search in i.get("body","").lower()]

    all_labels = sorted(set(l for i in issues for l in i.get("labels",[])))
    return render_template("issues.html",
        issues=filtered, all_issues=issues,
        all_labels=all_labels,
        label_filter=label_filter, platform_filter=platform_filter, search=search,
        last_synced=last_synced,
        repo=f"{ANYMEX_OWNER}/{ANYMEX_REPO}",
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

@app.route("/api/issues/sync", methods=["POST"])
@admin_required
def api_issues_sync():
    _threading.Thread(target=_sync_github_issues, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})

@app.route("/api/issues", methods=["GET"])
def api_issues_list():
    issues, last_synced = _get_github_issues()
    return jsonify({"issues": issues, "last_synced": last_synced})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #13 — GITHUB ISSUE → TODO LINK
# POST /api/issues/<number>/link  { todo_id } or { todo_id: null } to unlink
# POST /api/issues/<number>/import  — create a new TODO from this issue
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/issues/<int:issue_number>/link", methods=["POST"])
def api_issue_link(issue_number):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    todo_id = data.get("todo_id")

    links, links_sha = gh_read(FILE_ISSUE_LINKS, force=True)
    links = links or {}
    if todo_id is not None:
        links[str(issue_number)] = int(todo_id)
    else:
        links.pop(str(issue_number), None)
    gh_write(FILE_ISSUE_LINKS, links, links_sha, f"Link issue #{issue_number} → TODO #{todo_id}")

    # Also patch the github_issues DB record
    db, db_sha = gh_read(FILE_GITHUB_ISSUES, force=True)
    if db:
        for issue in (db.get("issues") or []):
            if issue.get("number") == issue_number:
                issue["linked_todo_id"] = todo_id
                break
        gh_write(FILE_GITHUB_ISSUES, db, db_sha, f"Issue link patch #{issue_number}")

    return jsonify({"ok": True})

@app.route("/api/issues/<int:issue_number>/import", methods=["POST"])
def api_issue_import(issue_number):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    # Check not already imported
    links, _ = gh_read(FILE_ISSUE_LINKS)
    if links and str(issue_number) in links:
        return jsonify({"error": f"Issue #{issue_number} already linked to TODO #{links[str(issue_number)]}"}), 409

    # Find the issue in DB
    db, _ = gh_read(FILE_GITHUB_ISSUES)
    issue = next((i for i in (db or {}).get("issues", []) if i.get("number") == issue_number), None)
    if not issue:
        return jsonify({"error": "Issue not found in DB — trigger a sync first"}), 404

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    next_id = max((t["id"] for t in todos), default=0) + 1
    user = get_session().get("user", {})

    new_todo = {
        "id":               next_id,
        "title":            issue.get("title","")[:120],
        "ai_description":   issue.get("body","")[:300],
        "status":           "todo",
        "priority":         "medium",
        "tags":             issue.get("labels", [])[:5],
        "assigned_to_id":   None,
        "due_date":         None,
        "added_by_id":      str(user.get("id", "")),
        "added_by":         user.get("username", "web"),
        "created_at":       datetime.datetime.utcnow().isoformat(),
        "updated_at":       datetime.datetime.utcnow().isoformat(),
        "auto_generated":   False,
        "source":           "github_issue",
        "github_issue_id":  issue_number,
        "github_issue_url": issue.get("html_url", ""),
        "watchers":         [],
    }
    todos.append(new_todo)
    gh_write(FILE_TODOS, todos, sha, f"Import GitHub issue #{issue_number} as TODO #{next_id}")

    # Link it
    links, links_sha = gh_read(FILE_ISSUE_LINKS, force=True)
    links = links or {}
    links[str(issue_number)] = next_id
    gh_write(FILE_ISSUE_LINKS, links, links_sha, f"Link issue #{issue_number} → TODO #{next_id}")

    notify_bot_board()
    _web_log_activity("TODO Created (GitHub Issue)", new_todo, user, extra=f"From issue #{issue_number}")
    return jsonify(enrich_todo(new_todo)), 201


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #14 — PLATFORM LABEL ON BUGS (stored in forum_posts_bugs.json)
# Already populated during sync via _detect_platform(); exposed via filter on /bugs
# ══════════════════════════════════════════════════════════════════════════════
# (No extra routes needed — /bugs page already accepts query params; platform
#  is populated on each post object during _sync_forum_to_db_inner)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #15 — TODO DEPENDENCY CHAINS
# Stored as "blocks": [id,...] and "blocked_by": [id,...] on each todo
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todo/<int:todo_id>/depends", methods=["POST"])
def api_todo_depends(todo_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    action     = data.get("action", "add")   # "add" or "remove"
    dep_id     = int(data.get("dep_id", 0))
    rel        = data.get("rel", "blocks")   # "blocks" or "blocked_by"

    if not dep_id:
        return jsonify({"error": "dep_id required"}), 400

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo     = next((t for t in todos if t["id"] == todo_id), None)
    dep_todo = next((t for t in todos if t["id"] == dep_id), None)
    if not todo or not dep_todo:
        return jsonify({"error": "TODO not found"}), 404

    key      = rel           # "blocks" or "blocked_by"
    rev_key  = "blocked_by" if rel == "blocks" else "blocks"

    lst      = todo.get(key, [])
    rev_lst  = dep_todo.get(rev_key, [])

    if action == "remove":
        lst     = [x for x in lst     if x != dep_id]
        rev_lst = [x for x in rev_lst if x != todo_id]
    else:
        if dep_id not in lst:     lst.append(dep_id)
        if todo_id not in rev_lst: rev_lst.append(todo_id)

    todo[key]         = lst
    dep_todo[rev_key] = rev_lst
    todo["updated_at"]     = datetime.datetime.utcnow().isoformat()
    dep_todo["updated_at"] = datetime.datetime.utcnow().isoformat()
    gh_write(FILE_TODOS, todos, sha, f"TODO #{todo_id} {rel} #{dep_id} ({action})")
    return jsonify({"ok": True, key: lst})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #16 — BUG SEVERITY TRIAGE
# PATCH /api/forum/<forum_type>/<thread_id>/severity  { severity: "critical"|... }
# ══════════════════════════════════════════════════════════════════════════════

SEVERITY_LEVELS = ("critical", "high", "medium", "low")

@app.route("/api/forum/<forum_type>/<thread_id>/severity", methods=["PATCH"])
def api_forum_severity(forum_type, thread_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    severity = data.get("severity")
    if severity and severity not in SEVERITY_LEVELS:
        return jsonify({"error": f"severity must be one of {SEVERITY_LEVELS}"}), 400

    db, sha = gh_read(_forum_file(forum_type), force=True)
    db = db or {}
    section = db.get(forum_type, {})
    for post in section.get("posts", []):
        if str(post.get("id")) == str(thread_id):
            post["severity"] = severity
            section["posts"] = section["posts"]
            db[forum_type] = section
            gh_write(_forum_file(forum_type), db, sha, f"Triage {forum_type} {thread_id} severity={severity}")
            return jsonify({"ok": True, "severity": severity})
    return jsonify({"error": "Post not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #17 — FORUM POST STATUS OVERRIDE
# PATCH /api/forum/<forum_type>/<thread_id>/status-override
# ══════════════════════════════════════════════════════════════════════════════

STATUS_OVERRIDES = ("known_issue", "wont_fix", "duplicate", "investigating", None)

@app.route("/api/forum/<forum_type>/<thread_id>/status-override", methods=["PATCH"])
def api_forum_status_override(forum_type, thread_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    override = data.get("override")  # None clears it
    if override and override not in STATUS_OVERRIDES:
        return jsonify({"error": f"Invalid override"}), 400

    db, sha = gh_read(_forum_file(forum_type), force=True)
    db = db or {}
    section = db.get(forum_type, {})
    for post in section.get("posts", []):
        if str(post.get("id")) == str(thread_id):
            post["status_override"] = override
            db[forum_type] = section
            gh_write(_forum_file(forum_type), db, sha, f"Status override {forum_type} {thread_id}: {override}")
            return jsonify({"ok": True, "status_override": override})
    return jsonify({"error": "Post not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #18 — UNIFIED SEARCH  /search
# Searches todos, archive, forum bugs, forum suggestions
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/search")
def search_page():
    q = request.args.get("q", "").lower().strip()
    results = {"todos": [], "archive": [], "bugs": [], "suggestions": []}
    if q:
        todos, _ = gh_read(FILE_TODOS)
        for t in (todos or []):
            if q in t.get("title","").lower() or q in (t.get("ai_description") or "").lower():
                results["todos"].append(enrich_todo(t))

        archive, _ = gh_read(FILE_TODOS_ARCHIVE)
        for t in (archive or []):
            if q in t.get("title","").lower() or q in (t.get("ai_description") or "").lower():
                results["archive"].append(enrich_todo(t))

        bugs_db, _ = gh_read(FILE_FORUM_POSTS_BUGS)
        for p in ((bugs_db or {}).get("bugs", {}).get("posts", [])):
            if q in p.get("title","").lower() or q in p.get("preview","").lower():
                results["bugs"].append(p)

        sugg_db, _ = gh_read(FILE_FORUM_POSTS_SUGGESTIONS)
        for p in ((sugg_db or {}).get("suggestions", {}).get("posts", [])):
            if q in p.get("title","").lower() or q in p.get("preview","").lower():
                results["suggestions"].append(p)

    total = sum(len(v) for v in results.values())
    return render_template("search.html",
        q=request.args.get("q", ""),
        results=results, total=total,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").lower().strip()
    if not q:
        return jsonify({"todos": [], "bugs": [], "suggestions": [], "issues": []})
    results = {"todos": [], "bugs": [], "suggestions": [], "issues": []}

    todos, _ = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    for t in ((todos or []) + (archive or [])):
        if q in t.get("title","").lower() or q in (t.get("ai_description") or "").lower():
            results["todos"].append({"id": t["id"], "title": t.get("title",""), "status": t.get("status","todo")})

    bugs_db, _ = gh_read(FILE_FORUM_POSTS_BUGS)
    for p in ((bugs_db or {}).get("bugs", {}).get("posts", [])):
        if q in p.get("title","").lower():
            results["bugs"].append({"id": p.get("id"), "title": p.get("title","")})

    issues_db, _ = gh_read(FILE_GITHUB_ISSUES)
    for i in (issues_db or {}).get("issues", []):
        if q in i.get("title","").lower():
            results["issues"].append({"number": i.get("number"), "title": i.get("title","")})

    return jsonify(results)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #19 — BOT HEALTH MONITOR
# GET /api/bot/health  — reads bot_health.json written by bot every 5 min
# POST /api/bot/health — bot writes its status here (internal endpoint)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/bot/health", methods=["GET"])
@login_required
def api_bot_health():
    health, _ = gh_read(FILE_BOT_HEALTH)
    health = health or {}
    # Also derive freshness
    last_beat = health.get("last_heartbeat", "")
    is_online = False
    if last_beat:
        try:
            age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_beat)).total_seconds()
            is_online = age < 360  # 6 min threshold
        except Exception:
            pass
    health["is_online"] = is_online
    return jsonify(health)

@app.route("/api/bot/health", methods=["POST"])
def api_bot_health_report():
    """Called by bot.py to report its health. Protected by INTERNAL_SECRET."""
    auth = request.headers.get("X-Internal-Secret", "")
    if INTERNAL_SECRET and auth != INTERNAL_SECRET:
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    data["last_heartbeat"] = datetime.datetime.utcnow().isoformat()
    existing, sha = gh_read(FILE_BOT_HEALTH, force=True)
    gh_write(FILE_BOT_HEALTH, data, sha, "Bot health update")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #20 — MEMBER PROFILE PAGES
# GET /member/<user_id>  — shows member's assigned todos, added todos, activity
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/member/<user_id>")
@login_required
def member_profile(user_id):
    db, _ = gh_read(FILE_MEMBERS)
    db = db or {}
    member = (db.get("members") or {}).get(str(user_id))
    if not member:
        abort(404)

    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    todos   = todos   or []
    archive = archive or []

    assigned  = [enrich_todo(t) for t in todos   if t.get("assigned_to_id") == str(user_id) and t.get("status") != "done"]
    added     = [enrich_todo(t) for t in todos   if t.get("added_by_id")    == str(user_id)]
    completed = [enrich_todo(t) for t in archive if t.get("assigned_to_id") == str(user_id)][:10]

    log, _ = gh_read(FILE_ACTIVITY_LOG)
    activity = [e for e in (log or []) if e.get("user_id") == str(user_id)][:20]

    return render_template("member_profile.html",
        member=member,
        assigned=assigned, added=added, completed=completed,
        activity=activity,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #21 — PUBLIC ROADMAP  /roadmap
# Only todos with "public": true are shown
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/roadmap")
def roadmap():
    todos, _ = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    public_active   = [enrich_todo(t) for t in (todos   or []) if t.get("public")]
    public_done     = [enrich_todo(t) for t in (archive or []) if t.get("public")][:20]

    by_status = {s: [] for s in ["todo","in_progress","review_needed","blocked"]}
    for t in public_active:
        s = t.get("status","todo")
        if s in by_status:
            by_status[s].append(t)

    return render_template("roadmap.html",
        by_status=by_status,
        public_done=public_done,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

@app.route("/api/todo/<int:todo_id>/public", methods=["POST"])
def api_todo_set_public(todo_id):
    """Toggle public visibility of a TODO (admin only)."""
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    public = bool(data.get("public", False))

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if todo:
        todo["public"] = public
        gh_write(FILE_TODOS, todos, sha, f"TODO #{todo_id} public={public}")
        return jsonify({"ok": True, "public": public})

    # Check archive
    archive, arch_sha = gh_read(FILE_TODOS_ARCHIVE, force=True)
    todo = next((t for t in (archive or []) if t["id"] == todo_id), None)
    if todo:
        todo["public"] = public
        gh_write(FILE_TODOS_ARCHIVE, archive, arch_sha, f"Archive TODO #{todo_id} public={public}")
        return jsonify({"ok": True, "public": public})

    return jsonify({"error": "Not found"}), 404


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #22 — RELEASE NOTES  /releases  +  admin release builder
# releases.json: [{ id, version, date, notes, todo_ids, published }]
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/releases")
def releases_page():
    releases, _ = gh_read(FILE_RELEASES)
    releases = sorted((releases or []), key=lambda r: r.get("date",""), reverse=True)
    published = [r for r in releases if r.get("published")]

    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    archive_map = {t["id"]: t for t in (archive or [])}

    enriched = []
    for r in published:
        r = dict(r)
        r["todos"] = [enrich_todo(archive_map[tid]) for tid in r.get("todo_ids",[]) if tid in archive_map]
        enriched.append(r)

    return render_template("releases.html",
        releases=enriched,
        user=get_session().get("user"),
        access_level=get_session().get("access_level", "public"),
    )

@app.route("/api/releases", methods=["GET"])
def api_releases_get():
    releases, _ = gh_read(FILE_RELEASES)
    return jsonify(releases or [])

@app.route("/api/releases", methods=["POST"])
@admin_required
def api_releases_create():
    data = request.json or {}
    version = (data.get("version") or "").strip()
    notes   = (data.get("notes") or "").strip()
    if not version:
        return jsonify({"error": "version required"}), 400
    releases, sha = gh_read(FILE_RELEASES, force=True)
    releases = releases or []
    new_release = {
        "id":        secrets.token_hex(6),
        "version":   version,
        "date":      data.get("date", datetime.datetime.utcnow().strftime("%Y-%m-%d")),
        "notes":     notes,
        "todo_ids":  data.get("todo_ids", []),
        "published": data.get("published", False),
        "created_by": str(get_session().get("user",{}).get("username","?")),
    }
    releases.insert(0, new_release)
    gh_write(FILE_RELEASES, releases, sha, f"Release {version}")
    return jsonify(new_release), 201

@app.route("/api/releases/<release_id>", methods=["PATCH"])
@admin_required
def api_releases_patch(release_id):
    data = request.json or {}
    releases, sha = gh_read(FILE_RELEASES, force=True)
    releases = releases or []
    for r in releases:
        if r.get("id") == release_id:
            for k in ("version","date","notes","todo_ids","published"):
                if k in data:
                    r[k] = data[k]
            gh_write(FILE_RELEASES, releases, sha, f"Update release {release_id}")
            return jsonify(r)
    return jsonify({"error": "Not found"}), 404

@app.route("/api/releases/<release_id>", methods=["DELETE"])
@admin_required
def api_releases_delete(release_id):
    releases, sha = gh_read(FILE_RELEASES, force=True)
    releases = [r for r in (releases or []) if r.get("id") != release_id]
    gh_write(FILE_RELEASES, releases, sha, f"Delete release {release_id}")
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE #13 — RECURRING TODOs (config stored on todo: recur field)
# PATCH /api/todo/<id> already supports arbitrary fields
# Background check: GET /api/todos/recurring  (admin view)
# POST /api/todo/<id>/recur  { interval: "daily"|"weekly"|"monthly" }
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/todo/<int:todo_id>/recur", methods=["POST"])
def api_todo_recur(todo_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    interval = data.get("interval")  # None clears it
    valid_intervals = (None, "daily", "weekly", "monthly")
    if interval not in valid_intervals:
        return jsonify({"error": f"interval must be one of {valid_intervals}"}), 400

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404

    if interval:
        # Calculate next_run
        now = datetime.datetime.utcnow()
        deltas = {"daily": datetime.timedelta(days=1), "weekly": datetime.timedelta(weeks=1), "monthly": datetime.timedelta(days=30)}
        next_run = (now + deltas[interval]).isoformat()
        todo["recur"] = {"interval": interval, "next_run": next_run}
    else:
        todo.pop("recur", None)

    todo["updated_at"] = datetime.datetime.utcnow().isoformat()
    gh_write(FILE_TODOS, todos, sha, f"TODO #{todo_id} recur={interval}")
    return jsonify({"ok": True, "recur": todo.get("recur")})

@app.route("/api/todos/recurring", methods=["GET"])
@login_required
def api_todos_recurring():
    todos, _ = gh_read(FILE_TODOS)
    recurring = [enrich_todo(t) for t in (todos or []) if t.get("recur")]
    return jsonify(recurring)


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED /board — expose new fields (public toggle, dependency info)
# Already handled by existing route; these helpers extend template data
# ══════════════════════════════════════════════════════════════════════════════

# Extend enrich_todo to include dependency display info
_original_enrich_todo = enrich_todo

def enrich_todo(t):
    t = _original_enrich_todo(t)
    # Dependency counts
    t["blocks_count"]     = len(t.get("blocks", []))
    t["blocked_by_count"] = len(t.get("blocked_by", []))
    t["is_blocked"]       = bool(t.get("blocked_by"))
    # Watcher count
    t["watcher_count"]    = len(t.get("watchers", []))
    # Time total
    t["total_minutes"]    = sum(l.get("minutes",0) for l in t.get("time_logs",[]))
    # Comment count (loaded lazily — just flag)
    return t


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED ANALYTICS — add burndown + velocity data
# ══════════════════════════════════════════════════════════════════════════════

# /analytics already exists; extend it with new data by overriding its view
# We patch the existing route by re-registering with a new function
app.view_functions.pop("analytics", None)

@app.route("/analytics")
@login_required
def analytics():
    todos, _   = gh_read(FILE_TODOS)
    archive, _ = gh_read(FILE_TODOS_ARCHIVE)
    todos   = todos or []
    archive = archive or []
    all_todos = todos + archive

    counts = {s: len([t for t in todos if t["status"] == s])
              for s in ["todo", "in_progress", "review_needed", "blocked"]}
    counts["done"] = len(archive)

    from collections import Counter
    added_by  = Counter(t.get("added_by_id") for t in all_todos if t.get("added_by_id"))
    assigned  = Counter(t.get("assigned_to_id") for t in all_todos if t.get("assigned_to_id"))
    tag_counts  = Counter(tag for t in all_todos for tag in t.get("tags", []))
    pri_counts  = Counter(t.get("priority", "medium") for t in todos if t.get("status") != "done")
    recent_done = [enrich_todo(t) for t in sorted(archive, key=lambda t: t.get("updated_at",""), reverse=True)[:10]]
    ai_count     = len([t for t in all_todos if t.get("auto_generated")])
    manual_count = len(all_todos) - ai_count

    # Velocity: completed per week for last 8 weeks
    now = datetime.datetime.utcnow()
    velocity = []
    for i in range(7, -1, -1):
        w_start = (now - datetime.timedelta(weeks=i+1)).isoformat()
        w_end   = (now - datetime.timedelta(weeks=i)).isoformat()
        cnt = sum(1 for t in archive if w_start <= (t.get("done_at") or t.get("updated_at","")) < w_end)
        velocity.append({"week": (now - datetime.timedelta(weeks=i)).strftime("%b %d"), "count": cnt})

    # Time tracked totals per user
    time_by_user: dict = {}
    for t in all_todos:
        for log in t.get("time_logs", []):
            uid = log.get("user_id","unknown")
            time_by_user[uid] = time_by_user.get(uid, 0) + log.get("minutes", 0)

    # Recurring todos
    recurring_count = len([t for t in todos if t.get("recur")])

    return render_template("analytics.html",
        user=get_session()["user"], access_level=get_session().get("access_level"),
        counts=counts, total=len(all_todos), archive_count=len(archive),
        tag_counts=dict(tag_counts), pri_counts=dict(pri_counts),
        ai_count=ai_count, manual_count=manual_count,
        recent_done=recent_done,
        added_by=dict(added_by.most_common(5)),
        assigned=dict(assigned.most_common(5)),
        velocity=velocity,
        time_by_user=dict(sorted(time_by_user.items(), key=lambda x: x[1], reverse=True)[:5]),
        recurring_count=recurring_count,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED SETTINGS PAGE — expose new admin config options
# ══════════════════════════════════════════════════════════════════════════════

app.view_functions.pop("settings", None)

@app.route("/settings")
@admin_required
def settings():
    cfg, _    = gh_read(FILE_CONFIG, force=True)
    cfg       = cfg or DEFAULT_CONFIG.copy()
    members_db = gh_read(FILE_TODO_MEMBERS)[0] or {}
    health, _  = gh_read(FILE_BOT_HEALTH)
    health     = health or {}
    last_beat  = health.get("last_heartbeat","")
    bot_online = False
    if last_beat:
        try:
            age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(last_beat)).total_seconds()
            bot_online = age < 360
        except Exception:
            pass
    releases, _ = gh_read(FILE_RELEASES)
    archive, _  = gh_read(FILE_TODOS_ARCHIVE)
    return render_template("settings.html",
        user=get_session()["user"], access_level=get_session().get("access_level"),
        cfg=cfg,
        members_db=members_db,
        health=health, bot_online=bot_online,
        releases=releases or [],
        archive_count=len(archive or []),
    )


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED /api/config — support new config fields
# ══════════════════════════════════════════════════════════════════════════════

app.view_functions.pop("api_config_save", None)

@app.route("/api/config", methods=["POST"])
def api_config_save():
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    allowed = ("prefix", "reminder_days", "reminder_time", "todo_style",
               "tag_permissions", "digest_channel", "digest_day",
               "anymex_owner", "anymex_repo")
    changes = []
    for k in allowed:
        if k in data:
            if data[k] != cfg.get(k):
                changes.append(f"{k} → {data[k]}")
            cfg[k] = data[k]
    ok = gh_write(FILE_CONFIG, cfg, sha, "Web: Config updated")
    if ok and changes:
        web_user = get_session().get("user", {})
        fake_todo = {"id": "—", "title": "Bot Config", "status": "todo"}
        _web_log_activity("⚙️ Settings Updated", fake_todo, web_user, extra="  ·  ".join(changes))
    return jsonify({"ok": ok})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
