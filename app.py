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

# GitHub DB (same as bot)
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN")
DATA_OWNER    = os.environ.get("DATA_OWNER", "Shebyyy")
DATA_REPO     = os.environ.get("DATA_REPO",  "anymex-support-db")
DATA_BRANCH   = os.environ.get("DATA_BRANCH", "main")
GITHUB_API    = "https://api.github.com"

FILE_CONFIG        = "config.json"
FILE_TODOS         = "todos.json"
FILE_TODOS_ARCHIVE = "todos_archive.json"

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
    url = f"{GITHUB_API}/repos/{DATA_OWNER}/{DATA_REPO}/contents/{filepath}?ref={DATA_BRANCH}"
    r = req.get(url, headers=gh_headers())
    if r.status_code == 404:
        return None, None
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    parsed = json.loads(content)
    _cache[filepath] = parsed
    _cache_ts[filepath] = now
    return parsed, data["sha"]

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
    r = req.put(url, headers=gh_headers(), json=payload)
    return r.status_code in (200, 201)


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
    r = req.get(f"{DISCORD_API}{endpoint}", headers={"Authorization": f"Bearer {token}"})
    return r.json() if r.ok else None

def get_access_token(code):
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    r = req.post(DISCORD_TOKEN_URL, data=data)
    print("TOKEN RESPONSE STATUS:", r.status_code)
    print("TOKEN RESPONSE BODY:", r.text)
    return r.json() if r.ok else None

def get_guild_member(token, guild_id):
    r = req.get(
        f"{DISCORD_API}/users/@me/guilds/{guild_id}/member",
        headers={"Authorization": f"Bearer {token}"}
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
    return t

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
        err = token_data.get("error", "unknown") if token_data else "no_response"
        desc = token_data.get("error_description", "") if token_data else ""
        return redirect(url_for("index") + f"?error=token_fail&reason={err}&desc={desc}")

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
    search          = request.args.get("q", "").lower().strip()

    filtered = todos
    if status_filter != "all":
        filtered = [t for t in filtered if t["status"] == status_filter]
    if priority_filter != "all":
        filtered = [t for t in filtered if t.get("priority") == priority_filter]
    if tag_filter != "all":
        filtered = [t for t in filtered if tag_filter in t.get("tags", [])]
    if search:
        filtered = [t for t in filtered if search in t["title"].lower()
                    or search in (t.get("ai_description") or "").lower()]

    all_tags = sorted(set(tag for t in todos for tag in t.get("tags", [])))

    return render_template("board.html",
        todos=filtered, all_todos=todos, archive_count=len(archive),
        cfg=cfg, all_tags=all_tags,
        status_filter=status_filter, priority_filter=priority_filter,
        tag_filter=tag_filter, search=search,
        user=get_session().get("user"), access_level=get_session().get("access_level", "public"),
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
    todos, _ = gh_read(FILE_TODOS)
    todos = [enrich_todo(t) for t in (todos or []) if t.get("status") != "done"]
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
        "added_by_id": str(user.get("id", "")),
        "added_by": user.get("username", "web"),
        "created_at": datetime.datetime.utcnow().isoformat(),
        "updated_at": datetime.datetime.utcnow().isoformat(),
        "auto_generated": False,
        "source": "web_dashboard",
    }
    todos.append(new_todo)
    ok = gh_write(FILE_TODOS, todos, sha, f"Web: Add TODO #{next_id} by {user.get('username','?')}")
    if ok:
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
    allowed = ("status", "priority", "tags", "assigned_to_id", "title", "ai_description")
    for field in allowed:
        if field in data:
            todo[field] = data[field]
    todo["updated_at"] = datetime.datetime.utcnow().isoformat()

    # Archive if done
    if todo.get("status") == "done":
        todos = [t for t in todos if t["id"] != todo_id]
        archive, arch_sha = gh_read(FILE_TODOS_ARCHIVE, force=True)
        archive = archive or []
        archive.append(todo)
        gh_write(FILE_TODOS_ARCHIVE, archive, arch_sha, f"Web: Archive TODO #{todo_id}")

    gh_write(FILE_TODOS, todos, sha, f"Web: Update TODO #{todo_id}")
    return jsonify(enrich_todo(todo))

@app.route("/api/todo/<int:todo_id>", methods=["DELETE"])
def api_todo_delete(todo_id):
    if get_session().get("access_level") not in ("manager", "admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403

    todos, sha = gh_read(FILE_TODOS, force=True)
    todos = todos or []
    original = len(todos)
    todos = [t for t in todos if t["id"] != todo_id]
    if len(todos) == original:
        return jsonify({"error": "Not found"}), 404
    gh_write(FILE_TODOS, todos, sha, f"Web: Delete TODO #{todo_id}")
    return jsonify({"ok": True})

@app.route("/api/config", methods=["POST"])
def api_config_save():
    if get_session().get("access_level") not in ("admin", "owner"):
        return jsonify({"error": "Forbidden"}), 403
    data = request.json or {}
    cfg, sha = gh_read(FILE_CONFIG, force=True)
    cfg = cfg or DEFAULT_CONFIG.copy()
    allowed = ("prefix", "reminder_days", "reminder_time", "todo_style")
    for k in allowed:
        if k in data:
            cfg[k] = data[k]
    ok = gh_write(FILE_CONFIG, cfg, sha, "Web: Config updated")
    return jsonify({"ok": ok})

@app.route("/api/me")
def api_me():
    return jsonify({
        "user": get_session().get("user"),
        "access_level": get_session().get("access_level", "public"),
        "member": get_session().get("member"),
    })

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

# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
