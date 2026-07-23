"""navcost — the ONE cost model shared by the efficiency floor and the efficiency score.

The navigation efficiency tests (see navtests.py) don't ask "did the bot arrive?" — the
pass/fail suite already does. They ask "how CLOSE to the fastest possible route did it
get?". Answering that needs two numbers in the SAME units:

  * the FLOOR — the cost of the time-optimal route over a fully-revealed map (`compute_par`),
  * the SCORE — the cost of the route the bot actually walked (`route_cost`),

and the whole point collapses if those two are computed even slightly differently. So both
live here and both call the same `route_cost`. The efficiency ratio is score / floor: 1.0
means the bot walked a time-optimal route; 2.0 means it spent twice the necessary time.

WHY TIME, NOT TILES. A step's cost is the *ground speed* of the tile you step onto —
Tibia's `groundSpeed` (`bank.waypoints`, surfaced as ItemFlags.ground_speed and, per-tile,
as colony walk-costs / nav.tile_cost). Beware the inversion the whole codebase shares:
groundSpeed is a DURATION (`step_ms ≈ groundSpeed * 1000 / playerSpeed`), so BIGGER means
SLOWER. Road is 100, grass 150, sand 160, swamp higher. Costing time instead of tiles is
what lets the floor prefer a LONGER route on fast road over a SHORTER one through swamp —
which is exactly the wrinkle these tests exist to reward:

    4 road tiles  @ 100 = 400   <- fewer minutes, MORE tiles: the optimum
    3 swamp tiles @ 150 = 450   <- fewer tiles, MORE minutes

`playerSpeed` is a single constant that scales every step equally, so it CANCELS in the
score/floor ratio. That's the trick that lets us score in modeled time without ever
touching a wall clock: the ratio is deterministic and reproducible, immune to server lag,
CPU load, or how busy the machine was on any given run.

CARDINAL MOVEMENT. Our mover only steps N/E/S/W (nav._STEPS has four entries), so there is
no diagonal-cost factor to model: the floor is computed over the same 4-neighbour movement
the bot can actually perform, which keeps the optimum genuinely reachable — a warm run CAN
converge on ratio ~1.0. If we ever teach the bot diagonal steps, the diagonal-time factor
belongs here, in `_step_cost`, so floor and score pick it up together.

FLOOR-CHANGES AND DOORS. A stairs/ladder/teleport hop is priced like `find_shared_route` prices
it — the cheapest step there is (LINK_HOP_COST) — because it's effectively instant and must
never look costlier than walking, or the floor would trudge past a shortcut. A door adds no
node of its own: the optimal map is revealed with the door OPEN, so its tile is just a
normal walkable tile and both floor and score pay its ground cost when stepping through. The
one-off server delay of the OPEN action itself is not modeled; it is a constant the cold and
warm runs share and both routes incur at the same door, so it cancels in every comparison we
actually make.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Sequence

from .nav import DEFAULT_GROUND_SPEED, MIN_GROUND_SPEED, find_shared_route

Tile = tuple[int, int, int]

# A floor-change / teleport hop costs the cheapest a step can ever be — identical to how
# nav.find_shared_route prices a link edge, so the floor and the score agree with the router that
# a shortcut is effectively free. Anything higher would make the model fear its own stairs.
LINK_HOP_COST = MIN_GROUND_SPEED


def _is_link_step(prev: Tile, cur: Tile) -> bool:
    """Was moving prev->cur a floor-change/teleport hop rather than a plain walk step?

    A normal walk moves exactly one tile on the same floor. Anything else — the z changed
    (stairs/ladder/hole) or we jumped more than one tile in x/y (a teleport) — is a link
    hop, which we price flat at LINK_HOP_COST to match the router.
    """
    if prev[2] != cur[2]:
        return True                                   # changed floor: stairs/ladder/hole
    return max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1])) > 1   # jumped: a teleport


def _step_cost(prev: Tile, cur: Tile, cost_of: Callable[[Tile], int]) -> int:
    """The modeled time to move from `prev` onto `cur`.

    Link hops are flat (see LINK_HOP_COST); an ordinary step costs the ground speed of the
    tile stepped ONTO — that's the tile whose surface you traverse, and it's the same tile
    nav.tile_cost / the colony walk-costs price. `cost_of` maps a tile to its ground speed
    (falling back to DEFAULT_GROUND_SPEED for anything unpriced).
    """
    if _is_link_step(prev, cur):
        return LINK_HOP_COST
    return cost_of(cur)


def make_cost_lookup(costs: Mapping[Tile, int]) -> Callable[[Tile], int]:
    """Turn a {tile -> ground speed} table into the `cost_of` callable route_cost wants.

    Unpriced tiles fall back to DEFAULT_GROUND_SPEED — the same pessimistic stand-in the
    planner uses, so an unknown tile is assumed a bit slow rather than free (which would
    flatter a route that crossed ground we never measured). The colony's get_walk_costs()
    is the usual source; a frozen fixture's stored costs is the other.
    """
    def cost_of(tile: Tile) -> int:
        return costs.get(tile, DEFAULT_GROUND_SPEED)
    return cost_of


def route_cost(path: Sequence[Tile], cost_of: Callable[[Tile], int]) -> int:
    """Total modeled travel time of walking `path`, tile by tile.

    `path` is the ordered list of tiles the walker stood on — the FIRST is the start (paid
    for by nothing; you're already there) and every subsequent tile adds one step's cost.
    Repeated/undone tiles are summed, not deduped: pacing back and forth into a room and out
    again pays for every step, which is precisely how an inefficient route earns a high
    score. Consecutive duplicates (a frame where we didn't move) are skipped so parser
    hiccups don't inflate the count.
    """
    total = 0
    prev: Tile | None = None
    for tile in path:
        t = (int(tile[0]), int(tile[1]), int(tile[2]))
        if prev is None:
            prev = t
            continue
        if t == prev:
            continue                         # no actual move this frame — ignore
        total += _step_cost(prev, t, cost_of)
        prev = t
    return total


def compute_par(walkable: set[Tile], links: dict[Tile, Tile], costs: Mapping[Tile, int],
            start: Tile, dest: Tile, reach: int = 0,
            unconfirmed_crossings: set[Tile] | None = None,
            step_links: set[Tile] | None = None) -> tuple[int, list[Tile]] | None:
    """The time-optimal route from `start` to `dest` over a fully-revealed map — the FLOOR.

    Delegates the search to nav.find_shared_route (weighted Dijkstra over the shared walkable graph
    plus learned stair/teleport links, priced by `costs`), then re-prices the resulting tile
    path through `route_cost` — the SAME accounting the bot's actual route is scored with, so
    floor and score are guaranteed commensurable. Returns (cost, [start, ...dest]) or None if
    `dest` isn't reachable through the revealed map (which, for a floor, means the reveal was
    incomplete — not that the bot failed).

    The map must be revealed with any blocking-but-openable obstacle (a door) already OPEN,
    so `walkable` includes its tile; find_shared_route then routes through it like any other ground
    and both floor and score pay its ground cost. See the module docstring.

    `unconfirmed_crossings` (pass `colony.get_unconfirmed_crossings()` from the SAME reveal) matters
    whenever the revealed region contains a STEP-type floor-change (a hole/stairs/teleporter)
    that was only ever SEEN during the reveal, never actually crossed into a confirmed link.
    Without this, find_shared_route has no idea that tile isn't ordinary ground and can route the
    "optimal" path straight through it — reporting a floor that isn't actually achievable,
    since in reality stepping there relocates you somewhere the solver never accounted for.
    Passing it makes the floor solved with the EXACT same honesty the live router uses (see
    nav.unconfirmed_crossing_cost): a route that avoids the object wins if it's genuinely cheaper,
    but the object is never silently treated as free passage. This was a real bug — a test
    region with such an object froze a par_cost lower than any bot (cold OR warm) could
    actually achieve, since both correctly refuse to gamble through it. Omitting this
    (the old behavior) is only correct for a region with no such loose ends.

    `step_links` (pass `colony.get_step_links()` from the SAME reveal) matters whenever
    `start` or `dest` sits exactly on a USE-type shortcut's source tile (a grate is the case
    that surfaced this): without it, find_shared_route treats EVERY link as auto-teleporting
    on arrival, so a floor that begins or ends exactly there gets solved with an artificial
    "walk away and back" detour instead of the true-optimal direct hop — or, worse, a goal
    exactly on that tile can look unreachable at all. See find_shared_route's docstring.
    """
    route = find_shared_route(walkable, links, start, dest, reach=reach, costs=dict(costs),
                               unconfirmed_crossings=unconfirmed_crossings,
                               step_links=step_links)
    if route is None:
        return None
    # find_shared_route yields (direction, is_teleport, landing) per step; the tiles we STAND on
    # are start followed by each landing. Price that exact sequence with route_cost.
    path: list[Tile] = [start] + [landing for _dir, _tp, landing in route]
    return route_cost(path, make_cost_lookup(costs)), path


def as_tiles(positions: Iterable) -> list[Tile]:
    """Normalise a path log (world.Position objects and/or (x,y,z) tuples) to plain tuples.

    The session records its path as Position objects; fixtures store tuples. Everything
    downstream wants tuples, so funnel both through here.
    """
    out: list[Tile] = []
    for p in positions:
        if hasattr(p, "x"):
            out.append((p.x, p.y, p.z))
        else:
            out.append((int(p[0]), int(p[1]), int(p[2])))
    return out
