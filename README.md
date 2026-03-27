# AnymeX TODO Bot

A Discord bot that keeps a live TODO board for the AnymeX team. All tasks are tracked, assigned, and updated directly in Discord — the board channel always reflects the current state automatically.

---

## How the Board Works

A dedicated channel is used as the TODO board. The bot posts and maintains embed cards there — one stats summary at the top, then paginated task cards below. Every time a TODO is added, updated, or completed, the cards update automatically.

If the board ever gets out of sync or messages go missing, the bot wipes the channel and reposts everything cleanly on its own. Nobody should ever need to manually clean it up.

The board channel is **strictly bot-only**. Any message typed there by anyone (including admins) is immediately deleted and the person gets a brief tip on how to use `#addtodo` instead.

---

## Adding TODOs

Type `#addtodo` in **any channel** — it doesn't have to be the board channel.

| Example | What happens |
|---|---|
| `#addtodo Fix login screen crash` | Adds a TODO with that title |
| `#addtodo` *(as a reply to a message)* | AI reads the message and generates a title + summary |
| `#addtodo Fix crash --msgs 111 222` | Combines multiple messages into one TODO |

When you use `#addtodo`, a confirmation prompt appears before anything is saved — you can review the title (and the AI's interpretation if it was auto-generated) before confirming.

The bot also checks for duplicates. If the same message is already tracked, or a TODO with an identical or very similar title already exists, it will tell you instead of creating a duplicate.

---

## Task Cards

Each TODO card shows:
- ID and title
- Status and priority
- Who it's assigned to and who added it
- AI-generated summary (if created via AI)

There are 4 card styles to choose from — Clean, Sidebar, Minimal, and Detailed. The whole board switches style at once with `/todo_style`.

---

## Statuses

| Status | Meaning |
|---|---|
| `todo` | Not started |
| `in_progress` | Someone is working on it |
| `review_needed` | Done but needs a look |
| `blocked` | Stuck, can't progress |
| `done` | Completed — removed from board and archived |

---

## Commands

### Managing TODOs

| Command | What it does |
|---|---|
| `/todo_assign <id> [user]` | Assign a TODO to yourself or someone else |
| `/todo_unassign <id>` | Remove the current assignment |
| `/todo_status <id> <status>` | Update the status |
| `/todo_priority <id> <priority>` | Set priority: `low` · `medium` · `high` |
| `/todo_delete <id>` | Delete a TODO (you can only delete ones you added, admins can delete any) |

### Viewing TODOs

| Command | What it does |
|---|---|
| `/todo_list` | All active TODOs |
| `/todo_mine` | TODOs assigned to you |
| `/todo_info <id>` | Full details for one TODO — source message, AI summary, timestamps, etc. |
| `/todo_filter` | Filter by status, priority, or assigned user |
| `/todo_archive` | The 10 most recently completed TODOs |

### Board

| Command | What it does |
|---|---|
| `/todo_style <1-4>` | Change the card style for the whole board |
| `/todo_refresh` | Wipe the board channel and repost everything fresh |

> Every slash command also has a prefix version (default prefix `ax!`), e.g. `ax!todoassign`, `ax!todolist`, etc.

---

## Reassignment Protection

If a TODO is already assigned to someone and you try to assign it to someone else, the bot will ask for confirmation first — it won't silently overwrite. Admins can bypass this and force-reassign directly.
