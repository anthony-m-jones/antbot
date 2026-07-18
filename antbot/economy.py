"""The economy catalog — what an item is WORTH and what it COSTS to carry.

WHY THIS EXISTS
A hauler has to answer "is this pile worth the trip?", and that needs two facts per
item: its value (what an NPC will pay for it) and its carry cost (weight + slots).
The modern client answers the value half with the Cyclopedia — a live lookup of which
vendor buys an item and for how much. We're on 8.60, which has no Cyclopedia.

We don't need one. The server's OWN data has it all, on disk:

  - `items.xml`            -> weight, containerSize     (see catalog.py)
  - `data-otservbr-global/npc/*.lua` -> npcConfig.shop  -> what each NPC buys/sells

and, crucially, the shop tables are keyed by `clientId` — the SAME id space we already
read off tiles (see the items.xml note in catalog.py). So we can build a complete
"who buys this, for how much" map OFFLINE, which is strictly better than the
Cyclopedia: it's available at planning time, for every item at once, with no protocol
round-trips. You cannot ask a Cyclopedia about a pile you haven't walked to yet; you
CAN score it the instant a scout lays eyes on it.

    npcConfig.shop = { -- Sellable items
        { itemName = "crowbar", clientId = 3304, buy = 260, sell = 50 },
    }

  `buy`  = what the NPC charges YOU (we pay this)
  `sell` = what the NPC pays YOU  (we receive this)  <- the hauler's value

An item with no `sell` anywhere is worth nothing to a hauler no matter how pretty it
looks: nobody buys it. An item's realisable value is the BEST `sell` across every NPC
that buys it; actually turning that into gold costs a trip to that NPC, which is the
(later) merchant role's problem, not the hauler's.

We parse the Lua with a regex rather than executing it. The shop lines are uniform,
machine-generated, and every one of the ~10k entries carries a clientId, so a regex is
honest here — and it means we never run untrusted server scripts to read a price list.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path

from .catalog import ItemInfo, load_item_catalog

log = logging.getLogger("antbot")

# Where the server keeps its NPC scripts (the shop tables live inside them).
_NPC_DIR = Path(__file__).resolve().parents[2] / "canary" / "data-otservbr-global" / "npc"
_ITEMS_XML = Path(__file__).resolve().parents[2] / "canary" / "data" / "items" / "items.xml"

# `local internalNpcName = "Ahmet"` — the shop's owner, so we can later route to them.
_RE_NPC_NAME = re.compile(r'^local internalNpcName\s*=\s*"([^"]+)"', re.M)
# One shop row. `buy`/`sell` are independent and either may be absent.
_RE_SHOP_ROW = re.compile(
    r'\{\s*itemName\s*=\s*"(?P<name>[^"]*)"\s*,\s*clientId\s*=\s*(?P<id>\d+)'
    r'(?:\s*,\s*buy\s*=\s*(?P<buy>\d+))?'
    r'(?:\s*,\s*sell\s*=\s*(?P<sell>\d+))?'
)

# A stack of a stackable item holds at most this many (classic Tibia rule). It's what
# makes "top off the stack you already carry" cost ZERO extra slots — see carry.py.
STACK_LIMIT = 100

# CURRENCY is the one thing no price table can tell us: no NPC "buys" gold, because gold
# IS the money — so the shop tables price it at 0 and a naive catalog would tell haulers
# to walk past the most valuable loot in the game. Its worth is simply its face value.
# (Rates are the classic ones: 100 gold to a platinum, 100 platinum to a crystal.)
CURRENCY_VALUE: dict[int, int] = {
    3031: 1,        # gold coin
    3035: 100,      # platinum coin
    3043: 10000,    # crystal coin
}


@dataclasses.dataclass
class ItemEconomy:
    """Everything the hauler needs to price one item id."""

    item_id: int
    name: str = ""
    weight: int = 0                       # per UNIT (a stack of 5 weighs 5x this)
    stackable: bool = False
    container_size: int | None = None     # slots this bag adds, if it's a container
    best_sell: int = 0                    # best gold an NPC will PAY us (0 = nobody buys)
    sold_to: tuple[str, ...] = ()         # NPCs paying `best_sell`, for the merchant trip
    best_buy: int = 0                     # cheapest an NPC will SELL it to us (0 = none)
    bought_from: tuple[str, ...] = ()

    @property
    def value_per_weight(self) -> float:
        """Gold per unit weight — the tie-breaker when a hauler is weight-bound."""
        if self.weight <= 0:
            return float(self.best_sell)   # weightless-but-valuable: always worth taking
        return self.best_sell / self.weight


def parse_npc_shops(npc_dir: Path = _NPC_DIR) -> dict[int, dict[str, list]]:
    """Scan every NPC script and return {client_id: {"sell": [(price, npc)], "buy": [...]}}.

    `sell` entries are what that NPC PAYS us for the item; `buy` is what it charges us.
    A missing directory just yields an empty map (the catalog then prices nothing rather
    than exploding) — the caller logs it.
    """
    shops: dict[int, dict[str, list]] = {}
    if not npc_dir.exists():
        log.warning("economy: no NPC directory at %s — items will have no prices", npc_dir)
        return shops
    files = 0
    for path in sorted(npc_dir.glob("*.lua")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name_m = _RE_NPC_NAME.search(text)
        npc_name = name_m.group(1) if name_m else path.stem
        found = False
        for m in _RE_SHOP_ROW.finditer(text):
            item_id = int(m.group("id"))
            entry = shops.setdefault(item_id, {"sell": [], "buy": []})
            if m.group("sell"):
                entry["sell"].append((int(m.group("sell")), npc_name))
                found = True
            if m.group("buy"):
                entry["buy"].append((int(m.group("buy")), npc_name))
                found = True
        files += int(found)
    log.info("economy: read shop tables from %d NPC scripts (%d priced item ids)",
             files, len(shops))
    return shops


def load_economy(items_xml: Path = _ITEMS_XML, npc_dir: Path = _NPC_DIR,
                 item_flags=None, catalog: dict[int, ItemInfo] | None = None
                 ) -> dict[int, ItemEconomy]:
    """Build {item_id: ItemEconomy} from items.xml + the NPC shop tables.

    `item_flags` (an ItemFlags) supplies `stackable`, which lives in appearances.dat
    rather than items.xml. It's optional so this can be unit-tested without the assets;
    without it everything is treated as non-stackable (the pessimistic assumption — it
    only ever over-estimates slot cost).
    """
    catalog = catalog if catalog is not None else load_item_catalog(items_xml)
    shops = parse_npc_shops(npc_dir)

    econ: dict[int, ItemEconomy] = {}
    for item_id, info in catalog.items():
        prices = shops.get(item_id) or {"sell": [], "buy": []}
        best_sell, sold_to = _best(prices["sell"], want_max=True)
        best_buy, bought_from = _best(prices["buy"], want_max=False)
        # Currency is worth its face value everywhere — no vendor trip needed to
        # realise it, so it beats any shop price the tables might list.
        if item_id in CURRENCY_VALUE:
            best_sell = max(best_sell, CURRENCY_VALUE[item_id])
            sold_to = ("(currency)",)
        econ[item_id] = ItemEconomy(
            item_id=item_id,
            name=info.name or "",
            weight=info.weight or 0,
            stackable=bool(item_flags.is_stackable(item_id)) if item_flags is not None else False,
            container_size=info.container_size,
            best_sell=best_sell, sold_to=sold_to,
            best_buy=best_buy, bought_from=bought_from,
        )
    sellable = sum(1 for e in econ.values() if e.best_sell > 0)
    log.info("economy: priced %d items (%d have a buyer)", len(econ), sellable)
    return econ


def _best(entries: list[tuple[int, str]], want_max: bool) -> tuple[int, tuple[str, ...]]:
    """Best price + every NPC offering it (so the merchant can pick the closest one)."""
    if not entries:
        return 0, ()
    price = max(e[0] for e in entries) if want_max else min(e[0] for e in entries)
    return price, tuple(sorted({n for p, n in entries if p == price}))
