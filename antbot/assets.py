"""Where to find the Canary asset files the bot reads.

antbot is a headless CLIENT, but three files that ship with the Canary SERVER are the
bot's source of truth for items and login:

  - ``data/items/appearances.dat`` — client asset: per-item flags used to parse the map
    (walkable? blocking? stackable? takeable? minimap colour?).
  - ``data/items/items.xml`` — server catalog: item names + weight + traversal behaviour,
    joined to appearances.dat by the shared item id.
  - ``key.pem`` — the server's RSA key (the standard OTServ key; the bot has the same
    modulus built in, so this is only used when present).

In this workspace the ``canary`` checkout sits two directories up, as a sibling of the
antbot tree. Published on its own, antbot won't have that neighbour — so set the
``ANTBOT_CANARY_DIR`` environment variable to your Canary checkout and every path below
follows from it. One knob, not three. The CLI flags (``--appearances`` / ``--items-xml``
/ ``--key-pem``) still override individual paths when you need to.
"""
from __future__ import annotations

import os
from pathlib import Path


def canary_dir() -> Path:
    """The Canary checkout root. ``ANTBOT_CANARY_DIR`` wins; else the sibling layout."""
    env = os.environ.get("ANTBOT_CANARY_DIR")
    if env:
        return Path(env)
    # Fallback for this workspace: …/<root>/canary alongside …/<root>/antbot.
    return Path(__file__).resolve().parents[2] / "canary"


def appearances_path() -> Path:
    return canary_dir() / "data" / "items" / "appearances.dat"


def items_xml_path() -> Path:
    return canary_dir() / "data" / "items" / "items.xml"


def key_pem_path() -> Path:
    return canary_dir() / "key.pem"
