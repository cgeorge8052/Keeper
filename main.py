"""
Discord Counter Bot
--------------------
Sends a message with configurable buttons (default: 3, but you can define
any number in buttons_config.json). Each button:
  - Tracks its own count PER USER (each person has their own tally per
    button, not a shared global total)
  - Can be configured to reset another button's count for that same user
    when clicked (e.g. Water resets Coffee back to 0)
  - Can be configured as a pure timer with no count at all ("track_count":
    false) — clicks on it only measure time, the number of clicks is
    never shown or used for anything
  - Can be configured as an On/Off toggle switch ("toggle": true) — the
    button's own label flips between "On"/"Off" (or custom text), and only
    the click that turns it ON fires the usual broadcast/message/reset
    behavior; turning it back OFF just updates the button silently
  - Can be configured as a plain LINK button ("url": set) — opens a URL in
    the browser and never touches the bot at all; rendered in its own
    "Link" section below the main button row
  - Tracks time since THAT user's own previous click on THAT button
    (a per-user, per-button timer — not a global "any click" timer)
  - Broadcasts publicly: the pinned counter message updates, AND a new
    visible chat message is posted using that button's own template

There is no global leaderboard of who has the highest count — only the
clicking user's own current count (for that button) is ever shown, at the
moment they click.

The pinned counter message itself uses Discord's newer "Components V2"
layout system (Container / TextDisplay / Separator / MediaGallery /
ActionRow, via discord.ui.LayoutView) instead of a classic Embed. This is
what lets the title, banner image, activity text, and button row all be
freely interleaved in one message, in whatever order you want — a classic
Embed always renders its image at the very bottom, below every field, with
no way to place text after it.

Run with:  python bot.py
Requires:  DISCORD_BOT_TOKEN environment variable (see .env.example)
Configure: edit buttons_config.json to change labels/emojis/colors/messages/
           reset behavior/timer-only behavior/toggle behavior/link buttons
"""

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # AI replies feature disabled if package isn't installed
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.5")
XAI_BASE_URL = os.getenv("XAI_BASE_URL")
AI_REPLY_SYSTEM_PROMPT = os.getenv("AI_REPLY_SYSTEM_PROMPT")
AI_REPLY_COOLDOWN_SECONDS = 20 # per-user cooldown to control cost/spam)

# Optional: track one specific person's activity. If they've posted
# anywhere the bot can see within the tracking window, the bot skips
# generating an AI reply and instead tells the asker that person is
# probably around to talk to directly.
_raw_tracked_user_id = os.getenv("TRACKED_USER_ID")
try:
    TRACKED_USER_ID = int(_raw_tracked_user_id) if _raw_tracked_user_id else None
except ValueError:
    print(f"Invalid TRACKED_USER_ID in .env: {_raw_tracked_user_id!r} — ignoring it.")
    TRACKED_USER_ID = None

TRACKED_USER_ONLINE_WINDOW_SECONDS = int(os.getenv("TRACKED_USER_ONLINE_WINDOW_MINUTES", "120")) * 60
TRACKED_USER_ONLINE_MESSAGE = os.getenv(
    "TRACKED_USER_ONLINE_MESSAGE",
    "👋 {user} was active pretty recently — you might get a faster answer just asking them directly!",
)

_tracked_user_last_seen: Optional[datetime] = None  # UTC datetime, updated on their every message

# Optional: restrict /setcount to specific role(s) by NAME instead of (or
# in addition to) Discord's Administrator permission. Using names instead
# of IDs means the same .env value works across multiple servers, as long
# as the role is named the same in each — role IDs are unique per server,
# but a role name like "Mod" can exist identically in several. Supports
# multiple roles separated by ";", e.g. "Mod;Admin" — a member with ANY of
# these role names is allowed. Matching is case-insensitive. If
# unset/empty, falls back to requiring Administrator.
_raw_setcount_role_names = os.getenv("SET_COUNT_ROLE_NAME", "")
SET_COUNT_ROLE_NAMES: set[str] = {
    part.strip().lower() for part in _raw_setcount_role_names.split(";") if part.strip()
}

# Optional: a specific channel where the counter is always expected to
# live. When set, lookup commands (/counts, /setcount) search THIS channel
# for "the counter" whenever `counter` isn't given explicitly — regardless
# of which channel the command itself was run in. Leave unset to keep the
# default behavior (search whichever channel the command was run in).
_raw_counter_channel_id = os.getenv("COUNTER_CHANNEL_ID")
try:
    COUNTER_CHANNEL_ID = int(_raw_counter_channel_id) if _raw_counter_channel_id else None
except ValueError:
    print(f"Invalid COUNTER_CHANNEL_ID in .env: {_raw_counter_channel_id!r} — ignoring it.")
    COUNTER_CHANNEL_ID = None

xai_client = (
    AsyncOpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL)
    if (AsyncOpenAI is not None and XAI_API_KEY)
    else None
)
_last_ai_reply_time: dict[int, float] = {}  # user_id -> monotonic time

intents = discord.Intents.default()
# Only request this privileged intent if the AI reply feature is actually
# configured — no need to force everyone to enable it in the Developer
# Portal if they're not using XAI_API_KEY.
intents.message_content = bool(XAI_API_KEY)
bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG_PATH = Path(__file__).parent / "buttons_config.json"

BANNER_FILENAME = "banner.png"
BANNER_PATH = Path(__file__).parent / BANNER_FILENAME

# Decorative title/divider text, styled after the reference layout
# (". Section Title ." with a "°.✧──────✧.°" rule underneath).
COUNTER_TITLE = ". Devotion Hub ."
DIVIDER = "°.✧────────────✧.°"

STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

DEFAULT_MESSAGE_TEMPLATE = "🔘 **{user}** clicked **{label}** (their count: {count}) — {gap_text}"
DEFAULT_DELETE_AFTER_SECONDS = 600  # 10 minutes


def load_button_config() -> dict:
    """
    Load buttons_config.json. Keys are button IDs (as strings, e.g. "1"),
    values configure that button:
      {
        "label": "Coffee",              # button text
        "emoji": "☕",                   # optional emoji shown on the button
        "style": "primary",             # primary | secondary | success | danger
        "message": "☕ {user} ...",      # chat message template, see placeholders below
        "resets": ["1"],                # button IDs whose per-user count gets
                                         # reset to 0 (for that same user) when
                                         # THIS button is clicked. Default: []
        "track_count": true,            # false = pure timer button; clicks
                                         # are timed but never counted or
                                         # shown as a number. Default: true
        "track_lifetime": true,         # false = don't keep a separate
                                         # lifetime total for this button —
                                         # only the current (soft) count is
                                         # tracked/shown. Useful for buttons
                                         # nothing ever resets, where a
                                         # lifetime total would just always
                                         # match the current count anyway.
                                         # Default: true. No effect if
                                         # track_count is false.
        "broadcast_channel_id": null,   # channel ID (string or number) to
                                         # post this button's chat message
                                         # to. Default: null, meaning "post
                                         # in whichever channel the counter
                                         # message itself is in"
        "delete_after_seconds": 600,    # auto-delete this button's chat
                                         # message after this many seconds.
                                         # Default: 600 (10 minutes). Set to
                                         # null/0 to keep messages forever.
                                         # Only affects the chat broadcast —
                                         # the pinned counter message is
                                         # never auto-deleted.
        "toggle": false,                # true = this is a per-user On/Off
                                         # toggle switch, not a counter. The
                                         # shared button on the pinned
                                         # message always looks the same —
                                         # its label/color never changes,
                                         # since state is tracked PER USER,
                                         # not globally. Each user's FIRST
                                         # click (turning it On for them)
                                         # fires the normal chat broadcast/
                                         # random phrase/reset behavior;
                                         # their SECOND click (turning it
                                         # back Off) is completely silent —
                                         # no message, no pinned-message
                                         # update at all. Default: false.
        "toggle_on_label": "On",        # inserted via the {toggle_state}
                                         # message placeholder when a user
                                         # turns this button on (see below)
        "toggle_off_label": "Off",      # currently unused for display (no
                                         # broadcast happens on the Off
                                         # click), kept for symmetry/future use
        "url": null                     # if set, this becomes a LINK button
                                         # instead of an interactive one: it
                                         # just opens this URL in the user's
                                         # browser and never triggers any
                                         # bot logic at all (no clicks are
                                         # tracked, none of the other fields
                                         # above apply). Rendered in its own
                                         # "Link" section, below the main
                                         # button row. Default: null.
      }

    Message template placeholders:
      {user}           - display name of the clicker
      {label}          - this button's configured label
      {toggle_state}   - only meaningful for toggle buttons: this button's
                         toggle_on_label, since the message only ever fires
                         on the On transition (empty string otherwise)
      {button}         - the button's ID (e.g. "1")
      {count}          - the clicking user's own current (soft) count for
                         this button ("—" if "track_count": false)
      {lifetime_count} - the clicking user's all-time count for this
                         button, which is NEVER reduced by a reset ("—" if
                         "track_count": false)
      {gap_text}       - human-readable time since this user's last click
                         on this button (or "first click on {label}" if none yet)

    A note about any resets triggered by this click is appended
    automatically to the chat message — you don't need a placeholder for it.

    "random_phrases": ["phrase one", "phrase two", ...] — optional list of
    strings. If present, one is picked at random on every click and sent
    as a SEPARATE follow-up chat message, right after the main broadcast
    message (not inline in the "message" template — there's no
    {random_phrase} placeholder for it).

    Falls back to a small built-in default config if the file is missing
    or invalid, so the bot still runs out of the box.
    """
    default_config = {
        "1": {"label": "1", "style": "primary", "message": DEFAULT_MESSAGE_TEMPLATE, "broadcast_channel_id": None,
              "delete_after_seconds": DEFAULT_DELETE_AFTER_SECONDS},
        "2": {"label": "2", "style": "success", "message": DEFAULT_MESSAGE_TEMPLATE, "resets": ["1"],
              "broadcast_channel_id": None, "delete_after_seconds": DEFAULT_DELETE_AFTER_SECONDS,
              "track_lifetime": False},
        "3": {"label": "3", "style": "danger", "message": "⏱️ **{user}** clicked **{label}** — {gap_text}",
              "track_count": False, "broadcast_channel_id": None, "delete_after_seconds": DEFAULT_DELETE_AFTER_SECONDS},
    }
    if not CONFIG_PATH.exists():
        print(f"No {CONFIG_PATH.name} found, using default button config.")
        return default_config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for button_id, cfg in data.items():
            cfg.setdefault("label", button_id)
            cfg.setdefault("style", "primary")
            cfg.setdefault("message", DEFAULT_MESSAGE_TEMPLATE)
            cfg.setdefault("resets", [])
            cfg.setdefault("track_count", True)
            cfg.setdefault("track_lifetime", True)
            cfg.setdefault("broadcast_channel_id", None)
            cfg.setdefault("delete_after_seconds", DEFAULT_DELETE_AFTER_SECONDS)
            cfg.setdefault("random_phrases", None)
            cfg.setdefault("toggle", False)
            cfg.setdefault("toggle_on_label", "On")
            cfg.setdefault("toggle_off_label", "Off")
            cfg.setdefault("url", None)
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to load {CONFIG_PATH.name} ({e}); using default button config.")
        return default_config


BUTTON_CONFIG = load_button_config()
BUTTON_IDS = list(BUTTON_CONFIG.keys())

# Link buttons (have a "url") never fire an interaction at all — Discord
# opens the URL client-side. They get no callback, no state tracking, no
# /counts entry, and are rendered in their own row/section. Every other
# button is "interactive" and goes through the normal click/state pipeline.
LINK_BUTTON_IDS = [b for b in BUTTON_IDS if BUTTON_CONFIG[b].get("url")]
INTERACTIVE_BUTTON_IDS = [b for b in BUTTON_IDS if b not in LINK_BUTTON_IDS]

# --- Persistent per-user data (survives bot restarts) -----------------------
#
# This is bot-wide, not tied to any single counter message: every user's
# soft/lifetime counts, toggle states, and last-click times live in one
# shared JSON file, keyed by user ID. A brand new CounterState (e.g. after
# running /counter again post-restart) seeds itself from this store, so
# people don't lose their history just because the bot restarted — even
# though, per the usual Discord limitation, buttons on the OLD message stop
# working after a restart unless the bot re-registers persistent views.
USER_DATA_PATH = Path(__file__).parent / "user_data.json"


def _empty_user_record() -> dict:
    return {"soft_counts": {}, "lifetime_counts": {}, "toggle_states": {}, "last_click": {}}


def load_user_data() -> dict:
    if not USER_DATA_PATH.exists():
        return {}
    try:
        with open(USER_DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Couldn't load {USER_DATA_PATH.name} ({e}); starting with empty user data.")
        return {}


def save_user_data() -> None:
    try:
        with open(USER_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(_user_data_store, f, indent=2)
    except OSError as e:
        print(f"Couldn't save {USER_DATA_PATH.name}: {e}")


_user_data_store: dict = load_user_data()


def get_persisted_user(user_id: int) -> dict:
    return _user_data_store.setdefault(str(user_id), _empty_user_record())


def sync_user_to_store(state: "CounterState", user_id: int) -> None:
    """Merge this user's current in-memory values from `state` into the
    shared persisted store and save to disk. Called after any mutation.

    Uses dict.update() (merge), not assignment (overwrite) — a fresh
    CounterState right after a restart may only have this user's data for
    ONE button in memory (whichever was just clicked/set), and overwriting
    the whole persisted record would silently wipe out their history for
    every other button.
    """
    entry = get_persisted_user(user_id)
    entry["soft_counts"].update(state._user_soft_counts.get(user_id, {}))
    entry["lifetime_counts"].update(state._user_lifetime_counts.get(user_id, {}))
    entry["toggle_states"].update(state._user_toggle_states.get(user_id, {}))
    entry["last_click"].update(
        {
            bid: state._last_click_wall[(user_id, bid)].isoformat()
            for bid in INTERACTIVE_BUTTON_IDS
            if (user_id, bid) in state._last_click_wall
        }
    )
    save_user_data()


async def resolve_broadcast_channel(client: discord.Client, channel_id) -> Optional[discord.abc.Messageable]:
    """Resolve a configured broadcast_channel_id (str, int, or None) into an
    actual channel object. Returns None if unset, invalid, or not found."""
    if not channel_id:
        return None
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        print(f"Invalid broadcast_channel_id in config: {channel_id!r}")
        return None

    channel = client.get_channel(cid)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(cid)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"Couldn't find/access configured broadcast channel {cid}: {e}")
        return None


def format_seconds(seconds: float) -> str:
    """Turn a float number of seconds into a friendly string like '2m 3.4s'."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {secs:.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {secs:.1f}s"


class CounterState:
    """Holds the live state for a single counter message with configurable,
    per-user buttons."""

    def __init__(self):
        # user_id -> {button_id: count}. Each user has their own independent
        # tally per button. Buttons with "track_count": false never get
        # incremented here.
        #
        # "Soft" counts are the everyday counter — they get reset to 0 for
        # buttons listed in another button's "resets" config.
        # "Lifetime" counts never reset; they just keep accumulating for as
        # long as this CounterState exists, regardless of any resets.
        self._user_soft_counts: dict[int, dict[str, int]] = {}
        self._user_lifetime_counts: dict[int, dict[str, int]] = {}

        # user_id -> {button_id: bool}. Toggle state is tracked PER USER
        # (like counts), not shared globally — the shared button on the
        # pinned message never visually reflects any single person's
        # state; only the chat announcement does, and only on the click
        # that turns it on for that specific user.
        self._user_toggle_states: dict[int, dict[str, bool]] = {}

        # (user_id, button_id) -> last click monotonic timestamp, used only
        # to compute the per-user/per-button gap. Every button gets one,
        # including timer-only buttons.
        self._last_click_time: dict[tuple[int, str], float] = {}

        # (user_id, button_id) -> last click wall-clock datetime (UTC), used
        # by the /counts command so it can show an actual point in time,
        # not just a duration computed during this bot session.
        self._last_click_wall: dict[tuple[int, str], datetime] = {}

        # Most recent click event, shown in the pinned embed.
        self.last_event: dict | None = None

    def _soft_counts_for(self, user_id: int) -> dict[str, int]:
        if user_id not in self._user_soft_counts:
            persisted = get_persisted_user(user_id)["soft_counts"]
            self._user_soft_counts[user_id] = {b: persisted.get(b, 0) for b in INTERACTIVE_BUTTON_IDS}
        return self._user_soft_counts[user_id]

    def _lifetime_counts_for(self, user_id: int) -> dict[str, int]:
        if user_id not in self._user_lifetime_counts:
            persisted = get_persisted_user(user_id)["lifetime_counts"]
            self._user_lifetime_counts[user_id] = {b: persisted.get(b, 0) for b in INTERACTIVE_BUTTON_IDS}
        return self._user_lifetime_counts[user_id]

    def get_toggle_state(self, user_id: int, button_id: str) -> bool:
        """This specific user's current On/Off state for a toggle button
        (False/Off if they've never clicked it). Falls back to the
        persisted store if this CounterState hasn't seen this user yet
        (e.g. right after a restart)."""
        if user_id in self._user_toggle_states and button_id in self._user_toggle_states[user_id]:
            return self._user_toggle_states[user_id][button_id]
        return bool(get_persisted_user(user_id)["toggle_states"].get(button_id, False))

    def register_click(self, user: discord.abc.User, button_id: str):
        now = time.monotonic()
        key = (user.id, button_id)

        gap = None
        if key in self._last_click_time:
            gap = now - self._last_click_time[key]
        self._last_click_time[key] = now
        self._last_click_wall[key] = datetime.now(timezone.utc)

        cfg = BUTTON_CONFIG[button_id]
        soft_counts = self._soft_counts_for(user.id)
        lifetime_counts = self._lifetime_counts_for(user.id)

        new_count = None
        new_lifetime_count = None
        new_toggle_state = None
        if cfg.get("toggle", False):
            # Toggle buttons don't count clicks at all — just flip this
            # user's own on/off flag. Broadcasting only on the On
            # transition is decided by the caller (CounterView._handle_click)
            # using this returned state.
            new_toggle_state = not self.get_toggle_state(user.id, button_id)
            self._user_toggle_states.setdefault(user.id, {})[button_id] = new_toggle_state
        elif cfg.get("track_count", True):
            soft_counts[button_id] += 1
            new_count = soft_counts[button_id]
            if cfg.get("track_lifetime", True):
                lifetime_counts[button_id] += 1
                new_lifetime_count = lifetime_counts[button_id]

        # Resets only ever affect the soft count — lifetime totals are
        # never touched by a reset, by design.
        reset_labels = []
        for reset_id in cfg.get("resets", []):
            if reset_id in soft_counts:
                soft_counts[reset_id] = 0
                reset_labels.append(BUTTON_CONFIG[reset_id]["label"])

        random_phrase = None
        phrases = cfg.get("random_phrases")
        if phrases:
            random_phrase = random.choice(phrases)

        self.last_event = {
            "user": user.display_name,
            "button": button_id,
            "gap": gap,
            "count": new_count,
            "lifetime_count": new_lifetime_count,
            "toggle_state": new_toggle_state,
            "reset_labels": reset_labels,
            "random_phrase": random_phrase,
        }
        sync_user_to_store(self, user.id)
        return gap

    def set_user_count(
            self, user_id: int, button_id: str, soft_count: int, lifetime_count: Optional[int] = None
    ) -> None:
        """Directly overwrite a user's count on a button — used by the
        admin-only /setcount command. Bypasses register_click entirely: no
        gap/timestamp tracking, no resets, no broadcast. Persists
        immediately."""
        soft = self._soft_counts_for(user_id)
        soft[button_id] = soft_count
        if lifetime_count is not None:
            lifetime = self._lifetime_counts_for(user_id)
            lifetime[button_id] = lifetime_count
        sync_user_to_store(self, user_id)

    def get_user_counts(self, user_id: int) -> dict[str, Optional[tuple[int, Optional[int]]]]:
        """Return {button_id: (soft_count, lifetime_count)} for a user.
        The whole entry is None for buttons with track_count=false or
        toggle=true (no count is ever tracked for those). The
        lifetime_count slot itself is None for buttons with
        track_lifetime=false (soft count only). Falls back to the
        persisted store if this CounterState hasn't seen this user yet."""
        soft = self._soft_counts_for(user_id)
        lifetime = self._lifetime_counts_for(user_id)
        result = {}
        for b in INTERACTIVE_BUTTON_IDS:
            cfg = BUTTON_CONFIG[b]
            if cfg.get("toggle", False) or not cfg.get("track_count", True):
                result[b] = None
            elif not cfg.get("track_lifetime", True):
                result[b] = (soft.get(b, 0), None)
            else:
                result[b] = (soft.get(b, 0), lifetime.get(b, 0))
        return result

    def get_last_click(self, user_id: int, button_id: str) -> Optional[datetime]:
        """Return the wall-clock UTC datetime of a user's last click on a
        button, or None if they've never clicked it. Falls back to the
        persisted store if this CounterState hasn't seen this user yet
        (e.g. right after a restart) — monotonic-based gap timing can't
        survive a restart, but the wall-clock 'last seen' can."""
        if (user_id, button_id) in self._last_click_wall:
            return self._last_click_wall[(user_id, button_id)]
        persisted = get_persisted_user(user_id)["last_click"].get(button_id)
        if persisted:
            try:
                return datetime.fromisoformat(persisted)
            except ValueError:
                return None
        return None

    def get_all_last_clicks(self, user_id: int) -> dict[str, Optional[datetime]]:
        return {b: self.get_last_click(user_id, b) for b in INTERACTIVE_BUTTON_IDS}

    def build_activity_text(self) -> str:
        """Build the 'Last Activity' section content as plain text (used in
        a TextDisplay component instead of an embed field)."""
        if not self.last_event:
            return f"-  Last Activity  -\n{DIVIDER}\nNo clicks yet."

        ev = self.last_event
        cfg = BUTTON_CONFIG[ev["button"]]
        label = cfg["label"]

        if ev["gap"] is not None:
            gap_text = f"{format_seconds(ev['gap'])} since their last click on {label}"
        else:
            gap_text = f"first click on {label}"

        if cfg.get("toggle", False):
            state_word = "On" if ev["toggle_state"] else "Off"
            line = f"**{ev['user']}** turned **{label}** {state_word}"
        else:
            line = f"**{ev['user']}** clicked **{label}**"
            if ev["count"] is not None:
                if ev["lifetime_count"] is not None:
                    line += f" (current: {ev['count']}, lifetime: {ev['lifetime_count']})"
                else:
                    line += f" (count: {ev['count']})"
        line += f" — {gap_text}"
        if ev["reset_labels"]:
            line += f"\n↺ Reset their {', '.join(ev['reset_labels'])} count to 0 (lifetime unaffected)."
        if ev["random_phrase"]:
            line += f"\n💬 {ev['random_phrase']}"

        return f"-  Last Activity  -\n{DIVIDER}\n{line}"

    @staticmethod
    def build_footer_text() -> str:
        return f"-# Last updated • {discord.utils.format_dt(datetime.now(timezone.utc), style='f')}"


class CounterView(discord.ui.LayoutView):
    """A persistent Components V2 layout: title, optional banner, a
    mutable 'Last Activity' text block, a row of buttons, and a footer —
    all interleaved in one Container, in that exact order (unlike a classic
    Embed, where fields and the image can't be freely interleaved with the
    component buttons below them)."""

    def __init__(self, state: CounterState):
        super().__init__(timeout=None)  # persistent - never expires
        self.state = state

        container = discord.ui.Container(accent_colour=discord.Color(0x1D003A))

        container.add_item(
            discord.ui.TextDisplay(
                f"## {COUNTER_TITLE}\n{DIVIDER}\n"
                "𝙸 𝚠𝚊𝚝𝚌𝚑 𝚠𝚑𝚎𝚗 𝚢𝚘𝚞 𝚜𝚚𝚞𝚒𝚛𝚝....𝚠𝚑𝚎𝚗 𝚢𝚘𝚞 𝚎𝚍𝚐𝚎..."
                "𝚃𝚛𝚞𝚜𝚝 𝚖𝚎 , 𝚝𝚑𝚊𝚝 𝚌𝚊𝚐𝚎 𝚋𝚎𝚝𝚝𝚎𝚛 𝚜𝚝𝚊𝚢 𝚘𝚗."
            )
        )

        if BANNER_PATH.exists():
            container.add_item(
                discord.ui.MediaGallery(discord.MediaGalleryItem(media=f"attachment://{BANNER_FILENAME}"))
            )

        container.add_item(discord.ui.Separator())

        # Keep a reference to this TextDisplay so later clicks can mutate
        # its .content directly instead of rebuilding the whole layout.
        self.activity_display = discord.ui.TextDisplay(state.build_activity_text())
        container.add_item(self.activity_display)

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-  Buttons  -\n{DIVIDER}"))

        action_row = discord.ui.ActionRow()
        for button_id in INTERACTIVE_BUTTON_IDS:
            cfg = BUTTON_CONFIG[button_id]
            # Toggle buttons look and behave like any other button on the
            # shared message — their label/style never changes, since
            # on/off state is tracked per-user and only shown in each
            # user's own chat announcement, not on the one shared button
            # everyone sees.
            item = discord.ui.Button(
                label=cfg["label"],
                emoji=cfg.get("emoji") or None,
                style=STYLE_MAP.get(cfg.get("style", "primary"), discord.ButtonStyle.primary),
                custom_id=f"counter_button_{button_id}",
            )
            item.callback = self._make_callback(button_id)
            action_row.add_item(item)
        container.add_item(action_row)

        # Link buttons get their own section — they never trigger the bot
        # at all, so they're intentionally separate from the interactive
        # row above.
        if LINK_BUTTON_IDS:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(f"-  Link  -\n{DIVIDER}"))
            link_row = discord.ui.ActionRow()
            for button_id in LINK_BUTTON_IDS:
                cfg = BUTTON_CONFIG[button_id]
                link_row.add_item(
                    discord.ui.Button(
                        label=cfg["label"],
                        emoji=cfg.get("emoji") or None,
                        style=discord.ButtonStyle.link,
                        url=cfg["url"],
                    )
                )
            container.add_item(link_row)

        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        self.footer_display = discord.ui.TextDisplay(state.build_footer_text())
        container.add_item(self.footer_display)

        self.add_item(container)

    def refresh(self):
        """Update the mutable text blocks in place after a click, instead
        of rebuilding the whole component tree."""
        self.activity_display.content = self.state.build_activity_text()
        self.footer_display.content = self.state.build_footer_text()

    def _make_callback(self, button_id: str):
        async def callback(interaction: discord.Interaction):
            await self._handle_click(interaction, button_id)

        return callback

    async def _handle_click(self, interaction: discord.Interaction, button_id: str):
        gap = self.state.register_click(interaction.user, button_id)
        ev = self.state.last_event
        cfg = BUTTON_CONFIG[button_id]
        label = cfg["label"]
        is_toggle = cfg.get("toggle", False)

        if is_toggle:
            # Toggle buttons never touch the pinned counter message at all —
            # the shared button's label/style stays exactly as configured,
            # and state is per-user, so there's nothing meaningful to show
            # on a message everyone shares. Just acknowledge the click.
            await interaction.response.defer()
            # Only the click that turns it ON for THIS user broadcasts —
            # the click that turns it back OFF is completely silent.
            if not ev["toggle_state"]:
                return
        else:
            # Update the pinned counter message with the latest activity.
            self.refresh()
            await interaction.response.edit_message(view=self)

        # Also post a visible chat message using this button's own template.
        if gap is not None:
            gap_text = f"{format_seconds(gap)} since their last click on {label}"
        else:
            gap_text = f"first click on {label}"

        message = cfg["message"].format(
            user=ev["user"],
            label=label,
            button=button_id,
            count=ev["count"] if ev["count"] is not None else "—",
            lifetime_count=ev["lifetime_count"] if ev["lifetime_count"] is not None else "—",
            gap_text=gap_text,
            toggle_state=cfg["toggle_on_label"] if is_toggle else "",
        )
        if ev["reset_labels"]:
            message += f"\n↺ Reset their {', '.join(ev['reset_labels'])} count to 0 (lifetime unaffected)."

        target_channel = await resolve_broadcast_channel(interaction.client, cfg.get("broadcast_channel_id"))
        delete_after = cfg.get("delete_after_seconds")

        async def _send(content: str):
            """Send one chat message to wherever this button broadcasts to,
            falling back to the interaction's own channel on failure."""
            if target_channel is not None and target_channel.id != interaction.channel_id:
                try:
                    return await target_channel.send(content)
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"Couldn't post to configured broadcast channel for button {button_id}: {e}")
                    return await interaction.followup.send(
                        content + "\n*(Couldn't reach the configured broadcast channel — posted here instead.)*",
                        wait=True,
                    )
            return await interaction.followup.send(content, wait=True)

        async def _schedule_delete(msg):
            if delete_after and msg is not None:
                try:
                    do_nothing = 1
                except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
                    print(f"Couldn't schedule deletion for a button {button_id} broadcast message: {e}")

        # Post the main broadcast message first.
        sent_message = await _send(message)
        await _schedule_delete(sent_message)

        # If this button has random_phrases configured, follow up with a
        # second message right after, containing the picked phrase.
        if ev["random_phrase"]:
            phrase_message = await _send(f"## {ev['random_phrase']}")
            await _schedule_delete(phrase_message)


# In-memory registry of active counters, keyed by message ID.
active_counters: dict[int, CounterState] = {}

# channel_id -> message_id of the most recently created counter in that
# channel. Lets lookup commands default to "the counter in this channel"
# without the user having to specify a message ID every time. This is
# just a fast-path cache — find_latest_counter_message_id() below is the
# real source of truth when this cache is empty or stale.
channel_latest_counter: dict[int, int] = {}

# How many recent messages to scan when hunting for a counter in a
# channel. The counter message is usually pinned near the top of activity
# in its own channel, so this rarely needs to look far.
COUNTER_SCAN_HISTORY_LIMIT = 50


async def find_latest_counter_message_id(
        channel: discord.abc.Messageable, limit: int = COUNTER_SCAN_HISTORY_LIMIT
) -> Optional[int]:
    """Scan a channel's recent history for the latest message that's a
    live, tracked counter — i.e. sent by this bot AND still present in
    active_counters (so it actually has a usable CounterState, not just
    any old bot message). Used as a fallback lookup for commands that
    don't specify `counter` explicitly, for when channel_latest_counter
    doesn't have (or no longer has) a good answer — e.g. right after a
    partial state loss, or a counter posted before the current mapping
    existed."""
    try:
        async for message in channel.history(limit=limit):
            if message.author.id == bot.user.id and message.id in active_counters:
                return message.id
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Couldn't scan channel history for a counter message: {e}")
    return None


def has_counter_admin_permission(interaction: discord.Interaction) -> bool:
    """Shared permission check for /counter, /counts, and /set_count: if
    SET_COUNT_ROLE_NAMES is configured, require ANY of those role names;
    otherwise fall back to requiring the Administrator permission. Doing
    this in one place instead of copy-pasting the same check into every
    command avoids the three copies quietly drifting out of sync."""
    caller_roles = getattr(interaction.user, "roles", [])
    if SET_COUNT_ROLE_NAMES:
        return any(role.name.lower() in SET_COUNT_ROLE_NAMES for role in caller_roles)
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def resolve_state(
        interaction: discord.Interaction, message_id: Optional[str]
) -> Optional[CounterState]:
    """Find the CounterState to query for a lookup command.

    If message_id is given (a raw ID or a pasted message link), use that
    counter specifically. Otherwise default to the most recently created
    counter in the "expected" channel — COUNTER_CHANNEL_ID if configured,
    otherwise whichever channel the command was run in — first checking
    the fast in-memory cache, then falling back to actually scanning that
    channel's message history if the cache doesn't have an answer.
    """
    if message_id:
        # Accept either a raw message ID or a full message link
        # (https://discord.com/channels/GUILD/CHANNEL/MESSAGE) — take only
        # the final path segment so we never mix digits across IDs.
        segment = message_id.strip().split("/")[-1]
        digits = "".join(ch for ch in segment if ch.isdigit())
        try:
            mid = int(digits) if digits else None
        except ValueError:
            mid = None
        return active_counters.get(mid) if mid else None

    if COUNTER_CHANNEL_ID is not None:
        search_channel = await resolve_broadcast_channel(interaction.client, COUNTER_CHANNEL_ID)
        search_channel_id = COUNTER_CHANNEL_ID
    else:
        search_channel = interaction.channel
        search_channel_id = interaction.channel_id

    if search_channel is None:
        return None  # COUNTER_CHANNEL_ID configured but couldn't be resolved

    mid = channel_latest_counter.get(search_channel_id)
    if mid is not None and mid in active_counters:
        return active_counters[mid]

    # Cache miss (or stale entry pointing at a counter that no longer
    # exists) — actually look for one in the channel itself.
    mid = await find_latest_counter_message_id(search_channel)
    if mid is not None:
        channel_latest_counter[search_channel_id] = mid  # warm the cache for next time
        return active_counters[mid]
    return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if xai_client is None:
        print(
            "AI reply feature is OFF (set XAI_API_KEY in .env to enable "
            "automatic replies when someone replies to this bot's messages)."
        )
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


@bot.event
async def on_message(message: discord.Message):
    global _tracked_user_last_seen

    # Required so slash commands / components keep working normally when
    # on_message is overridden like this.
    await bot.process_commands(message)

    if message.author.bot or message.author.id == bot.user.id:
        return
    if message.guild is None:
        return  # ignore DMs — only respond in server channels

    # Track the specific user's activity on EVERY message they send,
    # regardless of whether it's a reply/mention or the AI feature is even
    # on — this just needs their ID, not message content.
    if TRACKED_USER_ID is not None and message.author.id == TRACKED_USER_ID:
        _tracked_user_last_seen = datetime.now(timezone.utc)

    if xai_client is None:
        return

    # Two ways to trigger an AI reply: replying to one of the bot's own
    # messages, or @mentioning the bot directly. A single message could
    # technically satisfy both (e.g. a reply that also pings), so this
    # only ever builds ONE prompt/response — reply-context wins if both
    # are true, since it has richer context to work with.
    replied_to = None
    if message.reference:
        replied_to = message.reference.resolved
        if replied_to is None:
            try:
                replied_to = await message.channel.fetch_message(message.reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                replied_to = None
        if not isinstance(replied_to, discord.Message) or replied_to.author.id != bot.user.id:
            replied_to = None  # reply exists, but not aimed at this bot

    is_mention = bot.user in message.mentions

    if replied_to is None and not is_mention:
        return  # neither a reply to us nor a mention of us — ignore

    # Simple per-user cooldown to keep this from being spammed / running up
    # API costs — set it up-front so rapid-fire messages don't all queue up.
    now = time.monotonic()
    last = _last_ai_reply_time.get(message.author.id, 0.0)
    if now - last < AI_REPLY_COOLDOWN_SECONDS:
        return
    _last_ai_reply_time[message.author.id] = now

    # If the tracked user has posted recently, skip the AI response
    # entirely and point the asker at them instead — but only when someone
    # ELSE is asking (doesn't make sense to tell the tracked user "go talk
    # to yourself").
    if (
            TRACKED_USER_ID is not None
            and message.author.id != TRACKED_USER_ID
            and _tracked_user_last_seen is not None
            and (datetime.now(timezone.utc) - _tracked_user_last_seen).total_seconds()
            < TRACKED_USER_ONLINE_WINDOW_SECONDS
    ):
        tracked_user = bot.get_user(TRACKED_USER_ID)
        display_name = tracked_user.display_name if tracked_user else "they"
        try:
            await message.reply(
                TRACKED_USER_ONLINE_MESSAGE.format(user=display_name), mention_author=False
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"Couldn't send tracked-user referral message: {e}")
        return

    # clean_content resolves mention markup (e.g. "<@123>") into readable
    # "@Name" text, which reads better in the prompt than raw IDs.
    user_text = message.clean_content

    if replied_to is not None:
        prompt = (
            f'The bot previously said: "{replied_to.content}"\n\n'
            f'{message.author.display_name} replied: "{user_text}"\n\n'
            "Write a short, friendly reply."
        )
    else:
        prompt = (
            f'{message.author.display_name} mentioned the bot and said: "{user_text}"\n\n'
            "Write a short, friendly reply."
        )

    async with message.channel.typing():
        try:
            response = await xai_client.chat.completions.create(
                model=XAI_MODEL,
                max_tokens=300,
                messages=[
                    {"role": "system", "content": AI_REPLY_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            reply_text = (response.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"AI reply generation failed: {e}")
            return

    if not reply_text:
        return
    if len(reply_text) > 1900:  # stay safely under Discord's 2000-char limit
        reply_text = reply_text[:1900] + "…"

    try:
        await message.reply(reply_text, mention_author=False)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Couldn't send AI reply: {e}")


@bot.tree.command(name="counter", description="Post a new click counter with configured buttons")
async def counter_command(interaction: discord.Interaction):
    if not has_counter_admin_permission(interaction):
        await interaction.response.send_message(
            "You don't have permission to use this command."
        )
        return

    state = CounterState()
    view = CounterView(state)

    if BANNER_PATH.exists():
        banner_file = discord.File(BANNER_PATH, filename=BANNER_FILENAME)
        await interaction.response.send_message(view=view, file=banner_file)
    else:
        await interaction.response.send_message(view=view)

    sent_message = await interaction.original_response()
    active_counters[sent_message.id] = state
    channel_latest_counter[interaction.channel_id] = sent_message.id


@bot.tree.command(name="counts", description="See a user's counts and last-click times on a counter")
@app_commands.describe(
    user="Whose info to look up (defaults to you)",
    counter="Optional: paste a specific counter message link/ID (defaults to the latest one in this channel)",
)
async def counts_command(
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        counter: Optional[str] = None,
):
    if not has_counter_admin_permission(interaction):
        await interaction.response.send_message(
            "You don't have permission to use this command."
        )
        return

    target = user or interaction.user
    state = await resolve_state(interaction, counter)
    if state is None:
        await interaction.response.send_message(
            "I couldn't find a counter here. Run `/counter` first, or pass a specific "
            "counter message link/ID with the `counter` option."
        )
        return

    counts = state.get_user_counts(target.id)
    lines = []
    for button_id in INTERACTIVE_BUTTON_IDS:
        cfg = BUTTON_CONFIG[button_id]
        emoji = f"{cfg['emoji']} " if cfg.get("emoji") else ""

        last = state.get_last_click(target.id, button_id)
        if last is None:
            last_text = "never clicked"
        else:
            last_text = f"{discord.utils.format_dt(last, style='R')} ({discord.utils.format_dt(last, style='f')})"

        if cfg.get("toggle", False):
            # Toggle state is per-user now — this shows the target user's
            # own On/Off status, and when they last flipped it either way.
            state_word = "On" if state.get_toggle_state(target.id, button_id) else "Off"
            lines.append(f"{emoji}**{cfg['label']}:** {state_word} — last toggled {last_text}")
            continue

        value = counts[button_id]
        if value is not None:
            soft, lifetime = value
            if lifetime is not None:
                lines.append(
                    f"{emoji}**{cfg['label']}:** {soft} (lifetime: {lifetime}) — last click: {last_text}"
                )
            else:
                # track_lifetime is disabled for this button — soft count only.
                lines.append(f"{emoji}**{cfg['label']}:** {soft} — last click: {last_text}")
        else:
            # Timer-only button: no count at all, just the last-click time.
            lines.append(f"{emoji}**{cfg['label']}:** last click: {last_text}")

    embed = discord.Embed(
        title=f"📊 Stats for {target.display_name}",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="unlock_bot",
    description="If you're the tracked user, immediately let the bot's AI replies work normally again",
)
async def unlock_bot_command(interaction: discord.Interaction):
    global _tracked_user_last_seen

    if TRACKED_USER_ID is None:
        await interaction.response.send_message(
            "The tracked-user referral feature isn't configured (no `TRACKED_USER_ID` set)."
        )
        return
    if interaction.user.id != TRACKED_USER_ID:
        await interaction.response.send_message(
            "Only the tracked user can use this command."
        )
        return

    _tracked_user_last_seen = None
    await interaction.response.send_message(
        "🔓 Unlocked — AI replies will work normally until you post again."
    )


async def countable_button_autocomplete(interaction: discord.Interaction, current: str):
    """Only offer buttons that actually track a count — not toggles,
    timer-only, or link buttons."""
    current = current.lower()
    choices = []
    for button_id in INTERACTIVE_BUTTON_IDS:
        cfg = BUTTON_CONFIG[button_id]
        if cfg.get("toggle", False) or not cfg.get("track_count", True):
            continue
        if current in cfg["label"].lower() or current in button_id:
            choices.append(app_commands.Choice(name=f"{cfg['label']} ({button_id})", value=button_id))
    return choices[:25]


@bot.tree.command(name="set_count", description="Manually set a user's count on a button")
@app_commands.describe(
    user="Whose count to set",
    button="Which button's count to set",
    count="New count",
    counter="Optional: paste a specific counter message link/ID (defaults to the latest one in this channel)",
)
@app_commands.autocomplete(button=countable_button_autocomplete)
async def set_count_command(
        interaction: discord.Interaction,
        user: discord.Member,
        button: str,
        count: int,
        counter: Optional[str] = None,
):
    if not has_counter_admin_permission(interaction):
        await interaction.response.send_message(
            "You don't have permission to use this command."
        )
        return

    state = await resolve_state(interaction, counter)
    if state is None:
        await interaction.response.send_message(
            "I couldn't find a counter here. Run `/counter` first, or pass a specific "
            "counter message link/ID with the `counter` option."
        )
        return

    if button not in BUTTON_CONFIG:
        await interaction.response.send_message(
            f"Unknown button `{button}`. Use the autocomplete list to pick a valid one."
        )
        return

    cfg = BUTTON_CONFIG[button]
    if button in LINK_BUTTON_IDS or cfg.get("toggle", False) or not cfg.get("track_count", True):
        await interaction.response.send_message(
            f"**{cfg['label']}** doesn't track a count (it's a link, toggle, or timer-only button)."
        )
        return

    if count < 0:
        await interaction.response.send_message("Counts can't be negative.")
        return

    state.set_user_count(user.id, button, count)

    confirmation = f"✅ Set **{user.display_name}**'s **{cfg['label']}** count to **{count}**"
    await interaction.response.send_message(confirmation)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No DISCORD_BOT_TOKEN found. Copy .env.example to .env and add your bot token."
        )
    bot.run(TOKEN)
