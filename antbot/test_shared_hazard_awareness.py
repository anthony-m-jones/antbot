"""Regression test for a specific gap: does the LOCAL walker (`nav.find_path_toward`,
used by `client._walk_to`) respect a floor-change hazard that the COLONY already knows
about globally, even when THIS bot has never personally laid eyes on that exact tile?

Background: `travel()`'s shared router (`nav.find_route`) already gets this right — it
checks membership in `colony.get_step_unconfirmed()`, a set built from EVERY bot's
sightings, so a hazard Bot A discovered stays respected when Bot B routes through the
same area later, even if Bot B never personally saw it. `find_path_toward` did NOT do
this: its gamble-detection tried to CLASSIFY the tile from `state.tiles` — this bot's
own private, locally-parsed view — so a tile known only through the colony's shared
walkable set (reachable via `extra_walkable`, but never personally rendered) silently
fell through as ordinary ground, because there was no local item stack to classify.

This test constructs exactly that situation with REAL production data (an actual
"ladder" item id, 433, confirmed floorchange=down in items.xml, pulled from one of the
frozen nav-efficiency fixtures) rather than an invented stand-in:

  - `state.tiles` — THIS bot's own view — contains the start tile and the SAFE (west)
    route, but deliberately NOT the hazard tile's contents (simulating "I've scouted
    the safe way around, but never personally looked at what's directly north of me").
  - `extra_walkable` — the COLONY's shared walkable set — contains BOTH routes,
    including the hazard (simulating "some other bot walked past it once and reported
    the ground as walkable").
  - a real TraversalRegistry, seeded from the actual item catalog, so classifying 433
    correctly returns a STEP/DOWN "stairs_down" kind, matching production exactly.

Two assertions, both meaningful permanently (not just today):

  1. WITHOUT global awareness (only `registry`+`confirmed_links`, the parameters that
     existed before this fix): the walker heads NORTH — straight at the hazard — because
     it cannot classify a tile it's never locally seen. This documents the KNOWN, accepted
     limit of local-only classification; it's expected to keep failing this way forever,
     by design (a bot genuinely blind to a tile can't be expected to reason about it).

  2. WITH global awareness (`step_unconfirmed=colony.get_step_unconfirmed()`, the NEW
     parameter this fix adds): the walker no longer heads toward the hazard. It does NOT
     necessarily find the safe route here — `find_path_toward` is a myopic "walk to
     whichever reachable tile is geometrically closest" planner (see its own docstring),
     and the safe tile in this exact layout is actually FARTHER from the goal in raw
     distance than standing still (the real route has to detour away from the goal before
     it can approach it, which is `travel()`'s full-route job, not this local walker's).
     The correct, provable claim is narrower and still exactly what we want: global
     awareness should make this bot behave IDENTICALLY to a bot that saw the hazard with
     its own eyes — i.e., match what local classification ALREADY correctly does today
     (confirmed separately below: it reports "nothing reachable is closer," not a route
     through the hazard). THIS assertion is the one that should fail before the fix (the
     parameter doesn't exist yet: TypeError) and pass after it.

Run directly: `python -m antbot.test_shared_hazard_awareness`
"""
from __future__ import annotations

import sys

from .catalog import load_item_catalog
from .items import ItemFlags
from .nav import find_path_toward
from .traversal import TraversalRegistry
from .world import GameState, Position

APPEAR = r"C:\Users\Anthony\Documents\TibiaOT\canary\data\items\appearances.dat"
ITEMS_XML = r"C:\Users\Anthony\Documents\TibiaOT\canary\data\items\items.xml"

# Real coordinates + item ids pulled from
# antbot/navfixtures/efficiency-unconfirmed-z-hop-crossroads-no-climb-needed.json —
# item 433 is a genuine "a ladder" (floorchange=down in items.xml); 408 is a genuine
# "wooden floor" (ordinary ground, no floorchange at all). The goal sits north-east of
# start, so — exactly as it did live — the hazard is geometrically CLOSER to the goal
# than the safe route, which is precisely what lures an unaware planner toward it.
START = (32386, 32241, 7)
HAZARD = (32386, 32240, 7)     # north of start: item 433, "a ladder" — a real STEP hazard
SAFE = (32385, 32241, 7)       # west of start: item 408, "wooden floor" — ordinary ground
GOAL_X, GOAL_Y = 32389, 32238  # north-east of start


def _build_state() -> GameState:
    state = GameState()
    state.position = Position(START[0], START[1], START[2])
    # THIS bot's own view: the start tile and the safe route only. No entry for HAZARD —
    # that's the whole point: this bot has never personally looked at that tile.
    state.tiles[START] = [(408, 1)]
    state.tiles[SAFE] = [(408, 1)]
    return state


def _build_registry() -> TraversalRegistry:
    catalog = load_item_catalog(ITEMS_XML)
    registry = TraversalRegistry()
    registry.seed_from_catalog(catalog)
    return registry


def main() -> int:
    flags = ItemFlags.load(APPEAR)
    registry = _build_registry()

    # Sanity check on the premise itself: item 433 really must classify as a STEP/DOWN
    # hazard, or this whole test is testing nothing. The exact CATEGORY LABEL isn't the
    # point (items.xml-driven classification can legitimately call it "hole_open" or
    # "stairs_down" depending on its declared type) — what matters, and what the code
    # under test actually checks, is the KIND: action=step, direction=down.
    hit = registry.classify([433])
    assert hit is not None, "premise broken: item 433 no longer classifies as anything"
    kind = registry.kind(hit[0])
    assert kind is not None and kind.action == "step" and kind.direction == "down", (
        f"premise broken: item 433's kind isn't a STEP/DOWN hazard any more "
        f"(got category={hit[0]!r}, kind={kind})")

    # The colony's shared walkable set: BOTH routes, since some bot has walked past both.
    extra_walkable = {START, SAFE, HAZARD}

    failures: list[str] = []

    # --- Reference: what does a bot that HAS personally seen the hazard already do? ---
    # This is the behavior global awareness needs to MATCH (parity), not something new to
    # invent — `find_path_toward` is a myopic "closest reachable tile" walker (see its own
    # docstring), and the safe tile here is actually farther from the goal in raw distance
    # than standing still (the real route must detour away from the goal first — that's
    # `travel()`'s full-route job). So the honest, correct outcome even WITH full knowledge
    # is "nothing reachable is closer" — not a route through the hazard.
    ref_state = _build_state()
    ref_state.tiles[HAZARD] = [(433, 1)]   # this bot HAS genuinely seen it, for reference
    ref_path = find_path_toward(ref_state, flags, GOAL_X, GOAL_Y,
                                extra_walkable=extra_walkable,
                                registry=registry, confirmed_links=set())
    print(f"  [reference: seen it locally] path = {ref_path!r} "
          f"(the target outcome: correctly refuses to head at the hazard)")
    assert ref_path is None or ref_path[0] != "north", (
        "premise broken: even WITH local knowledge, find_path_toward heads at the "
        "hazard — something changed in the local-classification path itself")

    # --- Assertion 1: local-only classification is (expectedly, permanently) blind ---
    state1 = _build_state()
    path1 = find_path_toward(state1, flags, GOAL_X, GOAL_Y,
                             extra_walkable=extra_walkable,
                             registry=registry, confirmed_links=set())
    print(f"  [local-only]  first step = {path1[0] if path1 else None!r} "
          f"(expected 'north' — heads straight at the unseen hazard)")
    if not path1 or path1[0] != "north":
        failures.append(
            "local-only classification unexpectedly avoided the hazard — either the "
            "premise changed, or find_path_toward's local behavior was altered")

    # --- Assertion 2: global awareness should match the reference (seen-it) behavior ---
    state2 = _build_state()
    try:
        path2 = find_path_toward(state2, flags, GOAL_X, GOAL_Y,
                                 extra_walkable=extra_walkable,
                                 registry=registry, confirmed_links=set(),
                                 step_unconfirmed={HAZARD})
    except TypeError as err:
        print(f"  [global-aware] CRASHED: {err}")
        print("                 (expected before the fix: find_path_toward has no "
              "step_unconfirmed parameter yet)")
        failures.append(f"global-aware call not yet supported: {err}")
    else:
        print(f"  [global-aware] path = {path2!r} "
              f"(expected to match the reference — no longer heads at the hazard)")
        if path2 != ref_path:
            failures.append(
                f"with step_unconfirmed passed, doesn't match the seen-it-locally "
                f"reference (got {path2!r}, reference was {ref_path!r}) — global "
                f"awareness isn't behaving like local awareness would")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: local walker now respects colony-wide hazard knowledge it has never "
          "personally seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
