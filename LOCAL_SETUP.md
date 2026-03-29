# Running AnymeX Locally with Docker

This guide gets the bot + dashboard running on your own machine in a few minutes.

---

## Prerequisites

You need **Docker Desktop** installed.

- **Windows / Mac:** Download from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)
- **Linux:** Run `sudo apt install docker.io docker-compose-plugin` (Ubuntu/Debian)

Once installed, open **Docker Desktop** and make sure it's running (you'll see the whale icon in your taskbar).

---

## Step 1 — Clone / download the project

If you have git:
```bash
git clone https://github.com/Shebyyy/anymex-support-db
cd anymex-support-db
```

Or just download the ZIP from GitHub and extract it.

---

## Step 2 — Set up your .env file

Copy the example file and fill it in:

```bash
cp .env.example .env
```

Then open `.env` in any text editor (Notepad, VS Code, etc.) and fill in every value. The ones you **must** fill in to get started:

| Variable | Where to get it |
|---|---|
| `DISCORD_TOKEN` | Discord Dev Portal → Your App → Bot → Token |
| `DISCORD_GUILD_ID` | Right-click your server → Copy Server ID |
| `DISCORD_CLIENT_ID` | Discord Dev Portal → General Information → App ID |
| `DISCORD_CLIENT_SECRET` | Discord Dev Portal → OAuth2 → Client Secret |
| `DISCORD_REDIRECT_URI` | Set to `http://localhost:8080/callback` for local dev |
| `FLASK_SECRET` | Run: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GITHUB_TOKEN` | GitHub → Settings → Developer Settings → Personal Access Tokens |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) (free) |
| `DISCORD_BUGS_CHANNEL_ID` | Right-click your bugs forum channel → Copy Channel ID |
| `DISCORD_SUGGESTIONS_CHANNEL_ID` | Right-click your suggestions forum channel → Copy Channel ID |

> **Discord OAuth redirect:** You also need to add `http://localhost:8080/callback` in the Discord Developer Portal under **OAuth2 → Redirects**. Otherwise login won't work.

---

## Step 3 — Build and run with Docker

Open a terminal in the project folder and run:

```bash
docker build -t anymex .
```

This builds the image (downloads Python, installs packages). Only needed once — or when you change `requirements.txt`.

Then start it:

```bash
docker run --env-file .env -p 8080:8080 anymex
```

That's it. You should see the bot connect to Discord and the dashboard start up.

**Open the dashboard:** [http://localhost:8080](http://localhost:8080)

---

## Useful Docker commands

```bash
# Run in the background (detached mode)
docker run -d --env-file .env -p 8080:8080 --name anymex anymex

# See live logs (when running detached)
docker logs -f anymex

# Stop the container
docker stop anymex

# Start it again (without rebuilding)
docker start anymex

# Remove the container entirely
docker rm anymex

# Rebuild after code changes
docker build -t anymex .
docker stop anymex && docker rm anymex
docker run -d --env-file .env -p 8080:8080 --name anymex anymex
```

---

## Using Docker Compose (easier for repeated use)

Create a file called `docker-compose.yml` in the project folder:

```yaml
services:
  anymex:
    build: .
    env_file: .env
    ports:
      - "8080:8080"
    restart: unless-stopped
```

Then:

```bash
# Start
docker compose up -d

# See logs
docker compose logs -f

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

---

## Troubleshooting

**Bot doesn't connect to Discord**
- Check `DISCORD_TOKEN` in your `.env` — make sure there are no extra spaces
- Make sure the bot is added to your server (OAuth2 → URL Generator → `bot` + `applications.commands` scopes)

**Dashboard shows login error**
- Check that `DISCORD_REDIRECT_URI=http://localhost:8080/callback` matches exactly what's in Discord Dev Portal → OAuth2 → Redirects

**Port already in use**
- Something else is using port 8080. Change `-p 8080:8080` to `-p 8081:8080` and open `http://localhost:8081` instead

**Forum channels show "channel ID not configured"**
- Make sure `DISCORD_BUGS_CHANNEL_ID` and `DISCORD_SUGGESTIONS_CHANNEL_ID` are set in `.env`
- Enable Developer Mode in Discord (Settings → Advanced) then right-click the channel → Copy Channel ID

**GitHub write errors**
- Make sure your `GITHUB_TOKEN` has **Contents: read and write** permission on the data repo
- Check that `DATA_OWNER` and `DATA_REPO` match your actual repo

---

## File structure after setup

```
anymex/
├── bot.py
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml   ← you create this
├── .env                 ← you create this (never commit!)
├── .env.example         ← safe to commit
└── templates/
    ├── base.html
    ├── index.html
    ├── board.html
    ├── bugs.html
    ├── suggestions.html
    ├── dashboard.html
    ├── analytics.html
    ├── settings.html
    └── error.html
```

> ⚠️ **Never commit your `.env` file to GitHub.** Add it to `.gitignore`:
> ```
> echo ".env" >> .gitignore
> ```
