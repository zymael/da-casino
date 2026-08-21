# Never mutated on disk -- every call re-composites a fresh copy, same pattern as
# ranch_render.py/cards_render.py etc. Dialogue rendering itself now lives in npc_render.py
# (shared by every NPC/hub, replacing this module's old render_roy_greeting); this module just
# owns the path, same as dungeon_render.BANNER_PATH/ranch_render.BANNER_PATH.
BANNER_PATH = "assets/casino_banner.png"
