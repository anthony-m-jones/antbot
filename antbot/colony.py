"""The colony coordinator — shared state for the ant-farm dashboard (Phase C).

One `Colony` object is the single source of truth the dashboard reads: where each
bot currently is, and a shared *explored map* that every bot writes the tiles it
parses into. It's exactly the Tibia-minimap idea — the world colours itself in as
the colony walks around, and all bots contribute to (and could later read from)
the same map.

THREADING
The bots run in an asyncio event loop (main thread); the web server runs in a
separate daemon thread and reads this state to answer requests. So every access
goes through a lock — bots mutate under it, the server snapshots under it.

COORDINATES
Tiles are keyed by absolute (x, y, z). The dashboard draws one floor at a time
(the floor its bots are on), since floors overlap in x/y.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from .assets import items_xml_path
from .catalog import impassable_ground_ids, load_item_catalog
from .items import ItemFlags, automap_color_to_rgb
from .traversal import TraversalRegistry
from .world import GameState

log = logging.getLogger("antbot")

# Fallback minimap colour (index) for a tile whose items declare none — a neutral
# grey so unknown-but-seen ground still shows up.
_DEFAULT_COLOR_INDEX = 86  # rgb(102,102,102)

# Per-tile "ground speed" assumed for a tile whose ground declares no `bank`, and for
# ETA over tiles we haven't priced. Tibia step time ≈ groundSpeed*1000/playerSpeed, so
# bigger means SLOWER. Real per-tile speeds now come from appearances via
# `_walk_cost`; this is only the fallback. Matches nav.DEFAULT_GROUND_SPEED.
_DEFAULT_GROUND_SPEED = 150
_AVG_GROUND_SPEED = _DEFAULT_GROUND_SPEED   # kept: still the stand-in for unpriced ETA

# Where learned hazards persist between runs, so the colony doesn't have to
# re-discover (and re-lose a bot to) the same teleport every launch.
_HAZARD_FILE = Path(__file__).with_name("learned_hazards.json")

# Canary's server item catalog (names + floorchange/type), keyed by the same id
# the client uses. Location follows ANTBOT_CANARY_DIR (see assets.py) so a standalone
# antbot checkout can point at any Canary tree; the CLI --items-xml overrides it.
_CATALOG_FILE = items_xml_path()

# How long a claimed task may sit before we assume its bot died/relogged/got wedged and
# hand the work to someone else. Long enough that a slow haul isn't stolen mid-trip.
_TASK_CLAIM_TTL = 180.0

# Confidence half-life for loot sightings (seconds). NOT an expiry — sightings are only
# ever deleted by direct observation (see LootSighting). This just discounts the score of
# stale intel. Generous on purpose: items.xml gives ordinary loot no decay at all, so a
# sighting is usually still true; the honest value here is something to LEARN from bot
# feedback rather than guess. Corpse loot is the real exception (it rots in ~300s stages).
_LOOT_HALF_LIFE = 600.0

# Most sightings we keep. If we ever exceed it we drop the LEAST valuable, never the
# oldest — bounding memory must not throw away the crystal coin we haven't fetched yet.
_LOOT_MAX = 4000
# Distinct bots that must fail to walk to a pile before we write it off for good. More
# than one, because a single failure usually says more about our navigation than about
# the map; low enough that the swarm stops wasting trips on it quickly.
_UNREACHABLE_STRIKES = 2


@dataclasses.dataclass
class LootSighting:
    """"At time `last_seen`, this tile held this loot" — a cache, not a fact.

    WHY THERE IS NO TTL. It's tempting to expire sightings on a timer, but the server
    says that would be inventing an expiry the game doesn't have: ordinary loot
    (`gold coin`, `snakebite rod`, `leather boots`, …) carries NO `duration`/`decayTo`
    in items.xml and `cleanProtectionZones` is off, so loose items sit on the floor
    indefinitely. Only CORPSES rot (a dead rat is `duration=300 decayTo=…`, decaying in
    stages), and their loot goes with them. So age is not evidence of absence, and
    forgetting a crystal coin because five minutes passed would be daft.

    Instead: we only ever delete a sighting on DIRECT OBSERVATION — a bot stands there
    (or walks past) and the loot is gone. That's ground truth and it's free, since every
    bot already parses every tile in its view. Age only DISCOUNTS the score (see
    `confidence`), it never deletes: a half-confidence crystal coin still beats a fresh
    gold coin, which is exactly the ordering we want.
    """

    tile: tuple[int, int, int]
    items: list[tuple[int, int]]      # (item_id, count) — takeable things only
    value: int                        # best_sell total, priced by the economy catalog
    first_seen: float
    last_seen: float
    misses: int = 0                   # times a bot looked here and found nothing

    def confidence(self, now: float, half_life: float = _LOOT_HALF_LIFE) -> float:
        """P(still there), decaying with age. Never reaches 0 — only observation does.

        The half-life is a starting guess, deliberately generous because the item data
        says loose loot doesn't decay at all. It's the thing to LEARN from feedback:
        every hauler arrival is a labelled sample ("sighting aged A -> still there?"),
        which is an empirical survival curve rather than a number we made up.
        """
        age = max(0.0, now - self.last_seen)
        return 0.5 ** (age / half_life) if half_life > 0 else 1.0

    def expected_value(self, now: float) -> float:
        """Value discounted by how sure we are it's still there — what scoring wants."""
        return self.value * self.confidence(now)


@dataclasses.dataclass
class Task:
    """One unit of work the colony wants done, handed to a role-bot that asks for it.

    This is the seed of the "blackboard": instead of every bot deciding for itself what
    to do forever (explore), the colony posts tasks and idle role-bots claim them, do
    them, and report back. The hauler is the first consumer; suppliers/hunters slot into
    the same lifecycle later.

    Lifecycle: open -> claimed (by a bot) -> done | failed. A claim that goes stale (the
    bot died, relogged, or got stuck) is returned to `open` by `claim_task` so the work
    isn't lost with the bot — see _TASK_CLAIM_TTL.
    """

    id: int
    kind: str                                   # "haul" (more roles later)
    pickup: tuple[int, int, int]                # where to collect from
    dropoff: tuple[int, int, int]               # where to deliver to
    status: str = "open"                        # open | claimed | done | failed
    assignee: str | None = None                 # bot name holding the claim
    claimed_at: float = 0.0                     # monotonic-ish wall time of the claim
    note: str = ""                              # short human string for the dashboard
    created: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class BotView:
    """A single bot as the dashboard sees it."""

    name: str          # our label for the bot (e.g. the character name)
    character: str     # in-game character name
    x: int
    y: int
    z: int
    status: str        # short human string: "exploring", "idle", ...
    updated: float     # time.time() of the last update
    role: str = "scout"  # "scout" | "explore" | (future) hauler/supplier/hunter — the
                         # dashboard groups and filters bots by this.
    # Vitals (None until the first stats packet arrives). hp_percent is derived
    # for the compact dashboard display.
    hp: int | None = None
    max_hp: int | None = None
    mana: int | None = None
    max_mana: int | None = None
    level: int | None = None
    # What the bot is TRYING to do right now. `goal` is the tile it wants to reach;
    # `plan` is the route it found. goal set + plan empty = it wants to go somewhere it
    # can't path to, which from the outside is indistinguishable from a frozen bot. The
    # dashboard draws both so that difference is visible at a glance.
    goal: tuple[int, int, int] | None = None
    plan: list = dataclasses.field(default_factory=list)
    # Debug overlay inputs (only meaningful for the focused bot). `blocked` is this
    # bot's runtime-refused tiles as (x, y, z, remaining_ttl); `creatures` is the tiles
    # creatures occupy on its floor. Both are per-bot and live, unlike the colony-wide
    # hazard/unreachable sets, which is exactly the distinction the block inspector draws.
    blocked: list = dataclasses.field(default_factory=list)
    creatures: list = dataclasses.field(default_factory=list)

    @property
    def stuck(self) -> bool:
        """Wants to be somewhere, but has no route there."""
        return self.goal is not None and not self.plan

    @property
    def hp_percent(self) -> int | None:
        if self.hp is None or not self.max_hp:
            return None
        return max(0, min(100, round(self.hp * 100 / self.max_hp)))


class Colony:
    """Thread-safe shared state: live bot views + the shared explored map."""

    def __init__(self, item_flags: ItemFlags, hazard_file: Path | None = _HAZARD_FILE,
                 catalog_file: Path | None = _CATALOG_FILE) -> None:
        self._item_flags = item_flags
        self._lock = threading.Lock()
        self._bots: dict[str, BotView] = {}
        # The blackboard: posted work role-bots claim (see Task / add_task / claim_task).
        self._tasks: dict[int, Task] = {}
        self._next_task_id = 1
        # The loot map: (x,y,z) -> LootSighting. The swarm's shared eyes — anything any
        # bot walks past is fetchable by any hauler. Only direct observation removes an
        # entry; see LootSighting for why there's no TTL.
        self._loot: dict[tuple[int, int, int], LootSighting] = {}
        # Tiles whose loot can never be collected — sealed scenery (the priced crystal
        # coins behind a shop counter) or somewhere every bot fails to walk. Kept OUT of
        # the loot map entirely: our scorer ranks by value, so one unreachable pile of
        # coins outbids every real pile in town and haulers queue to fail on it. Saved
        # with the rest of the world knowledge — it's a fact about the map, and
        # re-learning it costs a wasted trip every restart.
        self._unreachable: set[tuple[int, int, int]] = set()
        # tile -> the bots that have failed to reach it (see report_unreachable). Not
        # persisted: it's evidence in progress, not a conclusion.
        self._unreachable_hits: dict[tuple[int, int, int], set] = {}
        # {age_minutes: (still_there, gone)} — measured, so the confidence half-life can
        # stop being a guess and start being data.
        self._loot_survival: dict[int, tuple[int, int]] = {}
        # Where self-directed haulers deliver what they find. Set by the farm (the temple,
        # for now). The real answer is "the nearest depot" — depots are shared per
        # character, so staged hauls to a local depot beat a long trip to a vendor — but
        # that needs depot positions and the merchant role. Until then: one stash.
        self.stash: tuple[int, int, int] | None = None
        # (x, y, z) -> minimap colour index (0-215). One entry per explored tile.
        self._explored: dict[tuple[int, int, int], int] = {}
        # (x, y, z) tiles that are walkable (ground, no blocking item). This is the
        # shared graph the world-wide route finder walks over (see nav.find_route),
        # so a bot can route across regions/floors using everyone's exploration.
        self._walkable: set[tuple[int, int, int]] = set()
        # tile -> what it costs to cross (Tibia groundSpeed; bigger = slower). Filled in
        # by contribute_tiles, consumed by the router so routes prefer roads over sand.
        self._walk_cost: dict[tuple[int, int, int], int] = {}
        # Tiles that whisk you away — stairs/ramps/holes (floor change) or
        # teleporters (position jump). We can't spot these from item data ahead of
        # time (the modern assets don't flag floor-change, and teleport
        # destinations are map-placed), so bots discover them by stepping on one.
        #
        # We keep two things:
        #  - `_hazards`: source tiles to AVOID while wandering, so bots stay local.
        #  - `_links`:   source -> destination edges, so those same tiles can be
        #                used as SHORTCUTS when routing to a far goal (a teleport
        #                is a hazard to a wanderer but a highway to a traveller).
        # Both persist to disk so the colony's map knowledge carries across runs.
        self._hazard_file = hazard_file
        self._hazards: set[tuple[int, int, int]] = set()
        self._links: dict[tuple[int, int, int], tuple[int, int, int]] = {}
        # Source tiles proven NOT to be a usable shortcut (travel stepped on one
        # and it didn't relocate us) — never trust or re-add these. This is how a
        # mis-recorded teleport source gets pruned so routing stops relying on it.
        self._bad_links: set[tuple[int, int, int]] = set()
        # Learned item-id -> object category (teleporter/stairs/...), so the colony
        # can recognise shortcut objects on sight. Shared by all bots. We seed it
        # from Canary's items.xml (ground truth: names + floorchange/type keyed by
        # the same client id we see on tiles), so ladders/holes/teleporters/doors
        # are known from the first frame instead of only after a bot stumbles onto
        # one. The catalog also gives us item NAMES (appearances.dat leaves world
        # objects unnamed), handy for the dashboard and logs.
        self.catalog: dict = {}
        self.traversal = TraversalRegistry()
        # What items are WORTH and what they cost to carry (see economy.py). Built once
        # here and shared by every bot, so a hauler can price a pile without re-reading
        # items.xml and a thousand NPC scripts per bot. Empty if the data isn't there —
        # the hauler then falls back to taking whatever it can lift.
        self.economy: dict = {}
        if catalog_file is not None:
            try:
                self.catalog = load_item_catalog(catalog_file)
                self.traversal.seed_from_catalog(self.catalog)
                self.traversal.seed_curated()  # Lua-script objects items.xml misses
                # Teach walkability that open water / lava ground is impassable, even
                # though appearances.dat under-flags it. Shared item_flags object, so
                # this reaches every bot's pathfinding too.
                water = impassable_ground_ids(self.catalog)
                self._item_flags.add_impassable_ground(water)
                log.info("colony: marked %d water/lava ground ids impassable", len(water))
                # Reuse the catalog we just parsed rather than reading items.xml twice.
                from .economy import load_economy
                self.economy = load_economy(item_flags=self._item_flags, catalog=self.catalog)
            except (OSError, ValueError) as err:
                log.warning("colony: could not load item catalog %s: %s", catalog_file, err)
        # A rolling log of noteworthy things bots hit (a bot got stuck, a shortcut
        # misbehaved, an object surprised us). Surfaced on the dashboard so we can
        # triage real issues into fixes. Bounded so it never grows without limit.
        self._events: deque = deque(maxlen=300)
        self._load_knowledge()

    def name_of(self, item_id: int) -> str:
        """Human name for an item id from the server catalog, or "" if unknown.

        appearances.dat leaves world objects unnamed, so this is our only source of
        readable names (e.g. tile id 433 -> "ladder"). Handy for the dashboard/logs.
        """
        info = self.catalog.get(item_id)
        return info.full_name if info is not None else ""

    def log_event(self, bot: str, level: str, category: str, message: str,
                  position=None) -> None:
        """Record a warning/error/info from a bot for the dashboard's issue list.

        `level` is "info" | "warning" | "error"; `category` is a short slug
        ("stuck", "shortcut", "anomaly", ...); `position` is an optional (x,y,z).
        Thread-safe. Also mirrored to the normal logger.
        """
        pos = None
        if position is not None:
            if hasattr(position, "x"):          # a world.Position
                pos = [position.x, position.y, position.z]
            else:                               # a plain (x, y, z) tuple
                pos = [position[0], position[1], position[2]]
        entry = {"time": time.time(), "bot": bot, "level": level,
                 "category": category, "message": message, "pos": pos}
        with self._lock:
            self._events.append(entry)
        log.log(logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO,
                "[%s] %s/%s: %s%s", level, bot, category, message,
                f" @ {pos}" if pos else "")

    def _load_knowledge(self) -> None:
        if not (self._hazard_file and self._hazard_file.exists()):
            return
        try:
            data = json.loads(self._hazard_file.read_text())
            if isinstance(data, list):
                # Legacy format: a bare list of hazard sources (no destinations).
                self._hazards = {tuple(t) for t in data}
            else:
                self._hazards = {tuple(t) for t in data.get("hazards", [])}
                self._links = {tuple(s): tuple(d) for s, d in data.get("links", [])}
                self._bad_links = {tuple(t) for t in data.get("bad_links", [])}
                self._unreachable = {tuple(t) for t in data.get("unreachable", [])}
            log.info("colony: loaded %d hazards, %d links, %d pruned, %d unreachable from %s",
                     len(self._hazards), len(self._links), len(self._bad_links),
                     len(self._unreachable), self._hazard_file)
        except (OSError, ValueError) as err:
            log.warning("colony: could not load world knowledge: %s", err)

    def _save_knowledge(self) -> None:
        if not self._hazard_file:
            return
        try:
            payload = {
                "hazards": [list(t) for t in self._hazards],
                "links": [[list(s), list(d)] for s, d in self._links.items()],
                "bad_links": [list(t) for t in self._bad_links],
                "unreachable": [list(t) for t in self._unreachable],
            }
            self._hazard_file.write_text(json.dumps(payload))
        except OSError as err:
            log.warning("colony: could not save world knowledge: %s", err)

    # -- bot updates -------------------------------------------------------

    def update_bot(self, name: str, character: str, position, status: str,
                   hp: int | None = None, max_hp: int | None = None,
                   mana: int | None = None, max_mana: int | None = None,
                   level: int | None = None, role: str = "scout",
                   goal=None, plan=None, blocked=None, creatures=None) -> None:
        """Record where a bot is now, its vitals, and what it's trying to do.

        `goal`/`plan` drive the intent overlay; `blocked`/`creatures` the block
        inspector — see BotView.
        """
        if position is None:
            return
        with self._lock:
            self._bots[name] = BotView(name, character, position.x, position.y,
                                       position.z, status, time.time(), role,
                                       hp=hp, max_hp=max_hp, mana=mana,
                                       max_mana=max_mana, level=level,
                                       goal=tuple(goal) if goal else None,
                                       plan=list(plan or []),
                                       blocked=list(blocked or []),
                                       creatures=list(creatures or []))

    def bot_positions(self) -> dict:
        """{bot name -> (x, y, z)} for every bot currently reporting. Used by the
        manager to verify a reset-to-temple actually took (are they at the temple?)."""
        with self._lock:
            return {name: (b.x, b.y, b.z) for name, b in self._bots.items()}

    def remove_bot(self, name: str) -> None:
        with self._lock:
            self._bots.pop(name, None)

    # -- the loot map: what bots have SEEN lying around ---------------------

    def mark_unreachable(self, tile, reason: str = "") -> None:
        """Record that `tile`'s loot can never be collected, and forget the sighting.

        Two callers, two kinds of evidence:
          - structural (`has_standable_neighbour`): there is nowhere to stand next to it,
            so it is sealed scenery — certain, and known the moment we lay eyes on it;
          - experiential (`report_unreachable`): bots keep failing to walk there.

        This is a permanent judgement, so it must only be made on real evidence. Seeing
        a pile and being unable to route to it is NOT evidence — the map is simply
        incomplete most of the time — which is why nothing here keys off the router.
        """
        key = tuple(tile)
        with self._lock:
            if key not in self._unreachable:
                self._unreachable.add(key)
                log.info("colony: %s is unreachable%s; dropping it from the loot map",
                         key, f" ({reason})" if reason else "")
            self._loot.pop(key, None)
            self._unreachable_hits.pop(key, None)
        self._save_knowledge()

    def report_unreachable(self, tile, bot_name: str) -> bool:
        """A bot failed to WALK to `tile`. Returns True once we give up on it for good.

        One failure means very little — our walker gets myopic, other bots block doors,
        a route may just not be explored yet. So we count distinct failures and only
        condemn the tile after several, which keeps a temporarily-awkward pile in play
        while still retiring one that everybody keeps failing on.
        """
        key = tuple(tile)
        with self._lock:
            if key in self._unreachable:
                return True
            hits = self._unreachable_hits.setdefault(key, set())
            hits.add(bot_name)
            enough = len(hits) >= _UNREACHABLE_STRIKES
        if enough:
            self.mark_unreachable(key, reason=f"{_UNREACHABLE_STRIKES} bots failed to reach it")
        return enough

    def unreachable_tiles(self) -> set:
        """A copy of the tiles we've written off, for a hauler's candidate filter."""
        with self._lock:
            return set(self._unreachable)

    def report_loot(self, tile, items: list[tuple[int, int]]) -> None:
        """A bot can see takeable loot on `tile`. Prices it and remembers it.

        Called from anywhere a bot parses tiles, so the swarm's eyes are shared: a scout
        wandering past a pile makes it visible to every hauler. Re-reporting the same
        tile refreshes `last_seen` (that's a re-confirmation, which restores confidence).
        """
        if tuple(tile) in self._unreachable:
            return                      # visible, priced, and provably not collectable
        if not items:
            self.forget_loot(tile)      # seen, and there's nothing here
            return
        value = 0
        for item_id, count in items:
            info = self.economy.get(item_id)
            if info is not None:
                value += info.best_sell * max(1, count)
        # Only track loot worth FETCHING. Bots can lift plenty of scenery no NPC buys,
        # and a map full of it is noise a hauler has to wade through. "Seen but
        # worthless" and "seen and empty" mean the same thing to a hauler — no work here
        # — so both forget. (Bags are worth ~0 too, but they're an outfitting decision
        # made from the catalog, not something we fetch off the floor. See BACKLOG.)
        if value <= 0:
            self.forget_loot(tile)
            return
        now = time.time()
        key = tuple(tile)
        with self._lock:
            existing = self._loot.get(key)
            if existing is None:
                self._loot[key] = LootSighting(key, list(items), value, now, now)
            else:
                existing.items = list(items)
                existing.value = value
                existing.last_seen = now
                existing.misses = 0     # we can see it: it's there
            if len(self._loot) > _LOOT_MAX:
                # Bound memory by VALUE, never by age — dropping the oldest would be
                # dropping exactly the valuable pile nobody has fetched yet.
                worst = min(self._loot.values(), key=lambda s: s.value)
                self._loot.pop(worst.tile, None)

    def forget_loot(self, tile) -> None:
        """A bot looked at `tile` and the loot is gone — the only thing that deletes.

        Also feeds the survival stats: we learn "a sighting of age A turned out to be
        stale", which is how the confidence half-life stops being a guess.
        """
        key = tuple(tile)
        with self._lock:
            sighting = self._loot.pop(key, None)
            if sighting is not None:
                self._note_outcome(time.time() - sighting.last_seen, survived=False)

    def confirm_loot(self, tile) -> None:
        """A bot is standing on `tile` and the loot IS still there — the other label."""
        key = tuple(tile)
        with self._lock:
            sighting = self._loot.get(key)
            if sighting is not None:
                self._note_outcome(time.time() - sighting.last_seen, survived=True)
                sighting.last_seen = time.time()

    def _note_outcome(self, age: float, survived: bool) -> None:
        """Record one labelled observation of "did a sighting this old still hold?".

        Bucketed by age so it's a survival curve we can read straight off, rather than a
        half-life we invented. Caller holds the lock.
        """
        bucket = int(age // 60)          # one-minute buckets
        hit, miss = self._loot_survival.get(bucket, (0, 0))
        self._loot_survival[bucket] = (hit + int(survived), miss + int(not survived))

    def loot_survival(self) -> dict[int, tuple[int, int]]:
        """{age_minutes: (still_there, gone)} — the measured survival of our intel."""
        with self._lock:
            return dict(self._loot_survival)

    def loot_snapshot(self, limit: int = 200) -> list[dict]:
        """The most valuable known loot (expected value, so stale intel ranks lower)."""
        now = time.time()
        with self._lock:
            best = sorted(self._loot.values(), key=lambda s: -s.expected_value(now))[:limit]
            return [
                {"tile": list(s.tile), "items": [list(i) for i in s.items],
                 "value": s.value, "expected": round(s.expected_value(now), 1),
                 "confidence": round(s.confidence(now), 3),
                 "age": round(now - s.last_seen, 1)}
                for s in best
            ]

    def _claimed_loot_tiles(self) -> set:
        """Loot tiles some bot is actively working. Caller holds the lock.

        A haul task's `pickup` IS the loot tile, so the blackboard already tells us who's
        on what — no second bookkeeping. Stale claims (dead/relogged bot) age out via
        _TASK_CLAIM_TTL and the pile becomes fair game again.
        """
        now = time.time()
        return {t.pickup for t in self._tasks.values()
                if t.status == "claimed" and now - t.claimed_at <= _TASK_CLAIM_TTL}

    def loot_candidates(self, z: int, limit: int = 40) -> list[LootSighting]:
        """Unclaimed sightings on floor `z`, richest first — the shortlist a hauler scores.

        We deliberately do NOT score here: only the bot knows what it can carry (its free
        capacity, its free slots, and which stacks it already holds — 90 more gold is free
        if it has 10). The colony's job is to offer candidates and settle races; the bot's
        job is to judge them. So this is a cheap pre-filter, and `claim_loot` is the
        atomic part.
        """
        now = time.time()
        with self._lock:
            claimed = self._claimed_loot_tiles()
            cands = [s for s in self._loot.values()
                     if s.tile[2] == z and s.tile not in claimed
                     and s.tile not in self._unreachable]
            cands.sort(key=lambda s: -s.expected_value(now))
            return cands[:limit]

    def claim_loot(self, tile, bot_name: str, dropoff, note: str = "") -> Task | None:
        """Atomically turn a loot sighting into a claimed haul task for `bot_name`.

        Returns None if the pile vanished or another hauler claimed it first — the caller
        just tries its next-best candidate. This is optimistic concurrency: bots score
        freely and only contend at the moment of claiming.
        """
        key = tuple(tile)
        now = time.time()
        with self._lock:
            if key not in self._loot:
                return None                      # someone looted it; intel is stale
            if key in self._claimed_loot_tiles():
                return None                      # another hauler got there first
            task = Task(id=self._next_task_id, kind="haul", pickup=key,
                        dropoff=tuple(dropoff), status="claimed", assignee=bot_name,
                        claimed_at=now, note=note)
            self._tasks[task.id] = task
            self._next_task_id += 1
        log.info("colony: %s self-claimed loot #%d at %s (%s)", bot_name, task.id, key, note)
        return task

    # -- the blackboard: posted work that role-bots claim -------------------

    def add_task(self, kind: str, pickup, dropoff, note: str = "") -> Task:
        """Post a unit of work for an idle role-bot to claim."""
        with self._lock:
            task = Task(id=self._next_task_id, kind=kind, pickup=tuple(pickup),
                        dropoff=tuple(dropoff), note=note)
            self._tasks[task.id] = task
            self._next_task_id += 1
        log.info("colony: posted task #%d %s %s -> %s", task.id, kind, task.pickup, task.dropoff)
        return task

    def claim_task(self, bot_name: str, kind: str | None = None,
                   avoid: set | None = None) -> Task | None:
        """Hand the oldest open task (of `kind`) to `bot_name`, or None if there's none.

        Also reclaims stale claims first: a bot that died/relogged/got stuck mid-task
        would otherwise strand that work forever, so a claim older than _TASK_CLAIM_TTL
        goes back to `open` for someone else.

        `avoid` is the caller's own set of pickups it has just failed to reach. Releasing
        an unreachable task returns it to `open`, and without this the same bot instantly
        re-claims it and retries forever — a busy-loop that burns the whole session on one
        unreachable pile. The task stays open on purpose: a bot standing somewhere else
        may well be able to get there.
        """
        now = time.time()
        avoid = avoid or set()
        with self._lock:
            for t in self._tasks.values():
                if t.status == "claimed" and now - t.claimed_at > _TASK_CLAIM_TTL:
                    log.info("colony: task #%d claim by %s went stale; reopening",
                             t.id, t.assignee)
                    t.status, t.assignee, t.claimed_at = "open", None, 0.0
            candidates = [t for t in self._tasks.values()
                          if t.status == "open" and (kind is None or t.kind == kind)
                          and tuple(t.pickup) not in avoid]
            if not candidates:
                return None
            task = min(candidates, key=lambda t: t.created)
            task.status, task.assignee, task.claimed_at = "claimed", bot_name, now
            return task

    def finish_task(self, task_id: int, ok: bool = True, note: str = "") -> None:
        """Mark a claimed task done (or failed, so we can see what didn't work)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.status = "done" if ok else "failed"
            if note:
                task.note = note
        log.info("colony: task #%d %s%s", task_id, "done" if ok else "FAILED",
                 f" ({note})" if note else "")

    def release_task(self, task_id: int) -> None:
        """Give a claimed task back without failing it (the bot couldn't get to it)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None and task.status == "claimed":
                task.status, task.assignee, task.claimed_at = "open", None, 0.0

    def tasks_snapshot(self) -> list[dict]:
        """All tasks as plain dicts (newest first) for the dashboard."""
        with self._lock:
            return [
                {"id": t.id, "kind": t.kind, "pickup": list(t.pickup),
                 "dropoff": list(t.dropoff), "status": t.status,
                 "assignee": t.assignee, "note": t.note,
                 "age": round(time.time() - t.created, 1)}
                for t in sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)
            ]

    def _tile_color(self, items: list[int]) -> int:
        """Pick a tile's minimap colour the way the Tibia client does.

        The client (`Tile::getMinimapColorByte`) colours a tile by the *topmost*
        structural thing that has a minimap colour, not the ground beneath it —
        which is why walls (drawn on top, and typically red) must win over the
        floor. We approximate that: prefer the colour of the topmost *blocking*
        item (walls/rocks/closed doors), so structures show up; otherwise fall
        back to the ground/terrain colour (first coloured item); otherwise grey.
        Loose non-blocking items are ignored so a dropped object doesn't repaint
        the tile.
        """
        for item_id in reversed(items):  # top of the stack downward
            if self._item_flags.is_blocking(item_id):
                color = self._item_flags.automap_color(item_id)
                if color is not None:
                    return color
        for item_id in items:  # ground/terrain
            color = self._item_flags.automap_color(item_id)
            if color is not None:
                return color
        return _DEFAULT_COLOR_INDEX

    def contribute_tiles(self, state: GameState) -> int:
        """Fold a bot's parsed tiles into the shared explored map.

        Returns how many *new* tiles this added, so callers can log exploration
        progress.
        """
        added = 0
        with self._lock:
            for (x, y, z), items in state.tiles.items():
                if (x, y, z) in self._explored:
                    continue
                # Tiles are [(id, count), ...]; the map only cares about the ids.
                ids = [item_id for item_id, _ in items]
                self._explored[(x, y, z)] = self._tile_color(ids)
                # Walkable = ground you can stand on: not impassable liquid (water/
                # lava, judged by the ground item) and nothing blocking on the tile.
                ground_ok = ids[0] not in self._item_flags.impassable_ground
                if ground_ok and not any(self._item_flags.is_blocking(item_id) for item_id in ids):
                    self._walkable.add((x, y, z))  # feeds the world-wide router
                    # What this tile costs to cross, so the router can prefer roads.
                    # Recorded here because this is the only place that still has the
                    # tile's ITEMS — `_walkable` is just coordinates by the time the
                    # router sees it. See nav.tile_cost for why bigger means slower.
                    speed = self._item_flags.ground_speed(ids[0])
                    self._walk_cost[(x, y, z)] = speed or _DEFAULT_GROUND_SPEED
                added += 1
        return added

    # -- shared hazard learning -------------------------------------------

    def report_link(self, source: tuple[int, int, int], dest: tuple[int, int, int]) -> None:
        """A bot found that stepping on `source` whisks it to `dest`.

        Records both the avoid-hazard (source) and the routing shortcut
        (source -> dest), and persists them. Ignored if the source was previously
        proven to be a dud (pruned), so we don't resurrect a bad link.
        """
        with self._lock:
            if source in self._bad_links:
                return
            changed = source not in self._hazards or self._links.get(source) != dest
            self._hazards.add(source)
            self._links[source] = dest
            if changed:
                self._save_knowledge()

    def confirm_link(self, source: tuple[int, int, int], dest: tuple[int, int, int]) -> None:
        """Travel actually took this shortcut and it led to `dest` — trust it.

        If `dest` differs from what we had recorded, correct it (self-healing when
        a source was right but the destination was mis-attributed).
        """
        with self._lock:
            if self._links.get(source) != dest:
                self._links[source] = dest
                self._save_knowledge()

    def mark_bad_link(self, source: tuple[int, int, int]) -> None:
        """Travel stepped on `source` and it did NOT relocate us — prune it.

        Removes it as a shortcut and remembers it as bad so it's never re-added.
        (Kept as a plain hazard so wanderers still avoid the odd tile, harmlessly.)
        """
        with self._lock:
            self._bad_links.add(source)
            self._links.pop(source, None)
            self._save_knowledge()

    def get_hazards(self) -> set[tuple[int, int, int]]:
        """A copy of all known hazard tiles, for a bot to fold into its avoid set."""
        with self._lock:
            return set(self._hazards)

    def get_links(self) -> dict[tuple[int, int, int], tuple[int, int, int]]:
        """A copy of all known teleport/floor-change shortcuts, for routing."""
        with self._lock:
            return dict(self._links)

    def get_walkable(self) -> set[tuple[int, int, int]]:
        """A copy of the shared walkable graph, for the world-wide route finder."""
        with self._lock:
            return set(self._walkable)

    def get_walk_costs(self) -> dict[tuple[int, int, int], int]:
        """Per-tile crossing time for the router (see nav.tile_cost). Bigger = slower."""
        with self._lock:
            return dict(self._walk_cost)

    def explored_floor(self, z: int) -> list:
        """Every explored tile on floor `z` as [x, y, css-colour] — the whole floor,
        not the live window, so the path-planner view can show it all at once."""
        with self._lock:
            return [
                [x, y, "#%02x%02x%02x" % automap_color_to_rgb(color)]
                for (x, y, fz), color in self._explored.items() if fz == z
            ]

    def plan_route(self, start: tuple, goal: tuple, speed: int = 200) -> dict:
        """Plan (don't execute) a route from `start` to `goal` over the shared map +
        learned teleport/stair/ladder links, for the dashboard's path inspector.

        Returns the tile path plus a summary: distance, a rough ETA (per-tile time
        scaled by `speed`), walk-step vs shortcut-hop counts, and gold cost. ETA and
        gold are ESTIMATES: per-tile time uses a flat ground speed until we extract
        real ground speeds, and gold is 0 until boat/carpet NPC travel exists (the
        route can't yet include those legs).
        """
        from .nav import find_route
        start, goal = tuple(start), tuple(goal)
        costs = self.get_walk_costs()
        route = find_route(self.get_walkable(), self.get_links(), start, goal,
                           costs=costs)
        if route is None:
            with self._lock:
                nlinks = len(self._links)
            return {"found": False, "start": list(start), "goal": list(goal),
                    "known_links": nlinks}
        path = [list(start)] + [list(step[2]) for step in route]
        hops = [list(step[2]) for step in route if step[1]]  # shortcut landings
        walk_steps = sum(1 for step in route if not step[1])
        tiles = len(route)
        speed = max(1, int(speed))
        # Tibia step time ≈ groundSpeed*1000/playerSpeed ms. We now know each tile's real
        # groundSpeed, so sum the actual grounds instead of assuming a flat one: a road
        # route and an equally-long trudge through sand no longer report the same ETA.
        # Teleport hops are instant and cost nothing. Unpriced tiles fall back to the
        # average.
        eta_seconds = round(
            sum(0 if is_tp else costs.get(landing, _AVG_GROUND_SPEED)
                for _dir, is_tp, landing in route) / speed, 1)
        return {
            "found": True, "start": list(start), "goal": list(goal),
            "path": path, "hops": hops,
            "summary": {"tiles": tiles, "walk_steps": walk_steps,
                        "shortcut_hops": len(hops), "eta_seconds": eta_seconds,
                        "gold_cost": 0},
        }

    # -- dashboard reads ---------------------------------------------------

    def snapshot(self, focus_name: str | None = None, radius: int = 130) -> dict:
        """A JSON-serialisable view for the dashboard.

        The colony can span whole regions once bots wander apart, so we send a
        *window* rather than every tile: the floor of the focused bot, centred on
        it, out to `radius` tiles. `focus_name` picks which bot to follow (from
        the dashboard's click); if it's missing or gone, we fall back to the first
        bot. This keeps the payload small and lets the user follow any bot — even
        far-flung ones the previous single-window view could never show. (The full
        explored map still lives in `_explored`; `explored_total` reports it.)
        """
        with self._lock:
            bots = list(self._bots.values())
            focus = None
            if focus_name is not None:
                focus = next((b for b in bots if b.name == focus_name), None)
            if focus is None and bots:
                focus = bots[0]

            floor = focus.z if focus else 7
            cx, cy = (focus.x, focus.y) if focus else (0, 0)
            tiles = [
                [x, y, "#%02x%02x%02x" % automap_color_to_rgb(color)]
                for (x, y, z), color in self._explored.items()
                if z == floor and abs(x - cx) <= radius and abs(y - cy) <= radius
            ]
            bot_dicts = [
                {"name": b.name, "character": b.character, "role": b.role,
                 "x": b.x, "y": b.y,
                 "z": b.z, "status": b.status, "age": round(time.time() - b.updated, 1),
                 "hp": b.hp, "max_hp": b.max_hp, "hp_percent": b.hp_percent,
                 "mana": b.mana, "max_mana": b.max_mana, "level": b.level,
                 # Intent overlay: where it's headed, the route it found, and whether
                 # it has no route at all (the signature of a "stuck" bot).
                 "goal": list(b.goal) if b.goal else None,
                 "plan": [list(t) for t in b.plan],
                 "stuck": b.stuck}
                for b in bots
            ]
            # Live creatures in the window, pooled from every bot's view. Deduped by the
            # server's creature id (two bots seeing the same monster = one dot), keeping
            # whichever sighting is freshest. These are LIVE (each bot prunes its own set
            # to its viewport) — not remembered positions; monster memory is a separate,
            # decaying thing (see BACKLOG). NPCs are stationary, monsters roam, players
            # are neither — kind lets the map colour them apart.
            creatures = {}
            for b in bots:
                for c in b.creatures:
                    if (c["z"] != floor
                            or abs(c["x"] - cx) > radius or abs(c["y"] - cy) > radius):
                        continue
                    prev = creatures.get(c["id"])
                    if prev is None or b.updated > prev[1]:
                        creatures[c["id"]] = (c, b.updated)
            creature_list = [
                {"x": c["x"], "y": c["y"], "kind": c["kind"],
                 "name": c["name"], "hp": c["hp"]}
                for c, _ts in creatures.values()
            ]
            now = time.time()
            events = [
                {**e, "ago": round(now - e["time"], 1)}
                for e in list(self._events)[-40:][::-1]  # newest first, last 40
            ]
        return {"floor": floor, "center": [cx, cy],
                "focus": focus.name if focus else None,
                "bots": bot_dicts, "tiles": tiles, "explored_total": len(self._explored),
                "creatures": creature_list, "events": events}

    def debug_tiles(self, focus_name: str | None = None, radius: int = 60) -> dict:
        """Categorised blocked tiles around the focused bot, for the block inspector.

        This is a DEBUG read — fetched only when the overlay is on — so it can afford to
        classify a window every poll. It exists to make the different KINDS of "blocked"
        visible, because they behave very differently and today they're invisible:

          structural  — seen, but can't stand: a wall/counter/closed door (`unpass`) or
                        water/lava ground. Global truth, never changes.
          runtime     — the SERVER refused us here at runtime (the focused bot's
                        `blocked_tiles`), each with its remaining TTL. This is the layer
                        that accumulates and boxes a scout in, then expires.
          hazard      — a known teleport / floor-change source the swarm avoids. Shared,
                        no expiry.
          unreachable — loot tiles written off as uncollectable. Persisted.
          occupied    — a creature is standing there right now. `find_path_toward` won't
                        route through these; `find_frontier` ignores them — which is one
                        way a frontier looks reachable but the walker can't get there.

        Everything is scoped to the focus floor and a `radius` box, and returned as
        {category: [[x, y, ...], ...]} in SCREEN-agnostic world coords.
        """
        with self._lock:
            bots = list(self._bots.values())
            focus = None
            if focus_name is not None:
                focus = next((b for b in bots if b.name == focus_name), None)
            if focus is None and bots:
                focus = bots[0]
            if focus is None:
                return {"floor": 7, "focus": None, "structural": [], "runtime": [],
                        "hazard": [], "unreachable": [], "occupied": [], "counts": {}}

            z = focus.z
            cx, cy = focus.x, focus.y

            def near(x, y):
                return abs(x - cx) <= radius and abs(y - cy) <= radius

            # Seen but not walkable = structural (wall / counter / water). _explored is
            # every seen tile; _walkable the standable subset; the difference is exactly
            # the terrain the router treats as solid.
            structural = [[x, y] for (x, y, fz) in self._explored
                          if fz == z and (x, y, z) not in self._walkable and near(x, y)]
            hazard = [[x, y] for (x, y, fz) in self._hazards
                      if fz == z and near(x, y)]
            unreachable = [[x, y] for (x, y, fz) in self._unreachable
                           if fz == z and near(x, y)]
            # The focus bot's runtime blocks, minus anything that's really a hazard (those
            # get OR'd into blocked_tiles on the bot) so the two categories stay distinct.
            haz = {(x, y, fz) for (x, y, fz) in self._hazards}
            runtime = [[x, y, ttl] for (x, y, bz, ttl) in focus.blocked
                       if bz == z and near(x, y) and (x, y, bz) not in haz]
            occupied = [[c["x"], c["y"]] for c in focus.creatures
                        if c["z"] == z and near(c["x"], c["y"])]

        return {
            "floor": z, "focus": focus.name, "center": [cx, cy],
            "structural": structural, "runtime": runtime, "hazard": hazard,
            "unreachable": unreachable, "occupied": occupied,
            "counts": {"structural": len(structural), "runtime": len(runtime),
                       "hazard": len(hazard), "unreachable": len(unreachable),
                       "occupied": len(occupied)},
        }
