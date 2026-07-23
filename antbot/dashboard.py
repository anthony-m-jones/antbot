"""The ant-farm web dashboard (Phase C).

A tiny stdlib HTTP server (no framework) that serves one page and one JSON feed:

    GET /            -> the dashboard page (canvas + polling JS, embedded below)
    GET /state.json  -> Colony.snapshot(), the live state the page draws

It runs in a daemon thread so the asyncio bots keep running in the main thread;
the request handler reads the Colony under its lock. The page polls the feed a
few times a second and redraws: the shared explored map as coloured tiles (Tibia
minimap colours) with each bot as a bright dot on top. Watching it, you see the
world colour itself in as the colony explores.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .colony import Colony

log = logging.getLogger("antbot")

# The page is static; only /state.json changes. Kept inline so the whole
# dashboard is dependency-free and self-contained.
# The dashboard page now lives in a separate file (antbot/dashboard.html) — the design
# ported from the UI mockup. We read it on each request so the page can be iterated
# without restarting the server; the endpoints below feed it real data.
_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html")


def _page() -> bytes:
    """Serve dashboard.html (read per-request for easy iteration)."""
    try:
        return _DASHBOARD_HTML.read_bytes()
    except OSError:
        return b"<h1>dashboard.html not found next to dashboard.py</h1>"


def _packet_list(sess) -> dict:
    """Packet-list rows for the inspector: one summary per buffered frame.

    Cheap: decode each frame only far enough to name its lead opcode. Newest first.
    """
    if sess is None:
        return {"capturing": False, "found": False, "frames": []}
    from .packet_decode import summarize
    rows = [{"seq": seq, "t": round(t, 3), "dir": direction, "size": len(payload),
             "op": summarize(payload, direction)}
            for seq, t, direction, payload in sess.frames_raw()]
    rows.reverse()   # newest first
    return {"capturing": sess.capture_frames, "found": True, "frames": rows}


def _packet_decode(sess, seq: int) -> dict:
    """Full decode (hex + parse tree) of one buffered frame by seq."""
    if sess is None:
        return {"error": "bot not found or offline"}
    got = sess.frame_by_seq(seq)
    if got is None:
        return {"error": "frame aged out of the buffer"}
    from .packet_decode import decode_frame
    direction, payload = got
    return decode_frame(payload, direction, item_flags=sess.item_flags)


def _route_query(colony: Colony, qs: dict) -> dict:
    """Plan a teleport-aware route from the lead bot to a queried tile."""
    from .nav import find_route
    try:
        gx = int(qs["x"][0]); gy = int(qs["y"][0]); gz = int(qs.get("z", ["7"])[0])
    except (KeyError, ValueError):
        return {"error": "need integer x, y (z optional)"}
    snap = colony.snapshot()
    if not snap["bots"]:
        return {"error": "no bots online to route from"}
    lead = snap["bots"][0]
    start = (lead["x"], lead["y"], lead["z"])
    route = find_route(colony.get_walkable(), colony.get_links(), start, (gx, gy, gz))
    if route is None:
        return {"start": start, "goal": [gx, gy, gz], "found": False,
                "known_links": len(colony.get_links())}
    return {"start": start, "goal": [gx, gy, gz], "found": True, "steps": len(route),
            "teleports_used": [list(step[2]) for step in route if step[1]]}


def _plan_query(colony: Colony, qs: dict) -> dict:
    """Plan a route between two arbitrary points for the visual path inspector."""
    try:
        start = (int(qs["sx"][0]), int(qs["sy"][0]), int(qs["sz"][0]))
        goal = (int(qs["ex"][0]), int(qs["ey"][0]), int(qs["ez"][0]))
        speed = int(qs.get("speed", ["200"])[0])
    except (KeyError, ValueError, IndexError):
        return {"error": "need integer sx,sy,sz,ex,ey,ez (speed optional)"}
    return colony.plan_route(start, goal, speed)


def _task_query(colony: Colony, qs: dict) -> dict:
    """Post a task on the colony blackboard (the hauler's work queue).

    `/task?action=add&px=&py=&pz=&dx=&dy=&dz=[&note=]` posts a haul: collect whatever is
    on the pickup tile and deliver it to the dropoff tile. `action=list` just returns the
    board. This is how work gets in for now; later the colony will post its own.
    """
    action = qs.get("action", ["add"])[0]
    if action == "list":
        return {"tasks": colony.tasks_snapshot()}
    if action != "add":
        return {"error": f"unknown task action: {action}"}
    try:
        pickup = (int(qs["px"][0]), int(qs["py"][0]), int(qs["pz"][0]))
        dropoff = (int(qs["dx"][0]), int(qs["dy"][0]), int(qs["dz"][0]))
    except (KeyError, ValueError, IndexError):
        return {"error": "need integer px,py,pz and dx,dy,dz"}
    note = qs.get("note", [""])[0]
    task = colony.add_task("haul", pickup, dropoff, note=note)
    return {"added": task.id, "kind": task.kind,
            "pickup": list(task.pickup), "dropoff": list(task.dropoff)}


def _list_recordings() -> list[dict]:
    """Metadata for every session recording on disk, newest first.

    Cheap: reads only each file's header line for the bot/start/mode and uses the file
    size + mtime for duration, so listing stays fast even with many recordings.
    """
    from .recorder import RECORDINGS_DIR
    out = []
    if not RECORDINGS_DIR.exists():
        return out
    for path in RECORDINGS_DIR.glob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                header = json.loads(fh.readline() or "{}")
            stat = path.stat()
            start = header.get("start", stat.st_mtime)
            out.append({
                "file": path.name,
                "bot": header.get("bot", path.stem),
                "mode": header.get("mode", "?"),
                "trigger": header.get("trigger"),   # set on flight-dumps
                "start": start,
                "duration": max(0.0, stat.st_mtime - start),
                "size": stat.st_size,
            })
        except (OSError, ValueError):
            continue
    out.sort(key=lambda r: r["start"], reverse=True)
    return out


def _load_recording(name: str) -> dict:
    """Parse one recording file into {header, records}. Rejects path traversal."""
    from .recorder import RECORDINGS_DIR
    # Basename only — never let a caller escape the recordings directory.
    safe = Path(name).name
    path = (RECORDINGS_DIR / safe).resolve()
    if path.parent != RECORDINGS_DIR.resolve() or not path.exists():
        return {"error": "no such recording"}
    header, records = {}, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "header":
            header = obj
        else:
            records.append(obj)
    return {"header": header, "records": records}


def _make_handler(colony: Colony, manager=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            if parsed.path == "/state.json":
                # ?focus=<bot name> follows a specific bot (from a dashboard click).
                focus = parse_qs(parsed.query).get("focus", [None])[0]
                snap = colony.snapshot(focus_name=focus)
                snap["manager"] = manager.status if manager else None
                # Pool size + default counts so the Start controls can size themselves.
                if manager is not None:
                    snap["pool_size"] = len(getattr(manager, "pool", []))
                    snap["default_scouts"] = getattr(manager, "default_scouts", 0)
                    snap["default_wanderers"] = getattr(manager, "default_wanderers", 0)
                    snap["default_haulers"] = getattr(manager, "default_haulers", 0)
                # The blackboard: work posted for role-bots (the hauler consumes these).
                snap["tasks"] = colony.tasks_snapshot()
                # The loot map: what the swarm has SEEN worth fetching, best first.
                snap["loot"] = colony.loot_snapshot(limit=50)
                self._send(200, "application/json", json.dumps(snap).encode("utf-8"))
            elif parsed.path == "/overview.json":
                # ?z=<floor>&block=<n> : the WHOLE floor, downsampled into blocks, so the
                # page can draw the colony's true extent when zoomed out. /state.json only
                # ships a window around the followed bot; this is the wide shot. Polled
                # rarely (it's much bigger), hence a separate endpoint rather than bloating
                # every 400ms state poll.
                q = parse_qs(parsed.query)
                z = int(q.get("z", [7])[0])
                block = int(q.get("block", [4])[0])
                self._send(200, "application/json",
                           json.dumps(colony.overview(z, block=block)).encode("utf-8"))
            elif parsed.path == "/command":
                # Control buttons post here: ?action=start|stop|reset (start also takes
                # ?scouts=&wanderers= so the browser chooses how many of each to launch).
                q = parse_qs(parsed.query)
                action = q.get("action", [""])[0]
                result = manager.command(action, q) if manager else {"error": "no manager"}
                self._send(200, "application/json", json.dumps(result).encode("utf-8"))
            elif parsed.path == "/route":
                # ?x=&y=&z= : plan a world-wide route (using teleport shortcuts)
                # from the lead bot to that tile, over everyone's shared map.
                self._send(200, "application/json",
                           json.dumps(_route_query(colony, parse_qs(parsed.query))).encode("utf-8"))
            elif parsed.path == "/map":
                # ?z= : every explored tile on that floor, for the path-planner view.
                q = parse_qs(parsed.query)
                try:
                    z = int(q.get("z", ["7"])[0])
                except ValueError:
                    z = 7
                self._send(200, "application/json",
                           json.dumps({"z": z, "tiles": colony.explored_floor(z)}).encode("utf-8"))
            elif parsed.path == "/plan":
                # ?sx=&sy=&sz=&ex=&ey=&ez=&speed= : plan a route between two arbitrary
                # points (the visual path inspector — NOT sent to any bot).
                self._send(200, "application/json",
                           json.dumps(_plan_query(colony, parse_qs(parsed.query))).encode("utf-8"))
            elif parsed.path == "/task":
                # Post work on the blackboard for a hauler to claim:
                #   /task?action=add&px=&py=&pz=&dx=&dy=&dz=[&note=]
                self._send(200, "application/json",
                           json.dumps(_task_query(colony, parse_qs(parsed.query))).encode("utf-8"))
            elif parsed.path == "/debug.json":
                # ?focus=<bot>&r=<radius> : categorised blocked tiles around a bot, for
                # the block-inspector overlay. A debug read, fetched only while the
                # overlay is on, so classifying a window each poll is fine.
                q = parse_qs(parsed.query)
                focus = q.get("focus", [None])[0]
                try:
                    r = max(10, min(130, int(q.get("r", ["60"])[0])))
                except ValueError:
                    r = 60
                self._send(200, "application/json",
                           json.dumps(colony.debug_tiles(focus, r)).encode("utf-8"))
            elif parsed.path == "/packets":
                # Toggle the packet inspector for a bot: ?bot=<name>&on=1|0
                q = parse_qs(parsed.query)
                name = q.get("bot", [None])[0]
                on = q.get("on", ["0"])[0] in ("1", "true", "on")
                sess = colony.session_for(name) if name else None
                if sess is not None:
                    sess.set_capture(on)
                self._send(200, "application/json",
                           json.dumps({"bot": name, "capturing": on and sess is not None,
                                       "found": sess is not None}).encode("utf-8"))
            elif parsed.path == "/packets.json":
                # The packet-list rows for a bot: ?bot=<name> -> [{seq,t,dir,size,op}].
                q = parse_qs(parsed.query)
                sess = colony.session_for(q.get("bot", [None])[0])
                self._send(200, "application/json",
                           json.dumps(_packet_list(sess)).encode("utf-8"))
            elif parsed.path == "/packet":
                # One decoded frame: ?bot=<name>&seq=<n> -> the hex + parse tree.
                q = parse_qs(parsed.query)
                sess = colony.session_for(q.get("bot", [None])[0])
                try:
                    seq = int(q.get("seq", ["0"])[0])
                except ValueError:
                    seq = 0
                self._send(200, "application/json",
                           json.dumps(_packet_decode(sess, seq)).encode("utf-8"))
            elif parsed.path == "/recordings":
                # List available session recordings (for the replay picker).
                self._send(200, "application/json",
                           json.dumps({"recordings": _list_recordings()}).encode("utf-8"))
            elif parsed.path == "/recording":
                # ?file= : the parsed records of one recording, for replay playback.
                name = parse_qs(parsed.query).get("file", [""])[0]
                self._send(200, "application/json",
                           json.dumps(_load_recording(name)).encode("utf-8"))
            elif parsed.path == "/" or parsed.path.startswith("/index"):
                self._send(200, "text/html; charset=utf-8", _page())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # keep the console clean; the bots do their own logging

    return Handler


def start_dashboard(colony: Colony, manager=None, port: int = 8100) -> ThreadingHTTPServer:
    """Start the dashboard server in a daemon thread; return the server object."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(colony, manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard")
    thread.start()
    log.info("dashboard: open http://127.0.0.1:%d in a browser", port)
    return server
