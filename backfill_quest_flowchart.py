"""One-off script: migrates quests.json from its old flat-array stage shape (stage identity =
array position) to the new stage-graph shape the flowchart editor and quests.py's ordinal-based
progress tracking expect (stage identity = an explicit "id" for graph edges plus a permanent
"ordinal" for durable flag storage -- see quests.py's own module docstring for the full design).

Touches quests.json only -- never opens a database connection or a Discord connection. This is
safe by construction, not just in practice: every backfilled stage's ordinal is assigned equal to
its original array position (0, 1, 2, ... in each quest's own original stage order), which is
*exactly* the integer today's "quest:<id>" flags already encode (see quests.py's module docstring
and _is_complete's own comment) -- so every row already in casino.db decodes correctly under the
new scheme with zero rewriting.

Idempotent guard: if the first quest's first stage already has an "ordinal" key, this is a no-op
(prints a message and exits without touching anything) -- appropriate for a one-time structural
migration, unlike e.g. backfill_bet_log.py's per-row dedup (which fits an incremental job, not
this one).

Usage:
    python backfill_quest_flowchart.py              # migrates quests.json in place (with a
                                                      # timestamped .bak taken first)
    python backfill_quest_flowchart.py --out PATH    # writes the migrated candidate to PATH
                                                      # instead, leaving quests.json untouched
                                                      # (used by the verification pass before ever
                                                      # touching the real file)
"""

import argparse
import copy
import json
import os
import shutil
from datetime import datetime

_QUESTS_PATH = os.path.join(os.path.dirname(__file__), "quests.json")


def already_migrated(quests: list[dict]) -> bool:
    return bool(quests) and bool(quests[0].get("stages")) and "ordinal" in quests[0]["stages"][0]


def migrate_quest(quest: dict) -> dict:
    old_stages = quest["stages"]
    new_stages = []
    for i, old_stage in enumerate(old_stages):
        stage_id = f"stage_{i}"
        new_stage = {
            "id": stage_id,
            "ordinal": i,
            "prompt": old_stage["prompt"],
            "journal_text": old_stage["journal_text"],
        }
        if old_stage.get("button_label"):
            new_stage["button_label"] = old_stage["button_label"]

        trigger = old_stage.get("trigger")
        if trigger is None:
            # Dialogue-only terminal (e.g. goo_zgoolok_bane) -- its old on_complete_message was
            # never read by any code path (turn_in() bailed out before reaching it whenever
            # trigger was None), so it's dropped here rather than carried into a path that can
            # never actually be taken.
            new_stage["paths"] = []
        else:
            path = {
                "trigger": trigger,
                "on_complete_message": old_stage["on_complete_message"],
                "x": 230 + i * 220,
                "y": 40,
            }
            if old_stage.get("reward"):
                path["reward"] = old_stage["reward"]
            if old_stage.get("reward_item"):
                path["reward_item"] = old_stage["reward_item"]
            if old_stage.get("reward_item_kind"):
                path["reward_item_kind"] = old_stage["reward_item_kind"]
            if old_stage.get("turn_in_label"):
                path["turn_in_label"] = old_stage["turn_in_label"]
            if i + 1 < len(old_stages):
                path["next"] = f"stage_{i + 1}"
            new_stage["paths"] = [path]

        new_stages.append(new_stage)

    new_quest = {k: v for k, v in quest.items() if k != "stages"}
    new_quest["start_stage"] = "stage_0"
    new_quest["next_ordinal"] = len(old_stages)
    new_quest["layout"] = {f"stage_{i}": {"x": 40 + i * 220, "y": 40} for i in range(len(old_stages))}
    new_quest["stages"] = new_stages
    return new_quest


def migrate(quests: list[dict]) -> list[dict]:
    return [migrate_quest(copy.deepcopy(quest)) for quest in quests]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the migrated candidate here instead of overwriting quests.json")
    args = parser.parse_args()

    with open(_QUESTS_PATH) as f:
        quests = json.load(f)

    if already_migrated(quests):
        print("quests.json already migrated (first stage already has an 'ordinal' key) -- nothing to do.")
        return

    migrated = migrate(quests)

    if args.out:
        # A pure candidate write, deliberately NOT validated by this script -- quests.py's own
        # module-level code calls its real loader against the *live* quests.json at import time,
        # so a fresh `import quests` in this same process can't validate a candidate sitting
        # somewhere else without the live file already being migrated first (see the in-place
        # branch below). The caller is expected to validate this candidate itself, e.g. by
        # temporarily swapping it into a scratch quests.json and importing quests against that.
        with open(args.out, "w") as f:
            json.dump(migrated, f, indent=2)
        print(f"Wrote migrated candidate to {args.out} (quests.json untouched, not validated by this script).")
        return

    # In-place: write first, then validate, rolling back immediately on any failure -- the mirror
    # image of "write to a temp file, validate, only then replace the live file" (this repo's usual
    # save discipline), forced by the above import-time constraint. quests.json is JSON content,
    # hot-reloadable in the running bot only through the admin panel's own save routes -- a raw
    # file edit like this one is never picked up by an already-running process until it's
    # restarted, so writing it directly here (backed by an automatic .bak and immediate rollback on
    # failure) carries no more risk than the admin panel's own edit flow already does.
    backup_path = f"{_QUESTS_PATH}.bak-{datetime.now():%Y%m%d%H%M%S}-pre-quest-flowchart-migration"
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
