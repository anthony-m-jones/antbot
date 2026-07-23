"""The bot's model of the game world — Phase A of the roadmap.

A1 got us our own position out of the login snapshot. A2+A3 (this file now)
parse the *map* the server sends at login: the grid of tiles, the items on them,
and the creatures standing on them. That gives us two things at once — a live set
of nearby creatures, and a walkable-tile grid that Phase B pathfinding will use.

Everything here mirrors Canary's send-side source exactly, so field order and the
run-length encoding are authoritative rather than guessed. Key references:

- sendAddCreature (protocolgame.cpp:8485) — the 0x0A self-appear that leads login.
- sendMapDescription (protocolgame.cpp:8362) — 0x64, prefixed with our position,
  then a GetMapDescription over an 18x14 viewport.
- GetMapDescription / GetFloorDescription (protocolgame.cpp:2234 / 2258) — the
  floor loop and the skip run-length encoding of tiles.
- GetCipsoft860TileDescription (protocolgame.cpp:2090) — a tile's things: ground
  item, stacked items, then creatures (up to 10 things total).
- AddItem (protocolgame.cpp:684) — item = u16 id + conditional count bytes (see
  items.py).
- AddCreature / AddOutfit (protocolgame.cpp:9537 / 9926) — creature encoding for
  the cipsoft860 profile.

THE RUN-LENGTH ENCODING (how tiles are packed)
The server walks the viewport in (floor, column, row) order with a single shared
`skip` counter, and emits, per non-empty tile, the tile's things followed by a
"skip marker": the two bytes `[skipCount][0xFF]`, i.e. a little-endian u16 >=
0xFF00 whose low byte says how many *empty* tiles follow before the next one.
Because item client-ids start at 100 and creature markers are 0x61/0x62/0x63
(97/98/99), a leading u16 unambiguously classifies each thing:

    u16 >= 0xFF00        -> skip marker: this tile has no more things; skip N
    u16 in 0x61/0x62/63  -> a creature (marker), fields follow
    u16 >= 100           -> an item, id == that u16, then item.py count bytes

So the reader is: for each tile slot, if we owe skips, consume one; otherwise read
things until we hit a skip marker, which also tells us the next run of empties.
"""

from __future__ import annotations

import dataclasses
import logging

from .items import ItemFlags
from .wire import MessageReader

log = logging.getLogger("antbot")

# --- Server -> client opcodes we understand so far -------------------------
OP_SELF_APPEAR = 0x0A       # "you are this creature": player id + login metadata
OP_MAP_DESCRIPTION = 0x64   # full map snapshot, prefixed with our position

# Canary sends SERVER_BEAT (fixed 50) in the self-appear packet. We read it as an
# alignment self-check: a different value means our offsets are wrong.
EXPECTED_SERVER_BEAT = 50

# Viewport the server sends (sendMapDescription):
#   width  = (MAP_MAX_CLIENT_VIEW_PORT_X + 1) * 2 = (8+1)*2 = 18
#   height = (MAP_MAX_CLIENT_VIEW_PORT_Y + 1) * 2 = (6+1)*2 = 14
# The player sits at the centre of this window, at local index (8, 6).
VIEWPORT_WIDTH = 18
VIEWPORT_HEIGHT = 14
CENTER_NX = 8
CENTER_NY = 6

# Floor constants (GetMapDescription).
SURFACE_LAYER = 7      # MAP_INIT_SURFACE_LAYER: ground level
LAYER_VIEW_LIMIT = 2   # underground you see +/- 2 floors
MAX_LAYERS = 16        # floors 0..15

# Creature "thing" markers inside a tile.
CREATURE_UNKNOWN = 0x61  # full creature data follows (first time we see it)
CREATURE_KNOWN = 0x62    # creature we've been told about before: id only
CREATURE_TURN = 0x63     # direction-only update (not seen in the login map)

# A leading u16 >= this is a skip marker, not a thing.
SKIP_MARKER_BASE = 0xFF00


@dataclasses.dataclass
class Position:
    """A tile coordinate. x/y ~32000 near the classic map centre; z is the floor
    (7 = ground, 0..6 above, 8..15 underground)."""

    x: int
    y: int
    z: int

    def __str__(self) -> str:
        return f"({self.x}, {self.y}, {self.z})"


@dataclasses.dataclass
class Creature:
    """A creature the server has described to us.

    For the 8.60 cipsoft profile the appear packet gives us a name but no explicit
    monster/npc/player type byte, so `name` is our main identifier beyond the id.
    `position` is where we last saw it in the map/updates.
    """

    creature_id: int
    name: str
    health_percent: int
    direction: int
    position: Position | None = None

    @property
    def kind(self) -> str:
        """'player' | 'monster' | 'npc', from the creature id RANGE.

        8.60 sends no type byte, but Canary hands out ids from fixed, non-overlapping
        bands (src/creatures/*.cpp): players 0x10000000–0x50000000, monsters from
        0x50000001, NPCs from 0x80000000. So the id alone classifies exactly — no
        datapack name-matching needed. This is what lets the map tell a wandering
        skunk (monster) from a stationary shopkeeper (npc), which matters because they
        want completely different memory treatment (see BACKLOG: creature memory).
        """
        if self.creature_id >= 0x80000000:
            return "npc"
        if self.creature_id >= 0x50000000:
            return "monster"
        return "player"


@dataclasses.dataclass
class GameState:
    """Everything the bot currently believes about itself and the world.

    Grows each phase. A1: player id + position. A2/A3: known creatures and a
    sparse tile grid (occupied tiles only; empty tiles are simply absent).
    """

    player_id: int | None = None
    position: Position | None = None
    snapshot_parsed: bool = False
    # Our own vitals, from the player-stats packet (0xA0). None until the first
    # stats packet arrives (shortly after login). Used for the dashboard HP display.
    hp: int | None = None
    max_hp: int | None = None
    mana: int | None = None
    max_mana: int | None = None
    level: int | None = None
    # FREE carry capacity (weight we can still take on), straight from 0xA0. One of the
    # hauler's two budgets; the server already accounts for vocation/level, so we don't.
    capacity: int | None = None
    # creature_id -> Creature. Includes ourselves (filter with player_id).
    creatures: dict[int, Creature] = dataclasses.field(default_factory=dict)
    # Health of our creature tracking, for diagnostics. A 0x6D move names no creature id
    # (only old/new position), so we can only identify the mover by where it stood — see
    # _relocate_creature. `creature_move_misses` counts moves we could not attribute to a
    # creature we know about (that creature is now frozen at a stale tile in our model);
    # `creature_ghosts_dropped` counts stale records we cleared off a destination tile.
    # A steadily climbing miss count means our view of monsters is drifting.
    creature_move_misses: int = 0
    creature_ghosts_dropped: int = 0
    # (x, y, z) -> list of item ids on that tile, ground first. Sparse: only
    # tiles that actually had contents appear here.
    tiles: dict[tuple[int, int, int], list[int]] = dataclasses.field(default_factory=dict)
    # Every tile slot the server has DESCRIBED to us, whether it had contents or was
    # empty. This is what distinguishes "never seen" (a real unexplored frontier) from
    # "seen but empty" (open air over a cliff, void beyond a ledge, a gap we're looking
    # down through). `tiles` only holds non-empty tiles, so on its own it can't tell the
    # two apart — a scout would chase a cliff edge forever, treating the open air past it
    # as unexplored. The frontier finder uses THIS set instead. Grows with the explored
    # area (like `tiles`); pruning is future work (see BACKLOG).
    seen: set[tuple[int, int, int]] = dataclasses.field(default_factory=set)
    # Slots added to `seen` since the colony last drained this (a cheap delta). The
    # colony maintains a SHARED seen set + frontier set (see Colony.contribute_tiles);
    # handing it only the new slots each report keeps that O(new tiles), not O(whole map).
    newly_seen: list[tuple[int, int, int]] = dataclasses.field(default_factory=list)
    # Tiles the server refused to let us walk onto even though our map thought
    # they were walkable (an unflagged blocker, a creature, a zone edge, ...).
    # The pathfinder treats these as blocked so it routes around them instead of
    # retrying a doomed step forever. Discovered at runtime as we bump into them.
    blocked_tiles: set[tuple[int, int, int]] = dataclasses.field(default_factory=set)
    # When each locally-rejected block EXPIRES (monotonic seconds). Rejections are
    # often transient — another creature or a clustered bot standing in the way — so
    # a block that isn't a real wall must time out, or bots slowly wall themselves
    # in with false blocks and get stuck. A real wall simply gets re-blocked next
    # time we bump it. Colony hazards (teleporters) are added WITHOUT an expiry entry
    # and so are never pruned. Keyed the same as blocked_tiles.
    blocked_expiry: dict[tuple[int, int, int], float] = dataclasses.field(default_factory=dict)
    # What we're wearing/wielding: worn slot (1-10, see SLOT_* below) -> (item_id,
    # count). Filled from the 0x78/0x79 inventory packets. Backpack CONTENTS (tools,
    # keys, potions) live in `containers`, not here — this is only the 10 equipment
    # slots. Used so a bot knows whether it carries a shovel / rope / key / potion.
    inventory: dict[int, tuple[int, int]] = dataclasses.field(default_factory=dict)
    # Open containers: container_id (0-based, as the server assigns on open) -> list
    # of (item_id, count), index 0 = top slot. The backpack auto-opens at login.
    containers: dict[int, list[tuple[int, int]]] = dataclasses.field(default_factory=dict)
    # container_id -> how many slots it holds (from the same 0x6E that lists contents).
    # free slots = capacity - len(contents); see carry.py. Kept alongside `containers`
    # rather than inside it so the (item_id, count) shape everything already reads
    # stays exactly as it is.
    container_caps: dict[int, int] = dataclasses.field(default_factory=dict)

    def nearby_creatures(self) -> list[Creature]:
        """Creatures other than ourselves."""
        return [c for c in self.creatures.values() if c.creature_id != self.player_id]

    def carries(self, *item_ids: int) -> tuple[tuple[int, int, int], int] | None:
        """Find the first of `item_ids` we carry: `(position, item_id)` or None.

        `position` is what the use-with / move protocols expect: `(0xFFFF, slot, 0)`
        for a worn item, or `(0xFFFF, 0x40|container_id, slot_index)` for a container
        item (mirrors client.inventory_pos / container_pos; its 3rd element doubles as
        the stackpos). Searches worn slots first, then open containers. Lets the
        executor point a shovel/rope/key at a tile without hard-coding where it sits.
        """
        wanted = set(item_ids)
        for slot, (item_id, _count) in self.inventory.items():
            if item_id in wanted:
                return ((0xFFFF, slot, 0), item_id)
        for cid, items in self.containers.items():
            for idx, (item_id, _count) in enumerate(items):
                if item_id in wanted:
                    return ((0xFFFF, 0x40 | cid, idx), item_id)
        return None


# ---------------------------------------------------------------------------
# Piece parsers (each consumes exactly its bytes from the shared reader)
# ---------------------------------------------------------------------------

def _parse_outfit(reader: MessageReader) -> None:
    """Consume an outfit (AddOutfit, 8.60: no mount because 860 < 870)."""
    look_type = reader.u16()
    if look_type != 0:
        reader.u8()  # head
        reader.u8()  # body
        reader.u8()  # legs
        reader.u8()  # feet
        reader.u8()  # addons
    else:
        reader.u16()  # lookTypeEx (an item id worn as an outfit)


def _parse_creature(reader: MessageReader, marker: int, state: GameState) -> Creature:
    """Parse a creature thing whose leading marker was already read.

    Layout for the cipsoft860 profile (AddCreature, protocolgame.cpp:9537):
      known (0x62):   u32 id
      unknown (0x61): u32 removeKnownId, u32 id, string name
      then both:      u8 health%, u8 direction, <outfit>, u8 lightLevel,
                      u8 lightColor, u16 stepSpeed, u8 skull, u8 partyShield,
      unknown only:   u8 guildEmblem
      then both:      u8 walkThrough
    """
    if marker == CREATURE_KNOWN:
        creature_id = reader.u32()
        # We were told this creature's details earlier; reuse the cached name.
        name = state.creatures[creature_id].name if creature_id in state.creatures else ""
    elif marker == CREATURE_UNKNOWN:
        reader.u32()  # removeKnownId: an id the server is evicting from its known
        # set to make room. We don't cap our own set, so we can ignore it.
        creature_id = reader.u32()
        name = reader.string()
    elif marker == CREATURE_TURN:
        # Direction-only update: u32 id, u8 direction. Does not occur in the login
        # map, but handled so update packets don't desync us later.
        creature_id = reader.u32()
        direction = reader.u8()
        creature = state.creatures.get(creature_id)
        if creature:
            creature.direction = direction
        return creature or Creature(creature_id, "", 0, direction)
    else:
        raise ValueError(f"not a creature marker: {marker:#06x}")

    health = reader.u8()
    direction = reader.u8()
    _parse_outfit(reader)
    reader.u8()   # light level
    reader.u8()   # light color
    reader.u16()  # step speed
    reader.u8()   # skull
    reader.u8()   # party shield
    if marker == CREATURE_UNKNOWN:
        reader.u8()  # guild emblem
    reader.u8()   # walk-through flag

    creature = Creature(creature_id, name, health, direction)
    state.creatures[creature_id] = creature
    return creature


def _parse_tile(reader: MessageReader, item_flags: ItemFlags, pos: Position,
                state: GameState) -> int:
    """Read one tile's things; return the trailing skip count.

    Reads things until a skip marker (u16 >= 0xFF00) ends the tile. Items are
    recorded into state.tiles as (id, count); creatures into state.creatures.
    """
    items: list[tuple[int, int]] = []
    while True:
        value = reader.u16()
        if value >= SKIP_MARKER_BASE:
            # End of this tile. The low byte is how many empty tiles follow.
            if items:
                state.tiles[(pos.x, pos.y, pos.z)] = items
            return value & 0xFF
        if value in (CREATURE_UNKNOWN, CREATURE_KNOWN, CREATURE_TURN):
            creature = _parse_creature(reader, value, state)
            creature.position = Position(pos.x, pos.y, pos.z)
        else:
            # `value` is the item's client id, followed by its conditional count byte.
            # We KEEP the count: a pile of 100 gold and a single coin share an id, and a
            # hauler has to tell them apart to price the pile.
            items.append((value, _read_count(reader, item_flags, value)))


def parse_map_area(reader: MessageReader, item_flags: ItemFlags,
                   base_x: int, base_y: int, base_z: int,
                   width: int, height: int, state: GameState) -> None:
    """Parse a rectangular map region using the skip run-length encoding.

    This is the shared core behind both the full login map (18x14, centred on us)
    and the narrow edge strips sent when we walk (an 18x1 row or a 1x14 column).
    `base_x`/`base_y`/`base_z` are the GetMapDescription arguments: the top-left of
    the region on floor `base_z`. It walks floors then columns then rows with one
    shared skip counter, exactly like GetMapDescription/GetFloorDescription. For
    each floor `nz`, tiles are offset by (base_z - nz) to model the diagonal
    perspective of higher/lower floors.
    """
    if base_z > SURFACE_LAYER:
        start_z, end_z, z_step = base_z - LAYER_VIEW_LIMIT, min(MAX_LAYERS - 1, base_z + LAYER_VIEW_LIMIT), 1
    else:
        start_z, end_z, z_step = SURFACE_LAYER, 0, -1

    skip = 0
    nz = start_z
    while True:
        offset = base_z - nz
        for nx in range(width):
            for ny in range(height):
                tile_pos = Position(base_x + nx + offset, base_y + ny + offset, nz)
                # Remember EVERY slot the server described — empty ones included. This is
                # how we later tell open air / cliff void (seen but empty) from genuinely
                # unexplored space (never described). See GameState.seen. Newly-seen slots
                # also go on `newly_seen` so the colony can update its shared frontier set
                # from just the delta.
                slot = (tile_pos.x, tile_pos.y, tile_pos.z)
                if slot not in state.seen:
                    state.seen.add(slot)
                    state.newly_seen.append(slot)
                if skip > 0:
                    skip -= 1  # this slot is a known-empty tile (void / gap / plain empty)
                    continue
                skip = _parse_tile(reader, item_flags, tile_pos, state)
        if nz == end_z:
            break
        nz += z_step


def parse_map_description(reader: MessageReader, item_flags: ItemFlags,
                          center: Position, state: GameState) -> None:
    """Parse the full 18x14 login map, centred on `center`."""
    parse_map_area(reader, item_flags,
                   center.x - CENTER_NX, center.y - CENTER_NY, center.z,
                   VIEWPORT_WIDTH, VIEWPORT_HEIGHT, state)


# ---------------------------------------------------------------------------
# Live updates while walking (B2)
# ---------------------------------------------------------------------------

# Opcodes we act on in ongoing frames.
OP_MOVE_CREATURE = 0x6D   # a creature (maybe us) moved: oldPos, oldStackpos, newPos
OP_SLICE_NORTH = 0x65     # new row revealed on the north edge after we moved
OP_SLICE_EAST = 0x66      # new column on the east edge
OP_SLICE_SOUTH = 0x67     # new row on the south edge
OP_SLICE_WEST = 0x68      # new column on the west edge
OP_ADD_THING = 0x6A       # a thing appeared: pos, stackpos, thing
OP_REMOVE_THING = 0x6C    # a thing was removed: pos, stackpos
OP_MAGIC_EFFECT = 0x83    # pos + effect (u16 for our profile); we just skip it
OP_CANCEL_WALK = 0xB5     # server rejected our walk: direction byte
OP_PING = 0x1E            # keepalive ping (no payload); pong is 0x1D
OP_PONG = 0x1D
OP_TEXT_MESSAGE = 0xB4    # type:u8 + string (for the info/status texts we see)
OP_CREATURE_SAY = 0xAA    # a creature/NPC/monster spoke — batched near our moves
OP_PLAYER_STATS = 0xA0    # our own vitals (health/mana/level/...) — see AddPlayerStats
OP_PLAYER_SKILLS = 0xA1   # our 7 skills, (level u8, percent u8) each — 14 bytes
OP_CANCEL_TARGET = 0xA3   # server cleared our attack target (u32); fires on every kill


@dataclasses.dataclass
class FrameResult:
    """What a single ongoing frame told us that the mover cares about."""

    moved: bool = False              # our own position changed this frame
    new_position: Position | None = None
    rejected: bool = False           # the server cancelled our walk (0xB5)
    # The server cleared our attack target (0xA3) — normally because it died. The client
    # mirrors this into `session.attack_target` so our idea of who we're hitting can't go
    # stale and suppress a re-target (see _engage_adjacent).
    target_cancelled: bool = False
    bailed_opcode: int | None = None  # first opcode we didn't understand (if any)
    # Byte offset of that opcode inside the frame. A bail ABANDONS the rest of a batched
    # frame, so knowing where it happened lets us dump the tail and work out the missing
    # message's real length instead of guessing it (a wrong length desyncs everything
    # after it, which is strictly worse than bailing).
    bailed_offset: int | None = None
    # (class, text) status lines the server sent this frame — see OP_TEXT_MESSAGE.
    messages: list[tuple[int, str]] = dataclasses.field(default_factory=list)


def _parse_thing(reader: MessageReader, item_flags: ItemFlags, pos: Position,
                 state: GameState) -> None:
    """Read a single thing (creature or item) at `pos` — used by 0x6A add-thing."""
    value = reader.u16()
    if value in (CREATURE_UNKNOWN, CREATURE_KNOWN, CREATURE_TURN):
        creature = _parse_creature(reader, value, state)
        creature.position = Position(pos.x, pos.y, pos.z)
    else:
        count = _read_count(reader, item_flags, value)
        state.tiles.setdefault((pos.x, pos.y, pos.z), []).append((value, count))


def _read_count(reader: MessageReader, item_flags: ItemFlags, item_id: int) -> int:
    """Read the conditional count/subtype byte(s) that FOLLOW an already-read item id.

    Stackable/liquid items carry extra byte(s); the first is the count (or fluid type).
    Used wherever we've consumed the id already (tiles, add-thing) and still owe the
    reader those bytes — returning the count instead of discarding it, so a stack's
    size survives into state.tiles.
    """
    count = 1
    extra = item_flags.extra_bytes(item_id)
    if extra:
        count = reader.u8()
        for _ in range(extra - 1):
            reader.u8()
    return count


def _read_item(reader: MessageReader, item_flags: ItemFlags) -> tuple[int, int]:
    """Read one AddItem (u16 id + conditional count/subtype byte) -> (id, count).

    Mirrors the server's AddItem, used by the container packets.
    """
    item_id = reader.u16()
    return item_id, _read_count(reader, item_flags, item_id)


def tile_ids(items) -> list[int]:
    """Just the ids from a tile's [(id, count), ...].

    For the consumers that care about item IDENTITY only — the traversal registry and
    the minimap colouring — so they keep their simple id-list API.
    """
    return [iid for iid, _ in items]


def _parse_slice(reader: MessageReader, item_flags: ItemFlags, opcode: int,
                 old: Position | None, new: Position | None, state: GameState) -> None:
    """Parse an edge strip (0x65-0x68) revealed by our own move.

    Origins/dimensions mirror sendMoveCreature (protocolgame.cpp:8715-8728). N/S
    strips are 18 wide x 1 tall; E/W strips are 1 wide x 14 tall; each spans all
    visible floors. Needs the old/new positions from the 0x6D that preceded it.
    """
    if new is None or old is None:
        raise ValueError("map slice without a preceding move")
    if opcode == OP_SLICE_NORTH:
        parse_map_area(reader, item_flags, old.x - CENTER_NX, new.y - CENTER_NY, new.z, VIEWPORT_WIDTH, 1, state)
    elif opcode == OP_SLICE_SOUTH:
        parse_map_area(reader, item_flags, old.x - CENTER_NX, new.y + CENTER_NY + 1, new.z, VIEWPORT_WIDTH, 1, state)
    elif opcode == OP_SLICE_EAST:
        parse_map_area(reader, item_flags, new.x + CENTER_NX + 1, new.y - CENTER_NY, new.z, 1, VIEWPORT_HEIGHT, state)
    elif opcode == OP_SLICE_WEST:
        parse_map_area(reader, item_flags, new.x - CENTER_NX, new.y - CENTER_NY, new.z, 1, VIEWPORT_HEIGHT, state)


def _clear_transient_block(state: GameState, tile: tuple[int, int, int]) -> bool:
    """Drop a RUNTIME block on `tile` if one is set. True if we cleared something.

    Called when a creature vacates a tile (moves off it or dies/leaves view on it). A
    tile a creature was standing on is, by definition, walkable ground — a creature can't
    occupy a wall — so any block sitting on it was only ever the creature getting in our
    way (a rejected/ignored step while it stood there). Once it's gone, that block is a
    lie, and waiting out the 12s TTL just leaves us pathing around empty floor and, at
    worst, walling ourselves into the corner the creature had us pinned in.

    We clear ONLY transient blocks — those with a `blocked_expiry` entry. Colony hazards
    (teleport/stair sources) are added to `blocked_tiles` WITHOUT an expiry precisely so
    they're never auto-pruned; a creature standing on a teleport must not let us clear the
    avoid and walk into it. Keying off the expiry map keeps hazards untouched.
    """
    if tile in state.blocked_expiry:
        state.blocked_tiles.discard(tile)
        del state.blocked_expiry[tile]
        return True
    return False


def _relocate_creature(state: GameState, old: Position, new: Position) -> bool:
    """A non-us creature moved from `old` to `new`; update its record. True if we found it.

    The awkward constraint: a 0x6D carries NO creature id — just (oldPos, oldStackpos,
    newPos) — so where a creature stood is the only handle we have for deciding who moved.
    That makes this inherently best-effort, and the job here is to stop ONE bad guess from
    snowballing into a permanently drifting picture of the monsters around us:

      - Never match OURSELVES. Our position comes from the slice-confirmed 0x6D (the one
        authoritative source, see the OP_SLICE branch); letting a bare creature-move drag
        our own record around corrupts the very thing everything else trusts.
      - Clear ghosts off the DESTINATION. Two creatures cannot share a tile, so anything
        still recorded on `new` is a leftover from an update we missed. Left alone it would
        absorb the next move aimed at that tile, and the error compounds silently. We only
        do this once we've positively identified the mover, so a miss can't make us delete
        a record that might still be good.
      - Count what we could not attribute, instead of returning silently. A miss means some
        creature is now frozen at a stale tile in our model; without a counter that damage
        is invisible, which is exactly how ragged monster tracking went unnoticed.
    """
    src = (old.x, old.y, old.z)
    dst = (new.x, new.y, new.z)

    mover = None
    for creature in state.creatures.values():
        if creature.creature_id == state.player_id:
            continue                      # our own position is not theirs to move
        pos = creature.position
        if pos is not None and (pos.x, pos.y, pos.z) == src:
            mover = creature
            break

    if mover is None:
        # Something moved onto `dst` that we don't have tracked. We deliberately do NOT
        # evict whatever we think is there: without having identified the mover we can't
        # tell a ghost from a real creature whose position we simply got wrong, and
        # dropping a live monster is worse than holding a stale one (it would stop
        # blocking pathfinding and stop being a melee target).
        state.creature_move_misses += 1
        return False

    for cid in [c.creature_id for c in state.creatures.values()
                if c is not mover and c.creature_id != state.player_id
                and c.position is not None
                and (c.position.x, c.position.y, c.position.z) == dst]:
        del state.creatures[cid]
        state.creature_ghosts_dropped += 1

    mover.position = Position(new.x, new.y, new.z)
    # The creature just vacated `src`. If we'd blocked that tile because it was standing
    # there in our way, free it now instead of pathing around empty floor for 12s.
    _clear_transient_block(state, src)
    return True


def _prune_distant_creatures(state: GameState) -> None:
    """Drop creatures that have scrolled outside the current viewport.

    When we move, the server sends 0x6C for creatures leaving view, but keeping a
    growing memory-map means stale creatures could otherwise linger and wrongly
    block pathfinding. This bounds the creature set to what we can currently see.
    """
    if state.position is None:
        return
    px, py = state.position.x, state.position.y
    for cid in [c.creature_id for c in state.creatures.values()
                if c.creature_id != state.player_id and c.position is not None
                and (abs(c.position.x - px) > CENTER_NX + 1 or abs(c.position.y - py) > CENTER_NY + 1)]:
        del state.creatures[cid]


def process_world_frame(payload: bytes, state: GameState, item_flags: ItemFlags) -> FrameResult:
    """Apply an ongoing (post-login) frame to `state`, best-effort.

    Parses opcodes from the start of the frame. Movement-critical packets (our
    move + the edge slice) always lead a walk-response frame, so we read those
    reliably. On the first opcode we don't have a handler for, we stop parsing
    this frame — the outer TCP framing means we resync cleanly at the next frame,
    so an unhandled opcode costs us at most this frame's remaining updates, never
    stream alignment.
    """
    reader = MessageReader(payload)
    result = FrameResult()
    move_old: Position | None = None
    move_new: Position | None = None
    # A 0x6D move whose us-or-creature identity we haven't resolved yet. Our OWN move is
    # always immediately followed by an edge slice (the server's sendMoveCreature player
    # branch appends 0x65-0x68 for any x/y change); another creature's move is a BARE
    # 0x6D with no slice. So we can't decide at the 0x6D itself — we wait to see whether a
    # slice follows. Until then the move is "pending"; a slice claims it as ours, anything
    # else resolves it as a creature. (Verified against Canary protocolgame.cpp.)
    pending_move: tuple[Position, Position] | None = None

    while not reader.eof():
        opcode = reader.u8()
        if opcode == OP_MAP_DESCRIPTION:
            # A full map re-send mid-session means we were relocated — this is how
            # the server delivers a TELEPORT (it sends 0x6C to remove us, then this
            # 0x64 centred on the destination), not a 0x6D move. Read the new
            # position, adopt it, and parse the fresh surroundings. _walk_local sees
            # that we didn't land where a step should and flags the tile a hazard.
            new = Position(reader.u16(), reader.u16(), reader.u8())
            state.position = new
            result.moved = True
            result.new_position = new
            parse_map_description(reader, item_flags, new, state)
        elif opcode == OP_MOVE_CREATURE:
            old = Position(reader.u16(), reader.u16(), reader.u8())
            reader.u8()  # old stack position
            new = Position(reader.u16(), reader.u16(), reader.u8())
            move_old, move_new = old, new
            # A previous unresolved 0x6D with no slice after it was a creature's move.
            if pending_move is not None:
                _relocate_creature(state, *pending_move)
            # Defer THIS one until we know whether a slice follows (see pending_move).
            pending_move = (old, new)
        elif opcode in (OP_SLICE_NORTH, OP_SLICE_EAST, OP_SLICE_SOUTH, OP_SLICE_WEST):
            _parse_slice(reader, item_flags, opcode, move_old, move_new, state)
            # A slice is sent ONLY for OUR movement, so the 0x6D it follows was ours and
            # `move_new` is our TRUE position — the single source of truth for where we
            # are. Old code guessed us-vs-creature by matching the 0x6D's `old` against our
            # believed position; that LATCHES both ways — our real move misread as a
            # creature's freezes us, and a creature's bare move whose `old` happens to
            # match our (already drifted) position drags us along the creature's path,
            # revealing no slices so those rows stay `unseen` and read as phantom frontiers
            # (the "pace the edge" bug). Trusting the slice removes both.
            if move_new is not None:
                here = (state.position.x, state.position.y, state.position.z) if state.position else None
                cur = (move_new.x, move_new.y, move_new.z)
                # A synced move steps from where we believed we were; anything else means
                # our position had drifted and we're re-adopting the truth.
                if here is not None and move_old is not None \
                        and (move_old.x, move_old.y, move_old.z) != here:
                    log.warning("move: RESYNC via slice — believed %s, server moved us %s -> %s",
                                state.position, move_old, move_new)
                state.position = move_new
                result.moved = True
                result.new_position = move_new
            pending_move = None        # the slice claimed this move as ours
        elif opcode == OP_ADD_THING:
            pos = Position(reader.u16(), reader.u16(), reader.u8())
            reader.u8()  # stack position
            _parse_thing(reader, item_flags, pos, state)
        elif opcode == OP_REMOVE_THING:
            pos = Position(reader.u16(), reader.u16(), reader.u8())
            reader.u8()  # stack position
            removed = [c.creature_id for c in state.creatures.values()
                       if c.position is not None and (c.position.x, c.position.y, c.position.z) == (pos.x, pos.y, pos.z)
                       and c.creature_id != state.player_id]
            for cid in removed:
                del state.creatures[cid]
            # A creature died or left this tile (the common case: WE just killed it). The
            # tile is walkable again, so drop any transient block we'd put there while it
            # was in our way — don't make the bot wait out the TTL on now-empty floor.
            if removed:
                _clear_transient_block(state, (pos.x, pos.y, pos.z))
        elif opcode == OP_MAGIC_EFFECT:
            reader.u16(); reader.u16(); reader.u8()  # position (5 bytes)
            reader.u16()                             # effect id (u16, our profile)
        elif opcode == OP_CANCEL_WALK:
            reader.u8()  # direction to face
            result.rejected = True
        elif opcode in (OP_PING, OP_PONG):
            pass  # keepalive, no payload
        elif opcode == OP_TEXT_MESSAGE:
            # Info/status messages seen while walking are type + string. Combat
            # messages (damage/exp) carry extra value fields, but those use 0x84
            # or arrive during fights; if one slips through and mis-parses, we
            # just bail on the frame's tail and resync next frame.
            cls = reader.u8()     # message class
            text = reader.string()
            # Keep the last few. The server explains every refused action in prose and
            # nowhere else — "You are too far away", "You cannot put that object there",
            # "You do not have enough room" — so this is our only feedback channel for
            # moves that silently do nothing. Bounded so a chatty server can't grow it.
            result.messages.append((cls, text))
        elif opcode == OP_CREATURE_SAY:
            # sendCreatureSay: statementId, name, level, speak type, position,
            # text. Monster/NPC local speech follows this layout (channel chat
            # uses a channelId instead of the position, but that doesn't occur
            # while hunting; if it does, we bail on the tail and resync).
            reader.u32()      # statement id
            reader.string()   # speaker name
            reader.u16()      # speaker level (0 for non-players)
            reader.u8()       # speak type
            reader.u16(); reader.u16(); reader.u8()  # position (5 bytes)
            reader.string()   # spoken text
        elif opcode == 0x78:
            # A worn equipment slot got an item (login, or when we equip something):
            # slot byte, item id, then the usual conditional count/subtype byte for
            # stackable/liquid items. We record it so the bot knows what it wears.
            slot = reader.u8()
            inv_id = reader.u16()
            count = 1
            extra = item_flags.extra_bytes(inv_id)
            if extra:
                count = reader.u8()          # first extra byte is count (or fluid type)
                for _ in range(extra - 1):
                    reader.u8()
            state.inventory[slot] = (inv_id, count)
        elif opcode == 0x79:
            state.inventory.pop(reader.u8(), None)   # a worn slot emptied
        elif opcode == 0x6E:
            # A container opened (the backpack auto-opens at login). Record its
            # contents so the bot knows what tools/keys/potions it carries. Old-
            # protocol layout: cid, the container's own item, name, capacity,
            # hasParent, item count, then that many items.
            cid = reader.u8()
            _read_item(reader, item_flags)   # the container item itself
            reader.string()                  # name
            # SLOT capacity — how many stacks this bag holds. The hauler's second
            # budget: free slots = capacity - len(contents). Stackables are cheap here
            # because topping up an existing stack needs no new slot.
            state.container_caps[cid] = reader.u8()
            reader.u8()                      # has-parent flag
            count = reader.u8()              # number of items that follow
            state.containers[cid] = [_read_item(reader, item_flags) for _ in range(count)]
        elif opcode == 0x86:
            # Creature shield/party marker (cipsoft860: u32 creature id + u8 colour).
            # Harmless to us, but it's BATCHED ahead of the inventory packets, so we
            # must consume it or we'd bail and never see our inventory.
            reader.u32()
            reader.u8()
        elif opcode == 0x92:
            # Creature walk-through flag (sendCreatureWalkthrough: u32 id + u8 bool).
            # We don't use it, but it's a per-creature update that the server can BATCH
            # AHEAD of our own move+slice in one network frame — so bailing on it would
            # drop our slice, drift us a tile and leave that row `unseen` (a phantom
            # frontier the scout then paces). Consuming its 5 bytes keeps us aligned so
            # the move+slice behind it still parse. (The move-attribution fix already
            # self-heals such drift; this removes the source.)
            reader.u32()
            reader.u8()
        # --- Creature/world property updates we don't use, but MUST consume ----------
        # These fire constantly around movement (a nearby creature turns, changes speed,
        # gains a skull; the day/night light ticks; we cross a protection-zone edge) and
        # the server batches several game messages per network frame. Bailing on any one
        # abandons the rest of THAT frame — including our own move+slice if it was batched
        # behind it — which drops a map row and strands the scout pacing a phantom
        # frontier. A real client parses every opcode for exactly this reason; so do we.
        # Byte layouts read straight from Canary protocolgame.cpp for our profile
        # (oldProtocol=true, version=860); the conditional fields those functions guard
        # behind version>=953 / version>=1059 are therefore ABSENT here.
        elif opcode == 0x82:
            # AddWorldLight: level u8 + colour u8. Global day/night tick — batches widely.
            reader.u8(); reader.u8()
        elif opcode == 0x6B:
            # 0x6B is TWO different messages sharing one opcode, and the u16 after the
            # stackpos is what tells them apart:
            #   marker == 0x0063 -> sendCreatureTurn: + creature id u32 + direction u8
            #   otherwise        -> sendUpdateTileItem: that u16 IS the item id
            #
            # We used to always assume the creature-turn form and consume 13 payload
            # bytes. On a tile-item update the real payload is 8, so we over-read by 5 and
            # landed mid-message — and since a desync makes the next byte read look like a
            # garbage opcode, we bailed and threw away the REST of that batched frame,
            # creature-moves included. Captured off the wire:
            #   6b | 85 7e 39 7e 07 | 04 | 50 17 | 6c ...
            # marker 0x1750 = an item id, and the next real opcode (0x6C) sits exactly 9
            # bytes in — the 5 bytes we were overshooting by.
            reader.u16(); reader.u16(); reader.u8()   # position
            reader.u8()                                # stack position
            marker = reader.u16()
            if marker == 0x0063:
                reader.u32()                           # creature id
                reader.u8()                            # direction
            else:
                # A tile item changed. `marker` was its id; stackable/liquid items carry
                # the usual trailing count/subtype byte.
                for _ in range(item_flags.extra_bytes(marker)):
                    reader.u8()
        elif opcode == 0x8F:
            # sendChangeSpeed: id u32 + speed u16. (baseSpeed u16 only for !oldProtocol.)
            reader.u32(); reader.u16()
        elif opcode == 0x8C:
            # sendCreatureHealth: id u32 + health% u8. Fires whenever a creature near us
            # takes damage — heavily batched with movement during a fight, which is when
            # 6 scouts pass through monsters. We also fold the health into the creature so
            # the map's wounded-creature pip stays live.
            cid = reader.u32(); hp = reader.u8()
            cr = state.creatures.get(cid)
            if cr is not None:
                cr.health_percent = hp
        elif opcode == 0x90:
            # sendCreatureSkull: id u32 + skull u8.
            reader.u32(); reader.u8()
        elif opcode == 0xA2:
            # sendIcons (status conditions): a single u16 bitmask for oldProtocol. Fires
            # when we enter/leave a protection zone — i.e. right alongside a move.
            reader.u16()
        elif opcode == 0x84:
            # Animated damage/heal/exp text (sendTextMessage damage variant): position
            # (x u16, y u16, z u8) + colour u8 + value string (u16-length-prefixed). Pure
            # floating combat numbers — everywhere once scouts pass through monsters.
            reader.u16(); reader.u16(); reader.u8()   # position
            reader.u8()                                # colour
            reader.string()                            # value text
        elif opcode == 0x6F:
            state.containers.pop(reader.u8(), None)   # container closed
        elif opcode == 0x70:
            # Item added to an open container at `slot`.
            cid = reader.u8()
            slot = reader.u16()
            item = _read_item(reader, item_flags)
            items = state.containers.setdefault(cid, [])
            items.insert(min(slot, len(items)), item)
        elif opcode == 0x71:
            # Item at `slot` changed (e.g. a stack count).
            cid = reader.u8()
            slot = reader.u16()
            item = _read_item(reader, item_flags)
            items = state.containers.get(cid)
            if items is not None and slot < len(items):
                items[slot] = item
        elif opcode == 0x72:
            # Item removed from `slot`; a trailing u16 (0 = none, else the item that
            # scrolls into view) must be consumed to stay aligned.
            cid = reader.u8()
            slot = reader.u16()
            tail_id = reader.u16()
            if tail_id != 0:
                for _ in range(item_flags.extra_bytes(tail_id)):
                    reader.u8()
            items = state.containers.get(cid)
            if items is not None and slot < len(items):
                items.pop(slot)
        elif opcode == OP_PLAYER_STATS:
            # Our own vitals (AddPlayerStats, cipsoft860 layout). We keep hp/mana/
            # level for the dashboard; the rest is consumed to stay aligned.
            state.hp = reader.u16()
            state.max_hp = reader.u16()
            # FREE capacity — how much more weight we can carry right now. One of the
            # two budgets that bound a hauler's load (the other is container slots).
            # It already varies by vocation/level, so we never have to model that.
            state.capacity = reader.u32()
            reader.u32()       # experience
            state.level = reader.u16()
            reader.u8()        # level percent
            state.mana = reader.u16()
            state.max_mana = reader.u16()
            reader.u8()        # magic level
            reader.u8()        # magic level percent
            reader.u8()        # soul
            reader.u16()       # stamina minutes
        elif opcode == OP_PLAYER_SKILLS:
            # Our seven classic skills (AddPlayerSkills): fist, club, sword, axe,
            # distance, shielding, fishing — each a (level u8, percent-to-next u8) pair,
            # so 14 bytes. MEASURED, not assumed: captured frames read
            # `a1 0a 06 0a 00 0a 00 0a 00 0a 00 0a 00 0a 00` and the very next byte is
            # 0xA0, the player-stats packet the server sends alongside — which lands
            # exactly at +14. We don't use skills yet; the point is simply not to abandon
            # the rest of the frame (see the `else` branch below for why that hurts).
            reader.skip(14)
        elif opcode == OP_CANCEL_TARGET:
            # The server clearing our attack target — it died, or left our view. Captured
            # frames are exactly `a3 00 00 00 00`, i.e. a single u32 (always 0 here).
            #
            # This one bit us specifically: bots now melee, so 0xA3 fires on every kill,
            # and bailing on it threw away everything batched BEHIND it in the same frame
            # — including the 0x6D creature-moves. That is why monsters appeared to freeze
            # or skip around. Consuming it keeps those moves.
            reader.u32()
            result.target_cancelled = True
        else:
            # No handler: stop here and let the next frame resync us. Record WHERE, so the
            # frame tail can be dumped and the message's true length derived empirically.
            result.bailed_opcode = opcode
            result.bailed_offset = reader.pos - 1     # the opcode byte itself
            break

    # A 0x6D still pending at frame end had no slice after it -> a creature's move.
    if pending_move is not None:
        _relocate_creature(state, *pending_move)

    if result.moved:
        _prune_distant_creatures(state)
    return result


# ---------------------------------------------------------------------------
# Login snapshot (self-appear + full map)
# ---------------------------------------------------------------------------

def parse_login_snapshot(payload: bytes, state: GameState, item_flags: ItemFlags,
                         *, has_bug_flag: bool = True) -> bool:
    """Parse the first world frame: our id/position, then the whole map.

    Returns True on success, having filled `state` (player_id, position,
    creatures, tiles). Returns False if the bytes did not line up, after logging
    a hex dump for diagnosis.

    Built-in alignment checks (why we can trust the result):
      1. The self-appear `beat` field must read as 50.
      2. GetCipsoft860TileDescription always adds the player creature to the
         player's own tile, so our own player_id MUST turn up as a creature at
         the viewport centre (local 8,6 on the surface floor). If it does, every
         item length before it was correct — a strong end-to-end check.
    """
    reader = MessageReader(payload)

    # --- Packet 1: 0x0A self-appear ---------------------------------------
    opcode = reader.u8()
    if opcode != OP_SELF_APPEAR:
        log.warning("login snapshot: expected self-appear 0x0A, got %#04x", opcode)
        return False
    player_id = reader.u32()
    beat = reader.u16()
    if has_bug_flag:
        reader.u8()  # inline bug-report permission flag
    if beat != EXPECTED_SERVER_BEAT:
        log.warning("login snapshot: beat was %d, expected %d — layout mismatch",
                    beat, EXPECTED_SERVER_BEAT)
        return False

    # --- Packet 2: 0x64 map description -----------------------------------
    opcode = reader.u8()
    if opcode != OP_MAP_DESCRIPTION:
        log.warning("login snapshot: expected map-description 0x64, got %#04x", opcode)
        return False
    position = Position(reader.u16(), reader.u16(), reader.u8())

    state.player_id = player_id
    state.position = position

    # Parse the map grid that follows. Any misjudged item length here throws
    # (over-read) or leaves us misaligned, which the centre-tile check catches.
    try:
        parse_map_description(reader, item_flags, position, state)
    except (IndexError, ValueError) as err:
        log.warning("login snapshot: map parse failed (%s); dumping head:", err)
        from . import wire
        log.warning("\n%s", wire.hexdump(payload, max_bytes=96))
        return False

    # Everything after the map in this first frame — the teleport effect, our
    # inventory, our STATS (hp/mana/level), skills — is what the ongoing-frame
    # parser already knows how to read. Run it over the tail so we pick up our
    # vitals from login (they may never be re-sent while we're at full health).
    map_end = reader.pos
    if map_end < len(payload):
        process_world_frame(payload[map_end:], state, item_flags)

    state.snapshot_parsed = True

    # Alignment check #3 (end-to-end): the login sends a teleport magic-effect
    # (0x83) right after the map. If our parse consumed *exactly* the whole map —
    # every tile and item length, not just up to the centre — the very next byte
    # is that opcode (or the frame ends here and it arrives in the next frame).
    if not reader.eof():
        next_op = reader.u8()
        if next_op == 0x83:
            log.debug("alignment ok: map consumed exactly, next opcode is 0x83 (magic effect)")
        else:
            log.debug("post-map opcode is %#04x (%d bytes remain) — not 0x83; "
                      "map may end at the frame boundary", next_op, reader.remaining())

    # Alignment check #2: did we find ourselves at the centre?
    me = state.creatures.get(player_id)
    if me is None:
        log.warning("login snapshot: parsed map but our own creature (id %d) was "
                    "not found — parse likely misaligned", player_id)
    elif me.position != position:
        log.warning("login snapshot: our creature is at %s but position says %s — "
                    "map parse misaligned", me.position, position)
    else:
        log.debug("alignment ok: found our creature at the viewport centre %s", position)

    return True
