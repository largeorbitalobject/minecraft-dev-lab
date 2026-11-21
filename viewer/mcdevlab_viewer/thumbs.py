"""Thumbnails for PNG screenshots.

Uses Pillow when the host has it (JPEG, a few KB each). Without Pillow a small pure-Python PNG decoder and
box-filter downscaler is used and the thumbnail is a PNG. Server code never imports this; only the collector.
"""
from __future__ import annotations

import io
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"

try:  # optional
    from PIL import Image  # type: ignore
    HAVE_PIL = True
except Exception:  # pragma: no cover
    Image = None
    HAVE_PIL = False


def png_size(data: bytes):
    """(width, height) from the IHDR chunk, or None."""
    if len(data) < 24 or data[:8] != PNG_SIG or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes):
    """Decode 8-bit non-interlaced PNG -> (width, height, channels, rows[list of bytearray]). Raises ValueError."""
    if data[:8] != PNG_SIG:
        raise ValueError("not a PNG")
    pos = 8
    idat = []
    plte = None
    w = h = depth = ctype = interlace = None
    while pos + 8 <= len(data):
        ln, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if kind == b"IHDR":
            w, h, depth, ctype, _comp, _flt, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            plte = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6):
        raise ValueError("unsupported PNG (depth=%s ctype=%s interlace=%s)" % (depth, ctype, interlace))
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    rows = []
    prev = bytearray(stride)
    off = 0
    for _ in range(h):
        f = raw[off]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                c = prev[i - ch] if i >= ch else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 255
        elif f != 0:
            raise ValueError("bad filter")
        rows.append(line)
        prev = line
    if ctype == 3:  # palette -> RGB
        pal = [plte[i:i + 3] for i in range(0, len(plte), 3)]
        rows = [bytearray(b"".join(pal[v] for v in r)) for r in rows]
        ch = 3
    return w, h, ch, rows


def _encode_png(w, h, ch, rows):
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[ch]
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _downscale_pure(png: bytes, max_w: int):
    w, h, ch, rows = decode_png(png)
    k = max(1, w // max_w)
    nw, nh = w // k, h // k
    out = []
    for y in range(nh):
        block = rows[y * k:(y + 1) * k]
        row = bytearray(nw * ch)
        for x in range(nw):
            for c in range(ch):
                if k == 1:
                    row[x * ch + c] = block[0][x * ch + c]
                else:
                    tot = 0
                    for r in block:
                        for dx in range(k):
                            tot += r[(x * k + dx) * ch + c]
                    row[x * ch + c] = tot // (k * k)
        out.append(row)
    return nw, nh, ch, out


def make_thumb(png: bytes, max_w: int = 360, force_pure: bool = False):
    """-> (bytes, ext, (orig_w, orig_h)). Raises ValueError for undecodable input."""
    size = png_size(png)
    if size is None:
        raise ValueError("not a PNG")
    if HAVE_PIL and not force_pure:
        im = Image.open(io.BytesIO(png))
        im.load()
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (8, 16, 47))
            if im.mode in ("RGBA", "LA"):
                bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[3])
                im = bg
            else:
                im = im.convert("RGB")
        ratio = max_w / float(im.width)
        if ratio < 1:
            im = im.resize((max_w, max(1, int(im.height * ratio))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=78, optimize=True)
        return buf.getvalue(), "jpg", size
    nw, nh, ch, rows = _downscale_pure(png, max_w)
    return _encode_png(nw, nh, ch, rows), "png", size
