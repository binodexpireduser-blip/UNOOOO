"""
storage.py - persistent storage for DrUNO (SQLite, standard library only).

Keeps these things so nothing is lost when the bot disconnects, crashes or restarts:
  * players      - the leaderboard / stats and the all-time records (updated after EVERY game)
  * monthly      - the same numbers for the current month only (UTC). Rows of finished
                   months are deleted automatically; nothing is lost because the all-time
                   table was already updated at the end of every game.
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
    _PLAYER_COLUMNS = """
                       nick_lc         TEXT NOT NULL,
                       display         TEXT NOT NULL,
                       games           INTEGER NOT NULL DEFAULT 0,
                       wins            INTEGER NOT NULL DEFAULT 0,
                       points          INTEGER NOT NULL DEFAULT 0,
                       cards_played    INTEGER NOT NULL DEFAULT 0,
                       streak          INTEGER NOT NULL DEFAULT 0,
                       best_streak     INTEGER NOT NULL DEFAULT 0,
                       fastest_win     INTEGER,
                       best_game_points INTEGER NOT NULL DEFAULT 0,
                       best_game_cards  INTEGER NOT NULL DEFAULT 0,
                       last_played     REAL"""

    def _create_tables(self):
        with self._db:
            self._db.execute(
                f"""CREATE TABLE IF NOT EXISTS players (
                       {self._PLAYER_COLUMNS},
                       PRIMARY KEY (nick_lc)
                   )"""
            )
            self._db.execute(
                f"""CREATE TABLE IF NOT EXISTS monthly (
                       month TEXT NOT NULL,
                       {self._PLAYER_COLUMNS},
                       PRIMARY KEY (month, nick_lc)
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
        # An older database (from before the records feature) gets the new columns added.
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(players)")}
        with self._db:
            for column in ("best_game_points", "best_game_cards"):
                if column not in have:
                    self._db.execute(
                        f"ALTER TABLE players ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
        self._purge_old_months(self.current_month())

    @staticmethod
    def current_month(now=None):
        """Month key in UTC, e.g. '2026-10'."""
        return time.strftime("%Y-%m", time.gmtime(time.time() if now is None else now))

    def _purge_old_months(self, month):
        with self._lock, self._db:
            self._db.execute("DELETE FROM monthly WHERE month <> ?", (month,))

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # Leaderboard / stats
    # ------------------------------------------------------------------
    @staticmethod
    def _next_stats(row, nick, won, points, duration, played):
        """New numbers for one player after one game (row = their old row or None)."""
        old = dict(row) if row is not None else {}
        new = {
            "games": old.get("games", 0) + 1,
            "wins": old.get("wins", 0),
            "points": old.get("points", 0),
            "cards_played": old.get("cards_played", 0) + int(played),
            "streak": old.get("streak", 0),
            "best_streak": old.get("best_streak", 0),
            "fastest_win": old.get("fastest_win"),
            "best_game_points": old.get("best_game_points", 0),
            "best_game_cards": max(old.get("best_game_cards", 0), int(played)),
        }
        if won:
            new["wins"] += 1
            new["points"] += int(points)
            new["streak"] += 1
            new["best_streak"] = max(new["best_streak"], new["streak"])
            new["best_game_points"] = max(new["best_game_points"], int(points))
            secs = int(duration)
            if new["fastest_win"] is None or secs < new["fastest_win"]:
                new["fastest_win"] = secs
        else:
            new["streak"] = 0
        return new

    def record_game(self, participants, winner, points, duration, plays=None):
        """Store the result of a finished game (all-time AND current month).

        participants: nicks that were still in the game at the end (the winner included).
        Anyone who left mid-game is simply not in this list, so the game is erased for them.
        Returns {nick_lowercase: new_all_time_total_points}.
        """
        plays = plays or {}
        now = time.time()
        month = self.current_month(now)
        totals = {}
        winner_lc = winner.lower()
        with self._lock, self._db:
            self._db.execute("DELETE FROM monthly WHERE month <> ?", (month,))
            for nick in participants:
                key = nick.lower()
                won = key == winner_lc
                played = plays.get(key, 0)

                row = self._db.execute(
                    "SELECT * FROM players WHERE nick_lc = ?", (key,)
                ).fetchone()
                new = self._next_stats(row, nick, won, points, duration, played)
                self._db.execute(
                    """INSERT OR REPLACE INTO players
                       (nick_lc, display, games, wins, points, cards_played, streak, best_streak,
                        fastest_win, best_game_points, best_game_cards, last_played)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, nick, new["games"], new["wins"], new["points"], new["cards_played"],
                     new["streak"], new["best_streak"], new["fastest_win"],
                     new["best_game_points"], new["best_game_cards"], now),
                )
                totals[key] = new["points"]

                mrow = self._db.execute(
                    "SELECT * FROM monthly WHERE month = ? AND nick_lc = ?", (month, key)
                ).fetchone()
                mnew = self._next_stats(mrow, nick, won, points, duration, played)
                self._db.execute(
                    """INSERT OR REPLACE INTO monthly
                       (month, nick_lc, display, games, wins, points, cards_played, streak,
                        best_streak, fastest_win, best_game_points, best_game_cards, last_played)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (month, key, nick, mnew["games"], mnew["wins"], mnew["points"],
                     mnew["cards_played"], mnew["streak"], mnew["best_streak"],
                     mnew["fastest_win"], mnew["best_game_points"], mnew["best_game_cards"], now),
                )
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
    # Records (all-time, or the current month only)
    # ------------------------------------------------------------------
    def _best(self, table, month, column, smallest=False):
        clause, params = self._hidden_clause()
        where, args = [clause], list(params)
        if month:
            where.append("month = ?")
            args.append(month)
        if smallest:
            where.append(f"{column} IS NOT NULL")
            order = f"{column} ASC"
        else:
            where.append(f"{column} > 0")
            order = f"{column} DESC"
        row = self._db.execute(
            f"SELECT display, {column} AS v FROM {table} WHERE {' AND '.join(where)} "
            f"ORDER BY {order}, nick_lc ASC LIMIT 1",
            args,
        ).fetchone()
        return (row["display"], row["v"]) if row else None

    def records(self, monthly=False, min_games=10):
        """The record holders. Hidden nicks (the computer player) never hold a record.

        high_score: most points in one game | fastest: quickest win (seconds)
        cards: most cards played by one player in one game | streak: best win streak
        winrate: best win percentage among players with at least min_games games
        """
        table = "monthly" if monthly else "players"
        month = self.current_month() if monthly else None
        with self._lock:
            clause, params = self._hidden_clause()
            where, args = [clause, "games >= ?"], list(params) + [int(min_games)]
            if month:
                where.append("month = ?")
                args.append(month)
            rate = self._db.execute(
                f"SELECT display, wins * 100.0 / games AS v FROM {table} "
                f"WHERE {' AND '.join(where)} ORDER BY v DESC, games DESC, nick_lc ASC LIMIT 1",
                args,
            ).fetchone()
            return {
                "high_score": self._best(table, month, "best_game_points"),
                "fastest": self._best(table, month, "fastest_win", smallest=True),
                "cards": self._best(table, month, "best_game_cards"),
                "streak": self._best(table, month, "best_streak"),
                "winrate": (rate["display"], rate["v"]) if rate else None,
            }

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
