"""GM ("god") helpers — operator tooling that drives the built-in GOD character to set up
test scenarios INSTANTLY, without a server restart.

The stock Canary server ships a ready-made GOD character — account "god" / password "god",
character "GOD", in the god group (full access) — and we install one tiny custom talkaction
(`/tpto`, see TPTO_SCRIPT) that teleports any ONLINE player to exact coordinates. GOD can
then place a test bot at a known start tile with a single say-command, replacing the slow
stop-server / DB-teleport / start-server dance for any test that doesn't need a full map
reload.

This is TEST / OPERATOR tooling ONLY. The colony bots never use GM powers — they play by
normal-player rules. GOD exists purely to position a test bot.

Two custom talkactions back this (source kept below in TPTO_SCRIPT / SETTILE_SCRIPT):
  /tpto PlayerName, x, y, z          -- teleport an online player to exact coords
  /settile x, y, z, fromId, toId     -- idempotently transform a tile item (reset a door)

Install / persistence: these live on the HOST at canary/docker/custom-scripts/{tpto,settile}.lua
and are bind-mounted into the server container by docker-compose.yml:

    volumes:
      - './custom-scripts:/canary/data/scripts/talkactions/custom'

Because it's a bind mount (not a copy into the image's writable layer), the scripts
re-attach on every `docker compose up` and so survive `docker compose down/up` — which
recreates the container from the image and would wipe anything `docker cp`-ed into /canary.
Canary auto-loads every .lua under data/scripts/ on startup, so the only requirement to pick
up an EDIT to these files is a server restart (/reload is disabled on this build):

    docker compose -p otbr up -d --force-recreate server   # or: docker restart otbr-server-1

The TPTO_SCRIPT / SETTILE_SCRIPT constants below are the source of truth; keep the mounted
host files in sync with them.
"""
from __future__ import annotations

import asyncio
import logging

from .items import ItemFlags

log = logging.getLogger("antbot.gm")

GM_ACCOUNT = "god"
GM_PASSWORD = "god"
GM_CHARACTER = "GOD"

# The talkaction we install (kept here as the source of truth; scratchpad/tpto.lua is a
# copy). god-only; teleports a named online player to exact coords.
TPTO_SCRIPT = '''\
-- Custom GM teleport: move a named ONLINE player to EXACT coordinates, instantly.
--   Usage:  /tpto PlayerName, x, y, z
-- Installed for the antbot test harness (see antbot/gm.py). God-only.
local tpto = TalkAction("/tpto")

function tpto.onSay(player, words, param)
\tlocal parts = param:split(",")
\tif not parts[4] then
\t\tplayer:sendCancelMessage("Usage: /tpto PlayerName, x, y, z")
\t\treturn true
\tend
\tlocal name = parts[1]:gsub("^%s+", ""):gsub("%s+$", "")
\tlocal target = Player(name)
\tif not target then
\t\tplayer:sendCancelMessage('Player "' .. name .. '" is not online.')
\t\treturn true
\tend
\tlocal dest = Position(tonumber(parts[2]), tonumber(parts[3]), tonumber(parts[4]))
\tif not dest:getTile() then
\t\tplayer:sendCancelMessage("Destination tile does not exist.")
\t\treturn true
\tend
\ttarget:teleportTo(dest, false)
\tplayer:sendTextMessage(MESSAGE_EVENT_ADVANCE,
\t\tstring.format("tpto: %s -> %d, %d, %d", target:getName(), dest.x, dest.y, dest.z))
\treturn true
end

tpto:separator(" ")
tpto:groupType("god")
tpto:register()
'''

# Idempotent tile-item transform: reset a door (or any item) to a known state, no restart.
SETTILE_SCRIPT = '''\
-- /settile x, y, z, fromId, toId  — if the tile carries fromId change it to toId; if it
-- already carries toId do nothing. Installed for the antbot test harness. God-only.
local settile = TalkAction("/settile")

function settile.onSay(player, words, param)
\tlocal p = param:split(",")
\tif not p[5] then
\t\tplayer:sendCancelMessage("Usage: /settile x, y, z, fromId, toId")
\t\treturn true
\tend
\tlocal pos = Position(tonumber(p[1]), tonumber(p[2]), tonumber(p[3]))
\tlocal tile = Tile(pos)
\tif not tile then
\t\tplayer:sendCancelMessage("settile: no tile there.")
\t\treturn true
\tend
\tlocal fromId, toId = tonumber(p[4]), tonumber(p[5])
\tif tile:getItemById(toId) then
\t\tplayer:sendTextMessage(MESSAGE_EVENT_ADVANCE,
\t\t\tstring.format("settile: %d,%d,%d already %d", pos.x, pos.y, pos.z, toId))
\t\treturn true
\tend
\tlocal item = tile:getItemById(fromId)
\tif item then
\t\titem:transform(toId)
\t\tplayer:sendTextMessage(MESSAGE_EVENT_ADVANCE,
\t\t\tstring.format("settile: %d,%d,%d %d -> %d", pos.x, pos.y, pos.z, fromId, toId))
\telse
\t\tplayer:sendCancelMessage(string.format(
\t\t\t"settile: tile %d,%d,%d has neither %d nor %d", pos.x, pos.y, pos.z, fromId, toId))
\tend
\treturn true
end

settile:separator(" ")
settile:groupType("god")
settile:register()
'''


async def connect_retry(item_flags: ItemFlags, account: str, password: str, character: str,
                        host: str = "127.0.0.1", login_port: int = 7171, tries: int = 5):
    """Open a game session, retrying with backoff. Canary's login server drops the
    connection (0-byte reply -> IncompleteReadError) when hit too fast from one IP; a short
    wait clears the throttle window. Used by the test harness, which logs several
    characters in per run."""
    from . import client, wire
    last: Exception | None = None
    for i in range(tries):
        try:
            return await client._connect_session(host, login_port, account, password,
                                                 character, item_flags, wire.OTSERV_RSA_N, 0)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, EOFError) as err:
            last = err
            log.warning("connect %s failed (%s); retry %d/%d",
                        character, type(err).__name__, i + 1, tries)
            await asyncio.sleep(2 + i * 2)   # 2, 4, 6, 8s backoff
    raise last if last else RuntimeError("connect failed")


async def gm_run(item_flags: ItemFlags, *commands: str,
                 host: str = "127.0.0.1", login_port: int = 7171) -> list[str]:
    """Log GOD in, `say` each command in order, log out. Returns the server's recent reply
    lines (so callers can check acks). One GOD login runs the whole batch — teleport, door
    resets, whatever — which keeps setup to a single round trip. GOD has god access, so
    /tpto, /settile, /kick etc. all work. Everything here is TEST tooling; the colony bots
    never touch it.
    """
    from . import client   # local import: avoid a cycle at module load
    session = await connect_retry(item_flags, GM_ACCOUNT, GM_PASSWORD, GM_CHARACTER,
                                  host=host, login_port=login_port)
    replies: list[str] = []

    async def action(sess) -> None:
        await asyncio.sleep(0.4)          # let the login snapshot settle
        for cmd in commands:
            await sess.say(cmd)
            await asyncio.sleep(0.8)      # a beat for the server to act + reply
        replies.extend(msg for _cls, msg in sess.messages)

    await client._run_in_world(session, action)
    return replies


async def gm_teleport(item_flags: ItemFlags, name: str, x: int, y: int, z: int,
                      host: str = "127.0.0.1", login_port: int = 7171) -> bool:
    """Teleport the ONLINE player `name` to (x, y, z). True iff the server acknowledged.

    The target MUST already be online — `/tpto` resolves it with `Player(name)`. Instant and
    leaves the world map untouched (unlike a restart), the fast path for positioning a bot.
    """
    replies = await gm_run(item_flags, f"/tpto {name}, {int(x)}, {int(y)}, {int(z)}",
                           host=host, login_port=login_port)
    ok = any("tpto:" in r for r in replies)
    if not ok:
        log.warning("gm_teleport: no ack for %s -> (%d,%d,%d); online + /tpto installed?",
                    name, x, y, z)
    return ok


async def gm_kick(item_flags: ItemFlags, name: str,
                  host: str = "127.0.0.1", login_port: int = 7171) -> None:
    """Force the player `name` OFFLINE (stock /kick -> Player:remove()). Best-effort: if
    they've already logged out it's a harmless 'Player not found'. Used in test teardown so
    a bot whose graceful logout the server declined (combat/PZ rules) doesn't linger."""
    await gm_run(item_flags, f"/kick {name}", host=host, login_port=login_port)


async def gm_settile(item_flags: ItemFlags, x: int, y: int, z: int,
                     from_id: int, to_id: int,
                     host: str = "127.0.0.1", login_port: int = 7171) -> bool:
    """Idempotently set the tile item at (x, y, z) to `to_id`, transforming `from_id` if
    present (see the /settile talkaction). Resets a door to a known state with no restart.
    True iff the tile ended up as `to_id`."""
    replies = await gm_run(item_flags, f"/settile {x}, {y}, {z}, {from_id}, {to_id}",
                           host=host, login_port=login_port)
    return any("settile:" in r and ("already %d" % to_id in r or "-> %d" % to_id in r)
               for r in replies)
