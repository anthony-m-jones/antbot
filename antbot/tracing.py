"""tracing — opt-in verbose call/return logging and frame-by-frame algorithm traces,
unified into ONE recordable timeline.

Two closely related tools live here, both OFF by default and cheap enough when off that
shipping them baked into production code costs nothing unless someone deliberately asks
to see them:

  1. `@log_call` — wraps a function (sync or async) so every CALL and RETURN is visible:
     the function's qualified name, its arguments, its return value (or the exception it
     raised), how long it took, and (best-effort) where the bot physically was at the
     time. This can go to TWO independent places, either or both at once:
       - the `antbot.calls` logger at DEBUG (human-readable text) — turn on with
             logging.getLogger("antbot.calls").setLevel(logging.DEBUG)
       - the `EventRecorder` bound for the current context (see `bind_recorder`), as
         structured JSONL — regardless of whether the console logger is also on.
     Applied to the DECISION-MAKING and STATE-CHANGING functions across nav/client/colony
     (not every single getter/helper) — see each module for which ones and why.

  2. `EventRecorder` — an append-only JSONL sink, and the SAME sink both `log_call`'s
     call/return events and an algorithm's frame-by-frame internals (a node popped, an
     edge relaxed — see nav.find_shared_route) write into. One file, one chronological,
     nested timeline: which function was entered, what it called, what THAT function's
     search did internally frame by frame, and what everything returned — meant for
     `frame_viewer.html` to replay, not to be read directly.

HOW THE NESTING WORKS. A recorder is BOUND for "the current context" via `bind_recorder`,
which stores it in a `contextvars.ContextVar` — so `log_call` (and anything else that
wants to record) can find it without a single function signature anywhere needing a
`recorder=` parameter threaded through it (an earlier version of this DID thread a
`frames=` parameter through navigate_to/travel/_follow_shared_route/_walk_local by hand;
this replaces that entirely — cleaner, and it means a NEW recording-aware function never
needs its signature touched at all, just the decorator).

A second contextvar holds the current call STACK (a tuple of call ids). It is never
mutated in place — pushing sets a brand-new tuple and remembers a `Token` to restore the
old one on the way back out — so this stays correct even across concurrent asyncio tasks
(the live colony runs one per bot): each task's context is an independent snapshot at the
point it was created, and in-place mutation of a shared list would leak between them,
which is exactly the bug this tuple-and-token pattern avoids.

Every event a frame-emitting algorithm writes while it's running tags itself with
`current_call_id()` — which, because that algorithm is itself `@log_call`-decorated, is
exactly ITS OWN call id by the time its body actually runs. That's the whole trick that
lets frame_viewer.html group "these 40 pop/relax frames" under "the find_shared_route
call that produced them" with no extra bookkeeping anywhere else in nav.py.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
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


def _extract_position(args: tuple, kwargs: dict) -> list[int] | None:
    """Best-effort: is a GameSession or GameState hiding in this call's arguments? If so,
    grab its (x, y, z) so the timeline can show where the bot physically was when this
    call happened, without every recorded function needing to report it deliberately.
    Duck-typed (an object with a `.state.position`, or a `.position`, that itself has an
    `.x`) for the same reason `_short` type-checks by name — no import cycle.
    """
    for value in (*args, *kwargs.values()):
        state = getattr(value, "state", None)
        pos = getattr(state, "position", None) if state is not None else getattr(value, "position", None)
        if pos is not None and hasattr(pos, "x"):
            return [pos.x, pos.y, pos.z]
    return None


# ---------------------------------------------------------------------------------------
# Ambient recording context: which EventRecorder is active, and the current call stack.
# ---------------------------------------------------------------------------------------
_next_call_id = itertools.count(1).__next__
_CALL_STACK: contextvars.ContextVar[tuple[int, ...]] = contextvars.ContextVar(
    "_CALL_STACK", default=())
_CURRENT_RECORDER: contextvars.ContextVar["EventRecorder | None"] = contextvars.ContextVar(
    "_CURRENT_RECORDER", default=None)


def bind_recorder(recorder: "EventRecorder") -> contextvars.Token:
    """Make `recorder` the active sink for every @log_call and frame-emitting call in the
    current context from here on (see the module docstring for what "context" buys you
    with concurrent bots). Returns a token — pass it to `unbind_recorder` when done
    (typically at the end of a test run); this restores whatever was bound before
    (nothing, normally)."""
    return _CURRENT_RECORDER.set(recorder)


def unbind_recorder(token: contextvars.Token) -> None:
    _CURRENT_RECORDER.reset(token)


def current_recorder() -> "EventRecorder | None":
    """The recorder bound for this context, or None if nobody's asked to record anything
    right now — the fast path every recording-aware call checks first."""
    return _CURRENT_RECORDER.get()


def current_call_id() -> int | None:
    """The call id of whichever @log_call-wrapped function is currently executing in this
    context (the innermost one), or None outside any tracked call. This is how a frame-
    emitting algorithm (find_shared_route et al.) tags its own frames with the call that
    produced them, without a call id ever being threaded into it as a parameter."""
    stack = _CALL_STACK.get()
    return stack[-1] if stack else None


def _enter(qualname: str, args: tuple, kwargs: dict) -> tuple[int, contextvars.Token]:
    parent_id = current_call_id()
    call_id = _next_call_id()
    token = _CALL_STACK.set(_CALL_STACK.get() + (call_id,))
    if log_calls.isEnabledFor(logging.DEBUG):
        log_calls.debug("-> %s", _format_call(qualname, args, kwargs))
    recorder = current_recorder()
    if recorder is not None:
        recorder.emit("call", call_id=call_id, parent_id=parent_id,
                      depth=len(_CALL_STACK.get()) - 1, qualname=qualname,
                      args=[_short(a) for a in args],
                      kwargs={k: _short(v) for k, v in kwargs.items()},
                      position=_extract_position(args, kwargs))
    return call_id, token


def _exit_returned(qualname: str, call_id: int, result: Any, t0: float) -> None:
    duration_ms = (time.monotonic() - t0) * 1000
    if log_calls.isEnabledFor(logging.DEBUG):
        log_calls.debug("<- %s = %s  (%.1fms)", qualname, _short(result), duration_ms)
    recorder = current_recorder()
    if recorder is not None:
        recorder.emit("return", call_id=call_id, result=_short(result),
                      duration_ms=round(duration_ms, 2))


def _exit_raised(qualname: str, call_id: int, err: Exception, t0: float) -> None:
    duration_ms = (time.monotonic() - t0) * 1000
    if log_calls.isEnabledFor(logging.DEBUG):
        log_calls.debug("<- %s RAISED %s: %s  (%.1fms)",
                        qualname, type(err).__name__, err, duration_ms)
    recorder = current_recorder()
    if recorder is not None:
        recorder.emit("raise", call_id=call_id, error_type=type(err).__name__,
                      error_message=str(err), duration_ms=round(duration_ms, 2))


def log_call(func: Callable) -> Callable:
    """Log this function's call (name + args + position, best-effort) and its return
    value/exception — to the `antbot.calls` DEBUG logger, to the EventRecorder bound for
    this context (see `bind_recorder`), or both, whichever are active. A no-op — one
    cheap check — when NEITHER is active. Works transparently for both `async def` and
    plain functions; re-raises exceptions after logging them, so this never changes what
    a caller observes, only what gets recorded.
    """
    qualname = func.__qualname__

    def _active() -> bool:
        return log_calls.isEnabledFor(logging.DEBUG) or current_recorder() is not None

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            if not _active():
                return await func(*args, **kwargs)
            call_id, token = _enter(qualname, args, kwargs)
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
            except Exception as err:  # noqa: BLE001 — log then let the caller see it too
                _exit_raised(qualname, call_id, err, t0)
                _CALL_STACK.reset(token)
                raise
            _exit_returned(qualname, call_id, result, t0)
            _CALL_STACK.reset(token)
            return result
        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        if not _active():
            return func(*args, **kwargs)
        call_id, token = _enter(qualname, args, kwargs)
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            _exit_raised(qualname, call_id, err, t0)
            _CALL_STACK.reset(token)
            raise
        _exit_returned(qualname, call_id, result, t0)
        _CALL_STACK.reset(token)
        return result
    return sync_wrapper


class EventRecorder:
    """Append-only JSONL sink for ONE unified timeline: `log_call`'s call/return/raise
    events AND an algorithm's frame-by-frame internals (a node popped, an edge relaxed —
    see nav.find_shared_route), all in one file, meant to be replayed later by
    `frame_viewer.html` — NOT meant to be read directly by a human (use the `antbot.calls`
    console logging for that instead, or alongside).

    Each `emit()` call writes one JSON object: `seq` (a monotonic counter — global across
    EVERY event this recorder writes, calls and frames alike, so the file is one true
    chronological order), `t` (wall-clock time), `kind` (the event's vocabulary is up to
    the caller — "call"/"return"/"raise" from `log_call`, "start"/"pop"/"relax"/"done"/
    "no_route" from a frame-emitting algorithm), plus whatever fields the caller passes.
    A frame event's `call_id` (see `current_call_id`) is what lets a viewer group "these
    40 pop/relax frames" under the specific `find_shared_route` call that produced them.

    Bind ONE recorder for the whole run you want captured (see `bind_recorder`) — there's
    no need to construct a new one per call the way an earlier version of this required;
    `current_call_id()`'s nesting is what tells events apart, not separate files.
    `close()` when done, or use as a context manager.
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

    def __enter__(self) -> "EventRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
