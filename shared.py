# shared.py - Shared Constants

FILE_CONFIG        = "config.json"
FILE_TODOS         = "todos.json"
FILE_TODOS_ARCHIVE = "todos_archive.json"
FILE_FORUM_LINKS   = "forum_links.json"
FILE_ACTIVITY_LOG  = "activity_log.json"
FILE_MEMBERS       = "members.json"
FILE_TODO_MEMBERS  = "todo_members.json"
FILE_COMMENTS      = "todo_comments.json"
FILE_SAVED_FILTERS = "saved_filters.json"
FILE_GITHUB_ISSUES = "github_issues.json"
FILE_ISSUE_LINKS   = "github_issue_links.json"
FILE_RELEASES      = "releases.json"
FILE_BOT_HEALTH    = "bot_health.json"
FILE_GUILD_EMOJIS  = "guild_emojis.json"
FILE_FORUM_POSTS_BUGS        = "forum_posts_bugs.json"
FILE_FORUM_POSTS_SUGGESTIONS = "forum_posts_suggestions.json"
FILE_BOARD_IDS       = "board_ids.json"
FILE_THREAD_MESSAGES = "thread_messages.json"
FILE_NOTIF_PREFS     = "user_notification_prefs.json"
FILE_NOTIFICATIONS   = "notifications.json"
FILE_USER_DEVICES    = "user_devices.json"

# Default notification preferences for a new user
DEFAULT_NOTIF_PREFS = {
    "notify_todo_created": True,   # new todo added
    "notify_todo_edited":  True,   # status / priority / assignee / title changed
    "notify_assigned":     True,   # you were assigned to a todo
    "notify_comment":      "all",  # "all" | "mention_only" | "none"
}

DEFAULT_CONFIG = {
    "todo_channel": None, "todo_roles": [], "todo_style": 1,
    "prefix": "ax!", "log_channel": None,
    "reminder_days": 3, "reminder_time": "09:00",
    "reminder_channel": None, "thread_channel": None,
}

DEFAULT_BOARD_IDS = {
    "stats_message_id": None,
    "style":            None,
    "pages":            [],
}

STATUS_COLORS = {
    "todo": "#378ADD", "in_progress": "#BA7517",
    "review_needed": "#888780", "blocked": "#E24B4A", "done": "#1D9E75",
}

STATUS_COLORS_INT = {
    "todo":          0x378ADD,
    "in_progress":   0xBA7517,
    "review_needed": 0x888780,
    "blocked":       0xE24B4A,
    "done":          0x1D9E75,
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

TAG_EMOJIS = {
    "bug":      "🔴",
    "feature":  "🟢",
    "urgent":   "🟠",
    "docs":     "🔵",
    "refactor": "🟣",
    "question": "🟡",
}
