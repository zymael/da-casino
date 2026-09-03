"""One-off script: adds the new "discuss_with" field (a list of npc ids who offer a topic button
for that stage -- see quests.py's own module docstring) to every stage in quests.json, defaulting
it to [quest["npc"]] -- the quest's own giver, exactly matching who could discuss a stage's dialogue
before this field existed. Byte-identical player-facing behavior immediately after this migration;
an admin can then add more NPCs to a stage's discuss_with going forward.

Assumes quests.json is already in the stage-graph shape backfill_quest_flowchart.py produces (every
quest has "npc" and a "stages" list of dicts) -- true for this repo's live quests.json already.

Touches quests.json only -- never opens a database connection or a Discord connection.

Idempotent guard: if the first quest's first stage already has a "discuss_with" key, this is a
no-op (prints a message and exits without touching anything).

Usage:
    python backfill_discuss_with.py              # migrates quests.json in place (with a
                                                    # timestamped .bak taken first)
    python backfill_discuss_with.py --out PATH    # writes the migrated candidate to PATH instead,
                                                    # leaving quests.json untouched (used by the
                                                    # verification pass before ever touching the
                                                    # real file)
"""

import argparse
import copy
import json
import os
import shutil
from datetime import datetime

_QUESTS_PATH = os.path.join(os.path.dirname(__file__), "quests.json")


def already_migrated(quests: list[dict]) -> bool:
    return bool(quests) and bool(quests[0].get("stages")) and "discuss_with" in quests[0]["stages"][0]


def migrate_quest(quest: dict) -> dict:
    new_quest = copy.deepcopy(quest)
    for stage in new_quest["stages"]:
        stage.setdefault("discuss_with", [quest["npc"]])
    return new_quest


def migrate(quests: list[dict]) -> list[dict]:
    return [migrate_quest(quest) for quest in quests]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the migrated candidate here instead of overwriting quests.json")
    args = parser.parse_args()

    with open(_QUESTS_PATH) as f:
        quests = json.load(f)

    if already_migrated(quests):
        print("quests.json already migrated (first stage already has a 'discuss_with' key) -- nothing to do.")
        return

    migrated = migrate(quests)

    if args.out:
        # A pure candidate write, deliberately NOT validated by this script -- see
        # backfill_quest_flowchart.py's own comment on this same pattern for why.
        with open(args.out, "w") as f:
            json.dump(migrated, f, indent=2)
        print(f"Wrote migrated candidate to {args.out} (quests.json untouched, not validated by this script).")
        return

    # In-place: write first, then validate, rolling back immediately on any failure -- see
    # backfill_quest_flowchart.py's own comment on this same pattern for why it's the mirror image
    # of this repo's usual "validate a temp file, only then replace the live one" discipline.
    backup_path = f"{_QUESTS_PATH}.bak-{datetime.now():%Y%m%d%H%M%S}-pre-discuss-with-migration"
    shutil.copy(_QUESTS_PATH, backup_path)
    with open(_QUESTS_PATH, "w") as f:
        json.dump(migrated, f, indent=2)

    try:
        import quests  # noqa: F401  -- import time itself runs the real, strict loader
    except Exception as e:
        shutil.copy(backup_path, _QUESTS_PATH)
        print(f"Migrated content failed validation ({e!r}) -- restored quests.json from backup. Backup kept at {backup_path}.")
        raise

    print(f"Validated OK. Migrated quests.json in place. Backup at {backup_path}.")


if __name__ == "__main__":
    main()
