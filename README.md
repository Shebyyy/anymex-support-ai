# AnymeX TODO Bot + Dashboard

A Discord bot that keeps a live TODO board for the AnymeX team, with a full web dashboard — Discord OAuth2 login, role-based access, analytics, and more. All data is stored in a GitHub repository as JSON files.

---

## Repository Structure

```
anymex-repo/
├── bot.py              ← Discord bot (aiohttp + discord.py)
├── app.py              ← Web dashboard (Flask)
├── requirements.txt    ← All dependencies
├── Dockerfile          ← Runs both bot + dashboard together
├── .env.example        ← All environment variables
├── LICENSE
└── templates/          ← Dashboard HTML pages
    ├── base.html
    ├── index.html      ← Public home with stats
    ├── board.html      ← Full TODO board + filters
    ├── dashboard.html  ← Personal task view (login required)
    ├── analytics.html  ← Charts & stats
    ├── settings.html   ← Admin config panel
    └── error.html
```

---

## How the Board Works

A dedicated Discord channel is used as the TODO board. The bot posts and maintains embed cards there — one stats summary at the top, then paginated task cards below. Every time a TODO is added, updated, or completed, the cards update automatically.

If the board ever gets out of sync or messages go missing, the bot wipes the channel and reposts everything cleanly on its own.

The board channel is **strictly bot-only**. Any message typed there by anyone (including admins) is immediately deleted.

---

## Adding TODOs

Type `#addtodo` in **any channel**.

| Example | What happens |
|---|---|
| `#addtodo Fix login screen crash` | Adds a TODO with that title |
| `#addtodo` *(as a reply to a message)* | AI reads the message and generates a title + summary |
| `#addtodo Fix crash --msgs 111 222` | Combines multiple messages into one TODO |

The bot checks for duplicates before saving and shows a confirmation prompt first.

---

## Task Cards

Each TODO card shows:
- ID and title
- Status and priority
- Who it's assigned to and who added it
- AI-generated summary (if created via AI)

There are 6 card styles — Clean, Sidebar, Minimal, Detailed, FAQ, Full Detail. Switch with `/todo_style`.

---

## Statuses

| Status | Meaning |
|---|---|
| `todo` | Not started |
| `in_progress` | Someone is working on it |
| `review_needed` | Done but needs a look |
| `blocked` | Stuck, can't progress |
| `done` | Completed — archived |

---

## Bot Commands

### Managing TODOs

| Command | What it does |
|---|---|
| `/todo_assign <id> [user]` | Assign a TODO to yourself or someone else |
| `/todo_unassign <id>` | Remove the current assignment |
| `/todo_status <id> <status>` | Update the status |
| `/todo_priority <id> <priority>` | Set priority: `low` · `medium` · `high` |
| `/todo_delete <id>` | Delete a TODO |

### Viewing TODOs

| Command | What it does |
|---|---|
| `/todo_list` | All active TODOs |
| `/todo_mine` | TODOs assigned to you |
| `/todo_info <id>` | Full details for one TODO |
| `/todo_filter` | Filter by status, priority, or assigned user |
| `/todo_archive` | 10 most recently completed TODOs |

### Board

| Command | What it does |
|---|---|
| `/todo_style <1-6>` | Change card style for the whole board |
| `/todo_refresh` | Wipe and repost the board fresh |

> Every slash command also has a prefix version (default `ax!`), e.g. `ax!todolist`, `ax!todostatus`, etc.

---

## Web Dashboard

The dashboard is available at your Render URL. Anyone can view the public board — Discord login unlocks more based on your server role.

### Access Levels

| Level | Who | Can Do |
|---|---|---|
| **public** | Not logged in | View board only |
| **member** | Any Discord member of the server | View board + personal task view |
| **manager** | Has a configured TODO manager role | Create / edit / update / delete TODOs |
| **admin** | Discord server admin or owner | Everything + settings panel |

### Pages

| Route | Access | Description |
|---|---|---|
| `/` | Public | Home with stats + recent TODOs |
| `/board` | Public | Full board with status/priority/tag filters |
| `/dashboard` | Login | Your assigned tasks + tasks you added |
| `/analytics` | Login | Charts: status, priority, tags, completions |
| `/settings` | Admin | Bot prefix, style, reminder config |

### API Endpoints

| Method | Route | Access | Description |
|---|---|---|---|
| GET | `/api/todos` | Public | All active TODOs as JSON |
| GET | `/api/todo/:id` | Public | Single TODO |
| POST | `/api/todo` | Manager+ | Create TODO |
| PATCH | `/api/todo/:id` | Manager+ | Update TODO |
| DELETE | `/api/todo/:id` | Manager+ | Delete TODO |
| POST | `/api/config` | Admin | Update bot config |
| GET | `/api/me` | — | Current session info |

---

## Environment Variables

Copy `.env.example` to `.env` for local dev. For Render, add them under **Environment**.

### Already had (bot)

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your bot token |
| `GITHUB_TOKEN` | GitHub PAT with repo read/write access |
| `GROQ_API_KEY` | Groq API key for AI title generation |

### New (dashboard only)

| Variable | How to get it |
|---|---|
| `DISCORD_CLIENT_ID` | Discord Dev Portal → your app → General Information → Application ID |
| `DISCORD_CLIENT_SECRET` | Discord Dev Portal → your app → OAuth2 → Client Secret |
| `DISCORD_REDIRECT_URI` | Set to `https://your-render-url.onrender.com/callback` — add the same URL in Discord Dev Portal → OAuth2 → Redirects |
| `DISCORD_GUILD_ID` | Right-click your server in Discord → Copy Server ID (requires Developer Mode) |
| `FLASK_SECRET` | Run: `python -c "import secrets; print(secrets.token_hex(32))"` |

---

## Deployment on Render

1. Push this repo to GitHub
2. Create a new **Web Service** on Render, connect the repo
3. Set **Start Command** to: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --daemon && python bot.py`
4. Add all environment variables from `.env.example`
5. Done — bot and dashboard run together on the same service

---

## License

MIT © 2026 Sheby
