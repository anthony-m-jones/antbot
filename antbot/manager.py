"""ColonyManager — a persistent, controllable supervisor for the bot swarm.

The `farm` command builds one of these and then idles; all the action is driven
by commands from the dashboard: "Log in & explore", "Stop & log out", "Reset to
temple". Because the dashboard's HTTP handler runs in a background thread while
the bots live on the asyncio event loop, commands cross threads via
`asyncio.run_coroutine_threadsafe`, which schedules a coroutine on our loop.

Commands are:
  start  — (re)launch an explorer for every configured bot that isn't running.
  stop   — tell every bot to finish its step and log out cleanly.
  reset  — stop everyone, move their characters back to the Thais temple in the
           database (a character's position only changes on login, so a "teleport
           to temple" is really log-out -> DB update -> log-in), then start again.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from .client import explore_session, hauler_session, scout_session

log = logging.getLogger("antbot")

# Thais temple ground floor — the colony's home / spawn.
TEMPLE = (32369, 32241, 7)

# Relog supervisor tuning. A bot's session ends for many reasons — the server
# dropped it, it stalled, or its character DIED (Canary logs a dead player out) —
# and we want it back online. But we must not hammer: Canary drops rapid logins
# from one IP, so we pace relogs and back off when a bot keeps failing/dying fast.
_RELOG_BASE = 5.0            # wait this long before the first relog after a run
_RELOG_MAX = 90.0           # cap the backoff between relogs
_HEALTHY_RUN_SECONDS = 30.0  # a session at least this long counts as "healthy"
_BACKOFF_FACTOR = 1.8       # multiply the wait each time a session ends too quickly

# reset-to-temple tuning. The reset is log-out -> DB position update -> log-in, but a
# character IN COMBAT can't log out (Canary's canLogout()), so its old in-memory
# session lingers (up to ~60s) and the relog RECONNECTS to it at the OLD position,
# ignoring our DB update. So we verify the reset took (are they near the temple?) and
# retry with a longer settle that outlasts combat + the ~60s stale-session removal.
_RESET_SETTLE = 6.0          # first pass: let clean logouts persist
_RESET_SETTLE_LONG = 65.0    # retry: outlast combat + the server's stale-session drop
_TEMPLE_RADIUS = 100         # Manhattan tiles from temple counted as "reset worked"
_RESET_VERIFY_TIMEOUT = 45.0  # how long to wait for bots back online near the temple

# Shared login queue. Confirmed by testing: one bot alone never gets dropped, but
# several logging in close together from the same IP do ("0 bytes read" — the server
# accepts the TCP connection then closes it, most likely DB contention on concurrent
# account/character loads). So ALL logins — initial and relog — pass through one gate
# that spaces them out: only one bot enters the login handshake at a time, at least
# _LOGIN_GAP_SECONDS apart. This replaces the old fixed per-bot stagger.
_LOGIN_GAP_SECONDS = 4.0


class ColonyManager:
    def __init__(self, colony, pool: list[tuple],
                 host: str, login_port: int, item_flags, rsa_n: int,
                 db_container: str = "otbr-db-1", db_user: str = "canary",
                 db_password: str = "canary", db_name: str = "canary",
                 record_mode: str = "flight",
                 default_scouts: int = 0, default_wanderers: int = 0,
                 default_haulers: int = 0) -> None:
        self.colony = colony
        # The account POOL a browser can draw bots from: [(account, password, character), …].
        # Roles are assigned at start time (from the dashboard's counts), NOT baked in here,
        # so the same pool can back any mix of scouts/wanderers.
        self.pool = pool
        # The roster currently meant to be running (each entry is pool spec + a role). Set by
        # `start()`; `start_all()`/reset restart exactly this set. Empty until the user starts.
        self._active_specs: list[tuple] = []
        # Initial values the dashboard's count inputs show (from the CLI, for convenience).
        self.default_scouts = default_scouts
        self.default_wanderers = default_wanderers
        self.default_haulers = default_haulers
        self.host = host
        self.login_port = login_port
        self.item_flags = item_flags
        self.rsa_n = rsa_n
        # Session recording (see recorder.py): "none" | "flight" | "full". Passed to each
        # bot's runner, which creates a per-session recorder. Default "flight" = a black
        # box that only writes to disk when a bot freezes/dies.
        self.record_mode = record_mode
        # DB access for the "reset to temple" command. Defaults match the Canary
        # docker quickstart; override if your setup differs.
        self._db = (db_container, db_user, db_password, db_name)
        self.loop: asyncio.AbstractEventLoop | None = None
        # character -> {"task": Task, "stop": asyncio.Event}
        self._bots: dict[str, dict] = {}
        self._busy = False       # ignore overlapping commands (e.g. double-clicks)
        self.status = "idle"     # short line the dashboard can show
        # Shared login queue: one bot in the login handshake at a time, spaced out.
        self._login_gate = asyncio.Lock()
        self._last_login = 0.0   # loop.time() of the most recent login start

    async def run(self) -> None:
        """Hold the event loop open. Bots are NOT auto-started — the dashboard's Start
        button (which calls `start`) launches them on demand. This is what lets the .bat
        just bring the observer up and hand control to the browser."""
        self.loop = asyncio.get_running_loop()
        while True:                       # commands drive everything from here on
            await asyncio.sleep(3600)

    # -- individual bot lifecycle -----------------------------------------

    async def _run_bot(self, index: int, account: str, password: str,
                       character: str | None, role: str, stop: asyncio.Event) -> None:
        """Keep this one bot online until asked to stop.

        Each pass logs the character in and runs its behaviour until the session
        ends — a clean stop, a disconnect, a stall, a login failure, or the
        character DYING (Canary logs a dead player out). Unless we were told to
        stop, we relog. We pace and back off so repeated fast failures/deaths don't
        hammer the server (which drops rapid logins from one IP); a session that ran
        healthily resets the backoff so a one-off drop relogs quickly.
        """
        runner = {"scout": scout_session, "hauler": hauler_session}.get(role, explore_session)
        fails = 0
        while not stop.is_set():
            # Pass through the shared login queue so the whole swarm never logs in as
            # a burst (which the server drops). Applies to the first login and every
            # relog alike.
            await self._space_login(stop)
            if stop.is_set():
                return
            started = self.loop.time() if self.loop is not None else 0.0
            try:
                await runner(self.host, self.login_port, account, password,
                             character, self.item_flags, self.colony,
                             duration=None, rsa_n=self.rsa_n, stop_event=stop,
                             record_mode=self.record_mode)
            except asyncio.CancelledError:
                raise
            except Exception as err:      # isolate one bot's failure from the rest
                log.warning("manager: bot %s session error: %s", character, err)
            if stop.is_set():
                return

            # The session ended on its own (drop / stall / death / login fail). Relog,
            # but decide how long to wait: quickly if it had run a while, backing off
            # (exponentially, capped) if it keeps ending almost immediately.
            ran = (self.loop.time() - started) if self.loop is not None else 0.0
            if ran >= _HEALTHY_RUN_SECONDS:
                fails = 0
            else:
                fails += 1
            # Per-bot jitter (index) so the whole swarm doesn't relog in lockstep and
            # re-trigger the same-IP drop.
            delay = min(_RELOG_BASE * (_BACKOFF_FACTOR ** fails), _RELOG_MAX) + index * 0.7
            log.info("manager: bot %s session ended after %.0fs; relogging in %.0fs (fails=%d)",
                     character, ran, delay, fails)
            await self._sleep_unless_stopped(delay, stop)

    @staticmethod
    async def _sleep_unless_stopped(delay: float, stop: asyncio.Event) -> None:
        """Sleep for `delay` seconds, but wake immediately if `stop` is set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _space_login(self, stop: asyncio.Event) -> None:
        """Block until it's this bot's turn to log in, per the shared login queue.

        Holding `_login_gate` serialises the swarm's logins (one handshake at a time)
        and, while held, sleeps out any remainder of `_LOGIN_GAP_SECONDS` since the
        previous login started — so N bots come online spaced ~that far apart instead
        of in a drop-inducing burst. We stamp `_last_login` when our turn begins so
        the NEXT bot measures its gap from us.
        """
        async with self._login_gate:
            if stop.is_set():
                return
            now = self.loop.time() if self.loop is not None else 0.0
            wait = _LOGIN_GAP_SECONDS - (now - self._last_login)
            if wait > 0:
                await self._sleep_unless_stopped(wait, stop)
            self._last_login = self.loop.time() if self.loop is not None else now

    # -- commands (run on the event loop) ---------------------------------

    def _pick_specs(self, scouts: int, wanderers: int, haulers: int = 0) -> list[tuple]:
        """Assign roles to pool accounts: wanderers, then scouts, then haulers.

        Capped at the pool size (can't run more bots than we have accounts for). Returns
        [(account, password, character, role), …].
        """
        specs: list[tuple] = []
        k = 0
        for role, n in (("explore", wanderers), ("scout", scouts), ("hauler", haulers)):
            for _ in range(max(0, n)):
                if k >= len(self.pool):
                    break
                a, p, c = self.pool[k]; specs.append((a, p, c, role)); k += 1
        return specs

    async def start(self, scouts: int, wanderers: int, haulers: int = 0) -> None:
        """Start the requested mix of bots from the pool (the dashboard Start button).

        Sets this as the active roster so reset-to-temple restarts the same set.
        """
        self._active_specs = self._pick_specs(scouts, wanderers, haulers)
        log.info("manager: start -> %d scout(s), %d wanderer(s), %d hauler(s) from a pool of %d",
                 scouts, wanderers, haulers, len(self.pool))
        await self._start_specs(self._active_specs)

    async def start_all(self) -> None:
        """(Re)start the currently active roster — used by reset-to-temple."""
        await self._start_specs(self._active_specs)

    async def _start_specs(self, specs: list[tuple]) -> None:
        started = 0
        for index, (account, password, character, role) in enumerate(specs):
            key = character or account
            existing = self._bots.get(key)
            if existing and not existing["task"].done():
                continue                  # already running
            stop = asyncio.Event()
            task = asyncio.create_task(self._run_bot(index, account, password, character, role, stop))
            self._bots[key] = {"task": task, "stop": stop}
            started += 1
        log.info("manager: %d bot(s) starting", started)

    async def stop_all(self) -> None:
        handles = list(self._bots.items())
        for _key, handle in handles:
            handle["stop"].set()          # ask each bot to log out cleanly
        pending = [h["task"] for _k, h in handles if not h["task"].done()]
        if pending:
            await asyncio.wait(pending, timeout=12)
        for _key, handle in handles:
            if not handle["task"].done():
                handle["task"].cancel()   # force any that didn't finish
        for key, _handle in handles:
            self.colony.remove_bot(key)   # drop them from the dashboard view
        self._bots.clear()
        log.info("manager: stop_all -> stopped %d bot(s)", len(handles))

    async def reset_to_temple(self) -> None:
        """Move every character back to the Thais temple, reliably.

        A single log-out -> DB-update -> log-in pass isn't enough: a character that
        can't log out (in combat) leaves a stale in-memory session that the relog
        reconnects to at the OLD position, so the DB update is silently ignored. We
        therefore verify (are they actually near the temple?) and retry with a longer
        settle that outlasts combat and the server's stale-session removal.
        """
        # Two passes: a quick one for characters that log out cleanly, then a patient
        # one whose long settle outlasts combat and the ~60s stale-session drop.
        for attempt, settle in enumerate((_RESET_SETTLE, _RESET_SETTLE_LONG), start=1):
            self.status = f"resetting (attempt {attempt})"
            await self.stop_all()
            await asyncio.sleep(settle)   # let logouts persist / stale sessions drop
            await self._reset_positions()
            await self.start_all()
            stuck = await self._verify_at_temple()
            if not stuck:
                log.info("manager: reset-to-temple succeeded on attempt %d", attempt)
                self.status = "idle"
                return
            log.warning("manager: reset attempt %d — still off-temple: %s", attempt, stuck)
        # A character that never reached the temple has an in-memory session that
        # won't die: it can't log out (Canary's canLogout() is false while it's in
        # combat outside a protection zone) so the relog keeps reconnecting to it at
        # its old spot, ignoring the DB (which already holds the temple position). No
        # DB reset can dislodge that — the character must die or the server restart.
        log.warning("manager: reset could not move %s (combat-locked in-memory session; "
                    "needs the character to die or a server restart)", stuck)
        self.status = f"reset incomplete: {len(stuck)} stuck"

    async def _verify_at_temple(self) -> list:
        """Return the configured characters still NOT near the temple ([] = success).

        Polls the colony's live positions (bots take a moment to log back in through
        the login queue). A character stuck at its old spot sits ~200 tiles away, far
        outside `_TEMPLE_RADIUS`, so this cleanly tells a real reset from a
        reconnect-to-stale-session.
        """
        chars = [c for (_a, _p, c, _r) in self._active_specs if c]
        if not chars:
            return []
        deadline = self.loop.time() + _RESET_VERIFY_TIMEOUT
        stuck = list(chars)
        while self.loop.time() < deadline and stuck:
            await asyncio.sleep(3.0)
            pos = self.colony.bot_positions()
            stuck = [
                name for name in chars
                if name not in pos
                or abs(pos[name][0] - TEMPLE[0]) + abs(pos[name][1] - TEMPLE[1]) > _TEMPLE_RADIUS
            ]
        return stuck

    async def _reset_positions(self) -> None:
        container, user, password, name = self._db
        # Reset the DB position of every character in the POOL (not just the running
        # ones) so a temple reset also cleans up any that logged out off-temple.
        chars = [c for (_a, _p, c) in self.pool if c]
        if not chars:
            return
        names = ",".join("'" + c.replace("'", "") + "'" for c in chars)
        sql = (f"UPDATE players SET posx={TEMPLE[0]}, posy={TEMPLE[1]}, posz={TEMPLE[2]} "
               f"WHERE name IN ({names});")
        cmd = ["docker", "exec", container, "mariadb", f"-u{user}", f"-p{password}", name, "-e", sql]
        result = await self.loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True, text=True))
        if result.returncode == 0:
            log.info("manager: reset %d character(s) to the Thais temple", len(chars))
        else:
            log.warning("manager: DB reset failed: %s", (result.stderr or "").strip()[:200])

    # -- command entry point from the dashboard thread --------------------

    def command(self, action: str, params: dict | None = None) -> dict:
        """Called from the HTTP (dashboard) thread; schedule the coroutine on our loop.

        `params` is the request's parsed query dict (values are lists, as from
        urllib parse_qs). "start" reads `scouts`/`wanderers` counts from it (falling back
        to the CLI defaults), so the browser drives how many of each role to launch.
        """
        params = params or {}

        def _count(name: str, default: int) -> int:
            try:
                return max(0, int(params.get(name, [default])[0]))
            except (ValueError, IndexError):
                return default

        if action == "start":
            scouts = _count("scouts", self.default_scouts)
            wanderers = _count("wanderers", self.default_wanderers)
            haulers = _count("haulers", self.default_haulers)
            if scouts + wanderers + haulers == 0:
                return {"error": "nothing to start (0 scouts, 0 wanderers, 0 haulers)"}
            def coro_factory(s=scouts, w=wanderers, h=haulers):
                return self.start(s, w, h)
        else:
            actions = {"stop": self.stop_all, "reset": self.reset_to_temple}
            coro_factory = actions.get(action)
        if coro_factory is None or self.loop is None:
            return {"error": f"unknown or unavailable action: {action}"}
        # Don't block the HTTP response on the whole (possibly long) command; kick
        # it off and let the live /state.json reflect the result as it happens.
        asyncio.run_coroutine_threadsafe(self._guarded(action, coro_factory), self.loop)
        return {"accepted": action}

    async def _guarded(self, action: str, coro_factory) -> None:
        if self._busy:
            log.info("manager: busy, ignoring '%s'", action)
            return
        self._busy = True
        self.status = f"running: {action}"
        try:
            await coro_factory()
            self.status = f"done: {action}"
        except Exception as err:
            log.error("manager: command '%s' failed: %s", action, err)
            self.status = f"error: {action}"
        finally:
            self._busy = False
