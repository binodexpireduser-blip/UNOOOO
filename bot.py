"""
bot.py - DrUNO, an UNO bot for IRC.

Two layers, kept apart on purpose:
  1. UnoGame  - the pure game engine (no IRC, no sockets). Easy to test.
  2. DrUnoBot - the IRC side: connection, commands, timers, flood-safe sending,
                leaderboard commands and saving/restoring games via storage.py.

Settings come from environment variables (see the CONFIG block). Nothing secret is
hard-coded: set IRC_PASSWORD and UNO_SECRET on Render.
"""

import os
import random
import re
import ssl
import time
import traceback
from collections import Counter, deque

import irc.bot
import irc.connection

from storage import Storage


# ============================================================================
# CONFIG
# ============================================================================
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


BOT_PLAYER = os.environ.get("UNO_BOT_PLAYER", "Khiladi-420")  # the computer opponent
MIN_PLAYERS = 2
MAX_PLAYERS = 4
HAND_SIZE = 7
LOBBY_SECONDS = _env_int("UNO_LOBBY_SECONDS", 20)
TURN_TIMEOUT = _env_int("UNO_TURN_TIMEOUT", 45)       # seconds a human may think
BOT_THINK_SECONDS = _env_float("UNO_BOT_THINK", 2.0)  # pause before the computer plays
RESUME_MAX_AGE = _env_int("UNO_RESUME_MAX_AGE", 900)  # drop saved games older than this
SEND_INTERVAL = _env_float("IRC_SEND_INTERVAL", 0.6)  # seconds per outgoing line (flood safety)
SEND_BURST = 4                                        # lines that may go out back-to-back

# ============================================================================
# mIRC COLOURS + CARD DISPLAY
# ============================================================================
CC = "\x03"
RESET = "\x0f"
BOLD = "\x02"

COLORS = ["Red", "Green", "Blue", "Yellow"]
COLOR_BG = {"Red": "04", "Blue": "12", "Green": "03", "Yellow": "08"}
COLOR_FG = {"Red": "00", "Blue": "00", "Green": "00", "Yellow": "01"}  # white / black text

WILD = "Wild"
WD4 = "Wild Draw Four"
ACTIONS = ("Skip", "Reverse", "Draw Two")


PINK = f"{CC}13"                                  # accent colour (numbers, "wins", "jo" ...)
GREEN = f"{CC}09"                                 # the computer player's name
WILD_BG = "15"                                    # Wild look: purple/blue/pink/green letters on light grey
WILD_CYCLE = ["06", "02", "13", "03"]
RAINBOW = ["04", "08", "09", "12", "06", "07"]   # red, yellow, green, blue, purple, orange (the word UNO)
_VALUE_LABEL = {"Draw Two": "Draw2"}


def rainbow(text, bg=None):
    """Every letter in its own colour (bg = optional background colour code)."""
    out = []
    for i, ch in enumerate(text):
        fg = RAINBOW[i % len(RAINBOW)]
        out.append(f"{CC}{fg},{bg}{ch}" if bg else f"{CC}{fg}{ch}")
    return "".join(out)


def wild_block(label):
    """Wild / WildDrawFour exactly like the reference picture."""
    parts = []
    for i, ch in enumerate(label):
        fg = WILD_CYCLE[i % len(WILD_CYCLE)]
        pad = " " if i == 0 else ""
        parts.append(f"{CC}{fg},{WILD_BG}{pad}{ch}")
    return "".join(parts) + " " + RESET


def fmt(card):
    """A card as a compact coloured block: Blue2, RedDraw2, GreenSkip, Wild, WildDrawFour."""
    color, value = card
    if color == "Wild":
        return wild_block("WildDrawFour" if value == WD4 else "Wild")
    label = _VALUE_LABEL.get(value, value)
    return f"{CC}{COLOR_FG[color]},{COLOR_BG[color]} {color}{label} {RESET}"


def color_block(color):
    return f"{CC}{COLOR_FG[color]},{COLOR_BG[color]} {color} {RESET}"


def b(nick):
    return f"{BOLD}{nick}{RESET}"


_BOT_NAME_RE = re.compile(r"(?<![\w-])" + re.escape(BOT_PLAYER) + r"(?![\w-])", re.IGNORECASE)
_UNO_RE = re.compile(r"(?<![\w-])UNO(?![\w-])", re.IGNORECASE)


def decorate(text):
    """Applied to everything the bot sends: the computer player's name in green, 'UNO' in rainbow."""
    text = _BOT_NAME_RE.sub(lambda m: f"{GREEN}{m.group(0)}{CC}", text)
    text = _UNO_RE.sub(lambda m: rainbow(m.group(0)) + CC, text)
    return text


# ============================================================================
# DECK + CARD PARSING
# ============================================================================
def build_deck():
    """The standard 108-card UNO deck."""
    deck = []
    for color in COLORS:
        for value in [str(n) for n in range(10)] + list(ACTIONS):
            deck.append((color, value))
            if value != "0":
                deck.append((color, value))
    for _ in range(4):
        deck.append(("Wild", WILD))
        deck.append(("Wild", WD4))
    return deck


def card_points(card):
    color, value = card
    if color == "Wild":
        return 50
    if value.isdigit():
        return int(value)
    return 20


def card_sort_key(card):
    color, value = card
    order = COLORS.index(color) if color in COLORS else len(COLORS)
    if value.isdigit():
        v = int(value)
    elif value in ACTIONS:
        v = 10 + ACTIONS.index(value)
    else:
        v = 20 + (1 if value == WD4 else 0)
    return (order, v)


_COLOR_WORDS = {"red": "r", "green": "g", "blue": "b", "yellow": "y"}
_COLOR_LETTERS = {"r": "Red", "g": "Green", "b": "Blue", "y": "Yellow"}
_ACTION_ALIASES = {
    "s": "Skip", "skip": "Skip",
    "r": "Reverse", "rev": "Reverse", "reverse": "Reverse",
    "d": "Draw Two", "d2": "Draw Two", "dt": "Draw Two", "+2": "Draw Two",
    "draw2": "Draw Two", "drawtwo": "Draw Two",
}
_WILD_ALIASES = {"w", "wild"}
_WD4_ALIASES = {"wd", "wd4", "d4", "+4", "w4", "wild4", "wilddraw4", "wilddrawfour"}


def parse_card(text):
    """Turn user input ('r5', 'rs', 'rd2', 'wd', 'wd4', 'red 5' ...) into a card tuple, or None."""
    t = re.sub(r"\s+", "", (text or "").lower())
    if not t:
        return None
    if t in _WILD_ALIASES:
        return ("Wild", WILD)
    if t in _WD4_ALIASES:
        return ("Wild", WD4)
    for word, letter in _COLOR_WORDS.items():
        if t.startswith(word):
            t = letter + t[len(word):]
            break
    if len(t) < 2 or t[0] not in _COLOR_LETTERS:
        return None
    rest = t[1:]
    if rest.isdigit():
        number = int(rest)
        if number > 9:
            return None
        value = str(number)
    elif rest in _ACTION_ALIASES:
        value = _ACTION_ALIASES[rest]
    else:
        return None
    return (_COLOR_LETTERS[t[0]], value)


def parse_color(text):
    t = (text or "").strip().lower()
    if t in _COLOR_WORDS:
        t = _COLOR_WORDS[t]
    return _COLOR_LETTERS.get(t)


def is_playable(card, color, value):
    return card[0] == "Wild" or card[0] == color or card[1] == value


def fmt_duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m:{seconds % 60:02d}s"


# ============================================================================
# GAME ENGINE (no IRC in here)
# ============================================================================
class Out:
    """What a game action wants said: channel lines, private notices, maybe a result."""

    def __init__(self):
        self.msgs = []
        self.notices = []
        self.result = None


class UnoGame:
    LOBBY = "LOBBY"
    PLAYING = "PLAYING"
    AWAIT_COLOR = "AWAIT_COLOR"
    OVER = "OVER"

    def __init__(self, channel, bot_name=BOT_PLAYER):
        self.channel = channel
        self.bot_name = bot_name
        self.state = self.LOBBY
        self.players = []          # display nicks in seating order
        self.hands = {}            # lowercase nick -> [cards]
        self.plays = {}            # lowercase nick -> number of cards they played
        self.strikes = {}          # lowercase nick -> consecutive timeouts
        self.draw_pile = []
        self.discard_pile = []     # last card = the top card
        self.turn_idx = 0
        self.direction = 1
        self.color = None
        self.value = None
        self.has_drawn = False
        self.cards_played = 0
        self.started_at = None
        self.version = 0           # bumped on every real state change
        self.serial = 0            # used by the bot layer to ignore stale timers
        self.out = Out()

    # ---- tiny helpers ------------------------------------------------------
    def say(self, text):
        self.out.msgs.append(text)

    def note(self, nick, text):
        self.out.notices.append((nick, text))

    def take_out(self):
        out, self.out = self.out, Out()
        return out

    def _bump(self):
        self.version += 1

    def _index(self, nick):
        low = nick.lower()
        for i, p in enumerate(self.players):
            if p.lower() == low:
                return i
        return -1

    def has_player(self, nick):
        return self._index(nick) >= 0

    def is_bot(self, nick):
        return nick.lower() == self.bot_name.lower()

    def humans(self):
        return [p for p in self.players if not self.is_bot(p)]

    def current(self):
        if self.state in (self.PLAYING, self.AWAIT_COLOR) and self.players:
            return self.players[self.turn_idx]
        return None

    def is_current(self, nick):
        cur = self.current()
        return cur is not None and cur.lower() == nick.lower()

    def _next_idx(self, steps=1):
        return (self.turn_idx + steps * self.direction) % len(self.players)

    def advance(self, steps=1):
        self.turn_idx = self._next_idx(steps)
        self.has_drawn = False
        self._bump()

    def top_str(self):
        if self.value in (WILD, WD4):
            return f"{fmt(('Wild', self.value))} {color_block(self.color)}"
        return fmt((self.color, self.value))

    def announce_turn(self):
        cur = self.current()
        if cur and not self.is_bot(cur):
            self.say(f"It is now {BOLD}{cur}'s{RESET} turn.")
            self.note_hand(cur)

    def hand_text(self, nick, sort=False):
        hand = self.hands.get(nick.lower(), [])
        if sort:
            hand.sort(key=card_sort_key)
        return " ".join(fmt(c) for c in hand) or "(no cards)"

    def note_hand(self, nick, sort=False):
        if not self.is_bot(nick):
            label = "Sorted Hand" if sort else "Your Hand"
            self.note(nick, f"{label}: {self.hand_text(nick, sort)}")

    # ---- deck --------------------------------------------------------------
    def _draw_one(self):
        if not self.draw_pile and len(self.discard_pile) > 1:
            top = self.discard_pile.pop()
            self.draw_pile = self.discard_pile
            self.discard_pile = [top]
            random.shuffle(self.draw_pile)
            self.say("Reshuffling the discard pile...")
        return self.draw_pile.pop() if self.draw_pile else None

    def _note_drew(self, nick, cards):
        if cards and not self.is_bot(nick):
            self.note(nick, "You drew: " + " ".join(fmt(c) for c in cards))

    def _give(self, nick, count):
        cards = []
        for _ in range(count):
            card = self._draw_one()
            if card is None:
                break
            self.hands[nick.lower()].append(card)
            cards.append(card)
        return cards

    # ---- lobby -------------------------------------------------------------
    def join(self, nick):
        if self.state != self.LOBBY or self.has_player(nick) or self.is_bot(nick):
            return
        if len(self.players) >= MAX_PLAYERS:
            self.note(nick, f"❌ The game is full ({MAX_PLAYERS}/{MAX_PLAYERS}).")
            return
        self.players.append(nick)
        self.say(f"{nick} joins UNO! ({len(self.players)} players)")
        self._bump()
        if len(self.players) >= MAX_PLAYERS:
            self.start()

    def finish_lobby(self):
        if self.state != self.LOBBY:
            return
        if not self.humans():
            self.state = self.OVER
            self.out.result = {"cancelled": True}
            return
        if len(self.players) < MIN_PLAYERS:
            self.players.append(self.bot_name)
            self.say(f"{self.bot_name} joins UNO and is playing!")
        self.start()

    def start(self):
        self.draw_pile = build_deck()
        random.shuffle(self.draw_pile)
        self.discard_pile = []
        for p in self.players:
            self.hands[p.lower()] = []
            self.plays[p.lower()] = 0
            self.strikes[p.lower()] = 0
            self._give(p, HAND_SIZE)

        starter = self.draw_pile.pop()
        while starter[0] == "Wild":               # a Wild may not start the game
            self.draw_pile.insert(0, starter)
            starter = self.draw_pile.pop()
        self.discard_pile.append(starter)
        self.color, self.value = starter
        self.state = self.PLAYING
        self.started_at = time.time()
        self.turn_idx = 0
        self.direction = 1
        self.has_drawn = False

        self.say(f"{BOLD}Welcome to UNO!{RESET}")
        self.say(f"{len(self.players)} players this round: {' '.join(self.players)}")

        first = self.players[0]
        if self.value == "Skip":
            self.say(f"{first} is skipped!")
            self.turn_idx = self._next_idx(1)
        elif self.value == "Reverse":
            if len(self.players) == 2:
                self.say(f"Reverse! {first} is skipped!")
                self.turn_idx = self._next_idx(1)
            else:
                self.direction = -1
                self.say("Direction reversed!")
        elif self.value == "Draw Two":
            drew = self._give(first, 2)
            self.say(f"{first} draws {len(drew)} cards and is skipped!")
            self.turn_idx = self._next_idx(1)
        self._bump()
        self.say(f"{b(self.current())} plays first... The top card is {self.top_str()}")
        for p in self.humans():
            self.note_hand(p)

    # ---- player actions ----------------------------------------------------
    def play(self, nick, text, explicit=True):
        """explicit=False is a bare card name: wrong input is ignored quietly (it may just be chat)."""
        if self.state == self.AWAIT_COLOR and self.is_current(nick):
            if explicit:
                self.note(nick, "❌ Choose a color first: co r / co g / co b / co y")
            return
        if self.state != self.PLAYING or not self.is_current(nick):
            return
        hand = self.hands[nick.lower()]
        card = parse_card(text)
        if card is None or card not in hand:
            if explicit:
                self.note(nick, "❌ You don't have that card, type 'dr' to draw or play another.")
            return
        if not is_playable(card, self.color, self.value):
            if explicit:
                self.note(nick, "❌ Invalid play! Must match current pile color or value.")
            return
        self._do_play(nick, card)

    def _do_play(self, nick, card):
        low = nick.lower()
        hand = self.hands[low]
        hand.remove(card)
        self.discard_pile.append(card)
        self.cards_played += 1
        self.plays[low] = self.plays.get(low, 0) + 1
        self.strikes[low] = 0
        self.has_drawn = False
        color, value = card
        self.value = value
        self._bump()

        if color == "Wild":
            if value == WD4:
                victim = self.players[self._next_idx(1)]
                drew = self._give(victim, 4)
                self.say(f"{b(nick)} plays {fmt(card)} 🔥 {b(victim)} draws {len(drew)} cards and is skipped...")
                self._note_drew(victim, drew)
            else:
                self.say(f"{b(nick)} plays {fmt(card)}")
            if not hand:
                return self._win(nick)
            self._uno_check(nick)
            self.state = self.AWAIT_COLOR
            if self.is_bot(nick):
                self._bot_choose_color(nick)
            else:
                self.say(f"{b(nick)}, choose a color: co r / co g / co b / co y")
            return

        self.color = color
        two = len(self.players) == 2
        nxt = self.players[self._next_idx(1)]
        if value == "Skip":
            self.say(f"{b(nick)} plays {fmt(card)}, {nxt} is skipped")
        elif value == "Reverse":
            if two:
                self.say(f"{b(nick)} plays {fmt(card)}, {nxt} is skipped")
            else:
                self.say(f"{b(nick)} plays {fmt(card)}, direction reversed")
        elif value == "Draw Two":
            drew = self._give(nxt, 2)
            self.say(f"{b(nick)} plays {fmt(card)} {b(nxt)} draws {len(drew)} cards and is skipped")
            self._note_drew(nxt, drew)
        else:
            self.say(f"{b(nick)} plays {fmt(card)} to {b(nxt)}")

        if not hand:
            return self._win(nick)
        self._uno_check(nick)

        if value in ("Skip", "Draw Two"):
            self.advance(2)
        elif value == "Reverse":
            if two:
                self.advance(2)
            else:
                self.direction *= -1
                self.advance(1)
        else:
            self.advance(1)
        self.announce_turn()

    def _uno_check(self, nick):
        if len(self.hands[nick.lower()]) == 1:
            self.say(f"⚠️ {BOLD}{nick}{RESET} has UNO!")

    def choose_color(self, nick, text):
        if self.state != self.AWAIT_COLOR or not self.is_current(nick):
            return
        color = parse_color(text)
        if color is None:
            self.note(nick, "❌ Invalid color choice. Use r, g, b or y")
            return
        self.strikes[nick.lower()] = 0
        self._apply_color(nick, color)

    def _apply_color(self, nick, color):
        self.color = color
        self.state = self.PLAYING
        self.say(f"🎨 {b(nick)} chose {color_block(color)} Play continues!")
        self.advance(2 if self.value == WD4 else 1)
        self.announce_turn()

    def draw(self, nick):
        if self.state != self.PLAYING or not self.is_current(nick):
            return
        if self.has_drawn:
            self.note(nick, "❌ You already drew a card! Play it with 'pl' or type 'pa' to pass.")
            return
        self.strikes[nick.lower()] = 0
        card = self._draw_one()
        if card is None:
            self.say(f"{nick} can't draw - no cards left. Turn passes.")
            self.advance(1)
            self.announce_turn()
            return
        self.hands[nick.lower()].append(card)
        self.has_drawn = True
        self._bump()
        self.note(nick, f"You drew: {fmt(card)}")
        self.say(f"{b(nick)} drew a card. They can play a card or type 'pa' to pass.")

    def pass_turn(self, nick):
        if self.state != self.PLAYING or not self.is_current(nick):
            return
        if not self.has_drawn:
            self.note(nick, "❌ You must draw a card ('dr') before you can pass your turn.")
            return
        self.strikes[nick.lower()] = 0
        self.say(f"⏩ {b(nick)} passes their turn.")
        self.advance(1)
        self.announce_turn()

    # ---- the computer player ----------------------------------------------
    def bot_turn(self):
        """One full turn for the computer player (draws first if it has to)."""
        if self.state != self.PLAYING:
            return
        nick = self.current()
        if nick is None or not self.is_bot(nick):
            return
        hand = self.hands[nick.lower()]
        playable = [c for c in hand if is_playable(c, self.color, self.value)]
        if not playable:
            card = self._draw_one()
            if card is None:
                self.say(f"{nick} can't draw - no cards left.")
                self.advance(1)
                self.announce_turn()
                return
            hand.append(card)
            self.say(f"{nick} drew a card")
            if is_playable(card, self.color, self.value):
                playable = [card]
            else:
                self.say(f"⏩ {nick} passes their turn.")
                self.advance(1)
                self.announce_turn()
                return
        self._do_play(nick, self._bot_pick(nick, playable))

    def _bot_pick(self, nick, playable):
        hand = self.hands[nick.lower()]
        opponent_cards = len(self.hands[self.players[self._next_idx(1)].lower()])
        plain = [c for c in playable if c[0] != "Wild"]
        if plain:
            pool = plain
            if opponent_cards <= 2:                      # hit them while they're nearly out
                attacks = [c for c in plain if c[1] in ACTIONS]
                if attacks:
                    pool = attacks
            counts = Counter(c[0] for c in hand)
            best = max(counts[c[0]] for c in pool)       # keep the colour we hold most
            return random.choice([c for c in pool if counts[c[0]] == best])
        wilds = [c for c in playable if c[1] == WD4] if opponent_cards <= 3 else []
        return random.choice(wilds or [c for c in playable if c[1] == WILD] or playable)

    def _bot_choose_color(self, nick):
        counts = Counter(c[0] for c in self.hands[nick.lower()] if c[0] != "Wild")
        color = counts.most_common(1)[0][0] if counts else random.choice(COLORS)
        self._apply_color(nick, color)

    # ---- ending ------------------------------------------------------------
    def _end(self, winner, points, cards_taken):
        self.state = self.OVER
        duration = time.time() - (self.started_at or time.time())
        self.say(f"🏆 {BOLD}{winner}{RESET} {PINK}wins {points}{CC} Points by taking {PINK}{cards_taken}{CC} Cards.")
        self.say(f"Cards played: {PINK}{self.cards_played}{CC} Cards in {PINK}{fmt_duration(duration)}{CC}")
        self.out.result = {
            "winner": winner,
            "points": points,
            "participants": list(self.players),
            "duration": duration,
            "cards_played": self.cards_played,
            "plays": dict(self.plays),
        }
        self._bump()

    def _win(self, nick):
        low = nick.lower()
        leftovers = [c for p in self.players if p.lower() != low for c in self.hands[p.lower()]]
        self._end(nick, sum(card_points(c) for c in leftovers), len(leftovers))

    # ---- leaving -----------------------------------------------------------
    def remove(self, nick, reason="left the game"):
        """!remove, QUIT, PART, KICK and timeouts all end up here."""
        idx = self._index(nick)
        if idx < 0 or self.is_bot(nick) or self.state == self.OVER:
            return
        leaver = self.players[idx]
        low = leaver.lower()

        if self.state == self.LOBBY:
            self.players.pop(idx)
            self.say(f"{leaver} left the lobby.")
            self._bump()
            if not self.players:
                self.state = self.OVER
                self.out.result = {"cancelled": True}
            return

        was_current = idx == self.turn_idx
        was_choosing = was_current and self.state == self.AWAIT_COLOR
        hand = self.hands.pop(low, [])
        self.plays.pop(low, None)
        self.strikes.pop(low, None)
        other_humans = [p for p in self.humans() if p.lower() != low]
        bot_in = any(self.is_bot(p) for p in self.players)
        self._bump()

        if not other_humans:
            if bot_in:                                   # forfeit: the computer wins
                self.players.pop(idx)
                self.say(f"{leaver} {reason}.")
                self._end(self.bot_name, sum(card_points(c) for c in hand), len(hand))
            else:
                self.players.pop(idx)
                self.state = self.OVER
                self.out.result = {"cancelled": True}
            return

        if len(other_humans) == 1 and not bot_in:        # two humans, one left: computer takes the seat
            self.players[idx] = self.bot_name
            self.hands[self.bot_name.lower()] = hand
            self.plays[self.bot_name.lower()] = 0
            self.strikes[self.bot_name.lower()] = 0
            self.say(f"{leaver} {reason}. {self.bot_name} takes over their cards!")
            if was_choosing:
                self._bot_choose_color(self.bot_name)
            elif was_current:
                self.has_drawn = False
            return

        # three or more humans: the rest simply play on
        self.discard_pile[0:0] = hand
        self.players.pop(idx)
        n = len(self.players)
        if was_current:
            self.turn_idx = idx % n if self.direction == 1 else (idx - 1) % n
            self.has_drawn = False
        elif idx < self.turn_idx:
            self.turn_idx -= 1
        self.say(f"{leaver} {reason}. Their cards go back to the pile.")
        if was_choosing:
            color = random.choice(COLORS)
            self.color = color
            self.state = self.PLAYING
            self.say(f"🎨 The color was picked automatically: {color_block(color)}")
            if self.value == WD4:
                self.advance(1)
        if was_current:
            self.announce_turn()

    def timeout(self):
        """The current human ran out of time."""
        nick = self.current()
        if nick is None or self.is_bot(nick):
            return
        low = nick.lower()
        self.strikes[low] = self.strikes.get(low, 0) + 1
        if self.strikes[low] >= 2:
            self.remove(nick, reason="timed out twice and was removed from the game")
            return
        if self.state == self.AWAIT_COLOR:
            counts = Counter(c[0] for c in self.hands[low] if c[0] != "Wild")
            color = counts.most_common(1)[0][0] if counts else random.choice(COLORS)
            self.say(f"⏰ {nick} took too long, a color was picked automatically. (strike 1/2)")
            self._apply_color(nick, color)
            return
        if not self.has_drawn:
            card = self._draw_one()
            if card is not None:
                self.hands[low].append(card)
                self.note(nick, f"You drew: {fmt(card)}")
        self.say(f"⏰ {nick} took too long: auto-draw and pass. (strike 1/2)")
        self.advance(1)
        self.announce_turn()

    def rename(self, old, new):
        idx = self._index(old)
        if idx < 0:
            return
        self.players[idx] = new
        ol, nl = old.lower(), new.lower()
        if ol != nl:
            for table in (self.hands, self.plays, self.strikes):
                if ol in table:
                    table[nl] = table.pop(ol)
        self._bump()

    # ---- info commands -----------------------------------------------------
    def info_order(self):
        names = []
        for i, p in enumerate(self.players):
            if self.state != self.LOBBY and i == self.turn_idx:
                names.append(f"👉 {BOLD}{p}{RESET}")
            else:
                names.append(p)
        arrow = "➡️" if self.direction == 1 else "⬅️"
        self.say(f"Order ({arrow}): {' | '.join(names)}")

    def info_top(self):
        if self.state in (self.PLAYING, self.AWAIT_COLOR):
            self.say(f"Current pile top card: {self.top_str()}")

    def info_turn(self):
        cur = self.current()
        if cur:
            self.say(f"It is currently {BOLD}{cur}'s{RESET} turn.")

    def info_counts(self):
        if self.state in (self.PLAYING, self.AWAIT_COLOR):
            counts = [f"{p}: {len(self.hands.get(p.lower(), []))} cards" for p in self.players]
            self.say(f"Card Counts: {' | '.join(counts)}")

    def info_stats(self):
        if self.state in (self.PLAYING, self.AWAIT_COLOR):
            elapsed = fmt_duration(time.time() - (self.started_at or time.time()))
            self.say(
                f"Match Stats: Total cards played: {self.cards_played} | "
                f"Cards left in deck: {len(self.draw_pile)} | Time: {elapsed}"
            )

    # ---- save / restore ----------------------------------------------------
    def to_dict(self):
        return {
            "channel": self.channel,
            "bot_name": self.bot_name,
            "state": self.state,
            "players": self.players,
            "hands": {k: [list(c) for c in v] for k, v in self.hands.items()},
            "plays": self.plays,
            "strikes": self.strikes,
            "draw_pile": [list(c) for c in self.draw_pile],
            "discard_pile": [list(c) for c in self.discard_pile],
            "turn_idx": self.turn_idx,
            "direction": self.direction,
            "color": self.color,
            "value": self.value,
            "has_drawn": self.has_drawn,
            "cards_played": self.cards_played,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, d):
        game = cls(d["channel"], d.get("bot_name", BOT_PLAYER))
        game.state = d["state"]
        game.players = list(d["players"])
        game.hands = {k: [tuple(c) for c in v] for k, v in d["hands"].items()}
        game.plays = dict(d.get("plays", {}))
        game.strikes = dict(d.get("strikes", {}))
        game.draw_pile = [tuple(c) for c in d["draw_pile"]]
        game.discard_pile = [tuple(c) for c in d["discard_pile"]]
        game.turn_idx = d["turn_idx"]
        game.direction = d["direction"]
        game.color = d["color"]
        game.value = d["value"]
        game.has_drawn = d["has_drawn"]
        game.cards_played = d["cards_played"]
        game.started_at = d["started_at"]
        return game


# ============================================================================
# IRC LAYER
# ============================================================================
def _split_message(text, limit=400):
    text = text.replace("\r", " ").replace("\n", " ")
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _valid_channel(name):
    return bool(re.match(r"^[#&][^\s,\x07]{1,49}$", name or ""))


class DrUnoBot(irc.bot.SingleServerIRCBot):
    def __init__(self, store, channels, nickname, realname, password, server, port=6697):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        ssl_factory = irc.connection.Factory(
            wrapper=lambda sock: context.wrap_socket(sock)
        )
        ident = os.environ.get("IRC_IDENT", nickname)

        super().__init__(
            [(server, port, password or None)], nickname, realname,
            connect_factory=ssl_factory, username=ident,
        )

        self.store = store
        self.password = password or ""
        self.home_channels = [c for c in channels if _valid_channel(c)]
        self.secret = os.environ.get("UNO_SECRET", "xlr8").lower()
        self.bot_player = BOT_PLAYER

        self.games = {}                   # lowercase channel -> UnoGame
        self.joined = set()               # lowercase channels we are currently in
        self._last_join_try = {}
        self._last_disconnect = 0.0
        self._last_reconnect_try = 0.0

        self._out_q = deque(maxlen=600)   # (timestamp, kind, target, text)
        self._tokens = float(SEND_BURST)
        self._last_tick = time.time()

        self._restore_games()

        sched = self.reactor.scheduler
        sched.execute_every(0.3, self._safe_cb(self._drain))
        sched.execute_every(30, self._safe_cb(self._watchdog))

    # ---- safety wrappers ---------------------------------------------------
    @staticmethod
    def _safe_cb(fn, *args):
        def run():
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()
        return run

    def _later(self, delay, fn, *args):
        self.reactor.scheduler.execute_after(delay, self._safe_cb(fn, *args))

    # ---- flood-safe sending ------------------------------------------------
    def say(self, target, text):
        for chunk in _split_message(decorate(text), 380):
            self._out_q.append((time.time(), "PRIVMSG", target, chunk))

    def notice(self, target, text):
        for chunk in _split_message(decorate(text), 380):
            self._out_q.append((time.time(), "NOTICE", target, chunk))

    def _drain(self):
        now = time.time()
        self._tokens = min(float(SEND_BURST), self._tokens + (now - self._last_tick) / SEND_INTERVAL)
        self._last_tick = now
        if not self.connection.is_connected():
            while self._out_q and now - self._out_q[0][0] > 90:
                self._out_q.popleft()
            return
        while self._out_q and self._tokens >= 1:
            stamp, kind, target, text = self._out_q.popleft()
            if now - stamp > 180:
                continue
            try:
                if kind == "NOTICE":
                    self.connection.notice(target, text)
                else:
                    self.connection.privmsg(target, text)
            except Exception as ex:
                print(f"[!] send failed ({ex}); will retry")
                self._out_q.appendleft((stamp, kind, target, text))
                break
            self._tokens -= 1

    # ---- connection events -------------------------------------------------
    def on_welcome(self, c, e):
        print("[+] Connected. Identifying and joining channels...")
        self._last_disconnect = 0.0
        self.joined.clear()
        if self.password:
            c.privmsg("NickServ", f"IDENTIFY {self.password}")
        self._later(2, self._join_all)

    def on_disconnect(self, c, e):
        # The irc library reconnects by itself (exponential back-off). The watchdog
        # below is a second safety net in case that ever stalls.
        print("[!] Disconnected from server.")
        self._last_disconnect = time.time()
        self.joined.clear()

    def _desired_channels(self):
        seen, wanted = set(), []
        for ch in self.home_channels + self.store.get_channels():
            if ch.lower() not in seen:
                seen.add(ch.lower())
                wanted.append(ch)
        return wanted

    def _join_all(self):
        if not self.connection.is_connected():
            return
        for ch in self._desired_channels():
            self._try_join(ch)

    def _try_join(self, ch):
        now = time.time()
        if now - self._last_join_try.get(ch.lower(), 0) < 20:
            return
        self._last_join_try[ch.lower()] = now
        self.connection.join(ch)

    def _watchdog(self):
        now = time.time()
        if not self.connection.is_connected():
            if (self._last_disconnect and now - self._last_disconnect > 90
                    and now - self._last_reconnect_try > 60):
                self._last_reconnect_try = now
                print("[!] Still offline - forcing a reconnect attempt.")
                try:
                    self._connect()
                except Exception as ex:
                    print(f"[!] Forced reconnect failed: {ex}")
            return
        for ch in self._desired_channels():          # rejoin anything we got kicked out of
            if ch.lower() not in self.joined:
                self._try_join(ch)

    def on_join(self, c, e):
        if e.source.nick.lower() != c.get_nickname().lower():
            return
        self._guard(self._self_joined, e.target)

    def _self_joined(self, channel):
        ch = channel.lower()
        self.joined.add(ch)
        game = self.games.get(ch)
        if game and game.state in (UnoGame.PLAYING, UnoGame.AWAIT_COLOR):
            self.say(channel, f"{BOLD}DrUNO is back!{RESET} The game continues.")
            game.info_top()
            game.announce_turn()
            if game.is_bot(game.current() or ""):
                game.say(f"It is {game.current()}'s turn.")
            self._flush(channel, game.take_out())
            self._arm(game)

    # ---- someone leaves the channel / server --------------------------------
    def on_part(self, c, e):
        if e.source.nick.lower() == c.get_nickname().lower():
            self.joined.discard(e.target.lower())
            return
        self._guard(self._player_gone, e.target, e.source.nick, "left the channel")

    def on_kick(self, c, e):
        kicked = e.arguments[0] if e.arguments else ""
        if kicked.lower() == c.get_nickname().lower():
            self.joined.discard(e.target.lower())
            return
        self._guard(self._player_gone, e.target, kicked, "was kicked")

    def on_quit(self, c, e):
        for game in list(self.games.values()):
            self._guard(self._player_gone, game.channel, e.source.nick, "disconnected")

    def on_nick(self, c, e):
        old, new = e.source.nick, e.target
        for game in list(self.games.values()):
            if game.has_player(old):
                game.rename(old, new)
                if game.state != UnoGame.LOBBY:
                    self.store.save_game(game.channel, game.to_dict())

    def _player_gone(self, channel, nick, reason):
        game = self.games.get(channel.lower())
        if game and game.has_player(nick) and not game.is_bot(nick):
            self._run(game, game.remove, nick, reason)

    # ---- message events ----------------------------------------------------
    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            traceback.print_exc()

    def on_pubmsg(self, c, e):
        self._guard(self._handle_public, e.source.nick, e.target, e.arguments[0])

    def on_privmsg(self, c, e):
        self._guard(self._handle_private, e.source.nick, e.arguments[0])

    # ---- private messages: the secret channel commands -----------------------
    def _handle_private(self, nick, text):
        parts = text.strip().split()
        if len(parts) < 2:
            return
        cmd, chan = parts[0].lower(), parts[1]
        if cmd == f"!call{self.secret}":
            if not _valid_channel(chan):
                return
            self.store.add_channel(chan)
            self._last_join_try.pop(chan.lower(), None)
            self._try_join(chan)
            self.say(nick, f"Joining {chan}")
        elif cmd == f"!leave{self.secret}":
            if not _valid_channel(chan):
                return
            if chan.lower() in [h.lower() for h in self.home_channels]:
                self.say(nick, f"{chan} is a home channel, I won't leave it.")
                return
            self.store.remove_channel(chan)
            self.games.pop(chan.lower(), None)
            self.store.delete_game(chan)
            self.joined.discard(chan.lower())
            self.connection.part(chan)
            self.say(nick, f"Left {chan}")

    # ---- public messages -----------------------------------------------------
    def _handle_public(self, nick, channel, text):
        text = text.strip()
        if not text or not channel.startswith(("#", "&")):
            return
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        ch = channel.lower()
        game = self.games.get(ch)
        is_me = nick.lower() == self.bot_player.lower()
        if is_me:
            return

        # ---- leaderboard ----
        if cmd == "!utop":
            return self._cmd_top(channel)
        if cmd == "!urank":
            return self._cmd_rank(channel, args[0] if args else nick)
        if cmd == "!ustats":
            return self._cmd_stats(channel, args[0] if args else nick)

        # ---- starting / joining ----
        if cmd == "!uno" and len(parts) == 1:
            return self._cmd_uno(nick, channel)
        if cmd in ("!jo", ".jo", "jo") and len(parts) == 1:
            if game and game.state == UnoGame.LOBBY:
                self._run(game, game.join, nick)
            return
        if cmd == "!gamerules":
            return self._cmd_rules(nick)
        if cmd == "!remove" and len(parts) == 1:
            if game and game.has_player(nick):
                self._run(game, game.remove, nick, "left the game")
            return

        if game is None or game.state in (UnoGame.LOBBY, UnoGame.OVER):
            if game and game.state == UnoGame.LOBBY and cmd in ("od", "!od") and len(parts) == 1:
                game.info_order()
                self._flush(channel, game.take_out())
            return

        # ---- in-game commands (optional leading '!') ----
        gcmd = cmd[1:] if cmd.startswith("!") else cmd
        single = len(parts) == 1
        info = {
            "od": game.info_order, "cd": game.info_top, "tu": game.info_turn,
            "ct": game.info_counts, "st": game.info_stats,
        }
        if gcmd in info and single:
            info[gcmd]()
            return self._flush(channel, game.take_out())
        if gcmd == "ca" and single:
            if game.has_player(nick):
                game.note_hand(nick, sort=True)
                self._flush(channel, game.take_out())
            return
        if gcmd == "pl":
            if not args:
                return self.notice(nick, "Usage: pl <card>   e.g. pl r5, pl bs, pl gd, pl w, pl wd")
            return self._run(game, game.play, nick, " ".join(args), True)
        if gcmd in ("co", "choose"):
            if args:
                self._run(game, game.choose_color, nick, args[0])
            return
        if gcmd == "dr" and single:
            return self._run(game, game.draw, nick)
        if gcmd in ("pa", "pass") and single:
            return self._run(game, game.pass_turn, nick)
        if game.has_player(nick) and parse_card(text) is not None:
            self._run(game, game.play, nick, text, False)    # bare card name

    # ---- commands ------------------------------------------------------------
    def _cmd_uno(self, nick, channel):
        ch = channel.lower()
        if ch in self.games:
            return
        game = UnoGame(channel, self.bot_player)
        game.players.append(nick)
        self.games[ch] = game
        self.say(
            channel,
            f"{BOLD}UNO!{RESET} initiated by {nick}. You have {PINK}{LOBBY_SECONDS}s{CC} to join! "
            f"Type '{PINK}jo{CC}' to jump in. (max {MAX_PLAYERS} players)",
        )
        self._later(LOBBY_SECONDS, self._lobby_timeout, ch, game)

    def _lobby_timeout(self, ch, game):
        if self.games.get(ch) is not game or game.state != UnoGame.LOBBY:
            return
        if ch not in self.joined:
            return self._later(5, self._lobby_timeout, ch, game)
        self._run(game, game.finish_lobby)

    def _cmd_rules(self, nick):
        lines = [
            "UNO rules: everyone starts with 7 cards. Match the top card by color or value with 'pl <card>' (or just type the card, e.g. r5, bs, gd = Green Draw Two, w = Wild, wd = Wild Draw Four).",
            "No match? 'dr' draws one card, then play it or 'pa' to pass. After a Wild, choose the color with 'co r/g/b/y'.",
            "Skip skips the next player, Reverse flips direction (acts as Skip with 2 players), Draw Two/Wild Draw Four make the next player draw and lose their turn.",
            "First to empty their hand scores the cards left in everyone else's hands. Other commands: od cd tu ct st ca !remove !utop !urank !ustats",
        ]
        for line in lines:
            self.notice(nick, line)

    def _cmd_top(self, channel):
        rows = self.store.top(10)
        if not rows:
            return self.say(channel, "No UNO games have been played yet.")
        line = " | ".join(f"{i}. {r['display']} {PINK}{r['points']}{CC}" for i, r in enumerate(rows, 1))
        self.say(channel, f"{BOLD}UNO Top 10{RESET} (points): {line}")

    def _cmd_rank(self, channel, target):
        target = target[:32]
        if target.lower() == self.bot_player.lower():
            return self.say(channel, f"{target} is not ranked.")
        info = self.store.rank_of(target)
        if info is None:
            return self.say(channel, f"{target} has no UNO stats yet.")
        self.say(
            channel,
            f"{BOLD}{info['display']}{RESET} is ranked #{info['rank']} of {info['total']} "
            f"with {PINK}{info['points']}{CC} points ({info['wins']} wins in {info['games']} games)",
        )

    def _cmd_stats(self, channel, target):
        target = target[:32]
        s = self.store.stats(target)
        if s is None:
            return self.say(channel, f"{target} has no UNO stats yet.")
        fastest = fmt_duration(s["fastest_win"]) if s["fastest_win"] is not None else "-"
        self.say(
            channel,
            f"{BOLD}{s['display']}{RESET}: Games {s['games']} | Wins {s['wins']} ({s['win_rate']}%) | "
            f"Points {PINK}{s['points']}{CC} | Streak {s['streak']} (best {s['best_streak']}) | "
            f"Fastest win {fastest} | Cards played {s['cards_played']}",
        )

    # ---- running game actions ----------------------------------------------
    def _flush(self, channel, out):
        for text in out.msgs:
            self.say(channel, text)
        for nick, text in out.notices:
            if nick.lower() != self.bot_player.lower():
                self.notice(nick, text)

    def _run(self, game, fn, *args):
        before = game.version
        fn(*args)
        out = game.take_out()
        self._flush(game.channel, out)
        if out.result is not None:
            return self._finish(game, out.result)
        if game.version != before:
            if game.state != UnoGame.LOBBY:
                self.store.save_game(game.channel, game.to_dict())
            self._arm(game)

    def _finish(self, game, result):
        ch = game.channel.lower()
        if self.games.get(ch) is game:
            del self.games[ch]
        self.store.delete_game(game.channel)
        if result.get("cancelled"):
            return
        totals = self.store.record_game(
            result["participants"], result["winner"], result["points"],
            result["duration"], result["plays"],
        )
        total = totals.get(result["winner"].lower())
        if total is not None:
            self.say(game.channel, f"{BOLD}{result['winner']}{RESET} total: {PINK}{total}{CC} Points")

    # ---- timers: computer moves and turn timeouts -------------------------------
    def _arm(self, game):
        game.serial += 1
        if game.state not in (UnoGame.PLAYING, UnoGame.AWAIT_COLOR):
            return
        cur = game.current()
        if cur is None:
            return
        ch = game.channel.lower()
        if game.is_bot(cur):
            self._later(BOT_THINK_SECONDS, self._bot_move, ch, game.serial)
        else:
            self._later(TURN_TIMEOUT, self._turn_timeout, ch, game.serial)

    def _bot_move(self, ch, serial):
        game = self.games.get(ch)
        if game is None or game.serial != serial:
            return
        if ch not in self.joined:
            return self._later(5, self._bot_move, ch, serial)
        self._run(game, game.bot_turn)

    def _turn_timeout(self, ch, serial):
        game = self.games.get(ch)
        if game is None or game.serial != serial:
            return
        if ch not in self.joined:                   # we're offline: don't punish the player
            return self._later(10, self._turn_timeout, ch, serial)
        self._run(game, game.timeout)

    # ---- restore after a restart ------------------------------------------------
    def _restore_games(self):
        now = time.time()
        for ch, data, updated in self.store.load_games():
            if now - updated > RESUME_MAX_AGE:
                self.store.delete_game(ch)
                continue
            try:
                game = UnoGame.from_dict(data)
            except Exception:
                self.store.delete_game(ch)
                continue
            if game.state in (UnoGame.PLAYING, UnoGame.AWAIT_COLOR):
                self.games[ch] = game
                print(f"[+] Restored a saved game in {game.channel}")
            else:
                self.store.delete_game(ch)
