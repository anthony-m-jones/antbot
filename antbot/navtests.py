"""Navigation test harness — declarative, reusable "can a bot get from A to B?" tests.

Each test teleports ONE bot to a start tile and requires it to reach a destination
within a time limit, under stated map preconditions. This gives feature work a concrete
objective and gives us regression coverage so we don't silently lose an ability.

A test run does, per test:
  1. STOP the game server. This guarantees the character is logged out (the fastest way)
     AND reloads the world map from the .otbm on the next start — so runtime map state
     (an opened door, a pulled lever) resets to the map's own state. Houses are the only
     things that persist, and our test tiles aren't houses.
  2. DB-TELEPORT the character to the start tile while the server is down (its stored
     position only takes effect at login, so it must be offline — which it is).
  3. START the server and log the bot in.
  4. VERIFY preconditions by reading the bot's OWN parsed tiles — e.g. "the door at
     (x,y,z) is closed (id 1629)". If a precondition can't be met (the map itself has the
     door open and we can't edit the .otbm), the test is SKIPped, not failed.
  5. DRIVE directed navigation (client.navigate_to) toward the destination until the bot
     stands on it or the time limit passes.
  6. Report PASS / FAIL / SKIP with timing.

Add a test by appending a NavTest to TESTS. Run:
    python -m antbot.navtests                # every test
    python -m antbot.navtests door           # tests whose name contains "door"

Requires Docker (the otbr-* containers) and the antbot package importable.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import socket
import subprocess
import time

from .items import ItemFlags
from .catalog import impassable_ground_ids, load_item_catalog
from .colony import Colony
from . import client, wire, gm, navcost, navfixture

log = logging.getLogger("antbot.navtests")

# --- environment (matches run-colony / the docker quickstart) -----------------
HOST = "127.0.0.1"
LOGIN_PORT = 7171
ACCOUNT = "test1"
PASSWORD = "test"
CHARACTER = "Druid 1"

SERVER_CONTAINER = "otbr-server-1"   # the game server: stop/start resets the world map
DB_CONTAINER = "otbr-db-1"           # MariaDB: where a character's stored position lives
DB_USER, DB_PASS, DB_NAME = "canary", "canary", "canary"

APPEAR = r"C:\Users\Anthony\Documents\TibiaOT\canary\data\items\appearances.dat"
ITEMS_XML = r"C:\Users\Anthony\Documents\TibiaOT\canary\data\items\items.xml"


# --- test definition ----------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class TileRequire:
    """A precondition: tile (x, y, z) must carry item `item_id` before the run.

    Checked against the bot's own parsed snapshot after login. `desc` is for the report
    ("door closed"). If the tile doesn't carry `item_id`, the test SKIPs — we can only
    verify map state, not rewrite the .otbm.
    """
    x: int
    y: int
    z: int
    item_id: int
    desc: str


@dataclasses.dataclass(frozen=True)
class TileSetup:
    """A setup action applied via GOD before the run: put the tile item at (x, y, z) into
    `to_id`, transforming `from_id` if it's currently that (idempotent — see /settile).
    This is how a door is reset to a known state WITHOUT a server restart."""
    x: int
    y: int
    z: int
    from_id: int
    to_id: int
    desc: str = ""


@dataclasses.dataclass(frozen=True)
class NavTest:
    name: str
    start: tuple[int, int, int]
    dest: tuple[int, int, int]
    time_limit: float
    requires: tuple[TileRequire, ...] = ()
    setup: tuple[TileSetup, ...] = ()   # GM tile transforms applied before the run
    # How the bot gets to the start tile:
    #   False (default) -> GOD teleports it there instantly (see antbot.gm) — no restart,
    #                      the fast path. Any map state (a door) is reset via `setup`.
    #   True            -> stop server, DB-teleport, start server. Slower, but it RELOADS
    #                      the whole map. Only needed if a test depends on map state that
    #                      /settile can't reset for us.
    reset_map: bool = False
    # Efficiency mode. When True this is scored, not just pass/failed: the bot runs the route
    # in a COLD pass (no prior map — it must explore) and a WARM pass (handed the fully-
    # revealed map from the frozen floor), and each route's modeled travel time is compared to
    # the time-optimal FLOOR (par_cost, see navfixture). The split separates two things you
    # optimize independently — cold = exploration quality, warm = pure routing quality over a
    # known map. See run_efficiency.
    measure_efficiency: bool = False
    # Optional pass/fail gates on the efficiency RATIO (route_cost / par_cost, >= 1.0), one
    # per pass so cold and warm can be tightened separately. None = report-only (the default,
    # so a new test is a measurement tool first; you ratchet a budget down as nav improves,
    # turning it into a regression gate). E.g. warm_budget=1.3 fails a warm route that spends
    # >30% over the optimum.
    cold_budget: float | None = None
    warm_budget: float | None = None


# --- the tests ----------------------------------------------------------------
TESTS: list[NavTest] = [
    # Smoke test for the fast (GM-teleport) path: start == dest, so navigation trivially
    # succeeds and the ONLY thing under test is that GOD placed us on the exact tile.
    NavTest(
        name="GM teleport smoke test",
        start=(32369, 32241, 7),
        dest=(32369, 32241, 7),
        time_limit=15.0,
    ),
    NavTest(
        name="Bot can open a door to leave a room",
        start=(32386, 32236, 7),
        dest=(32389, 32238, 7),
        time_limit=60.0,
        # GOD resets the door to CLOSED (1629) before the run — no restart. `requires`
        # then verifies it (the door is in view from this same-floor start).
        setup=(TileSetup(32388, 32238, 7, 1630, 1629, "close door"),),
        requires=(TileRequire(32388, 32238, 7, 1629, "door closed"),),
    ),
    # Harder: start a floor BELOW (z8) and reach a tile on z7 that's behind the same closed
    # door. Exercises cross-floor directed navigation (find + climb a way up) AND opening
    # the door. reset_map=True resets the door; the door tile isn't in view from the z8
    # start, so the precondition is trusted via the reload (see _drive).
    NavTest(
        name="Bot climbs a floor and opens a door",
        start=(32389, 32245, 8),
        dest=(32389, 32238, 7),
        time_limit=60.0,
        # GOD resets the door to CLOSED before the run. The door tile isn't in view from
        # the z8 start, so `requires` is trusted via the setup (see _drive).
        setup=(TileSetup(32388, 32238, 7, 1630, 1629, "close door"),),
        requires=(TileRequire(32388, 32238, 7, 1629, "door closed"),),
    ),
    # The same climb-and-door scenario, but SCORED for efficiency instead of pass/fail. It
    # runs cold (no prior map) then warm (reusing the cold pass's knowledge) and reports each
    # route's modeled time against the frozen optimum (see navfixture / run_efficiency). This
    # is the iteration tool for "the bot arrives, but wanders on the way": watch cold_ratio
    # to improve exploration and warm_ratio to improve routing over a known map, separately.
    # Budgets start None (report-only); tighten them once the ratios settle to lock in gains.
    NavTest(
        name="Efficiency: climb a floor and open a door",
        start=(32389, 32245, 8),
        dest=(32389, 32238, 7),
        time_limit=90.0,
        setup=(TileSetup(32388, 32238, 7, 1630, 1629, "close door"),),
        measure_efficiency=True,
        cold_budget=None,
        warm_budget=None,
    ),
    # ISOLATION CASE for the unconfirmed-z-hop routing question (see nav.unconfirmed_step_cost
    # / colony._step_unconfirmed). Start already at the ladder's OWN landing tile on z7 — same
    # floor as the goal, so no floor-change is required at all — but this tile is a genuine
    # crossroads: several recognized-but-uncrossed STEP-type objects (multiple stairs_down,
    # a teleporter) sit within a couple of tiles, plus the ladder itself back down. A cold
    # bot has to recognize NONE of them are worth taking (the door route is directly ahead
    # and cheap) without either (a) never considering them at all — which would be the same
    # "just get lucky" blind spot as before — or (b) chasing one speculatively and never
    # coming back, which is exactly the failure this test exists to catch. Deliberately
    # separated from the "climbs a floor" test above so the routing question can be iterated
    # on without the extra confound of the initial climb.
    NavTest(
        name="Efficiency: unconfirmed z-hop crossroads, no climb needed",
        start=(32386, 32241, 7),
        dest=(32389, 32238, 7),
        time_limit=90.0,
        setup=(TileSetup(32388, 32238, 7, 1630, 1629, "close door"),),
        measure_efficiency=True,
        cold_budget=None,
        warm_budget=None,
    ),
]


# --- docker / DB orchestration ------------------------------------------------
def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _running(container: str) -> bool:
    r = _docker("inspect", "-f", "{{.State.Running}}", container)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _wait_db_ready(timeout: float = 60) -> bool:
    """Wait until MariaDB answers a trivial query."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _docker("exec", DB_CONTAINER, "mariadb", f"-u{DB_USER}", f"-p{DB_PASS}",
                    DB_NAME, "-e", "SELECT 1;")
        if r.returncode == 0:
            return True
        time.sleep(2)
    return False


def _ensure_db() -> bool:
    if not _running(DB_CONTAINER):
        _docker("start", DB_CONTAINER)
    return _wait_db_ready()


def _db_teleport(character: str, x: int, y: int, z: int) -> bool:
    safe = character.replace("'", "")
    sql = (f"UPDATE players SET posx={int(x)}, posy={int(y)}, posz={int(z)} "
           f"WHERE name='{safe}';")
    r = _docker("exec", DB_CONTAINER, "mariadb", f"-u{DB_USER}", f"-p{DB_PASS}",
                DB_NAME, "-e", sql)
    if r.returncode != 0:
        log.error("teleport DB update failed: %s", (r.stderr or "").strip()[:200])
    return r.returncode == 0


def _port_open(host: str, port: int, timeout: float = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _stop_server() -> None:
    """Stop the game server. This logs everyone out AND flushes online players to the DB
    BEFORE we teleport — so our DB write can't be overwritten by a save-on-stop. Order
    matters: stop, THEN teleport, THEN start (see run_test)."""
    _docker("stop", SERVER_CONTAINER)


def _start_server_and_wait(timeout: float = 120) -> bool:
    """Start the game server (reloading the map from the .otbm), waiting for the login
    port to accept connections. We wait on the TCP port because map loading takes a
    while and connecting too early just errors."""
    _docker("start", SERVER_CONTAINER)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(HOST, LOGIN_PORT):
            time.sleep(3)   # port's up; give the world a moment to finish loading
            return True
        time.sleep(2)
    return False


# --- running one test ---------------------------------------------------------
@dataclasses.dataclass
class Result:
    name: str
    status: str          # "PASS" | "FAIL" | "SKIP" | "ERROR"
    detail: str
    seconds: float = 0.0

    def line(self) -> str:
        # ASCII only — this runs in a Windows cp1252 console that can't encode ✓/✗.
        mark = {"PASS": "+", "FAIL": "x", "SKIP": "-", "ERROR": "!"}.get(self.status, "?")
        t = f" ({self.seconds:.1f}s)" if self.seconds else ""
        return f"  [{mark}] {self.status:5} {self.name}{t}\n        {self.detail}"


async def _drive(colony: Colony, flags: ItemFlags, test: NavTest) -> Result:
    """Log the (already-teleported) bot in, verify preconditions, then navigate."""
    session = await gm.connect_retry(flags, ACCOUNT, PASSWORD, CHARACTER,
                                     host=HOST, login_port=LOGIN_PORT)
    session.colony = colony
    session.role = "navtest"

    outcome = {"result": None}

    async def action(sess) -> None:
        await asyncio.sleep(0.4)  # let the login snapshot finish parsing
        sx, sy, sz = test.start
        # Fast path: we logged in wherever we last were; in ONE GOD session, place us at the
        # start tile AND apply any tile setup (reset a door). No restart. (reset_map tests
        # are already at the start via DB teleport and reset via the map reload.)
        established: set[tuple[int, int, int, int]] = set()  # (x,y,z,id) GOD confirmed
        if not test.reset_map:
            cmds = [f"/tpto {CHARACTER}, {sx}, {sy}, {sz}"]
            cmds += [f"/settile {s.x}, {s.y}, {s.z}, {s.from_id}, {s.to_id}"
                     for s in test.setup]
            replies = await gm.gm_run(flags, *cmds)
            if not any("tpto:" in r for r in replies):
                outcome["result"] = Result(
                    test.name, "ERROR",
                    "GM teleport not acknowledged (is GOD online-capable and /tpto "
                    "installed on the server?)")
                return
            # Record which setups the SERVER confirmed (reply: "settile: x,y,z -> id" or
            # "... already id"). We trust this over the bot's own view: a /settile transform
            # of a remote tile doesn't reliably reach the bot's parsed map (esp. cross-floor
            # or before it moves), but the server's ack is authoritative.
            for s in test.setup:
                tag = f"settile: {s.x},{s.y},{s.z}"
                if any(tag in r and str(s.to_id) in r and "neither" not in r for r in replies):
                    established.add((s.x, s.y, s.z, s.to_id))
            for _ in range(25):   # wait for the teleport to reflect in our own position
                await asyncio.sleep(0.2)
                p = sess.state.position
                if p is not None and (p.x, p.y, p.z) == (sx, sy, sz):
                    break
        pos = sess.state.position
        # Sanity: are we actually at the start tile?
        if pos is None or (pos.x, pos.y, pos.z) != (sx, sy, sz):
            outcome["result"] = Result(
                test.name, "ERROR",
                f"expected to spawn at {test.start}, but we're at "
                f"{(pos.x, pos.y, pos.z) if pos else None}")
            return

        # Preconditions. Trust anything a map reload (reset_map) or a GOD-confirmed setup
        # established; only fall back to the bot's own parsed snapshot for tiles we neither
        # reset nor set up.
        for req in test.requires:
            key = (req.x, req.y, req.z, req.item_id)
            if test.reset_map or key in established:
                continue
            items = sess.state.tiles.get((req.x, req.y, req.z))
            if items is None:
                outcome["result"] = Result(
                    test.name, "SKIP",
                    f"can't verify precondition: tile {(req.x, req.y, req.z)} "
                    f"({req.desc}) is not in view and wasn't set up")
                return
            ids = [iid for iid, _c in items]
            if req.item_id not in ids:
                outcome["result"] = Result(
                    test.name, "SKIP",
                    f"precondition not met: tile {(req.x, req.y, req.z)} should carry "
                    f"{req.item_id} ({req.desc}); saw {ids}")
                return

        # Navigate to the destination under the time limit.
        dx, dy, dz = test.dest
        t0 = asyncio.get_event_loop().time()
        deadline = t0 + test.time_limit
        arrived = await client.navigate_to(sess, flags, dx, dy, dz, deadline=deadline)
        elapsed = asyncio.get_event_loop().time() - t0
        end = sess.state.position
        endpos = (end.x, end.y, end.z) if end else None
        if arrived:
            outcome["result"] = Result(test.name, "PASS",
                                       f"reached {test.dest}", elapsed)
        else:
            outcome["result"] = Result(
                test.name, "FAIL",
                f"did not reach {test.dest} within {test.time_limit:.0f}s "
                f"(ended at {endpos})", elapsed)

    try:
        await client._run_in_world(session, action)
    except Exception as err:  # noqa: BLE001 — a harness must not crash on one test
        return Result(test.name, "ERROR", f"{type(err).__name__}: {err}")
    return outcome["result"] or Result(test.name, "ERROR", "no result recorded")


async def run_test(test: NavTest) -> Result:
    print(f"\n>>> {test.name}")
    print(f"    start {test.start} -> dest {test.dest}, limit {test.time_limit:.0f}s")

    if test.reset_map:
        # Slow path (needed to reset runtime map state like an opened door): STOP first
        # (guarantees offline + flushes any save), THEN teleport (nothing can overwrite
        # it), THEN start (reloads the map). DB-teleporting before the stop lets the
        # stop's flush clobber our write, so order matters.
        if not _ensure_db():
            return Result(test.name, "ERROR", "database container not reachable")
        print("    stopping server ...")
        _stop_server()
        print("    teleporting to start (DB) ...")
        if not _db_teleport(CHARACTER, *test.start):
            return Result(test.name, "ERROR", "could not teleport the character")
        print("    starting server (reloads the map) ...")
        if not _start_server_and_wait():
            return Result(test.name, "ERROR", "server did not come back up in time")
    else:
        # Fast path: no restart. Just make sure the server is up; GOD teleports the bot
        # to the start inside _drive (see gm.gm_teleport).
        print("    positioning via GM teleport (no restart) ...")
        if not _port_open(HOST, LOGIN_PORT) and not _start_server_and_wait():
            return Result(test.name, "ERROR", "server did not come back up in time")

    flags = ItemFlags.load(APPEAR)
    flags.add_impassable_ground(impassable_ground_ids(load_item_catalog(ITEMS_XML)))
    colony = Colony(item_flags=flags)
    try:
        return await _drive(colony, flags, test)
    finally:
        # Teardown: force the test bot OFFLINE so it can't linger (its graceful logout can
        # be declined by the server — combat/PZ rules). Best-effort; a not-online char is a
        # harmless no-op. This is why the LAST test no longer leaves the bot logged in.
        try:
            await gm.gm_kick(flags, CHARACTER)
        except Exception as err:  # noqa: BLE001
            log.warning("teardown /kick failed: %s", err)


# --- efficiency scoring: cold vs warm against the frozen optimum --------------
def _door_cmds(test: NavTest) -> tuple[list[str], list[str]]:
    """(OPEN args, CLOSE args) — bare `/settile` arguments for each of the test's door setups.

    A TileSetup transforms from_id -> to_id, and our door setups go OPEN(1630) -> CLOSED(1629),
    so the setup itself is the CLOSE and its reverse is the OPEN. Assumes each setup's from_id
    is the passable (open) variant — true for every door test. Used to reveal the region with
    the door open (floor) and to re-close it before each scored pass (scenario precondition).
    """
    opens: list[str] = []
    closes: list[str] = []
    for s in test.setup:
        closes.append(f"{s.x}, {s.y}, {s.z}, {s.from_id}, {s.to_id}")   # open -> closed
        opens.append(f"{s.x}, {s.y}, {s.z}, {s.to_id}, {s.from_id}")    # closed -> open
    return opens, closes


def _trace(path: list, cost_of) -> str:
    """A per-step trace of a route: each tile with the time it cost and the running total.

    This is the iteration workhorse — when a route scores high, the trace shows exactly WHERE
    the cost piled up (a slow-ground detour, or the same tiles visited twice while pacing).
    """
    tiles = navcost.as_tiles(path)
    out: list[str] = []
    prev = None
    total = 0
    for t in tiles:
        if prev is None:
            out.append(f"        start {t}")
        else:
            c = navcost._step_cost(prev, t, cost_of)
            total += c
            out.append(f"          -> {t}  +{c:<3d} (cum {total})")
        prev = t
    return "\n".join(out)


async def run_efficiency(test: NavTest, phases: list[str], trace: bool,
                         rebuild_fixture: bool) -> Result:
    """Score `test` for route efficiency, pricing each route against the frozen time-optimal
    floor and gating on the per-pass budgets.

    COLD  — a fresh colony with no prior map: the bot must explore to find the route. Measures
            exploration quality (finding the exit) folded together with routing.
    WARM  — a fresh colony SEEDED with the fully-revealed region from the frozen floor fixture
            (every walkable tile + the floor-change links), i.e. the bot is handed the whole
            map. Measures pure routing quality over KNOWN terrain — this is where "arrives but
            paces around the room" shows up isolated from any exploration excuse. Should
            approach ratio 1.0.

    The two phases run in SEPARATE sessions and colonies (see run_one) — they never share
    state. That makes each independently reproducible (so `--phase cold` / `--phase warm` can
    be iterated on their own) and avoids a subtle trap: a cold pass walks through the opened
    door, which would poison a shared map into thinking the shut door is passable. The
    reported improvement (cold − warm) is thus two independent measurements: explore-from-
    scratch vs route-over-a-known-map.
    """
    print(f"\n>>> {test.name}  [efficiency: {'+'.join(phases)}]")
    print(f"    start {test.start} -> dest {test.dest}, limit {test.time_limit:.0f}s/pass")
    if not _port_open(HOST, LOGIN_PORT) and not _start_server_and_wait():
        return Result(test.name, "ERROR", "server did not come back up in time")

    flags = ItemFlags.load(APPEAR)
    flags.add_impassable_ground(impassable_ground_ids(load_item_catalog(ITEMS_XML)))
    opens, closes = _door_cmds(test)

    # The frozen floor. Build it once (a ~30s full reveal) if missing or if asked to rebuild.
    if rebuild_fixture:
        navfixture.fixture_path(test.name).unlink(missing_ok=True)
    fixture = navfixture.load(test.name)
    if fixture is None:
        print("    no floor fixture yet — revealing the region to compute par_cost ...")
        fixture = await navfixture.build(flags, test.name, test.start, test.dest,
                                         opens, closes, CHARACTER, ACCOUNT, PASSWORD,
                                         host=HOST, login_port=LOGIN_PORT)
        await asyncio.sleep(3)   # space logins — Canary throttles rapid ones
    print(f"    floor: par_cost={fixture.par_cost} over {len(fixture.par_path)} tiles")
    cost_of = fixture.cost_of()

    measured: dict[str, tuple[bool, int, list]] = {}   # phase -> (arrived, cost, path)

    async def run_one(phase: str) -> None:
        """Run one phase in its OWN fresh session + colony, so the two never contaminate each
        other. Isolation is essential: a cold pass walks THROUGH the (opened) door, which
        permanently records that tile as walkable in its colony — so if warm reused it, the
        router would plan straight through the shut door and wander. Fresh state per phase also
        makes cold and warm independently reproducible, which is what lets you iterate on each
        one on its own.
        """
        # Both phases start from a colony with NO persisted map (hazard_file=None); the
        # traversal registry is still catalog-seeded, so the bot recognizes a ladder/door on
        # sight — that's the rules of the world, not its layout.
        colony = Colony(item_flags=flags, hazard_file=None)
        # WARM = routing over a KNOWN map: seed the colony with the fully-revealed region from
        # the fixture, INCLUDING the door tile. The door must stay in the shared graph so the
        # router plans the SHORT route straight to it (excluding it sends the router hunting a
        # long way around — even up a floor — which is the wander an earlier version showed).
        # We instead make the door open correctly by showing it CLOSED in the bot's OWN view
        # (below): the local walker then stops at the door rather than trying to step through a
        # tile it wrongly believes is open, and navigate_to's open-the-door branch engages.
        if phase == "warm":
            colony.seed_known(fixture.costs, fixture.links, fixture.tiles)
        session = await gm.connect_retry(flags, ACCOUNT, PASSWORD, CHARACTER,
                                         host=HOST, login_port=LOGIN_PORT)
        session.colony = colony
        session.role = "efficiency"

        async def action(sess) -> None:
            await asyncio.sleep(0.4)
            # Seed the bot's OWN view too (the floor-change hunt reads state.tiles to SEE the
            # stairs), forcing each door to its CLOSED id so the local walker also stops at it
            # and navigate_to opens it — as a real revisit would.
            if phase == "warm":
                seed = {t: list(stack) for t, stack in fixture.tiles.items()}
                for s in test.setup:
                    stack = seed.get((s.x, s.y, s.z), [])
                    stack = [((s.to_id, c) if iid == s.from_id else (iid, c))
                             for iid, c in stack]
                    seed[(s.x, s.y, s.z)] = stack or [(s.to_id, 1)]
                sess.state.tiles.update(seed)
            # Put the bot at the start and close the door — the scenario precondition.
            await gm.gm_run(flags, f"/tpto {CHARACTER}, {test.start[0]}, {test.start[1]}, "
                                   f"{test.start[2]}", *[f"/settile {a}" for a in closes])
            for _ in range(25):
                await asyncio.sleep(0.2)
                p = sess.state.position
                if p is not None and (p.x, p.y, p.z) == test.start:
                    break
            p = sess.state.position
            if p is None or (p.x, p.y, p.z) != test.start:
                measured[phase] = (False, 0, [])
                return
            sess.path_log = []                  # capture this pass's route (see the move hook)
            loop = asyncio.get_event_loop()
            arrived = await client.navigate_to(sess, flags, test.dest[0], test.dest[1],
                                                test.dest[2],
                                                deadline=loop.time() + test.time_limit)
            # Price the exact route walked: prepend the start (the move hook records only tiles
            # we STEPPED onto, not the one we began on), then sum step costs from the frozen
            # table.
            path = [test.start] + navcost.as_tiles(sess.path_log)
            sess.path_log = None
            measured[phase] = (arrived, navcost.route_cost(path, cost_of), path)

        try:
            await client._run_in_world(session, action)
        finally:
            try:
                await gm.gm_kick(flags, CHARACTER)
            except Exception as err:  # noqa: BLE001
                log.warning("teardown /kick failed: %s", err)

    for i, phase in enumerate([p for p in ("cold", "warm") if p in phases]):
        if i:
            await asyncio.sleep(3)   # space the logins out — Canary throttles rapid ones
        try:
            await run_one(phase)
        except Exception as err:  # noqa: BLE001 — a harness must not crash on one phase
            return Result(test.name, "ERROR", f"{phase}: {type(err).__name__}: {err}")

    # Assemble the report. A pass that didn't arrive is a hard FAIL; otherwise each measured
    # pass is checked against its own budget (None = report-only).
    par = fixture.par_cost or 1
    budgets = {"cold": test.cold_budget, "warm": test.warm_budget}
    parts: list[str] = []
    status = "PASS"
    for phase in ("cold", "warm"):
        if phase not in measured:
            continue
        arrived, cost, path = measured[phase]
        if not arrived:
            status = "FAIL"
            parts.append(f"{phase}: DID NOT ARRIVE")
            continue
        ratio = cost / par
        gate = budgets[phase]
        flag = ""
        if gate is not None and ratio > gate:
            status = "FAIL"
            flag = f" >budget {gate:.2f}"
        parts.append(f"{phase} {cost} (x{ratio:.2f}{flag})")
        if trace:
            print(f"    {phase} route ({cost}, x{ratio:.2f}):\n{_trace(path, cost_of)}")
    if "cold" in measured and "warm" in measured \
            and measured["cold"][0] and measured["warm"][0]:
        gain = measured["cold"][1] - measured["warm"][1]
        pct = 100 * gain / (measured["cold"][1] or 1)
        parts.append(f"improvement {gain} ({pct:+.0f}%)")
    if trace:
        print(f"    optimal floor ({par}):\n{_trace(fixture.par_path, cost_of)}")
    return Result(test.name, status, f"par={par}; " + ", ".join(parts))


async def run_all(filter_substr: str | None, phases: list[str] | None = None,
                  trace: bool = False, rebuild_fixture: bool = False) -> int:
    phases = phases or ["cold", "warm"]
    tests = [t for t in TESTS
             if not filter_substr or filter_substr.lower() in t.name.lower()]
    if not tests:
        print(f"no tests match {filter_substr!r}")
        return 2
    results = []
    for i, t in enumerate(tests):
        if i:
            await asyncio.sleep(3)   # space logins out — Canary throttles rapid ones
        try:
            if t.measure_efficiency:
                results.append(await run_efficiency(t, phases, trace, rebuild_fixture))
            else:
                results.append(await run_test(t))
        except Exception as err:     # noqa: BLE001 — one test's failure isn't the suite's
            results.append(Result(t.name, "ERROR", f"{type(err).__name__}: {err}"))
    print("\n" + "=" * 60 + "\nNavigation test results:")
    for r in results:
        print(r.line())
    npass = sum(r.status == "PASS" for r in results)
    print(f"\n{npass}/{len(results)} passed"
          f"  ({sum(r.status=='FAIL' for r in results)} failed, "
          f"{sum(r.status=='SKIP' for r in results)} skipped, "
          f"{sum(r.status=='ERROR' for r in results)} errored)")
    # Exit non-zero if anything didn't pass (SKIP counts as not-pass for CI honesty).
    return 0 if npass == len(results) else 1


def main() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="Run antbot navigation tests.")
    ap.add_argument("filter", nargs="?", default=None,
                    help="only run tests whose name contains this substring")
    ap.add_argument("--phase", choices=["cold", "warm", "both"], default="both",
                    help="efficiency tests: run only the COLD pass, only the WARM pass, or "
                         "both (default). Lets you iterate on exploration vs routing "
                         "separately.")
    ap.add_argument("--trace", action="store_true",
                    help="efficiency tests: print each route's per-step cost trace (and the "
                         "optimal floor) so you can see WHERE a slow route lost time.")
    ap.add_argument("--rebuild-fixture", action="store_true",
                    help="efficiency tests: re-reveal the region and recompute the frozen "
                         "par_cost floor before scoring (use after changing the map or the "
                         "cost model).")
    args = ap.parse_args()
    phases = ["cold", "warm"] if args.phase == "both" else [args.phase]
    raise SystemExit(asyncio.run(run_all(args.filter, phases, args.trace,
                                         args.rebuild_fixture)))


if __name__ == "__main__":
    main()
