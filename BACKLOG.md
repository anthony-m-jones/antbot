# antbot backlog — everything not yet implemented

A living list of what's left, grouped by theme. See ROADMAP.md for what's DONE
and TRAVERSAL_DESIGN.md for the traversal system spec.

## Priority order (top-level, 2026-07-14)
1. ~~**Inventory parsing**~~ — DONE (worn slots + containers + `carries()`; USE_WITH wired).
2. **Directed "go here" + path visualization** (this section) — interactive control. NEXT.
3. **Group multi-select go-here** — incremental on #2.
4. **Survival: flee / heal** (Known-bug/roles) — keep bots alive; runs in parallel.
5. **Boat / carpet NPC travel** (NPC/vehicle section) — unlocks the sea leg of a route.
6. **Phase E roles** — hauler → supplier → hunter.

## Directed control & path visualization (NEW 2026-07-14)

Goal: point-and-click control of bots from the dashboard, and SEE how each bot
routes from its position to a target — including teleport/stair hops now, and boat/
carpet-NPC legs once those exist (#5 above).

- [ ] **"Go here" for one bot.** A per-bot *directed goal* (x, y, z) that interrupts
      the scout/explore loop, runs `travel` to it (world routing over the shared map
      + learned links already exists), then holds/idles — and can be cleared to
      resume. Pieces: (a) a goal slot on the bot the behaviour loop checks each round;
      (b) command plumbing `/command?action=goto&bot=NAME&x=&y=&z=` through the
      ColonyManager; (c) dashboard: click-to-select a bot, then click a destination
      tile on the minimap to send it.
- [x] **Path visualization / inspector (DONE 2026-07-14, Phase 1).** A visual-only
      "🧭 Plan path" mode in the dashboard: renders the WHOLE explored floor
      (`/map?z=`), click to drop a START (green) and END (red) pin, and it draws the
      planned route (`/plan` → `Colony.plan_route` over the shared walkable map +
      learned teleport/stair/ladder links, cross-floor) as a line with shortcut-hop
      dots, plus a report: distance (tiles), rough ETA (per-tile time × an optional
      speed input), and walk-step vs shortcut-hop counts. NOT sent to any bot. Verified
      live (20k-tile floor loads; a routable pair shows 20 tiles / ~15s / found).
      REFINEMENTS still open: (a) **gold cost** stays 0 until boat/carpet NPC travel
      exists as router edges; (b) **hop SUBTYPES** (teleport vs stair vs ladder vs
      shovel-hole, "closed until reached") need per-link category tracking; (c) ETA
      is a flat-ground estimate until real ground speeds land (below).
- [ ] **Ground-based walkability (SEPARATE FOLLOW-UP, wants a before/after).** Adopt
      the OTClient rule: a tile is walkable iff it has a walkable GROUND (the
      appearance `bank` flag) — extract `bank`/`waypoints` (ground speed) from
      appearances. This should fix water/lava/void more principled than the current
      catalog-name hack AND give real per-tile ETA. Capture a before/after (walkable-
      tile counts near water; a peninsula path) when implementing. See the OTClient
      dig: `Tile::isWalkable` = `NOT_WALKABLE || !getGround()`, `getGroundSpeed` =
      `bank().waypoints()`.
- [ ] **Group select + go here.** Multi-select bots (shift/box select on the
      dashboard) and issue one goto to all selected. Incremental on the single-bot
      version — same command, a list of bots.

## Observability / dashboard

- [x] **Bot health display.** DONE. We now parse the player-stats packet (0xA0)
      — which arrives in the login frame's *tail*, after the map and inventory, so
      it needed 0x78/0x79 inventory handlers plus login-tail processing to reach.
      hp/max_hp/mana/max_mana/level flow through BotView → snapshot → dashboard.
      The bot list shows a compact HP bar + percent (green/amber/red) always, and
      the focused bot gets a detail panel (lvl / hp / mana / pos). (Both, per the
      user's ask.) Files: world.py (parse), colony.py (BotView + snapshot),
      client.py (_report_to_colony), dashboard.py (hpBar + renderPanel).
- [x] **Bot warning/error log.** DONE. `Colony.log_event(bot, level, category,
      message, position)` appends to a thread-safe `_events` deque (mirrored to the
      logger); the snapshot exposes the newest 40 with an `ago` age, and the
      dashboard renders them in an "issues (warnings / errors)" panel. Bots call it
      via `session.log_event(...)` on give-up / boxed-in / shortcut-prune / and the
      watchdog. This is the anomaly channel the object-learning idea will reuse.
- [ ] Floor/region selector on the dashboard (currently follows the focused bot).
- [ ] Persist the shared explored map so the minimap (and standalone travel)
      survive a restart.

## Known bug — "bots sometimes stop moving"

- [x] **Watchdog + diagnostics.** DONE. `GameSession` stamps `last_frame_time`
      (every frame) and `last_move_time` (every successful step). `_check_watchdog`
      runs each loop tick: no frames for 20s → treat as a stalled/half-open TCP
      connection, log an error and abort (the supervisor relogs); no movement for
      25s → clear only the bot's *local* `blocked_tiles` (keeping colony hazards),
      reset the move clock and retry, logging a warning. Wired into explore + scout.
- [x] **Root-cause fix — explorer targeting (DONE 2026-07-13).** The dominant
      "stopped" symptom was NOT a freeze: the explorer picked *random* targets up to
      ~44 tiles away but capped the walk at 30 steps, so reachable targets got cut
      off mid-route and mis-logged as `gave up after 30 steps`, then it picked a new
      random (often backward) target → thrash in place. Fixes: (a) the explorer now
      heads for the nearest **frontier tile within its home box** (`find_frontier`
      with center+radius) — reachable by construction and always reveals new map,
      with random milling only as a fallback when the box is fully mapped; (b) the
      step budget is **sized to the target distance**; (c) `_walk_to` only logs a
      `navigation` issue on **genuine no-progress**, and milling passes
      `log_stuck=False`. Live result: navigation-warning spam went from ~10/30s to
      **0**, bots move smoothly, coverage still climbs (~12k+ tiles). The issue log
      is now clean, so it only lights up on real problems.
- [x] **Water-locked / peninsula stuck (DONE 2026-07-13, evening).** Three linked
      causes, all fixed: (1) `is_walkable` only checked the `unpass` flag, but only
      ~half of water ids carry it in appearances.dat, so bots treated open sea as
      walkable, flooded across it, and chased/​walked-onto water frontiers → thrash.
      Fixed by marking water/lava GROUND ids impassable from the catalog
      (`impassable_ground_ids`; checked on the tile's ground item so decorative
      "water" items don't wrongly block). (2) `find_frontier` (unbounded) returned
      frontiers the bounded step-planner couldn't reach (7 tiles across a bay = 40+
      by foot), so the scout hammered one unreachable frontier forever — fixed with
      a `skip` blacklist the scout fills when a walk makes no progress. (3) A truly
      water-locked scout then cycled `find_route` (full-map Dijkstra) several times a
      second — fixed with a boxed-in backoff (log once, sleep, stop probing). Live:
      water-walkable tiles 710→0, worst single-frontier hammer 323→2, route churn
      hundreds→0, exploration up (~16k tiles), 0 errors.
- [x] **Scouts stuck "but could travel" (DONE 2026-07-13, night).** Investigated
      Druid 2/4 pinned in place while able to travel. Causes + fixes: (1) the boxed-in
      backoff I'd just added SKIPPED the descent/travel probe, so a scout that had
      blacklisted its local frontiers just sat there — now it still runs the full
      escape chain (frontier + descent + use-object), paced by the backoff. (2) the
      frontier blacklist only cleared on floor change — now it also clears whenever
      the bot moves or backs off, so frontiers freed by the watchdog are retried.
      (3) THE BIG ONE: every rejected step was recorded as a PERMANENT block, so
      transient rejecters (a monster or a clustered bot racing into our path) slowly
      walled bots in — the reject log showed plain walkable grass being blocked, i.e.
      another bot stood there. Fixed with time-expiring blocks (`blocked_expiry`, TTL
      12s) plus skipping the block entirely when a creature currently occupies the
      tile. Live: permanent-block churn 146→2 per 90s, travel no-route churn →0,
      exploration up, 0 errors.
- [x] **Relog supervisor (DONE 2026-07-13, night).** Bots no longer stay dead.
      `ColonyManager._run_bot` is now a persistent loop: when a session ends for ANY
      reason — disconnect, stall, or the character DYING (Canary logs a dead player
      out) — it relogs, unless we asked it to stop. It paces with exponential backoff
      (5s→90s cap) that resets after a healthy (>30s) run, plus per-bot jitter so the
      swarm doesn't relog in lockstep. It also handles the second failure mode we
      found: a **frozen-but-connected** bot — the server keeps pinging us (so the
      receive-stall never fires) but silently drops our WALK packets (flood
      protection on a bot that spammed steps). The watchdog now force-relogs a bot
      that hasn't moved for `_RELOG_STUCK_SECONDS` (90s), giving it a fresh
      connection with a clean flood budget. Live (4 scouts, 200s): 7 supervisor
      relogs + 5 force-relogs, bots kept cycling back online, exploration climbed,
      0 errors.
- [x] **Shared login queue (DONE 2026-07-13, night).** Confirmed by experiment that
      the "0 bytes read" connection drops are a LOGIN-BURST / shared-IP issue, NOT
      accounts (each Druid is already on its own account `test1..test6`) and NOT a
      per-IP concurrent-connection cap (Canary has none; 6 bots coexist fine once
      connected): **1 scout alone = 0 drops**, but several logging in close together
      = many drops (server accepts the TCP then closes it — most likely DB contention
      on concurrent account/character loads). Fix: `ColonyManager._space_login` — a
      single `_login_gate` lock all logins AND relogs pass through, spacing them
      `_LOGIN_GAP_SECONDS` (4s) apart so only one handshake happens at a time. Live
      (6 scouts, 230s): drops fell from continuous to **2 total**, and all **6 bots
      stayed online** (vs. churning to idle before). Replaces the old fixed per-bot
      stagger.
- [x] **Send-side packet-rate cap + walk-timeout reroute (DONE 2026-07-13, night).**
      Canary DISCONNECTS a connection averaging >25 packets/sec (per connection, ~2s
      window). `GameSession._send` now rate-limits ALL outbound packets (walks/pings/
      everything) to `_MIN_SEND_INTERVAL` (80ms ≈ 12/s) behind a lock — well under the
      cutoff, invisible in normal play, only smoothing bursts. Separately, a "no
      response to walk" (server silently ignores a walk into e.g. a creature/edge) used
      to make the bot abort and re-target the SAME tile forever; it now blocks that
      tile briefly (expiring) and re-plans AROUND it, like a reject. Live (6 scouts):
      0 flood disconnects, relog churn halved (22→10 over a similar window), bots walk
      around unresponsive tiles instead of hammering; exploration unaffected.
- [x] **Chunked auto-walk (DONE 2026-07-14).** The bigger packet-rate lever: `_walk_to`
      now follows a path via the server's AUTO-WALK packet (client 0x64: count + dir
      bytes, encoding E=1/NE=2/N=3/NW=4/W=5/SW=6/S=7/SE=8) in chunks of 24 — one packet
      per chunk instead of one per step. `session.autowalk()` + `_autowalk_run()` sends
      a chunk and monitors the 0x6D moves, stopping (0x69) + re-planning on reject /
      hazard / glitch / silence, so all the per-step robustness is preserved (block-on-
      reject, learn-link-on-hazard, floor step-back). Single-step `walk()` kept for
      one-off moves (e.g. combat repositioning). Live: robust nav, 0 errors, server
      flood-DISCONNECTS ~1/210s. It did NOT kill the stuck/boxed/frozen churn — that's
      CLUSTERING (below), not flood. Possible refinement: when a bot is STUCK it still
      re-plans+auto-walks every ~0.1s; back off harder there to cut those packets too.
- [x] **Ocean-floor walkability (DONE 2026-07-14).** Q1 peninsula bug: a scout walked
      onto an already-mapped peninsula toward unexplored tiles across open water. Cause:
      our name-based impassable-ground fix matched "water"/"lava" but the open sea uses
      tiles named "ocean floor" / "sea floor" (+"brown sea floor") — no "water" in the
      name — so is_walkable let the flood cross the sea, find_frontier picked a frontier
      out in the ocean, and the scout chased land it could never reach. Fix: added those
      phrases to `_IMPASSABLE_GROUND_KEYWORDS` (catalog.py); impassable ground ids 733 ->
      783 (+50).
- [x] **Ground-speed flag parsed; NOT usable for walkability (INVESTIGATED 2026-07-14).**
      We now parse `AppearanceFlags.bank.waypoints` (the client's `getGroundSpeed`) —
      `ItemFlags.ground_speed(id)` / `has_ground_speed(id)`, 3203 walkable-ground ids.
      Wiring it into is_walkable was the plan, but the real data kills every variant:
        - "speed == 0 -> impassable": 205 items match, but ALL are already blocked by
          the name list or the unpass flag — 0 new tiles. A no-op.
        - "no bank at all -> impassable": DOES catch new tiles, but they're 305 ramps,
          85 stairs, grass, 12 ladders — walkable traversal tiles. Would break floor
          changing. Ramps/stairs carry no `bank` yet ARE walkable.
        - The actual culprit, "ocean floor", has speed=250 (a slow-but-present speed),
          so NO speed rule catches it — the asset marks water walkable and leaves the
          reject to the server. The name-based blend is strictly more accurate for us.
      So walkability stays name+unpass based (I was wrong earlier that speed was a clean
      signal). The parsed speed IS the right input for walk-COST / ETA (its actual client
      use) — feed it into colony.plan_route instead of the _AVG_GROUND_SPEED=150 constant
      (path-inspector ETA refinement, still open).
- [x] **Descent via recognised down-holes (DONE 2026-07-14).** A scout that climbed UP
      a ladder had no way back DOWN: going up records only the up-link, `_pick_descent`
      uses learned links only, and `find_use_object` skips STEP objects — so a bot could
      strand itself upstairs. Added `nav.find_descent` (nearest STEP+down object: open
      hole / trapdoor / ladder-hole-down / down-stairs, all seeded from the catalog as
      `hole_open`, 194 ids) and `client._try_descend`, wired into the scout's no-frontier
      chain after the learned-link descent. Handles both the nearby-hole case and the
      standing-ON-a-hole case (step off a neighbour then back on, since being *placed* on
      a hole doesn't drop you). Floor changes clear the tried-objects/tried-descents sets
      so each new floor is a fresh slate. Logic unit-verified. NOTE: it did NOT rescue the
      3 bots we saw stuck up top — those are GRIDLOCKED (next item), not a descent gap; a
      bot that can't take one step never reaches any escape branch.
- [x] **Break-out maneuver for stacked/gridlocked scouts (DONE 2026-07-14).** Root cause
      of the (32416,32220,6) pile-up: `is_walkable` returns False for any tile in
      `blocked_tiles`, so a scout that has accumulated blocks all around itself (mutual
      blocking while stacked) makes the planner refuse to route ANY step — it never even
      tries to move, freezes, force-relogs, respawns in place. Most of those blocks are
      STALE (the other bot has since moved). Fix: `is_walkable(..., ignore_blocked=True)`
      (terrain-only) + `client._escape_step` — when boxed-in (scout_stuck >= 8) the scout
      raw-steps out bypassing the block map, RETRYING each direction a few times (the
      blocker is usually another bot about to move or a dropped walk packet), directions
      shuffled so a cluster doesn't all pick the same exit, and clears a tile's stale
      block on a successful step. Live (4 scouts, 160s): all 4 ended at DISTINCT positions
      all "scouting" (was: 3 pinned on one tile, idle/frozen); 17 successful break-outs;
      frozen 8 -> 1, force-relogs 8 -> 1; explored ~9.5k -> ~17k. This is the big
      stability win.
- [x] **Disperse at the source (DONE 2026-07-14) — bigger win than the escape.** All
      scouts share ONE colony link graph, so multiple routed to the SAME descent landing
      and piled onto one tile (then the escape had to dig them out). `_pick_descent` now
      consults `colony.bot_positions()` and skips a shortcut whose LANDING already has
      another bot within `_DISPERSE_RADIUS` (4) — preferring uncrowded/different-floor
      exits, and declining to descend at all rather than stack onto a pile (it explores/
      escapes here instead and retries once the landing clears). Live (4 scouts, 160s),
      vs the escape-only run: boxed-in episodes 20 -> 1, break-outs 17 -> 1, force-relogs
      1 -> 0, explored ~17k -> ~29k, 4 distinct positions all scouting. Prevention beat
      recovery — the escape maneuver now rarely even fires. STILL OPEN (nice-to-have, no
      longer needed for stability): different START sectors per scout, ladder/use-object
      cooldown (not just descent-link landings), and reset-to-temple after N same-spot
      force-relogs (currently 0 relogs, so unneeded).
- [x] **reset-to-temple made reliable for the normal case (DONE 2026-07-13, night).**
      Root cause found (deeper than a save race): the DB already holds every Druid at
      the temple, and the temple UPDATE affects 0 rows. Bots still spawn at old
      positions because a character that can't log out — Canary's `canLogout()` is
      false while INFIGHT outside a protection zone — leaves a stale IN-MEMORY session,
      and with `replaceKickOnLogin=true` the relog RECONNECTS to it at its old spot,
      never loading the DB. Fix: `reset_to_temple` now does log-out → DB-update →
      log-in, then VERIFIES via the colony's live positions and RETRIES once with a
      long settle (65s) that outlasts combat + the ~60s stale-session drop; on failure
      it names the stuck characters and explains the cause. Works for any character
      that can log out.
- [ ] **Dislodge a combat-locked stuck character (the hard remainder).** A character
      pinned in a monster area, too strong to die, is in combat forever → never logs
      out → its in-memory session never dies → no DB reset can move it. Only remedies:
      the character dies, a server restart (`docker restart otbr-server-1`, clears all
      in-memory sessions so they reload the DB's temple position), or a GM `/kick`.
      Consider: (a) a colony command that walks a stuck character to the temple PZ
      then logs it out cleanly, or (b) detecting this state and surfacing "needs
      server restart" on the dashboard.
- [ ] **Escape a genuinely boxed-in character** (e.g. parked on a water-locked
      island): today it backs off and logs "boxed-in" but can't leave without a
      boat/teleport. Options: relog-to-temple, or NPC/boat travel (below).
- [ ] **Remaining freeze causes to still watch** (rare now, via the issue log):
      (c) stranded after a teleport to a dead-end, and true TCP stalls (the 20s
      watchdog). Keep an eye on the issues panel in long runs.

## Traversal — use-based objects (the current gap)

- [x] **Identify traversal objects from items.xml (DONE 2026-07-13).** The
      breakthrough: Canary's `data/items/items.xml` is a catalog keyed by the SAME
      client id we see on tiles (proven in `Items::loadFromProtobuf`). `catalog.py`
      reads it (id -> name / floorchange / type); `TraversalRegistry.seed_from_
      catalog` maps that to categories and pre-loads ~1,250 objects at startup:
      teleporters (64), stairs/ramps (240), walk-onto holes & descend-ladders (194),
      climb-up ladders (17), doors (737). Seeded knowledge is SOFT — a bot's learned
      observation overrides it (wiggle room). Also gives us item NAMES
      (`Colony.name_of`), which appearances.dat lacked. Makes the old "curated id
      list" idea unnecessary for these.
- [x] **Lua-scripted use objects — extracted what's generic (DONE 2026-07-13,
      night).** From Canary's `register_actions.lua`: `shovel_hole` ids
      {593,606,608,867,21341} (shovel → down) and `jungle_grass` {3696,3702,17153}
      (machete → unblock) are now seeded via `TraversalRegistry.seed_curated`.
      Findings: **grates/wells are QUEST-specific** in Canary (per-position scripts,
      e.g. oramond/rookgaard sewers, draw-well) — no generic id list to pull, so
      they're not a general mechanic. **Rope-spots** are a tile flag (`isRopeSpot()`),
      not an id list — a later source. NOTE: all of these are USE_WITH (need a carried
      tool), so the executor recognises them but can't ACT until inventory parsing
      lands — see below.
- [ ] **Trial-use learner with confidence + wiggle room** (user idea). For each
      object id, record which actions have been tried (use / shovel / rope / cast)
      and whether it reacted. Once an id is shown to react (or not), tentatively
      assume other tiles with that id behave the same — but DON'T treat it as
      100% certain:
        - Store a *confidence* per (id, action) — a count/score, not a boolean —
          so one contradicting observation doesn't flip a well-established fact.
        - Keep a **per-id default** (the majority behaviour) plus **per-tile
          overrides** for exceptions. Tibia has holes that usually drop you but
          occasionally don't — so a specific tile can differ from its id's default
          without us "unlearning" the id.
        - On a contradiction (a "known non-teleport" tile teleports, or vice
          versa), log a warning (where, expected, actual) and flag that tile for
          re-investigation by other bots, rather than overwriting the id's default.
- [x] **Travel executor — dispatch per action (DONE 2026-07-13, evening).**
      `client._perform_traversal` traverses a tile by its registry category: STEP
      (teleporter/stairs/open hole) walks onto it, USE (ladder/grate/well/unlocked
      door) calls `use_item`, and it returns where we landed if it relocated us.
      `_take_shortcut` makes `travel` USE vs STEP a shortcut correctly; the scout's
      `_try_use_object` walks to the nearest catalog use-object and uses it, then
      records the source->dest link so it becomes routable (`find_use_object` finds
      candidates, skipping step-objects). Unit-tested for every dispatch path; the
      STEP path + travel integration are live-verified (scouts descended floors and
      hit region-jumping teleporters through it, 0 errors). **Still to verify live:**
      the USE path on a real ladder/grate — the scout only reaches for one when a
      floor has NO step-link exit, which didn't happen near the teleporter-rich
      temple in testing. Needs either a longer run onto a ladder-only floor or a
      targeted test with a known ladder location.
- [x] **`use_with` traversal (DONE 2026-07-14).** `_perform_traversal` now looks up
      the required tool in inventory (`GameState.carries` + `traversal.TOOL_IDS` for
      shovel/rope/machete) and fires `use_item_with` at the object; skips cleanly if
      the tool isn't carried. Not yet live-fired (the test characters' bags are
      empty). `cast` (Magic Rope spell) still stubbed; `key`-on-door is per-door
      (which key fits which lock) and left for later.
- [~] **Proactive detection + live USE verification (LARGELY DONE 2026-07-14).**
      Scouts now try a NEARBY untried use-object every round (`_ONSIGHT_RADIUS=6`,
      `find_use_object` box-optimised) and prefer a use-object over a step-descent when
      one's within `_USE_OBJECT_RADIUS=25`. A deterministic probe near the Thais temple
      CONFIRMED the hard parts: `find_use_object` recognised REAL live objects by their
      wire ids — "a closed door" (1629/1631/1638), "closed fence gate" (2177/2179),
      "a ladder" (1948) — and `_perform_traversal` sent `use_item` for them live with
      no error (the id-space match + executor firing were the real uncertainties; both
      hold). ALSO fixed a seeding bug found here: `type=teleport` mislabelled common
      floor tiles (wooden floor 628/878, 128 in one harbour) as teleporters — now
      dropped (teleporters are learned by behaviour); tables/windows dropped from doors.
      STILL OPEN: (a) observe the server REACTION (door opens / ladder relocates) —
      blocked here only because the test characters had drifted onto random floors and
      spawn stuck (needs a clean char to navigate to a ladder); (b) treat an opened
      door as walkable so the pather routes THROUGH it — DONE, see below (2026-07-23);
      (c) the "wanderers avoid / never-prune" side.
- [x] **Doors are routable in `find_shared_route`, not just openable reactively (DONE
      2026-07-23).** Closes (b) above. Previously a door was invisible to the shared-map
      router: `contribute_tiles` never adds a closed (blocking) tile to `_walkable`, and
      `report_link` — the only thing that ever populated `_links` — is gated on the USE
      action actually RELOCATING the bot, which a door never does (it unblocks, it
      doesn't move you). So `find_shared_route` always routed AROUND a door if any other
      path existed, and travel() could only cross one by falling through to the slower
      per-round `_try_use_object` hunt. Fix: `contribute_tiles` now seeds a `door_unlocked`
      tile as a SELF-link (`_links[tile] = tile`, not in `_step_links`) the instant it's
      recognised — unlike a ladder/grate, a door's destination is already known (itself),
      so there's no need to wait for a bot to actually walk up and use it first.
      `find_shared_route` needed NO changes: a USE-type link source already gets a
      "walk here, then use it" edge for free (see its existing docstring) as long as it's
      in `links`, independent of `walkable` membership. Priced at `MIN_GROUND_SPEED` for
      now (same as a teleport hop) — plausible since opening one may cost no real time if
      the use-item packet lands before the walk would've, but unproven; see the new TODO
      below. Also fixed a real bug this surfaced: `travel()`'s post-shortcut check assumed
      success == relocation, which is true for every OTHER link kind but never for a door
      — naively `continue`-ing after using one just re-proposed and re-used the SAME door
      forever (it never advances past it, since opening it doesn't move you and nothing
      else lets the router "arrive" there). Fixed by taking one on-foot raw step onto/
      through the door right after using it (mirroring `_perform_traversal`'s new UNBLOCK
      handling, which optimistically clears the tile locally so that step can succeed) —
      a door that's actually locked simply rejects the step and falls through to the
      existing self-heal/prune path, same as any other stalled shortcut. VERIFIED live:
      full navtest suite 7/7 pass; "Bot can open a door to leave a room" now passes in
      1.2-1.6s (was going through the slower fallback); both door-adjacent efficiency
      fixtures (`climb a floor and open a door`, `unconfirmed z-hop crossroads`) rebuilt
      and hit cold=warm=par (ratio 1.00, 0% over optimal) — the door is no longer a
      detour, it's on the optimal path. Dropped the persisted `learned_hazards.json`
      (learned under the old, door-blind interpretation) so real bots start clean; it
      regenerates on its own.
- [ ] **Optimize door link cost.** `MIN_GROUND_SPEED` (same as a teleport) is a placeholder
      guess, not a measurement — a door needs a walk-adjacent + `use_item` round-trip that
      a teleport doesn't, so it may deserve its own, likely higher, constant. Once there's
      a navtest that can score real door-hop time against the alternatives, use it to tune
      a dedicated cost instead of sharing the teleport price (see the TODO in
      `nav.find_shared_route` next to where `MIN_GROUND_SPEED` is applied).
- [x] **Inventory parsing (DONE 2026-07-14).** `GameState.inventory` (worn slots
      1-10, from 0x78/0x79) + `GameState.containers` (from 0x6E open + 0x6F/0x70/0x71/
      0x72 updates) + `GameState.carries(*ids)` lookup. The backpack auto-opens at
      login (`_open_backpack`) so contents are visible. Verified live: worn items
      parse with correct names. KEY GOTCHA fixed along the way: the inventory 0x78
      packets are BATCHED behind a 0x86 creature-shield packet (cipsoft860) that we
      didn't handle, so we bailed before reaching them — now handled. NOTE: the test
      characters' bags are empty, so container-CONTENTS parsing + a real shovel-use
      haven't been exercised with actual items.
- [ ] **Keys**: recognise key items, map which key opens which door (action/key
      id), acquire + carry + use.
- [ ] **Locked doors are pruned FOREVER, with no way back (2026-07-23).** In `travel`'s
      shortcut handling, a door that rejects the on-foot step through it (still locked,
      no key) falls into the exact same `colony.mark_bad_link` path as a link whose
      recorded destination was simply wrong — permanently blacklisted in `_bad_links`,
      never reconsidered ([client.py](antbot/antbot/client.py) `travel`, around the
      self-heal/prune fallthrough after `_take_shortcut`). That's the right call for a
      genuinely bogus link, but wrong for a door that's only locked *right now* — once
      the "Keys" item above exists (or a quest/GM unlocks it), the door should become
      usable again, and today it can't: `_bad_links` has no expiry and no distinction
      between "this was never real" and "this is real but currently locked." Kept as
      permanent for now (deliberate, see the Gap 1/Gap 2 travel-executor redesign
      discussion) — revisit once key-learning lands: write a test where a bot meets a
      locked door it has no key for, later acquires the right key, and confirms it can
      route back through the same door instead of treating it as dead forever.

## Traversal — teleport precision (improvements)

- [ ] More precise teleport-*source* attribution (imprecise when position tracking
      hiccups). Self-healing prune/confirm is in; a verification pass would help.

## NPC / vehicle travel

- [ ] **Boat / carpet travel** as typed `npc-travel` edges: a curated route table
      (Thais↔Carlin↔Venore…, desert carpets) + the NPC conversation protocol
      (walk to NPC, say destination, confirm). Shares the use-item/say layer.

## Colony intelligence / scale

- [x] **Directed frontier exploration for wanderers too** (DONE 2026-07-13) —
      wanderers now use bounded `find_frontier` like scouts (see the explorer fix).
- [x] **Bounded per-step planning (DONE 2026-07-13)** — the big CPU win for scale.
      `state.tiles` never prunes, so re-planning every step used to flood the whole
      accumulated map (~55 ms/re-plan on a 48k-tile map). `find_path_toward` now
      takes a `max_radius`; the explorer/scout pass `_PLAN_RADIUS=40`, cutting a
      re-plan to ~3 ms (16×) with no routing-quality loss. goto/travel stay exact
      (unbounded). Next scale lever after this is connection pacing (below).
- [ ] **Colony blackboard + task assignment** — shared goals, roles beyond
      explore/scout (hauler, supplier, hunter — Phase E).
- [ ] **Scale past ~a dozen bots**: better connection pacing (server drops fast
      logins from one IP); maybe a login queue. (Planning cost is no longer the
      bottleneck — see above.)
- [ ] **Prune/cap `state.tiles` AND `state.seen` per bot** (memory, not CPU now): a
      long-running scout's tile dict grows without bound; `state.seen` (added for the
      open-air/cliff frontier fix — every observed slot, empties included) grows even
      faster. Evict both far from the bot (the colony keeps the shared map anyway), or an
      LRU cap. Keep them consistent: a pruned tile should also leave `seen` so re-entry
      re-reveals it.
- [ ] **Hierarchical / transit-node routing** for when the world map is huge
      (precompute portal-to-portal distances; most of a long route is a lookup in
      a tiny graph). Not needed until the map is large.

## Observability — session recorder + replay UI (ASSESSED 2026-07-14, not started)

- [x] **Session recorder + replay UI (DONE 2026-07-14, Tier 2).** Built to the locked
      spec (see REPLAY_DESIGN.md): `recorder.py` with flight-recorder (in-memory ring,
      auto-dump on freeze/death) + full modes, async buffered writes that never stall the
      nav loop; capture points for snapshots (pos/vitals/creatures), actions, and the
      decision "why" trace; `--record none|flight|full` (default flight). Dashboard replay
      mode: session picker, floor backdrop, bot + creature dots, scrubber, playback
      (setInterval so it survives a backgrounded tab), and a click-to-jump timeline log.
      Verified end to end in the browser (recording produces valid JSONL; replay renders,
      seeks, plays, jumps). OPEN follow-ups: whole-swarm timeline (multi-bot on one clock),
      zoom-to-bot framing, gzip on close — all noted in REPLAY_DESIGN.md. Tier 3 (view
      tiles) and Tier 4 (deterministic re-sim) remain the documented ambitious goals.
- [ ] (assessment kept for reference) **Record everything a bot does + a replay UI.**
      - Already have: the dashboard canvas renders floors + bot dots with the coord
        transforms (the fiddly UI); `Colony.snapshot()` assembles positions/status/vitals/
        events; `log_event` centralizes issues; action call sites are known (walk/autowalk/
        `_escape_step`/`_perform_traversal`).
      - Missing: creatures aren't reported to the colony yet (only local to the bot via
        `state.nearby_creatures()`) — add them to the record; a Recorder that taps the
        report path and appends timestamped, DELTA-encoded records (JSONL for grep, or
        SQLite for seek/query at scale); the replay UI (session picker + timeline scrubber
        + creature overlay + status/HP/action side panel, reusing the existing canvas).
      - Cost: ~2 state-changes/s/bot; delta+gzip ≈ 0.5–1 MB/hour/bot. A few bots = tens of
        MB; SWARM scale (100s) = GB/hour, so gate it (on-demand / sampled subset). Writes
        MUST be buffered/async so a slow disk never stalls the nav loop (the one real
        gotcha). Creatures are the heavy repeating field → delta-encode them.
      - Phasing: (1) ½–1 day: "record the feed" — append snapshot() per poll to JSONL +
        a scrubber replaying position/status/events on the existing canvas (≈80% of the
        watch-a-session value, nearly free). (2) 1–2 days: add creatures + per-bot actions
        (walk dir / autowalk chunk / use / escape / hazard) + replay overlay (the debugging
        gold). (3) later: the bot's own view-window tiles per snapshot (true "what it saw"),
        SQLite, per-bot record toggle. "Map-as-it-was" in v1 = final shared map as backdrop;
        true progressive reveal costs more storage.

## Dashboard redesign (DONE 2026-07-15)

- [x] **New "scientific-instrument" dashboard UI (from a Claude Design mockup).** The
      page now lives in `antbot/antbot/dashboard.html` (dashboard.py reads it per-request,
      so the design iterates without a server restart). Ported the mockup and wired every
      panel to the real endpoints — the designer built it against the exact /state.json
      shape, so it was mostly swapping mock generators for fetches. Working + live-verified:
      first-run EMPTY state (bots don't auto-start), Start with scouts/wanderers counts,
      live map (explored tiles + bot markers, bots filtered to the shown floor), bots rail
      with search + role chips + list/compact/dense density, events dock with severity
      filter, follow-a-bot, manual floor browse (/map override, ⤢ returns to live), Path
      Inspector (/plan), Replay (/recordings + /recording, scrubber + timeline + jump).
      Connection state is driven by the poll. Added `role` to bot reporting for the chips.
      PLANNED STUBS still in the UI (labelled, harmless): coverage HEATMAP toggle, and
      "creatures on the LIVE map" (needs creatures added to /state.json — they're only in
      recordings today). Also noted for scale: virtualize the bots rail rows, and the
      whole-swarm replay timeline (single-bot replay works; multi-bot merge is the gap).

## Phase F — item movement + the hauler

- [x] **`move_item` (client 0x78) works (VERIFIED live 2026-07-15).** `parseThrow` is
      `fromPos(x:u16,y:u16,z:u8) itemId:u16 fromStack:u8 toPos(x:u16,y:u16,z:u8) count:u8`,
      and it reuses the special position encoding we already build (`inventory_pos` =
      (0xFFFF, slot, 0); `container_pos` = (0xFFFF, 0x40|cid, idx)). Proven both ways with
      a real character: worn slot -> ground (tile went [410] -> [410, 3552]) and ground ->
      backpack (container became [(3552, 1)]). This is the hauler's core capability.
- [ ] **0x6C (remove-thing) never removes ITEMS from `state.tiles` — only creatures.**
      Found while testing move: after a successful pickup the tile still listed the item,
      so picked-up loot lingers as a phantom (a hauler would try to re-pick it forever).
      The hard part is that 0x6C only gives (pos, stackpos) and doesn't say whether the
      thing was a creature or an item — and the server's stackpos counts creatures, while
      our `tiles` list is items-only, so the index doesn't map cleanly. Options: (a) the
      hauler optimistically removes what it just picked up locally + forgets an item when
      a pickup is rejected (precise, self-correcting, no map corruption) — doing this for
      v0; (b) later, model the real stack order (ground, top items, creatures, down items)
      so 0x6C can be resolved exactly. NOTE: stale items can also mislead the traversal
      registry, so (b) is worth doing eventually.

- [x] **Task blackboard + the HAULER role (DONE 2026-07-15) — the first bot that WORKS.**
      `Colony` now posts work and role-bots claim it: `Task` (kind/pickup/dropoff/status/
      assignee) + `add_task` / `claim_task` / `finish_task` / `release_task` /
      `tasks_snapshot`, all under the colony lock. A claim that goes stale (bot died or
      relogged) is reopened after `_TASK_CLAIM_TTL` so work is never stranded with a bot;
      a hauler that can't reach a spot RELEASES rather than fails it. `client.hauler`
      loops: claim -> travel to pickup -> lift takeable items -> travel to dropoff -> put
      down -> report -> next; it idles cheaply when the board is empty. Wired as a role
      (`--haulers`, `/command?action=start&haulers=N`, `role` reported to the dashboard)
      and work is posted via `/task?action=add&px..&dx..`; tasks ride along in
      /state.json. Also added `ItemFlags.is_takeable` (assets' `take` flag, field 18) so
      it lifts loot and not the floor. VERIFIED live end to end: seeded 3 items on the
      ground, hauler collected + delivered them, then a REVERSE task hauled them back
      (4/4 collected at the delivery tile — proving the loot physically moved).
      Two real bugs found and fixed while building it:
        - **The stuck/frozen watchdog killed idle bots.** It read "not moving" as
          "wedged" — true for a scout, wrong for a hauler waiting for work, which got
          force-relogged every 91s forever. Added `GameSession.expect_movement`: a
          behaviour declares when it intends to sit still, and `_check_watchdog` then
          only applies the CONNECTION-level stall check (and keeps the movement clock
          fresh so the freeze check can't fire the moment it starts moving again).
        - **The hauler assumed its bag was open.** The login-time `_open_backpack` waits
          only ~2.4s for the worn inventory (which arrives in later frames), fires one
          'use', and never confirms the container opened — so the hauler reported
          "nothing to collect" while standing on loot. Added `_ensure_bag_open`, which
          re-opens on demand and waits for the 0x6E before giving up.
      OPEN follow-ups: the "delivered 4/3" style note is confusing (placed counts the
      whole bag, got counts this trip); dashboard has no tasks panel or haulers stepper
      yet (role chips already work); and haulers can only be given work by hand — the
      colony should eventually post its own (that's what hunters will feed).

- [x] **Economy catalog + 2-D carry model (DONE 2026-07-15) — steps 1-3 of the hauler
      economy.** The verdict on protocol first: **we do NOT need to leave 8.60.** The
      modern client answers "who buys this and for how much" with the Cyclopedia; the
      server's own data answers it better, offline: `data-otservbr-global/npc/*.lua`
      carry `npcConfig.shop` rows (`clientId`, `buy`, `sell`) — 10242 entries across 295
      shops, EVERY one keyed by clientId (the same id space we read off tiles). Offline
      beats the Cyclopedia because it prices piles we haven't walked to yet, all at once,
      with no round-trips. Free capacity and container slot capacity were already on the
      8.60 wire; we were just discarding them.
        - `catalog.py`: ItemInfo gains `weight` + `container_size`. GOTCHA: items.xml
          uses BOTH `containerSize` (303 items) and `containersize` (2361 — the common
          one), so attribute keys are matched case-insensitively; matching one spelling
          silently missed nearly every container. Now 2714 containers have known slots.
        - `economy.py`: `load_economy()` -> {id: ItemEconomy(weight, stackable,
          container_size, best_sell, sold_to, best_buy, bought_from)}. 37509 items priced,
          1446 with a buyer. GOTCHA: **currency prices at 0** — no NPC "buys" gold because
          gold IS the money, so a naive catalog tells haulers to walk past the best loot in
          the game. Hence `CURRENCY_VALUE` (gold 1 / platinum 100 / crystal 10000); a
          crystal coin weighs the same as a gold coin and is worth 10000x (v/w 1000).
        - `world.py`: keep `state.capacity` (0xA0 free capacity — already accounts for
          vocation/level, so we never model it) and `state.container_caps` (0x6E slot
          capacity). `items.py`: explicit `is_stackable` (it was conflated with liquid
          inside `extra_bytes`, so it couldn't be asked).
        - `carry.py`: the two budgets and what they cost. Slot cost turns on stacking —
          topping up a stack you already hold costs ZERO slots (90 more gold onto a stack
          of 10 is free; the 91st buys a new slot). `plan_pickup` greedily solves the 2-D
          knapsack scoring `value / (weight_cost/free_weight + slot_cost/free_slots)` —
          value per fraction-of-remaining-budget, so whichever budget actually binds
          dominates with no hand-tuned weights. Unit-verified: slot-bound takes the
          free-slot gold; weight-bound refuses the heavy sell=0 robe; partial stacks split
          to fit. Bags are deliberately NOT modelled here — they're pre-run outfitting,
          not a loot decision.
- [x] **Tiles now carry stack COUNTS — `state.tiles` migrated to `[(id, count), …]`
      (DONE 2026-07-15).** `_parse_tile` used to read a stackable's count byte and throw
      it away, so a pile of 100 gold was stored as `[3031]` — indistinguishable from one
      coin, which made honest loot scoring impossible. Counts are part of a tile's truth,
      so we migrated the shape rather than bolt on a parallel dict.
        - Producers: `_parse_tile` and `_parse_thing` (0x6A) now keep the count via a
          shared `_read_count()` (factored out of `_read_item`, which containers use).
        - Consumers updated: `is_walkable` (ids only), `find_use_object`/`find_descent`
          and the client's classify/learn sites (via a new `world.tile_ids()` helper, so
          the traversal registry keeps its id-based API — it keys on item IDENTITY),
          `contribute_tiles`/`_tile_color`, and `_perform_traversal`'s stackpos scan.
        - Bonus: `_collect_loot` now lifts a WHOLE STACK per move instead of one unit.
      VERIFIED: unit — a crafted wire tile parses to `[(4526,1),(3031,100)]` (count byte
      survives, skip marker still aligned); live — 423 tiles parsed, 0 malformed, nav +
      the open-air frontier test still pass.
- [ ] **Preparation model (bags) — self-tuning, per your call.** Bags are outfitting, not
      loot: before a run decide how much capacity to spend on slots (e.g. 300 of 8000).
      Perfect calculation is impossible — the loot map is a cache, not live — so make it
      LEARN: after each run record which budget bound first (slots vs weight) and how much
      loot was left behind, then nudge the bag count next run. Slot-bound -> more bags;
      weight-bound -> fewer (they only cost capacity). Small feedback loop, not a solver.
      Needs recursive bag-opening (nested containers are invisible until opened) and the
      merchant role (to buy bags). `container_size` for 2714 containers is already parsed,
      so a bag can be evaluated before it's ever picked up.

- [x] **Hauler now picks by VALUE, not by whatever's on top (DONE 2026-07-15).** The
      hauler was blind: `_collect_loot` lifted anything with the `take` flag, so it would
      fill up on a 2500-weight robe that sells for 0 and leave the gold. Now the colony
      builds the economy catalog once (`colony.economy`, reusing the items.xml it already
      parses) and `_collect_loot` runs each candidate through `carry.plan_pickup`.
      VERIFIED live: a pile of [robe(sell 0), rod(sell 100), boots, boots] -> the hauler
      took the rod + both boots and LEFT the robe; a follow-up haul on the robe-only tile
      reported "nothing to collect" — it stood on liftable loot and deliberately declined.
- [x] **GROUND PILES ARE LIFO — the server ignores the stackpos on a tile move.** Found
      the hard way: the value-picker asked for a rod buried under boots and the move was
      silently refused, while duplicate boots always "worked" (masking it — wrong slot,
      same id, server accepts). Reading Canary settles it:
        `internalGetThing(..., STACKPOS_MOVE)` -> `item = tile->getTopDownItem();`  // ALWAYS the top
        `playerMoveItem`: `if (!item || item->getID() != itemId) -> NOTPOSSIBLE;`
      So a tile move ALWAYS acts on the topmost item and is rejected unless the id we name
      matches it. A bot CANNOT reach into the middle of a pile — the stackpos we send is
      ignored entirely (which is also why the very first manual move test "worked": the
      boots simply happened to be on top). `_collect_loot` therefore pops the stack from
      the top and lets the economy decide how deep to go. NOTE: this is why the old blind
      hauler worked — taking the topmost was accidentally protocol-correct.
- [x] **BROWSE FIELD (0xCB) beats the LIFO rule — and it works on 8.60 (VERIFIED
      2026-07-15).** Investigating whether LIFO was an 8.60 limitation turned up three
      things worth writing down:
        1. **LIFO is NOT a protocol limitation.** The rule lives in the GAME layer
           (`Game::internalGetThing` / `STACKPOS_MOVE`) with no version branch anywhere,
           so 8.60, 11 and 15.25 are identical here. Upgrading the protocol would NOT let
           you pick out of the middle of a ground pile. (More evidence for staying on 8.60.)
        2. **CORPSES WERE NEVER THE PROBLEM — they're containers, not ground piles.**
           `internalGetThing`'s container branch indexes by `slot = pos.z`, i.e. arbitrary
           slot access. So looting a corpse only needs "open it, then take any slot in
           value order"; the LIFO rule never applied. That's the model for the future
           hunter/looter role.
        3. **Browse-field gives a GROUND TILE the same power.** `playerBrowseField` does
           `Container::createBrowseField(tile)` and sends it back as an ordinary 0x6E
           container-open — and it carries NO `oldProtocol` guard (unlike quick-loot,
           below). Verified live on 8.60: standing on a tile holding a robe, `0xCB`
           returned container 9 (cap 30) = [(7991, magician's robe)] — the ground item
           itself excluded. Container id is `0xF - ((x%3)*3 + (y%3))` (matched exactly);
           asking twice TOGGLES it shut; requires same floor + within 1 tile (the server
           auto-walks you otherwise). `session.browse_field()` is implemented.
      => `_collect_loot` now DOES this (DONE 2026-07-15): it browse-fields the tile, then
      lifts items out by SLOT in value order — which is what `carry.plan_pickup` wanted
      all along — and falls back to the honest top-down pop if the server won't browse
      (a Lua onBrowseField veto). This supersedes the "shove junk aside to dig" idea:
      no need to push junk around, we just address the pile as a container.
      PROVEN live with junk deliberately buried on top: tile
      [ground, rod(sell 100), boots, boots, robe(sell 0)] with the ROBE ON TOP ->
      hauler delivered 3/3, reaching UNDER the robe for the rod + boots and leaving the
      robe. The top-down hauler would have said "nothing to collect" and abandoned all
      of it. Gotchas handled: browsing the same tile again TOGGLES it shut (so we check
      before asking and toggle to close when done), and `GameSession.browse_cids` marks
      which containers are really tiles — otherwise `_bag_container_id` could pick a
      browse-field as "the bag" and cheerfully haul loot into the floor.
- [x] **QUICK LOOT is server-side but closed to us (8.60).** `parseQuickLoot` (0x8F),
      `parseLootContainer` (0x90) and `parseQuickLootBlackWhitelist` (0x91) exist, and the
      filtering + moving all happen server-side (`Game::playerQuickLoot`, with the
      white/blacklist and a designated loot container stored on the player). But
      `parseQuickLoot` opens with `if (oldProtocol) { return; }` — the gate is INSIDE the
      handler, not in the opcode switch, so reading the dispatch alone is misleading.
      Design lesson rather than a feature: their filter is a static item white/blacklist;
      ours (the economy catalog + 2-D carry model) is strictly smarter because it prices
      items and respects weight/slot budgets. All we lose is packet efficiency — one
      quick-loot packet vs N moves.

- [x] **Loot map: the swarm's shared eyes (DONE 2026-07-15) — step 4.** Every bot already
      parses every tile in view, so `_scan_loot` (throttled 1s, radius 8) reports takeable
      ground loot to `colony.report_loot`, which PRICES it with the economy catalog. Any
      bot walking past a pile makes it fetchable by every hauler. Exposed in /state.json
      as `loot`. Verified live: a hauler that merely spawned near a pile reported
      `[32372,32241,7] value=104 items=[boots,boots,rod]` with nobody asking it to look.
      Only loot with a BUYER is tracked — bots can lift plenty of scenery no NPC wants,
      and 16 of the first 19 sightings were value-0 noise.
      **THERE IS DELIBERATELY NO TTL.** The server settles it: ordinary loot (gold coin,
      rod, boots…) has NO `duration`/`decayTo` in items.xml and `cleanProtectionZones` is
      off, so loose items sit on the floor forever — only CORPSES rot (dead rat:
      `duration=300 decayTo=…`, in stages). Age is therefore not evidence of absence, and
      expiring a crystal coin because five minutes passed would invent a rule the game
      doesn't have. Instead: age only DISCOUNTS the score
      (`expected = value x 0.5^(age/_LOOT_HALF_LIFE)`), and sightings are deleted ONLY by
      direct observation. Verified: a 30-minute-stale crystal coin (conf 0.125, expected
      1250) still outranks fresh gold (100) and is still in memory. Memory is bounded by
      dropping the LEAST VALUABLE, never the oldest. Every arrival also records a
      labelled sample (`colony.loot_survival()`: {age_minutes: (still_there, gone)}), so
      the half-life can become measured rather than guessed.
- [x] **Fixed: purge/re-report race resurrected looted piles.** The ground-truth purge
      fired, then `_scan_loot` re-read `state.tiles` a second later — which still held the
      items, because 0x6C never removes items for us — and RESURRECTED the sighting. The
      loot map insisted a looted pile was still there, forever. Fix: the browse container
      is the server's own answer about a tile (exactly its movable contents), so on
      finishing a pickup we resync the LOCAL tile map from it (`tiles[tile] = [ground] +
      browse_contents`) BEFORE correcting the colony's map. Order matters. Verified live:
      source sighting went GONE the moment it was looted and stayed gone, while the
      destination sighting appeared — the map follows the loot. NOTE this also gives us a
      general way to repair the 0x6C phantom problem for any tile we visit.
- [ ] **Unreachable manual tasks busy-loop.** A haul task pointing somewhere the colony
      hasn't explored can't be routed, so the hauler releases it and instantly re-claims
      it, forever ("couldn't reach pickup" on repeat). Harmless today (1s pace) and it
      can't happen to loot-map-driven work — we only know about loot we've SEEN, which is
      by definition explored — but a claim that fails to route should back off, or fail
      after N attempts, rather than spin.

- [x] **`claim_best_haul`: haulers find their OWN work (DONE 2026-07-15) — step 5.**
      Pull-based, not push: the colony offers candidates and settles races, the BOT judges
      them. That split matters — only the bot knows what it can carry (its capacity, its
      free slots, which stacks it can top up for free), so the colony can't score for it.
        - `colony.loot_candidates(z)` — unclaimed sightings, richest first (a haul task's
          `pickup` IS the loot tile, so the blackboard already tracks who's on what; stale
          claims age out via _TASK_CLAIM_TTL and the pile frees up).
        - `colony.claim_loot(tile, bot, dropoff)` — atomic; returns None if the pile
          vanished or someone beat us. Optimistic concurrency: score freely, contend only
          at the claim, fall through to the next-best.
        - `client._claim_best_haul` — scores GOLD PER SECOND:
          `feasible_value x confidence / (walk_there + lift + carry_to_stash)`, where
          feasible_value comes from `plan_pickup` against this bot's real budgets. All
          three of value / value-per-weight / distance fall out of one number.
        - `colony.stash` (the farm sets it to TEMPLE) is where self-directed haulers
          deliver. The real answer is "nearest DEPOT" — depots are shared per character so
          staged hauls beat one long vendor trip — which needs depot positions + the
          merchant role.
      VERIFIED live with NO tasks posted: hauler spotted a pile 8 tiles off, claimed it
      ("~3 gold @ 0.3 gold/s"), walked, collected, delivered (3/2), then went idle.
      Two bugs found and fixed doing it:
        - **It scored without ensuring its bag was open**, so `free_slots` was 0,
          `plan_pickup` said "nothing fits", and the hauler stood on gold declining it
          forever. (The login-time `_open_backpack` is best-effort — the worn inventory can
          arrive late. That's now TWICE this has bitten; `_ensure_bag_open` at the point of
          use is the pattern that works.)
        - **Loot already AT the stash caused an infinite haul loop**: it lifted the pile,
          put it back on the same tile, saw it again, forever (10 tasks, same pile). A
          sighting on the stash tile is already home — skip it.

- [x] **Multi-pile trips (DONE 2026-07-15) — step 6, the hauler economy is complete.**
      The unit of work is now the TRIP, not the pile: going pile -> stash -> pile -> stash
      pays the walk home for every pile, which is absurd for a bot with 400k capacity and
      8 slots. A trip chains piles until a budget binds, then delivers once.
      The idea that makes it pay is the MARGINAL cost. Starting a trip, a pile costs the
      walk out AND the carry home (we'd not be going otherwise):
          seconds = d(me,P) + d(P,stash) + lift
      but once loaded and already heading home, that trip home is sunk, so one more pile
      only costs the DETOUR — the classic insertion cost:
          seconds = d(me,P) + d(P,stash) - d(me,stash) + lift
      ~0 for a pile on the way home, large for one in the opposite direction. Hence
      `_claim_best_haul(chaining=True)`, `_HAUL_MAX_DETOUR` (40 tiles) and `_HAUL_MAX_PILES`
      (5, so a trip always ends). "We're full, go home" needs no special check: once
      nothing fits, `plan_pickup` returns nothing and there's simply nothing left to claim.
      A pile's task is finished the moment it's collected (it IS collected); delivery is a
      separate phase, so a failed walk home doesn't lose the loot — we keep carrying and
      retry next lap.
      VERIFIED live, no tasks posted, 3 piles scattered at x=32374/32376/32378 with the
      stash at 32369: "claimed haul #1 (32374)" -> "chaining pile #2 (32376)" -> "chaining
      pile #3 (32378)" -> "trip done: 3 pile(s), delivered 3/3" — one walk home for three
      piles.
      WORTH KNOWING: it opened with the 2-gold boots rather than the 100-gold rod, because
      the rod (9 tiles out) was beyond `_LOOT_SCAN_RADIUS` (8) and so wasn't in the loot
      map yet — a hauler can't claim what nobody has seen. It then spotted the rod en
      route and chained it. So first claims are myopic and chaining quietly compensates;
      here the route was optimal anyway (18 tiles either way, the piles being in a line).
      If that myopia ever matters, the lever is the scan radius / more scouts feeding the
      map, not the scorer.

## Storage + selling — what 8.60 can actually reach (RESEARCHED 2026-07-15)

Corrected model (the "depot" is a PLACE, not an NPC): a Depot contains **Lockers**
(ITEM_LOCKER = 3497), which are containers holding four things — **Depot Chest** (~20
private Depot Boxes, ITEM_DEPOT_I = 22797…), **Supply Stash** (unlimited stacking, takes
whole containers at once, but refuses items that aren't `isItemStorable`), **Inbox** (mail
+ market deliveries) and **Market**. Stash and depot boxes are SAFE but PRIVATE — bots
cannot share items through storage.

- [x] **MARKET IS UNREACHABLE ON 8.60 — not a flag we can flip.** Every market packet is
      gated on `ProtocolFeature::MarketPackets`, and our profile simply doesn't have it:
        cipsoft860CanaryExtendedProfile.features = OldProtocolCompat | LegacyPayload |
            ExtendedSpriteFiles | MagicEffectU16 | InlineLoginBugReportFlag
      So the "haulers list below NPC price, sellers buy and flip to NPCs" idea can't run
      here. NOTE it would also be economically negative even if it worked: every bot is
      OURS, so gold moving between them is a zero-sum internal transfer, while the market
      charges fees on top — the only real income is the NPC sale at the end. The genuine
      problem it solves (privacy blocks storage-based transfer) has free answers on 8.60:
      a floor drop at a rendezvous (what we already do) or a direct trade.
- [x] **STASH DEPOSIT works on 8.60 (via a plain move); WITHDRAW does not.** The dedicated
      stash opcode is gated (`parseStashWithdraw` -> `if (oldProtocol) return;`), but
      stowing is ALSO implemented in the version-independent move path:
          if (isTryingToStow(toPos, toCylinder)) { player->stowItem(item, count, false); return; }
          isTryingToStow := toCylinder is a container whose item id == ITEM_LOCKER (3497)
                            AND toPos.z == ITEM_STASH_INDEX (1)
      No protocol gate. Since our `container_pos(cid, slot)` puts the slot in `z`, a stow
      is just `move_item(src, id, stack, container_pos(locker_cid, 1), count)` — the same
      0x78 we already use. Moving a whole CONTAINER there stows its contents.
- [x] **...WHICH MAKES THE STASH A BLACK HOLE — DO NOT STOW LOOT (corrected 2026-07-16).**
      Deposit-only is not a feature here, it's a trap: anything a hauler stows is gone
      until we migrate off 8.60. Since the entire point of hauling is to SELL the loot,
      stashing it is strictly worse than leaving it on the floor. The one-move
      "stow a whole container" trick is a nice capability with nothing to use it on.
      **Use DEPOT BOXES instead** — ordinary containers, so plain 0x78 moves work in BOTH
      directions, and they're reachable from any depot for that character. That is the
      real safe storage for 8.60. Stow stays documented only so nobody "optimises"
      `_deposit_loot` into it by accident.
- [x] **Depot Chest / Depot Boxes are ordinary containers** — open + move with the
      protocol we already have, and reachable from ANY depot for that character.
- [ ] **Design consequence: each bot should own its loot end-to-end; `stash = TEMPLE`
      should become "nearest Locker".** Because depot storage is per-character but
      reachable from any depot, one character can stash near the field, travel, withdraw
      near the vendor and sell — no inter-bot transfer, no market, no fees. The "seller"
      is a later PHASE of the same bot, not a different bot. Needs: locker positions
      (find them), then the NPC trade protocol for the actual selling.

### Follow-up findings (2026-07-16)

- [x] **The datapack alarm was FALSE — the economy catalog is valid.** The local checkout
      has no `otservbr.otbm` (only `canary.otbm`) and `data-canary` has 1 NPC, which made
      it look like the 295-shop catalog came from a datapack the server doesn't run. The
      LIVE container settles it: `dataPackDirectory = "data-otservbr-global"`,
      `mapName = "otservbr"`, and `data-otservbr-global/world/otservbr.otbm` is there at
      184,776,037 bytes. The 184 MB map is simply too big to keep in the working copy.
      So the otservbr NPCs we parsed ARE the live ones. No action needed.
- [x] **Thais depot lockers found** (by walking, not by parsing the OTBM):
      `(32352, 32231, z)` and `(32354, 32231, z)` for z = 7, 6, 5 — the building's three
      levels. Lockers are `unpass`, so a bot stands BESIDE one; the tiles due north,
      `(32352, 32230)` / `(32354, 32230)`, are the working spots. The depot's south face
      is solid wall at y=32232 — **the way in is from the north**.
- [x] **`is_stowable` is fully derivable offline — no live probing needed.** The chain:
      `Item::isStowable()` = `hasMarketAttributes() && !tier && wareId > 0 &&
      !isContainer() && wareId == id`, and `items.cpp` sets
      `wareId = object.flags().market().trade_as_object_id()` — i.e. straight out of
      **appearances.dat**, the file we already parse. Implemented in `items.py`
      (`AppearanceFlags.market = 36`, `AppearanceFlagMarket.trade_as_object_id = 2`,
      `container = 5`). Results: **4,827 stowable ids; 4,827 of 7,100 takeable ids**.
      Sanity checks all land:
        - leather boots / snakebite rod / magician's robe -> stowable
        - **gold / platinum / crystal coin -> NOT stowable** (no market wareId — nobody
          trades money for money; matches `CURRENCY_VALUE` having no NPC price row).
          `stowItem` special-cases only `ITEM_GOLD_POUCH`. **Coins must be carried.**
        - bag / chest -> not stowable (`!isContainer()`)
        - **lit candelabrum -> NOT stowable, plain candelabrum -> stowable** — the lit one
          decays, so it has no market entry. `!canDecay()` showing through.
      It's an UPPER BOUND: the parts we can't see (tier, owner, decay) are per-INSTANCE
      and only ever turn a True into a refusal. False is certain; True means "should work".
- [x] **Server status text is now captured** (`world.py` OP_TEXT_MESSAGE ->
      `FrameResult.messages` -> `GameSession.messages`, last 20). The server narrates
      every refusal in prose and NOWHERE else, so this was our only blind spot on failed
      moves. It paid off immediately: a silent pickup failure turned out to be
      "There is no way." (never reached the tile), and a stalled bot said "There is not
      enough room." Worth wiring into the hauler so a refusal teaches it something
      instead of just failing.
- [ ] **STOW-BY-MOVE IS STILL UNVERIFIED LIVE — blocked on navigation, not on protocol.**
      Everything above is source-derived; the one thing still missing is a bot standing
      next to a locker holding a stowable item. Four attempts failed for the SAME reason
      each time (see the nav item below), never because the stow itself was refused.
      When it runs, both outcomes are informative:
        - item leaves the bag and is NOT in the locker -> stow works;
        - **"This item cannot be stowed here." also PROVES the branch fires**, because that
          string exists only inside `stowItem()`, which is only reachable through
          `isTryingToStow` (a lit candelabrum is the ready-made negative control);
        - item merely appears inside the locker -> the branch did not fire.
      Scratch harness: `test_stow2.py`, `find_locker.py`, `map_depot.py`, `probe_move.py`.

- [x] **NAV BLOCKER FIXED (2026-07-16): bots now walk on the colony's shared map.**
      Root cause was two bugs stacked:
        1. `_walk_to` -> `find_path_toward` floods only `state.tiles` (what THIS bot has
           seen this session) and greedily heads for the reachable tile nearest the goal.
           A goal past the ~8-tile view isn't in the map at all -> zero steps, silent
           "stuck"; and "nearest reachable" is greedy, so in a maze it walks into the
           dead end closest to the goal and stops. Going AROUND a building means first
           moving AWAY from it, which greedy never does.
        2. **`travel` already planned a correct global route with `find_route` over the
           shared map — and then threw it away**, handing on-foot legs back to `_walk_to`.
           So we re-derived a blind local guess when a correct route was in hand. That was
           the actual bug; the shared map has been there all along.
      Fix: `nav.find_route` gained `reach` (stop within N tiles — a locker is `unpass`, so
      an exact-goal search returns None even though a bot can walk up and touch it) and
      `avoid` (hazards + the bot's own learned blocks; the shared walkable set only ever
      GROWS, so this is how local knowledge overrules it). New `client._walk_shared`
      FOLLOWS the shared route in autowalk chunks, re-planning on the shared map when a
      step is refused. `travel` uses it for both on-foot legs and shortcut-launch legs,
      and takes a `reach`; `_haul_travel` too. `_walk_to` is retained deliberately as the
      fallback: heading into UNEXPLORED space is exactly what the shared map can't help
      with, and greedy is the right instinct there. Known destination -> route it;
      unknown frontier -> feel your way.
      Verified live: round trips (temple -> west -> temple) arrive exactly on target in
      2-6s, on a bot that minutes earlier could not cross four tiles. Router unit-checked
      on a U-shaped barrier (it correctly walks away from the goal to get around), on
      reach=1 to an unwalkable goal, and on a genuinely-severed map (returns None rather
      than looping).
- [x] **Routing quality is now bounded by EXPLORATION, not by the planner.** `find_route`
      honestly answers "no route" when the shared map has no connected path — which is
      what it said for the Thais depot: `(32354, 32230)` is seen AND walkable, but sat in
      a pocket because no bot had walked the corridor linking it to the outside.
- [x] **Scouts don't map towns — they leave them (found + fixed 2026-07-16).** An
      unbounded scout always heads for the nearest GLOBAL frontier, and that is by
      definition the edge of the known region — so it beelines out of town and the map
      fills in as a thin line outward rather than a connected area. Measured: three
      unbounded scouts for 120s gave 9,522 walkable tiles and STILL no Temple->Depot
      route; a second run gave 1,427 tiles and a scout frozen 200+ tiles north at
      (32370, 32023). Coverage of any particular place was pure luck, which is why the
      depot approach kept not existing.
      Fix: `scout(center=, radius=)` -> `find_frontier`'s existing bounded mode (already
      built for wanderers, never wired to scouts) = SURVEY. A survey scout only takes
      frontiers inside the box and — critically — does NOT descend or hunt an exit when
      the box is done, since that is exactly how it escapes. Exposed via `scout_session`.
      Gotcha found live: a bot resumes wherever it logged out, so a survey scout starting
      outside its box finds no in-box frontier, reports "survey complete" and idles
      forever having never visited the area (observed: 298 tiles mapped). It now travels
      to the centre first.
- [x] **Unreachable loot is now kept OFF the map entirely (2026-07-16).** Tibia scenery is
      takeable-flagged and priced: the 3 crystal coins behind the Thais bank counter at
      `(32341, 32229, 7)` price at 30,000 gold and scored **634 gold/s** — so they
      outbid every real pile in town, and haulers queued to fail on them all session.
      They are decoration wearing a price tag. Two mechanisms, deliberately different
      kinds of evidence:
        - STRUCTURAL — `nav.has_standable_neighbour`: picking up needs range 1, so a pile
          with no standable tile among its 8 neighbours can NEVER be collected. Purely
          local (the bot saw the neighbours in the same view as the pile), so it can't
          confuse "unreachable" with "not explored yet" — the mistake a route-based test
          makes on a half-explored map. `_scan_loot` marks such tiles and never reports.
        - EXPERIENTIAL — `Colony.report_unreachable`: counts DISTINCT bots that failed to
          walk there and writes the tile off after `_UNREACHABLE_STRIKES = 2`. More than
          one bot, because a single failure says more about our navigation than the map.
      `Colony.mark_unreachable` drops the sighting, blocks re-reporting, filters
      `loot_candidates`, and PERSISTS in `learned_hazards.json` (a fact about the map;
      re-learning costs a wasted trip per restart). The coins are seeded there already —
      the user confirmed them unreachable, so there was no reason to pay to learn it.
      NOTE the structural test only catches FULLY sealed piles. Loot that has a standable
      neighbour but sits behind a locked door still needs the strikes path.
- [x] **Block inspector — a DEBUG overlay for the KINDS of "blocked" (2026-07-17).**
      There is no single "blocked" set; a tile can be un-walkable for several reasons that
      behave very differently, and they were all invisible. `is_walkable` is shared by
      `find_frontier` and `find_path_toward`, so the difference is NOT "which function" —
      it's the reason. The two functions DO differ in two ways worth seeing: `find_path_
      toward` refuses creature-occupied tiles and is radius-capped (~40); `find_frontier`
      ignores both. So a frontier can look reachable while the walker can't route there.
      Categories (colony `debug_tiles` -> `/debug.json` -> overlay, toggle ▦, default off,
      fetched only while on, scoped to the followed bot's window):
        structural  grey   — seen but unwalkable: wall/counter/door (`unpass`) or
                             water/lava ground. Global truth, `_explored` minus `_walkable`.
        runtime     amber  — the server refused THIS bot here (`blocked_tiles`), drawn with
                             remaining TTL. The layer that accumulates and boxes a scout in.
        hazard      purple — teleport/floor-change source (`_hazards`). Shared, no expiry.
        unreachable red    — written-off loot tile (`_unreachable`). Persisted.
        occupied    teal   — a creature is there now. `find_path_toward` avoids these,
                             `find_frontier` doesn't.
      Pixel-verified live (structural + occupied paint; endpoint returns correct counts).
      Extensions if useful: split structural into unpass-item vs impassable-ground; show
      the radius ring so the frontier/walker range mismatch is literally visible.
- [x] **Live creatures on the map (2026-07-17).** Every bot already parses creatures in
      view and prunes them to its viewport (`_prune_distant_creatures` + 0x6C removal), so
      the data is LIVE, not memory. Each bot ships its in-view creatures (id/x/y/z/kind/
      name/hp); the colony pools them into the snapshot, deduped by the server's creature
      id (two bots, one skunk = one dot), keeping the freshest sighting, windowed to the
      focus bot. Dashboard draws them by KIND — monster red diamond, player pale diamond,
      NPC blue square (a fixture, not a roamer). Wired the previously-stubbed ◈ toggle.
      KIND comes from the creature-id BAND, exact and offline (Canary src/creatures/*.cpp):
      players 0x10000000–0x50000000, monsters from 0x50000001, NPCs from 0x80000000 — no
      datapack name-match needed. Verified live: Bozo→npc, a player→player, 4 Skunks→
      monster; dedup + occupied + red rendering all confirmed. This is the layer that
      makes a bot boxed in by monsters (Druid 3's trap) obvious at a glance.

- [ ] **Creature MEMORY — decided design, not yet built.** Q: persist last-known creature
      positions, refresh on walk-by? Answer splits by kind, because they behave differently:
      - **NPCs: YES, remember + verify on walk-by.** Canary NPCs are stationary (stand at a
        post / pace 1–2 tiles), so a remembered position stays valid indefinitely; refresh
        when any bot passes, drop only if a bot is adjacent and it's gone (rare). Cheap,
        correct, and it's the seed for the merchant role ("nearest banker/shopkeeper") and
        for landmarks. The user's walk-by-verify model fits NPCs exactly.
      - **Monsters: NO, do not persist individual positions.** They move every turn, so a
        saved "Skunk at (x,y)" is wrong within seconds; trusting it as blocked poisons
        tiles that are now clear, trusting it as safe is dangerous. Right structure is a
        DECAYING danger/spawn heatmap keyed by AREA (spawns are fixed, so "this region has
        skunks" is stable) — feeds a future hunter role and lets scouts avoid death-traps.
        Bigger piece; separate future item.
      - **Players: no memory** — transient, not ours (may be the user).
- [x] **Latching position desync — FIXED (2026-07-17), likely a big mortality cause.**
      Found via the new creature overlay: Druid 3 showed frozen at ~(32304,32293) on the
      dashboard while actually stuck at (32321,32293) — ~17 tiles off — then looped
      "move in one direction" even after the user cleared the monsters blocking it.
      ROOT CAUSE: a 0x6D move carries no creature id, so `process_world_frame` decided
      "this move is us" purely by `old == state.position`. That LATCHES: the instant our
      tracked position drifts by one (a single 0x6D missed/misparsed during a chaotic
      creature encounter), every later move of ours fails the `old==pos` test, is blamed
      on "another creature" (`_relocate_creature`, a silent no-op when nothing is on
      `old`), and `state.position` FREEZES while the server keeps walking us. Downstream:
      `result.moved` stops firing (dashboard stalls, the exact stale-position symptom);
      `move_event` never sets, so the walker/escape steps all look FAILED though the
      server moved us (the single-direction retry loop); the watchdog then relogs a bot
      that was walking fine. This plausibly explains much of the freeze/relog mortality
      we fought all day — it triggers around monsters, which is where bots kept dying.
      FIX: the server sends a map slice (0x65–0x68) ONLY for OUR movement (the viewport
      scrolls), so at slice time `move_new` is our TRUE position, authoritative no matter
      what we believed. Adopt it there (`world.py`, slice dispatch): logs `RESYNC via
      slice` and re-adopts, breaking the latch the instant we next move. Belief-
      INDEPENDENT — unlike a first attempt keyed on expected step tiles, which are
      computed from the (already wrong) position and so can't match once drifted; that
      approach was removed. Unit-tested: realistic 5-tile drift heals via slice; synced
      moves unaffected; another creature's move (no slice) never mistaken for ours.
      Watch for: bots surviving creature encounters without the freeze→relog chain.
      **REVISED + STRENGTHENED (2026-07-17)** after the user reported bots "missing a row"
      of map and pacing the edge. Instrumented a bare-move dump: the culprit frames were
      12-byte BARE 0x6D moves (no slice), and Canary's `sendMoveCreature` (protocolgame.cpp)
      settles it authoritatively — the PLAYER's own move is ALWAYS a 0x6D followed by an
      edge slice (0x65-0x68 for any x/y change), while ANOTHER CREATURE's move is a bare
      0x6D with no slice. So the old "is this move us? match its `old` against our
      position" heuristic was wrong in BOTH directions once position drifted: our real
      move (old≠pos) got read as a creature's and froze us; AND a creature's bare move
      whose `old` happened to equal our drifted position dragged our tracked position
      along the CREATURE'S path — and since creature moves carry no slice, the rows our
      phantom position "crossed" were never revealed, stayed `unseen`, and read as
      frontier the bot paced forever (exactly the reported symptom). FIX: the slice is now
      the SOLE authority for our position. A 0x6D is deferred (`pending_move`); a slice
      claims it as ours (adopt `move_new`, RESYNC-log if `move_old`≠believed), anything
      else resolves it as a creature (`_relocate_creature`). The old-position self-match
      is gone entirely. Unit-tested: normal move quiet + advances; a creature's bare move
      whose old==our pos does NOT move us (relocates the creature); drift heals; batched
      [creature move][our move+slice] resolves both correctly. Grounded in server source,
      not inference.
- [ ] **Expose survey from the dashboard/manager** ("survey Thais", "survey around X").
      The role plumbing is in `scout`/`scout_session`; the manager still only knows
      unbounded scouts. This is how an operator opens up a region on demand.
- [ ] **Persist the colony's explored map.** `_save_knowledge` keeps hazards + links but
      NOT `_explored`/`_walkable`, so every observer restart re-learns the town from
      scratch and the first haul trips are blind again. The map is the colony's most
      expensive asset and the cheapest to store.
- [ ] **Druid 1 is stranded at (1119, 1094, 7)** — a ~20-tile walled cave nowhere near
      Thais, presumably through a teleport it stepped on. Harmless but it should be
      recovered (or the character reset), and it hints that bots wandering onto teleports
      need handling.

- [x] **Position desync / "missed row -> phantom frontier -> pace the edge" FIXED
      (2026-07-17).** Root cause, found by decoding a live frame the dashboard flagged:
      the server sends `0x6D` for BOTH our move and other creatures' moves. Ours is
      always followed by an edge slice (0x65-0x68, per Canary `sendMoveCreature` player
      branch); a creature's is a BARE `0x6D` with no slice. Old code guessed us-vs-creature
      by matching the `0x6D`'s `old` against our believed position — which latches BOTH
      ways: our real move misread as a creature's freezes our position (dashboard stalls,
      every walk step looks failed, watchdog relogs a bot that was walking fine); and a
      creature's bare move whose `old` happens to equal our already-drifted position drags
      us ALONG the creature's path — and since creature moves carry no slice, those rows
      never get `seen`, so `find_frontier` keeps luring the bot back to them (the pacing).
      Fix (world.py): the SLICE is the sole authority for our position. A `0x6D` is
      deferred (`pending_move`); a following slice claims it as ours and sets position,
      anything else resolves it as a creature (`_relocate_creature`). A bare `0x6D` never
      moves us. Drift now self-heals on the next real move instead of latching.
      Also handled `0x92` (creature walk-through) — a per-creature update the server can
      BATCH ahead of our move+slice; bailing on it dropped the slice and caused a 1-tile
      drift + a missed row. It's a fixed 5-byte consume.
      Verified live: single bot 420 moves -> 0 drift; two bots (the load case) 598 moves
      -> 1 self-healed drift, no pacing. Bare-move-old==us unit-tested to NOT follow.
      Residual: rare bails on `0xA1` (player skills) / `0xA2` (status icons) — our own,
      variable-length, not near movement, caused ~0 drift; left to bail-and-resync since
      mis-parsing their length would itself desync.
- [x] **Missed rows under load = we BAIL on unparsed opcodes, not packet loss (fixed
      2026-07-17).** TCP is lossless, so a real client never loses a slice — it just
      parses every opcode. We abandoned the rest of a network frame on the first opcode
      we didn't handle, and the server batches several game messages per frame, so a
      creature/combat update ahead of our move+slice dropped the slice -> a missed map
      row -> phantom frontier -> pacing. This is why 6 scouts near monsters still saw it.
      Fix (world.py): added consume handlers for the opcodes that fire around movement,
      byte layouts read from Canary protocolgame.cpp for OUR profile (oldProtocol=true,
      version=860, so the version>=953 / >=1059 conditional fields are absent):
        0x82 world light (2B) · 0x6B creature turn (13B) · 0x8C creature health (5B,
        also updates the creature's hp pip) · 0x8F creature speed (6B) · 0x90 skull (5B)
        · 0xA2 status icons (u16) · 0x84 damage/exp text (pos+colour+string).
      Method note: a mis-sized consume would CAUSE constant desync, so each addition was
      verified by re-running a 6-scout tally — right layouts make those opcodes leave the
      bail list AND keep resyncs ~0; a wrong one would spike resyncs or shift bails to
      garbage. Progression 77 bails -> 0 across the movement-relevant set; final run 887
      moves, **0 resyncs**. Chose this over a server-side `!resync` self-teleport
      talkaction — that would be an elevated capability a normal player lacks, and the
      user wants bots to obey normal-player rules. This fix is exactly what a real client
      does: parse everything.
      NOT exhaustive: only opcodes observed in Thais runs are handled. Others (outfit
      0x8E, light 0x8D, creature type 0x95, ...) will still bail if a bot hits a
      situation that sends them — but the move-attribution fix means any resulting drift
      SELF-HEALS on the next move, so the worst case is one rare transient missed row, not
      pacing. Add handlers as new opcodes surface. `0xA1` (skills) left bailing: login-
      only, not near movement, too branchy to risk.

## Phase E — roles (the dream)

- [ ] Explorer (formalised), Hauler (loot → shop), Supplier (potions → hunter),
      Hunter (combat: targeting/attack/heal/loot). Hunter needs the most new
      protocol (attack, health mgmt, looting).
