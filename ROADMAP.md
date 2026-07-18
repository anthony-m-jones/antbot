# antbot roadmap — building the colony

This is the agreed plan of attack (2026-07-12). It works backward from a dream
goal to a realistic first destination, then forward through phases that each end
in something you can *see*.

## The dream

Hundreds to thousands of bots cooperating on a shared server:

- One **power-leveling hunter** hunting a fixed spot for maximum exp/hr, never
  leaving.
- **Haulers** carrying its loot to shops and selling it.
- **Suppliers** ferrying potions/runes back and dropping them at the hunter's
  feet so it never has to leave.
- **Explorers** mapping the world, discovering efficient routes, marking
  important places (shops, depots, spawns).
- The colony pursues **common goals** (e.g. "maximize number of high-level
  characters").
- A **monitoring view** like an ant farm: all bots moving on one map at once,
  with the ability to zoom into a single bot and see what it's doing.

## The dream, decomposed into capability stacks

1. **Perception** — a bot knows its position, surrounding tiles, nearby
   creatures. Everything stands on this.
2. **Navigation** — pathfind to a coordinate; follow pre-mapped routes.
3. **Individual competence** — one bot that can hunt: fight, loot, use
   supplies, survive.
4. **Roles & coordination** — hunter / hauler / supplier / explorer sharing
   common state, cooperating toward colony goals.
5. **Scale** — hundreds/thousands of bots in one efficient process.
6. **Observation** — the ant-farm view.

Key insight: stacks 1 and 2 are load-bearing for *everything*. A hauler is
navigation + a few trade packets. An explorer is navigation + memory. Only the
hunter needs real combat logic. So perception and the map are the
highest-leverage work — and they're exactly where the bot is blind today.

## Realistic first destination

> A handful of bots that know where they are, can walk to a named destination on
> purpose, coordinate to avoid clumping, and appear as live dots on a map view
> you can open in a browser.

The smallest thing that already *feels* like an ant colony, with every piece a
direct foundation for the dream. No throwaway work.

## Phases (each ends in something visible)

### Phase A — Sight (perception)  ← IN PROGRESS
Decode the game protocol we currently ignore: the bot's own position (from the
0x0A login payload) and the surrounding map + creatures (from map/tile packets).
Turns blind walking into awareness. Single most important step.

Sub-steps (split by the item-metadata data dependency — see reality #2 in the
options discussion; tile parsing needs the 8.60 item dataset, position/creatures
do not):

- **A1 — instrument + position.** DONE 2026-07-12.
  - `wire.hexdump()` — the capture oracle (raw byte/ASCII dump).
  - `world.py` — `Position`, `GameState`, `parse_login_snapshot()`. Reads the
    0x0A self-appear (player id, beat, bug flag) then the 0x64 map-description
    prefix (x:u16, y:u16, z:u8). Uses beat==50 as an alignment self-check.
  - CLI: `python -m antbot pos ...` (log in, report position, log out) and
    `--dump-frames N -v` to dump raw frames.
  - Verified: parsed positions matched the DB exactly for two characters at
    different coordinates — Knight Noob 1 (1053,1040,7) and Druid Noob 1
    (32369,32241,7); beat alignment check passed; hex dump hand-decodes to the
    same fields.
- **A2+A3 — parse the map (MERGED 2026-07-12).** DONE. Investigation showed
  creatures and tiles are inseparable for our profile: the creatures around us at
  login are embedded *inside* the map's tiles (GetCipsoft860TileDescription),
  interleaved with items whose byte-length needs the item dataset. So we parse
  the map for real.
  - `items.py` — hand-parses Canary's `data/items/appearances.dat` protobuf (no
    dependency) for the stackable/liquid flags that set each item's wire length.
    42,107 objects, ids 100..54266.
  - `world.py` — full 8.60 map decoder: floor loop + skip run-length encoding,
    per-tile items (via item flags) and creatures (0x61/0x62 markers, AddCreature
    fields, AddOutfit). Builds `state.tiles` (sparse grid) and `state.creatures`.
  - CLI: `pos`/`walk` now report "N tiles, M creatures nearby" + each creature's
    name/hp/position.
  - Verified three ways: (1) byte-perfect — parse consumes the whole map and lands
    exactly on the 0x83 teleport effect the server sends next; (2) our own player
    creature is found at the viewport centre; (3) real-world correct — detected
    Quentin at the Thais temple, and 7 creatures across 3 floors at Rookgaard
    (Mountain Trolls, a Salamander, NPCs Inigo/Mr Morris), each with hp% + coords.
  - Bonus: `state.tiles` walkable grid is ready for Phase B pathfinding.

### Phase B — Purposeful movement (navigation)  ← DONE 2026-07-12
A* pathfinding over the walkable tiles the bot can now see; "walk to (x,y)" and
route-following. **Deliverable met:** `antbot goto --x --y` and the bot arrives,
including across screens.

- **B1 — walkability + A*.** DONE 2026-07-12.
  - `items.py` also reads the `unpass` flag (protobuf field 13) -> `is_blocking`.
  - `nav.py` — `is_walkable()` (ground present + no blocking item), A* `find_path`
    returning cardinal step directions, and `render_ascii()` (a text map:
    @ us / . walkable / # blocked / C creature / * path).
  - CLI `path --x --y` plans and prints the route with the map overlaid.
  - Verified on Rook 1: straight-line goal -> "east east east"; a goal past a wall
    -> a 6-step route that visibly threads around the '#' block. Both optimal.
- **B2 — live movement.** DONE 2026-07-12.
  - `world.py` `process_world_frame()` — applies ongoing frames: our move 0x6D
    (updates position), edge slices 0x65-0x68 (grow the map via `parse_map_area`),
    add/remove thing 0x6A/0x6C, magic effect 0x83, walk-cancel 0xB5, ping
    0x1E/0x1D, text 0xB4, creature-say 0xAA. Unknown opcodes bail-and-resync at
    the next frame (framing is external, so this never corrupts the stream).
  - `client.py` `_walk_to()` sends a step, waits for the move/cancel signal (which
    also paces us to walk speed), and re-plans; `goto` command.
  - Debugging note: intermittent position desyncs turned out to be bail-on-unknown
    (0xB4/0x1E/0xAA batched *ahead* of our move 0x6D in one frame) making us miss a
    move; handling those three opcodes fixed it. Verified with many gotos whose
    final position matched the DB exactly.
- **B3 — goto across screens.** DONE 2026-07-12.
  - `nav.py` `find_path_toward()` — BFS flood of the known map heading to the
    reachable tile closest to the goal, so goals beyond the current view work:
    walk to the frontier, slices reveal more, re-plan, repeat.
  - Verified: a goal 8 tiles beyond the view was reached exactly (10 steps through
    a bent corridor, DB-confirmed); genuinely walled directions correctly report
    "stuck" instead of desyncing.
  - Known limits (future): same-floor only (no stairs/z-change yet); doors/teleports
    treated as blocking; combat opcodes (0x8C etc.) not parsed yet (Phase E).

### Phase C — The ant farm (observation) — pulled EARLY  ← DONE 2026-07-12
A local web dashboard: bots report to a coordinator; the browser shows a live map
with bot dots. Built before roles so every later phase is visually debuggable.
**Deliverable met:** open http://127.0.0.1:8100, watch the bot move and the shared
minimap fill in live.

- `items.py` also extracts each item's `automap` colour (flags field 30) →
  authentic Tibia 216-colour minimap; `automap_color_to_rgb()` for the palette.
- `colony.py` — thread-safe `Colony`: live `BotView`s + a shared `explored`
  map `(x,y,z)->minimap colour` that every bot contributes its parsed tiles to
  (the user's shared-minimap idea; see the memory note). `snapshot()` → JSON.
- `dashboard.py` — stdlib `http.server` in a daemon thread; serves an embedded
  HTML/canvas page that polls `/state.json` and draws the minimap + bot dots.
- `client.py` — `explore_session` (wander to reveal map) + `run_explorers`
  (many bots, one colony); `GameSession._report_to_colony` feeds position +
  tiles on every step. `farm` command launches dashboard + bot(s).
- Robustness fixes found by testing: rejected steps mark the tile blocked
  (`state.blocked_tiles`) so we route around unflagged blockers/creatures instead
  of looping; accidental ramp/stairs floor-changes step back to stay on-floor
  (nav is same-floor only); a stuck-round backoff avoids busy-spinning.
- Verified live in a browser: temple rendered as a real minimap (stone/grass/
  water/wood), bot dot roaming z=7, explored tiles climbing continuously
  (1725 → 2292+ during one run).
- **Vitals + issue log + watchdog (DONE 2026-07-13).** Each bot now shows its
  health: we parse the player-stats packet (0xA0), which lives in the login
  frame's *tail* (after the map + inventory), so it needed inventory-slot handlers
  (0x78/0x79) and login-tail processing to reach. hp/mana/level flow to the
  dashboard: a compact colour-coded HP bar in the bot list (always visible) plus a
  detail panel (lvl / hp / mana / pos) for the focused bot. Bots also log
  structured warnings/errors (`Colony.log_event`) into a shared "issues" panel,
  and a per-session watchdog catches stalls (no frames 20s → relog) and freezes
  (no movement 25s → clear stale local blocks + retry). First live run with the
  issue log revealed the real "bots stop moving" cause is mostly the explorer
  giving up after 30 steps on unreachable targets — see BACKLOG.md.
- **Explorer targeting + planner scaling (DONE 2026-07-13).** The wanderer no
  longer walks toward random points (which got cut off by the step budget and
  looked like "stopping"); it heads for the nearest *frontier* tile inside its home
  box — reachable by construction, always revealing new map — and mills only when
  the box is fully mapped. Navigation-warning spam dropped to zero. Separately, the
  per-step planner (`find_path_toward`) grew a `max_radius` bound: since we re-plan
  every step and `state.tiles` never prunes, flooding the whole accumulated map was
  ~55 ms/re-plan on a 48k-tile map; bounding to a 40-tile local box cuts it to
  ~3 ms (16×), the key to running many bots per core. goto/travel stay unbounded
  for exact long-distance routing.
- Next for C/D: multiple bots exploring together (colony already supports it);
  pan/zoom + click-a-bot detail; per-bot tile-map pruning (memory at scale).

### Phase D — Many bots, one brain (coordination & scale)  ← IN PROGRESS
One asyncio process supervising N bots with a shared "blackboard" of colony
state, plus a simple task/assignment system.
**Deliverable:** launch many bots that spread out to explore instead of piling up.

- **Multiple explorer bots.** DONE 2026-07-12. `farm --bots N` runs N bots
  (accounts test1..testN, each its temple "Druid K"), all reporting to one
  Colony; `run_explorers` staggers logins (~2.5s apart, retries with backoff)
  because the server closes connections that arrive too fast from one IP, and
  isolates each bot's failures so one bad login never takes down the farm.
  Verified: 6 bots online at once, dispersed across the temple, underground
  (z=8) and a distant test zone, collaboratively filling a shared map past
  14k tiles, all live on the dashboard.
  - Dashboard now sends a *windowed* snapshot (±130 tiles around the focused bot)
    so far-flung bots don't zoom the view out to nothing.
- **Dashboard focus controls.** DONE 2026-07-12. `colony.snapshot(focus_name)`
  centres the window on any chosen bot (its floor + position); the dashboard adds
  clickable bot rows, canvas dot hit-testing, a "following …" indicator with an
  [auto] reset, and passes `?focus=<name>` to `/state.json`. Verified: following
  Druid 3/4/6 re-centres on each one's own position and floor (e.g. the distant
  ~(1030,1111,z7) zone the lead view never showed). Note: the in-app browser
  *screenshot* tool times out on the continuously-animating canvas here — feature
  verified via live data/`get_page_text`; renders fine in a real browser.
- **Hazard avoidance + correct minimap colours.** DONE 2026-07-12.
  - Minimap now colours a tile by its *topmost blocking* item (walls) then the
    ground, matching the client's `Tile::getMinimapColorByte` (top-down). Walls
    render red again, so the map is recognisable (temple went from a flat grey
    blob to clearly-Thais). `items.py` already had the automap colours.
  - Teleports/stairs/holes can't be spotted ahead of time (modern assets don't
    flag floor-change; teleport dests are map-placed), so bots detect them
    reactively: any step that lands somewhere other than the adjacent tile marks
    that tile a hazard. Crucially, a teleport arrives as a full `0x64` map
    re-send (not a `0x6D` move) — `process_world_frame` now handles mid-session
    `0x64`, so teleports are detected instead of silently desyncing the bot.
  - Hazards are shared across the colony AND persisted to
    `antbot/learned_hazards.json`, so the swarm collectively learns the exits and
    remembers them between runs. Explorers are also bounded to ~22 tiles around
    their spawn.
  - Verified: after one learning run, a fresh launch loaded 15 hazards and all 6
    bots stayed clustered at the temple with zero whisked away.
- **Dashboard control buttons.** DONE 2026-07-12. `farm` is now a persistent
  supervisor (`manager.py` `ColonyManager`) instead of fire-and-forget; the
  dashboard has buttons that drive it: **▶ Log in & explore** (start_all),
  **■ Stop & log out** (stop_all — sets each session's `stop_event` so bots log
  out cleanly), **⟲ Reset to temple** (stop → DB `UPDATE ... temple` → start,
  since a character's position only changes at login). HTTP thread → event loop
  via `run_coroutine_threadsafe`; `/command?action=`. Verified all three live.
- **Teleports as routing shortcuts.** DONE 2026-07-12. Instead of only avoiding
  hazards, bots record the LINK (source→destination) for every relocation
  (`colony.report_link`, persisted in the new `learned_hazards.json` format with
  `hazards` + `links`). The colony also keeps a shared `walkable` graph.
  `nav.find_route` is a teleport-aware Dijkstra (A* heuristic is inadmissible once
  teleports shorten distances) over the whole known world + link edges; a
  `/route?x=&y=&z=` endpoint queries it. Verified: routed to a z15 destination
  500+ tiles away and 8 floors down by chaining a stairs + a teleport (46 steps),
  impossible on foot. NOTE: the *executor* doesn't walk teleport routes yet (bots
  still avoid teleports while exploring); a "travel"/goto-far command that follows
  a `find_route` plan (intentionally stepping onto teleports) is the next step —
  and the eventual hierarchical (Google-Maps-style) planner for huge maps.
- **Travel command (execute shortcut routes).** DONE 2026-07-13. `client.travel`
  follows a `find_route` plan: walks on-foot stretches with `_walk_to`, and takes
  a shortcut by walking to the tile before it then `_raw_step`-ing onto the
  source (which the normal walker avoids), confirming it landed at the link's
  destination, and re-planning after each hop. Typed route model so NPC/boat
  travel slots in later. CLI `travel --x --y --z`. Verified: bot walked from the
  temple (z7) to a staircase and rode it up to a z6 destination — arriving on a
  different floor. Aborts gracefully if a shortcut misbehaves.
- **Teleport-link precision (self-healing).** DONE 2026-07-13. Three fixes so
  routing stops trusting bad teleport records:
  1. Detection filters out same-floor jumps of <=2 tiles as movement glitches
     (they were being mis-recorded as "teleports"); position just re-syncs.
  2. `travel` verifies each shortcut as it uses it: confirms good ones
     (`colony.confirm_link`), CORRECTS a real shortcut whose destination was
     mis-recorded, and PRUNES a source that doesn't actually relocate
     (`colony.mark_bad_link` -> `_bad_links`, persisted). `report_link` refuses to
     resurrect a pruned source.
  Verified: seeded a bogus teleport, travel stepped on it, saw no relocation,
  pruned it, and re-adding was rejected.
- **Tool-gated floor changes (shovel holes, rope spots).** NOT YET — flagged by
  the user. Some floor changes need an ACTION first: a shovel used on a sand hole
  to open it (then descend), a rope used on a rope spot to climb up (or the magic
  rope spell if the character can cast it and has mana). These need: (a) a typed
  edge carrying a REQUIREMENT (tool item / spell+mana); (b) the "use item on
  tile" protocol + inventory awareness (and spell casting); (c) recognising which
  tiles are holes/rope spots — likely by item id, since a *closed* hole gives no
  floor-change signal when stepped on. IMPORTANT interaction: a closed shovel
  hole looks exactly like a "dead" tile to the current pruner, so once we support
  them we must recognise hole/rope tiles (by id) BEFORE pruning, or we'll discard
  real shortcuts.
- **Scout role.** DONE 2026-07-13. Two behaviours make a scout map far instead of
  circling home: `nav.find_frontier` (BFS to the nearest tile bordering unexplored
  space) so it expands the mapped region outward on purpose, and, once a floor is
  fully mapped, `_pick_descent` + `travel` to drop through a discovered shortcut to
  a new floor/region. `farm --bots N --scouts M` runs home-bound wanderers plus
  scouts (roles tagged per bot in the ColonyManager). Verified: a solo scout
  mapped ~6700 tiles in 100s (≈10x a random wanderer); in the farm the scout ran
  ~225 tiles out while the wanderer stayed ~50 tiles from the temple.
- **Traversal system design** written up in `antbot/TRAVERSAL_DESIGN.md`
  (2026-07-13): identify shortcut/door OBJECTS by item id (a `traversal.py`
  registry) instead of trial-and-error; typed edges carrying category / action /
  direction / requirement; covers portals, stairs, ladders (UP), grates, wells,
  open + shovel + rope holes, unlocked + locked doors + keys. Needs a use-item /
  use-with / spell protocol layer + inventory parsing (not built).
- **Traversal registry + use-item/spell protocol.** DONE 2026-07-13 (foundation).
  - `items.py` now also extracts item NAMES; but env objects (holes/ladders/
    teleporters/doors) turned out to be UNNAMED in appearances.dat, so name-lookup
    doesn't classify them. `traversal.py` `TraversalRegistry` instead LEARNS ids
    from behaviour (step onto a tile, get relocated -> record its topmost item id
    as teleporter/stairs_up/stairs_down), persisted to `learned_objects.json`;
    wired into the colony + the relocation detection. Full `CATEGORY_META` (step/
    use/use_with/cast; up/down/teleport/unblock; tool/key/spell requirements) for
    every kind. Unit-tested.
  - Protocol senders on the session: `use_item` (0x82), `use_item_with` (0x83, for
    shovel-on-hole / key-on-door), `say` (0x96, chat + spell casting), plus
    `inventory_pos`/`container_pos` helpers. Live-verified: casting a spell + chat
    then walking cleanly (a malformed packet would desync the stream).
- **Object identification from items.xml.** DONE 2026-07-13 (evening). The key
  realisation: Canary's `data/items/items.xml` is a catalog keyed by the SAME client
  id we see on tiles (its `Items::loadFromProtobuf` indexes ItemTypes by the
  appearance id, then merges items.xml into that array by `id`). So we read it
  directly — no in-game "Look" needed (that's server-generated from this same data;
  we keep it as a future *runtime* verification tool). `catalog.py` parses id ->
  name / floorchange / type; `TraversalRegistry.seed_from_catalog` maps that to
  categories and seeds ~1,250 objects at startup (64 teleporters, 240 stairs, 194
  holes/descend-ladders, 17 climb-ladders, 737 doors). Seeded data is soft —
  learned observation overrides it (wiggle room). `Colony.name_of` finally gives
  world objects readable names. Unit-tested + startup ~0.1s. Nothing consumes the
  categories to *change* behaviour yet, so it's safe/additive.
- **Travel executor — category dispatch.** DONE 2026-07-13 (evening).
  `_perform_traversal` traverses a tile by its registry category (STEP walks on;
  USE calls `use_item` for ladders/grates/wells/doors and returns any relocation);
  `_take_shortcut` makes `travel` pick USE vs STEP correctly; the scout's
  `_try_use_object` + `find_use_object` walk to a catalog use-object, use it, and
  record the resulting link so it becomes routable. Unit-tested per dispatch path;
  STEP + travel integration live-verified (scouts descended and hit region-jumping
  teleporters, 0 errors). USE-on-a-real-ladder still to be seen live (scouts only
  reach for one on a floor with no walk-onto exit).
- **Still to do:** live-verify the USE path on an actual ladder/grate; the Lua-
  scripted use objects (grates/wells/rope spots/shovel holes live in
  `data/scripts/actions/*`, not items.xml); `use_with`/`cast` traversal + the
  inventory parsing it needs (shovel/rope/key); registry into detection (proactive
  avoid / never-prune); NPC/boat travel; persist the shared map; colony blackboard
  + tasks; scaling past ~a dozen bots.

### Phase E — Roles (in order of protocol difficulty)
1. **Explorer** — nav + memory (easiest).
2. **Hauler** — nav + trade/shop packets.
3. **Supplier** — hauler variant (carry, drop at a coordinate).
4. **Hunter** — combat (hardest): targeting, attacking, health management,
   looting. **Fully autonomous** — fights/loots/heals/flees with no human
   confirmation (decided 2026-07-12; fine on our own local server).
**Deliverable:** the loot-runner economy from the dream, one role at a time.

### Phase F — Colony goals & polish
Optimization loops (exp/hr tracking, efficient hunt routes), zoom-into-a-bot
detail view, and colony-level objectives ("maximize high-level characters").

## Working conventions
- **Verbose, teaching-style comments in all code** — the code should explain
  itself (protocol fields, crypto, asyncio structure, design choices).
- Each phase ends in a runnable, observable deliverable before moving on.
