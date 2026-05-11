# Life Organizer Telegram Bot

Telegram bot with 4 main sections:
- Income: `Add`, `Minus`, `View`
- Goals: short-term and long-term goals
- Workout: quick workout logging + average stats
- Setup: reminder mute/unmute and admin tools

It also includes:
- Login gate: asks name + password (`michael999` by default)
- Admin panel: list users and remove users
- Reminders in `Australia/Sydney` timezone

## Features Implemented

### 1) Income
- `Add`
- asks for amount
- asks for income category
- asks for job type (specific job)
- categories/job types can be created or deleted in `Income -> Manage Jobs`
- `Minus`
- records money used/spent
- `View`
- total gained
- total used
- net income (`gained - used`)

### 2) Goals
- `Short-term goals`
- stored as a list
- reminder every day at **8:00 PM Sydney**
- `Long-term goals`
- stored as projects
- reminder every **2 weeks** (checked daily at 8:05 PM Sydney; reminder sent only when 14 days have passed)

### 3) Setup (separate from Income/Goals)
- mute/unmute short-term reminders
- mute/unmute long-term reminders
- reminder status
- admin panel button (admin-only)

### 4) Workout Tracker
- `Worked Out` button
- asks for confirmation (`Yes` / `No`)
- `View Average` shows:
- total workouts logged
- average workouts per week
- average workouts per day

### 5) Auth + Admin
- New users must enter:
- name
- password (`BOT_ACCESS_PASSWORD`, default `michael999`)
- Admin panel (for IDs in `BOT_ADMIN_IDS`) can:
- list users
- remove users

## Project Files
- `bot.py` - main bot logic
- `requirements.txt` - dependencies
- `.env.example` - environment variables template
- `bot_data.sqlite3` - auto-created local database at runtime

## Local Run

1. Create virtual environment and install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create `.env` from example:

```bash
cp .env.example .env
```

3. Create a `.env` file (bot auto-loads it):

```bash
cp .env.example .env
```

Then edit `.env` values.

Alternative: export env vars manually (or use your process manager):

```bash
export TELEGRAM_BOT_TOKEN="<your-token>"
export BOT_ACCESS_PASSWORD="michael999"
export BOT_ADMIN_IDS="<your-telegram-user-id>"
```

4. Run:

```bash
python3 bot.py
```

## Deploy on Google Cloud VM (Ubuntu)

1. SSH into your VM and clone repo.
2. Install Python if needed:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

3. In project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Set env vars (recommended in systemd unit or shell profile).

5. Run with systemd (recommended):

Create `/etc/systemd/system/lifebot.service`:

```ini
[Unit]
Description=Life Organizer Telegram Bot
After=network.target

[Service]
User=<your-linux-user>
WorkingDirectory=<absolute-path-to-repo>
Environment=TELEGRAM_BOT_TOKEN=<token>
Environment=BOT_ACCESS_PASSWORD=michael999
Environment=BOT_ADMIN_IDS=<admin-telegram-id>
ExecStart=<absolute-path-to-repo>/.venv/bin/python <absolute-path-to-repo>/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable lifebot
sudo systemctl start lifebot
sudo systemctl status lifebot
```

## Push to GitHub

```bash
git init

git add .
git commit -m "Initial Telegram life organizer bot"

git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```

## Notes
- This uses SQLite; it is fine for small to moderate personal usage.
- If you want multi-instance scaling later, we can switch to Postgres.
- Keep your token private.
