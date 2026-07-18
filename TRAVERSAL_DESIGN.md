# Traversal system design — shortcuts, doors, keys, tools

This is the concrete plan for how the colony understands and uses every kind of
"non-walking" connection: teleporters, stairs, ladders, grates, wells, shovel
holes, rope spots, and doors (locked and unlocked). It supersedes the current
reactive "step on it and see what happens" approach, which only handles stairs
reliably and can't tell a real shortcut from a dead tile.

## Core idea: identify objects by item id, not by trial

Every one of these is a specific game OBJECT with a known item id (usually many
ids per kind — e.g. dozens of shovel-hole graphics). So instead of discovering a
teleporter by randomly walking into it, we look at the item ids on a tile and
classify it — exactly like recognising a door. This gives us:

- **Proactive knowledge** — we know a tile is a teleporter / hole / ladder / door
  *before* stepping on it, from the map data we already parse.
- **No false pruning** — a *closed* shovel hole gives no floor-change signal when
  stepped on, so the current pruner would wrongly discard it. Recognising it by id
  means we never confuse "needs a shovel" with "dead tile."
- **Correct avoidance vs use** — wanderers avoid these tiles; travellers use them.

The one thing item ids *don't* give us is a teleporter's DESTINATION (that's
map-placed server-side, never sent to clients). So teleporter *tiles* are known
from ids; their *destinations* are still learned by traversing once, then cached
as a confirmed link.

## The object registry

`traversal.py` will hold a registry mapping item id -> a `TraversalKind` with:

| field        | meaning                                                            |
|--------------|-------------------------------------------------------------------|
| `category`   | portal / stairs / ladder / grate / well / hole_open / shovel_hole / rope_spot / door / key |
| `action`     | how you traverse it (see below)                                   |
| `direction`  | up / down / teleport / unblock (doors)                            |
| `requires`   | nothing / shovel / rope / rope_or_spell / a specific key id       |

POPULATING IT (revised again 2026-07-13, evening): name-scanning the *client*
`appearances.dat` does NOT work — environmental objects are UNNAMED there and it
carries no floor-change flag. BUT we run the server, and the breakthrough is that
Canary's `data/items/items.xml` is the missing catalog, keyed by the SAME client id
we see on tiles (proven in Canary's `Items::loadFromProtobuf` — it indexes
ItemTypes by the appearance id, then merges items.xml into that array by `id`). So
population is now, in priority order:
  (0) SEEDED from items.xml — implemented (catalog.py + `seed_from_catalog`). The
      file gives per-id `name` + `floorchange` (down/n/s/e/w/alt) + `type`
      (door/teleport/ladder/key/…). `category_from_catalog` maps those to our
      categories, seeding ~1,250 objects at startup: teleporters, stairs/ramps,
      walk-onto holes/trapdoors/descend-ladders (floorchange=down), climb-up ladders
      (type=ladder), and doors. This is GROUND TRUTH, so it's trusted but SOFT.
  (1) LEARNED from behaviour — implemented, and OVERRIDES the seed (the "wiggle
      room"): when a bot steps onto a tile and gets relocated, we record that tile's
      topmost item id (`learn_from_tile`, persisted to `learned_objects.json`). A
      learned entry beats the seeded one, so Tibia's exceptions (a hole that doesn't
      drop you) self-correct without us editing the catalog.
  (2) STILL MISSING — the Lua-scripted use objects: grates, wells, rope spots and
      shovel holes react via `data/scripts/actions/*`, not items.xml (e.g. "sewer
      grate" 435 and "small hole" 387 seed as None). Their ids come next from the
      action scripts (or a trial-use pass). The Look command (server-generated tile
      description by pos+stackpos) stays a possible RUNTIME verification tool for
      dynamic state (is this door locked right now?), but is not needed for static
      identification. The CATEGORY_META table already describes how to traverse each
      once its id is known.

### Categories and how each is traversed

| category      | identify by | action                    | requires        | direction |
|---------------|-------------|---------------------------|-----------------|-----------|
| portal        | item id     | step onto it              | —               | teleport  |
| stairs / ramp | item id     | step onto it              | —               | up/down   |
| ladder        | item id     | **use** it                | —               | up        |
| grate / well  | item id     | **use** it                | —               | down      |
| hole_open     | item id     | step / use                | —               | down      |
| shovel_hole   | item id     | **use shovel** then descend | shovel item   | down      |
| rope_spot     | item id     | **use rope** (or cast Magic Rope) | rope item OR spell+mana | up |
| door (shut)   | item id     | **use** it → opens        | — (unlocked)    | unblock   |
| door (locked) | item id     | **use key** on it → opens | matching key id | unblock   |

`step onto it` = walk a cardinal step onto the tile (already implemented for
stairs/teleports). `use it` / `use X on it` = the "use item" / "use item with
item" protocol (opcodes to be added — see below). `cast` = send a spell say.

## Graph model (feeds nav.find_route)

Today a link is just `source -> dest`. Generalise to a typed EDGE:

    Edge(from_tile, to_tile, kind, requires, action)

- **stairs/ladder/grate/well/hole**: `to_tile` is one floor up/down. For
  step-kinds we know it from the traversal; for use-kinds the landing is learned
  on first use (like teleport dests) and cached.
- **portal**: `to_tile` learned by traversing once, then confirmed.
- **door**: an "unblock" edge — traversing it means "use it (with key if locked)"
  which makes the adjacent tile walkable; then a normal step. Locked doors are
  only usable if the colony has the key id (and a bot is carrying it).

`find_route` already does Dijkstra over walkable tiles + link edges; it extends to
typed edges by (a) only using an edge whose `requires` is satisfiable (bot has the
tool/key/mana), and (b) letting the executor know the `action` for each hop.

## Executor (client.travel)

Per hop, dispatch on `action`:
- `step` — `_raw_step` onto the source (done).
- `use` — walk adjacent, send "use item on tile", confirm the floor change / that
  the door opened.
- `use_tool` — ensure we carry the tool (shovel/rope), use it on the tile, then
  descend/ascend.
- `use_key` — ensure we carry the matching key, use it on the door, then step.
- `cast` — cast Magic Rope (check vocation can cast + mana), then ascend.

## Protocol pieces still to build

1. **Use item on a tile / thing** (`0x82` use, `0x83` use-with in the 8.60 family —
   verify exact opcodes against canary's `parseUseItem` / `parseUseItemEx`).
2. **Inventory / capacity awareness** — parse inventory slots (0x78/0x79) so we
   know if we carry a shovel/rope/key.
3. **Spell casting** — send a say with the spell words; track mana from stats.
4. **Keys** — recognise key items, know which key opens which door (door has an
   action id / key id; may need a hardcoded map or learning by trial).

## Build order

1. `traversal.py` registry + tile classification (proactive id lookup). This
   alone lets us *label* teleporters/holes/ladders/doors on the dashboard and stop
   mis-pruning holes.
2. Wire classification into detection (don't prune known objects) and into
   `find_route` (typed edges, step-kinds first: stairs/ladders/grates that are
   already reachable).
3. Add the "use item" protocol → grates/wells/ladders/open holes/unlocked doors.
4. Add tools + keys (inventory) → shovel holes, rope spots (+ Magic Rope spell),
   locked doors.

Until the registry exists, travel handles stairs/teleports reactively (with the
self-healing prune/confirm from 2026-07-13). The scout role (built next) uses
those reliable shortcuts to map far regions and will feed real destinations into
this system.
