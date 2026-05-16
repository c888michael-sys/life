import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file(Path(__file__).with_name(".env"))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_PASSWORD = os.getenv("BOT_ACCESS_PASSWORD", "michael999")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("BOT_ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DB_PATH = Path(__file__).with_name("bot_data.sqlite3")


@dataclass
class User:
    id: int
    telegram_id: int
    name: str
    is_authenticated: bool
    reminder_short_muted: bool
    reminder_long_muted: bool


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    is_authenticated INTEGER NOT NULL DEFAULT 0,
                    reminder_short_muted INTEGER NOT NULL DEFAULT 0,
                    reminder_long_muted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS income_categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, name),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS income_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, category_id, name),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(category_id) REFERENCES income_categories(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS income_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    entry_type TEXT NOT NULL,
                    income_type_id INTEGER,
                    note TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(income_type_id) REFERENCES income_types(id) ON DELETE SET NULL,
                    CHECK(entry_type IN ('gained', 'used'))
                );

                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    goal_type TEXT NOT NULL,
                    is_completed INTEGER NOT NULL DEFAULT 0,
                    last_reminded_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    CHECK(goal_type IN ('short', 'long'))
                );

                CREATE TABLE IF NOT EXISTS workouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    worked_out_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )

    def upsert_user(self, telegram_id: int, name: str, authenticated: bool) -> User:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET name = ?, is_authenticated = ?
                    WHERE telegram_id = ?
                    """,
                    (name, int(authenticated), telegram_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users (telegram_id, name, is_authenticated, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (telegram_id, name, int(authenticated), now),
                )
            result = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return self._row_to_user(result)

    def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def set_user_auth(self, telegram_id: int, is_authenticated: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_authenticated = ? WHERE telegram_id = ?",
                (int(is_authenticated), telegram_id),
            )

    def set_reminder_mute(self, user_id: int, goal_type: str, muted: bool) -> None:
        column = "reminder_short_muted" if goal_type == "short" else "reminder_long_muted"
        with self._connect() as conn:
            conn.execute(
                f"UPDATE users SET {column} = ? WHERE id = ?",  # nosec B608
                (int(muted), user_id),
            )

    def add_category(self, user_id: int, name: str) -> bool:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO income_categories (user_id, name, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, name, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_category(self, user_id: int, category_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM income_categories WHERE user_id = ? AND id = ?",
                (user_id, category_id),
            )

    def get_categories(self, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        return rows

    def add_income_type(self, user_id: int, category_id: int, name: str) -> bool:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO income_types (user_id, category_id, name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, category_id, name, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_income_type(self, user_id: int, income_type_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM income_types WHERE user_id = ? AND id = ?",
                (user_id, income_type_id),
            )

    def get_income_types(self, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.name, c.name AS category_name
                FROM income_types t
                JOIN income_categories c ON c.id = t.category_id
                WHERE t.user_id = ?
                ORDER BY c.name, t.name
                """,
                (user_id,),
            ).fetchall()
        return rows

    def get_income_types_by_category(self, user_id: int, category_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name
                FROM income_types
                WHERE user_id = ? AND category_id = ?
                ORDER BY name
                """,
                (user_id, category_id),
            ).fetchall()
        return rows

    def add_income_entry(
        self,
        user_id: int,
        amount: float,
        entry_type: str,
        income_type_id: int | None = None,
        note: str | None = None,
    ) -> None:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO income_entries (user_id, amount, entry_type, income_type_id, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, amount, entry_type, income_type_id, note, now),
            )

    def income_summary(self, user_id: int) -> dict[str, float]:
        with self._connect() as conn:
            gained = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM income_entries WHERE user_id = ? AND entry_type = 'gained'",
                (user_id,),
            ).fetchone()["total"]
            used = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM income_entries WHERE user_id = ? AND entry_type = 'used'",
                (user_id,),
            ).fetchone()["total"]
        return {
            "gained": float(gained),
            "used": float(used),
            "net": float(gained) - float(used),
        }

    def add_goal(self, user_id: int, goal_type: str, title: str) -> None:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO goals (user_id, title, goal_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, title, goal_type, now),
            )

    def list_goals(self, user_id: int, goal_type: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT id, title, goal_type, is_completed, created_at FROM goals WHERE user_id = ?"
        params: list[Any] = [user_id]
        if goal_type:
            query += " AND goal_type = ?"
            params.append(goal_type)
        query += " ORDER BY is_completed ASC, created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return rows

    def list_open_goals(self, user_id: int, goal_type: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, goal_type, created_at, last_reminded_at
                FROM goals
                WHERE user_id = ? AND goal_type = ? AND is_completed = 0
                ORDER BY created_at DESC
                """,
                (user_id, goal_type),
            ).fetchall()
        return rows

    def list_completed_goals(self, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, goal_type, created_at
                FROM goals
                WHERE user_id = ? AND is_completed = 1
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return rows

    def complete_goal(self, user_id: int, goal_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE goals SET is_completed = 1 WHERE user_id = ? AND id = ? AND is_completed = 0",
                (user_id, goal_id),
            )
        return result.rowcount > 0

    def users_for_short_reminders(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, telegram_id, name
                FROM users
                WHERE is_authenticated = 1 AND reminder_short_muted = 0
                """
            ).fetchall()

    def users_for_long_reminders(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, telegram_id, name
                FROM users
                WHERE is_authenticated = 1 AND reminder_long_muted = 0
                """
            ).fetchall()

    def goals_due_for_long_reminder(self, user_id: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(tz=SYDNEY_TZ) - timedelta(days=14)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title
                FROM goals
                WHERE user_id = ?
                  AND goal_type = 'long'
                  AND is_completed = 0
                  AND (last_reminded_at IS NULL OR last_reminded_at <= ?)
                ORDER BY created_at DESC
                """,
                (user_id, cutoff),
            ).fetchall()
        return rows

    def mark_goals_reminded(self, goal_ids: list[int]) -> None:
        if not goal_ids:
            return
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        placeholders = ",".join("?" for _ in goal_ids)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE goals SET last_reminded_at = ? WHERE id IN ({placeholders})",  # nosec B608
                (now, *goal_ids),
            )

    def list_users(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, telegram_id, name, is_authenticated, created_at
                FROM users
                ORDER BY created_at DESC
                """
            ).fetchall()

    def remove_user_by_telegram_id(self, telegram_id: int) -> bool:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        return result.rowcount > 0

    def add_workout_entry(self, user_id: int) -> None:
        now = datetime.now(tz=SYDNEY_TZ).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO workouts (user_id, worked_out_at) VALUES (?, ?)",
                (user_id, now),
            )

    def workout_stats(self, user_id: int) -> dict[str, float]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    MIN(worked_out_at) AS first_workout,
                    MAX(worked_out_at) AS last_workout
                FROM workouts
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

        total = int(row["total"] or 0)
        if total == 0 or not row["first_workout"]:
            return {"total": 0, "avg_per_week": 0.0, "avg_per_day": 0.0}

        first_workout = datetime.fromisoformat(row["first_workout"])
        now = datetime.now(tz=SYDNEY_TZ)
        tracked_days = max(1, (now.date() - first_workout.date()).days + 1)
        avg_per_day = total / tracked_days
        avg_per_week = avg_per_day * 7

        return {
            "total": float(total),
            "avg_per_week": avg_per_week,
            "avg_per_day": avg_per_day,
        }

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            telegram_id=row["telegram_id"],
            name=row["name"],
            is_authenticated=bool(row["is_authenticated"]),
            reminder_short_muted=bool(row["reminder_short_muted"]),
            reminder_long_muted=bool(row["reminder_long_muted"]),
        )


db = Database(DB_PATH)


# Persistent main-menu (reply keyboard) labels
MAIN_BTN_INCOME = "Income"
MAIN_BTN_GOALS = "Goals"
MAIN_BTN_WORKOUT = "Workout"
MAIN_BTN_SETUP = "Setup"
MAIN_MENU_LABELS = {MAIN_BTN_INCOME, MAIN_BTN_GOALS, MAIN_BTN_WORKOUT, MAIN_BTN_SETUP}


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MAIN_BTN_INCOME, MAIN_BTN_GOALS],
            [MAIN_BTN_WORKOUT, MAIN_BTN_SETUP],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def income_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add", callback_data="income_add")],
            [InlineKeyboardButton("Minus", callback_data="income_minus")],
            [InlineKeyboardButton("View", callback_data="income_view")],
            [InlineKeyboardButton("Manage Jobs", callback_data="income_manage")],
            [InlineKeyboardButton("Back", callback_data="main_back")],
        ]
    )


def goals_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add Short-Term Goal", callback_data="goals_add_short")],
            [InlineKeyboardButton("Add Long-Term Goal", callback_data="goals_add_long")],
            [InlineKeyboardButton("View Goals", callback_data="goals_view")],
            [InlineKeyboardButton("Complete Goal", callback_data="goals_complete")],
            [InlineKeyboardButton("Back", callback_data="main_back")],
        ]
    )


def workout_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Worked Out", callback_data="workout_log")],
            [InlineKeyboardButton("View Average", callback_data="workout_view")],
            [InlineKeyboardButton("Back", callback_data="main_back")],
        ]
    )


def workout_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes", callback_data="workout_confirm_yes")],
            [InlineKeyboardButton("No", callback_data="workout_confirm_no")],
        ]
    )


def setup_menu_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Mute Short-Term Reminders", callback_data="setup_mute_short")],
        [InlineKeyboardButton("Unmute Short-Term Reminders", callback_data="setup_unmute_short")],
        [InlineKeyboardButton("Mute Long-Term Reminders", callback_data="setup_mute_long")],
        [InlineKeyboardButton("Unmute Long-Term Reminders", callback_data="setup_unmute_long")],
        [InlineKeyboardButton("Reminder Status", callback_data="setup_status")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("Admin Panel", callback_data="setup_admin")])
    rows.append([InlineKeyboardButton("Back", callback_data="main_back")])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("List Users", callback_data="admin_list_users")],
            [InlineKeyboardButton("Remove User", callback_data="admin_remove_user")],
            [InlineKeyboardButton("Back", callback_data="main_setup")],
        ]
    )


async def send_or_edit(update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(text=text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=keyboard)


async def send_main_menu_message(update: Update, text: str = "Main menu") -> None:
    # Always send a fresh message so the persistent reply keyboard attaches properly.
    # If invoked from an inline callback, strip the inline buttons on the source message
    # so the chat doesn't keep a stale floating menu around.
    if update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=main_reply_keyboard())


async def force_refresh_main_menu(update: Update, text: str = "Main menu") -> None:
    # Used by /menu — bust any stale persistent-keyboard cache on the client by sending
    # remove_keyboard first, then re-attaching the current layout.
    await update.effective_message.reply_text("Refreshing menu...", reply_markup=ReplyKeyboardRemove())
    await update.effective_message.reply_text(text, reply_markup=main_reply_keyboard())


def clear_user_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "awaiting",
        "auth_name",
        "income_amount",
        "income_category",
        "goal_type",
        "selected_category_for_new_type",
    ):
        context.user_data.pop(key, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_user_flow(context)
    telegram_id = update.effective_user.id
    existing_user = db.get_user_by_telegram_id(telegram_id)

    if existing_user and existing_user.is_authenticated:
        await send_main_menu_message(update, f"Welcome back, {existing_user.name}.")
        return

    context.user_data["awaiting"] = "auth_name"
    await update.message.reply_text("Enter your name to begin:")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    await update.message.reply_text(
        "Use /start to log in.\n"
        "Use /menu to open the main menu.\n"
        "Main sections: Income, Goals, Workout, Setup.\n"
        "Admin panel appears in Setup for admin IDs."
    )


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_authenticated(update, context)
    if not user:
        return
    if not is_admin(user.telegram_id):
        await update.message.reply_text("Admin access only.")
        return
    await update.message.reply_text("Admin panel", reply_markup=admin_menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_authenticated(update, context)
    if not user:
        return
    clear_user_flow(context)
    await force_refresh_main_menu(update)


async def ensure_authenticated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> User | None:
    _ = context
    telegram_id = update.effective_user.id
    user = db.get_user_by_telegram_id(telegram_id)
    if user and user.is_authenticated:
        return user

    message = "Please use /start and log in first."
    if update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    else:
        await update.effective_message.reply_text(message)
    return None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_authenticated(update, context)
    if not user:
        return

    query = update.callback_query
    data = query.data

    if data == "main_back":
        clear_user_flow(context)
        await send_main_menu_message(update)
        return

    if data == "main_income":
        clear_user_flow(context)
        await send_or_edit(update, "Income menu", income_menu_keyboard())
        return

    if data == "main_goals":
        clear_user_flow(context)
        await send_or_edit(update, "Goals menu", goals_menu_keyboard())
        return

    if data == "main_workout":
        clear_user_flow(context)
        await send_or_edit(update, "Workout menu", workout_menu_keyboard())
        return

    if data == "main_setup":
        clear_user_flow(context)
        await send_or_edit(
            update,
            "Setup menu",
            setup_menu_keyboard(is_admin(update.effective_user.id)),
        )
        return

    if data == "income_add":
        context.user_data["awaiting"] = "income_add_amount"
        await send_or_edit(update, "Enter amount to add (e.g. 120.50):")
        return

    if data == "income_minus":
        context.user_data["awaiting"] = "income_minus_amount"
        await send_or_edit(update, "Enter amount to subtract/use (e.g. 45):")
        return

    if data == "income_view":
        summary = db.income_summary(user.id)
        text = (
            "Income summary\n"
            f"Total gained: ${summary['gained']:.2f}\n"
            f"Total used: ${summary['used']:.2f}\n"
            f"Net income: ${summary['net']:.2f}"
        )
        await send_or_edit(update, text, income_menu_keyboard())
        return

    if data == "income_manage":
        await send_or_edit(update, "Manage jobs and categories", income_manage_keyboard())
        return

    if data == "income_add_category":
        context.user_data["awaiting"] = "add_category_name"
        await send_or_edit(update, "Send the new category name:")
        return

    if data == "income_delete_category":
        categories = db.get_categories(user.id)
        if not categories:
            await send_or_edit(update, "No categories yet.", income_manage_keyboard())
            return
        rows = [
            [InlineKeyboardButton(c["name"], callback_data=f"delete_category:{c['id']}")]
            for c in categories
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="income_manage")])
        await send_or_edit(update, "Select category to delete:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("delete_category:"):
        category_id = int(data.split(":", 1)[1])
        db.delete_category(user.id, category_id)
        await send_or_edit(update, "Category deleted.", income_manage_keyboard())
        return

    if data == "income_add_type":
        categories = db.get_categories(user.id)
        if not categories:
            await send_or_edit(
                update,
                "Create a category first.",
                income_manage_keyboard(),
            )
            return
        rows = [
            [InlineKeyboardButton(c["name"], callback_data=f"add_type_category:{c['id']}")]
            for c in categories
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="income_manage")])
        await send_or_edit(update, "Pick a category for the new job type:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("add_type_category:"):
        category_id = int(data.split(":", 1)[1])
        context.user_data["selected_category_for_new_type"] = category_id
        context.user_data["awaiting"] = "add_income_type_name"
        await send_or_edit(update, "Send the job type name (e.g. Cafe Shift):")
        return

    if data == "income_delete_type":
        types_ = db.get_income_types(user.id)
        if not types_:
            await send_or_edit(update, "No job types yet.", income_manage_keyboard())
            return
        rows = [
            [
                InlineKeyboardButton(
                    f"{row['name']} ({row['category_name']})",
                    callback_data=f"delete_type:{row['id']}",
                )
            ]
            for row in types_
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="income_manage")])
        await send_or_edit(update, "Select job type to delete:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("delete_type:"):
        type_id = int(data.split(":", 1)[1])
        db.delete_income_type(user.id, type_id)
        await send_or_edit(update, "Job type deleted.", income_manage_keyboard())
        return

    if data.startswith("income_add_choose_category:"):
        category_id = int(data.split(":", 1)[1])
        context.user_data["income_category"] = category_id
        types_ = db.get_income_types_by_category(user.id, category_id)
        if not types_:
            await send_or_edit(
                update,
                "No job type exists in this category. Add one in Manage Jobs.",
                income_menu_keyboard(),
            )
            clear_user_flow(context)
            return

        rows = [
            [InlineKeyboardButton(t["name"], callback_data=f"income_add_type:{t['id']}")]
            for t in types_
        ]
        rows.append([InlineKeyboardButton("Cancel", callback_data="main_income")])
        await send_or_edit(update, "Pick the job type:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("income_add_type:"):
        type_id = int(data.split(":", 1)[1])
        amount = float(context.user_data["income_amount"])
        db.add_income_entry(
            user_id=user.id,
            amount=amount,
            entry_type="gained",
            income_type_id=type_id,
        )
        clear_user_flow(context)
        await send_or_edit(update, f"Added ${amount:.2f} income.", income_menu_keyboard())
        return

    if data == "goals_add_short":
        context.user_data["goal_type"] = "short"
        context.user_data["awaiting"] = "goal_title"
        await send_or_edit(update, "Send your short-term goal text:")
        return

    if data == "goals_add_long":
        context.user_data["goal_type"] = "long"
        context.user_data["awaiting"] = "goal_title"
        await send_or_edit(update, "Send your long-term project goal text:")
        return

    if data == "goals_view":
        open_short = db.list_open_goals(user.id, "short")
        open_long = db.list_open_goals(user.id, "long")

        if not open_short and not open_long:
            await send_or_edit(update, "No open goals.", goals_menu_keyboard())
            return

        lines = ["Open goals:"]
        open_goals = open_short + open_long
        for goal in open_goals:
            goal_type = "Short" if goal["goal_type"] == "short" else "Long"
            lines.append(f"#{goal['id']} [{goal_type}] {goal['title']}")
        await send_or_edit(update, "\n".join(lines), goals_menu_keyboard())
        return

    if data == "goals_complete":
        goals = db.list_open_goals(user.id, "short") + db.list_open_goals(user.id, "long")
        if not goals:
            await send_or_edit(update, "No open goals.", goals_menu_keyboard())
            return

        rows = [
            [InlineKeyboardButton(g["title"], callback_data=f"goal_complete:{g['id']}")]
            for g in goals
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="main_goals")])
        await send_or_edit(update, "Select goal to mark complete:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("goal_complete:"):
        goal_id = int(data.split(":", 1)[1])
        completed = db.complete_goal(user.id, goal_id)
        text = "Goal marked complete." if completed else "Goal not found or already complete."
        await send_or_edit(update, text, goals_menu_keyboard())
        return

    if data == "workout_log":
        await send_or_edit(update, "Did you work out? Confirm below.", workout_confirm_keyboard())
        return

    if data == "workout_confirm_yes":
        db.add_workout_entry(user.id)
        await send_or_edit(update, "Workout logged.", workout_menu_keyboard())
        return

    if data == "workout_confirm_no":
        await send_or_edit(update, "No workout logged.", workout_menu_keyboard())
        return

    if data == "workout_view":
        stats = db.workout_stats(user.id)
        if stats["total"] == 0:
            await send_or_edit(update, "No workouts logged yet.", workout_menu_keyboard())
            return
        text = (
            "Workout stats\n"
            f"Total workouts logged: {int(stats['total'])}\n"
            f"Average per week: {stats['avg_per_week']:.2f}\n"
            f"Average per day: {stats['avg_per_day']:.2f}"
        )
        await send_or_edit(update, text, workout_menu_keyboard())
        return

    if data == "setup_mute_short":
        db.set_reminder_mute(user.id, "short", True)
        await send_or_edit(update, "Short-term reminders muted.", setup_menu_keyboard(is_admin(user.telegram_id)))
        return

    if data == "setup_unmute_short":
        db.set_reminder_mute(user.id, "short", False)
        await send_or_edit(update, "Short-term reminders unmuted.", setup_menu_keyboard(is_admin(user.telegram_id)))
        return

    if data == "setup_mute_long":
        db.set_reminder_mute(user.id, "long", True)
        await send_or_edit(update, "Long-term reminders muted.", setup_menu_keyboard(is_admin(user.telegram_id)))
        return

    if data == "setup_unmute_long":
        db.set_reminder_mute(user.id, "long", False)
        await send_or_edit(update, "Long-term reminders unmuted.", setup_menu_keyboard(is_admin(user.telegram_id)))
        return

    if data == "setup_status":
        refreshed = db.get_user_by_telegram_id(user.telegram_id)
        short_status = "Muted" if refreshed.reminder_short_muted else "Active"
        long_status = "Muted" if refreshed.reminder_long_muted else "Active"
        msg = (
            "Reminder status\n"
            f"Short-term (daily 8:00 PM Sydney): {short_status}\n"
            f"Long-term (every 2 weeks): {long_status}"
        )
        await send_or_edit(update, msg, setup_menu_keyboard(is_admin(user.telegram_id)))
        return

    if data == "setup_admin":
        if not is_admin(user.telegram_id):
            await query.answer("Admin access only.", show_alert=True)
            return
        await send_or_edit(update, "Admin panel", admin_menu_keyboard())
        return

    if data == "admin_list_users":
        if not is_admin(user.telegram_id):
            await query.answer("Admin access only.", show_alert=True)
            return
        users = db.list_users()
        if not users:
            await send_or_edit(update, "No users found.", admin_menu_keyboard())
            return
        lines = ["Users:"]
        for row in users:
            auth_state = "yes" if row["is_authenticated"] else "no"
            lines.append(f"{row['name']} | tg:{row['telegram_id']} | auth:{auth_state}")
        await send_or_edit(update, "\n".join(lines), admin_menu_keyboard())
        return

    if data == "admin_remove_user":
        if not is_admin(user.telegram_id):
            await query.answer("Admin access only.", show_alert=True)
            return
        users = db.list_users()
        removable = [row for row in users if row["telegram_id"] != user.telegram_id]
        if not removable:
            await send_or_edit(update, "No removable users.", admin_menu_keyboard())
            return
        rows = [
            [
                InlineKeyboardButton(
                    f"{row['name']} ({row['telegram_id']})",
                    callback_data=f"admin_remove:{row['telegram_id']}",
                )
            ]
            for row in removable
        ]
        rows.append([InlineKeyboardButton("Back", callback_data="setup_admin")])
        await send_or_edit(update, "Select a user to remove:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("admin_remove:"):
        if not is_admin(user.telegram_id):
            await query.answer("Admin access only.", show_alert=True)
            return
        target_tg_id = int(data.split(":", 1)[1])
        removed = db.remove_user_by_telegram_id(target_tg_id)
        result_text = "User removed." if removed else "User not found."
        await send_or_edit(update, result_text, admin_menu_keyboard())
        return

    await query.answer("Action not recognized.")


def income_manage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add Category", callback_data="income_add_category")],
            [InlineKeyboardButton("Delete Category", callback_data="income_delete_category")],
            [InlineKeyboardButton("Add Job Type", callback_data="income_add_type")],
            [InlineKeyboardButton("Delete Job Type", callback_data="income_delete_type")],
            [InlineKeyboardButton("Back", callback_data="main_income")],
        ]
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")
    telegram_id = update.effective_user.id

    if awaiting == "auth_name":
        context.user_data["auth_name"] = text
        context.user_data["awaiting"] = "auth_password"
        await update.message.reply_text("Enter password:")
        return

    if awaiting == "auth_password":
        name = context.user_data.get("auth_name", "User")
        if text == BOT_PASSWORD:
            db.upsert_user(telegram_id, name, authenticated=True)
            clear_user_flow(context)
            await update.message.reply_text(
                f"Welcome, {name}. Access granted.",
                reply_markup=main_reply_keyboard(),
            )
        else:
            clear_user_flow(context)
            await update.message.reply_text("Incorrect password. Use /start to try again.")
        return

    user = db.get_user_by_telegram_id(telegram_id)
    if not user or not user.is_authenticated:
        await update.message.reply_text("Please use /start first.")
        return

    # Persistent main-menu buttons always navigate, even mid-flow (escape hatch).
    if text in MAIN_MENU_LABELS:
        clear_user_flow(context)
        if text == MAIN_BTN_INCOME:
            await update.message.reply_text("Income menu", reply_markup=income_menu_keyboard())
        elif text == MAIN_BTN_GOALS:
            await update.message.reply_text("Goals menu", reply_markup=goals_menu_keyboard())
        elif text == MAIN_BTN_WORKOUT:
            await update.message.reply_text("Workout menu", reply_markup=workout_menu_keyboard())
        elif text == MAIN_BTN_SETUP:
            await update.message.reply_text(
                "Setup menu",
                reply_markup=setup_menu_keyboard(is_admin(telegram_id)),
            )
        return

    if awaiting == "income_add_amount":
        amount = parse_amount(text)
        if amount is None:
            await update.message.reply_text("Please send a valid positive number.")
            return
        categories = db.get_categories(user.id)
        if not categories:
            clear_user_flow(context)
            await update.message.reply_text(
                "No categories found. Create one from Income -> Manage Jobs.",
                reply_markup=income_menu_keyboard(),
            )
            return

        context.user_data["income_amount"] = amount
        rows = [
            [InlineKeyboardButton(c["name"], callback_data=f"income_add_choose_category:{c['id']}")]
            for c in categories
        ]
        rows.append([InlineKeyboardButton("Cancel", callback_data="main_income")])
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "Choose income category:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if awaiting == "income_minus_amount":
        amount = parse_amount(text)
        if amount is None:
            await update.message.reply_text("Please send a valid positive number.")
            return
        db.add_income_entry(user_id=user.id, amount=amount, entry_type="used", note="manual minus")
        clear_user_flow(context)
        await update.message.reply_text(
            f"Subtracted ${amount:.2f}.",
            reply_markup=income_menu_keyboard(),
        )
        return

    if awaiting == "add_category_name":
        created = db.add_category(user.id, text)
        clear_user_flow(context)
        msg = "Category created." if created else "Category already exists."
        await update.message.reply_text(msg, reply_markup=income_manage_keyboard())
        return

    if awaiting == "add_income_type_name":
        category_id = context.user_data.get("selected_category_for_new_type")
        if category_id is None:
            clear_user_flow(context)
            await update.message.reply_text(
                "No category selected. Try again.", reply_markup=income_manage_keyboard()
            )
            return
        created = db.add_income_type(user.id, int(category_id), text)
        clear_user_flow(context)
        msg = "Job type created." if created else "That job type already exists in this category."
        await update.message.reply_text(msg, reply_markup=income_manage_keyboard())
        return

    if awaiting == "goal_title":
        goal_type = context.user_data.get("goal_type")
        if goal_type not in {"short", "long"}:
            clear_user_flow(context)
            await update.message.reply_text("Goal type missing. Try again from Goals menu.")
            return
        db.add_goal(user.id, goal_type, text)
        clear_user_flow(context)
        goal_label = "short-term" if goal_type == "short" else "long-term"
        await update.message.reply_text(
            f"Added {goal_label} goal.", reply_markup=goals_menu_keyboard()
        )
        return

    await update.message.reply_text(
        "Use the menu buttons to continue.", reply_markup=main_reply_keyboard()
    )


def parse_amount(raw: str) -> float | None:
    try:
        amount = float(raw)
    except ValueError:
        return None
    if amount <= 0:
        return None
    return amount


async def reminder_short_goals(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = db.users_for_short_reminders()
    for row in users:
        goals = db.list_open_goals(row["id"], "short")
        if not goals:
            continue
        lines = [
            "8:00 PM reminder: short-term goals to complete:",
            *[f"- {goal['title']}" for goal in goals],
        ]
        try:
            await context.bot.send_message(chat_id=row["telegram_id"], text="\n".join(lines))
        except Exception as exc:
            logger.warning("Failed sending short reminder to %s: %s", row["telegram_id"], exc)


async def reminder_long_goals(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = db.users_for_long_reminders()
    for row in users:
        goals = db.goals_due_for_long_reminder(row["id"])
        if not goals:
            continue
        lines = [
            "2-week reminder: long-term project goals:",
            *[f"- {goal['title']}" for goal in goals],
        ]
        try:
            await context.bot.send_message(chat_id=row["telegram_id"], text="\n".join(lines))
            db.mark_goals_reminded([goal["id"] for goal in goals])
        except Exception as exc:
            logger.warning("Failed sending long reminder to %s: %s", row["telegram_id"], exc)


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in environment before starting the bot.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.job_queue.run_daily(
        reminder_short_goals,
        time=time(hour=20, minute=0, tzinfo=SYDNEY_TZ),
        name="short_goal_daily_8pm_sydney",
    )

    # Runs every day at 8:05 PM Sydney and only sends long-goal reminders if 14 days passed.
    application.job_queue.run_daily(
        reminder_long_goals,
        time=time(hour=20, minute=5, tzinfo=SYDNEY_TZ),
        name="long_goal_2week_check",
    )

    return application


DUPLICATE_ALERT_STATE = Path(__file__).with_name(".duplicate_alert_count")
DUPLICATE_ALERT_MAX = 3


def _telegram_call(token: str, method: str, payload: dict | None = None, timeout: float = 15) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": exc.code, "description": str(exc)}


def check_for_duplicate_poller(token: str, admin_ids: set[int]) -> None:
    me = _telegram_call(token, "getMe", timeout=10)
    if not me.get("ok"):
        logger.error("getMe failed during startup probe: %s", me)
        return

    username = me["result"]["username"]
    logger.info("Bot @%s starting as PID %d (life bot)", username, os.getpid())

    probe = _telegram_call(token, "getUpdates", {"timeout": 0, "limit": 1}, timeout=15)
    if probe.get("error_code") != 409:
        try:
            DUPLICATE_ALERT_STATE.unlink()
        except FileNotFoundError:
            pass
        return

    try:
        count = int(DUPLICATE_ALERT_STATE.read_text().strip())
    except (FileNotFoundError, ValueError):
        count = 0

    if count < DUPLICATE_ALERT_MAX and admin_ids:
        host = os.uname().nodename if hasattr(os, "uname") else "unknown"
        text = (
            f"life bot: another instance is polling.\n"
            f"Host: {host}\n"
            f"PID refusing to start: {os.getpid()}\n"
            f"Alert {count + 1}/{DUPLICATE_ALERT_MAX} — will go silent after this.\n"
            f"Fix: ssh in, find the duplicate (ps -ef | grep 'life/bot.py'), "
            f"check /proc/<pid>/cgroup, kill the one not under life-bot.service."
        )
        for admin_id in admin_ids:
            send_result = _telegram_call(
                token, "sendMessage", {"chat_id": admin_id, "text": text}, timeout=10
            )
            if not send_result.get("ok"):
                logger.warning("Failed to send duplicate-alert to %s: %s", admin_id, send_result)
        try:
            DUPLICATE_ALERT_STATE.write_text(str(count + 1))
        except Exception as exc:
            logger.warning("Failed to write alert state: %s", exc)

    logger.error(
        "409 Conflict on startup probe — another instance is polling. Refusing to start (alert %d/%d).",
        min(count + 1, DUPLICATE_ALERT_MAX),
        DUPLICATE_ALERT_MAX,
    )
    sys.exit(1)


def main() -> None:
    check_for_duplicate_poller(BOT_TOKEN, ADMIN_IDS)
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
