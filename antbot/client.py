"""Headless Tibia 8.60 client for the local Canary server.

Implements the two connections a real 8.60 client makes:

1. Account login (port 7171): send credentials, receive the character list.
2. Game world (port 7175): server sends a challenge (0x1F + timestamp + random),
   the client answers with an RSA-sealed login that echoes the challenge, then
   all traffic is XTEA-encrypted.

Field order mirrors Canary's parsers exactly:
- ProtocolLogin::onRecvFirstMessage (protocollogin.cpp)
- ProtocolGame::onRecvFirstMessage + sendLoginChallenge (protocolgame.cpp)
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import struct
import threading
import time
from collections import deque

from . import wire
from .items import ItemFlags
from .wire import MessageReader, MessageWriter
from .carry import plan_pickup, value_of
from .tracing import log_call
from .world import GameState, parse_login_snapshot, process_world_frame, tile_ids

log = logging.getLogger("antbot")

PROTOCOL_VERSION = 860
CLIENT_OS = 2  # windows

# Asset signatures of the Canary-extended 8.6 package ("C86D"/"C86S"/"C86P").
# Canary resolves these to the Cipsoft860CanaryExtended profile, which does not
# require the item mapper before world entry.
DAT_SIGNATURE = 0x44363843
SPR_SIGNATURE = 0x53363843
PIC_SIGNATURE = 0x50363843

DIRECTION_OPCODES = {
    "north": 0x65,
    "east": 0x66,
    "south": 0x67,
    "west": 0x68,
}
# Coordinate change per step (north = y-1, south = y+1, east = x+1, west = x-1),
# used to figure out which tile a rejected step was aiming at.
DIRECTION_DELTAS = {
    "north": (0, -1),
    "east": (1, 0),
    "south": (0, 1),
    "west": (-1, 0),
}
LOGOUT_OPCODE = 0x14
PING_OPCODE = 0x1E
# Auto-walk (client 0x64): send a whole path in ONE packet instead of one packet per
# step — the server queues it and walks it out. This is how the real client's
# click-to-walk works, and it slashes our packet rate (the thing that trips Canary's
# ~25 pkt/s flood cutoff and freezes bots). Direction BYTES use a different numbering
# than the single-step opcodes: E=1, NE=2, N=3, NW=4, W=5, SW=6, S=7, SE=8. We only
# emit cardinals (the pathfinder is 4-directional). Single-step `walk()` stays for the
# times we want exactly one move (e.g. combat repositioning).
AUTOWALK_OPCODE = 0x64
STOP_AUTOWALK_OPCODE = 0x69
AUTOWALK_DIRS = {"north": 3, "east": 1, "south": 7, "west": 5}

# Minimum wall-clock gap between outbound packets. Canary disconnects a connection
# that averages more than 25 packets/second (per connection, over a ~2s window), and
# a bot that spins on rejects/re-plans can spike over that. 80ms caps us at ~12/s —
# well under the limit and far more than a walking bot needs (it steps a few times a
# second). In normal play this never waits; it only smooths bursts. See _send.
_MIN_SEND_INTERVAL = 0.08


# How many server status lines a session keeps (see GameSession.messages). Enough to
# cover the handful of frames around one action; we only ever read the recent tail.
_MESSAGE_TAIL = 20
# How many recent frames the packet inspector keeps per captured bot (each ~a few
# hundred bytes). Bounded so live capture can't grow without limit; old frames drop.
_FRAME_LOG_MAX = 400


def inventory_pos(slot: int) -> tuple[int, int, int]:
    """The special 'position' the protocol uses for a worn inventory slot (1-10)."""
    return (0xFFFF, slot, 0)


def container_pos(container_id: int, slot_index: int) -> tuple[int, int, int]:
    """The special 'position' for an item inside an open container."""
    return (0xFFFF, 0x40 | container_id, slot_index)


class LoginError(Exception):
    pass


@dataclasses.dataclass
class Character:
    name: str
    world: str
    ip: str
    port: int


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one [u16 length][u32 adler][body] frame; return body."""
    header = await reader.readexactly(2)
    (outer,) = struct.unpack("<H", header)
    data = await reader.readexactly(outer)
    checksum, body = struct.unpack("<I", data[:4])[0], data[4:]
    computed = wire.adler32(body)
    if checksum != computed:
        raise LoginError(f"checksum mismatch: got {checksum:#x}, computed {computed:#x}")
    return body


async def fetch_character_list(host: str, port: int, account: str, password: str,
                               rsa_n: int = wire.OTSERV_RSA_N) -> list[Character]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        xtea_key = wire.random_xtea_key()

        rsa_content = (
            MessageWriter()
            .u32(xtea_key[0]).u32(xtea_key[1]).u32(xtea_key[2]).u32(xtea_key[3])
            .string(account)
            .string(password)
            .bytes()
        )
        body = (
            MessageWriter()
            .u8(0x01)  # login protocol identifier
            .u16(CLIENT_OS)
            .u16(PROTOCOL_VERSION)
            .u32(DAT_SIGNATURE).u32(SPR_SIGNATURE).u32(PIC_SIGNATURE)
            .raw(wire.rsa_encrypt(wire.build_rsa_block(rsa_content), rsa_n))
            .bytes()
        )
        writer.write(wire.frame_plain(body))
        await writer.drain()

        blob = await _read_frame(reader)
        payload = wire.unframe_xtea(blob, xtea_key)
        return _parse_login_response(MessageReader(payload))
    finally:
        writer.close()


def _parse_login_response(msg: MessageReader) -> list[Character]:
    characters: list[Character] = []
    while not msg.eof():
        opcode = msg.u8()
        if opcode in (0x0A, 0x0B):  # error
            raise LoginError(msg.string())
        if opcode == 0x14:  # motd
            log.info("MOTD: %s", msg.string().replace("\n", " | "))
        elif opcode == 0x28:  # session key (not expected on legacy layout)
            msg.string()
        elif opcode == 0x64:  # legacy character list
            count = msg.u8()
            for _ in range(count):
                name = msg.string()
                world = msg.string()
                ip_raw = msg.u32()
                port = msg.u16()
                ip = ".".join(str((ip_raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))
                characters.append(Character(name, world, ip, port))
            premium_days = msg.u16()
            log.debug("premium days: %d", premium_days)
        else:
            raise LoginError(f"unexpected login opcode {opcode:#x}")
    return characters


class GameSession:
    """One live connection to the game world."""

    def __init__(self, host: str, port: int, account: str, password: str,
                 character: str, item_flags: ItemFlags, rsa_n: int = wire.OTSERV_RSA_N,
                 dump_frames: int = 0) -> None:
        self.host = host
        self.port = port
        self.account = account
        self.password = password
        self.character = character
        self.item_flags = item_flags
        self.rsa_n = rsa_n
        self.xtea_key = wire.random_xtea_key()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.frames_received = 0
        self.bytes_received = 0
        # Movement coordination: the receive loop sets `move_event` whenever our
        # own position changes or the server cancels a walk, so the goto executor
        # can send a step and then await the outcome. `last_rejected` records
        # which of the two it was.
        self.move_event = asyncio.Event()
        self.last_rejected = False
        # Recent (class, text) status lines from the server, newest last. The server
        # narrates every refusal here and nowhere else, so when a move quietly does
        # nothing this is the only place that says why.
        self.messages: list[tuple[int, str]] = []
        # A live trace of the behaviour's recent DECISIONS (phase + why), so the dashboard
        # can show the bot's "brain", not just its body. This is what makes a silly loop
        # visible: you watch the same few decisions repeat. Bounded ring.
        self.decisions: deque[tuple[float, str, str]] = deque(maxlen=30)
        # opcode -> how many frames we bailed on it. Bails mean "an opcode we don't parse
        # yet"; each is a candidate for a consume handler (see world.py). Recorded once
        # per opcode into the session (recorder.event) and summarised on close.
        self._bail_counts: dict[int, int] = {}
        # Packet inspector (learning tool). OFF by default and per-bot: when scaled up,
        # every bot runs with this false and pays only one bool check per frame. Enabled
        # from the dashboard on the bot you're studying. We store the RAW plaintext bytes
        # (already in hand — the decrypt output for rx, the pre-frame payload for tx) in a
        # bounded ring; NO decoding happens here (that's view-time only, off the hot path).
        # Credentials never enter it: rx capture is post-login, tx capture is gated on
        # `connected` so the password-bearing login packet is skipped.
        self.capture_frames = False
        # (seq, wall_time, "rx"|"tx", payload). The seq is a stable id even as the ring
        # rotates, so the dashboard can reference a frame that's still in the buffer.
        self.frame_log: deque[tuple[int, float, str, bytes]] = deque(maxlen=_FRAME_LOG_MAX)
        self._frame_seq = 0
        # Guards frame_log for cross-thread reads: the bot appends on the asyncio loop,
        # the dashboard reads from its HTTP thread. Taken ONLY while capturing (per-bot,
        # opt-in), so uncaptured bots — every bot at scale — never touch it.
        self._frame_lock = threading.Lock()
        # What this bot is currently TRYING to do, for the dashboard's intent overlay.
        # `goal` is where it wants to be; `plan` is the route it worked out to get there.
        # An empty `plan` with a `goal` set is the interesting state: the bot wants to go
        # somewhere it cannot find a path to, which looks from the outside exactly like a
        # frozen bot. Seeing that on the map is the whole point — it turns "the bot is
        # stuck" into "the bot is trying to reach THAT tile and can't".
        self.goal: tuple[int, int, int] | None = None
        self.plan: list[tuple[int, int, int]] = []
        # The monster we're currently auto-attacking (server-side target set via 0xA1), or
        # None. Tracked so `_engage_adjacent` only sends a packet when the target CHANGES
        # instead of every round — see that function.
        self.attack_target: int | None = None
        # Which planner produced the current `plan` (or last failed to): "shared" =
        # find_shared_route over the colony map (travel/_follow_shared_route), "local" = the myopic
        # find_nearest_step_toward over this bot's own view (_walk_local). Surfaced on the dashboard
        # so a stuck bot tells us WHICH pathfinder is at fault, not just that it's stuck.
        self.plan_source: str = ""
        # Pickup tiles THIS bot has failed to walk to -> when to forgive them
        # (monotonic). Stops us re-claiming a pile we just proved we can't reach; the
        # task itself stays open for a bot better placed to try. Expires because the
        # blocker is often temporary — an unexplored gap the scouts fill in later.
        self.unreachable: dict[tuple[int, int, int], float] = {}
        # Parsed model of the world (A1: our own id + position). Grows in later
        # phases. See world.py.
        self.state = GameState()
        # Diagnostic "capture oracle": dump the raw decrypted bytes of the first
        # `dump_frames` world frames as a hex/ASCII table. 0 disables it. This is
        # how we line real bytes up against Canary's send-side source when a
        # parser misbehaves.
        self.dump_frames = dump_frames
        # Optional link to the ant-farm coordinator (Phase C). When set, the
        # receive loop reports our position and contributes parsed tiles to the
        # shared explored map so the dashboard can draw us live.
        self.colony = None            # set by the farm/explore runner
        self.bot_name = character     # label used in the colony
        self.role = "scout"           # "scout" | "explore" | "hauler"; set by the session
                                      # runner, reported so the dashboard can group bots
        # Whether this bot is currently SUPPOSED to be moving. The stuck/frozen watchdog
        # reads "not moving" as "wedged", which is right for a scout but wrong for a bot
        # that's idle on purpose (a hauler waiting for work). Behaviours set this False
        # while they intend to sit still. See _check_watchdog.
        self.expect_movement = True
        self.status = "idle"          # short status string shown on the dashboard
        # Optional session recorder (see recorder.py). When set, capture points push
        # snapshots/actions/decisions/events for later replay. None = not recording.
        self.recorder = None
        # Optional path recorder for the efficiency tests (see navcost / navtests). When set
        # to a list, the receive loop appends our Position after every CONFIRMED own-move, so
        # the harness gets the exact tile-by-tile route the bot walked — including any
        # backtracking — to price against the time-optimal floor. None (the default) for
        # every normal bot, so this costs nothing at scale: it's operator/test tooling only.
        self.path_log: list | None = None
        # Container ids that are BROWSE FIELDS (a ground tile the server wrapped in a
        # container for us, see browse_field) rather than a bag we carry. They arrive as
        # ordinary containers, so without this a hauler could mistake a tile for its
        # backpack and cheerfully "haul" loot into the ground it's standing on.
        self.browse_cids: set[int] = set()
        self._last_loot_scan = 0.0    # throttle for _scan_loot (see there)
        # Set by the ColonyManager to ask this bot to wrap up and log out (e.g.
        # when the user clicks "Stop" on the dashboard). Both the explore loop and
        # the step loop poll it, so a stop takes effect within a step or two.
        self.stop_event = asyncio.Event()
        # Monotonic timestamps for the watchdog: when we last actually moved, and
        # when we last received any frame. A behaviour loop that sees no movement
        # for too long treats the bot as stuck and takes corrective action.
        self.last_move_time = time.monotonic()
        self.last_frame_time = time.monotonic()
        # When we last reported to the colony. The per-move report keeps a WALKING bot
        # fresh; this lets the frame loop add a slow heartbeat so a STUCK bot keeps
        # reporting too (see _REPORT_HEARTBEAT).
        self.last_report_time = 0.0
        self.last_block_clear = time.monotonic()  # when the stuck watchdog last cleared blocks
        # Outbound-packet rate limiter (see _MIN_SEND_INTERVAL). The lock serialises
        # sends across the behaviour loop and the ping loop so the pacing is honoured
        # and framed packets never interleave on the wire.
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0

    # -- packet inspector (see _FRAME_LOG_MAX / packet_decode) --------------
    def _capture(self, payload: bytes, direction: str) -> None:
        """Record one plaintext frame. Called only while capturing; keeps a reference to
        bytes that already exist (no copy/decode) under the cross-thread lock."""
        with self._frame_lock:
            self._frame_seq += 1
            self.frame_log.append((self._frame_seq, time.time(), direction, payload))

    def set_capture(self, on: bool) -> None:
        """Turn the inspector on/off for this bot (from the dashboard). Off clears the
        buffer so a later capture starts clean."""
        self.capture_frames = on
        if not on:
            with self._frame_lock:
                self.frame_log.clear()

    def frames_raw(self) -> list[tuple[int, float, str, bytes]]:
        """A snapshot of the buffered (seq, t, dir, payload) tuples, taken under the lock
        so a concurrent append can't corrupt the cross-thread read."""
        with self._frame_lock:
            return list(self.frame_log)

    def frame_by_seq(self, seq: int) -> tuple[str, bytes] | None:
        """(dir, payload) for one buffered frame by its stable seq, or None if aged out."""
        with self._frame_lock:
            for s, _t, d, p in self.frame_log:
                if s == seq:
                    return (d, p)
        return None

    def _report_to_colony(self) -> None:
        """Push our position + vitals + newly-explored tiles up, and pull shared
        hazards down."""
        if self.colony is None:
            return
        # Make this live session reachable by name so the dashboard can toggle packet
        # capture and read frames. Idempotent; the latest session per name wins (relogs).
        self.colony.register_session(self.bot_name, self)
        s = self.state
        # Runtime blocks with remaining TTL, for the dashboard's block-inspector overlay.
        # These are tiles the SERVER refused us at runtime — the layer that accumulates
        # and boxes a scout in. Colony hazards get OR'd into blocked_tiles below, but the
        # colony already knows those separately and subtracts them, so what we send here
        # stays "my own runtime refusals". remaining<0 marks a block with no expiry.
        now = time.monotonic()
        blocked = [(x, y, z, round(s.blocked_expiry.get((x, y, z), 0) - now, 1))
                   for (x, y, z) in s.blocked_tiles]
        # Creatures we can currently see, for the map's creature layer AND the block
        # inspector's "occupied" category (find_nearest_step_toward avoids these; find_frontier
        # doesn't). state.creatures is already pruned to the viewport, so these are live,
        # not stale memory. id + kind let the dashboard colour monster/npc/player apart
        # and dedupe when two bots see the same creature.
        creatures = [{"id": c.creature_id, "x": c.position.x, "y": c.position.y,
                      "z": c.position.z, "kind": c.kind, "name": c.name,
                      "hp": c.health_percent}
                     for c in s.nearby_creatures() if c.position is not None]
        self.colony.update_bot(self.bot_name, self.character, s.position, self.status,
                               hp=s.hp, max_hp=s.max_hp, mana=s.mana,
                               max_mana=s.max_mana, level=s.level, role=self.role,
                               goal=self.goal, plan=self.plan,
                               plan_source=self.plan_source,
                               blocked=blocked, creatures=creatures,
                               decisions=list(self.decisions),
                               # Creature-tracking health: moves we couldn't attribute to
                               # a known creature (each = a monster frozen at a stale tile)
                               # and stale records we cleared. See _relocate_creature.
                               tracking={"move_misses": s.creature_move_misses,
                                         "ghosts": s.creature_ghosts_dropped})
        self.colony.contribute_tiles(self.state)
        # Timestamp every report so the frame loop's heartbeat (see _run_read_loop) knows
        # when we last told the dashboard anything. A moving bot reports on every step;
        # this is what lets a STUCK bot keep reporting too, so monsters that walk up to a
        # boxed-in scout still appear on the map instead of the view freezing at its last
        # step.
        self.last_report_time = time.monotonic()
        # Share any loot we can see, so the swarm's eyes feed every hauler.
        _scan_loot(self)
        # Learn the hazards other bots have discovered, so we avoid exits we
        # haven't personally hit yet (collective learning). get_avoid_hazards() (not
        # get_hazards()) excludes a USE-type link source (a grate) -- merging it in raw
        # here permanently poisoned blocked_tiles for the rest of the session the moment
        # ANY bot learned that link, making its own tile unreachable as a destination
        # even for ordinary walking. This was a real, confirmed bug (see colony.py).
        self.state.blocked_tiles |= self.colony.get_avoid_hazards()
        # Feed the session recorder the same beat (position/vitals + creatures around us).
        if self.recorder is not None:
            self.recorder.snapshot(self.state, self.status)

    def log_event(self, level: str, category: str, message: str) -> None:
        """Convenience: record a dashboard event for this bot at its position."""
        if self.colony is not None:
            self.colony.log_event(self.bot_name, level, category, message, self.state.position)
        if self.recorder is not None:
            self.recorder.event(level, category, message)

    def decide(self, phase: str, why: str = "") -> None:
        """Record a behaviour decision — sets the visible `status` AND appends to the live
        decision trace (see `self.decisions`). `phase` is the short label the roster shows
        ('scouting', 'breaking out', 'descending', 'loop-break', …); `why` is the reason
        that makes a loop legible ('-> frontier (x,y,z)', '3 monsters adjacent', …).
        """
        self.status = f"{phase}: {why}" if why else phase
        self.decisions.append((time.time(), phase, why))
        if self.recorder is not None:
            self.recorder.decision(phase, why=why)

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

        # Server speaks first: 0x1F challenge (plaintext, with inner length).
        body = await _read_frame(self.reader)
        challenge = MessageReader(body)
        inner = challenge.u16()
        opcode = challenge.u8()
        if opcode != 0x1F:
            raise LoginError(f"expected challenge opcode 0x1F, got {opcode:#x} (inner={inner})")
        timestamp = challenge.u32()
        challenge_random = challenge.u8()
        log.debug("challenge: timestamp=%d random=%d", timestamp, challenge_random)

        rsa_content = (
            MessageWriter()
            .u32(self.xtea_key[0]).u32(self.xtea_key[1])
            .u32(self.xtea_key[2]).u32(self.xtea_key[3])
            .u8(0)  # gamemaster flag
            .string(self.account)
            .string(self.character)
            .string(self.password)
            .u32(timestamp)
            .u8(challenge_random)
            .bytes()
        )
        login = (
            MessageWriter()
            .u8(0x0A)  # game protocol identifier
            .u16(CLIENT_OS)
            .u16(PROTOCOL_VERSION)
            .raw(wire.rsa_encrypt(wire.build_rsa_block(rsa_content), self.rsa_n))
            .bytes()
        )
        self.writer.write(wire.frame_plain(login))
        await self.writer.drain()

    async def send_opcode(self, opcode: int) -> None:
        # Routes through _send so single-opcode packets (walks, pings, logout) are
        # rate-limited too — walks are the bulk of our traffic, so they matter most.
        await self._send(MessageWriter().u8(opcode))

    async def walk(self, direction: str) -> None:
        """Send exactly ONE step (0x65-0x68). For following a path, prefer
        `autowalk()` — this is for the cases we want a single, precise move."""
        if self.recorder is not None:
            self.recorder.action("walk", dir=direction)
        await self.send_opcode(DIRECTION_OPCODES[direction])

    async def autowalk(self, directions: list[str]) -> None:
        """Send a whole path in one packet (0x64): count then direction bytes. The
        server queues and walks them out step by step. Directions go in forward order
        (the server stores them reversed and consumes from the back)."""
        if self.recorder is not None:
            self.recorder.action("autowalk", dirs=list(directions))
        msg = MessageWriter().u8(AUTOWALK_OPCODE).u8(len(directions))
        for d in directions:
            msg.u8(AUTOWALK_DIRS[d])
        await self._send(msg)

    async def stop_autowalk(self) -> None:
        """Cancel a queued auto-walk (0x69) — e.g. a step relocated us mid-path."""
        await self.send_opcode(STOP_AUTOWALK_OPCODE)

    async def logout(self) -> None:
        await self.send_opcode(LOGOUT_OPCODE)

    async def _send(self, msg: MessageWriter) -> None:
        """Frame and send an already-built client message, rate-limited.

        Every outbound packet passes through here, so this is where we enforce the
        send ceiling that keeps us under Canary's ~25 packets/second flood cutoff
        (which otherwise DISCONNECTS us). The lock makes the pace-then-write atomic,
        so two concurrent senders (behaviour loop + ping loop) can't both slip a
        packet through in the same window or interleave frames. Normal play sends a
        few packets a second and never waits here; only bursts get smoothed.
        """
        assert self.writer is not None
        async with self._send_lock:
            wait = _MIN_SEND_INTERVAL - (time.monotonic() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = time.monotonic()
            payload = msg.bytes()
            # Packet inspector: capture the plaintext we send (opcode + fields), gated on
            # `connected` so the login packet — the only one carrying credentials — is
            # never captured. Off by default; one bool check otherwise.
            if self.capture_frames and self.connected.is_set():
                self._capture(payload, "tx")
            self.writer.write(wire.frame_xtea(payload, self.xtea_key))
            await self.writer.drain()

    async def use_item(self, x: int, y: int, z: int, item_id: int,
                       stackpos: int, index: int = 0) -> None:
        """Use an object on a tile (0x82) — a ladder, grate, well, open hole, or
        an unlocked door. `stackpos` is the object's position in the tile's stack."""
        if self.recorder is not None:
            self.recorder.action("use", item=item_id, at=[x, y, z])
        await self._send(
            MessageWriter().u8(0x82).u16(x).u16(y).u8(z).u16(item_id).u8(stackpos).u8(index)
        )

    async def use_item_with(self, from_pos, from_id: int, from_stack: int,
                            to_pos, to_id: int, to_stack: int) -> None:
        """Use one item ON another (0x83) — a shovel on a hole, a key on a door.

        Positions are (x, y, z). Inventory items use the special position
        (0xFFFF, slot, 0); container items (0xFFFF, 0x40|containerId, slotIndex).
        See inventory_pos() below.
        """
        if self.recorder is not None:
            self.recorder.action("use_with", tool=from_id, on=to_id, at=list(to_pos))
        w = MessageWriter().u8(0x83)
        w.u16(from_pos[0]).u16(from_pos[1]).u8(from_pos[2]).u16(from_id).u8(from_stack)
        w.u16(to_pos[0]).u16(to_pos[1]).u8(to_pos[2]).u16(to_id).u8(to_stack)
        await self._send(w)

    async def browse_field(self, x: int, y: int, z: int) -> None:
        """Open a ground TILE as a container (client 0xCB, parseBrowseField).

        This is the way around the LIFO rule on ground piles. A normal tile move always
        acts on the topmost item (Game::internalGetThing with STACKPOS_MOVE returns
        `tile->getTopDownItem()` and the move is refused unless our itemId matches it),
        so a bot can't reach into the middle of a pile. Browse-field asks the server to
        wrap the tile in a Container and send it back as an ordinary container-open
        (0x6E) — and CONTAINER access is by slot index (`slot = pos.z`), not LIFO. So we
        can then lift any item off the tile by slot, in value order.

        Notably `parseBrowseField` carries NO `oldProtocol` guard (unlike quick-loot,
        which returns early for us), so 8.60 may use it. The server requires us to be on
        the same floor and within 1 tile — it auto-walks us there otherwise. The
        container id it assigns is derived from the position, and asking twice TOGGLES it
        shut, so callers should open once and read `state.containers`.
        """
        await self._send(MessageWriter().u8(0xCB).u16(x).u16(y).u8(z))

    async def move_item(self, from_pos, item_id: int, from_stack: int,
                        to_pos, count: int = 1) -> None:
        """Move `count` of `item_id` from `from_pos` to `to_pos` (client 0x78, parseThrow).

        Positions are (x, y, z) world tiles, OR the special inventory/container encodings
        (see inventory_pos / container_pos: worn slot = (0xFFFF, slot, 0); container item
        = (0xFFFF, 0x40|containerId, slotIndex)). `from_stack` is the item's index in its
        source stack (the tile stackpos, or the slot). This is how a hauler picks loot off
        the ground (tile -> bag slot) and later puts it down (bag slot -> tile / depot).
        """
        if self.recorder is not None:
            self.recorder.action("move", item=item_id, frm=list(from_pos),
                                 to=list(to_pos), count=count)
        w = MessageWriter().u8(0x78)
        w.u16(from_pos[0]).u16(from_pos[1]).u8(from_pos[2]).u16(item_id).u8(from_stack)
        w.u16(to_pos[0]).u16(to_pos[1]).u8(to_pos[2]).u8(count)
        await self._send(w)

    async def say(self, text: str, talk_type: int = 1) -> None:
        """Speak (0x96). talk_type 1 = normal say — used for chat and for casting
        spells (the spell words are just said)."""
        await self._send(MessageWriter().u8(0x96).u8(talk_type).string(text))

    async def attack(self, creature_id: int) -> None:
        """Set our auto-attack target (client 0xA1 + u32 id); id 0 stops attacking.

        The server melees the target every turn while we're adjacent — we send this once
        and it keeps swinging (parseAttack -> playerSetAttackedCreature). It's how a
        boxed-in scout kills the monster occupying the tile it needs to step onto."""
        if self.recorder is not None:
            self.recorder.action("attack", target=creature_id)
        await self._send(MessageWriter().u8(0xA1).u32(creature_id))

    async def receive_loop(self) -> None:
        """Consume server frames; raise LoginError if the server rejects us."""
        assert self.reader is not None
        try:
            while True:
                blob = await _read_frame(self.reader)
                payload = wire.unframe_xtea(blob, self.xtea_key)
                self.frames_received += 1
                self.bytes_received += len(payload)
                self.last_frame_time = time.monotonic()  # for the stall watchdog

                # Capture oracle: dump the first few frames verbatim if asked.
                if self.frames_received <= self.dump_frames:
                    log.debug(
                        "raw world frame #%d (%d bytes):\n%s",
                        self.frames_received, len(payload),
                        wire.hexdump(payload, max_bytes=256),
                    )

                if not self.connected.is_set():
                    # The first world frame: detect entry and parse the snapshot.
                    self._inspect(payload)
                else:
                    # Ongoing frames: keep position/map/creatures live.
                    if self.capture_frames:      # packet inspector (off by default)
                        self._capture(payload, "rx")
                    result = process_world_frame(payload, self.state, self.item_flags)
                    if result.messages:
                        # The server only ever explains a refused action in prose, so
                        # keep the recent lines where behaviors can read them. Bounded:
                        # this is a diagnostic tail, not a chat log.
                        self.messages.extend(result.messages)
                        del self.messages[:-_MESSAGE_TAIL]
                    if result.target_cancelled:
                        # The server dropped our attack target (almost always: it died).
                        # Forget it, or `_engage_adjacent` would compare against a target
                        # that no longer exists and skip re-targeting the next monster.
                        self.attack_target = None
                    if result.moved or result.rejected:
                        self.last_rejected = result.rejected
                        self.move_event.set()
                        if result.moved:
                            self.last_move_time = time.monotonic()  # watchdog
                            # We changed tiles: refresh the dashboard's view of us
                            # and hand it whatever new map we revealed this step.
                            self._report_to_colony()
                            # Efficiency tests: record the exact tile we landed on, once
                            # per confirmed move (see navcost). This is the single choke
                            # point every real step passes through, so the captured route
                            # can't miss or double-count a step. Off (None) for normal bots.
                            if self.path_log is not None and self.state.position is not None:
                                self.path_log.append(self.state.position)
                    # Heartbeat: report even when we DIDN'T move, so a boxed-in bot keeps
                    # its dashboard view live. Frames keep arriving while stuck (creatures
                    # move around us, the world light ticks), so this fires reliably; the
                    # per-move report above resets last_report_time, so this never double-
                    # reports for a walking bot. This is the fix for "stuck bot with an
                    # invisible monster": the monster IS tracked, we just weren't reporting.
                    if time.monotonic() - self.last_report_time >= _REPORT_HEARTBEAT:
                        self._report_to_colony()
                    if result.bailed_opcode is not None:
                        log.debug("frame: unhandled opcode %#04x (resyncing next frame)",
                                  result.bailed_opcode)
                        # Persist bails into the session recording so we can review which
                        # opcodes we don't yet handle (each is a candidate for the "parse
                        # everything" fix — an unhandled opcode batched ahead of a
                        # move+slice drops a map row). Count them per opcode so a review
                        # sees frequency, and only record the FIRST of each to keep the
                        # recording small.
                        op = result.bailed_opcode
                        self._bail_counts[op] = self._bail_counts.get(op, 0) + 1
                        if self.recorder is not None and self._bail_counts[op] == 1:
                            self.recorder.event("warning", "bail",
                                                f"unhandled opcode {op:#04x}")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            log.info("server closed the connection")
        finally:
            if self._bail_counts:
                summary = ", ".join(f"{op:#04x}×{n}" for op, n
                                    in sorted(self._bail_counts.items(), key=lambda kv: -kv[1]))
                log.info("session bailed on unhandled opcodes: %s", summary)
                if self.recorder is not None:
                    self.recorder.event("info", "bail", f"bail summary: {summary}")
            self.closed.set()

    def _inspect(self, payload: bytes) -> None:
        """Look at an incoming world frame.

        In A1 we do two things: detect the moment we're accepted into the world,
        and on that first frame parse our position out of the login snapshot.
        Full opcode-by-opcode parsing of every frame is a later phase; for now we
        only peek at the leading opcode to classify the frame.
        """
        if not payload:
            return

        # Peek at the first opcode without disturbing the parser that the
        # snapshot reader will run over the whole payload.
        first_opcode = payload[0]

        if not self.connected.is_set():
            # Before we're confirmed in-game, the server can still reject us.
            if first_opcode == 0x14:  # disconnect with a human-readable reason
                raise LoginError(MessageReader(payload[1:]).string())
            if first_opcode == 0x16:  # "waiting list" (server full / queued)
                reader = MessageReader(payload[1:])
                reason = reader.string()
                retry = reader.u8()
                raise LoginError(f"waiting list: {reason} (retry in {retry}s)")

            # Anything else is real world data: this is the login snapshot frame.
            self.connected.set()
            log.info("entered game world (first opcode %#04x, %d bytes)", first_opcode, len(payload))

            # A2/A3 payoff: parse our id, position, AND the whole map (tiles +
            # creatures), then announce it loudly so it's easy to verify by eye.
            if parse_login_snapshot(payload, self.state, self.item_flags):
                nearby = self.state.nearby_creatures()
                log.info(
                    "PARSED LOGIN SNAPSHOT: player_id=%d position=%s | %d tiles, "
                    "%d creatures nearby", self.state.player_id, self.state.position,
                    len(self.state.tiles), len(nearby),
                )
                for c in nearby:
                    log.info("  creature: %-24s hp=%3d%% at %s",
                             c.name or "(hidden)", c.health_percent, c.position)
                # Seed the colony with our starting position and the login map.
                self._report_to_colony()
            else:
                log.warning("could not parse login snapshot; dumping first bytes:\n%s",
                            wire.hexdump(payload, max_bytes=64))
        else:
            # Subsequent frames: we don't decode them yet, just log lightly so
            # the stream is visible at debug level.
            log.debug("frame: first opcode %#04x, %d bytes", first_opcode, len(payload))

    async def ping_loop(self, interval: float = 5.0) -> None:
        # Canary pings us every 5s and kicks after 60s without a pong, so we answer
        # on the same cadence. A failed SEND means the socket is really dead — mark
        # the session closed so the behaviour loop stops promptly (and the manager
        # can relog it) instead of waiting out the receive-side stall watchdog.
        while not self.closed.is_set():
            await asyncio.sleep(interval)
            try:
                await self.send_opcode(PING_OPCODE)
            except (ConnectionResetError, RuntimeError, OSError):
                self.closed.set()
                return

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()


def _select_character(characters: list[Character], character_name: str | None) -> Character:
    """Pick the character to play: a named one, or the first on the account."""
    for c in characters:
        log.info("character: %-20s world=%s %s:%d", c.name, c.world, c.ip, c.port)
    if character_name:
        matches = [c for c in characters if c.name.lower() == character_name.lower()]
        if not matches:
            raise LoginError(f"character {character_name!r} not found on account")
        return matches[0]
    return characters[0]


async def _connect_session(host: str, login_port: int, account: str, password: str,
                           character_name: str | None, item_flags: ItemFlags,
                           rsa_n: int, dump_frames: int) -> GameSession:
    """Fetch the character list, pick a character, and open the game connection."""
    characters = await fetch_character_list(host, login_port, account, password, rsa_n)
    if not characters:
        raise LoginError("account has no characters")
    selected = _select_character(characters, character_name)
    log.info("logging in as %r on %s:%d", selected.name, selected.ip, selected.port)
    session = GameSession(selected.ip, selected.port, account, password, selected.name,
                          item_flags, rsa_n, dump_frames=dump_frames)
    await session.connect()
    return session


_SLOT_BACKPACK = 3   # the worn container slot


async def _open_backpack(session: GameSession) -> None:
    """Open the worn backpack so we can see (and later use) what's inside.

    The server only sends a container's contents once it's OPEN, and the cipsoft860
    login doesn't auto-open it — so we do, by 'using' the bag in slot 3 shortly after
    the worn inventory arrives. Best-effort and quick.
    """
    for _ in range(8):
        await asyncio.sleep(0.3)
        if session.closed.is_set() or session.stop_event.is_set():
            return
        bag = session.state.inventory.get(_SLOT_BACKPACK)
        if bag:
            try:
                await session.use_item(0xFFFF, _SLOT_BACKPACK, 0, bag[0], 0)
            except (ConnectionResetError, RuntimeError, OSError) as err:
                log.debug("could not open backpack: %s", err)
            return


async def _run_in_world(session: GameSession, action) -> None:
    """Shared lifecycle for a command: enter the world, run `action`, log out.

    `action` is an async callable taking the session; it runs once we're in-world
    (position/map parsed). This holds the receive loop, the keepalive ping, the
    timeout handling and the task cleanup in one place so each command only has to
    supply its own behaviour.
    """
    recv_task = asyncio.create_task(session.receive_loop())
    ping_task = asyncio.create_task(session.ping_loop())
    try:
        # Wait until we're actually in the world (or the receive loop raised a
        # rejection). session.state is populated by this point.
        await asyncio.wait_for(session.connected.wait(), timeout=10)
        await _open_backpack(session)   # reveal bag contents (tools/keys/potions)
        await action(session)
        if not session.closed.is_set():
            log.info("logging out")
            await session.logout()
            await asyncio.sleep(1)
    except TimeoutError:
        if recv_task.done() and recv_task.exception():
            raise recv_task.exception()  # surface the real login error
        raise LoginError("timed out waiting for world data")
    finally:
        ping_task.cancel()
        await session.close()
        if not recv_task.done():
            recv_task.cancel()
        else:
            exc = recv_task.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                raise exc

    log.info("session finished: %d frames, %d bytes of world data received",
             session.frames_received, session.bytes_received)


async def walk_session(host: str, login_port: int, account: str, password: str,
                       character_name: str | None, duration: float, item_flags: ItemFlags,
                       rsa_n: int = wire.OTSERV_RSA_N, dump_frames: int = 0) -> None:
    """Log a character in, report its position, then optionally random-walk.

    A `duration` of 0 means "log in, announce position, log straight back out" —
    the `pos` command. A positive duration additionally walks randomly.
    """
    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, dump_frames)

    async def action(session: GameSession) -> None:
        if duration > 0:
            log.info("walking randomly for %.0f seconds...", duration)
            end = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < end and not session.closed.is_set():
                direction = random.choice(list(DIRECTION_OPCODES))
                log.info("walk %s", direction)
                await session.walk(direction)
                await asyncio.sleep(random.uniform(0.8, 1.6))
        else:
            # Give the snapshot a beat to be parsed/logged before we leave.
            await asyncio.sleep(0.2)

    await _run_in_world(session, action)


async def path_session(host: str, login_port: int, account: str, password: str,
                       character_name: str | None, goal_x: int, goal_y: int,
                       item_flags: ItemFlags, rsa_n: int = wire.OTSERV_RSA_N,
                       dump_frames: int = 0) -> None:
    """B1: log in and A*-plan a route from our position to (goal_x, goal_y).

    Prints the plan; does not walk it yet (that's B2). The point is to prove the
    walkability model and pathfinder against the real parsed map.
    """
    from .nav import find_path_on_floor, render_ascii

    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, dump_frames)

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)  # let the snapshot finish parsing
        start = session.state.position
        path = find_path_on_floor(session.state, item_flags, goal_x, goal_y)
        if path is None:
            log.info("NO PATH from %s to (%d, %d) within the known map",
                     start, goal_x, goal_y)
        elif not path:
            log.info("already standing on (%d, %d)", goal_x, goal_y)
        else:
            log.info("PATH from %s to (%d, %d): %d steps: %s",
                     start, goal_x, goal_y, len(path), " -> ".join(path))
        # Draw the known area with the route overlaid so it can be checked by eye.
        log.info("map around %s (@ = us, * = path, C = creature, # = blocked):\n%s",
                 start, render_ascii(session.state, item_flags, path))

    await _run_in_world(session, action)


# Watchdog thresholds (seconds).
_STALL_SECONDS = 20    # no data from the server at all -> connection is dead
# How often a NON-moving bot re-reports to the colony. A walking bot reports on every
# step (see the frame loop); this bounds the extra reports for a stuck/idle bot to keep
# the dashboard's view of it — position, vitals, and the CREATURES around it — fresh
# without spamming. 1s is far slower than a walking bot already reports, so it adds no
# load at scale; it exists purely so a boxed-in bot doesn't vanish from the live view
# (and take the monster that trapped it with it).
_REPORT_HEARTBEAT = 1.0

_STUCK_SECONDS = 25    # alive but not moving -> probably boxed in by stale blocks
_RELOG_STUCK_SECONDS = 90  # alive but STILL not moving this long -> force a relog
                           # (fresh connection; clears server-side flood throttling)

# How long a locally-rejected tile stays blocked before we'll try it again. Long
# enough to route around a wall during one traversal, short enough that a tile
# blocked by a passing creature/bot frees up on its own (see blocked_expiry).
_BLOCK_TTL = 12.0


def _prune_blocked_tiles(session: GameSession) -> None:
    """Drop locally-rejected blocks whose time-out has passed.

    Transient rejecters (a monster or a clustered bot that stepped into our path)
    would otherwise leave permanent false walls that slowly box us in. Colony
    hazards have no expiry entry, so this never touches them. Call once per loop.
    """
    exp = session.state.blocked_expiry
    if not exp:
        return
    now = time.monotonic()
    for tile in [t for t, when in exp.items() if when <= now]:
        exp.pop(tile, None)
        session.state.blocked_tiles.discard(tile)

# How far the per-step planner floods when re-planning during exploration. We
# re-plan every step and only walk its first move, so a local view is plenty; this
# caps per-step planning cost so many bots can run at once (see find_nearest_step_toward's
# `max_radius`). Comfortably larger than the wanderer's home radius so it can still
# route around walls. goto/travel pass None instead (exact long-distance routing).
_PLAN_RADIUS = 40
# Failed travel attempts at one shared frontier before a scout retires it (leak into a
# sealed room, or a link it can't execute). Low so we stop wasting trips quickly.
_FRONTIER_MAX_TRIES = 3
# How long a scout may reveal NO new map before the loop-breaker sends it home to the
# temple for a clean restart. Long enough not to trip on a slow-but-real approach; short
# enough that a genuine loop doesn't grind for minutes.
_LOOP_BREAK_SECONDS = 60.0

# How near an untried use-object (ladder/hole/grate/door) has to be for a scout to
# detour and try it before falling back to a known step-descent. Small, so we only
# take short detours; each object is tried once (the scout's `tried` set), so this
# discovers use-shortcuts without thrashing.
_USE_OBJECT_RADIUS = 25

# Radius for the per-round "on sight" check: if a use-object is essentially right
# next to the scout while it's exploring, try it once. This is what makes bots
# actually USE ladders/holes/doors during normal play (a step-descent would always
# win otherwise), and it's cheap because find_use_object scans only this small box.
_ONSIGHT_RADIUS = 6

# Radius for hunting a STEP-DOWN object (open hole / trapdoor / ladder-hole-down /
# down-stairs) to deliberately descend a floor. A touch wider than the use-object
# radius because the common case is a scout stranded on a small upper platform after
# climbing a ladder, where the way back down is the hole right next to where it landed.
_DESCENT_RADIUS = 30

# How long `scout` lets ONE `navigate_to` call try before giving control back to its own
# round loop. Scout picks WHERE to go (a frontier, a learned shortcut, home); `navigate_to`
# owns HOW to get there (open a door, hunt a floor change) — see navigate_to's docstring.
# A bound is essential here: unlike the nav tests (which want navigate_to to keep retrying
# until arrival or a real deadline), scout must stay responsive every round to its OWN
# stuck-counting, boxed-in escape, and self-defence — logic navigate_to knows nothing
# about. A few seconds is enough for one honest attempt without starving those checks.
_SCOUT_NAV_BUDGET = 8.0

# Consecutive `navigate_to` rounds with NO net movement before it considers the block
# might be a MONSTER (or a stale crowd) worth fighting/stepping through rather than just
# re-planning around forever. A monster occupies its tile transiently (never durably
# blocked — see _walk_local's "rejected" handling), so a corridor with one rat in it can
# otherwise make navigate_to spin indefinitely: replan, get rejected, replan, forever.
# Set low relative to scout's own 8-round threshold because one navigate_to round is
# already a full _walk_local attempt (up to 60 steps) or a travel() hop — several of those
# producing zero progress is a much stronger "genuinely blocked" signal than one scout tick.
_NAV_FIGHT_THRESHOLD = 3

# How long an attempted-but-unresolved exit object (a door we used, a hole we tried to
# step onto) stays on the "already tried" list before we'll attempt it again. The old
# behaviour retired an object for the WHOLE session on that floor, so a single
# transient failure — a creature blocking the doorway that round, a step that didn't
# register, the launch tile briefly unreachable — permanently abandoned that exit and
# left the scout to random-walk the room forever. A TTL lets a real dead-end stay
# retired long enough not to thrash, while giving a fluke another chance. (Mirrors how
# _BLOCK_TTL forgives transient tile rejections.)
_TRIED_TTL = 45.0

# How close another bot must be to a shortcut's LANDING for a scout to consider that
# landing "taken" and pick a different exit. All scouts share one colony link graph, so
# without this they route to the SAME descent destination and pile onto one tile — the
# root of the gridlock the escape maneuver then has to dig them out of. Dispersing at
# the source (here) means fewer pile-ups to escape in the first place.
_DISPERSE_RADIUS = 4


def _check_watchdog(session: GameSession) -> bool:
    """Detect and recover a bot that has stopped making progress.

    Returns True if the caller should abort the behaviour loop (so the manager can
    relog it). Three failure modes, from the "bots sometimes just stop" work:

    - The TCP link went half-open: we get NO frames for a long time. End the session.
    - Frozen but connected: we ARE receiving frames (the server keeps pinging us) but
      the server stopped honouring our WALKS — typically its flood protection kicked
      in on a bot that spammed steps, so our packets are silently dropped. Pings keep
      the stall watchdog happy, so this would hang forever; after a long no-move
      spell we force a relog, which gives us a fresh connection with a clean flood
      budget. (This is the case the plain relog supervisor can't see, because the
      session never actually ends on its own.)
    - Alive but briefly stuck: usually our avoid-set (`blocked_tiles`) accumulated
      false blocks. Clear the LOCAL cruft (keeping the colony's shared hazards) and
      retry. We do this on a timer WITHOUT resetting last_move_time, so the freeze
      escalation above can still measure the true no-move duration.
    """
    now = time.monotonic()
    since_frame = now - session.last_frame_time
    since_move = now - session.last_move_time

    if since_frame > _STALL_SECONDS:
        session.log_event("error", "stall",
                          f"no data from server for {since_frame:.0f}s; logging out")
        session.closed.set()
        return True

    # A bot can be deliberately still — a hauler waiting for work is idle BY DESIGN, not
    # frozen. Only the connection-level stall above applies to it; the movement checks
    # below assume we're actually trying to walk. We also keep its movement clock fresh,
    # so the freeze check can't fire the instant it picks up work and starts moving again.
    if not session.expect_movement:
        session.last_move_time = now
        return False

    if since_move > _RELOG_STUCK_SECONDS:
        session.log_event("error", "frozen",
                          f"no movement for {since_move:.0f}s despite retries "
                          f"(server likely dropping our walks); forcing a relog")
        session.closed.set()
        return True

    if since_move > _STUCK_SECONDS and (now - session.last_block_clear) > _STUCK_SECONDS:
        # get_avoid_hazards() (not get_hazards()), same reasoning as _report_to_colony:
        # a USE-type link source shouldn't survive this "clear my own blocks" reset either.
        keep = session.colony.get_avoid_hazards() if session.colony is not None else set()
        cleared = len(session.state.blocked_tiles - keep)
        session.state.blocked_tiles = set(keep)
        session.state.blocked_expiry.clear()  # all local blocks just went away
        session.last_block_clear = now         # rate-limit the clear (NOT last_move_time)
        session.log_event("warning", "stuck",
                          f"no movement for {since_move:.0f}s; cleared {cleared} "
                          f"stale blocked tiles and retrying")
    return False


_AUTOWALK_CHUNK = 24   # steps sent per auto-walk packet (server allows up to 255)


@log_call
async def _autowalk_run(session: GameSession, chunk: list[str], start):
    """Send `chunk` (direction names) as ONE auto-walk packet and follow the moves.

    The server queues the whole path and walks it out (0x6D per step); we watch our
    position against the chunk's expected tiles and stop the moment something differs.
    Returns `(kind, steps_done, *ctx)`:
      ("done", n, end_pos)                 - reached the chunk's expected end
      ("rejected", n, blocked_tile)        - server cancelled; couldn't enter that tile
      ("hazard", n, prev, cur, direction)  - a step relocated us (floor change/teleport)
      ("glitch", n)                        - a small off-path nudge; just re-plan
      ("silent", n, dead_tile)             - server went silent on the next step
      ("closed", n)                        - session ended
    Any anomaly sends stop-auto-walk (0x69) so the queued remainder doesn't wander.
    """
    expected = [(start.x, start.y, start.z)]
    p = expected[0]
    for d in chunk:
        dx, dy = DIRECTION_DELTAS[d]
        p = (p[0] + dx, p[1] + dy, p[2])
        expected.append(p)
    idx_of = {pos: i for i, pos in enumerate(expected)}  # BFS paths don't revisit
    end = expected[-1]

    await session.autowalk(chunk)
    done = 0
    while done < len(chunk):
        session.move_event.clear()
        session.last_rejected = False
        try:
            await asyncio.wait_for(session.move_event.wait(), timeout=3.0)
        except TimeoutError:
            cur = session.state.position
            if cur is not None and (cur.x, cur.y, cur.z) == end:
                return ("done", len(chunk), cur)     # all moves arrived, missed the event
            return ("silent", done, expected[done + 1])
        if session.closed.is_set() or session.stop_event.is_set():
            return ("closed", done)
        if session.last_rejected:
            await session.stop_autowalk()
            return ("rejected", done, expected[done + 1])
        cur = session.state.position
        if cur is None:
            return ("closed", done)
        cpos = (cur.x, cur.y, cur.z)
        j = idx_of.get(cpos)
        if j is not None and j > done:          # advanced along the path (maybe >1)
            done = j
            if done == len(chunk):
                return ("done", done, cur)
            continue
        # Off the expected path: a movement glitch (small same-floor nudge) or a hazard
        # (floor change / teleport). Either way stop the queued remainder.
        prev, nxt = expected[done], expected[done + 1]
        await session.stop_autowalk()
        if cpos[2] == prev[2] and abs(cpos[0] - nxt[0]) + abs(cpos[1] - nxt[1]) <= 2:
            return ("glitch", done)
        return ("hazard", done, prev, cur, chunk[done])
    return ("done", done, session.state.position)


@log_call
async def _walk_local(session: GameSession, item_flags: ItemFlags,
                      goal_x: int, goal_y: int, max_steps: int = 300,
                      plan_radius: int | None = None, log_stuck: bool = True) -> bool:
    """Walk the character to (goal_x, goal_y), following the path via chunked
    auto-walk and re-planning after each chunk (or the moment a step misbehaves).

    We plan a path, hand a chunk of it to the server as ONE auto-walk packet
    (`_autowalk_run`), and re-plan when the chunk finishes or a step is
    rejected/relocates us. This keeps us robust (the map updates live; a bad step
    leads to a fresh plan) while sending ~1 packet per chunk instead of per step —
    the change that keeps us under Canary's flood cutoff. Same-floor goals (B2/B3).

    `plan_radius` bounds each re-plan's flood to a local box (see
    `find_nearest_step_toward`); the frequent explorer/scout callers pass `_PLAN_RADIUS` so
    per-step planning stays cheap with many bots. `goto`/`travel` leave it None for
    exact long-distance routing.

    `log_stuck` controls whether failing to reach the goal records a "navigation"
    issue. Callers heading for a target that SHOULD be reachable (a frontier tile,
    a goto) leave it True. Callers doing deliberately-random wandering (the home
    milling fallback) pass False, since not arriving at a random point is expected
    and shouldn't spam the issue log.
    """
    from .nav import find_nearest_step_toward

    # Remember how far we started from the goal so we can tell, at the end, whether
    # running out of `max_steps` meant "genuinely impeded" (worth logging as an
    # issue) or just "the target was farther than the budget" (normal, stay quiet).
    start_pos = session.state.position
    start_dist = (abs(start_pos.x - goal_x) + abs(start_pos.y - goal_y)
                  if start_pos is not None else None)

    steps = 0
    while steps < max_steps:
        pos = session.state.position
        if pos is None or session.closed.is_set() or session.stop_event.is_set():
            return False
        if (pos.x, pos.y) == (goal_x, goal_y):
            log.info("arrived at %s", pos)
            return True

        # Hit back while we walk. A bot spends most of its life inside this loop, not in
        # the scout round, so without this a monster could chase and chew on us the whole
        # way to a goal and we'd never swing. Costs a packet only when the target changes.
        await _engage_adjacent(session)

        # Plan toward the goal over the *known* map. When the goal is still beyond
        # what we've seen, this heads for the reachable tile closest to it; each
        # chunk scrolls more map in, so the next plan reaches further (B3). We hand it
        # the colony's shared walkable set too, so the flood can cross ground a teammate
        # mapped (or that we saw before relogging) instead of dead-ending on our private
        # view — see find_nearest_step_toward's `extra_walkable`.
        shared = session.colony.walkable_ref() if session.colony is not None else None
        registry = session.colony.traversal if session.colony is not None else None
        known_links = set(session.colony.get_links()) if session.colony is not None else None
        # colony.get_unconfirmed_crossings() is GLOBAL — built from every bot's sightings, not
        # just ours — so this catches a hazard another bot discovered even if this session
        # has never personally seen that exact tile (see nav.find_nearest_step_toward's docstring).
        colony_hazards = (session.colony.get_unconfirmed_crossings()
                         if session.colony is not None else None)
        path = find_nearest_step_toward(session.state, item_flags, goal_x, goal_y,
                                max_radius=plan_radius, extra_walkable=shared,
                                registry=registry, confirmed_links=known_links,
                                unconfirmed_crossings=colony_hazards)
        # Publish the intent BEFORE acting on it, so a bot that can't plan still shows
        # the dashboard what it was reaching for. That "goal set, plan empty" state is
        # precisely what an outside observer sees as a frozen bot. `plan_source` records
        # WHICH planner is steering (this is the myopic local one) so the dashboard can
        # show which path is producing a given decision or failure.
        session.goal = (goal_x, goal_y, pos.z)
        session.plan = _path_tiles(pos, path) if path else []
        session.plan_source = "local"
        if not path:
            log.info("stuck: cannot get closer to (%d, %d) from %s", goal_x, goal_y, pos)
            session._report_to_colony()   # make the failed attempt visible
            return False

        chunk = path[:_AUTOWALK_CHUNK]
        outcome = await _autowalk_run(session, chunk, pos)
        kind, done = outcome[0], outcome[1]
        steps += max(1, done)   # count real progress so max_steps still bounds us

        if kind == "closed":
            return False
        if kind in ("done", "glitch"):
            continue            # re-plan the next chunk (or hit the arrival check)
        if kind == "silent":
            # The server neither moved us nor cancelled — it silently ignored the next
            # step (a tile it won't let us onto but won't reject). Block it briefly and
            # re-plan AROUND it (the block expires, so a fluke gets retried later).
            dead = outcome[2]
            session.state.blocked_tiles.add(dead)
            session.state.blocked_expiry[dead] = time.monotonic() + _BLOCK_TTL
            log.warning("no response walking toward %s; blocking %s for %.0fs and re-planning",
                        dead, dead, _BLOCK_TTL)
            await asyncio.sleep(0.1)
            continue
        if kind == "rejected":
            # A step was refused. Transient (a creature/other bot raced onto the tile)
            # or permanent (an obstacle our flags missed). Only mark a block when no
            # creature is there, so a passing monster doesn't poison the shared map and
            # slowly wall us in (the accumulating-blocked-tiles stuck-loop).
            blocked = outcome[2]
            occupied = any(
                c.position is not None
                and (c.position.x, c.position.y, c.position.z) == blocked
                for c in session.state.nearby_creatures()
            )
            if occupied:
                log.debug("walk rejected by a creature at %s; waiting, not blocking", blocked)
            else:
                session.state.blocked_tiles.add(blocked)
                session.state.blocked_expiry[blocked] = time.monotonic() + _BLOCK_TTL
                log.info("walk rejected entering %s; blocking for %.0fs and re-planning",
                         blocked, _BLOCK_TTL)
            await asyncio.sleep(0.1)
            continue
        if kind == "hazard":
            # A step relocated us — stairs/ramp/hole (floor changed) or a teleport
            # (position jumped). Mark the source blocked + share it as a shortcut so
            # every bot learns this exit, then (for a floor change) step back.
            _n, prev, new, direction = outcome[1], outcome[2], outcome[3], outcome[4]
            dx, dy = DIRECTION_DELTAS[direction]
            hazard = (prev[0] + dx, prev[1] + dy, prev[2])   # the tile we stepped onto
            session.state.blocked_tiles.add(hazard)
            if session.colony is not None:
                # An ordinary walk step can only ever accidentally trigger a STEP-type
                # object (a USE-type one does nothing when merely stepped over) — see
                # colony._step_links.
                session.colony.report_link(hazard, (new.x, new.y, new.z), action="step")
                src_items = session.state.tiles.get(hazard)
                if src_items:
                    from .traversal import category_from_relocation
                    xy_jump = abs(new.x - hazard[0]) + abs(new.y - hazard[1])
                    category = category_from_relocation(prev[2], new.z, xy_jump)
                    session.colony.traversal.learn_from_tile(tile_ids(src_items), category)
            htype = "floor-change" if new.z != prev[2] else "teleport"
            log.info("hazard (%s) at %s -> landed %s; recorded shortcut, avoiding for now",
                     htype, hazard, new)
            if new.z != prev[2]:
                # Same-floor navigation only: step back onto our floor (single step).
                reverse = {"north": "south", "south": "north",
                           "east": "west", "west": "east"}[direction]
                session.move_event.clear()
                session.last_rejected = False
                await session.walk(reverse)
                try:
                    await asyncio.wait_for(session.move_event.wait(), timeout=3.0)
                except TimeoutError:
                    pass
            continue

    # Budget exhausted without arriving. Only flag this as an issue if we barely
    # closed the gap — otherwise it just means the target was farther than the step
    # budget (the bot was walking fine), which is normal and shouldn't spam the log.
    end_pos = session.state.position
    if log_stuck and start_dist is not None and end_pos is not None:
        progress = start_dist - (abs(end_pos.x - goal_x) + abs(end_pos.y - goal_y))
        if progress < max(2, max_steps // 3):
            session.log_event("warning", "navigation",
                              f"little progress toward ({goal_x},{goal_y}): moved "
                              f"{progress} tiles closer in {max_steps} steps")
    return False


def _path_tiles(start, directions: list[str]) -> list[tuple[int, int, int]]:
    """Turn a list of step directions into the tiles they cross, for the intent overlay.

    The planners speak in directions (that's what the walk packets want), but a human
    looking at a map needs coordinates.
    """
    x, y, z = start.x, start.y, start.z
    tiles = []
    for name in directions:
        dx, dy = DIRECTION_DELTAS[name]
        x, y = x + dx, y + dy
        tiles.append((x, y, z))
    return tiles


@log_call
async def _cross_door(session: GameSession, item_flags: ItemFlags,
                      door: tuple[int, int, int], direction: str) -> bool:
    """We're adjacent to a tile the colony knows as a door; open it if our freshest
    local view says it's currently shut, then step onto/through it.

    The shared map only ever records THAT a tile is a door (`contribute_tiles`'s
    door_unlocked self-link seeding) — never whether it's open or closed right now,
    since that changes moment to moment and a tile is only ever classified once
    (never re-checked after `_explored` claims it). So this asks `state.tiles` — this
    bot's own view, freshest right where we're standing — instead of trusting that
    stale shared belief either way.

    Skipping the `use_item` when it's ALREADY open isn't just an optimization: using
    an open door a second time TOGGLES it shut in Tibia, so blindly using it on every
    crossing (the way `_take_shortcut` does for a genuine, always-use-it shortcut)
    would have us slamming doors on ourselves. Doors are the one link kind where
    "is it actually closed right now" has to be checked, not assumed.

    Returns True if we ended up past it (whether or not opening was needed), False if
    the step still failed afterward (locked, or something else occupying it).
    """
    from .nav import is_walkable
    colony = session.colony
    if colony is not None and not is_walkable(session.state, item_flags, *door):
        items = session.state.tiles.get(door)
        if items:
            hit = colony.traversal.classify(tile_ids(items))
            if hit is not None and hit[0] in _DOOR_CATEGORIES:
                await _perform_traversal(session, door, direction, hit[0], hit[1])
    return await _raw_step(session, direction)


@log_call
async def _follow_shared_route(session: GameSession, item_flags: ItemFlags,
                               goal_x: int, goal_y: int, goal_z: int, reach: int = 0,
                               max_steps: int = 400,
                               seed_route: list | None = None) -> bool:
    """Walk to (or within `reach` of) a goal by following a route planned over the
    COLONY's shared explored map. Returns True if we arrived.

    This exists because `_walk_local` is MYOPIC, and that is the single biggest reason
    bots fail to get anywhere. `_walk_local` floods `state.tiles` — only what THIS bot has
    personally seen this session — and then greedily heads for the reachable tile
    nearest the goal. Two failure modes follow, and both were biting us hard:

      1. A goal past the bot's ~8-tile view isn't in its map at all, so the flood finds
         nothing better than where it stands: it takes ZERO steps and reports "stuck",
         silently. Waypoint-hopping toward the goal doesn't save it, because
      2. "nearest reachable tile" is a greedy heuristic, and a town is a maze. The bot
         walks into the dead end that happens to be closest to the goal and stops. Going
         AROUND a building means first moving AWAY from the target, which greedy never
         does.

    The colony already knows the whole town — every bot pours its tiles into the shared
    map — and `find_shared_route` already does a proper Dijkstra over it. That knowledge was
    just never used for walking: `travel` planned a real route and then threw it away,
    handing the on-foot legs back to `_walk_local`. So we re-derived a blind local guess
    from a private map when a correct global route was already in hand.

    Here we follow the actual route: take its directions, hand them to the server in
    autowalk chunks, and re-plan (still over the shared map) whenever a step misbehaves.
    Tiles the server refuses go into the bot's `blocked_tiles` and are fed back as
    `avoid`, so the shared map — which only ever grows and never marks a tile bad —
    can't keep routing us into the same wall.

    The route is a PLAN, not a query we repeat out of habit: we compute it once with
    `find_shared_route` (a Dijkstra over the whole shared map — not free) and then just
    walk successive `_AUTOWALK_CHUNK` slices of that same route, chunk after chunk,
    with no re-planning in between. `_autowalk_run` tells us exactly when the cached
    plan stops being trustworthy — "done" means we landed EXACTLY where the chunk
    predicted, so the remaining cached steps are still valid and we simply advance our
    index into them; anything else (glitch/silent/rejected/hazard) means we ended up
    somewhere the plan didn't expect, so the directions after it no longer point where
    we think — THAT is when we throw the route away and pay for a fresh Dijkstra from
    wherever we actually are. A clean multi-hundred-tile walk now costs one plan instead
    of one every ~24 steps.

    `seed_route` lets a caller that already ran an equivalent `find_shared_route` hand
    us its answer instead of making us solve the identical problem again. `travel` is
    the one caller in this position: it plans WITH links (to look for a shortcut hop),
    and when it finds none, the route it just computed IS this walker's route too —
    same start, same goal, and provably no link tile anywhere on it (a link crossing
    always shows up as `is_teleport=True`, so "no shortcut found" means the search
    never touched one). We only spend it once, on the very first iteration; every
    replan after that (something drifted, so the cached plan's directions no longer
    apply) goes back to computing our own, since by then `blocked_tiles` may have
    grown in ways `travel`'s search never saw.

    A DOOR along the route is handled inline, not by `travel` — unlike every other
    link kind, using one never relocates you (see `contribute_tiles`'s door_unlocked
    self-link seeding: source -> itself), so there's no frame-of-reference shift that
    would demand travel's stop/hop/re-plan machinery. Before each chunk we scan the
    upcoming stretch of the CACHED route (bounded to `_AUTOWALK_CHUNK`, using the
    colony's link table — global knowledge, so this works exactly as well 24 tiles
    into territory we've never personally seen as it does one step away) for the
    first tile recorded as a door. If one turns up, the chunk is cut short right
    before it, and `_cross_door` opens it (only if our freshest local view says it's
    actually shut right now — see there) and steps through, WITHOUT discarding the
    rest of the cached route. Only a door that won't open at all (locked, or
    something's occupying it) forces a real replan.

    Scouts still want `_walk_local`: heading into UNEXPLORED space is exactly what the
    shared map can't help with, and greedy-toward-the-goal is the right instinct there.
    So this returns False when there's no colony or no known route, and callers fall
    back. Known destination -> route it; unknown frontier -> feel your way.
    """
    from .nav import find_shared_route, within_reach

    colony = session.colony
    if colony is None:
        return False

    goal = (goal_x, goal_y, goal_z)
    steps = 0
    route = None            # the cached plan: list of (direction, is_teleport, landing)
    idx = 0                 # how far into `route` we've already walked
    while steps < max_steps:
        pos = session.state.position
        if pos is None or session.closed.is_set() or session.stop_event.is_set():
            return False
        here = (pos.x, pos.y, pos.z)
        if within_reach(here, goal, reach):
            return True

        # Hit back while we walk — same reason as in `_walk_local`: this loop is where the
        # time goes, so self-defence has to live here too, not only in the scout round.
        await _engage_adjacent(session)

        # Let transient blocks lapse first, so a bot that stepped into our path once
        # doesn't leave a permanent phantom wall in the route planner.
        _prune_blocked_tiles(session)

        if route is None or idx >= len(route):
            if seed_route is not None:
                # `travel` already solved this one (see the docstring) — spend it
                # instead of re-running an identical Dijkstra. One-time use: clear it
                # so every later replan in this call computes its own, current route.
                route = seed_route
                seed_route = None
            else:
                # Links are deliberately EXCLUDED: this is the on-foot leg walker. `travel`
                # owns shortcut traversal, because stepping onto a teleport needs a raw step
                # and a re-plan afterwards. Passing links here would produce a route whose
                # directions we'd then walk straight into a stairwell.
                # get_avoid_hazards() (not get_hazards()) excludes a USE-type link source (a
                # grate): walking over or near one does nothing, so treating it as forbidden
                # ground can make a goal that sits exactly on one permanently unreachable here —
                # this walker has no OTHER way to reach it, since `links={}` here deliberately
                # excludes the "deliberately use it" option too. STEP-type sources stay avoided
                # (genuinely dangerous to blunder into on foot) — see colony.get_avoid_hazards().
                avoid = colony.get_avoid_hazards() | set(session.state.blocked_tiles)
                route = find_shared_route(colony.get_walkable(), {}, here, goal, reach=reach,
                                   avoid=avoid, costs=colony.get_walk_costs(),
                                   unconfirmed_crossings=colony.get_unconfirmed_crossings(),
                                   step_links=colony.get_step_links())
            idx = 0
            session.goal = goal
            session.plan_source = "shared"   # find_shared_route over the colony map
            if not route:
                # Either we're there (handled above) or the shared map doesn't connect us.
                session.plan = []
                session._report_to_colony()
                return False

        session.plan = [landing for _dir, _tp, landing in route[idx:]]

        # Look ahead (bounded, cheap — dict lookups against the colony's link table,
        # not a live-sight check) for the first door in the stretch we're about to
        # walk. A door is a SELF-link: `links[tile] == tile` is what tells one apart
        # from an ordinary relocating link (stairs/hole/teleporter), which we must
        # NOT try to inline-cross here — that needs travel's stop/hop/re-plan dance.
        links = colony.get_links()
        door_at = next((k for k, (_dir, _tp, landing) in
                        enumerate(route[idx:idx + _AUTOWALK_CHUNK])
                        if links.get(landing) == landing), None)

        if door_at == 0:
            direction, _tp, door_tile = route[idx]
            if await _cross_door(session, item_flags, door_tile, direction):
                idx += 1
                steps += 1
                continue
            # Didn't open/cross. Same reasoning as an ordinary rejected step below:
            # only blame the DOOR (permanently prune it) if nothing alive explains
            # the failure — a creature standing on it is transient, not a lock.
            occupied = any(
                c.position is not None
                and (c.position.x, c.position.y, c.position.z) == door_tile
                for c in session.state.nearby_creatures())
            if not occupied:
                colony.mark_bad_link(door_tile)
                session.log_event("warning", "door",
                                  f"door at {door_tile} wouldn't open or cross; pruned")
            session.state.blocked_tiles.add(door_tile)
            session.state.blocked_expiry[door_tile] = time.monotonic() + _BLOCK_TTL
            route = None
            await asyncio.sleep(0.1)
            continue

        end = idx + door_at if door_at is not None else idx + _AUTOWALK_CHUNK
        chunk = [direction for direction, _tp, _land in route[idx:end]]
        outcome = await _autowalk_run(session, chunk, pos)
        kind, done = outcome[0], outcome[1]
        steps += max(1, done)

        if kind == "closed":
            return False
        if kind == "done":
            # Landed exactly where the cached plan predicted — the rest of `route` is
            # still trustworthy, so just advance through it. No replan.
            idx += done
            continue

        # Every other outcome means we're no longer where the cached plan assumed, so
        # its remaining directions would walk us somewhere wrong. Drop it; the top of
        # the loop will pay for one fresh Dijkstra from our real position.
        route = None
        if kind == "glitch":
            continue
        if kind == "silent":
            dead = outcome[2]
            session.state.blocked_tiles.add(dead)
            session.state.blocked_expiry[dead] = time.monotonic() + _BLOCK_TTL
            await asyncio.sleep(0.1)
            continue
        if kind == "rejected":
            blocked = outcome[2]
            occupied = any(
                c.position is not None
                and (c.position.x, c.position.y, c.position.z) == blocked
                for c in session.state.nearby_creatures()
            )
            if not occupied:
                # A real obstacle the shared map thinks is walkable. Remember it so the
                # next re-plan routes around instead of retrying it forever.
                session.state.blocked_tiles.add(blocked)
                session.state.blocked_expiry[blocked] = time.monotonic() + _BLOCK_TTL
            await asyncio.sleep(0.1)
            continue
        if kind == "hazard":
            # We walked onto a shortcut mid-route. Record it and let `travel` decide;
            # for a plain on-foot leg, blocking it and re-planning is enough.
            _n, prev, new, direction = outcome[1], outcome[2], outcome[3], outcome[4]
            dx, dy = DIRECTION_DELTAS[direction]
            hazard = (prev[0] + dx, prev[1] + dy, prev[2])
            session.state.blocked_tiles.add(hazard)
            colony.report_link(hazard, (new.x, new.y, new.z), action="step")  # see _autowalk_run
            if new.z != prev[2]:
                return False   # we're off our floor; the caller re-plans globally
            continue
    return False


async def _raw_step(session: GameSession, direction: str, timeout: float = 3.0) -> bool:
    """Send one walk step and wait for the outcome, WITHOUT any avoidance.

    Unlike `_walk_local` (which plans around hazards), this steps exactly where told —
    used by `travel` to deliberately walk onto a stair/teleport tile to traverse a
    shortcut. Returns True if we moved (or teleported), False on reject/timeout.
    """
    session.move_event.clear()
    session.last_rejected = False
    await session.walk(direction)
    try:
        await asyncio.wait_for(session.move_event.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return not session.last_rejected


def _adjacent_monsters(session: GameSession) -> list:
    """Monsters standing on a tile next to us (Chebyshev 1) on our floor.

    These are the ones that can actually box us in — a monster one tile away occupies a
    tile the walker won't step onto. NPCs/players are excluded (kind), so we never swing
    at a shopkeeper or another bot. Weakest first, so we clear the tile likeliest to open
    soonest. Uses `state.creatures`, which is already pruned to the viewport.
    """
    p = session.state.position
    if p is None:
        return []
    out = []
    for c in session.state.creatures.values():
        if c.position is None or c.creature_id == session.state.player_id:
            continue
        if c.kind != "monster":
            continue
        if c.position.z == p.z and max(abs(c.position.x - p.x),
                                       abs(c.position.y - p.y)) == 1:
            out.append(c)
    out.sort(key=lambda c: c.health_percent)
    return out


async def _engage_adjacent(session: GameSession) -> bool:
    """Melee whatever monster is standing next to us. Cheap, non-blocking, every round.

    This is SELF-DEFENCE, and it's deliberately separate from `_fight_free` (which is an
    escape tool that only runs once a scout has been immobile for 8 straight rounds). A
    bot being chewed on while it's still walking never trips that counter, so before this
    existed a scout would take a beating the whole way across a map and never swing back.

    The trick that makes running this every round free: 0xA1 sets a PERSISTENT server-side
    attack target — the server keeps swinging every turn while we're adjacent (see
    `attack`). So we don't re-send anything while the fight is going; we only send a packet
    when the target actually CHANGES. In a steady fight that's one packet per kill, which
    keeps us far under Canary's flood cutoff even with a large swarm, and it never blocks
    the caller, so exploration continues while the server does the melee.

    We also FOCUS FIRE: once locked onto a monster we stay on it while it's still next to
    us, rather than re-picking the weakest each round (their HP changes as we hit them,
    which would otherwise make us flip targets constantly and spam re-target packets).

    Returns True if we're engaged with something.
    """
    adj = _adjacent_monsters(session)
    if not adj:
        # Nothing next to us any more — drop the auto-attack so we don't stay locked on
        # a creature that died or wandered off.
        if session.attack_target is not None:
            await session.attack(0)
            session.attack_target = None
        return False
    # Still fighting the same monster? The server is already swinging — send nothing.
    if any(c.creature_id == session.attack_target for c in adj):
        return True
    target = adj[0]                      # _adjacent_monsters sorts weakest first
    await session.attack(target.creature_id)
    session.attack_target = target.creature_id
    log.debug("engaging %s (%d%% hp); %d adjacent",
              target.name or "monster", target.health_percent, len(adj))
    return True


# How long we'll keep meleeing our way out before giving up this bout (seconds). A
# bounded fight: if we can't clear a path in this window, fall back to the normal escape
# chain rather than swing forever.
_FIGHT_FREE_SECONDS = 12.0


@log_call
async def _fight_free(session: GameSession, item_flags: ItemFlags,
                      max_seconds: float | None = None) -> bool:
    """Boxed in by MONSTERS: melee them until a tile opens, then let the caller re-plan.

    Only called when we're already stuck and can't step out — so there's nowhere to flee
    to and no health check to make (fleeing isn't an option; the way out IS through the
    monster). We target the weakest adjacent monster, let the server auto-melee, and stop
    the moment ANY neighbour becomes walkable (a monster died or wandered off). Returns
    True if we opened a path.

    `max_seconds` caps the bout below the usual `_FIGHT_FREE_SECONDS` — a time-boxed
    caller (`navigate_to` with a `deadline`) passes whatever's left of its own budget, so
    a fight can't run the caller past a deadline it promised to respect. None uses the
    default.
    """
    from .nav import is_walkable
    if not _adjacent_monsters(session):
        return False    # not a monster problem — leave it to the normal escape chain

    loop = asyncio.get_event_loop()
    budget = _FIGHT_FREE_SECONDS if max_seconds is None else max(0.0, max_seconds)
    deadline = loop.time() + budget
    session.status = "fighting free"
    session.expect_movement = False   # standing and swinging is intended; don't relog us
    target = None
    while loop.time() < deadline and not session.closed.is_set() \
            and not session.stop_event.is_set():
        p = session.state.position
        if p is None:
            return False
        # Tiles a creature occupies RIGHT NOW. A monster stands on walkable floor, and we
        # never add creature-occupied tiles to blocked_tiles, so is_walkable reports the
        # rat's own tile as a free exit — the original bug: _fight_free saw a "way out"
        # and returned before ever swinging. A tile is only a real exit if it's walkable
        # AND nothing is standing on it. (ignore_blocked: judge terrain + occupancy, not
        # our stale block map, so a tile that just cleared isn't hidden by an old block.)
        occupied = {(c.position.x, c.position.y)
                    for c in session.state.creatures.values()
                    if c.position is not None and c.position.z == p.z
                    and c.creature_id != session.state.player_id}
        opened = any(
            (p.x + dx, p.y + dy) not in occupied
            and is_walkable(session.state, item_flags, p.x + dx, p.y + dy, p.z,
                            ignore_blocked=True)
            for _name, (dx, dy) in DIRECTION_DELTAS.items())
        if opened:                       # a monster died or wandered off -> go
            session.expect_movement = True
            return True
        adj = _adjacent_monsters(session)
        if not adj:
            session.expect_movement = True
            return True     # nothing adjacent to fight and (checked above) a tile's open
        # (Re)issue the attack on the weakest adjacent monster. Re-target if ours died.
        # Keep `session.attack_target` in step: `_engage_adjacent` runs every round too and
        # decides whether to send a packet by comparing against it, so if we changed the
        # server's target behind its back the two would keep overwriting each other.
        weakest = adj[0]
        if target != weakest.creature_id:
            target = weakest.creature_id
            session.log_event("info", "combat",
                              f"boxed in — meleeing {weakest.name or 'monster'} "
                              f"({weakest.health_percent}% hp); {len(adj)} adjacent")
            await session.attack(target)
            session.attack_target = target
        await asyncio.sleep(1.0)   # a melee swing lands roughly this often
    session.expect_movement = True
    await session.attack(0)        # stop attacking when we give up the bout
    session.attack_target = None
    return False


@log_call
async def _escape_step(session: GameSession, item_flags: ItemFlags,
                       tries_per_dir: int = 3) -> bool:
    """Boxed-in last resort: physically step OUT, bypassing the blocked-tile map.

    Why this exists: when scouts stack up (all crowded onto/around one tile, blocking
    each other), each one accumulates dozens of `blocked_tiles` around itself, so the
    planner — which treats every blocked tile as a wall — refuses to route ANY step and
    the bot freezes, force-relogs, and respawns in the same spot. But most of those
    blocks are STALE: the other bot that was in the way has since moved. So here we drop
    to raw stepping. For each direction whose *terrain* is walkable (ignoring the block
    map), we try to step, and we RETRY the same direction a few times — the obstacle is
    usually another creature/bot about to move, or a walk packet the server dropped, and
    a moment later the same step goes through. The first direction that moves us wins.

    Directions are shuffled so a cluster of stacked bots don't all pick the same exit
    and re-collide. On a successful step we clear that tile's stale block so the planner
    can use it again. Returns True if we moved.
    """
    import random

    pos = session.state.position
    if pos is None:
        return False
    from .nav import is_walkable

    dirs = list(DIRECTION_DELTAS.items())
    random.shuffle(dirs)
    for name, (dx, dy) in dirs:
        nx, ny, nz = pos.x + dx, pos.y + dy, pos.z
        # Only try real terrain — a genuine wall/water/void is never worth hammering.
        # `ignore_blocked=True` is the whole point: we DO try tiles we'd earlier blocked.
        if not is_walkable(session.state, item_flags, nx, ny, nz, ignore_blocked=True):
            continue
        for _ in range(tries_per_dir):
            if session.closed.is_set() or session.stop_event.is_set():
                return False
            if await _raw_step(session, name):
                # We moved onto it, so it wasn't really impassable — clear any stale
                # block so the planner stops treating it as a wall.
                session.state.blocked_tiles.discard((nx, ny, nz))
                session.state.blocked_expiry.pop((nx, ny, nz), None)
                log.info("escape: broke out %s to (%d, %d, %d)", name, nx, ny, nz)
                if session.recorder is not None:
                    session.recorder.decision("escaped", dir=name, to=[nx, ny, nz])
                return True
            # Rejected or dropped — wait a beat for the blocker to move / the flood
            # window to pass, then try the SAME direction again.
            await asyncio.sleep(0.35)
    return False


@log_call
async def _perform_traversal(session: GameSession, source: tuple[int, int, int],
                             direction: str, category: str, item_id: int):
    """Execute the action a traversal `category` requires, at tile `source` (x,y,z).

    The bot must already be standing adjacent to (or on) `source`. Dispatches by the
    category's action, looked up from the registry (see traversal.CATEGORY_META):
      - STEP  (teleporter / stairs / open hole): walk onto it in `direction`. This
        is the same move the plain link-executor makes; we route it here so callers
        have one entry point for "traverse whatever is on this tile".
      - USE   (ladder / grate / well / unlocked door): `use_item` the object. The
        server then either relocates us (ladder/grate carry us to another floor) or
        just opens the way (an unlocked door — no relocation).
      - USE_WITH (shovel/rope hole, locked door) and CAST (rope spell): these need a
        carried tool/key or spell words. Inventory isn't tracked yet, so for now we
        log the need and skip — this is exactly where inventory parsing plugs in.

    Returns the Position we ended up at IF the action relocated us (a real shortcut),
    else None. Recording the resulting link is the caller's responsibility.
    """
    from .traversal import STEP, USE, USE_WITH, UNBLOCK, TOOL_IDS

    reg = session.colony.traversal if session.colony is not None else None
    kind = reg.kind(category) if reg is not None else None
    if kind is None:
        return None
    before = session.state.position
    if before is None:
        return None

    if kind.action == STEP:
        await _raw_step(session, direction)
    elif kind.action == USE:
        # stackpos is the object's index in the tile's rendered stack. Our tiles
        # list is ground-first, so the object sits at the highest index carrying its
        # id (there can be several items on a tile).
        items = session.state.tiles.get(source, [])
        stackpos = max((i for i, (v, _c) in enumerate(items) if v == item_id),
                       default=max(len(items) - 1, 0))
        await session.use_item(source[0], source[1], source[2], item_id, stackpos)
        # Using a ladder/grate makes the server move us (as a floor-change frame, not
        # a normal 0x6D step), so we can't wait on move_event — give it a moment and
        # then read our position.
        await asyncio.sleep(0.35)
        # Grates are ambiguous and we can't tell statically (see traversal.seed_by_name):
        # some are USE objects, others are step-teleports baked into the map. If USE
        # didn't move us, STEP onto it — and if THAT moves us, relearn the id as the step
        # object it actually is, so future routing steps onto it instead of uselessly
        # "using" it. This is what makes "try grates on sight" work for either mechanism.
        if category == "grate":
            here = session.state.position
            if here is not None and (here.x, here.y, here.z) == (before.x, before.y, before.z):
                await _raw_step(session, direction)
                stepped = session.state.position
                if (stepped is not None and reg is not None
                        and (stepped.x, stepped.y, stepped.z) != (before.x, before.y, before.z)):
                    from .traversal import category_from_relocation
                    xy = abs(stepped.x - before.x) + abs(stepped.y - before.y)
                    reg.learn(item_id, category_from_relocation(before.z, stepped.z, xy))
    elif kind.action == USE_WITH:
        # Use a carried tool ON the object (shovel-on-hole, machete-on-grass). Look
        # up the required tool in our inventory (now that we parse it); if we don't
        # carry it, skip. (Keys are per-door and not in TOOL_IDS yet.)
        tool_ids = TOOL_IDS.get(kind.requires or "")
        carried = session.state.carries(*tool_ids) if tool_ids else None
        if carried is None:
            session.log_event("info", "traversal",
                              f"{category} at {source} needs a '{kind.requires}' we "
                              f"don't carry; skipping")
            return None
        tool_pos, tool_id = carried
        items = session.state.tiles.get(source, [])
        to_stack = max((i for i, (v, _c) in enumerate(items) if v == item_id),
                       default=max(len(items) - 1, 0))
        # tool_pos[2] doubles as the from-stackpos (0 worn, slot index in a container).
        await session.use_item_with(tool_pos, tool_id, tool_pos[2],
                                    source, item_id, to_stack)
        await asyncio.sleep(0.35)
    else:  # CAST (e.g. Magic Rope) — spell layer not built yet
        session.log_event("info", "traversal",
                          f"{category} at {source} needs a '{kind.requires}' spell; "
                          f"not built yet — skipping")
        return None

    after = session.state.position
    if after is None:
        return None
    if (after.x, after.y, after.z) != (before.x, before.y, before.z):
        return after   # the action relocated us — a shortcut

    if kind.direction == UNBLOCK:
        # By design this NEVER relocates us (opening a door doesn't move you), so the
        # relocation check above can't tell success from failure here. Optimistically
        # treat the object as cleared so whatever walks next (the local walker, or the
        # shared-route follower after a router-driven `_take_shortcut` hop) can pass
        # through right away, in case the server's own transform (closed->open) is
        # still in flight. A still-shut door (locked, no key — USE_WITH above already
        # skipped without touching the tile) simply re-blocks on the next rejected
        # step; nothing here claims success that wasn't earned.
        items = session.state.tiles.get(source)
        if items:
            session.state.tiles[source] = [(i, c) for (i, c) in items if i != item_id]
        session.state.blocked_tiles.discard(source)
        session.state.blocked_expiry.pop(source, None)
    return None


@log_call
async def _take_shortcut(session: GameSession, source: tuple[int, int, int],
                         direction: str) -> None:
    """Traverse a shortcut source tile the right way: USE it if it carries a known
    use-object (ladder/grate/…), otherwise step onto it (teleporter/stairs/hole).

    Used by `travel`, which only routes through relocation links, so whatever we
    dispatch here is expected to move us; the caller reads `session.state.position`
    afterward to see where we landed (and self-heals a mis-recorded link).
    """
    colony = session.colony
    if colony is not None:
        items = session.state.tiles.get(source)
        if items:
            hit = colony.traversal.classify(tile_ids(items))
            if hit is not None:
                kind = colony.traversal.kind(hit[0])
                if kind is not None and kind.action != "step":
                    await _perform_traversal(session, source, direction, hit[0], hit[1])
                    return
    await _raw_step(session, direction)


def _live_tried(tried: dict, now: float) -> set:
    """Prune expired 'already tried' entries and return the still-cooling-down tile set.

    The exit-object seekers keep a {tile: retry_time} map (see _TRIED_TTL): drop any whose
    retry time has arrived (so a fluke failure gets reconsidered), and hand back the tiles
    still on cooldown as a plain set for the finders' `skip` argument.
    """
    for t in [t for t, exp in tried.items() if now >= exp]:
        del tried[t]
    return set(tried)


# Door categories: objects we OPEN. Directed navigation restricts _try_use_object to these
# (a door clears the way) — it must never "use" a ladder/grate mid-route, which relocates
# us off our path. The scout, hunting any exit, passes only=None.
_DOOR_CATEGORIES = {"door_unlocked", "door_locked"}


@log_call
async def _reach_and_use(session: GameSession, item_flags: ItemFlags, found,
                         max_steps: int = 40):
    """Get beside a found USE-object and use it — the ONE 'walk up to a thing and use it'
    path, shared by the scout's exit hunt and directed navigation so their adjacency
    handling can't drift apart. `found` is `find_use_object`'s tuple (source, item_id,
    category, launch, direction). Returns the Position we landed at IF using it relocated
    us (a ladder/grate shortcut), else None.

    We only need to be ADJACENT: `use_item` works from any of the eight surrounding tiles,
    and — crucially for a SHUT door — `find_use_object` can hand back a `launch` on the FAR
    side of the object, which is unreachable through it. So when we're already beside the
    object we don't chase `launch`. Opening a DOOR (used, no relocation) optimistically
    treats its tile as passable — see `_perform_traversal`'s UNBLOCK handling — so the
    pather routes through it immediately in case the server's transform (closed->open)
    update is still in flight; a still-shut door simply re-blocks on the next rejected step.
    """
    source, item_id, category, launch, direction = found

    def beside(p) -> bool:
        return (p is not None and p.z == source[2]
                and max(abs(p.x - source[0]), abs(p.y - source[1])) == 1)

    if not beside(session.state.position):
        await _walk_local(session, item_flags, launch[0], launch[1],
                       max_steps=max_steps, plan_radius=_PLAN_RADIUS)
        if not beside(session.state.position):
            return None   # couldn't get beside it this lap

    session.status = f"using {category}"
    return await _perform_traversal(session, source, direction, category, item_id)


@log_call
async def _try_use_object(session: GameSession, item_flags: ItemFlags,
                          tried: dict, max_dist: int | None = None,
                          only: set[str] | None = None) -> bool:
    """Walk to the nearest known use-object and traverse it.

    This is what turns the catalog's seeded knowledge into real, routable shortcuts:
    a scout looks for a ladder/grate/hole/door it has seen, walks up to it, and USES
    it. If that relocates us (a ladder), we record the source->destination link so the
    whole colony can route through it (and `travel` will USE it, not step, thanks to
    `_take_shortcut`). Using a DOOR opens it, unblocking a path the pather previously
    treated as a wall.

    `only` restricts to those categories — directed navigation passes {door categories}
    so it opens a blocking door but never "uses" a ladder/grate that would relocate it.
    `tried` maps a recently-attempted object tile -> when we'll try it again, so we don't
    hammer the same one, but ALSO don't abandon it forever after one fluke (see
    _TRIED_TTL). `max_dist` caps how far we'll detour to a candidate. Returns True if we
    attempted an object, False if there was nothing to try.
    """
    from .nav import find_use_object

    colony = session.colony
    if colony is None:
        return False
    now = time.monotonic()
    found = find_use_object(session.state, item_flags, colony.traversal,
                            skip=_live_tried(tried, now), max_dist=max_dist, only=only)
    if found is None:
        return False

    source, category = found[0], found[2]
    tried[source] = now + _TRIED_TTL    # attempted — hold off, but retry after the TTL
    landing = await _reach_and_use(session, item_flags, found)
    if landing is not None:
        # find_use_object only ever returns USE-based objects (it explicitly skips STEP
        # ones — see there), so this is always a deliberate use.
        colony.report_link(source, (landing.x, landing.y, landing.z), action="use")
        session.log_event("info", "traversal",
                          f"used {category} at {source} -> "
                          f"({landing.x},{landing.y},{landing.z}); recorded shortcut")
    return True


@log_call
async def _try_use_underfoot(session: GameSession, item_flags: ItemFlags,
                             tried: dict, direction: str) -> bool:
    """Are we standing exactly on a USE-type floor-change (a ladder/grate) that goes
    `direction`? If so, use it directly — the fix for a real gap `_try_change_floor` can't
    close on its own.

    `_try_change_floor`'s own "standing on it" fallback (see there) only re-triggers a
    STEP-type object: stepping off and back onto it works BECAUSE a STEP object fires on
    the walk itself. A USE-type object is the opposite — walking over it does nothing, so
    the ONLY way to trigger one we're already standing on is a deliberate `_perform_traversal`
    "use", exactly as if we'd just walked up and used it on purpose. `find_descent`
    deliberately skips distance-0 candidates for the same reason `_try_change_floor` handles
    "standing on it" as a separate case rather than folding it into the hunt: an object we're
    ON has no adjacent launch tile to path to.

    `tried` is the SAME cooldown dict floor-change hunting already uses (tile -> retry time),
    so a `use` that doesn't relocate us (a locked door needing a key we don't carry) isn't
    hammered every round. Returns True if we attempted something here, False if there was
    nothing to try (plain ground, a STEP object, the wrong direction, or on cooldown).
    """
    from .nav import is_walkable

    colony = session.colony
    if colony is None:
        return False
    pos = session.state.position
    if pos is None:
        return False
    now = time.monotonic()
    here_tile = (pos.x, pos.y, pos.z)
    if here_tile in _live_tried(tried, now):
        return False
    items = session.state.tiles.get(here_tile)
    if not items:
        return False
    hit = colony.traversal.classify(tile_ids(items))
    if hit is None:
        return False
    category, item_id = hit
    kind = colony.traversal.kind(category)
    if kind is None or kind.action == "step" or kind.direction != direction:
        return False
    # _perform_traversal wants a CARDINAL direction (it uses one for USE's "grate that's
    # secretly a step" fallback, which raw-steps us). We're not launching FROM anywhere —
    # we're already on the object — so any walkable neighbour is a reasonable placeholder
    # for that rare fallback; it's a graceful no-op if it fires and picks wrong (next round
    # just retries).
    cardinal = next((name for name, (dx, dy) in DIRECTION_DELTAS.items()
                     if is_walkable(session.state, item_flags,
                                    pos.x + dx, pos.y + dy, pos.z)), "north")
    tried[here_tile] = now + _TRIED_TTL
    landing = await _perform_traversal(session, here_tile, cardinal, category, item_id)
    if landing is not None and landing.z != pos.z:
        # Gated above on kind.action != "step", so this is always a deliberate use.
        colony.report_link(here_tile, (landing.x, landing.y, landing.z), action="use")
        session.log_event("info", "traversal",
                          f"used {category} at {here_tile} (standing on it) -> "
                          f"({landing.x},{landing.y},{landing.z}); recorded shortcut")
    return True


@log_call
async def _try_change_floor(session: GameSession, item_flags: ItemFlags,
                            tried: dict,
                            max_dist: int | None = None, direction: str = "down") -> bool:
    """Scout helper: find a nearby STEP floor-change in `direction` and take it.

    The counterpart to `_try_use_object` for the STEP floor-changes it skips, closing two
    real gaps. DOWN: a scout that climbs UP a ladder gets no learned link back down and
    can't stumble down the hole by accident (we blacklist a fallen-through hole as a
    hazard, so the pather routes AROUND it forever), so it strands itself up top. UP: a
    scout in a room whose only way out is an up-staircase (a STEP object, so
    find_use_object ignores it) would otherwise never leave. Here we deliberately hunt the
    matching-direction object — hole/trapdoor/down-stairs for down, up-stairs for up — and
    step onto it. A relocation is recorded as a source->destination link the colony can
    route directly next time.

    Two shapes are handled: the object is NEARBY (walk to an adjacent launch tile and step
    onto it), or we're STANDING ON it (find_descent skips distance-0). The latter matters
    because a ladder often lands you right on top of the down-hole, and being *placed* on
    a floor-change doesn't trigger it — only *stepping* onto it does — so we step off to a
    neighbour and back on. Returns True if we attempted a floor-change.
    """
    from .nav import find_descent, is_walkable

    colony = session.colony
    if colony is None:
        return False
    pos = session.state.position
    if pos is None:
        return False
    now = time.monotonic()
    verb = "descending" if direction == "down" else "ascending"

    skip = _live_tried(tried, now)
    found = find_descent(session.state, item_flags, colony.traversal,
                         skip=skip, max_dist=max_dist, direction=direction)
    if found is not None:
        source, item_id, category, launch, direction = found
        tried[source] = now + _TRIED_TTL   # attempted — hold off, but retry after the TTL
        if (pos.x, pos.y) != (launch[0], launch[1]):
            await _walk_local(session, item_flags, launch[0], launch[1],
                           max_steps=40, plan_radius=_PLAN_RADIUS)
            pos = session.state.position
            if pos is None or (pos.x, pos.y) != (launch[0], launch[1]):
                return True  # couldn't reach it; counts as tried and move on
        session.status = f"{verb} ({category})"
        landing = await _perform_traversal(session, source, direction, category, item_id)
        if landing is not None and landing.z != pos.z:
            # find_descent returns EITHER action (a STEP-down hole or a USE-down grate can
            # both match `direction` — see there), so check which this one actually was.
            found_kind = colony.traversal.kind(category)
            found_action = "step" if found_kind is not None and found_kind.action == "step" else "use"
            colony.report_link(source, (landing.x, landing.y, landing.z), action=found_action)
            session.log_event("info", "traversal",
                              f"{verb} via {category} at {source} -> "
                              f"({landing.x},{landing.y},{landing.z}); recorded shortcut")
        return True

    # Nothing nearby to walk to — but we might be STANDING on the floor-change (which
    # find_descent skips at distance 0). Step off to any walkable neighbour, then back
    # on, so the *step* onto it triggers the move. BUT respect the cooldown: if THIS tile
    # is a floor-change we've recently tried, don't re-trigger it — that's exactly the
    # dead-end bounce (come up a hole, land on it, immediately fall back down). Let the
    # caller move on to the room's other exits.
    here_tile = (pos.x, pos.y, pos.z)
    if here_tile in skip:
        return False
    here = session.state.tiles.get(here_tile)
    hit = colony.traversal.classify(tile_ids(here)) if here else None
    if hit is not None:
        kind = colony.traversal.kind(hit[0])
        if kind is not None and kind.action == "step" and kind.direction == direction:
            tried[here_tile] = now + _TRIED_TTL   # record so we don't immediately re-do it
            for name, (dx, dy) in DIRECTION_DELTAS.items():
                if not is_walkable(session.state, item_flags, pos.x + dx, pos.y + dy, pos.z):
                    continue
                session.status = f"{verb} (step off/on)"
                if not await _raw_step(session, name):        # off the tile
                    return True
                reverse = {"north": "south", "south": "north",
                           "east": "west", "west": "east"}[name]
                await _raw_step(session, reverse)             # back onto it -> triggers
                landing = session.state.position
                if landing is not None and landing.z != pos.z:
                    # Gated above on kind.action == "step" (only a STEP object re-triggers
                    # from stepping off and back on).
                    colony.report_link((pos.x, pos.y, pos.z),
                                       (landing.x, landing.y, landing.z), action="step")
                    session.log_event("info", "traversal",
                                      f"{verb} off/on at {(pos.x, pos.y, pos.z)} -> "
                                      f"({landing.x},{landing.y},{landing.z})")
                return True
    return False


@log_call
async def travel(session: GameSession, item_flags: ItemFlags,
                 goal_x: int, goal_y: int, goal_z: int, max_hops: int = 40,
                 reach: int = 0) -> bool:
    """Travel to (goal_x, goal_y, goal_z) across the whole world, using shortcuts.

    Plans a route with `find_shared_route` over the colony's shared walkable map + learned
    links (stairs/holes/teleports), then executes it hop by hop:
      - on-foot stretches — INCLUDING any door along the way, which `_follow_shared_route`
        opens inline since using one never relocates you — are walked with
        `_follow_shared_route` (falling back to `_walk_local`'s local feel-your-way only
        when the shared map can't connect us — e.g. the last stretch into somewhere
        nobody's been),
      - a RELOCATING shortcut hop (stairs/hole/teleporter/ladder/grate — anything that
        actually moves you, unlike a door) is traversed by stepping onto the source
        tile, which the normal walker avoids — so we do it with a raw step and confirm
        we landed where the link said.
    We re-plan after every such shortcut because teleporting shifts our whole frame of
    reference. Returns True on arrival. (NPC/boat travel will add another hop type
    here once the conversation protocol exists.)

    `reach` accepts arrival within that many tiles — for walking up to an OBJECT
    (a locker, a pile under a blocking item) rather than onto a tile.
    """
    from .nav import find_shared_route, within_reach, is_walkable

    colony = session.colony
    if colony is None:
        log.warning("travel needs a colony (shared map + links)")
        return False

    goal = (goal_x, goal_y, goal_z)
    for _ in range(max_hops):
        pos = session.state.position
        if pos is None or session.closed.is_set() or session.stop_event.is_set():
            return False
        here = (pos.x, pos.y, pos.z)
        if within_reach(here, goal, reach):
            log.info("travel: arrived at %s", pos)
            return True

        # "A teleport is a hazard to a wanderer but a highway to a traveller" — and this
        # is the router that's supposed to drive the highway. But `report_link` files every
        # link SOURCE into the hazard set, and `find_shared_route` rejects an avoided tile BEFORE
        # it consults the links table, so handing it the raw hazards made it unable to
        # traverse ANY shortcut: every goal reachable only via a ladder/hole/teleport came
        # back "no route". Meanwhile `reachable_frontier` floods across links freely, so it
        # kept proposing cross-floor frontiers this router structurally could not reach —
        # the bot chased the same unreachable tile forever. Subtract the links we mean to
        # use, so the two agree on what "reachable" means.
        links = colony.get_links()
        route = find_shared_route(colony.get_walkable(), links, here, goal,
                           reach=reach, avoid=colony.get_hazards() - set(links),
                           costs=colony.get_walk_costs(),
                           unconfirmed_crossings=colony.get_unconfirmed_crossings(),
                           step_links=colony.get_step_links())
        if route is None:
            # Nothing in the shared map connects us — normal when the goal is somewhere
            # nobody has been, or when we're standing in a not-yet-connected pocket.
            # Feel our way locally, then LOOP: walking reveals tiles, and the map we just
            # grew may well contain the route that didn't exist a moment ago. (Observed
            # exactly this: no route Temple->Depot, one local walk, and the route appeared
            # — 68 steps. Returning here instead of re-planning threw that away.)
            log.info("travel: no route from %s to (%d,%d,%d) in the known world; "
                     "trying local navigation", here, goal_x, goal_y, goal_z)
            await _walk_local(session, item_flags, goal_x, goal_y)
            colony.contribute_tiles(session.state)   # publish what we just revealed
            p = session.state.position
            if p is None:
                return False
            if within_reach((p.x, p.y, p.z), goal, reach):
                return True
            if (p.x, p.y, p.z) == here:
                # Local navigation couldn't move us either: genuinely stuck, not myopic.
                return False
            continue

        # Find the first RELOCATING shortcut hop; walk on foot up to the tile it starts
        # from, then take it. A door crossing also shows up with is_teleport=True (see
        # find_shared_route's docstring — USE-type links get a "use it" edge), but
        # `links[door] == door` (contribute_tiles' self-link seeding: using one never
        # relocates you) is exactly what tells it apart from a real one — so it's
        # filtered out here and left for `_follow_shared_route` to open inline as it
        # walks past. If there's no REAL shortcut left, the whole route is on-foot.
        first_teleport = next((i for i, step in enumerate(route)
                               if step[1] and links.get(step[2]) != step[2]), None)

        if first_teleport is None:
            # Pure on-foot route (same floor / same region). Follow the route we just
            # planned over the shared map — NOT `_walk_local`, which would discard it and
            # re-guess from this bot's private, ~8-tile view (see `_follow_shared_route`).
            # `route` IS `_follow_shared_route`'s own answer too (no shortcut on it, same
            # start/goal) — hand it over as `seed_route` instead of making it re-plan the
            # identical thing on its first step.
            if await _follow_shared_route(session, item_flags, goal_x, goal_y, goal_z,
                                  reach=reach, seed_route=route):
                continue
            # The shared route ran out (unexplored last stretch, or we got pushed off
            # it). Feel the rest of the way locally, then re-plan: what we just walked
            # over may complete the map.
            await _walk_local(session, item_flags, goal_x, goal_y)
            colony.contribute_tiles(session.state)
            p = session.state.position
            if p is None:
                return False
            if (p.x, p.y, p.z) == here:
                return False        # local navigation is stuck too; don't spin
            continue

        # The shortcut is step `first_teleport`; the tile we must be standing on to
        # take it is the landing of the previous step (or `here` if it's first).
        direction = route[first_teleport][0]
        launch = route[first_teleport - 1][2] if first_teleport > 0 else here
        landing = route[first_teleport][2]

        if here != launch:
            # Walk on foot to the launch tile (same floor as us), following the shared
            # route; fall back to local navigation only if that can't do it. The on-foot
            # PREFIX of `route` (everything before the shortcut) already IS this leg —
            # seed it rather than re-planning the same steps.
            if not await _follow_shared_route(session, item_flags, launch[0], launch[1],
                                      launch[2], seed_route=route[:first_teleport]):
                await _walk_local(session, item_flags, launch[0], launch[1])
            p = session.state.position
            if p is None or (p.x, p.y, p.z) != launch:
                log.info("travel: couldn't reach shortcut launch %s; aborting", launch)
                return False

        # Deliberately traverse the shortcut tile. `_take_shortcut` USES it if it's a
        # ladder/grate and steps onto it if it's a teleporter/stairs/open hole — never
        # a door, which `first_teleport` above already filtered out.
        if direction == "__here__":
            # find_shared_route's sentinel (see its docstring): `launch` IS the link's
            # source tile — we started standing on it, so there's no adjacency to walk.
            # _take_shortcut/_perform_traversal only consult `direction` as a cardinal for
            # a USE object's rare "secretly a STEP" fallback, so any walkable neighbour is
            # a fine placeholder (same trick as `_try_use_underfoot`).
            source = launch
            cardinal = next((name for name, (dx, dy) in DIRECTION_DELTAS.items()
                             if is_walkable(session.state, item_flags,
                                            source[0] + dx, source[1] + dy, source[2])),
                            "north")
        else:
            source = (launch[0] + DIRECTION_DELTAS[direction][0],
                      launch[1] + DIRECTION_DELTAS[direction][1], launch[2])
            cardinal = direction
        log.info("travel: taking shortcut %s -> %s (via %s)", source, landing, direction)
        await _take_shortcut(session, source, cardinal)
        after = session.state.position
        if after is None:
            return False
        after_t = (after.x, after.y, after.z)

        if after_t == landing:
            colony.confirm_link(source, landing)          # trusted from now on
            log.info("travel: shortcut landed us at %s", after)
            continue

        # Did stepping on the source relocate us at all (floor change, or a real
        # jump)? If so it IS a shortcut — the destination was just mis-recorded, so
        # correct it and re-plan with the truth (self-healing).
        if after.z != source[2] or abs(after.x - source[0]) + abs(after.y - source[1]) > 2:
            colony.confirm_link(source, after_t)
            log.info("travel: shortcut %s really leads to %s (was %s); corrected, re-planning",
                     source, after, landing)
            continue

        # It didn't take us anywhere — the source was never a real shortcut (an
        # imprecise/false record). Prune it so nothing routes through it again.
        colony.mark_bad_link(source)
        session.log_event("warning", "shortcut",
                          f"recorded shortcut at {source} did nothing; pruned it")
        # continue: re-plan without the bad link

    log.warning("travel: gave up after %d hops at %s", max_hops, session.state.position)
    return False


async def travel_session(host: str, login_port: int, account: str, password: str,
                         character_name: str | None, goal_x: int, goal_y: int, goal_z: int,
                         item_flags: ItemFlags, colony, rsa_n: int = wire.OTSERV_RSA_N) -> None:
    """Log in and travel to (goal_x, goal_y, goal_z) using shortcut routes."""
    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, 0)
    session.colony = colony

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)
        session.status = "travelling"
        log.info("travel: from %s to (%d,%d,%d)", session.state.position, goal_x, goal_y, goal_z)
        await travel(session, item_flags, goal_x, goal_y, goal_z)

    await _run_in_world(session, action)


def _scan_exits(session: GameSession, item_flags: ItemFlags, radius: int = 25) -> list:
    """Diagnostic: every recognised exit object the bot can see nearby, and WHY each is or
    isn't usable. Turns 'the scout won't try the door' into a concrete, readable list —
    was the door tile even in our map? does it have a walkable tile to use it from? is it
    on the tried cooldown? Returns a list of dicts, nearest first.
    """
    from .nav import is_walkable
    st = session.state
    pos = st.position
    if pos is None or session.colony is None:
        return []
    reg = session.colony.traversal
    px, py, z = pos.x, pos.y, pos.z
    out = []
    for (x, y, tz), items in list(st.tiles.items()):
        if tz != z:
            continue
        d = abs(x - px) + abs(y - py)
        if d == 0 or d > radius:
            continue
        hit = reg.classify(tile_ids(items))
        if hit is None:
            continue
        category, item_id = hit
        launch = None
        for name, (dx, dy) in DIRECTION_DELTAS.items():
            if is_walkable(st, item_flags, x + dx, y + dy, z):
                launch = (x + dx, y + dy)
                break
        out.append({"cat": category, "id": item_id, "pos": (x, y, z), "dist": d,
                    "launch": launch})
    out.sort(key=lambda r: r["dist"])
    return out


def _pick_descent(session: GameSession, recent: dict, cooldown: float = 20.0):
    """Choose a discovered shortcut on our floor to drop to a new area.

    Prefers links whose destination is on a *different* floor (to spread the scout
    downward/upward), skips ones used very recently so we don't bounce up and down the
    same staircase, and — for dispersal — avoids landings another bot is already sitting
    on. All scouts share one colony link graph, so without that last check they route to
    the SAME destination and pile onto one tile; here we'd rather stay and explore than
    add to a pile (returns None if every reachable exit lands on a crowded tile).
    """
    if session.colony is None or session.state.position is None:
        return None
    z = session.state.position.z
    now = asyncio.get_event_loop().time()
    candidates = [(s, d) for s, d in session.colony.get_links().items()
                  if s[2] == z and now - recent.get(s, -1e9) > cooldown]
    if not candidates:
        return None

    # Where every OTHER bot is right now, so we can tell which landings are taken.
    others = [p for n, p in session.colony.bot_positions().items() if n != session.bot_name]

    def crowded(dest: tuple[int, int, int]) -> bool:
        return any(o[2] == dest[2]
                   and abs(o[0] - dest[0]) + abs(o[1] - dest[1]) <= _DISPERSE_RADIUS
                   for o in others)

    # Sort by (not-crowded first, then different-floor first). If the very best option
    # still lands somewhere crowded, decline — the scout will explore/escape here this
    # round instead of stacking onto another bot, and try again once the landing clears.
    candidates.sort(key=lambda sd: (crowded(sd[1]), 0 if sd[1][2] != z else 1))
    best = candidates[0]
    if crowded(best[1]):
        if session.recorder is not None:
            session.recorder.decision("declined_descent", reason="crowded", dest=list(best[1]))
        return None
    return best


async def scout(session: GameSession, item_flags: ItemFlags,
                duration: float | None = None,
                center: tuple[int, int] | None = None,
                radius: int | None = None) -> None:
    """Map far and wide: expand the frontier, then descend to map new floors.

    Unlike the home-bound wanderers, a scout (a) always heads for the nearest
    UNEXPLORED edge (`find_frontier`) so the mapped region grows outward on
    purpose, and (b) once a floor's reachable area is fully mapped, deliberately
    takes a discovered shortcut to a new floor/region and keeps going. Everything
    it sees flows into the colony's shared map + link graph, which is what feeds
    the router real destinations.

    `center` + `radius` turn that into a SURVEY: only frontiers inside the box are
    considered, and the scout won't descend out of it. Unbounded scouting is right for
    "find me new world", but it's the wrong tool for "make this town routable" — an
    unbounded scout beelines for the nearest global frontier, which is always the edge
    of the known region, so it leaves town almost immediately and the map fills in a
    thin line outward instead of a connected area. That's exactly why routing inside
    Thais kept failing: the depot's approach never got mapped, and whether it did was
    down to luck. Bound the scout to a region and it fills the region in.
    """
    import random

    loop = asyncio.get_event_loop()
    end = loop.time() + duration if duration else float("inf")
    recent_descents: dict = {}
    tried_objects: dict = {}            # exit object tile -> when we'll retry it (TTL)
    tried_descents: dict = {}           # floor-change tile -> when we'll retry it (TTL)
    stale_frontiers: set = set()        # (x,y,z) frontiers we've given up reaching
    frontier_tries: dict = {}           # frontier -> failed travel attempts; retire after
                                        # a few so a leak into a sealed room can't lure us
                                        # forever. Cleared with stale_frontiers.
    last_floor = None
    scout_stuck = 0                     # consecutive rounds without moving
    boxed_logged = False                # so we log a boxed-in scout only once
    last_exit_report = 0.0              # throttle the "what exits can I see" diagnostic
    # Loop-breaker: the honest measure of "am I actually doing my job" is whether I'm
    # REVEALING NEW MAP. If `seen` hasn't grown in a while, I'm looping in explored
    # territory — so give up, go home to the temple, and start fresh from a known-
    # connected spot rather than thrash where I am. This is a calm circuit-breaker, not a
    # fix for the underlying loop; it just keeps a stuck bot from being miserable to watch.
    last_seen_count = len(session.state.seen)
    last_growth = loop.time()

    while loop.time() < end and not session.closed.is_set() and not session.stop_event.is_set():
        if _check_watchdog(session):   # dead connection -> abort
            break
        _prune_blocked_tiles(session)  # expire transient (creature/bot) blocks
        pos = session.state.position
        if pos is None:
            break

        # -- loop-breaker: no new map for a while => go home and reset ---------------
        now = loop.time()
        if len(session.state.seen) > last_seen_count:
            last_seen_count = len(session.state.seen)
            last_growth = now
            # Real progress: we revealed new map, so any frontier we'd retired as
            # "unreachable" deserves reconsidering from here. This is the RIGHT trigger to
            # forget the frontier blacklist — tying it to actual map growth, NOT to every
            # step (which is what let a scout circle a room forever: it kept moving toward
            # a frontier it never resolved, and each twitch wiped the retirement that would
            # otherwise have exhausted the frontier and dropped it into the exit hunt).
            stale_frontiers.clear(); frontier_tries.clear()
        elif center is None and now - last_growth > _LOOP_BREAK_SECONDS:
            home = getattr(session.colony, "stash", None) if session.colony else None
            session.log_event("warning", "loop-break",
                              f"no new map for {_LOOP_BREAK_SECONDS:.0f}s at "
                              f"({pos.x},{pos.y},{pos.z}); returning to temple, fresh start")
            # Wipe the local stuck-state so we re-evaluate everything from scratch. The
            # SHARED map is untouched — that's the point: we head home, then pick a fresh
            # reachable frontier from what the swarm now knows.
            #
            # `stale_frontiers` deliberately SURVIVES. It isn't stuck-state, it's earned
            # knowledge: each entry cost us _FRONTIER_MAX_TRIES real attempts to prove
            # unreachable. Clearing it here (as this did originally) re-armed the very trap
            # that caused the loop — the bot retired a frontier after 3 tries, ran 60s
            # without new map, loop-broke, forgot the retirement, and walked straight back.
            # That's the repeat we watched in the decision trace. Genuine region changes are
            # already handled by the floor-change clear below.
            frontier_tries.clear()
            session.state.blocked_tiles.clear(); session.state.blocked_expiry.clear()
            scout_stuck = 0
            last_growth = now            # don't immediately re-trigger
            session.expect_movement = True
            if home is not None:
                session.decide("loop-break", f"no new map {_LOOP_BREAK_SECONDS:.0f}s -> temple")
                if not await navigate_to(session, item_flags, home[0], home[1], home[2],
                                         deadline=now + _SCOUT_NAV_BUDGET, only=None):
                    # Can't even route home (we're in a disconnected pocket). Idling beats
                    # thrashing — sit quietly until the watchdog/relog recovers us.
                    session.decide("loop-break", "home unreachable, idling")
                    session.expect_movement = False
                    await asyncio.sleep(5.0)
            else:
                session.decide("loop-break", "no home set, idling")
                session.expect_movement = False
                await asyncio.sleep(5.0)
            continue

        # Forget our blacklists whenever we change floor/region: frontiers that were
        # unreachable here may be reachable from the new area, and vice versa.
        if pos.z != last_floor:
            stale_frontiers.clear(); frontier_tries.clear()
            last_floor = pos.z
            scout_stuck = 0
            boxed_logged = False

        round_start = (pos.x, pos.y, pos.z)

        # Self-defence, every round: if something is next to us, hit it. Non-blocking —
        # it just keeps the server-side attack target current, so we go on scouting while
        # the melee happens. (`_fight_free` below is a different thing: an escape tool for
        # when monsters have us fully boxed in.)
        await _engage_adjacent(session)

        # On-sight object use: if a ladder/hole/grate/door is right next to us, try
        # it once (proactive detection). This is the thing that actually exercises
        # the USE executor during normal exploration — climb a ladder we walked up
        # to, open a door blocking the way — instead of only as a boxed-in fallback.
        # Each object is tried at most once (tried_objects), so it can't thrash.
        if await _try_use_object(session, item_flags, tried_objects,
                                 max_dist=_ONSIGHT_RADIUS):
            await asyncio.sleep(0.2)
            after = session.state.position
            if after is None or (after.x, after.y, after.z) == round_start:
                scout_stuck += 1
            else:
                scout_stuck = 0
                boxed_logged = False
                stale_frontiers.clear(); frontier_tries.clear()
            continue

        # Boxed in — we've gone several rounds without moving. Back off so the
        # descent probe's full-map route search runs at most ~once/few-seconds, and
        # DROP the frontier blacklist: the watchdog periodically clears the stale
        # blocked tiles that walled us in, so frontiers we couldn't reach a moment
        # ago may be reachable now. Crucially we still run the full escape chain
        # below (frontier + descent + use-object) — a boxed-in scout escapes by
        # travelling out, so we must keep trying to.
        if scout_stuck >= 8:
            if not boxed_logged:
                nblk = len(session.state.blocked_tiles)
                nlinks = (sum(1 for s in session.colony.get_links() if s[2] == pos.z)
                          if session.colony is not None else 0)
                session.log_event("warning", "boxed-in",
                                  f"scout can't move from {round_start}; {nblk} blocked "
                                  f"tiles here, {nlinks} known exits on this floor — "
                                  f"trying to break out")
                boxed_logged = True
            session.decide("breaking out",
                           f"{len(_adjacent_monsters(session))} monsters, "
                           f"{len(session.state.blocked_tiles)} blocks")
            stale_frontiers.clear(); frontier_tries.clear()
            # If MONSTERS are what's boxing us in, no amount of re-stepping or re-routing
            # helps — the walkable tiles are occupied by living creatures. Melee our way
            # out first (only when stuck, so fleeing was never an option). If it opens a
            # path, reset and re-plan from here.
            if _adjacent_monsters(session) and await _fight_free(session, item_flags):
                scout_stuck = 0
                boxed_logged = False
                await asyncio.sleep(0.1)
                continue
            # FIRST try to physically step out, bypassing the (largely stale) blocked-
            # tile map that's pinning us — this is what un-sticks stacked bots that the
            # planner can't route because it thinks every neighbour is a wall. If it
            # works we've moved, so reset the stuck counter and re-plan from the new spot.
            if await _escape_step(session, item_flags):
                scout_stuck = 0
                boxed_logged = False
                await asyncio.sleep(0.1)
                continue
            # Couldn't force a step (genuinely walled in for now) — back off so the
            # escape/descent probes below run at most ~once/few-seconds, then fall
            # through to the planner-based escape chain (a link out may have appeared).
            await asyncio.sleep(3.0)

        # Features A + B + C — SHARED, REACHABILITY-AWARE frontier. Ask the colony for the
        # nearest frontier we can actually reach by walking + known ladders/stairs (it
        # floods the shared map from us over links). This is the whole fix for the wasted
        # balcony/island trips:
        #   A — a tile another bot already revealed isn't a frontier for anyone;
        #   B — a finished region has no frontiers, and a walled-off/disconnected room is
        #       never reached by the flood, so neither is ever targeted;
        #   C — a frontier down a ladder comes back as a target `travel` routes to DOWN the
        #       ladder, instead of the old doomed same-floor walk to a disconnected tile.
        # The selector must exclude exactly what the router will (see reachable_frontier's
        # `avoid`), or it hands us frontiers `travel` can't reach and we loop on them. Note
        # blocked_tiles already absorbs the hazard set (see _report_to_colony), and BOTH
        # are minus the links — those are the shortcuts we intend to ride, not walls.
        frontier = None
        if session.colony is not None:
            nav_avoid = ((session.colony.get_hazards() | set(session.state.blocked_tiles))
                         - set(session.colony.get_links()))
            frontier = session.colony.reachable_frontier(
                pos.x, pos.y, pos.z, skip=stale_frontiers,
                center=center, radius=radius, avoid=nav_avoid)
        if frontier is not None:
            session.decide("surveying" if center else "scouting",
                           f"-> frontier {frontier}")
            arrived = await navigate_to(session, item_flags, frontier[0], frontier[1],
                                        frontier[2], deadline=loop.time() + _SCOUT_NAV_BUDGET,
                                        only=None)
            if arrived:
                frontier_tries.pop(frontier, None)   # reached it; its unseen edge reveals
            else:
                # Couldn't get there — a leak into a sealed room, or a link we can't
                # execute. Retire it durably after a few tries so it can't lure us back
                # (stale_frontiers persists until we change floor).
                frontier_tries[frontier] = frontier_tries.get(frontier, 0) + 1
                if frontier_tries[frontier] >= _FRONTIER_MAX_TRIES:
                    stale_frontiers.add(frontier)
        elif center is not None:
            # SURVEY MODE and no frontier in the box. Two very different reasons:
            if abs(pos.x - center[0]) + abs(pos.y - center[1]) > radius:
                # We're not even in the area yet — a bot resumes wherever it logged out,
                # which can be hundreds of tiles away. find_frontier is bounded to the
                # box, so from out here it always says None and we'd idle forever
                # "surveying" an area we never visit. Go there first.
                session.decide("to survey area", f"outside box -> {center}")
                session.expect_movement = True
                log.info("scout: outside survey area; travelling to %s", center)
                await navigate_to(session, item_flags, center[0], center[1], pos.z,
                                  deadline=loop.time() + _SCOUT_NAV_BUDGET, only=None)
            else:
                # Genuinely done. Descending or hunting an exit is what we must NOT do —
                # that's how a scout leaves town and stops contributing the connected
                # coverage the router needs. Idle; the caller ends us by `duration`.
                session.decide("survey complete", "box fully mapped, idling")
                session.expect_movement = False   # stillness is intended: don't relog
                session._report_to_colony()
                await asyncio.sleep(2.0)
        else:
            # Reachable land here is mapped (or walled off) — find a way OFF this floor.
            # Order matters, and it MUST prefer LOCAL exits over map-wide ones:
            #   1. a nearby use-object (door/ladder/grate) — open/use it right here;
            #   2. a nearby down-hole, then a nearby up-staircase — step onto it;
            #   3. ONLY if nothing local exists, travel to a learned link elsewhere.
            # The old order tried the learned link (2) BEFORE the local hole/stairs, and
            # that trapped scouts: once the colony has learned ANY link on this floor,
            # `_pick_descent` always returns one, so a scout walled in a small room spent
            # forever trying to `travel` to a far link it couldn't reach — and never tried
            # the hole or staircase sitting right there in the room (they were unreachable
            # `elif` branches). Local first means the room's own exit is always attempted.
            # Runs even when boxed in (that's how a stuck scout travels out); the backoff
            # above paces the searches.
            #
            # Diagnostic (throttled): whenever we're hunting an exit, log every recognised
            # exit object we can see and why it's usable or not — the fast way to answer
            # "why won't it try the door" (not in our map? no walkable tile to use it
            # from?). Cheap and bounded to _USE_OBJECT_RADIUS.
            if now - last_exit_report > 5.0:
                last_exit_report = now
                seen_exits = _scan_exits(session, item_flags, radius=_USE_OBJECT_RADIUS)
                if seen_exits:
                    summary = "; ".join(
                        f"{e['cat']}#{e['id']}@{e['pos'][0]},{e['pos'][1]} d{e['dist']} "
                        f"launch={'yes' if e['launch'] else 'NONE'}"
                        for e in seen_exits[:8])
                    session.log_event("info", "exits",
                                      f"can see {len(seen_exits)} exit(s): {summary}")
                else:
                    session.log_event("info", "exits", "no recognised exit in view")

            if await _try_use_object(session, item_flags, tried_objects,
                                     max_dist=_USE_OBJECT_RADIUS):
                pass  # attempted a nearby ladder/hole/grate/door (status set inside)
            elif await _try_change_floor(session, item_flags, tried_descents,
                                    max_dist=_DESCENT_RADIUS):
                # A nearby way DOWN we recognise: a hole / trapdoor / down-stairs we step
                # onto, or a sewer grate we use. Tried before any map-wide link so a scout
                # actually uses the exit in its own room instead of trekking off to a
                # distant one it may not even be able to reach.
                pass  # attempted a local descent (status set inside)
            elif await _try_change_floor(session, item_flags, tried_descents,
                                    max_dist=_DESCENT_RADIUS, direction="up"):
                # A nearby UP-staircase. Down is preferred (that's how a scout ranges into
                # new territory), but a room whose only exit is up-stairs would otherwise
                # trap it. Up-ladders are USE objects already covered above; this catches
                # the STEP up-stairs find_use_object ignores.
                pass  # attempted a local ascent (status set inside)
            elif (link := _pick_descent(session, recent_descents)) is not None:
                # No LOCAL exit worked — fall back to a learned link elsewhere on the
                # floor and travel to it. This is the efficient dispersal path when the
                # local area is genuinely mapped out; it's just no longer allowed to
                # pre-empt the local exits above.
                src, dst = link
                recent_descents[src] = loop.time()
                session.decide("descending", f"{src} -> {dst}")
                log.info("scout: floor mapped; taking shortcut %s -> %s to a new area", src, dst)
                await navigate_to(session, item_flags, dst[0], dst[1], dst[2],
                                  deadline=loop.time() + _SCOUT_NAV_BUDGET, only=None)
            else:
                # No known way off this floor yet — hop somewhere far to bump into
                # (and thus discover) an undiscovered staircase/teleport.
                session.decide("seeking exit", "no known way off floor")
                await _walk_local(session, item_flags,
                               pos.x + random.randint(-30, 30),
                               pos.y + random.randint(-30, 30),
                               max_steps=40, plan_radius=_PLAN_RADIUS)

        # Track whether this whole round actually moved us, to detect a boxed-in scout.
        # NOTE: we deliberately do NOT clear the frontier blacklist here. Mere movement
        # isn't progress — a scout circling a room moves every round yet resolves nothing,
        # and wiping the retirement on each step is exactly what kept it from ever
        # exhausting an unreachable frontier and reaching the exit hunt. The blacklist is
        # cleared on real map growth (see the loop-breaker above) and on floor change.
        after = session.state.position
        if after is not None and (after.x, after.y, after.z) == round_start:
            scout_stuck += 1
        else:
            scout_stuck = 0
            boxed_logged = False
            # A floor change means the old floor's frontier judgements no longer apply —
            # re-evaluate its frontiers next time we're here.
            #
            # We do NOT clear tried_objects/tried_descents on floor change any more — the
            # 45s TTL governs re-tries instead. Clearing them here caused a dead-end
            # BOUNCE: a scout would take a down-hole into a small room, find its only way
            # out is back up, return — and the floor change wiped the "tried" memory, so it
            # immediately re-took the SAME hole and never advanced to the room's OTHER
            # exits (the up-staircase, the door). Keeping the cooldown across the round
            # trip lets it try the hole, then the stairs, then the door in turn, while the
            # TTL still lets it revisit the hole later in case something changed.
            if after is not None and after.z != round_start[2]:
                stale_frontiers.clear(); frontier_tries.clear()
        await asyncio.sleep(0.2)
    session.status = "idle"
    session._report_to_colony()


def _attach_recorder(session: GameSession, record_mode: str) -> None:
    """Give the session a recorder unless recording is off. Called after connect, so
    session.bot_name/character are resolved. Failure to record must never take the bot
    down, so we swallow errors (a missing recordings dir shouldn't kill a scout)."""
    if not record_mode or record_mode == "none":
        return
    try:
        from .recorder import SessionRecorder
        session.recorder = SessionRecorder(session.bot_name, session.character, mode=record_mode)
    except Exception as err:  # recording is best-effort; never fail the bot over it
        log.warning("recorder: could not start for %s: %s", session.bot_name, err)


async def scout_session(host: str, login_port: int, account: str, password: str,
                        character_name: str | None, item_flags: ItemFlags, colony,
                        duration: float | None = None, rsa_n: int = wire.OTSERV_RSA_N,
                        stop_event: asyncio.Event | None = None,
                        record_mode: str = "none",
                        center: tuple[int, int] | None = None,
                        radius: int | None = None) -> None:
    """Log a character in and run the scout behaviour, reporting to the colony.

    Pass `center` + `radius` to SURVEY that area instead of ranging outward — see
    `scout`. That's what makes a region (a town, a hunting ground) routable.
    """
    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, 0)
    session.colony = colony
    session.role = "scout"
    if stop_event is not None:
        session.stop_event = stop_event
    _attach_recorder(session, record_mode)

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)
        log.info("scout: starting from %s", session.state.position)
        await scout(session, item_flags, duration, center=center, radius=radius)

    try:
        await _run_in_world(session, action)
    finally:
        if session.recorder is not None:
            session.recorder.close()


@log_call
async def navigate_to(session: GameSession, item_flags: ItemFlags,
                      x: int, y: int, z: int, deadline: float | None = None,
                      only: set[str] | None = _DOOR_CATEGORIES) -> bool:
    """Directed navigation to an EXACT tile — the ONE place "get me to a known point"
    lives, used by the nav tests AND (see `scout`) by any bot heading to a destination
    it already picked (a frontier, a learned shortcut, home). Scout's OWN job stays
    picking where to go; this owns how to get there, so a fix here (opening a stuck
    door, recovering a floor mismatch) benefits every caller instead of needing a
    second, independently-drifting copy.

    Routes over the colony's shared map (`travel`) with a local feel-your-way fallback,
    retrying until we're standing on (x, y, z) or `deadline` (an event-loop time) passes.
    Returns True iff we arrived exactly.

    `only` restricts which USE-objects a stalled walk may clear — the default
    (`_DOOR_CATEGORIES`) is right for the nav tests and anything that must NOT be
    knocked off its route by ducking into a ladder/grate. Pass `only=None` for a caller
    (scout) that's happy to be relocated by ANY exit it stumbles on while trying to
    make progress — that's exploration, not a wrong turn.

    WRONG FLOOR, STANDING ON A USE-OBJECT. `_try_change_floor` hunts nearby floor-changes
    and re-triggers a STEP-type one we're already standing on (a step fires on the walk
    itself), but a USE-type one (a grate is the case that surfaced this) does nothing when
    merely stood on — the only way to fire it is a deliberate use. If a destination sits
    exactly on a grate/ladder, `_try_change_floor` alone leaves us parked there forever;
    `_try_use_underfoot` is the fallback that notices and uses it directly.

    BLOCKED/DETOURED BY A CREATURE. A door or a wrong floor aren't the only things that
    can stand between here and there — a monster (or another bot/player) parked in a
    single-tile corridor does too, and unlike terrain, an occupied tile is never durably
    blocked (see `_walk_local`'s "rejected" handling), so re-planning around it can spin
    forever if it's the ONLY route. This loop tracks net progress round to round; once
    we've gone genuinely nowhere for `_NAV_FIGHT_THRESHOLD` rounds, it escalates the same
    way `scout` always has for its own travel (see `_fight_free`/`_escape_step`) — melee
    through if a monster is actually adjacent, else try a raw step past what might just be
    a stale block from a bot/player that has since moved on. Generalized here so it isn't
    scout-only: any caller heading to a known destination gets it.

    Every planner call underneath this one (`travel`'s own `find_shared_route`,
    `_follow_shared_route`'s, `_walk_local`'s `find_nearest_step_toward`) automatically
    records to whatever `tracing.EventRecorder` is bound for the current context (see
    `tracing.bind_recorder`) — no parameter needed here, or anywhere in the call chain
    below; that's the whole point of making it ambient. Costs nothing when none is bound.
    """
    loop = asyncio.get_event_loop()
    door_tried: dict = {}    # door tile -> retry time, so we don't hammer one that won't open
    floor_tried: dict = {}   # floor-change tile -> retry time (same idea, for stairs/holes)
    nav_stuck = 0            # consecutive rounds with no net movement — see _NAV_FIGHT_THRESHOLD
    while True:
        pos = session.state.position
        if pos is not None and (pos.x, pos.y, pos.z) == (x, y, z):
            return True
        if session.closed.is_set() or session.stop_event.is_set():
            return False
        if deadline is not None and loop.time() >= deadline:
            return False
        round_start = (pos.x, pos.y, pos.z) if pos is not None else None
        before = round_start

        # Blocked or badly detoured for a while — check whether a MONSTER is the reason
        # before doing anything else this round, mirroring scout's own priority order
        # (fight, THEN the normal navigation chain). `_fight_free` itself is a no-op
        # (returns False immediately) if nothing is actually adjacent, so this is safe to
        # call speculatively. Cap the bout by whatever's left of our own deadline, if any,
        # so a fight can't run a time-boxed caller (a nav test) past what it promised.
        if nav_stuck >= _NAV_FIGHT_THRESHOLD:
            remaining = (deadline - loop.time()) if deadline is not None else None
            if await _fight_free(session, item_flags, max_seconds=remaining):
                nav_stuck = 0
                continue
            # Not a monster (or the fight timed out) — maybe it's a STALE block: another
            # bot/player that was in the way has since moved, but our blocked-tile map
            # still thinks the tile is a wall. A raw step bypasses that bookkeeping.
            if await _escape_step(session, item_flags):
                nav_stuck = 0
                continue

        # Prefer the shared-map router (it uses learned links and crosses floors when the
        # colony knows a connecting route).
        if (session.colony is not None
                and await travel(session, item_flags, x, y, z)):
            nav_stuck = 0
            continue

        # travel() may have moved us a long way even though it didn't fully arrive — e.g.
        # it can legitimately climb to the target floor and then get stuck at a closed
        # door. `pos`/`before` above were captured BEFORE travel() ran, so re-read them now:
        # deciding the floor-vs-walk branch from the STALE pre-travel position is exactly
        # the bug the efficiency test caught. With a known map, travel() climbed us onto
        # the destination floor and stalled at the door; the stale pos.z (still the OLD,
        # lower floor) said "wrong floor", so we hunted an unrelated staircase instead of
        # opening the door we were already standing next to — a multi-thousand-cost detour
        # for what should have been one _try_use_object call.
        pos = session.state.position
        if pos is None:
            await asyncio.sleep(0.1)
            continue
        before = (pos.x, pos.y, pos.z)

        if pos.z != z:
            # Wrong floor. Check STANDING ON IT before hunting: _try_change_floor's hunt
            # (find_descent, up to _DESCENT_RADIUS=30 tiles) will happily walk us toward
            # some OTHER, farther floor-change the instant it finds one — it has no way to
            # know "you're already standing on the answer" until its hunt comes up empty,
            # and a 30-tile radius in a stair-dense building almost always finds SOMETHING.
            # So if we don't check the current tile FIRST, a destination that sits exactly
            # on a floor-change (a grate is the case that exposed this) never gets used:
            # the hunt keeps finding and chasing a decoy instead, every single round.
            # _try_use_underfoot only ever fires for a USE-type match (ladders/grates) —
            # STEP-type "standing on it" stays _try_change_floor's own job (stepping off
            # and back on, which is what actually retriggers a STEP object).
            floor_direction = "up" if z < pos.z else "down"
            used_underfoot = await _try_use_underfoot(session, item_flags, floor_tried,
                                                       floor_direction)
            if not used_underfoot:
                await _try_change_floor(session, item_flags, floor_tried,
                                   max_dist=_DESCENT_RADIUS,
                                   direction=floor_direction)
        else:
            # Right floor — walk toward the target. If that gets us nowhere, an obstacle
            # (a closed door, or — for a caller that allows it — any known exit) may block
            # the only route: clear the nearest one the walker parked us beside.
            await _walk_local(session, item_flags, x, y,
                           max_steps=60, plan_radius=_PLAN_RADIUS)
            after = session.state.position
            after_t = (after.x, after.y, after.z) if after is not None else None
            if after_t == before:
                await _try_use_object(session, item_flags, door_tried,
                                      max_dist=_USE_OBJECT_RADIUS, only=only)

        # Net progress THIS ROUND, start to finish — the honest signal for the fight/
        # escape escalation above. A round that changed floor/tile counts as progress
        # even if we're still short of (x, y, z); only a truly flat round counts against us.
        final = session.state.position
        if final is not None and (final.x, final.y, final.z) == round_start:
            nav_stuck += 1
        else:
            nav_stuck = 0
        await asyncio.sleep(0.1)


async def goto_session(host: str, login_port: int, account: str, password: str,
                       character_name: str | None, goal_x: int, goal_y: int,
                       item_flags: ItemFlags, rsa_n: int = wire.OTSERV_RSA_N,
                       dump_frames: int = 0) -> None:
    """B2: log in and actually walk the character to (goal_x, goal_y)."""
    from .nav import render_ascii

    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, dump_frames)

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)  # let the snapshot finish parsing
        log.info("walking from %s to (%d, %d)...", session.state.position, goal_x, goal_y)
        await _walk_local(session, item_flags, goal_x, goal_y)
        log.info("final map at %s:\n%s",
                 session.state.position, render_ascii(session.state, item_flags))

    await _run_in_world(session, action)


# --- the hauler role: move loot the colony asks for ---------------------------
_HAUL_MAX_ITEMS = 8    # items lifted per trip (bag space + keeps a trip bounded)
_HAUL_SETTLE = 0.5     # give the server a beat to confirm each move


# How often a bot shares what loot it can see, and how far around itself it looks. The
# scan is O(radius^2) dictionary hits, so a second's throttle keeps it free even with a
# big swarm; the radius is roughly the view the server actually describes to us.
_LOOT_SCAN_INTERVAL = 1.0
_LOOT_SCAN_RADIUS = 8


def _scan_loot(session: GameSession) -> None:
    """Tell the colony about any takeable loot in sight — the swarm's shared eyes.

    Every bot already parses every tile in its view, so this costs nothing extra and
    means a scout wandering past a pile makes it fetchable by every hauler.

    We skip anything with no standable tile around it (`has_standable_neighbour`). A LOT
    of Tibia's scenery is takeable-flagged and priced — the crystal coins behind the
    Thais bank counter are worth 30,000 gold on paper — but is sealed in masonry and can
    never be picked up. Since the hauler ranks by value, one decorative pile like that
    outranks every real pile in town, and every hauler queues to fail on it forever. It
    isn't loot; it's map decoration wearing a price tag, and it doesn't belong on the map
    at all.

    CAVEAT worth knowing: our tile map can hold PHANTOMS. The server's 0x6C never tells
    us an item LEFT a tile (it only resolves creatures for us — see BACKLOG), so a pile
    someone else took can linger in our view until the tile is re-described. We report it
    anyway: a phantom sighting costs one wasted trip, and the hauler purges it for real on
    arrival (its browse-field is the server's own answer about what's there). Better a
    stale lead than a missed crystal coin. That's the opposite call from the unreachable
    filter above, and deliberately so: a phantom is a lead that MIGHT pay and self-clears
    on arrival, while sealed scenery is a lead that provably never pays and never clears.
    """
    from .nav import has_standable_neighbour
    colony = session.colony
    pos = session.state.position
    if colony is None or pos is None:
        return
    now = time.monotonic()
    if now - session._last_loot_scan < _LOOT_SCAN_INTERVAL:
        return
    session._last_loot_scan = now
    flags = session.item_flags
    r = _LOOT_SCAN_RADIUS
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            key = (pos.x + dx, pos.y + dy, pos.z)
            items = session.state.tiles.get(key)
            if not items or len(items) < 2:
                continue          # bare ground (index 0) — nothing lying on it
            loot = [(iid, cnt) for iid, cnt in items[1:] if flags.is_takeable(iid)]
            if not loot:
                continue
            if not has_standable_neighbour(session.state, flags, key):
                colony.mark_unreachable(key)   # sealed scenery: never note it again
                continue
            colony.report_loot(key, loot)


def _browse_cid(x: int, y: int) -> int:
    """The container id the server will assign a browse-field for this tile.

    Mirrors Game::playerBrowseField exactly:
        dummyContainerId = 0xF - ((pos.x % 3) * 3 + (pos.y % 3))
    Knowing it up front matters because asking to browse a tile that's ALREADY browsed
    toggles it shut — so we check before we ask.
    """
    return 0xF - ((x % 3) * 3 + (y % 3))


async def _open_browse_field(session: GameSession, tile: tuple[int, int, int],
                             tries: int = 12) -> int | None:
    """Open `tile` as a container so its items are addressable BY SLOT; return the cid.

    This is how we escape the LIFO rule on ground piles (a plain tile move always takes
    the topmost item — see `_collect_loot`). Returns None if the server doesn't open one
    (a Lua `onBrowseField` event may veto it), and the caller falls back to top-down.
    """
    cid = _browse_cid(tile[0], tile[1])
    if cid in session.state.containers:
        session.browse_cids.add(cid)
        return cid                    # already open; asking again would close it
    await session.browse_field(tile[0], tile[1], tile[2])
    for _ in range(tries):
        await asyncio.sleep(0.25)
        if cid in session.state.containers:
            session.browse_cids.add(cid)
            return cid
        if session.closed.is_set() or session.stop_event.is_set():
            return None
    return None


async def _close_browse_field(session: GameSession, tile: tuple[int, int, int],
                              cid: int) -> None:
    """Shut a browse-field. Browsing the same tile again TOGGLES it closed (server-side),
    which keeps stale tile-containers from piling up as the hauler moves on."""
    if cid not in session.state.containers:
        session.browse_cids.discard(cid)
        return
    try:
        await session.browse_field(tile[0], tile[1], tile[2])
        await asyncio.sleep(0.3)
    except (ConnectionResetError, RuntimeError, OSError):
        pass
    session.browse_cids.discard(cid)


def _bag_container_id(session: GameSession) -> int | None:
    """The open backpack's container id — the bag we haul into. None if no bag is open.

    `_run_in_world` opens the slot-3 bag at login, so this is normally container 0.
    Browse-fields are skipped: they're ground tiles wearing a container costume, and
    hauling into one would just put the loot back on the floor.
    """
    return next((cid for cid in session.state.containers
                 if cid not in session.browse_cids), None)


async def _ensure_bag_open(session: GameSession, tries: int = 4) -> int | None:
    """Guarantee we have an OPEN bag to haul into; return its container id (or None).

    The login-time open is best-effort: `_open_backpack` waits a couple of seconds for
    the worn inventory to arrive, fires one 'use' at the slot-3 bag, and returns WITHOUT
    confirming the container actually opened. If the inventory lands late (it arrives in
    later frames, not the login tail) the bag never opens — and a hauler that merely
    ASSUMES it has one then reports "nothing to collect" while standing on the loot.
    So the hauler re-opens on demand and waits for the 0x6E to land before giving up.
    """
    bag = _bag_container_id(session)
    if bag is not None:
        return bag
    for _ in range(tries):
        if session.closed.is_set() or session.stop_event.is_set():
            return None
        await _open_backpack(session)
        for _ in range(10):                 # give the container packet time to arrive
            await asyncio.sleep(0.3)
            bag = _bag_container_id(session)
            if bag is not None:
                return bag
    return None


async def _collect_loot(session: GameSession, item_flags: ItemFlags,
                        tile: tuple[int, int, int],
                        max_items: int = _HAUL_MAX_ITEMS) -> list[int]:
    """Lift the WORTH-TAKING items off `tile` into our bag; return the ids we got.

    WHY WE BROWSE THE TILE FIRST. A plain move from a tile is LIFO: Canary resolves it
    with `internalGetThing(..., STACKPOS_MOVE)`, which ignores the stackpos we send and
    returns `tile->getTopDownItem()` — and `playerMoveItem` then refuses the move unless
    that top item's id matches the one we named. So a bot can only ever take what's on
    top, which is useless when the gold is under a worthless robe.

    Browse-field is the way out (and it works on 8.60 — see `GameSession.browse_field`):
    the server wraps the TILE in a Container and sends it as an ordinary 0x6E, and
    container access is by SLOT (`slot = pos.z`), not LIFO. So we open the tile, then
    lift items out of it in whatever order we like — which is what `carry.plan_pickup`
    wanted all along: score every candidate against BOTH budgets (weight from 0xA0,
    slots from our bag) and take the best that fits, whole stacks at a time.

    If the server won't browse the tile (a Lua `onBrowseField` event may veto it) we fall
    back to the honest top-down pop, which is protocol-correct but can't dig.
    """
    got: list[int] = []
    bag = await _ensure_bag_open(session)
    if bag is None:
        session.log_event("warning", "task", "no open bag to haul into")
        return got
    econ = getattr(session.colony, "economy", None) if session.colony is not None else None
    browse = await _open_browse_field(session, tile)
    if browse is None:
        session.log_event("info", "task",
                          f"browse-field unavailable at {tile}; taking top-down only")
    refused: set[int] = set()   # ids the server wouldn't move; don't retry them forever
    try:
        for _ in range(max_items):
            # Candidates: from the browsed tile-container we can see (and address) every
            # item; without it we can only ever consider the topmost one.
            if browse is not None:
                slots = session.state.containers.get(browse) or []
                cands = [(iid, cnt) for iid, cnt in slots
                         if item_flags.is_takeable(iid) and iid not in refused]
            else:
                items = session.state.tiles.get(tile) or []
                top = items[-1] if len(items) > 1 else None
                cands = ([top] if top and item_flags.is_takeable(top[0])
                         and top[0] not in refused else [])
            if not cands:
                break

            if econ:
                plan = plan_pickup(session.state, item_flags, econ, cands)
                if not plan:
                    break      # nothing left worth a slot, or nothing else fits
                item_id, item_count = plan[0]
            else:
                item_id, item_count = cands[-1]    # no catalog: just take one

            # Where to move it FROM. A browsed tile is addressed like any container.
            if browse is not None:
                slot = next((i for i, (iid, _c) in enumerate(slots) if iid == item_id), None)
                if slot is None:
                    break
                src, src_stack = container_pos(browse, slot), slot
            else:
                src, src_stack = tile, len(session.state.tiles.get(tile) or []) - 1

            if session.recorder is not None:
                session.recorder.decision("pickup", item=item_id, count=item_count,
                                          value=value_of(econ, item_id, item_count) if econ else None,
                                          via="browse" if browse is not None else "top")
            before = len(session.state.containers.get(bag, []))
            await session.move_item(src, item_id, src_stack, container_pos(bag, 0),
                                    max(1, item_count))
            await asyncio.sleep(_HAUL_SETTLE)
            gained = len(session.state.containers.get(bag, [])) > before
            if gained:
                got.append(item_id)
                # Browsed tiles keep themselves honest (the server sends container
                # updates); the raw tile map doesn't — its 0x6C never removes items for
                # us, so drop what we took from our own view or we'd chase a phantom.
                if browse is None:
                    cur = session.state.tiles.get(tile)
                    if cur and cur and cur[-1][0] == item_id:
                        cur.pop()
            else:
                refused.add(item_id)
                session.log_event("info", "task",
                                  f"couldn't lift {item_id} x{item_count} at {tile}")
    finally:
        if browse is not None:
            # GROUND TRUTH, and we must apply it to BOTH maps or it doesn't stick.
            # The browse container is the server's own answer about this tile: exactly
            # its movable contents (the ground itself is excluded). So:
            #  1. Resync our LOCAL tile map from it. Our 0x6C never removes items (it
            #     only resolves creatures for us), so `state.tiles` keeps phantoms of
            #     everything we just picked up.
            #  2. Then correct the colony's shared loot map ([] means "nothing here",
            #     which report_loot turns into a forget).
            # Order matters: without step 1, `_scan_loot` would re-read those phantoms a
            # second later and resurrect the very sighting step 2 just purged — the loot
            # map would insist a looted pile is still there, forever.
            left_all = list(session.state.containers.get(browse) or [])
            cur = session.state.tiles.get(tile)
            if cur:
                session.state.tiles[tile] = [cur[0]] + left_all   # ground + what's real
            if session.colony is not None:
                left = [(iid, cnt) for iid, cnt in left_all if item_flags.is_takeable(iid)]
                session.colony.report_loot(tile, left)
            await _close_browse_field(session, tile, browse)
    return got


async def _deposit_loot(session: GameSession, tile: tuple[int, int, int]) -> int:
    """Put everything in our bag down on `tile`; return how many items we placed."""
    bag = await _ensure_bag_open(session)
    if bag is None:
        return 0
    placed = 0
    for _ in range(_HAUL_MAX_ITEMS + 2):
        items = session.state.containers.get(bag) or []
        if not items:
            break
        item_id, count = items[0]
        before = len(items)
        await session.move_item(container_pos(bag, 0), item_id, 0, tile,
                                count if count > 1 else 1)
        await asyncio.sleep(_HAUL_SETTLE)
        if len(session.state.containers.get(bag) or []) < before:
            placed += 1
        else:
            session.log_event("info", "task", f"couldn't drop item {item_id} at {tile}")
            break   # the tile won't take it (full / blocked) — don't spin
    return placed


async def _haul_travel(session: GameSession, item_flags: ItemFlags,
                       target: tuple[int, int, int], reach: int = 0) -> bool:
    """Get to `target` (x, y, z) — across floors if needed. True if we arrived.

    `reach` > 0 means "get within that many tiles" instead of standing on it — for a
    target you touch rather than occupy (a locker is `unpass`; a pile can be pinned
    under something blocking).
    """
    from .nav import within_reach
    tx, ty, tz = target
    pos = session.state.position
    if pos is not None and within_reach((pos.x, pos.y, pos.z), target, reach):
        return True
    await travel(session, item_flags, tx, ty, tz, reach=reach)
    p = session.state.position
    return p is not None and within_reach((p.x, p.y, p.z), target, reach)


# Rough costs for scoring a haul, in the only currency that matters: seconds. They don't
# need to be exact — they only have to RANK piles sensibly, and every candidate is scored
# with the same yardstick. Walking dominates, which is the point.
_HAUL_TILES_PER_SEC = 1.3      # a bot's real walking pace, near enough
_HAUL_PICKUP_SECONDS = 0.8     # per item lifted (the move + its settle)
_HAUL_CANDIDATE_TRIES = 5      # how many of the best we'll try to claim before giving up
_HAUL_MAX_PILES = 5            # piles chained into one trip, so a trip always ends
_HAUL_MAX_DETOUR = 40          # tiles of detour we'll accept for one more pile mid-trip
# How long a bot remembers that it couldn't reach a pile. Long enough to stop it
# thrashing on the same unreachable target for a whole session; short enough that once
# the scouts open up the route, the pile comes back into play by itself.
_UNREACHABLE_TTL = 180.0


def _live_unreachable(session: GameSession) -> set[tuple[int, int, int]]:
    """Pickups this bot still considers unreachable, forgetting the lapsed ones."""
    now = time.monotonic()
    for tile in [t for t, when in session.unreachable.items() if when <= now]:
        session.unreachable.pop(tile, None)
    return set(session.unreachable)


async def _claim_best_haul(session: GameSession, item_flags: ItemFlags,
                           chaining: bool = False):
    """Score the shared loot map for THIS bot and claim the best pile — self-directed work.

    This is what turns haulers from order-takers into something that finds its own work:
    instead of a human posting tasks, the bot reads what the swarm has seen and picks.

    Scoring is gold-per-second, which unifies the three things that matter:

        score = feasible_value x confidence / seconds

    - FEASIBLE value, not sticker value: `plan_pickup` decides what this bot can actually
      carry given its weight and slot budgets, so a pile of heavy junk it has no room for
      scores nothing. That's also why the colony can't score for us — only we know what
      we're carrying and which stacks we can top up for free.
    - CONFIDENCE discounts stale intel without deleting it, so a half-sure crystal coin
      still beats a fresh copper one (see LootSighting).

    `chaining` is what makes multi-pile trips pay. Starting a trip, a pile costs the walk
    out AND the carry home — we'd not be making that trip otherwise:

        seconds = d(me,P) + d(P,stash) + lift

    But once we're ALREADY loaded and heading for the stash, that trip home is sunk, so
    the only real cost of one more pile is the DETOUR — the classic insertion cost:

        seconds = d(me,P) + d(P,stash) - d(me,stash) + lift

    which is ~0 for a pile on the way home and large for one in the opposite direction.
    That's precisely why chaining beats pile->stash->pile->stash: the trip home gets
    amortised over everything we grab along it.

    Claiming is optimistic: we score freely and only contend at the claim, retrying the
    next-best if another hauler beat us to it.
    """
    colony = session.colony
    pos = session.state.position
    if colony is None or pos is None:
        return None
    stash = getattr(colony, "stash", None)
    if stash is None:
        return None            # nowhere to deliver; nothing sensible to claim
    econ = getattr(colony, "economy", None)
    if not econ:
        return None
    cands = colony.loot_candidates(pos.z)
    if not cands:
        return None
    # We must know our SLOT budget before we can judge anything: with no open bag
    # `free_slots` is 0, `plan_pickup` decides nothing fits, and the hauler would stand
    # on a pile of gold declining it forever. The login-time open is best-effort (the
    # worn inventory can arrive late), so make sure of it here rather than assume.
    if await _ensure_bag_open(session) is None:
        return None

    now = time.time()
    stash_t = tuple(stash)
    unreachable = _live_unreachable(session)
    scored = []
    for s in cands:
        if s.tile == stash_t:
            continue            # already AT the stash: it's home, there's no haul to do.
                                # (Without this a hauler lifts the pile and puts it right
                                # back on the same tile, then sees it again — forever.)
        if s.tile in unreachable:
            continue            # we already proved we can't walk there; don't re-post it
        plan = plan_pickup(session.state, item_flags, econ, s.items)
        if not plan:
            continue            # nothing here we'd want, or nothing that still fits
        value = sum(value_of(econ, iid, n) for iid, n in plan) * s.confidence(now)
        if value <= 0:
            continue
        to_pile = abs(s.tile[0] - pos.x) + abs(s.tile[1] - pos.y)
        pile_home = abs(s.tile[0] - stash_t[0]) + abs(s.tile[1] - stash_t[1])
        # When chaining, the walk home is already owed, so only the DETOUR is new.
        already_owed = (abs(pos.x - stash_t[0]) + abs(pos.y - stash_t[1])) if chaining else 0
        tiles = max(0, to_pile + pile_home - already_owed)
        seconds = tiles / _HAUL_TILES_PER_SEC + _HAUL_PICKUP_SECONDS * len(plan)
        if seconds <= 0:
            seconds = _HAUL_PICKUP_SECONDS      # right under our feet: still costs a lift
        if chaining and tiles > _HAUL_MAX_DETOUR:
            continue                            # too far out of our way to be worth it
        scored.append((value / seconds, value, s))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])

    for rate, value, s in scored[:_HAUL_CANDIDATE_TRIES]:
        task = colony.claim_loot(s.tile, session.bot_name, stash,
                                 note=f"~{int(value)} gold @ {rate:.1f} gold/s")
        if task is not None:
            if session.recorder is not None:
                session.recorder.decision("claimed_loot", tile=list(s.tile),
                                          value=int(value), rate=round(rate, 2))
            return task
    return None                 # everything we fancied got claimed out from under us


async def hauler(session: GameSession, item_flags: ItemFlags,
                 duration: float | None = None) -> None:
    """Move loot for the colony — the first role that does WORK rather than explore.

    The loop is the whole point of the blackboard: claim a posted haul task, walk to its
    pickup, lift what's there — and then, crucially, look for ANOTHER pile worth grabbing
    before walking home. With no work posted it idles (cheaply) rather than wandering.

    THE UNIT OF WORK IS THE TRIP, NOT THE PILE. Going pile -> stash -> pile -> stash pays
    the walk home for every single pile, which is madness when the bot has 400k capacity
    and eight slots. So a trip chains piles (`_claim_best_haul(chaining=True)`, which
    scores by DETOUR rather than round-trip) until a budget binds — at which point
    `plan_pickup` stops finding anything that fits and there's simply nothing more to
    claim — and only then delivers everything in one go. The walk home is amortised over
    the whole haul.

    A pile's task is finished the moment we collect it (the pile IS collected); delivery
    is a separate phase, so a failed walk home doesn't lose the loot — we keep carrying it
    and try again next lap. Failure to REACH a pile releases it (`release_task`) instead
    of failing it, so another bot can try; anything still claimed at logout is released
    too, and the colony reopens stale claims itself (see Colony._TASK_CLAIM_TTL).
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + duration if duration else float("inf")
    colony = session.colony
    pending: list = []      # piles claimed for this trip, not yet visited
    carried = 0             # items in the bag from this trip
    visited = 0             # piles worked this trip (bounds the trip)
    while (loop.time() < end and not session.closed.is_set()
           and not session.stop_event.is_set()):
        if _check_watchdog(session):
            break
        _prune_blocked_tiles(session)
        if colony is None or session.state.position is None:
            await asyncio.sleep(1.0)
            continue

        # --- start a trip: posted work first (a human asked), else find our own ---
        if not pending and carried == 0:
            task = colony.claim_task(session.bot_name, kind="haul",
                                     avoid=_live_unreachable(session))
            if task is None:
                task = await _claim_best_haul(session, item_flags, chaining=False)
            if task is None:
                # Idle ON PURPOSE — tell the watchdog so it doesn't read stillness as a
                # freeze and relog us in a loop (see GameSession.expect_movement).
                session.expect_movement = False
                session.status = "waiting for work"
                session._report_to_colony()   # keep the dashboard status fresh while still
                await asyncio.sleep(2.0)
                continue
            session.expect_movement = True    # we're about to walk; watchdog back on
            visited = 0
            pending.append(task)
            session.log_event("info", "task",
                              f"claimed haul #{task.id}: {task.pickup} -> {task.dropoff}")

        # --- work the next claimed pile ---
        if pending:
            cur = pending.pop(0)
            session.status = f"haul #{cur.id}: to pickup"
            if not await _haul_travel(session, item_flags, tuple(cur.pickup)):
                # Don't re-claim what we just proved we can't walk to (it goes back to
                # `open` for a better-placed bot). Without this we'd re-claim it on the
                # very next lap and thrash on it until the session died.
                session.unreachable[tuple(cur.pickup)] = (time.monotonic()
                                                          + _UNREACHABLE_TTL)
                # Tell the colony too: enough bots failing here means the pile isn't
                # awkward, it's unreachable, and the swarm should stop paying for it.
                written_off = colony.report_unreachable(tuple(cur.pickup), session.bot_name)
                session.log_event("warning", "task",
                                  f"haul #{cur.id}: couldn't reach pickup {cur.pickup}; "
                                  + ("written off as unreachable" if written_off
                                     else f"skipping it for {_UNREACHABLE_TTL:.0f}s"))
                colony.release_task(cur.id)
            else:
                session.status = f"haul #{cur.id}: collecting"
                got = await _collect_loot(session, item_flags, tuple(cur.pickup))
                carried += len(got)
                visited += 1
                colony.finish_task(cur.id, ok=True,
                                   note=f"collected {len(got)}" if got else "nothing to collect")
            # Chain: with loot aboard and room left, is another pile nearly on our way?
            # _claim_best_haul returns None once nothing more fits our budgets, which is
            # exactly the "we're full, go home" signal — no separate check needed.
            if carried and visited < _HAUL_MAX_PILES and not pending:
                nxt = await _claim_best_haul(session, item_flags, chaining=True)
                if nxt is not None:
                    pending.append(nxt)
                    session.log_event("info", "task",
                                      f"chaining pile #{nxt.id} at {nxt.pickup} into this trip")
            if pending:
                continue          # go get it before heading home

        # --- deliver the whole trip in one go ---
        if carried == 0:
            continue              # nothing aboard: start a fresh trip
        stash = getattr(colony, "stash", None) or session.state.position
        stash_t = tuple(stash) if not hasattr(stash, "x") else (stash.x, stash.y, stash.z)
        session.status = f"haul: delivering {carried} from {visited} pile(s)"
        if not await _haul_travel(session, item_flags, stash_t):
            session.log_event("warning", "task",
                              f"couldn't reach the stash {stash_t} (still carrying {carried})")
            await asyncio.sleep(2.0)
            continue              # keep the loot; try again next lap
        placed = await _deposit_loot(session, stash_t)
        session.log_event("info", "task",
                          f"trip done: {visited} pile(s), delivered {placed}/{carried} item(s)")
        if session.recorder is not None:
            session.recorder.decision("trip_done", piles=visited, delivered=placed)
        carried = 0
        visited = 0
        await asyncio.sleep(0.3)

    # Never strand work when we log out: hand back anything we claimed but never reached.
    # (Piles we already collected are finished; their loot is in the bag and will be
    # delivered by whoever is carrying it next time, or dropped on death — the colony's
    # stale-claim reopening covers the rest.)
    if colony is not None:
        for t in pending:
            colony.release_task(t.id)
    session.status = "idle"
    session._report_to_colony()


async def hauler_session(host: str, login_port: int, account: str, password: str,
                         character_name: str | None, item_flags: ItemFlags, colony,
                         duration: float | None = None, rsa_n: int = wire.OTSERV_RSA_N,
                         stop_event: asyncio.Event | None = None,
                         record_mode: str = "none") -> None:
    """Log a character in and run the hauler behaviour, reporting to the colony."""
    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, 0)
    session.colony = colony
    session.role = "hauler"
    if stop_event is not None:
        session.stop_event = stop_event
    _attach_recorder(session, record_mode)

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)   # _run_in_world already opened our bag
        log.info("hauler: starting from %s", session.state.position)
        await hauler(session, item_flags, duration)

    try:
        await _run_in_world(session, action)
    finally:
        if session.recorder is not None:
            session.recorder.close()


async def explore_session(host: str, login_port: int, account: str, password: str,
                          character_name: str | None, item_flags: ItemFlags, colony,
                          duration: float | None = None, rsa_n: int = wire.OTSERV_RSA_N,
                          stop_event: asyncio.Event | None = None, bound: int = 22,
                          record_mode: str = "none") -> None:
    """Wander to reveal the map, feeding position + tiles to the colony (Phase C).

    The behaviour: each round, head for the nearest FRONTIER tile within a bounded
    box around home — the closest reachable tile that borders unseen space. Because
    frontier tiles are found by flooding *walkable* tiles, a path to them provably
    exists, so the bot always makes real progress and reveals fresh map instead of
    thrashing toward random (often unreachable) points. When the home box is fully
    mapped it falls back to idle milling. The receive loop does the actual colony
    reporting on every step (see GameSession._report_to_colony).

    `duration` caps how long to explore (None = until stopped). `stop_event` lets
    the ColonyManager tell the bot to wrap up and log out on demand (dashboard
    "Stop" button); we adopt it as the session's stop event.
    """
    import random
    from .nav import find_frontier

    session = await _connect_session(host, login_port, account, password,
                                     character_name, item_flags, rsa_n, 0)
    session.colony = colony
    session.role = "explore"
    if stop_event is not None:
        session.stop_event = stop_event
    _attach_recorder(session, record_mode)

    async def action(session: GameSession) -> None:
        await asyncio.sleep(0.2)
        session.status = "exploring"
        # Anchor exploration to where we started ("home") and only pick targets
        # within a bounded radius of it. This keeps the colony clustered around
        # the spawn (the temple) instead of drifting to the map edges where the
        # stairs/holes/teleports live — and if a bot IS whisked far away by an
        # interior teleport, it keeps trying to head home (and idles when it
        # can't, rather than wandering off) so the swarm stays together.
        home = session.state.position
        hx, hy = (home.x, home.y) if home else (0, 0)
        BOUND = bound
        loop = asyncio.get_event_loop()
        end = loop.time() + duration if duration else float("inf")
        stuck_rounds = 0
        while (loop.time() < end and not session.closed.is_set()
               and not session.stop_event.is_set()):
            if _check_watchdog(session):   # dead connection -> abort
                break
            _prune_blocked_tiles(session)  # expire transient (creature/bot) blocks
            pos = session.state.position
            if pos is None:
                break
            before = (pos.x, pos.y)

            # Prefer the nearest unexplored edge inside our home box: reachable by
            # construction and guaranteed to reveal new map. Only when the whole box
            # is mapped do we mill toward a random nearby tile just to keep moving.
            frontier = find_frontier(session.state, item_flags,
                                     center=(hx, hy), radius=BOUND)
            if frontier is not None:
                target_x, target_y = frontier
                is_frontier = True          # reachable by construction -> warn if not
            else:
                target_x = hx + random.randint(-BOUND, BOUND)
                target_y = hy + random.randint(-BOUND, BOUND)
                is_frontier = False         # random milling -> not arriving is fine

            # Size the step budget to the distance so a reachable target is never
            # cut off mid-route (which used to be mis-logged as "gave up"). Bound
            # each re-plan to the local neighbourhood for cheap planning at scale.
            dist = abs(target_x - pos.x) + abs(target_y - pos.y)
            budget = min(60, dist * 2 + 8)
            await _walk_local(session, item_flags, target_x, target_y,
                           max_steps=budget, plan_radius=_PLAN_RADIUS,
                           log_stuck=is_frontier)
            after = session.state.position
            # If a whole round of walking didn't move us, we're probably boxed in
            # (every reachable tile toward our targets is blocked). Back off so we
            # don't busy-loop; the watchdog will clear stale blocks if it persists.
            if after is not None and (after.x, after.y) == before:
                stuck_rounds += 1
            else:
                stuck_rounds = 0
            if stuck_rounds >= 6:
                if stuck_rounds == 6:
                    session.log_event("warning", "stuck", "boxed in; can't reach any target")
                session.status = "boxed in"
                await asyncio.sleep(2.0)
            else:
                session.status = "exploring"
                await asyncio.sleep(0.3)
        session.status = "idle"
        session._report_to_colony()

    try:
        await _run_in_world(session, action)
    finally:
        if session.recorder is not None:
            session.recorder.close()


async def run_explorers(colony, bots: list[tuple[str, str, str | None]], host: str,
                        login_port: int, item_flags: ItemFlags, duration: float,
                        rsa_n: int = wire.OTSERV_RSA_N) -> None:
    """Run several explorer bots concurrently, all reporting to one colony.

    `bots` is a list of (account, password, character) triples. Each runs in the
    same event loop; asyncio multiplexes their connections, so one process can
    host the whole (small, for now) colony. Exceptions are surfaced per-bot so one
    failing login doesn't take the others down.
    """
    async def guarded(index: int, account: str, password: str, character: str | None) -> None:
        # Stagger logins so bots don't all hammer the server's connection handling
        # at once (it closes connections that arrive too fast from one IP).
        await asyncio.sleep(index * 2.5)
        attempts = 4
        for attempt in range(attempts):
            try:
                await explore_session(host, login_port, account, password, character,
                                      item_flags, colony, duration, rsa_n)
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # isolate one bot's failure from the rest
                if attempt + 1 < attempts:
                    backoff = 3.0 + attempt * 3.0  # let the congestion clear
                    log.warning("bot %s/%s connect failed (%s); retry %d/%d in %.0fs",
                                account, character, err, attempt + 1, attempts - 1, backoff)
                    await asyncio.sleep(backoff)
                else:
                    log.error("bot %s/%s gave up after %d attempts: %s",
                              account, character, attempts, err)

    # gather with return_exceptions so a straggler can never cancel the others.
    await asyncio.gather(*(guarded(i, a, p, c) for i, (a, p, c) in enumerate(bots)),
                         return_exceptions=True)
