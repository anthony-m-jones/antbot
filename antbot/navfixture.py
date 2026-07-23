"""navfixture — build and load the frozen efficiency FLOOR for a navigation test.

An efficiency test scores the bot's route against the fastest route that EXISTS. That
fastest route is a property of the map, not of the bot, so we compute it ONCE from a full
reveal of the region and freeze it as a fixture (a small JSON file). Test runs then just
compare the bot's actual route cost to this stored `par_cost` — no re-solving, and a stable
denominator you can ratchet a budget against.

Building the floor (`build`) has three parts, all operator/test tooling (GOD is used to set
the scene; the colony bots never touch any of this):

  1. OPEN every openable obstacle (a door) so the region is revealed as it is when passable —
     the optimal route walks THROUGH the door, so its tile must be walkable ground during the
     reveal. See navcost's module docstring for why an open door needs no special node.
  2. FULLY REVEAL the region: GOD teleports the test character across a coarse grid covering
     the bounding box on every floor involved, while the character's own session parses each
     view and folds it into the shared map (walkable tiles + per-tile ground speeds). One
     real navigate across the floor-change is also driven, because a stairs/ladder LINK is
     only recorded when the bot actually STEPS through it (GOD-teleporting over it doesn't
     teach the link), and the optimal route needs that edge to cross floors.
  3. SOLVE for the time-optimal route with navcost.compute_par (weighted Dijkstra over the
     revealed graph) and freeze (par_cost, par_path, the region's cost table) to disk.

`load` reads the fixture back for the test harness. The stored cost table is what BOTH the
floor and the bot's actual route are priced against, so the two are always commensurable.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path

from . import client, gm
from .items import ItemFlags
from .colony import Colony
from . import navcost

log = logging.getLogger("antbot.navfixture")

Tile = tuple[int, int, int]

# Where frozen floors live, one JSON per test (slugified name). Checked in alongside the
# code so the floor is auditable and travels with the repo.
FIXTURE_DIR = Path(__file__).with_name("navfixtures")

# How far PAST the start/dest/door bounding box to reveal, and how far apart to space the
# GOD teleport anchors. The grid step is a little under the server's view radius (~8-9
# tiles) so consecutive anchors overlap and leave no unseen gap between them.
_REVEAL_MARGIN = 9
_GRID_STEP = 8


@dataclasses.dataclass(frozen=True)
class ParFixture:
    """The frozen efficiency floor for one test."""
    name: str
    start: Tile
    dest: Tile
    par_cost: int
    par_path: list[Tile]
    costs: dict[Tile, int]      # region ground speeds — the shared table for floor AND score
    # The full revealed graph, so the WARM pass can be handed complete map knowledge without
    # a preceding cold exploration (see navtests.run_efficiency / Colony.seed_for_test). walkable
    # keys == costs keys; links carries the stair/ladder/teleport edges across floors.
    walkable: set[Tile]
    links: dict[Tile, Tile]
    # The revealed tiles as full item stacks [(id, count), ...], so the warm pass can seed the
    # bot's OWN local view (state.tiles) too — not just the shared colony map. That's what the
    # floor-change hunt (find_descent) reads to SEE the stairs, so without it a standalone warm
    # run can't spot the way up and wanders. This makes warm deterministic and self-contained.
    tiles: dict[Tile, list]

    def cost_of(self):
        """The `cost_of` callable navcost wants, backed by this fixture's frozen table."""
        return navcost.make_cost_lookup(self.costs)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / f"{_slug(name)}.json"


def load(name: str) -> ParFixture | None:
    """Read the frozen floor for `name`, or None if it hasn't been built yet."""
    path = fixture_path(name)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return ParFixture(
        name=data["name"],
        start=tuple(data["start"]),
        dest=tuple(data["dest"]),
        par_cost=int(data["par_cost"]),
        par_path=[tuple(t) for t in data["par_path"]],
        costs={(c[0], c[1], c[2]): c[3] for c in data["costs"]},
        walkable={(t[0], t[1], t[2]) for t in data.get("walkable", [])},
        links={tuple(s): tuple(d) for s, d in data.get("links", [])},
        # Restore full stacks as (id, count) tuples — the runtime shape state.tiles uses.
        tiles={(t[0], t[1], t[2]): [tuple(p) for p in t[3]]
               for t in data.get("tiles", [])},
    )


def _save(fixture: ParFixture) -> Path:
    FIXTURE_DIR.mkdir(exist_ok=True)
    path = fixture_path(fixture.name)
    payload = {
        "name": fixture.name,
        "start": list(fixture.start),
        "dest": list(fixture.dest),
        "par_cost": fixture.par_cost,
        "par_path": [list(t) for t in fixture.par_path],
        # The region's cost table — every revealed walkable tile and its ground speed. Both
        # the floor and the bot's actual route are priced against this, so they can't drift.
        "costs": [[x, y, z, spd] for (x, y, z), spd in sorted(fixture.costs.items())],
        # The full revealed graph + tile stacks. Not needed to SCORE, but they let the floor
        # be re-solved if the cost model changes (walkable/links) and let the warm pass seed
        # the bot's own view (tiles) — see ParFixture.
        "walkable": [[x, y, z] for (x, y, z) in sorted(fixture.walkable)],
        "links": [[list(s), list(d)] for s, d in sorted(fixture.links.items())],
        "tiles": [[x, y, z, [list(p) for p in stack]]
                  for (x, y, z), stack in sorted(fixture.tiles.items())],
    }
    path.write_text(json.dumps(payload, indent=1))
    return path


def _bbox_anchors(points: list[Tile]) -> dict[int, list[Tile]]:
    """Grid of GOD-teleport anchor tiles per floor, covering the points' bounding box + margin.

    Returns {z: [(x, y, z), ...]}. Anchors are spaced `_GRID_STEP` apart so their server views
    overlap and the union covers the whole box; a few anchors may land on walls/void, which
    /tpto simply refuses (harmless — neighbouring anchors still reveal that ground).
    """
    per_floor: dict[int, list[Tile]] = {}
    zs = sorted({p[2] for p in points})
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs) - _REVEAL_MARGIN, max(xs) + _REVEAL_MARGIN
    y0, y1 = min(ys) - _REVEAL_MARGIN, max(ys) + _REVEAL_MARGIN
    for z in zs:
        anchors: list[Tile] = []
        x = x0
        while x <= x1:
            y = y0
            while y <= y1:
                anchors.append((x, y, z))
                y += _GRID_STEP
            x += _GRID_STEP
        per_floor[z] = anchors
    return per_floor


async def build(item_flags: ItemFlags, name: str, start: Tile, dest: Tile,
                door_opens: list[str], door_closes: list[str],
                character: str, account: str, password: str,
                host: str = "127.0.0.1", login_port: int = 7171) -> ParFixture:
    """Reveal the region for test `name` and freeze its time-optimal route to a fixture.

    `door_opens` / `door_closes` are ready-made `/settile` argument strings (without the
    leading command) that make each door passable for the reveal and closed again after —
    the caller derives them from the test's setup. `character` is the test bot GOD will
    puppet across the reveal grid.
    """
    # 1) Open the doors so the region reveals as a passable route.
    if door_opens:
        await gm.gm_run(item_flags, *[f"/settile {a}" for a in door_opens],
                        host=host, login_port=login_port)

    # A fresh colony with NO persisted knowledge (hazard_file=None): the fixture must capture
    # only THIS region's reveal, not the whole world's accumulated map from learned_hazards.
    # The cross-floor link the optimal route needs is recorded by the real navigate below, so
    # we don't depend on persisted links. This keeps the fixture bounded and its "region"
    # meaningful — and keeps the shared graph we seed into the warm pass small and local.
    colony = Colony(item_flags=item_flags, hazard_file=None)
    anchors = _bbox_anchors([start, dest])

    session = await gm.connect_retry(item_flags, account, password, character,
                                     host=host, login_port=login_port)
    session.colony = colony
    session.role = "navfixture"

    result: dict = {}

    async def action(sess) -> None:
        import asyncio
        await asyncio.sleep(0.5)
        # 2a) Grid reveal: GOD walks the bot across every anchor on every floor in a single
        # login (0.8s apart), while THIS session's receive loop parses each view and folds
        # it into `colony` automatically (see _report_to_colony -> contribute_tiles). We
        # order floors dest-first so we finish standing near where the link search wants us.
        tour: list[str] = []
        for z in sorted(anchors):                          # low z (higher floor) first
            for (ax, ay, az) in anchors[z]:
                tour.append(f"/tpto {character}, {ax}, {ay}, {az}")
        log.info("navfixture: revealing %d anchors across floors %s",
                 len(tour), sorted(anchors))
        # Batch the tour; contribute after so even views that didn't trigger a `moved`
        # event (a teleport can arrive as a fresh map) are folded in.
        await gm.gm_run(item_flags, *tour, host=host, login_port=login_port)
        for _ in range(10):
            await asyncio.sleep(0.3)
            colony.contribute_tiles(sess.state)

        # 2b) Record the cross-floor LINK by driving one real navigation: put the bot at the
        # start and let it climb to the destination for real (the door is open). Stepping
        # through the stairs/ladder is what teaches colony._links the edge the optimal route
        # needs; the walk also reveals the corridor at ground level.
        await gm.gm_run(item_flags, f"/tpto {character}, {start[0]}, {start[1]}, {start[2]}",
                        host=host, login_port=login_port)
        for _ in range(15):
            await asyncio.sleep(0.2)
            p = sess.state.position
            if p is not None and (p.x, p.y, p.z) == start:
                break
        loop = asyncio.get_event_loop()
        await client.navigate_to(sess, item_flags, dest[0], dest[1], dest[2],
                                 deadline=loop.time() + 90)
        colony.contribute_tiles(sess.state)

        # 3) Solve for the time-optimal route over everything we revealed.
        walkable = colony.get_walkable()
        links = colony.get_links()
        costs = colony.get_walk_costs()
        # Any STEP-type object the reveal SAW but the real navigate above never actually
        # crossed (so it's not in `links`) must be priced the same honest, non-free way the
        # live router prices it — otherwise the solver can route the "optimal" path straight
        # through an object that would really relocate you, freezing a par_cost no bot could
        # actually achieve. See navcost.compute_par's docstring.
        unconfirmed_crossings = colony.get_unconfirmed_crossings()
        # The destination sits behind the (now open) door, so its tile is walkable in the
        # reveal. If it somehow isn't reachable, the reveal was incomplete — surface that
        # rather than freezing a bogus floor.
        solved = navcost.compute_par(walkable, links, costs, start, dest,
                                     unconfirmed_crossings=unconfirmed_crossings)
        if solved is None:
            result["error"] = (
                f"could not solve an optimal route {start} -> {dest} over the revealed "
                f"region ({len(walkable)} walkable tiles, {len(links)} links). The reveal "
                "may be incomplete or the link across floors wasn't recorded.")
            return
        par_cost, par_path = solved
        # Freeze only the costs for tiles the optimal path or the region actually uses; keep
        # the whole revealed region's costs so the actual route (which may wander onto other
        # revealed tiles) is always priced from the same table.
        region_costs = {t: costs.get(t, navcost.DEFAULT_GROUND_SPEED) for t in walkable}
        # Snapshot the bot's OWN accumulated view (full item stacks) — state.tiles is never
        # pruned (world.py), so after the whole reveal it holds every tile we parsed. This is
        # what seeds the warm pass's local view. Copy the stacks so later frames can't mutate
        # what we froze.
        region_tiles = {t: [tuple(p) for p in stack]
                        for t, stack in sess.state.tiles.items()}
        result["fixture"] = ParFixture(name, start, dest, par_cost, par_path, region_costs,
                                       walkable=set(walkable), links=dict(links),
                                       tiles=region_tiles)

    try:
        await client._run_in_world(session, action)
    finally:
        # Restore the doors to closed and log the puppet bot out, so the world is left exactly
        # as a test expects to find it.
        if door_closes:
            await gm.gm_run(item_flags, *[f"/settile {a}" for a in door_closes],
                            host=host, login_port=login_port)
        await gm.gm_kick(item_flags, character, host=host, login_port=login_port)

    if "error" in result:
        raise RuntimeError(result["error"])
    fixture: ParFixture = result["fixture"]
    path = _save(fixture)
    log.info("navfixture: froze %s par_cost=%d (%d path tiles) -> %s",
             name, fixture.par_cost, len(fixture.par_path), path)
    return fixture
