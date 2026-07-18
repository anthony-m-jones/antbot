"""Traversal registry — classifying the objects you can travel through.

See TRAVERSAL_DESIGN.md for the full picture. In short: teleporters, stairs,
ladders, grates, wells, holes and doors are all specific game OBJECTS, and once we
know a tile carries one we know how to deal with it (step / use / use-a-tool /
use-a-key / cast). The dream was to look each object's kind up from the assets by
name — but appearances.dat leaves these environmental objects UNNAMED (only
pickups have names), and it doesn't flag floor-change either. So instead the
colony LEARNS object ids from experience: when a bot steps onto a tile and gets
whisked, we record that tile's object id as (e.g.) a teleporter, and from then on
every tile with that id is known proactively — no need to step on each one.

That covers the step-based objects (teleporters, stairs) we can learn by walking.
Use-based objects (ladders, grates, doors, shovel/rope holes) can't be learned by
stepping (nothing happens until you use a tool/spell).

We now SEED the registry from ground truth: Canary's items.xml (see catalog.py)
gives us, per client id, a `type` (door / teleport / ladder / …) and a
`floorchange` (down / north / …). `category_from_catalog` below turns those into
our categories, and `TraversalRegistry.seed_from_catalog` pre-loads them. Seeded
knowledge is trusted but SOFT: a bot's own observation (via `learn`) always wins,
which is the "wiggle room" for Tibia's exceptions (a hole that, unusually, doesn't
drop you). items.xml doesn't cover the Lua-scripted use objects (grates, wells,
rope spots, shovel holes) — those still need a later source (action scripts or a
trial-use pass); the CATEGORY_META model below already describes them all.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

log = logging.getLogger("antbot")

# --- how you traverse a thing -------------------------------------------------
STEP = "step"            # walk onto the tile (teleporters, stairs, open holes)
USE = "use"              # "use" the object (ladders, grates, wells, unlocked doors)
USE_WITH = "use_with"    # use a tool/key ON it (shovel holes, rope spots, locked doors)
CAST = "cast"            # cast a spell (Magic Rope up a rope spot)

# --- which way it takes you ---------------------------------------------------
UP = "up"
DOWN = "down"
TELEPORT = "teleport"
UNBLOCK = "unblock"      # doors: opens the way rather than moving you

# category -> (action, direction, required tool/spell or None)
CATEGORY_META: dict[str, tuple[str, str, str | None]] = {
    "teleporter":    (STEP, TELEPORT, None),
    "stairs_up":     (STEP, UP, None),
    "stairs_down":   (STEP, DOWN, None),
    "ladder":        (USE, UP, None),
    "grate":         (USE, DOWN, None),
    "well":          (USE, DOWN, None),
    "hole_open":     (STEP, DOWN, None),
    "shovel_hole":   (USE_WITH, DOWN, "shovel"),
    "rope_spot":     (USE_WITH, UP, "rope"),   # or CAST "exani tera" if we can
    "door_unlocked": (USE, UNBLOCK, None),
    "door_locked":   (USE_WITH, UNBLOCK, "key"),
    "jungle_grass":  (USE_WITH, UNBLOCK, "machete"),  # cut the grass to clear the path
}

_STEP_FILE = Path(__file__).with_name("learned_objects.json")

# Item ids that satisfy a category's `requires` tool (from items.xml by name/type).
# The executor looks the carried tool up here to fire a use-with. A locked door's
# `requires="key"` is deliberately absent: which key fits which lock is per-door, a
# later problem — for now those just skip.
TOOL_IDS: dict[str, tuple[int, ...]] = {
    "shovel": (3457, 7883, 7894, 15689),
    "rope": (3003, 6981, 6982, 6983, 7884),
    "machete": (3308,),
}

# Object ids that Canary's Lua action scripts drive but items.xml doesn't flag
# (curated from data-otservbr-global/scripts/lib/register_actions.lua, verified by
# reading onUseShovel/onUseRope/onUseMachete). We seed these so a bot recognises them
# on sight instead of trial-using every one. They're all USE_WITH (need a carried
# tool), so the executor can't ACT on them until inventory parsing lands — but
# recognising them keeps a scout from mistaking one for a dead tile. NOTE: grates and
# wells turned out to be QUEST-specific in Canary (per-position scripts), not a
# generic id mechanic, so there's no generic list to pull for those; rope-spots are a
# tile flag rather than an id list (a later source).
_CURATED_OBJECTS: dict[str, list[int]] = {
    # `holes`: a shovel digs these open and drops you down (onUseShovel: transform +
    # z+1 + teleport down).
    "shovel_hole": [593, 606, 608, 867, 21341],
    # `jungleGrass`: a machete cuts these, opening the path.
    "jungle_grass": [3696, 3702, 17153],
}


@dataclasses.dataclass
class TraversalKind:
    category: str
    action: str
    direction: str
    requires: str | None


def category_from_relocation(old_z: int, new_z: int, xy_jump: int) -> str:
    """Classify a step-triggered relocation into a learnable object category."""
    if xy_jump > 2:          # a real jump across the map
        return "teleporter"
    return "stairs_up" if new_z < old_z else "stairs_down"


def category_from_catalog(info) -> str | None:
    """Map an items.xml `ItemInfo` (see catalog.py) to a traversal category.

    Returns None when the item isn't a traversal object (the vast majority — most
    ids are grass, walls, loot, …). The `type` attribute is the strongest signal;
    `floorchange` catches stairs/ramps/holes that have no explicit type.

    Notes on the mappings:
    - `type=teleport` is NOT seeded: it means teleport-CAPABLE, and Canary tags
      common floor/deck tiles with it (wooden floor 628/878, carved stone 516,
      boats), so seeding it labelled every wooden-floor tile a teleporter (128 in one
      harbour!). Real teleporters relocate you, so we learn them by BEHAVIOUR instead
      (category_from_relocation on a step that jumps us) — no false positives.
    - `type=ladder` is the USE-to-climb-UP ladder. The walk-onto-to-descend "ladder"
      tiles (e.g. id 433) carry `floorchange=down` instead and fall through to
      hole_open below — which is correct, since you traverse them by stepping.
    - `type=door` seeds as door_unlocked (the optimistic default: try to open it).
      A door that turns out to be locked is corrected the moment a USE fails. A few
      non-doors carry type=door in the assets (tables, some windows); we drop those
      by name so we don't try to "open" furniture.
    - horizontal `floorchange` (n/s/e/w/alt) is stairs; items.xml doesn't say up vs
      down, so we seed the common case (up) and let observation refine it.
    """
    t = (info.type or "").lower()
    name = (info.name or "").lower()
    if t == "ladder":
        return "ladder"           # USE -> up
    if t == "door" and not any(w in name for w in ("table", "window")):
        return "door_unlocked"    # USE -> unblock (locked ones get corrected on use)

    fc = (info.floorchange or "").lower()
    if fc == "down":
        return "hole_open"        # STEP -> down (holes, trapdoors, descend-ladders)
    if fc in ("north", "south", "east", "west", "southalt", "eastalt"):
        return "stairs_up"        # STEP -> up (common case; learner refines to down)
    return None


class TraversalRegistry:
    """item id -> object category, learned from behaviour and persisted.

    Only holds what we've actually learned (step-based objects, for now). Use-based
    ids will be added later; the CATEGORY_META table already describes how to
    traverse every kind once an id is known.
    """

    def __init__(self, path: Path | None = _STEP_FILE) -> None:
        self._path = path
        # Two layers, consulted learned-first so observation always beats the
        # catalog (the "wiggle room"):
        #   _by_id  : learned from a bot's own experience. Persisted to disk.
        #   _seeded : ground truth read from items.xml each startup. NOT persisted
        #             (it's cheap to re-derive and we never want stale catalog data).
        self._by_id: dict[int, str] = {}
        self._seeded: dict[int, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path and self._path.exists():
            try:
                self._by_id = {int(k): v for k, v in json.loads(self._path.read_text()).items()}
                log.info("traversal: loaded %d known objects from %s", len(self._by_id), self._path)
            except (OSError, ValueError) as err:
                log.warning("traversal: could not load objects: %s", err)

    def _save(self) -> None:
        if self._path:
            try:
                self._path.write_text(json.dumps({str(k): v for k, v in self._by_id.items()}))
            except OSError as err:
                log.warning("traversal: could not save objects: %s", err)

    def learn(self, item_id: int, category: str) -> None:
        if self._by_id.get(item_id) != category:
            self._by_id[item_id] = category
            log.info("traversal: learned item %d is a %s", item_id, category)
            self._save()

    def learn_from_tile(self, items: list[int], category: str) -> None:
        """We got relocated stepping on a tile — record its object's id.

        The object (teleporter/stair) is the topmost item on the tile, so learn
        that id. Heuristic, but self-consistent: the same object id recurs on every
        instance of that shortcut.
        """
        if items:
            self.learn(items[-1], category)

    def seed_from_catalog(self, catalog: dict) -> int:
        """Pre-load categories from Canary's items.xml (see catalog.py).

        `catalog` is {id: ItemInfo}. We keep these in the soft `_seeded` layer, so a
        bot's own learned observation still wins. Returns how many ids were seeded.
        """
        seeded: dict[int, str] = {}
        for item_id, info in catalog.items():
            category = category_from_catalog(info)
            if category is not None:
                seeded[item_id] = category
        self._seeded = seeded
        log.info("traversal: seeded %d objects from the item catalog", len(seeded))
        return len(seeded)

    def seed_curated(self) -> int:
        """Add the Lua-script-derived use-with objects (see _CURATED_OBJECTS).

        Call AFTER seed_from_catalog (it adds to the same `_seeded` layer). Doesn't
        override a catalog category for an id. Returns how many new ids were added.
        """
        added = 0
        for category, ids in _CURATED_OBJECTS.items():
            for item_id in ids:
                if item_id not in self._seeded:
                    self._seeded[item_id] = category
                    added += 1
        log.info("traversal: seeded %d curated use-with objects (shovel/machete)", added)
        return added

    def category_of(self, item_id: int) -> str | None:
        # Learned (observed) beats seeded (catalog); that's the wiggle room.
        return self._by_id.get(item_id) or self._seeded.get(item_id)

    def classify(self, items: list[int]) -> tuple[str, int] | None:
        """Return (category, item_id) if this tile carries a known object, else None.

        Lets us recognise a teleporter / stair / hole / ladder / door from the map
        alone, so wanderers avoid them, travellers use them, and the pruner never
        mistakes a known object for a dead tile. Consults learned then seeded data.
        """
        for item_id in reversed(items):  # topmost first
            category = self._by_id.get(item_id) or self._seeded.get(item_id)
            if category is not None:
                return (category, item_id)
        return None

    def kind(self, category: str) -> TraversalKind | None:
        meta = CATEGORY_META.get(category)
        if meta is None:
            return None
        return TraversalKind(category, *meta)

    def __len__(self) -> int:
        # The learned (persisted) count — what "known objects" has always meant.
        return len(self._by_id)

    @property
    def seeded_count(self) -> int:
        """How many ids the item catalog contributed (not persisted)."""
        return len(self._seeded)
