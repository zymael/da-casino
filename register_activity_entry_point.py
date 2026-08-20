"""One-off script: registers the PRIMARY_ENTRY_POINT application command that gives this
Application a Launch button for its Activity. discord.py 2.7.1 has no support for this command
type (its AppCommandType only has chat_input/message/user, and no `handler` field at all), so
this hits Discord's REST API directly.

Deliberately a single POST of one command, never a bulk PUT (bulk-overwrite would wipe every
other global command this app has registered, including bot.py's /play). Safe to re-run --
Discord treats a POST with the same `name` as an upsert of that one command, not a duplicate.

Run manually, once (or again if you want to change the name/description):
    python3 register_activity_entry_point.py
"""

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

APPLICATION_ID = os.environ["DISCORD_ACTIVITY_CLIENT_ID"]
BOT_TOKEN = os.environ["DISCORD_TOKEN"]

# Discord's edge rejects requests with no (or a generic) User-Agent.
USER_AGENT = "da-casino-bot (https://github.com/zymael/da-casino, 1.0)"

COMMAND = {
    "name": "play-rpg",
    "description": "Launch the Da Casino top-down RPG",
    "type": 4,  # PRIMARY_ENTRY_POINT
    "handler": 2,  # DISCORD_LAUNCH_ACTIVITY -- Discord launches the Activity directly
}


def _request(method: str, path: str = "", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/applications/{APPLICATION_ID}/commands{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def main():
    # Discord allows exactly one PRIMARY_ENTRY_POINT command per application -- enabling
    # Activities in the Developer Portal auto-creates a default one ("launch" / "Launch an
    # activity"), so check first rather than assuming this script needs to create it. If it
    # already exists (with any name/description), that's the Launch button and there's nothing
    # to do here.
    existing = [cmd for cmd in _request("GET") if cmd["type"] == 4]
    if existing:
        print("PRIMARY_ENTRY_POINT command already exists, nothing to do:")
        print(json.dumps(existing[0], indent=2))
        return

    try:
        created = _request("POST", body=COMMAND)
        print("Created:")
        print(json.dumps(created, indent=2))
    except urllib.error.HTTPError as e:
        print(e.code)
        print(e.read().decode())
        raise


if __name__ == "__main__":
    main()
