"""Session recorder — log everything a bot does, for replay + debugging.

See REPLAY_DESIGN.md for the full picture. In short: a `SessionRecorder` is attached
to a `GameSession` and captures Tier-2 fidelity — per-tick position/status/vitals, the
CREATURES around the bot, the ACTIONS it took (walk / autowalk / use / escape / descend),
and the DECISIONS behind them (chose frontier, declined descent — crowded, boxed-in). The
dashboard's replay mode reads the resulting JSONL back and lets you scrub the session.

Two modes:
  - "flight"  (default): a time-bounded in-memory ring (last `flight_window` seconds).
              Nothing hits disk UNTIL a bad event fires (`flight_triggers`, e.g. a
              freeze/death), at which point the whole ring is dumped — a black box that
              keeps the footage around every rare bug without drowning in data.
  - "full"    (opt-in): every record is streamed to a file for a run you want to watch
              end to end.

HARD RULE (see design doc): recording must never stall the navigation loop. `snapshot()`
/ `action()` / `decision()` / `event()` only append to an in-memory buffer (O(1)); the
actual disk writes happen on a background 1s flush task and are handed to a thread
executor so file I/O never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("antbot")

# All recordings live here (shared with the dashboard, which serves them for replay).
# antbot/antbot/recorder.py -> parents[1] is the antbot project dir.
RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings"

# Defaults.
_FLIGHT_WINDOW = 180.0                     # seconds of history the black box keeps
_FLIGHT_TRIGGERS = ("frozen", "death")     # event categories that auto-persist the ring
_SNAPSHOT_MIN_INTERVAL = 0.4               # throttle: at most ~2-3 snapshots/sec
_FLUSH_INTERVAL = 1.0                      # how often the background task writes to disk


def _safe(name: str) -> str:
    """Filesystem-safe version of a bot name (for the recording filename)."""
    return "".join(c if c.isalnum() else "_" for c in name)


def _stamp(t: float) -> str:
    """Compact local timestamp for filenames, e.g. 20260714_183205."""
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(t))


class SessionRecorder:
    """Records one bot's session (one login-to-logout connection) to JSONL.

    Attach to a GameSession as `session.recorder`; the session's capture points call
    the `snapshot/action/decision/event` methods. Call `close()` when the session ends.
    """

    def __init__(self, bot_name: str, character: str, mode: str = "flight",
                 outdir: Path = RECORDINGS_DIR, flight_window: float = _FLIGHT_WINDOW,
                 flight_triggers=_FLIGHT_TRIGGERS) -> None:
        self.bot_name = bot_name
        self.character = character
        self.mode = mode                       # "flight" | "full"
        self.outdir = Path(outdir)
        self.flight_window = flight_window
        self.flight_triggers = set(flight_triggers)
        self.start = time.time()
        self._header = {"type": "header", "bot": bot_name, "character": character,
                        "start": self.start, "mode": mode}
        # In "full" mode `_buf` is the pending-write queue (drained to `_file`); in
        # "flight" mode it's the time-bounded ring that a trigger dumps.
        self._buf: deque[dict] = deque()
        self._file = None
        self._closed = False
        self._flush_task = None
        self._last_snap_t = 0.0
        self._last_snap_key: tuple | None = None

        if self.mode == "full":
            self.outdir.mkdir(parents=True, exist_ok=True)
            self._path = self.outdir / f"{_safe(bot_name)}_{_stamp(self.start)}.jsonl"
            self._file = self._path.open("w", encoding="utf-8")
            self._file.write(json.dumps(self._header) + "\n")
            self._flush_task = asyncio.ensure_future(self._flusher())
            log.info("recorder: %s recording (full) -> %s", bot_name, self._path)

    # -- capture API (all O(1), safe to call from the nav loop) ---------------

    def snapshot(self, state, status: str) -> None:
        """Record where we are + our vitals + the creatures around us right now."""
        if self._closed:
            return
        pos = state.position
        if pos is None:
            return
        # Throttle: skip if nothing meaningful changed and it's been < the min interval
        # (position/status carry the spine; creatures move but we don't need every frame).
        now = time.time()
        key = (pos.x, pos.y, pos.z, status)
        if key == self._last_snap_key and (now - self._last_snap_t) < _SNAPSHOT_MIN_INTERVAL:
            return
        self._last_snap_t = now
        self._last_snap_key = key
        creatures = [
            {"id": c.creature_id, "name": c.name,
             "x": c.position.x, "y": c.position.y, "z": c.position.z,
             "hp": c.health_percent}
            for c in state.nearby_creatures() if c.position is not None
        ]
        self._rec({"t": now, "type": "snapshot",
                   "x": pos.x, "y": pos.y, "z": pos.z, "status": status,
                   "hp": state.hp, "maxhp": state.max_hp,
                   "mana": state.mana, "maxmana": state.max_mana, "level": state.level,
                   "creatures": creatures})

    def action(self, action: str, **fields) -> None:
        """Record a concrete action the bot took (walk / autowalk / use / ...)."""
        if not self._closed:
            self._rec({"t": time.time(), "type": "action", "action": action, **fields})

    def decision(self, what: str, **fields) -> None:
        """Record WHY the bot did something (chose frontier, declined descent, ...)."""
        if not self._closed:
            self._rec({"t": time.time(), "type": "decision", "what": what, **fields})

    def event(self, level: str, category: str, message: str) -> None:
        """Mirror a dashboard event; in flight mode a trigger category dumps the ring."""
        if self._closed:
            return
        self._rec({"t": time.time(), "type": "event",
                   "level": level, "category": category, "msg": message})
        if self.mode == "flight" and category in self.flight_triggers:
            self.dump(trigger=category)

    # -- persistence ----------------------------------------------------------

    def _rec(self, obj: dict) -> None:
        """Append a record. Full mode queues it for the flusher; flight mode rings it."""
        self._buf.append(obj)
        if self.mode == "flight":
            cutoff = time.time() - self.flight_window
            while self._buf and self._buf[0].get("t", self.start) < cutoff:
                self._buf.popleft()

    def dump(self, trigger: str = "manual") -> Path | None:
        """Persist the current flight ring to a file (black-box snapshot)."""
        if self.mode != "flight" or not self._buf:
            return None
        self.outdir.mkdir(parents=True, exist_ok=True)
        path = self.outdir / f"{_safe(self.bot_name)}_{_stamp(time.time())}_flight_{_safe(trigger)}.jsonl"
        header = dict(self._header, dumped=time.time(), trigger=trigger)
        lines = [json.dumps(header)] + [json.dumps(r) for r in self._buf]
        # Offload the write so a slow disk never stalls the loop; fall back to a direct
        # write if we're not on a running loop (e.g. a unit test).
        blob = "\n".join(lines) + "\n"
        try:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, path.write_text, blob)
        except RuntimeError:
            path.write_text(blob)
        log.info("recorder: %s flight-dumped %d records (%s) -> %s",
                 self.bot_name, len(self._buf), trigger, path)
        return path

    async def _flusher(self) -> None:
        """Background task (full mode): batch-write queued records every second."""
        try:
            while not self._closed:
                await asyncio.sleep(_FLUSH_INTERVAL)
                await self._drain()
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        """Write everything queued to the open file, off the event-loop thread."""
        if self._file is None or not self._buf:
            return
        lines = []
        while self._buf:
            lines.append(json.dumps(self._buf.popleft()))
        blob = "\n".join(lines) + "\n"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_blob, blob)

    def _write_blob(self, blob: str) -> None:
        if self._file is not None:
            self._file.write(blob)
            self._file.flush()

    def close(self) -> None:
        """Finalise the recording. Full mode flushes + closes the file; flight mode
        discards its ring (a clean session isn't worth keeping — only triggered dumps
        are), so nothing hits disk unless a trigger fired during the run."""
        if self._closed:
            return
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
        if self._file is not None:
            # Final synchronous drain — we're shutting down, so simplicity wins.
            if self._buf:
                self._file.write("\n".join(json.dumps(r) for r in self._buf) + "\n")
            self._file.flush()
            self._file.close()
            log.info("recorder: %s closed recording %s", self.bot_name, self._path)
