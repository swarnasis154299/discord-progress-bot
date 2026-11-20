import asyncio
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template_string, request, session, url_for

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DATABASE_PATH", "progress.sqlite3")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
# Comma-separated Discord category IDs whose channels the bot tracks
CATEGORY_IDS = {int(x) for x in os.getenv("CATEGORY_IDS", "").replace(" ", "").split(",") if x}
WAIT_DAYS = 5
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-this-password")
DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", secrets.token_hex(32))

with open(os.getenv("TASKS_PATH", "tasks.json"), "r", encoding="utf-8") as f:
    TASKS = json.load(f)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)
app.secret_key = DASHBOARD_SECRET


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            task_index INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            completed_at TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            task_index INTEGER NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL,
            retry_until TEXT
        )
        """)
        conn.commit()


def get_progress(guild_id, user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM progress WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO progress(guild_id,user_id) VALUES(?,?)",
                (guild_id, user_id)
            )
            conn.commit()
            return {
                "guild_id": guild_id,
                "user_id": user_id,
                "task_index": 0,
                "blocked_until": None,
                "completed_at": None,
            }
        return dict(row)


def save_progress(guild_id, user_id, task_index, blocked_until=None, completed_at=None):
    with db() as conn:
        conn.execute("""
        INSERT INTO progress(guild_id,user_id,task_index,blocked_until,completed_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(guild_id,user_id) DO UPDATE SET
          task_index=excluded.task_index,
          blocked_until=excluded.blocked_until,
          completed_at=excluded.completed_at
        """, (guild_id, user_id, task_index, blocked_until, completed_at))
        conn.commit()


def log_response(guild_id, user_id, task_index, answer, retry_until=None):
    with db() as conn:
        conn.execute("""
        INSERT INTO responses(guild_id,user_id,task_index,answer,created_at,retry_until)
        VALUES(?,?,?,?,?,?)
        """, (
            guild_id,
            user_id,
            task_index,
            answer,
            iso(datetime.now(timezone.utc)),
            retry_until,
        ))
        conn.commit()


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value) if value else None


def usable_channel(category):
    channels = sorted(category.text_channels, key=lambda c: c.position)
    return channels[0] if channels else None


async def send_question(member, task_index):
    if not member or task_index >= len(TASKS):
        return

    task = TASKS[task_index]
    channel = None
    for category_id in CATEGORY_IDS:
        category = member.guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            channel = usable_channel(category)
            if channel:
                break

    if channel is None:
        return

    embed = discord.Embed(
        title=f"Progress Check — Step {task_index + 1} of {len(TASKS)}",
        description=task["question"],
        color=discord.Color.blurple()
    )
    embed.add_field(name="Stage", value=task["stage"], inline=False)
    embed.set_footer(text=f"Assigned to {member.display_name}")

    await channel.send(
        content=member.mention,
        embed=embed,
        view=QuestionView(member.id, member.guild.id, task_index)
    )


class QuestionView(discord.ui.View):
    def __init__(self, user_id, guild_id, task_index):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.guild_id = guild_id
        self.task_index = task_index

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This check-in belongs to another member.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, custom_id="progress_yes")
    async def yes(self, interaction, button):
        progress = get_progress(self.guild_id, self.user_id)
        if progress["task_index"] != self.task_index:
            await interaction.response.send_message("This question is no longer active.", ephemeral=True)
            return

        log_response(self.guild_id, self.user_id, self.task_index, "yes")
        next_index = self.task_index + 1
        completed = iso(datetime.now(timezone.utc)) if next_index >= len(TASKS) else None
        save_progress(self.guild_id, self.user_id, next_index, None, completed)

        await interaction.response.edit_message(
            content=f"✅ Completed: **{TASKS[self.task_index]['task']}**",
            embed=None, view=None
        )

        if completed:
            await interaction.followup.send(
                "🎓 Congratulations — you completed all tasks!", ephemeral=True
            )
        else:
            await send_question(interaction.guild.get_member(self.user_id), next_index)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, custom_id="progress_no")
    async def no(self, interaction, button):
        progress = get_progress(self.guild_id, self.user_id)
        if progress["task_index"] != self.task_index:
            await interaction.response.send_message("This question is no longer active.", ephemeral=True)
            return

        retry = datetime.now(timezone.utc) + timedelta(days=WAIT_DAYS)
        retry_text = iso(retry)
        log_response(self.guild_id, self.user_id, self.task_index, "no", retry_text)
        save_progress(self.guild_id, self.user_id, self.task_index, retry_text, None)

        task = TASKS[self.task_index]
        text = f"❌ **Task:** {task['task']}\n\n{task['instructions']}"
        if task.get("resource_url"):
            text += f"\n\nResource: {task['resource_url']}"

        await interaction.response.edit_message(content=text, embed=None, view=None)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def reminder_loop():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        with db() as conn:
            rows = conn.execute("""
                SELECT user_id, task_index, blocked_until
                FROM progress
                WHERE guild_id=? AND task_index < ?
                  AND blocked_until IS NOT NULL
            """, (guild.id, len(TASKS))).fetchall()

        for row in rows:
            due = parse_iso(row["blocked_until"])
            if due and due <= now:
                member = guild.get_member(row["user_id"])
                if member:
                    save_progress(guild.id, member.id, row["task_index"], None, None)
                    await send_question(member, row["task_index"])


@bot.event
async def on_ready():
    init_db()
    try:
        await bot.tree.sync()
    except Exception as exc:
        print(f"Slash-command sync failed: {exc}")
    if not reminder_loop.is_running():
        reminder_loop.start()
    print(f"Logged in as {bot.user}.")


@bot.tree.command(name="test_deploy", description="Start the five-day questionnaire automation immediately.")
@commands.has_permissions(manage_guild=True)
async def test_deploy(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel) or interaction.channel.category_id not in CATEGORY_IDS:
        await interaction.response.send_message(
            "Use this command inside a text channel belonging to one of the configured categories.",
            ephemeral=True
        )
        return

    progress = get_progress(interaction.guild.id, interaction.user.id)
    if progress["completed_at"]:
        await interaction.response.send_message("You have already completed all tasks.", ephemeral=True)
        return

    blocked = parse_iso(progress["blocked_until"])
    if blocked and blocked > datetime.now(timezone.utc):
        await interaction.response.send_message(
            f"Your next check-in is due <t:{int(blocked.timestamp())}:R>.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Test deployment started. Sending the first pending question now.", ephemeral=True
    )
    await send_question(interaction.guild.get_member(interaction.user.id), progress["task_index"])


@bot.command()
@commands.has_permissions(manage_guild=True)
async def start(ctx):
    progress = get_progress(ctx.guild.id, ctx.author.id)
    if progress["completed_at"]:
        await ctx.send("You have already completed all tasks.")
        return

    blocked = parse_iso(progress["blocked_until"])
    if blocked and blocked > datetime.now(timezone.utc):
        await ctx.send(f"Your next check-in is due <t:{int(blocked.timestamp())}:R>.")
        return

    await send_question(ctx.author, progress["task_index"])
    await ctx.send("Your questionnaire has started.", delete_after=5)


@bot.command()
@commands.has_permissions(manage_guild=True)
async def reset(ctx, member: discord.Member = None):
    member = member or ctx.author
    save_progress(ctx.guild.id, member.id, 0, None, None)
    await ctx.send(f"Reset progress for {member.mention}.")


BASE_STYLE = """
<style>
body{font-family:Arial,sans-serif;background:#111827;color:#f3f4f6;margin:0;padding:24px}
.container{max-width:1200px;margin:auto}.nav{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
a,.button{color:#fff;background:#2563eb;padding:9px 13px;border-radius:7px;text-decoration:none;border:0;cursor:pointer}
.card{background:#1f2937;border:1px solid #374151;border-radius:10px;padding:18px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.metric{font-size:28px;font-weight:bold}.muted{color:#9ca3af}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #374151;vertical-align:top}th{color:#93c5fd}
input{padding:10px;border-radius:6px;border:1px solid #4b5563;background:#111827;color:#fff;margin-right:8px}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#374151}.ok{background:#166534}.warn{background:#92400e}
@media(max-width:650px){body{padding:12px}table{font-size:13px}.card{overflow:auto}}
</style>
"""


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def member_from_request():
    try:
        guild_id = int(request.args.get("guild_id", ""))
        user_id = int(request.args.get("user_id", ""))
    except ValueError:
        abort(400, "guild_id and user_id must be numeric")

    guild = bot.get_guild(guild_id)
    if guild is None:
        abort(404, "Discord server not found")
    member = guild.get_member(user_id)
    if member is None:
        abort(404, "Member not found")
    return guild, member


@app.route("/")
def dashboard_home():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), DASHBOARD_PASSWORD):
            session["admin_logged_in"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        error = "Incorrect password."

    return render_template_string(BASE_STYLE + """
    <div class="container"><div class="card">
    <h1>Admin dashboard login</h1>
    {% if error %}<p class="warn">{{ error }}</p>{% endif %}
    <form method="post"><input type="password" name="password" placeholder="Dashboard password" required><button class="button">Sign in</button></form>
    </div></div>
    """, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None and bot.guilds:
        guild_id = bot.guilds[0].id

    guild = bot.get_guild(guild_id) if guild_id else None
    with db() as conn:
        progress_rows = conn.execute("SELECT * FROM progress WHERE guild_id=?", (guild_id,)).fetchall() if guild_id else []
        response_rows = conn.execute("SELECT * FROM responses WHERE guild_id=? ORDER BY id DESC LIMIT 500", (guild_id,)).fetchall() if guild_id else []
        yes_count = conn.execute("SELECT COUNT(*) FROM responses WHERE guild_id=? AND answer='yes'", (guild_id,)).fetchone()[0] if guild_id else 0
        no_count = conn.execute("SELECT COUNT(*) FROM responses WHERE guild_id=? AND answer='no'", (guild_id,)).fetchone()[0] if guild_id else 0

    completed = sum(1 for r in progress_rows if r["completed_at"])
    blocked = sum(1 for r in progress_rows if r["blocked_until"])
    active = len(progress_rows) - completed
    total = len(progress_rows)
    completion_rate = round(completed / total * 100, 1) if total else 0

    members = []
    for row in progress_rows:
        member = guild.get_member(row["user_id"]) if guild else None
        members.append({
            "name": member.display_name if member else str(row["user_id"]),
            "user_id": row["user_id"],
            "step": min(row["task_index"] + 1, len(TASKS)),
            "completed": bool(row["completed_at"]),
            "blocked_until": row["blocked_until"],
            "completed_at": row["completed_at"],
        })

    return render_template_string(BASE_STYLE + """
    <div class="container">
      <div class="nav"><a href="{{ url_for('admin_dashboard', guild_id=guild_id) }}">Admin overview</a><a href="{{ url_for('admin_logout') }}">Logout</a>{% if guild_id %}<a href="{{ url_for('member_dashboard', guild_id=guild_id, user_id=(guild.owner_id if guild else 0)) }}">Member example</a>{% endif %}</div>
      <h1>Questionnaire analytics</h1>
      <p class="muted">Server: {{ guild.name if guild else 'No connected server' }}</p>
      <div class="grid">
        <div class="card"><div class="muted">Tracked members</div><div class="metric">{{ total }}</div></div>
        <div class="card"><div class="muted">Active</div><div class="metric">{{ active }}</div></div>
        <div class="card"><div class="muted">Blocked / waiting</div><div class="metric">{{ blocked }}</div></div>
        <div class="card"><div class="muted">Completed</div><div class="metric">{{ completed }}</div></div>
        <div class="card"><div class="muted">Completion rate</div><div class="metric">{{ completion_rate }}%</div></div>
        <div class="card"><div class="muted">Yes / No responses</div><div class="metric">{{ yes_count }} / {{ no_count }}</div></div>
      </div>
      <div class="card"><h2>Member progress</h2><table><tr><th>Member</th><th>Step</th><th>Status</th><th>Next retry</th><th>Completed</th><th>Details</th></tr>
      {% for m in members %}<tr><td>{{ m.name }}<br><span class="muted">{{ m.user_id }}</span></td><td>{{ m.step }} / {{ task_count }}</td><td>{% if m.completed %}<span class="badge ok">Completed</span>{% elif m.blocked_until %}<span class="badge warn">Waiting</span>{% else %}<span class="badge">Active</span>{% endif %}</td><td>{{ m.blocked_until or '—' }}</td><td>{{ m.completed_at or '—' }}</td><td><a href="{{ url_for('member_dashboard', guild_id=guild_id, user_id=m.user_id) }}">View</a></td></tr>{% endfor %}
      </table></div>
      <div class="card"><h2>Recent response history</h2><table><tr><th>Time</th><th>Member</th><th>Question</th><th>Answer</th><th>Retry until</th></tr>
      {% for r in responses %}<tr><td>{{ r.created_at }}</td><td>{{ r.user_id }}</td><td>{{ r.task_index + 1 }} — {{ tasks[r.task_index].get('task', tasks[r.task_index].get('question','')) }}</td><td>{{ r.answer|upper }}</td><td>{{ r.retry_until or '—' }}</td></tr>{% endfor %}
      </table></div>
    </div>
    """, guild=guild, guild_id=guild_id, total=total, active=active, blocked=blocked,
       completed=completed, completion_rate=completion_rate, yes_count=yes_count, no_count=no_count,
       members=members, responses=response_rows, tasks=TASKS, task_count=len(TASKS))


@app.route("/member")
def member_dashboard():
    guild, member = member_from_request()
    progress = get_progress(guild.id, member.id)
    with db() as conn:
        history = conn.execute(
            "SELECT * FROM responses WHERE guild_id=? AND user_id=? ORDER BY id DESC",
            (guild.id, member.id)
        ).fetchall()

    current = TASKS[progress["task_index"]] if progress["task_index"] < len(TASKS) else None
    completed_count = progress["task_index"]
    percentage = round(completed_count / len(TASKS) * 100, 1) if TASKS else 100

    return render_template_string(BASE_STYLE + """
    <div class="container">
      <div class="nav"><a href="{{ url_for('member_dashboard', guild_id=guild.id, user_id=member.id) }}">Refresh</a></div>
      <h1>{{ member.display_name }}'s progress</h1>
      <p class="muted">{{ guild.name }}</p>
      <div class="grid">
        <div class="card"><div class="muted">Progress</div><div class="metric">{{ percentage }}%</div></div>
        <div class="card"><div class="muted">Completed</div><div class="metric">{{ completed_count }} / {{ task_count }}</div></div>
        <div class="card"><div class="muted">Status</div><div class="metric">{% if progress.completed_at %}Graduated{% elif progress.blocked_until %}Waiting{% else %}Active{% endif %}</div></div>
      </div>
      <div class="card"><h2>Current question</h2>{% if current %}<p><b>Step {{ progress.task_index + 1 }}:</b> {{ current.question }}</p><p><b>Stage:</b> {{ current.stage }}</p>{% else %}<p>You have completed every task.</p>{% endif %}<p><b>Next retry:</b> {{ progress.blocked_until or 'Not waiting' }}</p></div>
      <div class="card"><h2>Response history</h2><table><tr><th>Time</th><th>Step</th><th>Answer</th><th>Retry until</th></tr>{% for r in history %}<tr><td>{{ r.created_at }}</td><td>{{ r.task_index + 1 }}</td><td>{{ r.answer|upper }}</td><td>{{ r.retry_until or '—' }}</td></tr>{% endfor %}</table></div>
    </div>
    """, guild=guild, member=member, progress=progress, current=current,
       completed_count=completed_count, percentage=percentage, task_count=len(TASKS), history=history)


def run_dashboard():
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env first.")
    init_db()
    threading.Thread(target=run_dashboard, daemon=True).start()
    bot.run(TOKEN)
