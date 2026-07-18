"""The carry model — what a bot can actually pick up, and what's worth picking up.

A hauler is bound by TWO independent budgets, and the interesting behaviour lives in
the tension between them:

  WEIGHT  free capacity, straight from the 0xA0 stats packet (`state.capacity`). The
          server already accounts for vocation and level, so we never model that.
  SLOTS   free container slots: for every OPEN container, `capacity - len(contents)`
          (capacity comes from the 0x6E that opened it, see `state.container_caps`).

The slot budget is where stackables earn their keep. A stack holds up to
`economy.STACK_LIMIT` (100), so topping up a stack you ALREADY carry costs **zero**
extra slots — 90 more gold coins are free if you're holding 10. That's why a hauler
should hoover currency and be picky about bulky one-per-slot junk.

SCORING. Choosing what to take under two budgets is a 2-D knapsack — NP-hard exactly,
but a greedy pass is more than good enough here and is what `plan_pickup` does. The
trick is the metric: we score each candidate by

    value  /  ( weight_cost/free_weight  +  slot_cost/free_slots )

i.e. value per *fraction of the remaining budget it consumes*. Normalising each cost by
its own headroom means whichever budget is actually binding dominates automatically —
weight-bound and the heavy junk falls away; slot-bound and the non-stackables do, while
stackables you can top off score near-infinitely because they cost no slot at all. No
special cases, no hand-tuned weights.

NOT MODELLED HERE (by design): bags. A bag adds slots but costs weight and a slot, and
you don't decide that by staring at loot — it's a pre-run outfitting choice (see the
preparation model in BACKLOG). Keeping it out means this module answers exactly one
question: given what I'm carrying, what should I take?
"""

from __future__ import annotations

import logging
import math

from .economy import STACK_LIMIT, ItemEconomy
from .world import GameState

log = logging.getLogger("antbot")


def free_weight(state: GameState) -> int:
    """Weight we can still take on (0 if the server hasn't told us our capacity yet)."""
    return int(state.capacity or 0)


def free_slots(state: GameState) -> int:
    """Empty slots across every OPEN container.

    Containers whose capacity we don't know are skipped rather than guessed — an
    unopened nested bag contributes nothing until we open it (that's the recursive
    bag-opening item in BACKLOG).
    """
    total = 0
    for cid, items in state.containers.items():
        cap = state.container_caps.get(cid)
        if cap is None:
            continue
        total += max(0, cap - len(items))
    return total


def stack_room(state: GameState, item_flags, item_id: int) -> int:
    """How many more of `item_id` fit into stacks we ALREADY hold — for free.

    This is the whole reason stackables are cheap: these units cost no new slot.
    """
    if not item_flags.is_stackable(item_id):
        return 0
    room = 0
    for items in state.containers.values():
        for iid, count in items:
            if iid == item_id:
                room += max(0, STACK_LIMIT - count)
    return room


def marginal_cost(state: GameState, item_flags, econ: dict[int, ItemEconomy],
                  item_id: int, count: int = 1) -> tuple[int, int]:
    """(weight_cost, slot_cost) of taking `count` of `item_id` ON TOP of our load.

    Weight is simply per-unit weight x count. Slots depend on stacking: a
    non-stackable costs one slot each; a stackable first fills the room in stacks we
    already carry (free), and only then needs ceil(remainder / STACK_LIMIT) new slots.
    """
    info = econ.get(item_id)
    weight = (info.weight if info else 0) * count
    if item_flags.is_stackable(item_id):
        overflow = max(0, count - stack_room(state, item_flags, item_id))
        slots = math.ceil(overflow / STACK_LIMIT)
    else:
        slots = count
    return weight, slots


def value_of(econ: dict[int, ItemEconomy], item_id: int, count: int = 1) -> int:
    """Best gold an NPC would pay for `count` of this item (currency = face value)."""
    info = econ.get(item_id)
    return (info.best_sell if info else 0) * count


def score(value: int, weight_cost: int, slot_cost: int,
          free_w: int, free_s: int) -> float:
    """Value per fraction-of-remaining-budget consumed. Higher is better.

    Returns -inf when it simply doesn't fit. A cost of zero against BOTH budgets (e.g.
    a few coins topping off a stack, weightless) is free money, hence `inf`.
    """
    if weight_cost > free_w or slot_cost > free_s:
        return float("-inf")
    if value <= 0:
        return float("-inf")            # nobody buys it; not worth a slot
    frac = 0.0
    if free_w > 0:
        frac += weight_cost / free_w
    if free_s > 0:
        frac += slot_cost / free_s
    if frac <= 0:
        return float("inf")
    return value / frac


def plan_pickup(state: GameState, item_flags, econ: dict[int, ItemEconomy],
                candidates: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Greedily choose what to take from a pile, respecting BOTH budgets.

    `candidates` is [(item_id, count), ...] — what's lying there. Returns the subset
    (possibly with reduced counts) worth taking, best-first. Budgets are simulated as
    we go, so later picks see the headroom the earlier ones consumed. Stackables are
    split when only part of a stack fits.

    Greedy on a 2-D knapsack isn't optimal, but the error is tiny for realistic piles
    and the alternative (exact DP over two dimensions) isn't worth the complexity for
    a bot that can simply come back for the rest.
    """
    remaining_w = free_weight(state)
    remaining_s = free_slots(state)
    # Simulated stack room, so two picks of the same stackable don't both claim it.
    taken: list[tuple[int, int]] = []
    pool = [(iid, n) for iid, n in candidates if n > 0]

    while pool and (remaining_w > 0 or remaining_s > 0):
        best, best_score, best_take = None, float("-inf"), 0
        for idx, (iid, n) in enumerate(pool):
            take = _largest_that_fits(state, item_flags, econ, iid, n,
                                      remaining_w, remaining_s, taken)
            if take <= 0:
                continue
            w, s = _cost_with_taken(state, item_flags, econ, iid, take, taken)
            sc = score(value_of(econ, iid, take), w, s, remaining_w, remaining_s)
            if sc > best_score:
                best, best_score, best_take = idx, sc, take
        if best is None or best_score == float("-inf"):
            break                        # nothing left that fits and is worth gold
        iid, n = pool[best]
        w, s = _cost_with_taken(state, item_flags, econ, iid, best_take, taken)
        remaining_w -= w
        remaining_s -= s
        taken.append((iid, best_take))
        if best_take >= n:
            pool.pop(best)
        else:
            pool[best] = (iid, n - best_take)   # partial stack: leave the rest
    return taken


def _cost_with_taken(state, item_flags, econ, item_id, count, taken) -> tuple[int, int]:
    """marginal_cost, but accounting for what this same trip has already picked up.

    Without this, two picks of the same stackable would each think the existing stack
    room is theirs, and we'd under-count slots.
    """
    already = sum(n for iid, n in taken if iid == item_id)
    info = econ.get(item_id)
    weight = (info.weight if info else 0) * count
    if item_flags.is_stackable(item_id):
        room = max(0, stack_room(state, item_flags, item_id) - already)
        # Slots this pick needs = new stacks after (already + count) minus those the
        # earlier picks already paid for.
        before = math.ceil(max(0, already - stack_room(state, item_flags, item_id)) / STACK_LIMIT)
        after = math.ceil(max(0, already + count - stack_room(state, item_flags, item_id)) / STACK_LIMIT)
        slots = max(0, after - before)
        _ = room
    else:
        slots = count
    return weight, slots


def _largest_that_fits(state, item_flags, econ, item_id, n,
                       free_w, free_s, taken) -> int:
    """The biggest slice of `n` that fits both budgets (stackables can be split)."""
    if n <= 0:
        return 0
    lo, hi, best = 1, n, 0
    while lo <= hi:                      # binary search — costs are monotonic in count
        mid = (lo + hi) // 2
        w, s = _cost_with_taken(state, item_flags, econ, item_id, mid, taken)
        if w <= free_w and s <= free_s:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best
