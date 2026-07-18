# antbot

Headless Tibia **8.60** clients for the local Canary server — the foundation of
the ant colony. Pure Python (3.12+, stdlib only), asyncio-based so one process
can eventually host many bots.

## Why 8.60?

Canary accepts three client protocols concurrently in the same game world:
15.25 (port 7172), 11.00 (7174) and 8.60 (7175). The 8.60 wire protocol is by
far the simplest (single-byte opcodes, small packets), so bots speak 8.60 while
a human observes with the graphical 15.25 OTClient.

## Setup

**1. The server — stock OpenTibiaBR Canary, no build.** You need Docker + Docker
Compose. Clone [`opentibiabr/canary`](https://github.com/opentibiabr/canary), and from
its `docker/` directory copy the example env to `.env` and start it:

```bash
cd canary/docker
cp .env.example .env        # defaults are fine for local play
docker compose up -d
```

This *pulls* the official `ghcr.io/opentibiabr/canary:latest` image (nothing is compiled
locally), auto-downloads the ~184 MB `otservbr.otbm` map, and — because
`CANARY_TEST_ACCOUNTS=true` — seeds the accounts the bots use: **`test1`…`test15`,
password `test`**. Ports: **7171** login, **7172** game (15.25), **7175** game (8.60).

No server code changes are required. 8.60 support is stock: `allowOldProtocol = true` is
Canary's default, and the RSA key is the standard OTServ key (antbot has the same modulus
built in). A human who wants to *watch* the world can point a 15.25 OTClient at
`127.0.0.1` — it auto-downloads its own assets — but the bots need no graphical client;
they are the client.

**2. Canary asset files.** antbot reads three files from the Canary checkout at runtime —
`data/items/appearances.dat` (map/item parsing), `data/items/items.xml` (item catalog),
and `key.pem` (RSA, optional). If your `canary` checkout is **not** the sibling directory
this workspace assumes, point antbot at it with one environment variable:

```bash
export ANTBOT_CANARY_DIR=/path/to/canary      # Windows: set ANTBOT_CANARY_DIR=C:\path\to\canary
```

(Or override individual paths with `--appearances`, `--items-xml`, `--key-pem`.)

**3. Run.** Python 3.12+, standard library only — no `pip install`:

```bash
python -m antbot farm --password test --pool 6 --scouts 3 --open-browser
```

## Usage

```powershell
# List characters on an account (classic login, port 7171)
python -m antbot list --account test1 --password test

# Log a character in and walk randomly for 30 seconds
python -m antbot walk --account test1 --password test --character "Knight Noob 1" --duration 30
```

Seeded docker accounts: `test1` … `test15` and `dawn`, password `test`.
Note: the *classic* login uses the account **name**; the HTTP login-server
(used by the 15.25 client) uses the email form `@test1`.

## Layout

- `antbot/wire.py` — framing, Adler-32, XTEA, RSA, message reader/writer.
  Docstrings note which Canary source file defines each layout.
- `antbot/client.py` — account login, game handshake (challenge → RSA login),
  walk/ping/logout, receive loop.
- `antbot/__main__.py` — CLI. Reads the RSA modulus from `../canary/key.pem`
  when present (falls back to the classic OTServ key, which is the same).

## Protocol cheat sheet (LegacyClassic transport)

```
frame  = [u16 outer length][u32 adler32][body]
body   = XTEA([u16 inner length][payload][pad to 8])   after key exchange
       = plaintext                                      first packet / challenge

login (7171):  0x01 os:u16 ver:u16 dat:u32 spr:u32 pic:u32 RSA128(0x00 xtea[16]
               account:str password:str pad)
game  (7175):  server sends 0x1F ts:u32 rand:u8 first; client answers
               0x0A os:u16 ver:u16 RSA128(0x00 xtea[16] gm:u8 account:str
               character:str password:str ts:u32 rand:u8 pad)
walk opcodes:  0x65 north, 0x66 east, 0x67 south, 0x68 west; 0x14 logout;
               0x1E keepalive ping
```

Strings are `u16 length + latin-1 bytes`; all integers little-endian.
