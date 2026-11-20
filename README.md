# Discord Progress Bot

A Discord bot that walks members through a step-by-step program, asks them about each task in their own channel, tracks who has done what, and gives admins a web dashboard of everyone's progress.

- Tasks are defined in `tasks.json` (stage, question, task, instructions, optional resource link), so you can turn any course, onboarding flow or challenge into a tracked program.
- Tracks channels inside the categories you list in `CATEGORY_IDS`.
- Admin dashboard with password login, plus a per-member progress page.
- SQLite, created automatically. No external database.

## Install

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and fill in your Discord bot token, the category IDs to track, and a dashboard password. Edit `tasks.json` with your own program steps.

```bash
copy .env.example .env
```

On macOS/Linux:

```bash
cp .env.example .env
```

Enable these intents in the Discord Developer Portal:

- Server Members Intent
- Message Content Intent, if using the prefix commands `!start` and `!reset`

## Run

```bash
python bot.py
```

## Dashboard routes

Admin login:

```text
http://127.0.0.1:5000/admin/login
```

Admin dashboard:

```text
http://127.0.0.1:5000/admin
```

Member dashboard:

```text
http://127.0.0.1:5000/member?guild_id=YOUR_SERVER_ID&user_id=DISCORD_USER_ID
```

The SQLite database is created automatically when the bot starts.

## License

MIT. See [LICENSE](LICENSE).
