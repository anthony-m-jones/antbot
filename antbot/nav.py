"""Navigation — Phase B. Walkability and A* pathfinding over the parsed map.

This turns the sparse tile map that `world.py` builds (state.tiles) into routes
the bot can actually walk. B1 (this file) plans a path across the *currently
known* area — the ~18x14 window we parsed at login. B2/B3 will keep that map live
as the bot moves and let goals sit beyond the current view.

COORDINATES AND DIRECTIONS
Tibia's axes: +x is east, +y is *south* (y increases downward), z is the floor.
So the four cardinal steps map to coordinate deltas as below, and the names match
`client.DIRECTION_OPCODES` so a planned path can be handed straight to the walker.
"""

from __future__ import annotations

import heapq
import logging

from .items import ItemFlags
from .world import GameState, tile_ids

log = logging.getLogger("antbot")

# (name, dx, dy) for the four cardinal steps. Names match DIRECTION_OPCODES.
# north = y-1, south = y+1, east = x+1, west = x-1.
_STEPS: list[tuple[str, int, int]] = [
    ("north", 0, -1),
    ("east", 1, 0),
    ("south", 0, 1),
    ("west", -1, 0),
]


# Ground speed we assume when a tile's ground declares no `bank` (ramps and stairs are
# the usual case). Pessimistic on purpose: guessing "fast" would make the planner favour
# exactly the tiles it knows least about.
DEFAULT_GROUND_SPEED = 150
# The quickest ground in the game (stone tile / wooden floor / road = 100). Used to keep
# the A* heuristic admissible once steps cost real time: the cheapest a step can ever be.
MIN_GROUND_SPEED = 100

# Dedicated logger for the Dijkstra/A* routing decisions themselves (which tiles get
# considered, what an unconfirmed z-hop is priced at, which one — if any — wins). Kept
# SEPARATE from the module's normal "antbot" logger so this can be switched on with
# `logging.getLogger("antbot.route").setLevel(logging.DEBUG)` without drowning in every
# other subsystem's routine INFO noise. Off (no handler => no output) by default.
route_log = logging.getLogger("antbot.route")


def unconfirmed_crossing_cost(tile: tuple[int, int, int], goal_x: int, goal_y: int) -> int:
    """The OPTIMISTIC lower-bound cost of gambling on an unconfirmed STEP-type floor-
    change (a hole/open-stairs/teleporter we've SEEN but never actually crossed) as a
    shortcut to (goal_x, goal_y), instead of its ordinary ground cost.

    Why this exists: a STEP-type object relocates you the instant you walk onto it —
    unlike a ladder (USE-type, needs a deliberate action), there's no way to merely pass
    near one. So an ordinary same-floor walk that doesn't know any better can stumble
    through one by accident while just trying to close 2D distance to a goal, taking it
    somewhere else entirely. Blocking such tiles outright breaks routing in map regions
    that are genuinely staircase-dense (there may be little ordinary ground between
    them); pricing them at a flat penalty risks steering AWAY from a route that's
    actually optimal. Neither is honest, because we don't know what's on the other side.

    This is the answer: charge the CHEAPEST it could possibly cost to actually pay off —
    one hop up/down (there), the best-case straight-line distance from here to the goal
    at the fastest ground in the game, and one hop back (here) if a landing conveniently
    close to the goal existed. That's a genuine ADMISSIBLE lower bound (nothing crosses
    ground faster, and no route through an unconfirmed crossing can beat "instant
    teleport to right next to the goal"), so using it as this tile's cost can only ever
    make the search UNDER-charge the gamble, never bias away from a route that's truly
    better. A confirmed route that's genuinely cheaper still wins; an unconfirmed
    crossing only "wins" the search when even its most generous case beats the known
    alternative — worth investigating, not free, not forbidden.

    Once a crossing is CONFIRMED (actually walked), it becomes a real `_links` entry and
    this no longer applies — the router prices it at the flat, trusted link-hop cost
    instead, wherever it truly leads.
    """
    return 2 * MIN_GROUND_SPEED + (abs(tile[0] - goal_x) + abs(tile[1] - goal_y)) * MIN_GROUND_SPEED


def tile_cost(state: GameState, item_flags: ItemFlags, x: int, y: int, z: int) -> int:
    """What it COSTS to step onto (x, y, z) — Tibia's `groundSpeed` for its ground.

    Beware the inversion: groundSpeed is a DURATION, not a speed. The server computes a
    step as `groundSpeed * 1000 / playerSpeed` ms, so BIGGER means SLOWER. A road is 100,
    grass 150, sand 160, ocean floor 250 — walking a sand dune costs 60% more time than
    the road beside it.

    Planning with this instead of "every step costs 1" is what makes bots use the roads.
    That matters most for EXPLORING: a scout's budget is time, not steps, so preferring
    fast ground means more tiles revealed per minute — and it naturally sequences the
    work, mapping a town's streets before wandering off into the countryside, because
    the cheap frontiers are all on the fast ground.

    The ground is the tile's bottom item; anything above it (a wall, a rug, a pile of
    loot) doesn't change what the floor costs to cross.
    """
    items = state.tiles.get((x, y, z))
    if not items:
        return DEFAULT_GROUND_SPEED
    speed = item_flags.ground_speed(items[0][0])
    return speed if speed else DEFAULT_GROUND_SPEED


def is_walkable(state: GameState, item_flags: ItemFlags, x: int, y: int, z: int,
                ignore_blocked: bool = False) -> bool:
    """Can the bot stand on tile (x, y, z), based on what we've parsed?

    A tile is walkable if we know it has contents (so it has a ground tile — void
    we've never seen is treated as not walkable) and none of its items carry the
    `unpass` blocking flag (walls, closed doors, rocks, water edges, ...).

    Note this does not consider creatures — the pathfinder handles those
    separately, so callers can reason about terrain and occupancy independently.

    `ignore_blocked=True` skips the transient `blocked_tiles` layer and judges TERRAIN
    only. The break-out maneuver uses this: when a scout has walled itself in with
    blocked tiles (many of them stale — other bots that have since moved), it must be
    able to ask "is this a real wall, or just something we blocked earlier?" and try
    stepping onto the latter.
    """
    if not ignore_blocked and (x, y, z) in state.blocked_tiles:
        return False  # the server refused this tile before; don't retry it
    items = state.tiles.get((x, y, z))
    if not items:
        return False  # unknown / void: no ground to stand on
    # Impassable liquid ground (open water / lava). The ground is the bottom of the
    # stack; appearances.dat doesn't reliably flag these `unpass`, so without this a
    # bot treats the sea as walkable and floods across it (peninsula stuck-loop).
    # Tiles are [(id, count), ...] — walkability only ever cares about the ids.
    if items[0][0] in getattr(item_flags, "impassable_ground", ()):
        return False
    return not any(item_flags.is_blocking(item_id) for item_id, _ in items)


def has_standable_neighbour(state: GameState, item_flags: ItemFlags,
                            tile: tuple[int, int, int]) -> bool:
    """Could a bot ever stand close enough to touch `tile`?

    Picking something up needs range 1 (Chebyshev — diagonals count), so a pile is only
    ever fetchable if the tile itself or one of its eight neighbours is standable. Loot
    walled inside scenery — the crystal coins behind the Thais bank counter, say — has
    no standable tile anywhere around it and can NEVER be collected. It is map
    decoration wearing a price tag.

    This matters because such a pile is worse than useless to the loot map: our scorer
    ranks by value, so a decorative pile of crystal coins outranks every real pile in
    town and haulers queue up to fail on it forever.

    Deliberately LOCAL: it only looks at the eight neighbours, all of which the bot saw
    in the same view as the pile itself. So it never confuses "unreachable" with "we
    haven't explored the way there yet" — the mistake a route-based test would make on
    a half-explored map. A tile we've never seen doesn't count as standable, which is
    the safe direction: it just means we don't judge until we've looked.
    """
    x, y, z = tile
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if is_walkable(state, item_flags, x + dx, y + dy, z, ignore_blocked=True):
                return True
    return False


def find_path_on_floor(state: GameState, item_flags: ItemFlags,
                        goal_x: int, goal_y: int) -> list[str] | None:
    """A* from our current position to (goal_x, goal_y) on the same floor.

    Returns the list of step directions to walk (e.g. ["north", "north", "east"]),
    an empty list if we are already there, or None if no route exists within the
    known map. Same-floor only for B1 — stairs/ramps come with floor handling
    later.

    Creatures block: we won't route *through* a tile occupied by another
    creature, though the goal tile itself is allowed to be occupied (you might be
    pathing toward one).
    """
    if state.position is None:
        return None
    z = state.position.z
    start = (state.position.x, state.position.y)
    goal = (goal_x, goal_y)

    if start == goal:
        return []

    # Tiles occupied by other creatures — treated as blocked (except the goal).
    occupied = {
        (c.position.x, c.position.y)
        for c in state.nearby_creatures()
        if c.position is not None and c.position.z == z
    }

    # The goal must be reachable terrain, otherwise there's nothing to plan to.
    if not is_walkable(state, item_flags, goal_x, goal_y, z):
        log.debug("goal (%d, %d, %d) is not walkable terrain", goal_x, goal_y, z)
        return None

    def heuristic(node: tuple[int, int]) -> int:
        # Manhattan distance, priced at the FASTEST ground in the game. Steps now cost
        # real time (see tile_cost), so the estimate has to be in the same units as g —
        # and it must never overstate the true remaining cost or A* can return a
        # non-optimal path. No route can beat MIN_GROUND_SPEED per step, so this is the
        # tightest admissible bound available.
        return (abs(node[0] - goal[0]) + abs(node[1] - goal[1])) * MIN_GROUND_SPEED

    # Standard A*. open_heap entries are (f_score, g_score, node).
    open_heap: list[tuple[int, int, tuple[int, int]]] = [(heuristic(start), 0, start)]
    # For each reached node, remember (previous_node, direction_taken) to rebuild.
    came_from: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    best_g: dict[tuple[int, int], int] = {start: 0}

    while open_heap:
        _, g, current = heapq.heappop(open_heap)
        if current == goal:
            return _reconstruct(came_from, current)
        if g > best_g.get(current, g):
            continue  # a better path to `current` was already processed
        for name, dx, dy in _STEPS:
            neighbor = (current[0] + dx, current[1] + dy)
            if neighbor != goal:
                if not is_walkable(state, item_flags, neighbor[0], neighbor[1], z):
                    continue
                if neighbor in occupied:
                    continue
            tentative = g + tile_cost(state, item_flags, neighbor[0], neighbor[1], z)
            if tentative < best_g.get(neighbor, 1 << 30):
                best_g[neighbor] = tentative
                came_from[neighbor] = (current, name)
                heapq.heappush(open_heap, (tentative + heuristic(neighbor), tentative, neighbor))

    return None  # exhausted the known area without reaching the goal


def find_frontier(state: GameState, item_flags: ItemFlags,
                  max_search: int = 8000,
                  center: tuple[int, int] | None = None,
                  radius: int | None = None,
                  skip: set[tuple[int, int]] | None = None,
                  shared_seen: set[tuple[int, int, int]] | None = None
                  ) -> tuple[int, int] | None:
    """Find the walkable tile that borders UNEXPLORED space and is QUICKEST to reach.

    This is what makes exploration purposeful instead of random: it flood-fills the
    known walkable area from our position and returns the closest tile that has a
    neighbour we've NEVER SEEN (a slot not in `state.seen` — as opposed to a wall or
    open air over a cliff, both of which we HAVE seen: the wall is a blocking tile, the
    void is a slot the server described as empty). Testing against `seen` rather than
    `state.tiles` is what stops a scout running a ledge forever, mistaking the open air
    past it for unexplored land. Walking to a real frontier reveals new map; the next
    frontier is further out, so the mapped region steadily expands outward.

    "Closest" means closest in TIME, not in tiles: the flood is a Dijkstra weighted by
    `tile_cost`, so the frontier we pick is the one we can actually get to soonest. A
    scout's budget is time, so this straightforwardly buys more map per minute. It also
    ORDERS the work the way you'd want by itself — road frontiers are cheap and get
    taken first, so a town's streets fill in before anyone wanders off across the
    fields, and the slow off-road frontiers are left until the fast ones run out. No
    explicit "do towns first" rule needed; it falls out of costing time honestly.

    Two modes:
      - Scout (center=None): flood the whole reachable floor, capped at
        `max_search` tiles. Returns the globally-nearest frontier so scouts range
        outward without bound.
      - Wanderer (center + radius given): only consider — and only flood toward —
        tiles within `radius` Manhattan of `center` (the bot's home/temple). This
        keeps the colony clustered near spawn AND bounds the search to a small box,
        so it stays cheap even after the bot has accumulated a huge tile map. When
        the home box is fully mapped it returns None, and the caller falls back to
        idle milling.

    Returns the (x, y) of that frontier tile on the current floor, or None if the
    reachable (and, for a wanderer, in-bounds) area is fully mapped.
    """
    if state.position is None:
        return None
    z = state.position.z
    start = (state.position.x, state.position.y)

    def within(pt: tuple[int, int]) -> bool:
        # No bound for scouts; a Manhattan box around home for wanderers.
        if center is None or radius is None:
            return True
        return abs(pt[0] - center[0]) + abs(pt[1] - center[1]) <= radius

    skip = skip or set()
    # Dijkstra over travel TIME (see tile_cost), so the first frontier we pop is the one
    # reachable soonest — not merely the one fewest tiles away. With a uniform cost this
    # degenerates to the breadth-first search it replaced.
    best_cost: dict[tuple[int, int], int] = {start: 0}
    heap: list[tuple[int, tuple[int, int]]] = [(0, start)]
    settled: set[tuple[int, int]] = set()
    while heap and len(settled) < max_search:
        cost, (cx, cy) = heapq.heappop(heap)
        if (cx, cy) in settled:
            continue
        settled.add((cx, cy))
        # Is this a frontier tile — does it touch anything unseen? (Only tiles that
        # are themselves in-bounds count, so a wanderer doesn't get lured outward;
        # and not one the caller has ruled out as unreachable-in-practice via `skip`.)
        if within((cx, cy)) and (cx, cy) not in skip:
            for _name, dx, dy in _STEPS:
                nb = (cx + dx, cy + dy, z)
                # A neighbour is a real frontier only if NOBODY has seen it — our own
                # view OR the shared swarm view. Flooding still uses our OWN reachable
                # walkable tiles (so the target is reachable), but a tile another bot has
                # already revealed no longer lures us here. That's feature A: reachability
                # from us, resolution from the whole colony.
                if nb not in state.seen and (shared_seen is None or nb not in shared_seen):
                    return (cx, cy)
        # Otherwise keep flooding over walkable neighbours, but never expand past
        # the bound — that's what both keeps wanderers home and caps the work.
        for _name, dx, dy in _STEPS:
            nxt = (cx + dx, cy + dy)
            if nxt in settled or not within(nxt):
                continue
            if not is_walkable(state, item_flags, nxt[0], nxt[1], z):
                continue
            nd = cost + tile_cost(state, item_flags, nxt[0], nxt[1], z)
            if nd < best_cost.get(nxt, 1 << 30):
                best_cost[nxt] = nd
                heapq.heappush(heap, (nd, nxt))
    return None


def find_nearest_step_toward(state: GameState, item_flags: ItemFlags,
                              goal_x: int, goal_y: int,
                              max_radius: int | None = None,
                              extra_walkable: set[tuple[int, int, int]] | None = None,
                              registry=None,
                              confirmed_links: set[tuple[int, int, int]] | None = None,
                              unconfirmed_crossings: set[tuple[int, int, int]] | None = None,
                              ) -> list[str] | None:
    """Path *toward* (goal_x, goal_y), even if it's beyond what we've seen.

    This is what makes `goto` work across screens (B3). The goal tile usually
    isn't in the known map yet, so exact A* would give up. Instead we flood the
    known walkable area from our position and head for the reachable tile that
    gets us *closest* to the goal. Walking there scrolls new map in via the edge
    slices; the next call floods the now-larger map and gets closer still, and so
    on until the goal itself becomes reachable.

    `max_radius` bounds the flood to a Manhattan box of that many tiles around our
    current position. This matters for PERFORMANCE: callers that re-plan every step
    (the explorer/scout) only ever consume the *first* step of the returned path,
    so there is no point flooding the entire accumulated tile map — which grows
    without bound as a bot explores. A local flood of radius ~40 gives more than
    enough lookahead to route around walls, is self-correcting (we re-plan from the
    new position each step), and turns per-step planning from O(map) into O(radius²)
    — the key to running many bots at once. Leave it None (the default) for `goto`
    /`travel`, which want exact long-distance routing to a specific tile.

    `extra_walkable` lets the flood cross tiles THIS bot hasn't personally seen but
    the COLONY knows are walkable (pass `colony.walkable_ref()`). Without it the flood
    is limited to `state.tiles` — a private, one-session view — so a bot that just
    relogged (empty private map) or whose only route home runs across ground a teammate
    mapped would find nothing and falsely report "no route", even though a path plainly
    exists on the shared map. We still test the private map FIRST (first-hand and more
    current, incl. live creatures), and — crucially — we still honour `blocked_tiles`:
    a tile the server refused this bot stays refused even if the shared map likes it, so
    this can't re-open the very trap the bot just backed out of. The flood stays bounded
    by `max_radius`, so handing in the whole shared set costs only O(1) membership tests.

    `registry` + `confirmed_links` (pass `colony.traversal` / `set(colony.get_links())`)
    price an UNCONFIRMED STEP-type floor-change (a hole/open-stairs/teleporter we've SEEN
    but never crossed) at `unconfirmed_crossing_cost` instead of its ordinary ground cost —
    see that function for why. Without this, an ordinary walk toward (goal_x, goal_y) can
    silently step onto one mid-route (it's not blocking, so `is_walkable` allows it) and
    get relocated somewhere else entirely. Omit both (the default) to skip this pricing
    entirely — used by callers with no colony to consult.

    `unconfirmed_crossings` (pass `colony.get_unconfirmed_crossings()`) is a SEPARATE, cheaper check
    for the SAME kind of tile, and matters for a gap `registry`/`confirmed_links` alone
    can't close: those two can only classify a tile whose CONTENTS are already in `state.
    tiles` — this bot's own, private view. A tile reachable only through `extra_walkable`
    (the colony's shared walkable set) may be a hazard some OTHER bot already discovered
    and reported, with nothing in this bot's own view to classify at all — silently falling
    through as ordinary ground otherwise. `colony.get_unconfirmed_crossings()` is already a plain
    tile membership set (no item stack needed to consult it), built the same way for
    `find_shared_route`, so passing it here gives the local walker the SAME colony-wide hazard
    awareness the shared router already has, not just what this bot personally happened to
    see. Checked first (cheaper, and authoritative — the colony set is already maintained to
    drop a tile the moment it's confirmed as a real link); local classification is the
    fallback for anything only this bot has ever seen.

    LOGGING: emits to the `antbot.route` logger — see `find_shared_route`'s docstring and
    `nav.route_log`.

    Returns the directions toward that best frontier tile, or None if we can't get
    any closer (blocked in, or the goal's direction is walled off in known space).
    """
    if state.position is None:
        return None
    z = state.position.z
    start = (state.position.x, state.position.y)
    goal = (goal_x, goal_y)

    occupied = {
        (c.position.x, c.position.y)
        for c in state.nearby_creatures()
        if c.position is not None and c.position.z == z
    }

    # Dijkstra flood over known walkable tiles, weighted by travel TIME (`tile_cost`),
    # so the route we hand back prefers roads over sand even when that costs a tile or
    # two. Track the reachable tile with the smallest Manhattan distance to the goal —
    # closeness to the goal is still measured in tiles, since the goal is usually beyond
    # the known map and we have no idea what the ground out there costs. Only the
    # ROUTE is time-optimal. (With a uniform cost this degenerates to the breadth-first
    # search it replaced.)
    came_from: dict[tuple[int, int], tuple[tuple[int, int], str]] = {}
    best_cost: dict[tuple[int, int], int] = {start: 0}
    heap: list[tuple[int, tuple[int, int]]] = [(0, start)]
    settled: set[tuple[int, int]] = set()
    best = start
    best_dist = abs(start[0] - goal[0]) + abs(start[1] - goal[1])
    debug = route_log.isEnabledFor(logging.DEBUG)
    gambled_tiles: set[tuple[int, int]] = set()

    while heap:
        cost, current = heapq.heappop(heap)
        if current in settled:
            continue
        settled.add(current)
        distance = abs(current[0] - goal[0]) + abs(current[1] - goal[1])
        # A gambled-onto tile (an unconfirmed z-hop) must NEVER become the walk-toward
        # TARGET on raw geometric proximity alone. This selection is distance-only —
        # it doesn't look at cost at all — so an expensive gamble that happens to sit
        # geometrically nearer the goal (very possible: the real route often has to
        # detour AWAY from the goal first to get around a wall) would otherwise win by
        # sheer coordinates despite `unconfirmed_crossing_cost` correctly pricing it as
        # expensive. That priced-but-still-selected combination is exactly the bug this
        # test isolated: the gamble cost only ordered WHEN Dijkstra visited it, never
        # whether it got PICKED. Crossing one on purpose stays `_try_change_floor`'s job;
        # ordinary walking should just report "no closer ordinary tile" and let the
        # caller's stuck-handling escalate to that deliberate hunt instead.
        if distance < best_dist and current not in gambled_tiles:
            best_dist, best = distance, current
        if current == goal:
            best = current
            break
        for name, dx, dy in _STEPS:
            neighbor = (current[0] + dx, current[1] + dy)
            if neighbor in settled:
                continue
            # Bounded flood: never expand past `max_radius` from where we stand.
            if max_radius is not None and (
                    abs(neighbor[0] - start[0]) + abs(neighbor[1] - start[1]) > max_radius):
                continue
            if not is_walkable(state, item_flags, neighbor[0], neighbor[1], z):
                # Fall back to the colony's shared knowledge for tiles we haven't seen
                # first-hand — but never for a tile the server refused US (blocked_tiles),
                # or we'd walk straight back into the trap we just escaped.
                if (extra_walkable is None
                        or (neighbor[0], neighbor[1], z) not in extra_walkable
                        or (neighbor[0], neighbor[1], z) in state.blocked_tiles):
                    continue
            if neighbor != goal and neighbor in occupied:
                continue
            n_tile = (neighbor[0], neighbor[1], z)
            # Colony-wide knowledge first (cheap membership test, no item stack needed —
            # this is what catches a hazard some OTHER bot discovered that THIS bot has
            # never personally seen). Fall back to classifying our OWN view for anything
            # only we have laid eyes on, which the shared set wouldn't know about yet.
            if unconfirmed_crossings is not None and n_tile in unconfirmed_crossings:
                gamble = True
            else:
                gamble = False
                if (registry is not None
                        and (confirmed_links is None or n_tile not in confirmed_links)):
                    items = state.tiles.get(n_tile)
                    if items:
                        hit = registry.classify(tile_ids(items))
                        if hit is not None:
                            kind = registry.kind(hit[0])
                            if kind is not None and kind.action == "step":
                                gamble = True
            step = (unconfirmed_crossing_cost(n_tile, goal_x, goal_y) if gamble
                   else tile_cost(state, item_flags, neighbor[0], neighbor[1], z))
            nd = cost + step
            improved = nd < best_cost.get(neighbor, 1 << 30)
            if debug:
                route_log.debug(
                    "  relax %s -%s-> %s  step=%d (%s)  total=%d%s",
                    (current[0], current[1], z), name, n_tile, step,
                    "GAMBLE unconfirmed z-hop" if gamble else "ground", nd,
                    " [IMPROVED]" if improved else " [no improvement, skipped]")
            if improved:
                best_cost[neighbor] = nd
                came_from[neighbor] = (current, name)
                heapq.heappush(heap, (nd, neighbor))
                if gamble:
                    gambled_tiles.add(neighbor)
                else:
                    gambled_tiles.discard(neighbor)

    if best == start:
        if debug:
            route_log.debug("find_nearest_step_toward: nothing reachable is closer than %s to %s",
                            start, goal)
        return None  # nothing reachable is closer than where we already stand
    path = _reconstruct(came_from, best)
    if best in gambled_tiles:
        route_log.info("find_nearest_step_toward: DECIDED to head for an unconfirmed z-hop at "
                       "%s (best reachable tile toward %s from %s)", best, goal, start)
    return path


def find_use_object(state: GameState, item_flags: ItemFlags, registry,
                    skip: set[tuple[int, int, int]] | None = None,
                    max_dist: int | None = None,
                    only: set[str] | None = None):
    """Find the nearest tile on our floor carrying a known USE-based object.

    `only`, if given, restricts to those categories — e.g. {"door_unlocked"} to hunt only
    doors. Directed navigation uses this: it wants to OPEN a door blocking the route, but
    must NOT "use" a ladder/grate (which would relocate it off its path).

    "USE-based" = the traversal registry classifies it into a category whose action
    is anything other than a plain STEP (ladders, grates, wells, doors, shovel/rope
    holes). Step-based objects (teleporters/stairs/open holes) are already handled
    as ordinary walk-onto links, so we skip them here.

    This is how a scout turns the seeded catalog knowledge into actual shortcuts: it
    walks to one of these objects and uses it (see client `_perform_traversal`),
    which — if it relocates us — gets recorded as a link the whole colony can route
    through afterwards.

    Returns `(source, item_id, category, launch, direction)` where `source` is the
    object's (x, y, z), `launch` is an adjacent walkable tile to stand on, and
    `direction` is the cardinal name from launch to source; or None if there's no
    reachable use-object we haven't already ruled out (`skip`). We pick by Manhattan
    distance and only require a walkable neighbour — the caller does the real
    navigation, so we keep this scan cheap (no pathfinding per candidate).
    """
    if state.position is None:
        return None
    skip = skip or set()
    px, py, z = state.position.x, state.position.y, state.position.z

    # With a small `max_dist` (the scout's per-round on-sight check), scan only the
    # box around us instead of the whole accumulated tile map — O(max_dist²) rather
    # than O(map), so it's cheap to run every round even with a huge explored area.
    if max_dist is not None and max_dist <= 30:
        candidates = (
            ((x, y, z), state.tiles.get((x, y, z)))
            for x in range(px - max_dist, px + max_dist + 1)
            for y in range(py - max_dist, py + max_dist + 1)
        )
        candidates = ((key, items) for key, items in candidates if items)
    else:
        candidates = state.tiles.items()

    best = None
    best_dist = 1 << 30
    for (x, y, tz), items in candidates:
        if tz != z or (x, y, tz) in skip:
            continue
        hit = registry.classify(tile_ids(items))   # the registry keys on item identity
        if hit is None:
            continue
        category, item_id = hit
        kind = registry.kind(category)
        if kind is None or kind.action == "step":   # step objects aren't "used"
            continue
        if only is not None and category not in only:
            continue
        dist = abs(x - px) + abs(y - py)
        if dist == 0 or dist >= best_dist:
            continue
        if max_dist is not None and dist > max_dist:
            continue   # too far to be worth a detour (caller only wants NEARBY ones)
        # Need somewhere adjacent and walkable to stand while using it.
        for name, dx, dy in _STEPS:
            launch = (x + dx, y + dy)
            if is_walkable(state, item_flags, launch[0], launch[1], z):
                # `direction` is launch -> source, i.e. the opposite of this step.
                opposite = {"north": "south", "south": "north",
                            "east": "west", "west": "east"}[name]
                best = ((x, y, z), item_id, category, (launch[0], launch[1], z), opposite)
                best_dist = dist
                break
    return best


def find_descent(state: GameState, item_flags: ItemFlags, registry,
                 skip: set[tuple[int, int, int]] | None = None,
                 max_dist: int | None = None, direction: str = "down"):
    """Find the nearest tile on our floor that takes us in `direction` (down/up) a floor.

    The deliberate floor-change hunt: any object whose direction matches — for `down`, a
    hole / trapdoor / ladder-hole-down / down-stair you STEP onto, or a grate / well you
    USE; for `up`, an up-staircase you step onto (USE-ladders up are already found by
    `find_use_object`). (We used to restrict this to STEP-down, which stranded a scout
    both above a grate-only sewer AND in a room whose only way out was up-stairs.)
    `find_use_object` is category-agnostic and grabs the nearest object of ANY kind; this
    one filters by direction so a floor-change is sought ON PURPOSE — what a scout needs
    when a room's only exits lead off this floor. The caller traverses it the right way
    (step vs use) via `_perform_traversal`.

    Why we don't just let normal pathfinding walk over the hole: once a bot has fallen
    through a hole we mark that tile blocked (a hazard), so the planner routes AROUND
    it forever after. To go down on purpose we must target the hole explicitly and
    step onto it with `_raw_step` (which ignores the block), which is what the caller
    does with the `(source, launch, direction)` this returns.

    Returns `(source, item_id, category, launch, direction)` — same shape as
    `find_use_object`: `source` is the hole's (x, y, z), `launch` an adjacent walkable
    tile to stand on, `direction` the cardinal name from launch onto the hole; or None
    if there's no reachable descent tile we haven't already ruled out via `skip`.
    """
    if state.position is None:
        return None
    skip = skip or set()
    px, py, z = state.position.x, state.position.y, state.position.z

    # Same cheap box-vs-whole-map scan as find_use_object (see there for the why).
    if max_dist is not None and max_dist <= 30:
        candidates = (
            ((x, y, z), state.tiles.get((x, y, z)))
            for x in range(px - max_dist, px + max_dist + 1)
            for y in range(py - max_dist, py + max_dist + 1)
        )
        candidates = ((key, items) for key, items in candidates if items)
    else:
        candidates = state.tiles.items()

    best = None
    best_dist = 1 << 30
    for (x, y, tz), items in candidates:
        if tz != z or (x, y, tz) in skip:
            continue
        hit = registry.classify(tile_ids(items))   # the registry keys on item identity
        if hit is None:
            continue
        category, item_id = hit
        kind = registry.kind(category)
        # Anything that takes us in `direction`, however you traverse it: filter by
        # DIRECTION, not action, so a sewer grate (USE-down) or an up-staircase (STEP-up)
        # is a first-class floor-change target, deliberately sought (this hunt reaches
        # _DESCENT_RADIUS=30) rather than merely stumbled on by find_use_object. The caller
        # dispatches step-vs-use via _perform_traversal. Sideways teleporters are excluded.
        if kind is None or kind.direction != direction:
            continue
        dist = abs(x - px) + abs(y - py)
        if dist == 0 or dist >= best_dist:
            continue
        if max_dist is not None and dist > max_dist:
            continue
        for name, dx, dy in _STEPS:
            launch = (x + dx, y + dy)
            if is_walkable(state, item_flags, launch[0], launch[1], z):
                opposite = {"north": "south", "south": "north",
                            "east": "west", "west": "east"}[name]
                best = ((x, y, z), item_id, category, (launch[0], launch[1], z), opposite)
                best_dist = dist
                break
    return best


def _reconstruct(came_from: dict[tuple[int, int], tuple[tuple[int, int], str]],
                 node: tuple[int, int]) -> list[str]:
    """Walk the came_from chain back to the start, yielding directions in order."""
    directions: list[str] = []
    while node in came_from:
        prev, name = came_from[node]
        directions.append(name)
        node = prev
    directions.reverse()
    return directions


def within_reach(node: tuple[int, int, int], goal: tuple[int, int, int],
                 reach: int) -> bool:
    """Is `node` close enough to `goal` to act on it?

    Chebyshev distance on the same floor, because that's the server's own rule for
    reaching a thing: diagonals count, so the eight tiles around an object are all
    "range 1". `reach=0` means standing exactly on it.
    """
    return (node[2] == goal[2]
            and max(abs(node[0] - goal[0]), abs(node[1] - goal[1])) <= reach)


def find_shared_route(walkable: set[tuple[int, int, int]],
                      links: dict[tuple[int, int, int], tuple[int, int, int]],
                      start: tuple[int, int, int],
                      goal: tuple[int, int, int],
                      reach: int = 0,
                      avoid: set[tuple[int, int, int]] | None = None,
                      costs: dict[tuple[int, int, int], int] | None = None,
                      unconfirmed_crossings: set[tuple[int, int, int]] | None = None,
                      ) -> list[tuple[str, bool, tuple[int, int, int]]] | None:
    """Route across the *whole known world*, using teleports/stairs as shortcuts.

    Unlike `find_path_on_floor` (single floor, on-foot only), this searches the colony's
    entire shared walkable map plus the learned links, so a far goal on another
    floor or in another region is reachable by stepping through a teleport. Nodes
    are absolute (x, y, z); edges are:
      - a cardinal step to an adjacent walkable tile (cost 1), and
      - stepping onto a link source tile, which lands you at its destination
        (cost 1) — the teleport/stairs used as a highway.

    We use Dijkstra rather than A*: a teleport can make the true distance far
    shorter than the Manhattan estimate, which would make an A* heuristic
    inadmissible (and its routes wrong). Dijkstra needs no heuristic. (For very
    large maps this is where a hierarchical, Google-Maps-style approach would come
    in later; for the sizes we explore now it's fine.)

    `reach` stops at any tile within that Chebyshev distance of `goal` instead of on
    it. This is what makes the router usable for OBJECTS rather than destinations: a
    locker, a wall lever or a loot pile under a blocking item is not itself walkable,
    so an exact-goal search returns None even when the bot could plainly walk up and
    touch it. Interacting only needs range 1 anyway.

    `avoid` is never entered — learned hazards and tiles the server has refused us.
    The shared walkable map only ever GROWS (a tile that looked walkable stays in it),
    so this is how a bot keeps its hard-won local knowledge from being overruled.

    `costs` prices each tile in travel time (`tile_cost` / the colony's walk costs), so
    routes prefer the road. Omit it and every step costs the same, which is the old
    fewest-tiles behaviour. A teleport hop is priced at the cheapest possible step: it is
    effectively instant, and must never look worse than walking.

    `unconfirmed_crossings` (pass `colony.get_unconfirmed_crossings()`) prices a tile at
    `unconfirmed_crossing_cost` toward `goal` instead of its ordinary `costs` value — see
    that function. These are STEP-type floor-changes (holes/open-stairs/teleporters)
    we've SEEN but never actually crossed: they aren't in `links` yet, so without this
    they're just ordinary walkable ground to this search, and a route can walk straight
    onto one and get relocated somewhere else. Once a tile like this is actually crossed
    it moves into `links` and this no longer applies to it.

    `start` is always a legal node even if it isn't in `walkable`: we are, self
    evidently, standing on it, whatever the map believes.

    Returns a list of (direction, is_teleport, resulting_position) steps, or None
    if the goal isn't reachable through what we've explored. `is_teleport` marks a
    step where walking `direction` puts you onto a link tile and whisks you to
    `resulting_position` instead of the adjacent tile.

    LOGGING: emits to the `antbot.route` logger at DEBUG (every edge relaxed) and INFO
    (a unconfirmed_crossings tile actually winning a spot on the final route) — see
    `nav.route_log`. Off by default; turn it on to watch the search's own reasoning.
    """
    if within_reach(start, goal, reach):
        return []

    avoid = avoid or set()
    unconfirmed_crossings = unconfirmed_crossings or set()
    dist = {start: 0}
    prev: dict[tuple[int, int, int], tuple[tuple[int, int, int], str, bool]] = {}
    heap: list[tuple[int, tuple[int, int, int]]] = [(0, start)]
    target: tuple[int, int, int] | None = None
    debug = route_log.isEnabledFor(logging.DEBUG)
    if debug:
        route_log.debug("find_shared_route: start=%s goal=%s reach=%d, %d unconfirmed z-hop "
                        "candidate(s) in view: %s",
                        start, goal, reach, len(unconfirmed_crossings), sorted(unconfirmed_crossings))

    while heap:
        d, node = heapq.heappop(heap)
        if within_reach(node, goal, reach):
            target = node
            break
        if d > dist.get(node, 1 << 60):
            continue
        x, y, z = node
        for name, dx, dy in _STEPS:
            adjacent = (x + dx, y + dy, z)
            if adjacent in avoid:
                continue
            if adjacent in links:
                # Stepping onto this tile teleports us to its destination.
                landing, teleport = links[adjacent], True
            elif adjacent in walkable:
                landing, teleport = adjacent, False
            else:
                continue
            if landing in avoid:
                continue
            # Both branches must be in the SAME units, or the router mis-prices one of
            # them badly. A teleport is instant, so it costs the cheapest step there is —
            # never more than walking, or we'd trudge straight past a portal.
            gamble = not teleport and landing in unconfirmed_crossings
            if teleport:
                step = MIN_GROUND_SPEED
            elif gamble:
                step = unconfirmed_crossing_cost(landing, goal[0], goal[1])
            elif costs:
                step = costs.get(landing, DEFAULT_GROUND_SPEED)
            else:
                step = 1        # unpriced: every step equal, the old fewest-tiles rule
            nd = d + step
            improved = nd < dist.get(landing, 1 << 60)
            if debug:
                route_log.debug(
                    "  relax %s -%s-> %s  step=%d (%s)  total=%d%s%s",
                    node, name, landing, step,
                    "GAMBLE unconfirmed z-hop" if gamble else ("link" if teleport else "ground"),
                    nd, " -> confirmed link" if teleport else "",
                    " [IMPROVED]" if improved else " [no improvement, skipped]")
            if improved:
                dist[landing] = nd
                prev[landing] = (node, name, teleport)
                heapq.heappush(heap, (nd, landing))

    if target is None or target not in prev:
        if debug:
            route_log.debug("find_shared_route: NO ROUTE found from %s to %s", start, goal)
        return None

    route: list[tuple[str, bool, tuple[int, int, int]]] = []
    node = target
    while node in prev:
        came, name, teleport = prev[node]
        route.append((name, teleport, node))
        node = came
    route.reverse()

    gambled_on = [land for _n, _tp, land in route if land in unconfirmed_crossings]
    if gambled_on:
        route_log.info("find_shared_route: DECIDED to cross unconfirmed z-hop(s) %s en route "
                       "%s -> %s (total cost %d) — betting the optimistic estimate beats "
                       "the known alternative", gambled_on, start, goal, dist.get(target, -1))
    elif debug:
        route_log.debug("find_shared_route: settled on an all-confirmed-ground route %s -> %s "
                        "(total cost %d)", start, goal, dist.get(target, -1))
    return route


# Deltas by direction name, shared with the renderer.
_DELTAS = {name: (dx, dy) for name, dx, dy in _STEPS}


def render_ascii(state: GameState, item_flags: ItemFlags,
                 path: list[str] | None = None) -> str:
    """Draw the known area around the bot as text, for eyeballing the parse/plan.

    Legend:
        @  us            *  a planned path tile      C  another creature
        .  walkable      #  known but blocked        (space) unknown / not seen

    If `path` is given, the tiles it walks through are marked with '*', so you can
    literally see the route thread between the walls.
    """
    if state.position is None:
        return "(no position)"
    px, py, z = state.position.x, state.position.y, state.position.z

    # Project the path into the set of tiles it visits.
    path_tiles: set[tuple[int, int]] = set()
    cx, cy = px, py
    for direction in path or []:
        dx, dy = _DELTAS[direction]
        cx, cy = cx + dx, cy + dy
        path_tiles.add((cx, cy))

    occupied = {
        (c.position.x, c.position.y)
        for c in state.nearby_creatures()
        if c.position is not None and c.position.z == z
    }

    lines = []
    for y in range(py - 6, py + 8):
        row = []
        for x in range(px - 8, px + 10):
            if (x, y) == (px, py):
                ch = "@"
            elif (x, y) in path_tiles:
                ch = "*"
            elif (x, y) in occupied:
                ch = "C"
            elif is_walkable(state, item_flags, x, y, z):
                ch = "."
            elif (x, y, z) in state.tiles:
                ch = "#"  # known tile, but something on it blocks
            else:
                ch = " "  # never parsed: void or out of window
            row.append(ch)
        lines.append("".join(row))
    return "\n".join(lines)
