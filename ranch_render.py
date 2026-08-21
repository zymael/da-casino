# Never mutated on disk -- every call re-composites a fresh copy over the original banner, same
# as every other dynamically-rendered image in this bot (cards_render.py etc). Dialogue rendering
# itself now lives in npc_render.py (shared by every NPC/hub); this module just owns the path,
# same as dungeon_render.BANNER_PATH/casino_render.BANNER_PATH.
BANNER_PATH = "assets/ranch_banner.png"
