"""tracing — opt-in verbose call/return logging and frame-by-frame algorithm traces.

Two independent, unrelated-in-mechanism tools live here, both OFF by default and both
cheap enough when off that shipping them baked into production code costs nothing unless
someone deliberately asks to see them:

  1. `@log_call` — wraps a function (sync or async) so every CALL and RETURN is logged at
     DEBUG on the `antbot.calls` logger: the function's qualified name, its arguments, its
     return value (or the exception it raised), and how long it took. Off by default; turn
     it on with:

         logging.getLogger("antbot.calls").setLevel(logging.DEBUG)

     The very first thing the wrapper does is an `isEnabledFor(DEBUG)` check, so a
     decorated function costs one boolean test when tracing is off — the same design the
     codebase already uses for `antbot.route`'s per-edge Dijkstra logging (see nav.py).
     Applied to the DECISION-MAKING and STATE-CHANGING functions across nav/client/colony
     (not every single getter/helper) — see each module for which ones and why.

  2. `FrameLogger` — an append-only JSONL sink for an algorithm's frame-by-frame internals
     (one JSON object per line: a node popped, an edge relaxed, a search concluding). This
     is a DIFFERENT, finer granularity than `log_call` — it's not "which functions were
     called" but "what is find_shared_route's Dijkstra actually doing, step by step, inside
     ONE call" — meant to be replayed later by a visualization tool, not read directly.
     Unlike `log_call`, there's no global on/off switch: a caller passes a `FrameLogger`
     instance into the algorithm (see nav.find_shared_route's `frames` param) only when it
     specifically wants that one call's internals recorded, since a route search's frames
     are only meaningful in the context of the ONE call that produced them.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

log_calls = logging.getLogger("antbot.calls")

# Longest a single argument/return-value repr is allowed to get before truncation — long
# enough to show real values (a tile tuple, a short list of directions) without a stray
# GameState or a colony's whole walkable set blowing a log line up to megabytes.
_MAX_REPR = 200
# Containers bigger than this are shown as a size summary instead of their full contents —
# same reasoning: `state.tiles` or `colony.get_walkable()` in a call's args/return would
# otherwise dump thousands of entries into a single DEBUG line.
_MAX_CONTAINER_ITEMS = 8


def _short(value: Any) -> str:
    """A repr short enough for one log line: full for scalars and small containers, a size
    summary for anything that would otherwise flood it.

    Type-checked by NAME (not isinstance against an import) deliberately: this module sits
    underneath nav/client/colony (they import it, not the reverse), so it can't import
    GameState/Colony itself without a cycle. GameState in particular is a plain
    `@dataclass` with no custom `__repr__` — its auto-generated one would dump the entire
    `tiles` dict (thousands of entries once a bot's explored anything) into a single DEBUG
    line, which is the exact flood this function exists to prevent.
    """
    name = type(value).__name__
    if name == "GameState":
        pos = getattr(value, "position", None)
        ntiles = len(getattr(value, "tiles", None) or {})
        return f"GameState(position={pos!r}, tiles=len={ntiles})"
    if name == "Colony":
        return "Colony(...)"
    if isinstance(value, (set, frozenset, list, tuple)) and len(value) > _MAX_CONTAINER_ITEMS:
        return f"{type(value).__name__}(len={len(value)})"
    if isinstance(value, dict) and len(value) > _MAX_CONTAINER_ITEMS:
        return f"dict(len={len(value)})"
    try:
        r = repr(value)
    except Exception:  # noqa: BLE001 — a broken __repr__ must not break tracing
        return f"<{type(value).__name__} (repr failed)>"
    return r if len(r) <= _MAX_REPR else r[:_MAX_REPR] + f"...(+{len(r) - _MAX_REPR} chars)"


def _format_call(qualname: str, args: tuple, kwargs: dict) -> str:
    parts = [_short(a) for a in args] + [f"{k}={_short(v)}" for k, v in kwargs.items()]
    return f"{qualname}({', '.join(parts)})"


def log_call(func: Callable) -> Callable:
    """Log this function's call (name + args) and return value (or exception) at DEBUG on
    `antbot.calls`. A no-op — one `isEnabledFor` check — unless that logger is turned on.
    Works transparently for both `async def` and plain functions; re-raises exceptions
    after logging them, so this never changes what a caller observes, only what gets
    written to the log.
    """
    qualname = func.__qualname__

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            if not log_calls.isEnabledFor(logging.DEBUG):
                return await func(*args, **kwargs)
            log_calls.debug("-> %s", _format_call(qualname, args, kwargs))
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
            except Exception as err:  # noqa: BLE001 — log then let the caller see it too
                log_calls.debug("<- %s RAISED %s: %s  (%.1fms)",
                                qualname, type(err).__name__, err,
                                (time.monotonic() - t0) * 1000)
                raise
            log_calls.debug("<- %s = %s  (%.1fms)",
                            qualname, _short(result), (time.monotonic() - t0) * 1000)
            return result
        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        if not log_calls.isEnabledFor(logging.DEBUG):
            return func(*args, **kwargs)
        log_calls.debug("-> %s", _format_call(qualname, args, kwargs))
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            log_calls.debug("<- %s RAISED %s: %s  (%.1fms)",
                            qualname, type(err).__name__, err,
                            (time.monotonic() - t0) * 1000)
            raise
        log_calls.debug("<- %s = %s  (%.1fms)",
                        qualname, _short(result), (time.monotonic() - t0) * 1000)
        return result
    return sync_wrapper


class FrameLogger:
    """Append-only JSONL sink for one algorithm run's frame-by-frame internals, meant to be
    replayed later by a visualization tool — NOT meant to be read as a log by a human (use
    `log_call` or the existing `antbot.route` DEBUG logging for that).

    Each `emit()` call writes one JSON object: `seq` (a monotonic counter — the frame
    index a visualizer steps through), `t` (wall-clock time), `kind` (what kind of event —
    "start"/"pop"/"relax"/"done"/"no_route", the vocabulary is up to the caller), plus
    whatever fields the caller passes. Tuples (tile coordinates) serialize as JSON arrays
    automatically; anything else unserializable falls back to `str()` rather than crashing
    a live bot over a logging convenience.

    Construct one PER SEARCH CALL you want recorded (see nav.find_shared_route's `frames`
    param) — there's no global enable/disable flag, because a search's frames only make
    sense in the context of the one call that produced them. `close()` when done, or use as
    a context manager.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._seq = 0

    def emit(self, kind: str, **fields: Any) -> None:
        self._seq += 1
        record = {"seq": self._seq, "t": time.time(), "kind": kind, **fields}
        self._fh.write(json.dumps(record, default=str) + "\n")

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "FrameLogger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
