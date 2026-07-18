"""Server item catalog — names + traversal behaviour from Canary's items.xml.

WHY THIS EXISTS
---------------
appearances.dat (the client asset we parse in items.py) leaves environment objects
UNNAMED and never flags floor-change, so from the *client* side a ladder, a hole
and a patch of grass all look like anonymous non-blocking tiles. That's the wall we
kept hitting when trying to recognise the objects a bot can travel through.

But WE run the server, and Canary ships `data/items/items.xml`: a catalog keyed by
the SAME item id the client uses. We confirmed this in the Canary source —
`Items::loadFromProtobuf()` stores each ItemType at `items[appearance.id()]`
(indexing by the client's appearance id), then `loadFromXml()` merges items.xml
into that very array by its `id`/`fromid` attribute. So:

    items.xml `id`  ==  client appearance id  ==  the id we already store in
                                                   GameState.tiles from the map

That means items.xml is ground truth we can read straight off disk and join to the
tiles our bots see — no id translation, no in-game "Look" round-trips. The Look
command (client asks the server to describe a tile by pos+stackpos, server replies
with text built from this same data) would give the same names at runtime, but the
file is complete, instant and offline, so we read the file.

WHAT THIS MODULE DOES
---------------------
It is a plain, stdlib-only reader of items.xml. It does NOT decide traversal
categories — that mapping (floorchange/type -> ladder/hole/teleporter/...) lives in
traversal.py, which is the one place that knows about categories. Here we only
surface the raw facts per id: name, article, and the two attributes that reveal how
a tile behaves — `floorchange` (down / north / south / east / west / …alt) and
`type` (door / teleport / ladder / key / bed / carpet / …).
"""

from __future__ import annotations

import dataclasses
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger("antbot")


def _int_or_none(value: str | None) -> int | None:
    """items.xml attribute values are strings; a few are malformed. Never explode."""
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


@dataclasses.dataclass
class ItemInfo:
    """The catalog facts for one item id (see module docstring)."""

    id: int
    name: str = ""
    article: str = ""             # "a" / "an" / "" — grammatical article for the name
    floorchange: str | None = None  # "down","north","south","east","west","southalt","eastalt"
    type: str | None = None         # "door","teleport","ladder","key","bed","carpet",...
    # Economy facts (Phase F). `weight` is the item's weight in the server's units and
    # is what bounds a hauler's load; `container_size` is how many SLOTS a bag/backpack
    # adds, which is what the pre-run outfitting decision trades capacity for.
    weight: int | None = None
    container_size: int | None = None

    @property
    def full_name(self) -> str:
        """"a ladder", "an energy shrine", or just the bare name when article-less."""
        return f"{self.article} {self.name}".strip() if self.article else self.name


def load_item_catalog(path: str | Path) -> dict[int, ItemInfo]:
    """Parse items.xml into a dict of {item_id: ItemInfo}.

    Handles both spellings the file uses for its ids:
      - a single `<item id="433" .../>`, and
      - a compact range `<item fromid="482" toid="483" .../>` that assigns the same
        name/attributes to every id in [fromid, toid] (the file uses this heavily
        for families of near-identical tiles).

    The attributes we care about live in nested `<attribute key=... value=.../>`
    children, so we scan those too: `floorchange`/`type` (how a tile behaves) and
    `weight`/`containerSize` (what a hauler can carry — see economy.py). The rest of
    the file (armor, slotType, …) is ignored; we surface more the day a role needs it.
    """
    path = Path(path)
    catalog: dict[int, ItemInfo] = {}

    # The file is a few MB; a one-shot parse is simplest and comfortably within
    # memory. Each direct <item> child of <items> is one definition.
    root = ET.parse(str(path)).getroot()
    for elem in root.findall("item"):
        name = elem.get("name", "") or ""
        article = elem.get("article", "") or ""

        floorchange: str | None = None
        typ: str | None = None
        weight: int | None = None
        container_size: int | None = None
        for attr in elem.findall("attribute"):
            # items.xml is inconsistent about case: the SAME attribute appears as both
            # `containerSize` (303 items) and `containersize` (2361 — the common one),
            # so match case-insensitively or we'd silently miss most containers.
            key = (attr.get("key") or "").lower()
            if key == "floorchange":
                floorchange = attr.get("value")
            elif key == "type":
                typ = attr.get("value")
            elif key == "weight":
                weight = _int_or_none(attr.get("value"))
            elif key == "containersize":
                container_size = _int_or_none(attr.get("value"))

        # Resolve which ids this definition applies to.
        if elem.get("id") is not None:
            ids: range | list[int] = [int(elem.get("id"))]
        elif elem.get("fromid") is not None and elem.get("toid") is not None:
            ids = range(int(elem.get("fromid")), int(elem.get("toid")) + 1)
        else:
            continue  # a definition with no id at all — nothing to key on

        for item_id in ids:
            catalog[item_id] = ItemInfo(item_id, name, article, floorchange, typ,
                                        weight, container_size)

    log.info("catalog: loaded %d item definitions from %s", len(catalog), path.name)
    return catalog


# Name fragments that identify a ground tile you cannot stand on (open water, lava).
# Matching is intentionally broad because the result is only ever consulted for a
# tile's GROUND item (stack index 0) — see ItemFlags.impassable_ground — so a
# "watermelon" or "water bucket" sitting on real ground is never affected.
# Substrings that mark a GROUND tile (tile item index 0) as impassable liquid, used
# to fix walkability the assets under-flag. "water"/"lava" catch most of it, but the
# open sea around coastlines uses tiles named "ocean floor" / "sea floor" (no "water"
# in the name) — miss those and a scout floods find_frontier straight across the
# ocean, picks a frontier out at sea, and walks a peninsula trying to reach land it
# can never touch. These phrases are specific enough not to catch walkable oddities
# like coastal "stairs"/"rock" that merely render with a bluish automap colour. The
# real long-term fix is the client-style rule (a tile is walkable only if its ground
# carries a movement speed) — see BACKLOG; until we parse that flag, names it is.
_IMPASSABLE_GROUND_KEYWORDS = ("water", "lava", "ocean floor", "sea floor")


def impassable_ground_ids(catalog: dict[int, ItemInfo],
                          keywords=_IMPASSABLE_GROUND_KEYWORDS) -> set[int]:
    """Item ids whose catalog name marks them as impassable liquid ground.

    Used to fix walkability for the sea/lava: appearances.dat under-flags these as
    `unpass`, so we blend in the server's names to recognise them. See
    `ItemFlags.impassable_ground` for why matching broadly here is safe.
    """
    needles = tuple(k.lower() for k in keywords)
    return {
        item_id for item_id, info in catalog.items()
        if info.name and any(k in info.name.lower() for k in needles)
    }
