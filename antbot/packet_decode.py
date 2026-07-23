"""View-time packet decoder for the inspector (learning tool).

Turns a raw frame (the plaintext bytes the client sends/receives, AFTER XTEA) into a
named, hierarchical tree of fields with byte offsets — the data behind the hex+tree view
(A) and the icicle byte-map (B). It runs ONLY when a human opens a packet, never on the
bot's hot path, so it can be as thorough as it likes.

Node shape (uniform, so one viewer renders both A and B):

    {"name": str, "off": int, "size": int,
     "type": str | None, "value": ... | None,
     "children": [node, ...] | None}

`off`/`size` index into the frame's bytes (width for the icicle; range to highlight in
the hex). Leaves carry `type`+`value`; groups carry `children`.

Scope + honesty: every opcode is length-bounded EXCEPT the map-data ones (0x64 full map,
0x65-0x68 edge slices), which use Tibia's skip-RLE tile encoding with no length prefix —
you must fully parse the tiles to find where they end. Rather than half-parse and lose
the boundary, we decode up to the map-data opcode and stop with a clear "map data (bulk)"
node. In a batched frame the common case is move + slice: the move decodes fully, the
slice shows as bulk. Full tile expansion is a later enrichment.
"""
from __future__ import annotations

from .wire import MessageReader

# ---------------------------------------------------------------------------
# Small tracing helper: every read records its byte range, so the viewer can
# cross-highlight bytes <-> fields.
# ---------------------------------------------------------------------------

_DIR = {0: "north", 1: "east", 2: "south", 3: "west",
        4: "northeast", 5: "southeast", 6: "southwest", 7: "northwest"}


class _Reader:
    def __init__(self, data: bytes, flags=None) -> None:
        self.r = MessageReader(data)
        # The item catalog, when the caller has one. Decoders need it to know whether an
        # item id is followed by a count/subtype byte — see _count_bytes.
        self.flags = flags

    @property
    def pos(self) -> int:
        return self.r._pos

    def _leaf(self, name, kind, value, off, extra=None):
        node = {"name": name, "off": off, "size": self.pos - off,
                "type": kind, "value": value}
        if extra:
            node["hint"] = extra
        return node

    def u8(self, name, hint=None):
        off = self.pos
        return self._leaf(name, "u8", self.r.u8(), off, hint)

    def hexbyte(self, name):
        off = self.pos
        return self._leaf(name, "u8", f"0x{self.r.u8():02X}", off)

    def u16(self, name, hint=None):
        off = self.pos
        return self._leaf(name, "u16", self.r.u16(), off, hint)

    def u32(self, name, hint=None):
        off = self.pos
        return self._leaf(name, "u32", self.r.u32(), off, hint)

    def string(self, name):
        off = self.pos
        return self._leaf(name, "string", self.r.string(), off)

    def group(self, name, children):
        off = children[0]["off"] if children else self.pos
        size = sum(c["size"] for c in children)
        return {"name": name, "off": off, "size": size,
                "type": None, "value": None, "children": children}

    def position(self, name):
        return self.group(name, [self.u16("x"), self.u16("y"), self.u8("z")])


# ---------------------------------------------------------------------------
# Per-opcode field decoders. Each takes the reader positioned AFTER the opcode
# byte and returns the opcode's child field nodes. Only length-bounded opcodes
# live here; map-data opcodes are handled specially (they stop the walk).
# ---------------------------------------------------------------------------

def _move_creature(t):
    return [t.position("old"), t.u8("old stackpos"), t.position("new")]

def _remove_thing(t):
    return [t.position("pos"), t.u8("stackpos")]

def _magic_effect(t):
    return [t.position("pos"), t.u16("effect id")]

def _cancel_walk(t):
    d = t.u8("direction"); d["hint"] = _DIR.get(d["value"])
    return [d]

def _text_message(t):
    return [t.u8("message class"), t.string("text")]

def _creature_say(t):
    return [t.u32("statement id"), t.string("speaker"), t.u16("speaker level"),
            t.u8("speak type"), t.position("pos"), t.string("text")]

def _world_light(t):
    return [t.u8("level"), t.u8("colour")]

def _creature_turn(t):
    return [t.position("pos"), t.u8("stackpos"), t.u16("0x63 marker"),
            t.u32("creature id"), t.u8("direction")]

def _creature_health(t):
    return [t.u32("creature id"), t.u8("health %")]

def _creature_speed(t):
    return [t.u32("creature id"), t.u16("speed")]

def _creature_skull(t):
    return [t.u32("creature id"), t.u8("skull")]

def _icons(t):
    return [t.u16("status bitmask")]

def _anim_text(t):
    return [t.position("pos"), t.u8("colour"), t.string("value")]

def _player_stats(t):
    return [t.u16("health"), t.u16("max health"), t.u32("free capacity"),
            t.u32("experience"), t.u16("level"), t.u8("level %"),
            t.u16("mana"), t.u16("max mana"), t.u8("magic level"),
            t.u8("magic level %"), t.u8("soul"), t.u16("stamina min")]

# --- Things: the shared shapes 0x6A and the container/inventory opcodes reuse ---------
# These mirror world.py's parser (_parse_thing / _parse_creature / _parse_outfit /
# _read_item) rather than re-deriving the layouts, so the inspector can't drift away from
# what the bot actually does — if one is wrong, both are wrong in the same way, which is
# far easier to spot than two subtly different readings of the same bytes.

_SKILLS = ("fist", "club", "sword", "axe", "distance", "shielding", "fishing")


def _count_bytes(t, item_id):
    """The conditional count/subtype byte(s) a stackable/liquid item carries.

    Needs the item catalog to know whether they're present. Without it we can't tell, so
    we read nothing and say so — the same honesty rule the map-data 'bulk' node follows.
    """
    if t.flags is None:
        return []
    extra = t.flags.extra_bytes(item_id)
    if not extra:
        return []
    out = [t.u8("count/subtype")]
    out += [t.u8(f"extra[{i}]") for i in range(extra - 1)]
    return out


def _item_fields(t, label="item"):
    node = t.u16(f"{label} id")
    return [node] + _count_bytes(t, node["value"])


def _outfit_fields(t):
    look = t.u16("look type")
    kids = [look]
    if look["value"] != 0:
        kids += [t.u8("head"), t.u8("body"), t.u8("legs"), t.u8("feet"), t.u8("addons")]
    else:
        kids += [t.u16("look type ex")]   # an item worn as an outfit
    return [t.group("outfit", kids)]


def _creature_fields(t, marker):
    """A creature body whose 0x61/0x62/0x63 marker has already been read."""
    known = (marker == 0x0062)
    if marker == 0x0063:                       # direction-only update
        return [t.u32("creature id"), t.u8("direction")]
    kids = []
    if not known:
        kids.append(t.u32("remove known id"))  # server evicting one from its known set
    kids.append(t.u32("creature id"))
    if not known:
        kids.append(t.string("name"))
    kids += [t.u8("health %"), t.u8("direction")]
    kids += _outfit_fields(t)
    kids += [t.u8("light level"), t.u8("light colour"), t.u16("step speed"),
             t.u8("skull"), t.u8("party shield")]
    if not known:
        kids.append(t.u8("guild emblem"))      # only in the full-data form
    kids.append(t.u8("walk-through"))
    return [t.group("creature", kids)]


def _add_thing(t):
    """0x6A: something appeared on a tile — a creature OR an item, told apart by the u16
    that follows the stackpos (0x61/0x62/0x63 = creature markers; anything else = item id).
    """
    fields = [t.position("pos"), t.u8("stackpos")]
    thing = t.u16("thing")
    value = thing["value"]
    if value in (0x0061, 0x0062, 0x0063):
        thing["hint"] = {0x0061: "creature (full data)", 0x0062: "creature (known)",
                         0x0063: "creature (turn)"}[value]
        fields.append(thing)
        fields += _creature_fields(t, value)
    else:
        thing["name"] = "item id"
        thing["hint"] = "item"
        fields.append(thing)
        fields += _count_bytes(t, value)
    return fields


def _container_open(t):
    return ([t.u8("container id")] + _item_fields(t, "container")
            + [t.string("name"), t.u8("slot capacity"), t.u8("has parent"),
               t.u8("item count")])


def _container_close(t):
    return [t.u8("container id")]


def _container_add(t):
    return [t.u8("container id"), t.u16("slot")] + _item_fields(t)


def _container_update(t):
    return [t.u8("container id"), t.u16("slot")] + _item_fields(t)


def _container_remove(t):
    # A trailing u16 (0 = nothing) names the item scrolling into the freed slot.
    fields = [t.u8("container id"), t.u16("slot")]
    tail = t.u16("scroll-in item id")
    fields.append(tail)
    if tail["value"] != 0:
        fields += _count_bytes(t, tail["value"])
    return fields


def _inventory_set(t):
    return [t.u8("slot")] + _item_fields(t)


def _inventory_clear(t):
    return [t.u8("slot")]


def _party_shield(t):
    return [t.u32("creature id"), t.u8("shield colour")]


def _walkthrough(t):
    return [t.u32("creature id"), t.u8("walk-through")]


def _player_skills(t):
    # 7 skills, each (level u8, percent-to-next u8) — measured off the wire, see world.py.
    return [t.group(name, [t.u8("level"), t.u8("percent")]) for name in _SKILLS]


def _cancel_target(t):
    return [t.u32("creature id (0 = cleared)")]


def _self_appear(t):
    # Leads the login frame: who we are, plus login metadata.
    return [t.u32("player id"), t.u16("beat duration"), t.u8("can report bugs")]

# server->client opcode table: id -> (name, field-decoder or None)
_RX = {
    0x6D: ("move-creature", _move_creature),
    0x6C: ("remove-thing", _remove_thing),
    0x83: ("magic-effect", _magic_effect),
    0xB5: ("cancel-walk", _cancel_walk),
    0xB4: ("text-message", _text_message),
    0xAA: ("creature-say", _creature_say),
    0x82: ("world-light", _world_light),
    0x6B: ("creature-turn", _creature_turn),
    0x8C: ("creature-health", _creature_health),
    0x8F: ("creature-speed", _creature_speed),
    0x90: ("creature-skull", _creature_skull),
    0xA2: ("status-icons", _icons),
    0x84: ("animated-text", _anim_text),
    0xA0: ("player-stats", _player_stats),
    0x1E: ("ping", None),
    0x1D: ("pong", None),
    # Everything below the parser already handled while the inspector still showed
    # "undecoded" — a viewer gap, not a bot bug, but it made the inspector cry wolf.
    0x0A: ("self-appear", _self_appear),
    0x6A: ("add-thing", _add_thing),
    0x6E: ("container-open", _container_open),
    0x6F: ("container-close", _container_close),
    0x70: ("container-add", _container_add),
    0x71: ("container-update", _container_update),
    0x72: ("container-remove", _container_remove),
    0x78: ("inventory-set", _inventory_set),
    0x79: ("inventory-clear", _inventory_clear),
    0x86: ("party-shield", _party_shield),
    0x92: ("creature-walkthrough", _walkthrough),
    0xA1: ("player-skills", _player_skills),
    0xA3: ("cancel-target", _cancel_target),
}
# Map-data opcodes: length only knowable by fully parsing skip-RLE tiles. We stop here.
_RX_MAPDATA = {0x64: "map-description", 0x65: "slice-north", 0x66: "slice-east",
               0x67: "slice-south", 0x68: "slice-west"}


# Auto-walk (0x64) uses a DIFFERENT direction encoding than cardinal steps/cancel-walk:
# here 1=E, 2=NE, 3=N, 4=NW, 5=W, 6=SW, 7=S, 8=SE (matches Canary's parseAutoWalk).
_AUTOWALK_DIR = {1: "east", 2: "northeast", 3: "north", 4: "northwest",
                 5: "west", 6: "southwest", 7: "south", 8: "southeast"}

def _autowalk(t):
    n = t.u8("step count")
    steps = []
    for i in range(n["value"]):
        d = t.u8(f"dir[{i}]")
        d["hint"] = _AUTOWALK_DIR.get(d["value"])
        steps.append(d)
    return [n, t.group("directions", steps)] if steps else [n]

def _throw(t):  # move item (0x78)
    return [t.position("from"), t.u16("item id"), t.u8("from stackpos"),
            t.position("to"), t.u8("count")]

def _use(t):    # 0x82
    return [t.position("pos"), t.u16("item id"), t.u8("stackpos"), t.u8("index")]

def _use_with(t):  # 0x83
    return [t.position("from"), t.u16("from id"), t.u8("from stackpos"),
            t.position("to"), t.u16("to id"), t.u8("to stackpos")]

def _attack(t):
    return [t.u32("creature id")]

def _say(t):
    return [t.u8("talk type"), t.string("text")]

# client->server opcode table
_TX = {
    0x65: ("walk-north", None), 0x66: ("walk-east", None),
    0x67: ("walk-south", None), 0x68: ("walk-west", None),
    0x64: ("auto-walk", _autowalk), 0x69: ("stop-autowalk", None),
    0x78: ("move-item", _throw), 0x82: ("use-item", _use),
    0x83: ("use-with", _use_with), 0xA1: ("attack", _attack),
    0x96: ("say", _say), 0x1E: ("ping", None), 0x14: ("logout", None),
}


def summarize(payload: bytes, direction: str) -> str:
    """The lead opcode as '0xNN name', for the packet-list rows — no full decode."""
    if not payload:
        return "(empty)"
    code = payload[0]
    if direction != "tx" and code in _RX_MAPDATA:
        return f"0x{code:02X} {_RX_MAPDATA[code]}"
    name = (_TX if direction == "tx" else _RX).get(code, (None,))[0]
    return f"0x{code:02X} {name or 'undecoded'}"


def decode_frame(payload: bytes, direction: str, item_flags=None) -> dict:
    """Decode one plaintext frame into the inspector tree. `direction` is 'rx' or 'tx'."""
    table = _TX if direction == "tx" else _RX
    mapdata = {} if direction == "tx" else _RX_MAPDATA
    t = _Reader(payload, flags=item_flags)
    children = []
    note = None
    try:
        while not t.r.eof():
            op_off = t.pos
            op = t.hexbyte("opcode")
            code = int(op["value"], 16)
            if code in mapdata:
                # Unbounded tile-RLE from here — decode the opcode, mark the rest bulk.
                rest = len(payload) - t.pos
                op["hint"] = mapdata[code]
                children.append({
                    "name": f"{op['value']} {mapdata[code]}", "off": op_off,
                    "size": len(payload) - op_off, "type": None, "value": None,
                    "children": [op, {"name": "map data (bulk — not expanded yet)",
                                      "off": t.pos, "size": rest, "type": "bytes",
                                      "value": f"{rest} bytes"}]})
                break
            name, dec = table.get(code, (None, None))
            fields = [op]
            if name is None:
                # Opcode we don't decode: honest stop, like the parser's bail.
                rest = len(payload) - t.pos
                op["hint"] = "unrecognised"
                children.append({
                    "name": f"{op['value']} — undecoded", "off": op_off,
                    "size": len(payload) - op_off, "type": None, "value": None,
                    "children": [op] + ([{"name": "undecoded tail", "off": t.pos,
                                          "size": rest, "type": "bytes",
                                          "value": f"{rest} bytes"}] if rest else [])})
                note = f"stopped at unrecognised opcode {op['value']}"
                break
            op["hint"] = name
            if dec is not None:
                fields.extend(dec(t))
            children.append({"name": f"{op['value']} {name}", "off": op_off,
                             "size": t.pos - op_off, "type": None, "value": None,
                             "children": fields})
    except Exception as err:               # never let a decode bug crash the viewer
        note = f"decode error: {type(err).__name__}: {err}"

    return {"dir": direction, "size": len(payload),
            "hex": payload.hex(), "children": children, "note": note}
