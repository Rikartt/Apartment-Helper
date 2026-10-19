"""Generate the PWA icons (red "WG" on near-black) with the standard library only.
Run: python tools/make_icons.py   -> frontend/icons/icon-192.png, icon-512.png, icon-maskable-512.png
"""
import struct
import zlib
from pathlib import Path

BG, FG = (17, 17, 17), (204, 0, 0)
GLYPHS = {
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
}


def render(size: int, mark_frac: float) -> bytes:
    """mark_frac = width of the WG mark relative to the icon (kept < 0.8 so it survives masking)."""
    text = "WG"
    cols = len(text) * 5 + (len(text) - 1)          # 1 column gap
    cell = max(1, int(size * mark_frac / cols))
    w, h = cols * cell, 7 * cell
    x0, y0 = (size - w) // 2, (size - h) // 2
    rows = []
    for y in range(size):
        row = bytearray([0])                         # PNG filter byte
        for x in range(size):
            c = BG
            gx, gy = x - x0, y - y0
            if 0 <= gx < w and 0 <= gy < h:
                col, r = gx // cell, gy // cell
                letter, lc = divmod(col, 6)
                if lc < 5 and GLYPHS[text[letter]][r][lc] == "1":
                    c = FG
            row += bytes(c)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "frontend" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    (out / "icon-192.png").write_bytes(render(192, 0.70))
    (out / "icon-512.png").write_bytes(render(512, 0.70))
    (out / "icon-maskable-512.png").write_bytes(render(512, 0.50))   # inside the 80% safe zone
