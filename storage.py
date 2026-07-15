import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "guess_game.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS games (
                game_date TEXT,
                mode TEXT,               -- 'pitcher' or 'batter'
                player_id INTEGER,
                player_name TEXT,
                title TEXT,
                tracker_message_id TEXT,
                PRIMARY KEY (game_date, mode)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS guesses (
                game_date TEXT,
                mode TEXT,
                user_id TEXT,
                user_name TEXT,
                guess TEXT,
                correct INTEGER,
                PRIMARY KEY (game_date, mode, user_id)
            )
        """)


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))


def get_config(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def save_game(game_date: str, mode: str, player_id: int, player_name: str, title: str):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO games (game_date, mode, player_id, player_name, title) VALUES (?, ?, ?, ?, ?)",
            (game_date, mode, player_id, player_name, title),
        )


def get_game(game_date: str, mode: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM games WHERE game_date = ? AND mode = ?", (game_date, mode)
        ).fetchone()
        return dict(row) if row else None


def set_tracker_message(game_date: str, mode: str, message_id: str):
    with _conn() as c:
        c.execute(
            "UPDATE games SET tracker_message_id = ? WHERE game_date = ? AND mode = ?",
            (message_id, game_date, mode),
        )


def add_guess(game_date: str, mode: str, user_id: str, user_name: str, guess: str, correct: bool) -> bool:
    """Returns False if this user already guessed today (one guess each)."""
    with _conn() as c:
        try:
            c.execute(
                "INSERT INTO guesses (game_date, mode, user_id, user_name, guess, correct) VALUES (?, ?, ?, ?, ?, ?)",
                (game_date, mode, user_id, user_name, guess, int(correct)),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_guesses(game_date: str, mode: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM guesses WHERE game_date = ? AND mode = ? ORDER BY rowid",
            (game_date, mode),
        ).fetchall()
        return [dict(r) for r in rows]


def get_leaderboard(limit: int = 10) -> list[dict]:
    """Combined across BOTH games -- getting the pitcher and batter right
    on the same day counts as 2-0."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT user_name,
                   SUM(correct) AS wins,
                   COUNT(*) - SUM(correct) AS losses,
                   COUNT(*) AS total
            FROM guesses
            GROUP BY user_id
            ORDER BY wins DESC, total ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_all_guesses():
    """Wipes the leaderboard (all guess history, both games)."""
    with _conn() as c:
        c.execute("DELETE FROM guesses")
