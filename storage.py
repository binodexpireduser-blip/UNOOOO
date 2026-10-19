"""
storage.py - persistent storage for DrUNO (SQLite, standard library only).

Keeps three things so nothing is lost when the bot disconnects, crashes or restarts:
  * players      - the leaderboard / stats
  * channels     - extra channels the bot was told to join (!callxlr8)
  * saved_games  - a snapshot of every running game, so a game survives a restart

Where the file lives:
  DATA_DIR env var (default "data")  ->  <DATA_DIR>/druno.db

IMPORTANT for Render: the normal filesystem is wiped on every deploy/restart of the
service. To keep the database for good, attach a Persistent Disk to the service and
set DATA_DIR to the disk's mount path (for example /var/data).
"""

import json
import os
import sqlite3
import threading
import time


class Storage:
    def __init__(self, path=None, hidden_nicks=()):
        if path is None:
            data_dir = os.environ.get("DATA_DIR", "data")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "druno.db")
        else:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)

        self.path = path
        # Nicks that are tracked but never shown on the leaderboard (the computer player).
        self.hidden = {n.lower() for n in hidden_nicks}
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            try:
                self._db.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass
            self._create_tables()

    # ------------------------------------------------------------------
    def _create_tables(self):
        with self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS players (
                       nick_lc      TEXT PRIMARY KEY,
                       display      TEXT NOT NULL,
                       games        INTEGER NOT NULL DEFAULT 0,
                       wins         INTEGER NOT NULL DEFAULT 0,
                       points       INTEGER NOT NULL DEFAULT 0,
                       cards_played INTEGER NOT NULL DEFAULT 0,
                       streak       INTEGER NOT NULL DEFAULT 0,
                       best_streak  INTEGER NOT NULL DEFAULT 0,
                       fastest_win  INTEGER,
                       last_played  REAL
                   )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS channels (
                       name_lc TEXT PRIMARY KEY,
                       name    TEXT NOT NULL
                   )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS saved_games (
                       channel_lc TEXT PRIMARY KEY,
                       data       TEXT NOT NULL,
                       updated    REAL NOT NULL
                   )"""
            )

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # Leaderboard / stats
    # ------------------------------------------------------------------
    def record_game(self, participants, winner, points, duration, plays=None):
        """Store the result of a finished game.

        participants: nicks that were still in the game at the end (the winner included).
        Anyone who left mid-game is simply not in this list, so the game is erased for them.
        Returns {nick_lowercase: new_total_points}.
        """
        plays = plays or {}
        now = time.time()
        totals = {}
        winner_lc = winner.lower()
        with self._lock, self._db:
            for nick in participants:
                key = nick.lower()
                row = self._db.execute(
                    "SELECT * FROM players WHERE nick_lc = ?", (key,)
                ).fetchone()
                if row is None:
                    games = wins = pts = cards = streak = best = 0
                    fastest = None
                else:
                    games, wins, pts = row["games"], row["wins"], row["points"]
                    cards, streak, best = row["cards_played"], row["streak"], row["best_streak"]
                    fastest = row["fastest_win"]

                games += 1
                cards += int(plays.get(key, 0))
                if key == winner_lc:
                    wins += 1
                    pts += int(points)
                    streak += 1
                    best = max(best, streak)
                    secs = int(duration)
                    if fastest is None or secs < fastest:
                        fastest = secs
                else:
                    streak = 0

                self._db.execute(
                    """INSERT OR REPLACE INTO players
                       (nick_lc, display, games, wins, points, cards_played,
                        streak, best_streak, fastest_win, last_played)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, nick, games, wins, pts, cards, streak, best, fastest, now),
                )
                totals[key] = pts
        return totals

    def _hidden_clause(self):
        if not self.hidden:
            return "1=1", ()
        marks = ",".join("?" for _ in self.hidden)
        return f"nick_lc NOT IN ({marks})", tuple(sorted(self.hidden))

    def top(self, limit=10):
        clause, params = self._hidden_clause()
        with self._lock:
            rows = self._db.execute(
                f"""SELECT display, points, wins, games FROM players
                    WHERE {clause}
                    ORDER BY points DESC, wins DESC, nick_lc ASC
                    LIMIT ?""",
                params + (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def rank_of(self, nick):
        """Returns {'rank','total','display','points','wins','games'} or None."""
        key = nick.lower()
        if key in self.hidden:
            return None
        clause, params = self._hidden_clause()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM players WHERE nick_lc = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            better = self._db.execute(
                f"""SELECT COUNT(*) FROM players
                    WHERE {clause}
                      AND (points > ?
                           OR (points = ? AND wins > ?)
                           OR (points = ? AND wins = ? AND nick_lc < ?))""",
                params
                + (row["points"], row["points"], row["wins"],
                   row["points"], row["wins"], key),
            ).fetchone()[0]
            total = self._db.execute(
                f"SELECT COUNT(*) FROM players WHERE {clause}", params
            ).fetchone()[0]
        return {
            "rank": better + 1,
            "total": total,
            "display": row["display"],
            "points": row["points"],
            "wins": row["wins"],
            "games": row["games"],
        }

    def stats(self, nick):
        """Full stats for one player (the computer player included) or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM players WHERE nick_lc = ?", (nick.lower(),)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["win_rate"] = round(100.0 * data["wins"] / data["games"]) if data["games"] else 0
        return data

    # ------------------------------------------------------------------
    # Extra channels (added with !callxlr8, removed with !leavexlr8)
    # ------------------------------------------------------------------
    def add_channel(self, name):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO channels (name_lc, name) VALUES (?, ?)",
                (name.lower(), name),
            )

    def remove_channel(self, name):
        with self._lock, self._db:
            self._db.execute("DELETE FROM channels WHERE name_lc = ?", (name.lower(),))

    def get_channels(self):
        with self._lock:
            rows = self._db.execute("SELECT name FROM channels ORDER BY name_lc").fetchall()
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------
    # Running-game snapshots (so a restart does not kill a game in progress)
    # ------------------------------------------------------------------
    def save_game(self, channel, data):
        payload = json.dumps(data, separators=(",", ":"))
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO saved_games (channel_lc, data, updated) VALUES (?, ?, ?)",
                (channel.lower(), payload, time.time()),
            )

    def delete_game(self, channel):
        with self._lock, self._db:
            self._db.execute("DELETE FROM saved_games WHERE channel_lc = ?", (channel.lower(),))

    def load_games(self):
        """Returns [(channel_lowercase, data_dict, updated_timestamp), ...]."""
        with self._lock:
            rows = self._db.execute(
                "SELECT channel_lc, data, updated FROM saved_games"
            ).fetchall()
        games = []
        for r in rows:
            try:
                games.append((r["channel_lc"], json.loads(r["data"]), r["updated"]))
            except ValueError:
                self.delete_game(r["channel_lc"])
        return games
