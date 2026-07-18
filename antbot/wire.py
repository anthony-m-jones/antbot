"""Low-level wire primitives for the Tibia 8.60 legacy-classic transport.

Byte layout (both directions), as implemented by Canary's LegacyClassic
transport profile (canary/src/server/network/protocol/protocol_profile.cpp):

    [u16 outer length][u32 adler32][body]

- outer length counts everything after the length field (checksum included).
- adler32 covers everything after the checksum field.
- Once XTEA is enabled, body = XTEA(u16 inner length + payload + padding to 8);
  inner length counts payload bytes only.
- The client's first packet on a connection is never XTEA-encrypted and has no
  inner length; it carries an RSA-encrypted 128-byte block instead.
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from pathlib import Path

XTEA_DELTA = 0x9E3779B9
U32 = 0xFFFFFFFF

# The classic OpenTibia RSA public key (canary/key.pem ships the matching
# private key). load_modulus_from_pem() is the authoritative source; this
# constant is the documented fallback.
OTSERV_RSA_N = int(
    "1091201329673994292788609605089955415282375029027981291234687579"
    "3726629149257644633073969600111060390723088861007265581882535850"
    "3429057592827629436413108566029093628212635953836686562675849720"
    "6207862794310902180176810615217550567108238764764442605581471797"
    "07119674283982419152118103759076030616683978566631413"
)
OTSERV_RSA_E = 65537


def adler32(data: bytes) -> int:
    return zlib.adler32(data) & U32


def hexdump(data: bytes, width: int = 16, max_bytes: int | None = None) -> str:
    """Render bytes as a classic offset / hex / ASCII dump.

    This is our "capture oracle" for reverse-engineering the protocol: when a
    parser disagrees with reality, dumping the raw decrypted payload lets us line
    the bytes up against Canary's send-side source by eye. Purely diagnostic; it
    has no effect on the wire.

    Example line:
        0000  0a 10 00 00 40 32 00 64  70 7e 76 7e 07 ...   ....@2.dp~v~.

    - `width` bytes are shown per row.
    - `max_bytes` truncates very long payloads (e.g. the multi-KB login frame)
      so logs stay readable; None means dump everything.
    """
    if max_bytes is not None and len(data) > max_bytes:
        shown = data[:max_bytes]
        suffix = f"\n… ({len(data) - max_bytes} more bytes truncated)"
    else:
        shown = data
        suffix = ""

    lines = []
    for offset in range(0, len(shown), width):
        chunk = shown[offset : offset + width]
        # Hex column, with an extra gap after 8 bytes for readability.
        hex_parts = [f"{byte:02x}" for byte in chunk]
        if len(hex_parts) > 8:
            hex_parts.insert(8, "")
        hex_col = " ".join(hex_parts).ljust(width * 3)
        # ASCII column: printable bytes shown literally, others as a dot.
        ascii_col = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:04x}  {hex_col}  {ascii_col}")
    return "\n".join(lines) + suffix


# ---------------------------------------------------------------------------
# XTEA (OpenTibia flavour: 32 rounds, little-endian 32-bit halves)
# ---------------------------------------------------------------------------

def xtea_encrypt(data: bytes, key: tuple[int, int, int, int]) -> bytes:
    if len(data) % 8 != 0:
        raise ValueError(f"XTEA block size must be a multiple of 8, got {len(data)}")
    out = bytearray(len(data))
    for off in range(0, len(data), 8):
        v0, v1 = struct.unpack_from("<II", data, off)
        s = 0
        for _ in range(32):
            v0 = (v0 + (((((v1 << 4) & U32) ^ (v1 >> 5)) + v1) ^ (s + key[s & 3]))) & U32
            s = (s + XTEA_DELTA) & U32
            v1 = (v1 + (((((v0 << 4) & U32) ^ (v0 >> 5)) + v0) ^ (s + key[(s >> 11) & 3]))) & U32
        struct.pack_into("<II", out, off, v0, v1)
    return bytes(out)


def xtea_decrypt(data: bytes, key: tuple[int, int, int, int]) -> bytes:
    if len(data) % 8 != 0:
        raise ValueError(f"XTEA block size must be a multiple of 8, got {len(data)}")
    out = bytearray(len(data))
    for off in range(0, len(data), 8):
        v0, v1 = struct.unpack_from("<II", data, off)
        s = 0xC6EF3720
        for _ in range(32):
            v1 = (v1 - (((((v0 << 4) & U32) ^ (v0 >> 5)) + v0) ^ (s + key[(s >> 11) & 3]))) & U32
            s = (s - XTEA_DELTA) & U32
            v0 = (v0 - (((((v1 << 4) & U32) ^ (v1 >> 5)) + v1) ^ (s + key[s & 3]))) & U32
        struct.pack_into("<II", out, off, v0, v1)
    return bytes(out)


# ---------------------------------------------------------------------------
# RSA (encrypt-only; the server holds the private key)
# ---------------------------------------------------------------------------

def rsa_encrypt(block: bytes, n: int = OTSERV_RSA_N, e: int = OTSERV_RSA_E) -> bytes:
    if len(block) != 128:
        raise ValueError(f"RSA block must be exactly 128 bytes, got {len(block)}")
    if block[0] != 0:
        raise ValueError("first RSA plaintext byte must be 0")
    m = int.from_bytes(block, "big")
    c = pow(m, e, n)
    return c.to_bytes(128, "big")


def load_modulus_from_pem(path: str | Path) -> tuple[int, int]:
    """Extract (n, e) from a PKCS#1 'RSA PRIVATE KEY' PEM (like canary/key.pem)."""
    text = Path(path).read_text()
    b64 = "".join(line.strip() for line in text.splitlines() if not line.startswith("-"))
    der = base64.b64decode(b64)

    def read_tlv(buf: bytes, idx: int) -> tuple[int, int, int]:
        tag = buf[idx]
        idx += 1
        length = buf[idx]
        idx += 1
        if length & 0x80:
            n_len = length & 0x7F
            length = int.from_bytes(buf[idx : idx + n_len], "big")
            idx += n_len
        return tag, length, idx

    tag, _, idx = read_tlv(der, 0)  # outer SEQUENCE
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")

    values = []
    for _ in range(3):  # version, n, e
        tag, length, idx = read_tlv(der, idx)
        if tag != 0x02:
            raise ValueError("expected DER INTEGER")
        values.append(int.from_bytes(der[idx : idx + length], "big"))
        idx += length

    return values[1], values[2]


# ---------------------------------------------------------------------------
# Message reader / writer
# ---------------------------------------------------------------------------

class MessageWriter:
    def __init__(self) -> None:
        self._buf = bytearray()

    def u8(self, value: int) -> "MessageWriter":
        self._buf.append(value & 0xFF)
        return self

    def u16(self, value: int) -> "MessageWriter":
        self._buf += struct.pack("<H", value & 0xFFFF)
        return self

    def u32(self, value: int) -> "MessageWriter":
        self._buf += struct.pack("<I", value & U32)
        return self

    def string(self, value: str) -> "MessageWriter":
        raw = value.encode("latin-1")
        self.u16(len(raw))
        self._buf += raw
        return self

    def raw(self, data: bytes) -> "MessageWriter":
        self._buf += data
        return self

    def bytes(self) -> bytes:
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


class MessageReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def eof(self) -> bool:
        return self._pos >= len(self._data)

    def u8(self) -> int:
        value = self._data[self._pos]
        self._pos += 1
        return value

    def u16(self) -> int:
        (value,) = struct.unpack_from("<H", self._data, self._pos)
        self._pos += 2
        return value

    def u32(self) -> int:
        (value,) = struct.unpack_from("<I", self._data, self._pos)
        self._pos += 4
        return value

    def string(self) -> str:
        length = self.u16()
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        return raw.decode("latin-1")

    def skip(self, count: int) -> None:
        self._pos += count

    @property
    def pos(self) -> int:
        """Bytes consumed so far — lets a caller resume parsing the remainder."""
        return self._pos


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def frame_plain(body: bytes) -> bytes:
    """Frame a client 'first packet' (no XTEA, no inner length)."""
    checksum = adler32(body)
    return struct.pack("<HI", len(body) + 4, checksum) + body


def frame_xtea(payload: bytes, key: tuple[int, int, int, int]) -> bytes:
    """Frame a client packet after XTEA is enabled (legacy inner length)."""
    inner = struct.pack("<H", len(payload)) + payload
    padding = (-len(inner)) % 8
    inner += bytes(padding)
    encrypted = xtea_encrypt(inner, key)
    checksum = adler32(encrypted)
    return struct.pack("<HI", len(encrypted) + 4, checksum) + encrypted


def unframe_xtea(blob: bytes, key: tuple[int, int, int, int]) -> bytes:
    """Decrypt a received body (after its adler prefix) into the payload."""
    decrypted = xtea_decrypt(blob, key)
    (inner,) = struct.unpack_from("<H", decrypted, 0)
    if inner + 2 > len(decrypted):
        raise ValueError(f"invalid inner length {inner} for {len(decrypted)}-byte block")
    return decrypted[2 : 2 + inner]


def random_xtea_key() -> tuple[int, int, int, int]:
    raw = os.urandom(16)
    return struct.unpack("<IIII", raw)  # type: ignore[return-value]


def build_rsa_block(content: bytes) -> bytes:
    """Pad RSA plaintext content (without leading zero) to the 128-byte block."""
    if len(content) > 127:
        raise ValueError(f"RSA content too large: {len(content)} > 127")
    return bytes([0]) + content + os.urandom(127 - len(content))
