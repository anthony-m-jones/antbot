"""Item metadata for 8.60 wire parsing — extracted from appearances.dat.

WHY THIS EXISTS
On the 8.60 wire an item on a tile is encoded as its u16 client id followed by
*conditional* extra bytes (ProtocolGame::AddItem, protocolgame.cpp:684, the
oldProtocol/8.60 path):

    u16 id
    if the item is stackable (cumulative):        u8 count
    if the item is a splash or fluid container:   u8 count/subtype

There is no length prefix, so to know how many bytes an item occupies — and thus
stay byte-aligned while walking across a tile's contents — we must know these
flags for every item id. Get one wrong and the rest of the map packet decodes as
garbage.

WHERE THE DATA COMES FROM
Canary 3.6.1 ships no classic Tibia.dat / items.otb; item metadata lives in the
modern protobuf file `data/items/appearances.dat`. Rather than pull in a protobuf
runtime and compile the schema, we hand-parse the three booleans we actually
need. The schema (canary/src/protobuf/appearances.proto) gives the field numbers:

    Appearances.object              = field 1   (repeated Appearance)
    Appearance.id                   = field 1   (uint32)
    Appearance.flags                = field 3   (AppearanceFlags, embedded)
    AppearanceFlags.cumulative      = field 6   (bool)  -> stackable
    AppearanceFlags.liquidpool      = field 12  (bool)  -> splash
    AppearanceFlags.liquidcontainer = field 19  (bool)  -> fluid container

PROTOBUF WIRE FORMAT (the little we rely on)
Every field is preceded by a varint "tag" = (field_number << 3) | wire_type:
  wire_type 0 = varint         (bools/ints — the flags and the id)
  wire_type 1 = 64-bit fixed   (skip 8 bytes)
  wire_type 2 = length-delimited (embedded messages, strings, bytes)
  wire_type 5 = 32-bit fixed   (skip 4 bytes)
Unknown fields are skipped generically by their wire type, so we are robust to
every part of the schema we don't care about.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("antbot")

# Protobuf field numbers we care about (see module docstring).
_F_APPEARANCES_OBJECT = 1
_F_APPEARANCE_ID = 1
_F_APPEARANCE_FLAGS = 3
_F_APPEARANCE_NAME = 4    # bytes: the item's display name (e.g. "a hole", "sewer grate")
_F_FLAG_CUMULATIVE = 6        # stackable  -> +1 count byte on the wire
_F_FLAG_LIQUIDPOOL = 12       # splash     -> +1 count byte on the wire
_F_FLAG_LIQUIDCONTAINER = 19  # fluid      -> +1 count byte on the wire
_F_FLAG_CONTAINER = 5         # bag/chest/... — excluded from stowing
_F_FLAG_MARKET = 36           # AppearanceFlagMarket submessage -> wareId
_F_MARKET_TRADE_AS = 2        # AppearanceFlagMarket.trade_as_object_id
# Not a wire-length flag, but needed for navigation (Phase B): an item flagged
# `unpass` blocks walking onto its tile (walls, closed doors, water edges, ...).
_F_FLAG_UNPASS = 13
# `take` marks an item you can PICK UP (loot, equipment, gold — as opposed to ground,
# walls and scenery). The hauler uses this to know what's worth moving off a tile.
_F_FLAG_TAKE = 18
# For the dashboard minimap (Phase C): a ground item's automap colour is the tile
# colour shown on the Tibia minimap. `automap` is a submessage {color = field 1}.
_F_FLAG_AUTOMAP = 30
_F_AUTOMAP_COLOR = 1
# The GROUND SPEED (Phase B walkability). `bank` is a submessage {waypoints = field 1}
# giving the movement cost of standing on this ground — the client's
# `getGroundSpeed()`. Only real, walkable GROUND tiles carry it; open water / lava /
# void grounds have no `bank` at all, which is exactly the signal we want: a tile whose
# ground has no waypoints is one you can't walk on. See ItemFlags.ground_speed.
_F_FLAG_BANK = 1
_F_BANK_WAYPOINTS = 1

# Protobuf wire types.
_WT_VARINT = 0
_WT_64BIT = 1
_WT_LEN = 2
_WT_32BIT = 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint at `pos`; return (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):  # high bit clear = last byte of the varint
            return result, pos
        shift += 7


def _skip_field(buf: bytes, pos: int, wire_type: int) -> int:
    """Advance past one field's value given its wire type; return new_pos."""
    if wire_type == _WT_VARINT:
        _, pos = _read_varint(buf, pos)
    elif wire_type == _WT_LEN:
        length, pos = _read_varint(buf, pos)
        pos += length
    elif wire_type == _WT_32BIT:
        pos += 4
    elif wire_type == _WT_64BIT:
        pos += 8
    else:
        raise ValueError(f"unsupported protobuf wire type {wire_type} at {pos}")
    return pos


def _parse_automap_color(buf: bytes, start: int, end: int) -> int | None:
    """Parse an AppearanceFlagAutomap submessage; return its colour index."""
    pos = start
    color: int | None = None
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            if field == _F_AUTOMAP_COLOR:
                color = value
        else:
            pos = _skip_field(buf, pos, wire_type)
    return color


def _parse_bank(buf: bytes, start: int, end: int) -> int | None:
    """Parse an AppearanceFlagBank submessage; return its `waypoints` ground speed."""
    pos = start
    waypoints: int | None = None
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            if field == _F_BANK_WAYPOINTS:
                waypoints = value
        else:
            pos = _skip_field(buf, pos, wire_type)
    return waypoints


def _parse_market(buf: bytes, start: int, end: int) -> int | None:
    """Parse an AppearanceFlagMarket submessage; return `trade_as_object_id` (else None).

    This is the server's `wareId`: items.cpp does
        iType.wareId = object.flags().has_market() ? market().trade_as_object_id() : 0
    so the market flag in this asset IS the whole source of truth for wareId. That
    matters far beyond the market itself — see `is_stowable`.
    """
    ware_id: int | None = None
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            if field == _F_MARKET_TRADE_AS:
                ware_id = value
        else:
            pos = _skip_field(buf, pos, wire_type)
    return ware_id


def _parse_flags(buf: bytes, start: int, end: int) -> tuple[bool, bool, bool, int | None, int | None, bool, int | None, bool]:
    """Parse an AppearanceFlags submessage.

    Returns (stackable, liquid, blocking, automap_color, ground_speed, takeable,
    ware_id, container).
    automap_color is the minimap colour index (0-215) if declared, else None.
    ground_speed is the `bank.waypoints` movement cost if the item declares a `bank`
    (i.e. it's a walkable ground), else None. takeable is the `take` flag: the item can
    be picked up (loot/equipment), as opposed to ground/walls/scenery.
    ware_id / container feed `is_stowable`.
    """
    stackable = False
    liquid = False
    blocking = False
    automap_color: int | None = None
    ground_speed: int | None = None
    takeable = False
    ware_id: int | None = None
    container = False
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            if field == _F_FLAG_CUMULATIVE and value:
                stackable = True
            elif field in (_F_FLAG_LIQUIDPOOL, _F_FLAG_LIQUIDCONTAINER) and value:
                liquid = True
            elif field == _F_FLAG_UNPASS and value:
                blocking = True
            elif field == _F_FLAG_TAKE and value:
                takeable = True
            elif field == _F_FLAG_CONTAINER and value:
                container = True
        elif wire_type == _WT_LEN:
            length, pos = _read_varint(buf, pos)
            if field == _F_FLAG_AUTOMAP:
                automap_color = _parse_automap_color(buf, pos, pos + length)
            elif field == _F_FLAG_BANK:
                ground_speed = _parse_bank(buf, pos, pos + length)
            elif field == _F_FLAG_MARKET:
                ware_id = _parse_market(buf, pos, pos + length)
            pos += length
        else:
            pos = _skip_field(buf, pos, wire_type)
    return (stackable, liquid, blocking, automap_color, ground_speed, takeable,
            ware_id, container)


def _parse_appearance(buf: bytes, start: int, end: int) -> tuple[int | None, int, bool, int | None, str, int | None, bool, bool, bool]:
    """Parse one Appearance; return (item_id, wire_extra_bytes, blocking, automap_color,
    name, ground_speed, takeable, stackable, stowable)."""
    item_id: int | None = None
    extra = 0
    blocking = False
    automap_color: int | None = None
    name = ""
    ground_speed: int | None = None
    takeable = False
    stackable = False
    ware_id: int | None = None
    container = False
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 0x07
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(buf, pos)
            if field == _F_APPEARANCE_ID:
                item_id = value
        elif wire_type == _WT_LEN:
            length, pos = _read_varint(buf, pos)
            if field == _F_APPEARANCE_FLAGS:
                (stackable, liquid, blocking, automap_color, ground_speed, takeable,
                 ware_id, container) = _parse_flags(buf, pos, pos + length)
                # One count byte per independent condition (matches AddItem).
                extra = int(stackable) + int(liquid)
            elif field == _F_APPEARANCE_NAME:
                name = buf[pos:pos + length].decode("latin-1", "replace")
            pos += length
        else:
            pos = _skip_field(buf, pos, wire_type)
    # Mirror Item::isStowable(): a market wareId that points at THIS id, and not a
    # container. (The server also excludes tiered/decaying/owned instances, but those
    # are runtime facts about one item, not the type — see is_stowable.)
    stowable = (ware_id is not None and ware_id > 0 and ware_id == item_id
                and not container)
    return (item_id, extra, blocking, automap_color, name, ground_speed, takeable,
            stackable, stowable)


class ItemFlags:
    """Per-item lookups used for wire parsing (A2/A3) and navigation (Phase B).

    - `extra_bytes(id)` -> 0, 1, or 2: count bytes the server appends after an
      item's u16 id on the 8.60 wire. Unknown ids default to 0 (the common case:
      a plain, non-stackable, non-fluid item is just its bare id).
    - `is_blocking(id)` -> bool: whether the item has the `unpass` flag and so
      blocks walking onto its tile (walls, closed doors, rocks, ...).
    """

    def __init__(self, extra: dict[int, int], blocking: set[int],
                 automap: dict[int, int], names: dict[int, str],
                 ground_speed: dict[int, int], takeable: set[int] | None = None,
                 stackable: set[int] | None = None,
                 stowable: set[int] | None = None) -> None:
        # Item ids you can pick up (the `take` flag) — what a hauler may lift off a tile.
        self._takeable = takeable or set()
        # Item ids the Supply Stash will accept (see is_stowable).
        self._stowable = stowable or set()
        # Item ids that STACK (the `cumulative` flag). Needed on its own because
        # `extra_bytes` folds stackable together with liquid, so it can't answer this.
        # It's what makes topping off a stack you already carry cost zero slots.
        self._stackable = stackable or set()
        self._extra = extra
        self._blocking = blocking
        self._automap = automap
        self._names = names
        # item id -> bank.waypoints (ground movement cost). Only present for items
        # that are actually walkable grounds; absent for water/lava/void grounds and
        # for every non-ground item. See `ground_speed` and `has_ground_speed`.
        self._ground_speed = ground_speed
        # Ground tiles you can't stand on even though appearances.dat doesn't flag
        # them `unpass` — chiefly open water and lava. The asset's unpass flag is
        # unreliable for these (only ~half of water ids carry it), so a bot would
        # otherwise treat the sea as walkable, flood across it, and chase frontiers
        # it can never reach. Populated from the server catalog (see
        # catalog.impassable_ground_ids) and consulted ONLY for a tile's ground item
        # (stack index 0), so a decorative "water"/"watermelon" item lying on real
        # ground never wrongly blocks the tile. Empty until a caller fills it.
        self.impassable_ground: set[int] = set()

    def add_impassable_ground(self, ids) -> None:
        """Register item ids that, as a tile's GROUND, make the tile unwalkable."""
        self.impassable_ground.update(ids)

    def extra_bytes(self, item_id: int) -> int:
        return self._extra.get(item_id, 0)

    def is_blocking(self, item_id: int) -> bool:
        return item_id in self._blocking

    def is_takeable(self, item_id: int) -> bool:
        """Can this item be picked up? (the assets' `take` flag)

        True for loot/equipment/gold; False for ground, walls and scenery. The hauler
        uses it to decide what on a tile is worth lifting — without it we'd try to pick
        up the floor and get refused.
        """
        return item_id in self._takeable

    def is_stackable(self, item_id: int) -> bool:
        """Does this item stack? (the assets' `cumulative` flag)

        Up to `economy.STACK_LIMIT` per stack. This is what makes a stackable cheap to
        carry: adding to a stack you already hold costs no extra slot at all.
        """
        return item_id in self._stackable

    def is_stowable(self, item_id: int) -> bool:
        """Will the Supply Stash accept this item? (mirrors Item::isStowable)

        The stash is the ONLY safe storage 8.60 can reach: its withdraw packet is
        oldProtocol-gated, but depositing rides the ordinary move path, so a bot can put
        things in even though it can never take them out again. That makes this question
        worth answering BEFORE walking to a depot — the server's answer is just the
        prose "This item cannot be stowed here."

        The server's test is `hasMarketAttributes() && !tier && wareId > 0 &&
        !isContainer() && wareId == id`, and wareId comes straight from this asset file
        (items.cpp: `object.flags().market().trade_as_object_id()`). So the type-level
        answer is fully offline. The parts we can't see here are per-INSTANCE, and each
        one only ever makes a stow fail, never succeed: an item that is tiered, has an
        owner, or can decay is refused. In other words this is an upper bound — treat a
        False as certain and a True as "the server should accept it".
        """
        return item_id in self._stowable

    def automap_color(self, item_id: int) -> int | None:
        """Minimap colour index (0-215) for this item, or None if it declares none."""
        return self._automap.get(item_id)

    def ground_speed(self, item_id: int) -> int | None:
        """The item's `bank.waypoints` ground speed, or None if it has no `bank`.

        Only walkable GROUND tiles carry this. `None` means either "not a ground" or
        "an un-walkable ground" (open water / lava / void) — the client's own signal
        that you can't stand there. See `has_ground_speed` for the walkability test.
        """
        return self._ground_speed.get(item_id)

    def has_ground_speed(self, item_id: int) -> bool:
        """True if this item is a ground you can walk on (declares a `bank.waypoints`).

        This is the client-accurate walkability test for a tile's GROUND item: a real
        floor has a movement speed; the sea/lava/void does not. Used (on the ground
        item only) to reject tiles the `unpass` flag misses — the sea being the big one.
        """
        return item_id in self._ground_speed

    def name(self, item_id: int) -> str:
        """The item's display name (e.g. 'a hole', 'sewer grate'), or '' if unknown."""
        return self._names.get(item_id, "")

    def ids_matching(self, *keywords: str) -> set[int]:
        """All item ids whose name contains any of the (lowercase) keywords.

        Used by the traversal registry to auto-discover ladders/holes/doors/etc.
        by name, so we don't have to hand-list every graphic variant.
        """
        needles = [k.lower() for k in keywords]
        return {
            item_id for item_id, nm in self._names.items()
            if any(n in nm.lower() for n in needles)
        }

    def __len__(self) -> int:
        return len(self._extra)

    @classmethod
    def load(cls, path: str | Path) -> "ItemFlags":
        """Load and parse appearances.dat into the item-flag lookups."""
        buf = Path(path).read_bytes()
        extra: dict[int, int] = {}
        blocking: set[int] = set()
        automap: dict[int, int] = {}
        names: dict[int, str] = {}
        ground_speed: dict[int, int] = {}
        takeable: set[int] = set()
        stackable_ids: set[int] = set()
        stowable_ids: set[int] = set()
        pos, end = 0, len(buf)
        # Top level is an `Appearances` message: a stream of field-1 (`object`)
        # length-delimited Appearance submessages, plus other repeated fields we
        # ignore (outfits/effects/missiles live in different id spaces and are
        # never sent as tile items).
        while pos < end:
            tag, pos = _read_varint(buf, pos)
            field, wire_type = tag >> 3, tag & 0x07
            if wire_type == _WT_LEN:
                length, pos = _read_varint(buf, pos)
                if field == _F_APPEARANCES_OBJECT:
                    (item_id, item_extra, item_blocking, item_automap, item_name,
                     item_speed, item_take, item_stack,
                     item_stow) = _parse_appearance(buf, pos, pos + length)
                    if item_id is not None:
                        extra[item_id] = item_extra
                        if item_take:
                            takeable.add(item_id)
                        if item_stack:
                            stackable_ids.add(item_id)
                        if item_stow:
                            stowable_ids.add(item_id)
                        if item_blocking:
                            blocking.add(item_id)
                        if item_automap is not None:
                            automap[item_id] = item_automap
                        if item_name:
                            names[item_id] = item_name
                        if item_speed is not None:
                            ground_speed[item_id] = item_speed
                pos += length
            else:
                pos = _skip_field(buf, pos, wire_type)

        wire_variable = sum(1 for v in extra.values() if v > 0)
        log.info("loaded item flags for %d objects (%d stackable/liquid, %d blocking, %d minimap, %d named, %d walkable-ground, %d takeable, %d stackable, %d stowable) from %s",
                 len(extra), wire_variable, len(blocking), len(automap), len(names),
                 len(ground_speed), len(takeable), len(stackable_ids),
                 len(stowable_ids), path)
        return cls(extra, blocking, automap, names, ground_speed, takeable,
                   stackable_ids, stowable_ids)


def automap_color_to_rgb(color: int) -> tuple[int, int, int]:
    """Convert a Tibia minimap colour index (0-215) to an (r, g, b) triple.

    Tibia's minimap uses a 6x6x6 colour cube: each channel takes one of
    {0, 51, 102, 153, 204, 255}. Index = r*36 + g*6 + b in that cube.
    """
    color = max(0, min(215, color))
    r = (color // 36) % 6
    g = (color // 6) % 6
    b = color % 6
    return (r * 51, g * 51, b * 51)
