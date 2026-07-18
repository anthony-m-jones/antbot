"""CLI entry point.

    python -m antbot list --account test1 --password test
    python -m antbot pos  --account test1 --password test --character "Knight Noob 1"
    python -m antbot walk --account test1 --password test --character "Sorcerer Noob 1"

    python -m antbot path --account test1 --password test --character "Rook 1" --x 32072 --y 31904
    python -m antbot farm --account test1 --password test --character "Druid Noob 1" --duration 120

Commands:
    list  — log in, print the account's character list, disconnect.
    pos   — log a character in, print its position, log straight back out (A1).
    walk  — like pos, but random-walk for --duration seconds first.
    path  — A*-plan a route from the character's position to --x/--y (B1).
    goto  — actually walk the character to --x/--y, re-planning as it goes (B2).
    farm  — launch the ant-farm dashboard + explorer bot(s); watch them live (C).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import assets, wire
from .client import (LoginError, fetch_character_list, goto_session, path_session,
                     travel_session, walk_session)
from .colony import Colony
from .dashboard import start_dashboard
from .items import ItemFlags
from .manager import TEMPLE, ColonyManager


def _resolve_rsa(args: argparse.Namespace) -> int:
    pem = Path(args.key_pem)
    if pem.exists():
        n, e = wire.load_modulus_from_pem(pem)
        if e != wire.OTSERV_RSA_E:
            raise SystemExit(f"unexpected RSA exponent {e} in {pem}")
        if n != wire.OTSERV_RSA_N:
            logging.getLogger("antbot").info("using RSA modulus from %s (differs from builtin)", pem)
        return n
    return wire.OTSERV_RSA_N


def main() -> int:
    parser = argparse.ArgumentParser(prog="antbot", description="Headless 8.60 bot for Canary")
    parser.add_argument("command", choices=["list", "pos", "walk", "path", "goto", "travel", "farm"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--login-port", type=int, default=7171)
    parser.add_argument("--account", default=None,
                        help="account (required for all commands except farm, which uses --pool)")
    parser.add_argument("--password", required=True)
    parser.add_argument("--character", default=None, help="character name (default: first on account)")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to walk around (walk command)")
    parser.add_argument("--x", type=int, default=None, help="target x coordinate (path/goto/travel)")
    parser.add_argument("--y", type=int, default=None, help="target y coordinate (path/goto/travel)")
    parser.add_argument("--z", type=int, default=7, help="target floor (travel command)")
    parser.add_argument("--dashboard-port", type=int, default=8100, help="dashboard HTTP port (farm command)")
    parser.add_argument("--bots", type=int, default=1,
                        help="farm: number of home-bound wanderer bots (accounts test1..)")
    parser.add_argument("--scouts", type=int, default=0,
                        help="farm: number of scout bots that range far to map the world")
    parser.add_argument("--haulers", type=int, default=0,
                        help="farm: default number of hauler bots (move loot between tiles)")
    parser.add_argument("--record", choices=["none", "flight", "full"], default="flight",
                        help="farm: session recording. flight=black box (dump on freeze/death), "
                             "full=record every session end to end, none=off")
    parser.add_argument("--pool", type=int, default=6,
                        help="farm: size of the account pool (accounts test1..testN) the browser "
                             "can start bots from")
    parser.add_argument("--open-browser", action="store_true",
                        help="farm: open the dashboard in the default browser once it's up")
    parser.add_argument("--dump-frames", type=int, default=0,
                        help="hex-dump the first N raw world frames (needs -v); the capture oracle")
    # Canary asset locations. Defaults follow $ANTBOT_CANARY_DIR (see assets.py) so a
    # standalone antbot checkout works by setting that one env var; each flag overrides
    # its own path.
    parser.add_argument("--key-pem", default=str(assets.key_pem_path()),
                        help="path to canary key.pem to derive the RSA modulus")
    parser.add_argument("--appearances", default=str(assets.appearances_path()),
                        help="path to appearances.dat for item flags (map parsing)")
    parser.add_argument("--items-xml", default=str(assets.items_xml_path()),
                        help="path to canary items.xml (item names/weights/traversal)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    # Every command except `farm` connects a single named account; farm draws bots from
    # the test1..testN pool instead, so it doesn't need --account.
    if args.command != "farm" and not args.account:
        parser.error(f"--account is required for the '{args.command}' command")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    rsa_n = _resolve_rsa(args)

    try:
        if args.command == "list":
            characters = asyncio.run(
                fetch_character_list(args.host, args.login_port, args.account, args.password, rsa_n)
            )
            for c in characters:
                print(f"{c.name:24} {c.world:24} {c.ip}:{c.port}")
        elif args.command == "farm":
            # The ant farm: a persistent coordinator + dashboard. It brings the observer
            # up but does NOT auto-start bots — the dashboard's Start button launches them
            # on demand (choose how many scouts/wanderers in the browser). Runs until Ctrl+C.
            item_flags = ItemFlags.load(args.appearances)
            colony = Colony(item_flags, catalog_file=Path(args.items_xml))
            # Where self-directed haulers deliver what they find. The temple for now; the
            # real answer is the nearest DEPOT (they're shared per character, so staged
            # hauls beat one long trip to a vendor) once the merchant role exists.
            colony.stash = TEMPLE
            # The account POOL the browser can draw from: each account testK owns a
            # temple-spawned "Druid K". Roles are assigned at start time from the browser.
            pool = [(f"test{k}", args.password, f"Druid {k}") for k in range(1, max(1, args.pool) + 1)]
            manager = ColonyManager(colony, pool, args.host, args.login_port, item_flags, rsa_n,
                                    record_mode=args.record,
                                    default_scouts=args.scouts, default_wanderers=args.bots,
                                    default_haulers=args.haulers)
            start_dashboard(colony, manager, args.dashboard_port)
            url = f"http://127.0.0.1:{args.dashboard_port}"
            if args.open_browser:
                # start_dashboard has already bound the socket, so the page is reachable now.
                import webbrowser
                webbrowser.open(url)
            print(f"antbot observer up at {url} — click Start in the browser to launch bots.")
            try:
                asyncio.run(manager.run())
            except KeyboardInterrupt:
                pass
        else:
            # Map parsing needs item flags; load them once up front.
            item_flags = ItemFlags.load(args.appearances)
            if args.command in ("path", "goto"):
                if args.x is None or args.y is None:
                    raise SystemExit(f"{args.command} requires --x and --y")
                runner = path_session if args.command == "path" else goto_session
                asyncio.run(
                    runner(args.host, args.login_port, args.account, args.password,
                           args.character, args.x, args.y, item_flags, rsa_n,
                           dump_frames=args.dump_frames)
                )
            elif args.command == "travel":
                if args.x is None or args.y is None:
                    raise SystemExit("travel requires --x and --y (and optional --z)")
                # A fresh colony loads the persisted links; the login snapshot maps
                # the local area. Best for local/floor-change goals — a shared,
                # already-explored colony map (the farm) reaches far teleport goals.
                colony = Colony(item_flags, catalog_file=Path(args.items_xml))
                asyncio.run(
                    travel_session(args.host, args.login_port, args.account, args.password,
                                   args.character, args.x, args.y, args.z, item_flags, colony, rsa_n)
                )
            else:
                # "pos" is a walk session with zero walk time: log in, report
                # position + creatures (from the login snapshot), log out.
                duration = args.duration if args.command == "walk" else 0.0
                asyncio.run(
                    walk_session(args.host, args.login_port, args.account, args.password,
                                 args.character, duration, item_flags, rsa_n,
                                 dump_frames=args.dump_frames)
                )
    except LoginError as err:
        print(f"LOGIN ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
