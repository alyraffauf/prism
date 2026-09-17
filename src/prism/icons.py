"""Generate PNG list icons."""

import base64
import hashlib
import re
import struct
import zlib

from .gems import gem_color
from .models.publication import Icon


def solid_png(color: str) -> bytes:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError("Icon colors must be six-digit hex values, such as #0F52BA")
    pixel = bytes.fromhex(color[1:])
    size = 256
    # Each PNG scanline begins with a zero byte selecting the 'None' filter.
    scanlines = (b"\x00" + pixel * size) * size

    def chunk(kind: bytes, contents: bytes) -> bytes:
        return (
            struct.pack(">I", len(contents))
            + kind
            + contents
            + struct.pack(">I", zlib.crc32(kind + contents))
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + chunk(b"IEND", b"")
    )


def gem_icon(name: str) -> Icon:
    color = gem_color(name)
    contents = solid_png(color)
    return {
        "color": color,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "png": base64.b64encode(contents).decode("ascii"),
    }
