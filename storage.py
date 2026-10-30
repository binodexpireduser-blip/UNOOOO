"""
storage.py - persistent storage for DrUNO.

Two copies of the data are kept:
  * SQLite  (local file)  - fast, used for everything the bot shows.
  * Turso   (cloud)       - the safe copy. Render wipes the local disk on every restart or
                            redeploy, so on start-up the bot compares both copies and fills
                            whichever one is missing or behind.

Nothing in the game ever waits for Turso: finished games are written to SQLite at once and
queued ("outbox"); a background thread copies the queue to Turso. If Turso is unreachable
the queue just waits and is retried, the bot keeps playing.

Turso is switched on by two environment variables (set them on Render):
    TURSO_DATABASE_URL   libsql://your-db-yourorg.turso.io   (https:// also works)
    TURSO_AUTH_TOKEN     the database token
Without them the bot runs on SQLite only.

What is stored (kept small on purpose):
  players     one row per player: all-time stats and the numbers behind the all-time records
  monthly     one row per player for the CURRENT month only (UTC). Rows of earlier months are
              deleted automatically; nothing is lost because players is updated after every game.
  channels    extra channels added with !callxlr8
  saved_games snapshots of games in progress (local only, deleted when the game ends)
  There is no per-game history.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

PLAYER_COLS = [
    "nick_lc", "display", "games", "wins", "points", "cards_played", "streak",
    "best_streak", "fastest_win", "best_game_points", "best_game_cards", "last_played",
]
MONTHLY_COLS = ["month"] + PLAYER_COLS

_PLAYER_DEFS = """
    nick_lc          TEXT NOT NULL,
    display          TEXT NOT NULL,
    games            INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    points           INTEGER NOT NULL DEFAULT 0,
    cards_played     INTEGER NOT NULL DEFAULT 0,
    streak           INTEGER NOT NULL DEFAULT 0,
    best_streak      INTEGER NOT NULL DEFAULT 0,
    fastest_win      INTEGER,
    best_game_points INTEGER NOT NULL DEFAULT 0,
    best_game_cards  INTEGER NOT NULL DEFAULT 0,
    last_played      REAL"""

DDL = [
    f"CREATE TABLE IF NOT EXISTS players ({_PLAYER_DEFS}, PRIMARY KEY (nick_lc))",
    f"CREATE TABLE IF NOT EXISTS monthly (month TEXT NOT NULL, {_PLAYER_DEFS}, PRIMARY KEY (month, nick_lc))",
    "CREATE TABLE IF NOT EXISTS channels (name_lc TEXT PRIMARY KEY, name TEXT NOT NULL)",
]


def _upsert_sql(table, row):
    """Write one absolute row. The WHERE keeps a row from ever being replaced by one with
    fewer games, so an empty or stale copy can never overwrite good totals."""
    cols = MONTHLY_COLS if table == "monthly" else PLAYER_COLS
    keys = ("month", "nick_lc") if table == "monthly" else ("nick_lc",)
    sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {sets} "
        f"WHERE excluded.games >= {table}.games"
    )
    return sql, [row.get(c) for c in cols]


class TursoClient:
    """Tiny Turso client over HTTPS (SQL over HTTP, /v2/pipeline) - standard library only."""

    def __init__(self, url, token, timeout=10):
        url = (url or "").strip()
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self.endpoint = url.rstrip("/") + "/v2/pipeline"
        self.token = (token or "").strip()
        self.timeout = timeout

    @staticmethod
    def _enc(value):
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": str(int(value))}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": value}
        return {"type": "text", "value": str(value)}

    @staticmethod
    def _dec(cell):
        kind = cell.get("type")
        if kind == "null":
            return None
        value = cell.get("value")
        if kind == "integer":
            return int(value)
        if kind == "float":
            return float(value)
        return value

    def run(self, statements):
        """statements = [(sql, [args])]. Returns one list of row-dicts per statement.
        Raises on any failure (the token is never put in an error message)."""
        requests = [
            {"type": "execute", "stmt": {"sql": sql, "args": [self._enc(a) for a in args]}}
            for sql, args in statements
        ]
        requests.append({"type": "close"})
        body = json.dumps({"requests": requests}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"Turso HTTP {ex.code}") from None
        results = data.get("results", [])
        out = []
        for i in range(len(statements)):
            if i >= len(results) or results[i].get("type") != "ok":
                msg = ""
                if i < len(results):
                    msg = str(results[i].get("error", {}).get("message", ""))[:100]
                raise RuntimeError(f"Turso statement failed: {msg}")
            res = results[i]["response"]["result"]
            cols = [c["name"] for c in res.get("cols", [])]
            out.append([dict(zip(cols, [self._dec(c) for c in row])) for row in res.get("rows", [])])
        return out


class Storage:
    def __init__(self, path=None, hidden_nicks=(), turso_url=None, turso_token=None, seed=True):
        if path is None:
            data_dir = os.environ.get("DATA_DIR", "data")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "druno.db")
        else:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)

        self.path = path
        # Nicks that are tracked but never shown on boards or records (the computer player).
        self.hidden = {n.lower() for n in hidden_nicks}
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
                try:
                    self._db.execute(pragma)
                except sqlite3.DatabaseError:
                    pass
            self._create_tables()

        url = turso_url if turso_url is not None else os.environ.get("TURSO_DATABASE_URL", "")
        token = turso_token if turso_token is not None else os.environ.get("TURSO_AUTH_TOKEN", "")
        self._remote = TursoClient(url, token) if url and token else None
        self._seed = seed
        self._reconciled = False
        self._last_ok = 0.0
        self._last_error = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker = None
        if self._remote:
            self._worker = threading.Thread(target=self._run_worker, name="turso-sync", daemon=True)
            self._worker.start()
        else:
            self._seed_if_needed()

    # ------------------------------------------------------------------
    # Local tables
    # ------------------------------------------------------------------
    def _create_tables(self):
        with self._db:
            for ddl in DDL:
                self._db.execute(ddl)
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS saved_games (
                       channel_lc TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"""
            )
            self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS outbox (
                       kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                       seq INTEGER NOT NULL, PRIMARY KEY (kind, key))"""
            )
        # A database from before the records feature gets the new columns.
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

    def _meta_get(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key, value):
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=3)
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # Outbox (what still has to be copied to Turso)
    # ------------------------------------------------------------------
    def _enqueue(self, kind, key, payload):
        """Remember the newest version of a row to copy to Turso (one entry per row)."""
        if not self._remote:
            return
        seq = int(self._meta_get("seq") or 0) + 1
        self._meta_set("seq", seq)
        self._db.execute(
            "INSERT OR REPLACE INTO outbox (kind, key, payload, seq) VALUES (?, ?, ?, ?)",
            (kind, key, json.dumps(payload, separators=(",", ":")), seq),
        )
        self._wake.set()

    def _enqueue_row(self, table, row):
        key = row["nick_lc"] if table == "players" else f"{row['month']}|{row['nick_lc']}"
        self._enqueue("player" if table == "players" else "monthly", key, dict(row))

    # ------------------------------------------------------------------
    # Games, leaderboard, stats
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

    def _write_row(self, table, row):
        cols = MONTHLY_COLS if table == "monthly" else PLAYER_COLS
        self._db.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row.get(c) for c in cols],
        )

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

                row = self._db.execute("SELECT * FROM players WHERE nick_lc = ?", (key,)).fetchone()
                new = self._next_stats(row, nick, won, points, duration, played)
                new.update(nick_lc=key, display=nick, last_played=now)
                self._write_row("players", new)
                self._enqueue_row("players", new)
                totals[key] = new["points"]

                mrow = self._db.execute(
                    "SELECT * FROM monthly WHERE month = ? AND nick_lc = ?", (month, key)
                ).fetchone()
                mnew = self._next_stats(mrow, nick, won, points, duration, played)
                mnew.update(month=month, nick_lc=key, display=nick, last_played=now)
                self._write_row("monthly", mnew)
                self._enqueue_row("monthly", mnew)
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

    def top_month(self, limit=10):
        """Top players by points of the current month (UTC)."""
        clause, params = self._hidden_clause()
        with self._lock:
            rows = self._db.execute(
                f"""SELECT display, points, wins, games FROM monthly
                    WHERE {clause} AND month = ?
                    ORDER BY points DESC, wins DESC, nick_lc ASC
                    LIMIT ?""",
                params + (self.current_month(), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def rank_of(self, nick):
        """Returns {'rank','total','display','points','wins','games'} or None."""
        key = nick.lower()
        if key in self.hidden:
            return None
        clause, params = self._hidden_clause()
        with self._lock:
            row = self._db.execute("SELECT * FROM players WHERE nick_lc = ?", (key,)).fetchone()
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
            self._enqueue("channel", name.lower(), {"name": name, "deleted": False})

    def remove_channel(self, name):
        with self._lock, self._db:
            self._db.execute("DELETE FROM channels WHERE name_lc = ?", (name.lower(),))
            self._enqueue("channel", name.lower(), {"name": name, "deleted": True})

    def get_channels(self):
        with self._lock:
            rows = self._db.execute("SELECT name FROM channels ORDER BY name_lc").fetchall()
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------
    # Running-game snapshots (local only: they change every move and only matter for minutes)
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
            rows = self._db.execute("SELECT channel_lc, data, updated FROM saved_games").fetchall()
        games = []
        for r in rows:
            try:
                games.append((r["channel_lc"], json.loads(r["data"]), r["updated"]))
            except ValueError:
                self.delete_game(r["channel_lc"])
        return games

    # ------------------------------------------------------------------
    # Status (for !unobotstatus)
    # ------------------------------------------------------------------
    def status(self):
        try:
            with self._lock:
                self._db.execute("SELECT 1").fetchone()
                pending = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            sqlite_ok = True
        except sqlite3.Error:
            sqlite_ok, pending = False, 0
        now = time.time()
        connected = bool(self._remote and self._reconciled and self._last_ok
                         and now - self._last_ok < 180 and not self._last_error)
        return {
            "sqlite_ok": sqlite_ok,
            "turso_configured": self._remote is not None,
            "turso_connected": connected,
            "turso_last_ok_age": (now - self._last_ok) if self._last_ok else None,
            "pending": pending,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------
    # Turso sync (runs in its own thread, never blocks the bot)
    # ------------------------------------------------------------------
    def _run_worker(self):
        backoff = 5
        last_ping = 0.0
        while not self._stop.is_set():
            try:
                if not self._reconciled:
                    self._remote_setup()
                    self._reconcile()
                    self._reconciled = True
                    self._seed_if_needed()
                while self._push_pending():
                    pass
                if time.time() - last_ping > 60:             # light health check while idle
                    self._remote.run([("SELECT 1", [])])
                    last_ping = time.time()
                self._last_ok = time.time()
                self._last_error = None
                backoff = 5
                self._wake.wait(timeout=30)
                self._wake.clear()
            except Exception as ex:
                self._last_error = f"{type(ex).__name__}: {ex}"[:120]
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 120)

    def _remote_setup(self):
        self._remote.run([(ddl, []) for ddl in DDL])

    def _push_pending(self):
        """Copy up to 40 queued rows to Turso. Returns False when the queue is empty."""
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, key, payload, seq FROM outbox ORDER BY seq LIMIT 40"
            ).fetchall()
        if not rows:
            return False
        statements = [("DELETE FROM monthly WHERE month <> ?", [self.current_month()])]
        for r in rows:
            payload = json.loads(r["payload"])
            if r["kind"] == "player":
                statements.append(_upsert_sql("players", payload))
            elif r["kind"] == "monthly":
                statements.append(_upsert_sql("monthly", payload))
            elif payload.get("deleted"):
                statements.append(("DELETE FROM channels WHERE name_lc = ?", [r["key"]]))
            else:
                statements.append((
                    "INSERT OR REPLACE INTO channels (name_lc, name) VALUES (?, ?)",
                    [r["key"], payload["name"]],
                ))
        self._remote.run(statements)
        with self._lock, self._db:
            for r in rows:                  # only if no newer version was queued meanwhile
                self._db.execute(
                    "DELETE FROM outbox WHERE kind = ? AND key = ? AND seq = ?",
                    (r["kind"], r["key"], r["seq"]),
                )
        return True

    def _reconcile(self):
        """Once per start-up: whichever side has more games for a player wins that row.
        Fresh local file -> filled from Turso. First time Turso is switched on -> filled from local."""
        month = self.current_month()
        remote_players, remote_monthly, remote_channels = self._remote.run([
            ("SELECT * FROM players", []),
            ("SELECT * FROM monthly WHERE month = ?", [month]),
            ("SELECT name_lc, name FROM channels", []),
        ])
        with self._lock, self._db:
            self._merge_rows("players", ("nick_lc",), remote_players, None)
            self._merge_rows("monthly", ("month", "nick_lc"), remote_monthly, month)
            self._merge_channels(remote_channels)

    def _merge_rows(self, table, keys, remote_rows, month):
        sql = f"SELECT * FROM {table}" + (" WHERE month = ?" if month else "")
        local = {tuple(r[k] for k in keys): dict(r)
                 for r in self._db.execute(sql, (month,) if month else ())}
        remote = {tuple(r[k] for k in keys): r for r in remote_rows}
        for key, r in remote.items():
            mine = local.get(key)
            if mine is None or (r.get("games") or 0) > mine["games"]:
                self._write_row(table, r)                       # Turso is ahead: take its row
            elif mine["games"] > (r.get("games") or 0):
                self._enqueue_row(table, mine)                  # local is ahead: copy it up
        for key, mine in local.items():
            if key not in remote:
                self._enqueue_row(table, mine)

    def _merge_channels(self, remote_rows):
        final = {r["name_lc"]: r["name"] for r in remote_rows}
        mine = {r["name_lc"]: r["name"] for r in self._db.execute("SELECT name_lc, name FROM channels")}
        pending = {
            r["key"]: json.loads(r["payload"])
            for r in self._db.execute("SELECT key, payload FROM outbox WHERE kind = 'channel'")
        }
        for key, name in mine.items():
            if key not in final and not pending.get(key, {}).get("deleted"):
                final[key] = name
                if key not in pending:
                    self._enqueue("channel", key, {"name": name, "deleted": False})
        for key, p in pending.items():
            if p.get("deleted"):
                final.pop(key, None)
            else:
                final[key] = p["name"]
        self._db.execute("DELETE FROM channels")
        for key, name in final.items():
            self._db.execute("INSERT INTO channels (name_lc, name) VALUES (?, ?)", (key, name))

    # ------------------------------------------------------------------
    # One-time import of the numbers that existed before Turso was added
    # ------------------------------------------------------------------
    def _seed_if_needed(self):
        """Fills an EMPTY database with SEED_PLAYERS, once. It never runs when any player
        already exists, so it cannot overwrite real data. It is safe to delete SEED_PLAYERS
        from this file after the first successful deploy."""
        if not self._seed:
            return
        with self._lock, self._db:
            if self._meta_get("seeded"):
                return
            empty = self._db.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
            self._meta_set("seeded", "1")
            if not empty:
                return
            now = time.time()
            month = self.current_month(now)
            for p in SEED_PLAYERS:
                row = dict(p, nick_lc=p["display"].lower(), last_played=now)
                self._write_row("players", row)
                self._enqueue_row("players", row)
                if month == SEED_MONTH:                  # those games were all played this month
                    mrow = dict(row, month=SEED_MONTH)
                    self._write_row("monthly", mrow)
                    self._enqueue_row("monthly", mrow)


# The leaderboard as it stood when Turso was added (all games were played in October 2026 UTC).
# Per-match records are only known where they were shown: heisty's 305-point game, Zeeshan's
# 61 cards in one game, and the single games of sasha and Danishhh.
SEED_MONTH = "2026-10"
SEED_PLAYERS = [
    dict(display="Juggu", games=45, wins=21, points=1734, cards_played=536, streak=2, best_streak=3, fastest_win=32, best_game_points=0, best_game_cards=0),
    dict(display="heisty", games=38, wins=19, points=1621, cards_played=413, streak=0, best_streak=3, fastest_win=27, best_game_points=305, best_game_cards=0),
    dict(display="Starboy", games=27, wins=13, points=747, cards_played=379, streak=5, best_streak=5, fastest_win=79, best_game_points=0, best_game_cards=0),
    dict(display="Antonio", games=25, wins=13, points=711, cards_played=287, streak=0, best_streak=4, fastest_win=51, best_game_points=0, best_game_cards=0),
    dict(display="CLONE", games=5, wins=2, points=316, cards_played=39, streak=2, best_streak=2, fastest_win=79, best_game_points=0, best_game_cards=0),
    dict(display="DaVinci", games=3, wins=3, points=185, cards_played=29, streak=3, best_streak=3, fastest_win=73, best_game_points=0, best_game_cards=0),
    dict(display="Stargirl", games=4, wins=2, points=120, cards_played=58, streak=0, best_streak=1, fastest_win=125, best_game_points=0, best_game_cards=0),
    dict(display="sasha", games=1, wins=1, points=118, cards_played=20, streak=1, best_streak=1, fastest_win=236, best_game_points=118, best_game_cards=20),
    dict(display="Danishhh", games=2, wins=1, points=84, cards_played=38, streak=0, best_streak=1, fastest_win=187, best_game_points=84, best_game_cards=0),
    dict(display="Zeeshan", games=8, wins=3, points=80, cards_played=118, streak=0, best_streak=2, fastest_win=45, best_game_points=0, best_game_cards=61),
]
