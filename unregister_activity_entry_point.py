"""One-off script: removes this Application's PRIMARY_ENTRY_POINT command (the Activity's Launch
button), the counterpart to register_activity_entry_point.py -- now that the Activity itself has
been stripped from the codebase, the command would otherwise linger in Discord's UI pointing at a
page that no longer exists.

Run manually, once:
    python3 unregister_activity_entry_point.py
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


def _request(method: str, path: str = "", body: dict | None = None):
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
        body_bytes = resp.read()
        return json.loads(body_bytes.decode()) if body_bytes else None


def main():
    existing = [cmd for cmd in _request("GET") if cmd["type"] == 4]
    if not existing:
        print("No PRIMARY_ENTRY_POINT command found -- nothing to do.")
        return

    command = existing[0]
    try:
        _request("DELETE", f"/{command['id']}")
        print(f"Deleted PRIMARY_ENTRY_POINT command {command['id']!r} ({command['name']!r}).")
    except urllib.error.HTTPError as e:
        print(e.code)
        print(e.read().decode())
        raise


if __name__ == "__main__":
    main()
