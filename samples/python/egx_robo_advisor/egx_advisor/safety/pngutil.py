"""Minimal PNG reader for the deterministic badge probes.

Pillow is a fine dependency for an application, but the demo-mode guard is the
one component that must never fail to load: if it cannot run, the bot must not
click. Keeping its pixel path on zlib and the standard library means the guard
(and its tests) work on a bare interpreter, and Pillow becomes an optional
accelerator rather than a prerequisite for safety.

Supports 8-bit truecolour with and without alpha, which is what every
screenshot path in cua-computer produces.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True, slots=True)
class RgbImage:
    """Decoded image as tightly packed RGB triples."""

    width: int
    height: int
    pixels: bytes  # length == width * height * 3

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) outside {self.width}x{self.height}")
        offset = (y * self.width + x) * 3
        return (
            self.pixels[offset],
            self.pixels[offset + 1],
            self.pixels[offset + 2],
        )


def decode_png(data: bytes) -> RgbImage:
    """Decode an 8-bit truecolour PNG. Raises ValueError on anything else."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("not a PNG stream")

    pos = len(PNG_MAGIC)
    header: tuple[int, int, int, int, int, int, int] | None = None
    idat = bytearray()

    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length  # 4 len + 4 type + payload + 4 crc

        if ctype == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break

    if header is None:
        raise ValueError("PNG has no IHDR chunk")

    width, height, depth, colour, compression, filter_method, interlace = header
    if depth != 8:
        raise ValueError(f"only 8-bit PNGs are supported, got {depth}-bit")
    if colour not in (2, 6):
        raise ValueError(f"only truecolour PNGs are supported, got colour type {colour}")
    if interlace != 0:
        raise ValueError("interlaced PNGs are not supported")
    if compression != 0 or filter_method != 0:
        raise ValueError("unsupported PNG compression or filter method")

    channels = 3 if colour == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        raise ValueError("truncated PNG image data")

    out = bytearray(width * height * 3)
    previous = bytearray(stride)
    offset = 0
    for row in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        _unfilter(line, previous, filter_type, channels)

        dest = row * width * 3
        if channels == 3:
            out[dest : dest + stride] = line
        else:
            for px in range(width):
                src = px * 4
                out[dest + px * 3 : dest + px * 3 + 3] = line[src : src + 3]
        previous = line

    return RgbImage(width=width, height=height, pixels=bytes(out))


def _unfilter(line: bytearray, previous: bytearray, filter_type: int, bpp: int) -> None:
    """Reverse a PNG scanline filter in place (RFC 2083 section 6)."""
    if filter_type == 0:
        return
    if filter_type == 1:  # Sub
        for i in range(bpp, len(line)):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif filter_type == 2:  # Up
        for i in range(len(line)):
            line[i] = (line[i] + previous[i]) & 0xFF
    elif filter_type == 3:  # Average
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            up = previous[i]
            up_left = previous[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF
    else:
        raise ValueError(f"unknown PNG filter type {filter_type}")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def encode_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode packed RGB triples as an unfiltered PNG. Used by the tests."""
    if len(pixels) != width * height * 3:
        raise ValueError("pixel buffer does not match dimensions")
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type: None
        raw += pixels[row * width * 3 : (row + 1) * width * 3]

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    return (
        PNG_MAGIC
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
