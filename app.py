import os
import threading
import time

from flask import Flask, jsonify

from bot import DrUnoBot, BOT_PLAYER
from storage import Storage

app = Flask(__name__)

# --- settings (override with environment variables on Render) -----------------
IRC_SERVER = os.environ.get("IRC_SERVER", "irc.hybridirc.com")
IRC_PORT = int(os.environ.get("IRC_PORT", "6697"))
IRC_CHANNELS = [c.strip() for c in os.environ.get("IRC_CHANNELS", "#global").split(",") if c.strip()]
# The bot's login is written here on purpose, so nothing has to be set on Render for it
# (an old IRC_NICK / IRC_REALNAME / IRC_PASSWORD value on Render can no longer change it).
IRC_NICK = "DrUno"
IRC_REALNAME = "DrUno"
IRC_PASSWORD = "heyhey123"      # NickServ password of the nick above

# One storage object for the whole life of the process: it survives bot restarts.
# Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN on Render to switch on the cloud backup.
store = Storage(hidden_nicks=[BOT_PLAYER])

STATE = {"bot": None, "restarts": 0, "started": time.time()}


@app.route("/")
def home():
    return "DrUNO IRC Bot is running 24/7! 🎴"


@app.route("/health")
def health():
    bot = STATE["bot"]
    try:
        connected = bool(bot and bot.connection.is_connected())
        games = len(bot.games) if bot else 0
    except Exception:
        connected, games = False, 0
    db = store.status()
    return jsonify(
        status="ok",
        irc_connected=connected,
        active_games=games,
        restarts=STATE["restarts"],
        uptime_seconds=int(time.time() - STATE["started"]),
        sqlite_ok=db["sqlite_ok"],
        turso_configured=db["turso_configured"],
        turso_connected=db["turso_connected"],
        turso_pending=db["pending"],
    )


def run_irc():
    while True:
        bot = None
        try:
            bot = DrUnoBot(store, IRC_CHANNELS, IRC_NICK, IRC_REALNAME, IRC_PASSWORD, IRC_SERVER, IRC_PORT,
                           started_at=STATE["started"], restarts=STATE["restarts"])
            STATE["bot"] = bot
            bot.start()
        except Exception as e:
            print(f"[!] IRC Bot crashed with error: {e}. Restarting in 15 seconds...")
        finally:
            STATE["restarts"] += 1
            try:
                if bot is not None:
                    bot.connection.disconnect("Restarting")
            except Exception:
                pass
        time.sleep(15)


if __name__ == "__main__":
    # IRC bot runs in a background thread
    irc_thread = threading.Thread(target=run_irc, daemon=True)
    irc_thread.start()

    # Flask keeps the Render web service awake (point UptimeRobot at its URL)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
